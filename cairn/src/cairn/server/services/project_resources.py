"""项目资源、临时导入批次和安全 ZIP 解包。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
from pathlib import Path
import shutil
import stat
from typing import Any, AsyncIterator, Sequence
from uuid import uuid4
import zipfile

from fastapi import HTTPException

from cairn.server.db import get_conn
from cairn.server.models import (
    CreateProjectResourceRequest,
    ProjectResource,
    ProjectResourceBatch,
    ProjectResourceBatchMember,
    ProjectResourceConfig,
    ProjectResourceLimits,
    UpdateProjectResourceBatchMemberRequest,
)

from .ids import get_project_or_404, next_project_resource_id
from .time_utils import utcnow

PROJECT_RESOURCE_ROOT_ENV = "CAIRN_PROJECT_RESOURCE_ROOT"
DEFAULT_PROJECT_RESOURCE_ROOT = "datas/cairn/project-resources"
PROJECT_RESOURCE_MAX_FILE_BYTES = 10 * 1024 * 1024
PROJECT_RESOURCE_MAX_COUNT = 200
PROJECT_RESOURCE_MAX_TOTAL_BYTES = 100 * 1024 * 1024
PROJECT_RESOURCE_MAX_ZIP_UPLOAD_BYTES = 50 * 1024 * 1024
PROJECT_RESOURCE_MAX_ZIP_ENTRIES = 1000
PROJECT_RESOURCE_MAX_COMPRESSION_RATIO = 100
PROJECT_RESOURCE_MAX_OPEN_BATCHES_PER_USER = 4
PROJECT_RESOURCE_MAX_STAGED_BYTES_PER_USER = 200 * 1024 * 1024
PROJECT_RESOURCE_BATCH_TTL_SECONDS = 24 * 60 * 60
PROJECT_RESOURCE_MAX_PATH_BYTES = 1024
PROJECT_RESOURCE_MAX_SEGMENT_BYTES = 255
PROJECT_RESOURCE_RESERVED_NAMES = {"manifest.json"}
PROJECT_RESOURCE_KINDS = ("note", "script", "file")
PROJECT_RESOURCE_SCRIPT_EXTENSIONS = {
    ".bash", ".js", ".mjs", ".ps1", ".py", ".rb", ".sh", ".ts", ".zsh",
}
PROJECT_RESOURCE_SCRIPT_NAMES = {"makefile"}
PROJECT_RESOURCE_TEXT_EXTENSIONS = {
    ".conf", ".csv", ".env", ".ini", ".json", ".log", ".md", ".rst", ".toml",
    ".tsv", ".txt", ".xml", ".yaml", ".yml",
}


@dataclass(frozen=True, slots=True)
class ProjectResourceContent:
    content: bytes
    path: str


def _positive_env(name: str, fallback: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return fallback
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return parsed


def project_resource_limits() -> ProjectResourceLimits:
    return ProjectResourceLimits(
        max_active_count=_positive_env(
            "CAIRN_PROJECT_RESOURCE_MAX_ACTIVE_COUNT", PROJECT_RESOURCE_MAX_COUNT
        ),
        max_file_bytes=_positive_env(
            "CAIRN_PROJECT_RESOURCE_MAX_FILE_BYTES", PROJECT_RESOURCE_MAX_FILE_BYTES
        ),
        max_active_total_bytes=_positive_env(
            "CAIRN_PROJECT_RESOURCE_MAX_ACTIVE_TOTAL_BYTES",
            PROJECT_RESOURCE_MAX_TOTAL_BYTES,
        ),
        max_zip_upload_bytes=_positive_env(
            "CAIRN_PROJECT_RESOURCE_MAX_ZIP_UPLOAD_BYTES",
            PROJECT_RESOURCE_MAX_ZIP_UPLOAD_BYTES,
        ),
        max_zip_entries=_positive_env(
            "CAIRN_PROJECT_RESOURCE_MAX_ZIP_ENTRIES", PROJECT_RESOURCE_MAX_ZIP_ENTRIES
        ),
        max_compression_ratio=_positive_env(
            "CAIRN_PROJECT_RESOURCE_MAX_COMPRESSION_RATIO",
            PROJECT_RESOURCE_MAX_COMPRESSION_RATIO,
        ),
        max_open_batches_per_user=_positive_env(
            "CAIRN_PROJECT_RESOURCE_MAX_OPEN_BATCHES_PER_USER",
            PROJECT_RESOURCE_MAX_OPEN_BATCHES_PER_USER,
        ),
        max_staged_bytes_per_user=_positive_env(
            "CAIRN_PROJECT_RESOURCE_MAX_STAGED_BYTES_PER_USER",
            PROJECT_RESOURCE_MAX_STAGED_BYTES_PER_USER,
        ),
        max_path_bytes=_positive_env(
            "CAIRN_PROJECT_RESOURCE_MAX_PATH_BYTES", PROJECT_RESOURCE_MAX_PATH_BYTES
        ),
        max_path_segment_bytes=_positive_env(
            "CAIRN_PROJECT_RESOURCE_MAX_PATH_SEGMENT_BYTES",
            PROJECT_RESOURCE_MAX_SEGMENT_BYTES,
        ),
    )


def project_resource_batch_ttl_seconds() -> int:
    return _positive_env(
        "CAIRN_PROJECT_RESOURCE_BATCH_TTL_SECONDS",
        PROJECT_RESOURCE_BATCH_TTL_SECONDS,
    )


def project_resource_config() -> ProjectResourceConfig:
    return ProjectResourceConfig(
        kinds=list(PROJECT_RESOURCE_KINDS),
        limits=project_resource_limits(),
        reserved_names=sorted(PROJECT_RESOURCE_RESERVED_NAMES),
        batch_ttl_seconds=project_resource_batch_ttl_seconds(),
        zip_policy={
            "allowed_compression_methods": ["stored", "deflated"],
            "nested_archives": "opaque",
            "preserve_relative_directories": True,
        },
    )


def project_resource_root() -> Path:
    configured = os.environ.get(PROJECT_RESOURCE_ROOT_ENV, "").strip()
    return Path(configured or DEFAULT_PROJECT_RESOURCE_ROOT).expanduser().resolve()


def infer_project_resource_kind(path: str) -> str:
    """按文件名推断展示类型，未知或二进制格式统一作为 file。"""
    name = Path(path).name.casefold()
    if name in PROJECT_RESOURCE_SCRIPT_NAMES:
        return "script"
    suffix = Path(name).suffix
    if suffix in PROJECT_RESOURCE_SCRIPT_EXTENSIONS:
        return "script"
    if suffix in PROJECT_RESOURCE_TEXT_EXTENSIONS:
        return "note"
    return "file"


def validate_project_resource_path(value: str) -> str:
    """校验逻辑相对路径，但不改写用户提供的合法路径。"""
    limits = project_resource_limits()
    if not isinstance(value, str) or not value or not value.strip():
        raise ValueError("path must not be empty")
    if value.startswith("/") or "\\" in value:
        raise ValueError("path must be a safe relative POSIX path")
    if len(value.encode("utf-8")) > limits.max_path_bytes:
        raise ValueError(f"path must not exceed {limits.max_path_bytes} UTF-8 bytes")
    segments = value.split("/")
    if len(segments[0]) == 2 and segments[0][0].isalpha() and segments[0][1] == ":":
        raise ValueError("path must not contain a Windows drive prefix")
    for segment in segments:
        if not segment or segment in {".", ".."}:
            raise ValueError("path contains an invalid segment")
        if len(segment.encode("utf-8")) > limits.max_path_segment_bytes:
            raise ValueError(
                f"path segment must not exceed {limits.max_path_segment_bytes} UTF-8 bytes"
            )
        if segment.casefold() in PROJECT_RESOURCE_RESERVED_NAMES:
            raise ValueError(f"path name is reserved: {segment}")
        if any(ord(character) < 32 or ord(character) == 127 for character in segment):
            raise ValueError("path contains control characters")
    return value


def _path_collision_key(path: str) -> str:
    return path.casefold()


def _paths_collide(first: str, second: str) -> bool:
    """拒绝同名以及“文件同时作为目录”的祖先路径冲突。"""
    first_key = _path_collision_key(first)
    second_key = _path_collision_key(second)
    return (
        first_key == second_key
        or first_key.startswith(f"{second_key}/")
        or second_key.startswith(f"{first_key}/")
    )


def _find_path_collision(path: str, existing: Sequence[str]) -> str | None:
    return next((item for item in existing if _paths_collide(path, item)), None)


def _path_collision_error(path: str, collision: str) -> HTTPException:
    if _path_collision_key(path) == _path_collision_key(collision):
        return HTTPException(409, f"Project resource path already exists: {collision}")
    return HTTPException(409, f"Project resource path conflicts with: {collision}")


def project_resource_to_model(row: Any) -> ProjectResource:
    return ProjectResource(
        id=row["id"],
        project_id=row["project_id"],
        kind=row["kind"],
        scope=row["scope"],
        path=row["path"],
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        active=bool(row["active"]),
        created_by=row["created_by"],
        created_at=row["created_at"],
        deactivated_at=row["deactivated_at"],
    )


def _batch_member_to_model(row: Any) -> ProjectResourceBatchMember:
    return ProjectResourceBatchMember(
        id=row["id"],
        kind=row["kind"],
        path=row["path"],
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        source=row["source"],
    )


def _batch_to_model(conn: Any, row: Any) -> ProjectResourceBatch:
    members = conn.execute(
        """
        SELECT * FROM project_resource_batch_members
        WHERE batch_id = ? ORDER BY path, id
        """,
        (row["id"],),
    ).fetchall()
    models = [_batch_member_to_model(member) for member in members]
    return ProjectResourceBatch(
        id=row["id"],
        state=row["state"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        resource_count=len(models),
        total_bytes=sum(member.size_bytes for member in models),
        members=models,
    )


def list_project_resource_records(
    project_id: str,
    *,
    active_only: bool = True,
    conn: Any | None = None,
) -> list[ProjectResource]:
    if conn is not None:
        return _list_project_resource_records(conn, project_id, active_only=active_only)
    with get_conn() as owned_conn:
        return _list_project_resource_records(owned_conn, project_id, active_only=active_only)


def _list_project_resource_records(
    conn: Any,
    project_id: str,
    *,
    active_only: bool,
) -> list[ProjectResource]:
    get_project_or_404(conn, project_id)
    active_clause = "AND active = 1" if active_only else ""
    rows = conn.execute(
        f"""
        SELECT * FROM project_resources
        WHERE project_id = ? {active_clause}
        ORDER BY created_at, id
        """,
        (project_id,),
    ).fetchall()
    return [project_resource_to_model(row) for row in rows]


def read_project_resource_content(project_id: str, resource_id: str) -> ProjectResourceContent:
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        row = conn.execute(
            "SELECT * FROM project_resources WHERE id = ? AND project_id = ?",
            (resource_id, project_id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, f"Project resource {resource_id} not found")

    disk_path = _project_resource_disk_path(project_id, resource_id)
    if not disk_path.is_file():
        raise HTTPException(404, f"Project resource {resource_id} content not found")
    content = disk_path.read_bytes()
    if len(content) != row["size_bytes"] or sha256(content).hexdigest() != row["sha256"]:
        raise HTTPException(409, f"Project resource {resource_id} content integrity check failed")
    return ProjectResourceContent(content=content, path=row["path"])


def create_project_resource_record(
    project_id: str,
    body: CreateProjectResourceRequest,
    *,
    created_by: str,
) -> ProjectResource:
    with get_conn() as conn:
        _lock_and_check_project_resource_mutation(conn, project_id)
        return _insert_project_resource_records(
            conn,
            project_id,
            [body],
            created_by=created_by,
        )[0]


def create_initial_project_resource_records(
    conn: Any,
    project_id: str,
    bodies: Sequence[CreateProjectResourceRequest],
    *,
    created_by: str,
) -> list[ProjectResource]:
    """项目创建事务内写入旧版 JSON 资源。"""
    get_project_or_404(conn, project_id)
    return _insert_project_resource_records(conn, project_id, bodies, created_by=created_by)


def _insert_project_resource_records(
    conn: Any,
    project_id: str,
    bodies: Sequence[CreateProjectResourceRequest],
    *,
    created_by: str,
) -> list[ProjectResource]:
    prepared = [
        (body.kind, validate_project_resource_path(body.path), body.content.encode("utf-8"))
        for body in bodies
    ]
    return _insert_prepared_project_resources(conn, project_id, prepared, created_by=created_by)


def _check_resource_quota_and_collisions(
    conn: Any,
    project_id: str,
    prepared: Sequence[tuple[str, str, int]],
) -> None:
    limits = project_resource_limits()
    prepared_paths: list[str] = []
    for _kind, path, size_bytes in prepared:
        if size_bytes > limits.max_file_bytes:
            raise HTTPException(413, f"Project resource exceeds the {limits.max_file_bytes}-byte file limit")
        collision = _find_path_collision(path, prepared_paths)
        if collision is not None:
            raise _path_collision_error(path, collision)
        prepared_paths.append(path)

    existing = conn.execute(
        """
        SELECT path, size_bytes, active FROM project_resources
        WHERE project_id = ?
        """,
        (project_id,),
    ).fetchall()
    active_existing = [row for row in existing if bool(row["active"])]
    existing_paths = [row["path"] for row in existing]
    for path in prepared_paths:
        collision = _find_path_collision(path, existing_paths)
        if collision is not None:
            raise _path_collision_error(path, collision)
    if len(active_existing) + len(prepared) > limits.max_active_count:
        raise HTTPException(409, f"Project resources exceed the maximum {limits.max_active_count} active resources")
    total = sum(int(row["size_bytes"]) for row in active_existing) + sum(item[2] for item in prepared)
    if total > limits.max_active_total_bytes:
        raise HTTPException(413, f"Project resources exceed the {limits.max_active_total_bytes}-byte total limit")


def _insert_prepared_project_resources(
    conn: Any,
    project_id: str,
    prepared: Sequence[tuple[str, str, bytes]],
    *,
    created_by: str,
) -> list[ProjectResource]:
    _check_resource_quota_and_collisions(
        conn,
        project_id,
        [(kind, path, len(content)) for kind, path, content in prepared],
    )
    created_paths: list[Path] = []
    resources: list[ProjectResource] = []
    try:
        for kind, path, content in prepared:
            resource_id = next_project_resource_id(conn, project_id)
            digest = sha256(content).hexdigest()
            now = utcnow()
            disk_path = _project_resource_disk_path(project_id, resource_id, create_parent=True)
            with disk_path.open("xb") as resource_file:
                resource_file.write(content)
            created_paths.append(disk_path)
            conn.execute(
                """
                INSERT INTO project_resources (
                    id, project_id, kind, scope, path, size_bytes, sha256,
                    active, created_by, created_at, deactivated_at
                ) VALUES (?, ?, ?, 'project', ?, ?, ?, 1, ?, ?, NULL)
                """,
                (resource_id, project_id, kind, path, len(content), digest, created_by, now),
            )
            row = conn.execute(
                "SELECT * FROM project_resources WHERE id = ? AND project_id = ?",
                (resource_id, project_id),
            ).fetchone()
            assert row is not None
            resources.append(project_resource_to_model(row))
        return resources
    except Exception:
        for disk_path in reversed(created_paths):
            disk_path.unlink(missing_ok=True)
        raise


def deactivate_project_resource_record(project_id: str, resource_id: str) -> None:
    with get_conn() as conn:
        _lock_and_check_project_resource_mutation(conn, project_id)
        row = conn.execute(
            "SELECT active FROM project_resources WHERE id = ? AND project_id = ?",
            (resource_id, project_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"Project resource {resource_id} not found")
        if not bool(row["active"]):
            return
        conn.execute(
            """
            UPDATE project_resources SET active = 0, deactivated_at = ?
            WHERE id = ? AND project_id = ? AND active = 1
            """,
            (utcnow(), resource_id, project_id),
        )


def create_project_resource_batch(*, tenant_id: str, user_id: str) -> ProjectResourceBatch:
    if not tenant_id:
        raise HTTPException(403, "Tenant context required")
    cleanup_expired_project_resource_batches()
    batch_id = f"prb_{uuid4().hex}"
    created = datetime.now(timezone.utc)
    expires = created + timedelta(seconds=project_resource_batch_ttl_seconds())
    with get_conn() as conn:
        # 用户行锁串行化同一账号的批次创建与跨批次暂存配额检查。
        conn.execute("UPDATE users SET id = id WHERE id = ?", (user_id,))
        limits = project_resource_limits()
        open_count = conn.execute(
            """
            SELECT COUNT(*) AS value FROM project_resource_batches
            WHERE tenant_id = ? AND user_id = ? AND state = 'open'
            """,
            (tenant_id, user_id),
        ).fetchone()["value"]
        if int(open_count) >= limits.max_open_batches_per_user:
            raise HTTPException(
                409,
                f"User already has {limits.max_open_batches_per_user} open project resource batches",
            )
        conn.execute(
            """
            INSERT INTO project_resource_batches
                (id, tenant_id, user_id, state, created_at, expires_at)
            VALUES (?, ?, ?, 'open', ?, ?)
            """,
            (batch_id, tenant_id, user_id, _format_time(created), _format_time(expires)),
        )
        row = conn.execute("SELECT * FROM project_resource_batches WHERE id = ?", (batch_id,)).fetchone()
        assert row is not None
        return _batch_to_model(conn, row)


def get_project_resource_batch(batch_id: str, *, tenant_id: str, user_id: str) -> ProjectResourceBatch:
    cleanup_expired_project_resource_batches()
    with get_conn() as conn:
        row = _get_owned_batch(conn, batch_id, tenant_id=tenant_id, user_id=user_id)
        return _batch_to_model(conn, row)


async def upload_project_resource_batch_file(
    batch_id: str,
    stream: AsyncIterator[bytes],
    *,
    path: str,
    kind: str | None,
    tenant_id: str,
    user_id: str,
) -> ProjectResourceBatchMember:
    path = validate_project_resource_path(path)
    kind = kind or infer_project_resource_kind(path)
    if kind not in PROJECT_RESOURCE_KINDS:
        raise HTTPException(422, "Unsupported project resource kind")
    get_project_resource_batch(batch_id, tenant_id=tenant_id, user_id=user_id)
    limits = project_resource_limits()
    temp_path = _batch_directory(batch_id, create=True) / f".upload-{uuid4().hex}"
    digest = sha256()
    size_bytes = 0
    try:
        with temp_path.open("xb") as output:
            async for chunk in stream:
                if not chunk:
                    continue
                size_bytes += len(chunk)
                if size_bytes > limits.max_file_bytes:
                    raise HTTPException(413, f"Project resource exceeds the {limits.max_file_bytes}-byte file limit")
                output.write(chunk)
                digest.update(chunk)
        with get_conn() as conn:
            batch = _lock_owned_open_batch(conn, batch_id, tenant_id=tenant_id, user_id=user_id)
            _check_batch_additions(conn, batch["id"], [(path, size_bytes)])
            member_id = f"prm_{uuid4().hex}"
            final_path = _batch_member_disk_path(batch_id, member_id, create_parent=True)
            os.replace(temp_path, final_path)
            try:
                conn.execute(
                    """
                    INSERT INTO project_resource_batch_members
                        (id, batch_id, kind, path, size_bytes, sha256, source, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'file', ?)
                    """,
                    (member_id, batch_id, kind, path, size_bytes, digest.hexdigest(), utcnow()),
                )
            except Exception:
                final_path.unlink(missing_ok=True)
                raise
            row = conn.execute(
                "SELECT * FROM project_resource_batch_members WHERE id = ? AND batch_id = ?",
                (member_id, batch_id),
            ).fetchone()
            assert row is not None
            return _batch_member_to_model(row)
    finally:
        temp_path.unlink(missing_ok=True)


async def import_project_resource_batch_zip(
    batch_id: str,
    stream: AsyncIterator[bytes],
    *,
    tenant_id: str,
    user_id: str,
) -> ProjectResourceBatch:
    get_project_resource_batch(batch_id, tenant_id=tenant_id, user_id=user_id)
    limits = project_resource_limits()
    directory = _batch_directory(batch_id, create=True)
    archive_path = directory / f".archive-{uuid4().hex}.zip"
    upload_bytes = 0
    try:
        with archive_path.open("xb") as output:
            async for chunk in stream:
                if not chunk:
                    continue
                upload_bytes += len(chunk)
                if upload_bytes > limits.max_zip_upload_bytes:
                    raise HTTPException(413, f"ZIP upload exceeds the {limits.max_zip_upload_bytes}-byte limit")
                output.write(chunk)
        prepared = _preflight_zip(archive_path, limits)
        with get_conn() as conn:
            batch = _lock_owned_open_batch(conn, batch_id, tenant_id=tenant_id, user_id=user_id)
            _check_batch_additions(conn, batch["id"], [(path, info.file_size) for path, info in prepared])
            extracted: list[tuple[str, str, int, str, Path]] = []
            try:
                with zipfile.ZipFile(archive_path) as archive:
                    for path, info in prepared:
                        member_id = f"prm_{uuid4().hex}"
                        temp_member = directory / f".member-{uuid4().hex}"
                        digest = sha256()
                        size_bytes = 0
                        with archive.open(info, "r") as source, temp_member.open("xb") as output:
                            while True:
                                chunk = source.read(64 * 1024)
                                if not chunk:
                                    break
                                size_bytes += len(chunk)
                                if size_bytes > info.file_size or size_bytes > limits.max_file_bytes:
                                    raise HTTPException(400, f"ZIP entry size changed during extraction: {path}")
                                output.write(chunk)
                                digest.update(chunk)
                        if size_bytes != info.file_size:
                            raise HTTPException(400, f"ZIP entry size changed during extraction: {path}")
                        extracted.append((member_id, path, size_bytes, digest.hexdigest(), temp_member))
                for member_id, path, size_bytes, digest_hex, temp_member in extracted:
                    final_path = _batch_member_disk_path(batch_id, member_id, create_parent=True)
                    os.replace(temp_member, final_path)
                    conn.execute(
                        """
                        INSERT INTO project_resource_batch_members
                            (id, batch_id, kind, path, size_bytes, sha256, source, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, 'zip', ?)
                        """,
                        (
                            member_id,
                            batch_id,
                            infer_project_resource_kind(path),
                            path,
                            size_bytes,
                            digest_hex,
                            utcnow(),
                        ),
                    )
            except Exception:
                for member_id, _path, _size, _digest, temp_member in extracted:
                    temp_member.unlink(missing_ok=True)
                    _batch_member_disk_path(batch_id, member_id).unlink(missing_ok=True)
                raise
            refreshed = conn.execute(
                "SELECT * FROM project_resource_batches WHERE id = ?", (batch_id,)
            ).fetchone()
            assert refreshed is not None
            return _batch_to_model(conn, refreshed)
    except (zipfile.BadZipFile, RuntimeError, EOFError) as exc:
        raise HTTPException(400, "Invalid or unsupported ZIP archive") from exc
    finally:
        archive_path.unlink(missing_ok=True)


def update_project_resource_batch_member(
    batch_id: str,
    member_id: str,
    body: UpdateProjectResourceBatchMemberRequest,
    *,
    tenant_id: str,
    user_id: str,
) -> ProjectResourceBatchMember:
    with get_conn() as conn:
        _lock_owned_open_batch(conn, batch_id, tenant_id=tenant_id, user_id=user_id)
        row = conn.execute(
            "SELECT * FROM project_resource_batch_members WHERE id = ? AND batch_id = ?",
            (member_id, batch_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Project resource batch member not found")
        path = body.path if body.path is not None else row["path"]
        kind = body.kind if body.kind is not None else row["kind"]
        if body.path is not None:
            others = conn.execute(
                "SELECT id, path FROM project_resource_batch_members WHERE batch_id = ? AND id <> ?",
                (batch_id, member_id),
            ).fetchall()
            collision = _find_path_collision(path, [item["path"] for item in others])
            if collision is not None:
                raise _path_collision_error(path, collision)
        conn.execute(
            "UPDATE project_resource_batch_members SET path = ?, kind = ? WHERE id = ? AND batch_id = ?",
            (path, kind, member_id, batch_id),
        )
        updated = conn.execute(
            "SELECT * FROM project_resource_batch_members WHERE id = ? AND batch_id = ?",
            (member_id, batch_id),
        ).fetchone()
        assert updated is not None
        return _batch_member_to_model(updated)


def delete_project_resource_batch_member(
    batch_id: str,
    member_id: str,
    *,
    tenant_id: str,
    user_id: str,
) -> None:
    with get_conn() as conn:
        _lock_owned_open_batch(conn, batch_id, tenant_id=tenant_id, user_id=user_id)
        deleted = conn.execute(
            "DELETE FROM project_resource_batch_members WHERE id = ? AND batch_id = ?",
            (member_id, batch_id),
        )
        if deleted.rowcount != 1:
            raise HTTPException(404, "Project resource batch member not found")
    _batch_member_disk_path(batch_id, member_id).unlink(missing_ok=True)


def delete_project_resource_batch(batch_id: str, *, tenant_id: str, user_id: str) -> None:
    with get_conn() as conn:
        row = _get_owned_batch(conn, batch_id, tenant_id=tenant_id, user_id=user_id)
        conn.execute("DELETE FROM project_resource_batches WHERE id = ?", (row["id"],))
    shutil.rmtree(_batch_directory(batch_id), ignore_errors=True)


def commit_project_resource_batch(
    project_id: str,
    batch_id: str,
    *,
    tenant_id: str,
    user_id: str,
    created_by: str,
    allow_cross_tenant: bool = False,
) -> list[ProjectResource]:
    with get_conn() as conn:
        resources = consume_project_resource_batch(
            conn,
            project_id,
            batch_id,
            tenant_id=tenant_id,
            user_id=user_id,
            created_by=created_by,
            require_stopped=True,
            allow_cross_tenant=allow_cross_tenant,
        )
    delete_project_resource_batch_staging(batch_id)
    return resources


def consume_project_resource_batch(
    conn: Any,
    project_id: str,
    batch_id: str,
    *,
    tenant_id: str,
    user_id: str,
    created_by: str,
    require_stopped: bool,
    allow_cross_tenant: bool = False,
) -> list[ProjectResource]:
    if require_stopped:
        project = _lock_and_check_project_resource_mutation(conn, project_id)
    else:
        project = get_project_or_404(conn, project_id)
    if project["tenant_id"] != tenant_id and not allow_cross_tenant:
        raise HTTPException(404, "Project resource batch not found")
    batch = _lock_owned_open_batch(conn, batch_id, tenant_id=tenant_id, user_id=user_id)
    members = conn.execute(
        """
        SELECT * FROM project_resource_batch_members
        WHERE batch_id = ? ORDER BY path, id
        """,
        (batch_id,),
    ).fetchall()
    prepared_meta = [(row["kind"], row["path"], int(row["size_bytes"])) for row in members]
    _check_resource_quota_and_collisions(conn, project_id, prepared_meta)
    created_paths: list[Path] = []
    resources: list[ProjectResource] = []
    try:
        for member in members:
            source = _batch_member_disk_path(batch_id, member["id"])
            if not source.is_file():
                raise HTTPException(409, f"Project resource batch member content not found: {member['path']}")
            resource_id = next_project_resource_id(conn, project_id)
            target = _project_resource_disk_path(project_id, resource_id, create_parent=True)
            digest = sha256()
            size_bytes = 0
            with source.open("rb") as input_file, target.open("xb") as output_file:
                while True:
                    chunk = input_file.read(64 * 1024)
                    if not chunk:
                        break
                    size_bytes += len(chunk)
                    output_file.write(chunk)
                    digest.update(chunk)
            created_paths.append(target)
            if size_bytes != member["size_bytes"] or digest.hexdigest() != member["sha256"]:
                raise HTTPException(409, f"Project resource batch member integrity check failed: {member['path']}")
            now = utcnow()
            conn.execute(
                """
                INSERT INTO project_resources (
                    id, project_id, kind, scope, path, size_bytes, sha256,
                    active, created_by, created_at, deactivated_at
                ) VALUES (?, ?, ?, 'project', ?, ?, ?, 1, ?, ?, NULL)
                """,
                (
                    resource_id,
                    project_id,
                    member["kind"],
                    member["path"],
                    size_bytes,
                    member["sha256"],
                    created_by,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM project_resources WHERE id = ? AND project_id = ?",
                (resource_id, project_id),
            ).fetchone()
            assert row is not None
            resources.append(project_resource_to_model(row))
        conn.execute(
            """
            UPDATE project_resource_batches
            SET state = 'consumed', consumed_at = ?, consumed_project_id = ?
            WHERE id = ? AND state = 'open'
            """,
            (utcnow(), project_id, batch_id),
        )
        return resources
    except Exception:
        for path in reversed(created_paths):
            path.unlink(missing_ok=True)
        raise


def cleanup_expired_project_resource_batches() -> int:
    """删除过期批次；数据库先提交，磁盘清理采用 best-effort。"""
    now = utcnow()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id FROM project_resource_batches
            WHERE expires_at <= ?
            """,
            (now,),
        ).fetchall()
        if rows:
            placeholders = ", ".join("?" for _ in rows)
            conn.execute(
                f"DELETE FROM project_resource_batches WHERE id IN ({placeholders})",
                tuple(row["id"] for row in rows),
            )
    for row in rows:
        shutil.rmtree(_batch_directory(row["id"]), ignore_errors=True)
    return len(rows)


def delete_project_resource_batch_staging(batch_id: str) -> None:
    """批次消费提交后删除暂存副本；元数据保留到 TTL 过期以阻止重放。"""
    shutil.rmtree(_batch_directory(batch_id), ignore_errors=True)


def delete_project_resource_directory(project_id: str) -> None:
    """项目删除后尽力清理资源文件；失败不应撤销数据库删除。"""
    try:
        directory = _project_resource_project_directory(project_id)
        if directory.exists():
            shutil.rmtree(directory)
    except OSError:
        pass


def _get_owned_batch(conn: Any, batch_id: str, *, tenant_id: str, user_id: str) -> Any:
    row = conn.execute(
        """
        SELECT * FROM project_resource_batches
        WHERE id = ? AND tenant_id = ? AND user_id = ?
        """,
        (batch_id, tenant_id, user_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Project resource batch not found")
    if row["state"] == "open" and row["expires_at"] <= utcnow():
        raise HTTPException(410, "Project resource batch expired")
    return row


def _lock_owned_open_batch(conn: Any, batch_id: str, *, tenant_id: str, user_id: str) -> Any:
    # 同一用户的所有 open batch 共用暂存配额，先锁用户再锁批次以固定锁顺序。
    conn.execute("UPDATE users SET id = id WHERE id = ?", (user_id,))
    conn.execute(
        """
        UPDATE project_resource_batches SET id = id
        WHERE id = ? AND tenant_id = ? AND user_id = ?
        """,
        (batch_id, tenant_id, user_id),
    )
    row = _get_owned_batch(conn, batch_id, tenant_id=tenant_id, user_id=user_id)
    if row["state"] != "open":
        raise HTTPException(409, "Project resource batch has already been consumed")
    return row


def _check_batch_additions(conn: Any, batch_id: str, additions: Sequence[tuple[str, int]]) -> None:
    limits = project_resource_limits()
    owner = conn.execute(
        "SELECT tenant_id, user_id FROM project_resource_batches WHERE id = ?",
        (batch_id,),
    ).fetchone()
    assert owner is not None
    staged_bytes = conn.execute(
        """
        SELECT COALESCE(SUM(m.size_bytes), 0) AS value
        FROM project_resource_batches AS b
        JOIN project_resource_batch_members AS m ON m.batch_id = b.id
        WHERE b.tenant_id = ? AND b.user_id = ? AND b.state = 'open'
        """,
        (owner["tenant_id"], owner["user_id"]),
    ).fetchone()["value"]
    added_bytes = sum(size for _path, size in additions)
    if int(staged_bytes) + added_bytes > limits.max_staged_bytes_per_user:
        raise HTTPException(
            413,
            f"User staged project resources exceed the {limits.max_staged_bytes_per_user}-byte limit",
        )
    existing = conn.execute(
        "SELECT path, size_bytes FROM project_resource_batch_members WHERE batch_id = ?",
        (batch_id,),
    ).fetchall()
    if len(existing) + len(additions) > limits.max_active_count:
        raise HTTPException(409, f"Project resource batch exceeds {limits.max_active_count} resources")
    total = sum(int(row["size_bytes"]) for row in existing) + sum(size for _path, size in additions)
    if total > limits.max_active_total_bytes:
        raise HTTPException(413, f"Project resource batch exceeds the {limits.max_active_total_bytes}-byte total limit")
    paths = [row["path"] for row in existing]
    for path, size_bytes in additions:
        if size_bytes > limits.max_file_bytes:
            raise HTTPException(413, f"Project resource exceeds the {limits.max_file_bytes}-byte file limit")
        collision = _find_path_collision(path, paths)
        if collision is not None:
            raise _path_collision_error(path, collision)
        paths.append(path)


def _preflight_zip(
    archive_path: Path,
    limits: ProjectResourceLimits,
) -> list[tuple[str, zipfile.ZipInfo]]:
    prepared: list[tuple[str, zipfile.ZipInfo]] = []
    paths: list[str] = []
    total_bytes = 0
    with zipfile.ZipFile(archive_path) as archive:
        for entry_index, info in enumerate(archive.infolist(), start=1):
            if entry_index > limits.max_zip_entries:
                raise HTTPException(413, f"ZIP contains more than {limits.max_zip_entries} entries")
            original_name = info.orig_filename
            if info.is_dir() or original_name.endswith("/"):
                continue
            if info.flag_bits & 0x1:
                raise HTTPException(400, f"Encrypted ZIP entries are not allowed: {original_name}")
            if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise HTTPException(400, f"Unsupported ZIP compression method: {original_name}")
            mode = (info.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(mode)
            if file_type not in {0, stat.S_IFREG}:
                raise HTTPException(400, f"ZIP special files are not allowed: {original_name}")
            try:
                path = validate_project_resource_path(original_name)
            except ValueError as exc:
                raise HTTPException(400, f"Unsafe ZIP entry path: {original_name}") from exc
            collision = _find_path_collision(path, paths)
            if collision is not None:
                raise _path_collision_error(path, collision)
            paths.append(path)
            if info.file_size > limits.max_file_bytes:
                raise HTTPException(413, f"ZIP entry exceeds the file limit: {path}")
            if info.file_size and info.file_size > max(info.compress_size, 1) * limits.max_compression_ratio:
                raise HTTPException(413, f"ZIP entry compression ratio is too high: {path}")
            total_bytes += info.file_size
            if total_bytes > limits.max_active_total_bytes:
                raise HTTPException(413, "ZIP uncompressed content exceeds the project total limit")
            prepared.append((path, info))
    return prepared


def _lock_and_check_project_resource_mutation(conn: Any, project_id: str) -> Any:
    # 无操作 UPDATE 在 SQLite/PostgreSQL 都会串行化同一项目的资源配额检查。
    conn.execute("UPDATE projects SET id = id WHERE id = ?", (project_id,))
    project = get_project_or_404(conn, project_id)
    if project["status"] not in {"stopped", "paused"}:
        raise HTTPException(409, "Project resources can only be changed while the project is stopped")
    from .project_records import project_pause_draining, project_pause_unconfirmed

    if project_pause_unconfirmed(conn, project_id) or (project["status"] == "paused" and project_pause_draining(conn, project)):
        raise HTTPException(409, "project_pause_draining")
    active_run = conn.execute(
        """
        SELECT 1 FROM task_runs
        WHERE project_id = ? AND status IN ('queued', 'running') LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    if active_run is not None:
        raise HTTPException(409, "Project resources cannot change while a task run is queued or running")
    return project


def _project_resource_project_directory(project_id: str) -> Path:
    safe_project_id = _safe_storage_segment(project_id, "project_id")
    root = project_resource_root()
    directory = root / safe_project_id
    try:
        directory.resolve().relative_to(root)
    except ValueError as exc:
        raise HTTPException(404, "Project resource storage path is invalid") from exc
    return directory


def _project_resource_disk_path(project_id: str, resource_id: str, *, create_parent: bool = False) -> Path:
    safe_resource_id = _safe_storage_segment(resource_id, "resource_id")
    root = project_resource_root()
    if create_parent:
        root.mkdir(parents=True, exist_ok=True)
    directory = _project_resource_project_directory(project_id)
    if create_parent:
        directory.mkdir(parents=True, exist_ok=True)
    disk_path = directory / safe_resource_id
    try:
        disk_path.resolve().relative_to(directory.resolve())
    except ValueError as exc:
        raise HTTPException(404, "Project resource storage path is invalid") from exc
    return disk_path


def _batch_directory(batch_id: str, *, create: bool = False) -> Path:
    safe_batch_id = _safe_storage_segment(batch_id, "batch_id")
    root = project_resource_root() / ".batches"
    directory = root / safe_batch_id
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise HTTPException(404, "Project resource batch storage path is invalid") from exc
    return directory


def _batch_member_disk_path(batch_id: str, member_id: str, *, create_parent: bool = False) -> Path:
    safe_member_id = _safe_storage_segment(member_id, "member_id")
    directory = _batch_directory(batch_id, create=create_parent)
    path = directory / safe_member_id
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError as exc:
        raise HTTPException(404, "Project resource batch member path is invalid") from exc
    return path


def _safe_storage_segment(value: str, name: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise HTTPException(404, f"Project resource {name} is invalid")
    return value


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [name for name in globals() if not name.startswith("_")]
