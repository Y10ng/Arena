"""不可变、版本化 advisory library 服务。"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path
from re import fullmatch
import sqlite3
from tempfile import NamedTemporaryFile

from fastapi import HTTPException

from cairn.server.schemas.libraries import (
    Library,
    LibraryBinding,
    LibraryFile,
    LibraryFileContent,
    LibraryFileWrite,
    LibraryRevisionRef,
    LibraryVersion,
)


ADVISORY_LIBRARY_ROOT_ENV_VAR = "CAIRN_ADVISORY_LIBRARY_ROOT"
DEFAULT_ADVISORY_LIBRARY_ROOT = "datas/cairn/advisory-library-content"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def library_content_root() -> Path:
    configured = os.environ.get(ADVISORY_LIBRARY_ROOT_ENV_VAR, "").strip()
    return Path(configured or DEFAULT_ADVISORY_LIBRARY_ROOT).expanduser().resolve()


def library_storage_path(storage_key: str) -> Path:
    if not fullmatch(r"[0-9a-f]{64}", storage_key):
        raise RuntimeError("Invalid advisory library storage key")
    return library_content_root() / storage_key[:2] / storage_key[2:]


def _store_content(content: bytes) -> str:
    storage_key = sha256(content).hexdigest()
    destination = library_storage_path(storage_key)
    if destination.exists():
        existing = destination.read_bytes()
        if existing != content:
            raise RuntimeError(f"Advisory library storage collision: {storage_key}")
        return storage_key
    destination.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return storage_key


def validate_library_key(value: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not fullmatch(r"[a-z0-9_-]{1,128}", text):
        raise HTTPException(
            422,
            "Library key must contain only lowercase letters, numbers, underscores, or hyphens",
        )
    return text


def validate_library_path(value: str) -> str:
    text = value.replace("\\", "/") if isinstance(value, str) else ""
    if not text or text.startswith("/") or text.endswith("/") or len(text) > 512:
        raise HTTPException(422, "Library file path must be a relative path of at most 512 characters")
    parts = text.split("/")
    if any(not part or part in {".", ".."} or len(part) > 255 for part in parts):
        raise HTTPException(422, "Library file path contains an invalid segment")
    if any(any(ord(char) < 32 or ord(char) == 127 for char in part) for part in parts):
        raise HTTPException(422, "Library file path must not contain control characters")
    return "/".join(parts)


def list_libraries(conn: sqlite3.Connection, *, include_archived: bool = False) -> list[Library]:
    where = "" if include_archived else "WHERE library.archived_at IS NULL"
    rows = conn.execute(
        f"""
        SELECT library.key, library.name, library.created_at, library.archived_at,
               (SELECT MAX(version.version) FROM advisory_library_versions AS version
                WHERE version.library_key = library.key) AS latest_version,
               (SELECT COUNT(*) FROM advisory_library_files AS file
                WHERE file.library_key = library.key
                  AND file.version = (SELECT MAX(version.version)
                                      FROM advisory_library_versions AS version
                                      WHERE version.library_key = library.key)) AS file_count
        FROM advisory_libraries AS library
        {where}
        ORDER BY library.key
        """
    ).fetchall()
    return [_library_model(row) for row in rows]


def get_library(conn: sqlite3.Connection, key: str, *, include_archived: bool = False) -> Library:
    libraries = [item for item in list_libraries(conn, include_archived=include_archived) if item.key == key]
    if not libraries:
        raise HTTPException(404, f"Library {key} not found")
    return libraries[0]


def create_library(conn: sqlite3.Connection, key: str, name: str | None = None) -> Library:
    key = validate_library_key(key)
    row = conn.execute("SELECT key FROM advisory_libraries WHERE key = ?", (key,)).fetchone()
    if row is not None:
        raise HTTPException(409, f"Library {key} already exists")
    conn.execute(
        "INSERT INTO advisory_libraries (key, name, created_at) VALUES (?, ?, ?)",
        (key, name or key, _utcnow()),
    )
    return get_library(conn, key)


def archive_library(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("UPDATE advisory_libraries SET key = key WHERE key = ?", (key,))
    row = conn.execute("SELECT archived_at FROM advisory_libraries WHERE key = ?", (key,)).fetchone()
    if row is None:
        return
    binding = conn.execute(
        "SELECT project_id FROM project_advisory_library_bindings WHERE library_key = ? LIMIT 1",
        (key,),
    ).fetchone()
    if binding is not None:
        raise HTTPException(409, f"Library {key} is bound to project {binding['project_id']}")
    schedule_binding = conn.execute(
        "SELECT schedule_id FROM project_schedule_libraries WHERE library_key = ? LIMIT 1",
        (key,),
    ).fetchone()
    if schedule_binding is not None:
        raise HTTPException(409, f"Library {key} is bound to schedule {schedule_binding['schedule_id']}")
    if row["archived_at"] is None:
        conn.execute("UPDATE advisory_libraries SET archived_at = ? WHERE key = ?", (_utcnow(), key))


def list_selectable_current_libraries(conn: sqlite3.Connection) -> list[Library]:
    """列出可供新项目选择且已经发布版本的 library。"""
    return [item for item in list_libraries(conn) if item.latest_version is not None]


def resolve_current_library_revisions(
    conn: sqlite3.Connection,
    library_keys: list[str],
) -> list[LibraryRevisionRef]:
    """在调用方现有事务中把可变 key 固定为当前不可变 revision。"""
    resolved: list[LibraryRevisionRef] = []
    seen: set[str] = set()
    for raw_key in library_keys:
        key = validate_library_key(raw_key)
        if key in seen:
            continue
        seen.add(key)
        revision = get_library_version(conn, key)
        resolved.append(
            LibraryRevisionRef(
                library_key=key,
                version=revision.version,
                digest_sha256=revision.digest_sha256,
            )
        )
    return resolved


def bind_library_revision(
    conn: sqlite3.Connection,
    project_id: str,
    revision: LibraryRevisionRef,
) -> LibraryBinding:
    project_id = project_id.strip()
    if not project_id:
        raise HTTPException(422, "Library binding project_id is required")
    _lock_library_for_binding(conn, revision.library_key)
    _check_library_revision(conn, revision)
    created_at = _utcnow()
    conn.execute(
        """INSERT INTO project_advisory_library_bindings
           (project_id, library_key, version, created_at)
           VALUES (?, ?, ?, ?)""",
        (project_id, revision.library_key, revision.version, created_at),
    )
    return LibraryBinding(
        project_id=project_id,
        library_key=revision.library_key,
        version=revision.version,
        created_at=created_at,
    )


def _lock_library_for_binding(conn: sqlite3.Connection, key: str) -> None:
    # 归档与新增绑定串行，避免归档检查后又提交新的计划引用。
    conn.execute("UPDATE advisory_libraries SET key = key WHERE key = ?", (key,))
    get_library(conn, key)


def _check_library_revision(conn: sqlite3.Connection, revision: LibraryRevisionRef) -> None:
    selected = get_library_version(conn, revision.library_key, revision.version)
    if selected.digest_sha256 != revision.digest_sha256:
        raise HTTPException(409, f"Library {revision.library_key} version {revision.version} digest mismatch")


def bind_schedule_library_revision(
    conn: sqlite3.Connection,
    schedule_id: str,
    revision: LibraryRevisionRef,
) -> LibraryRevisionRef:
    _lock_library_for_binding(conn, revision.library_key)
    _check_library_revision(conn, revision)
    conn.execute(
        """INSERT INTO project_schedule_libraries
           (schedule_id, library_key, version, digest_sha256, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (schedule_id, revision.library_key, revision.version, revision.digest_sha256, _utcnow()),
    )
    return revision


def list_schedule_library_revisions(
    conn: sqlite3.Connection,
    schedule_id: str,
) -> list[LibraryRevisionRef]:
    rows = conn.execute(
        """SELECT library_key, version, digest_sha256 FROM project_schedule_libraries
           WHERE schedule_id = ? ORDER BY library_key""",
        (schedule_id,),
    ).fetchall()
    revisions = [LibraryRevisionRef(**dict(row)) for row in rows]
    for revision in revisions:
        _check_library_revision(conn, revision)
    return revisions


def validate_library_revision_content(
    conn: sqlite3.Connection,
    revision: LibraryRevisionRef,
) -> None:
    """触发前校验固定版本原件，不把全库读盘放进详情列表。"""
    _check_library_revision(conn, revision)
    digest = sha256()
    for file in sorted(list_library_revision_files(conn, revision), key=lambda item: item.path):
        try:
            content = read_library_revision_content(conn, revision, file.path)
        except (OSError, RuntimeError) as exc:
            raise HTTPException(
                409, f"Library {revision.library_key} version {revision.version} content integrity check failed"
            ) from exc
        _update_library_digest(digest, file.path, content)
    if digest.hexdigest() != revision.digest_sha256:
        raise HTTPException(409, f"Library {revision.library_key} version {revision.version} digest mismatch")


def list_library_bindings(
    conn: sqlite3.Connection,
    project_id: str,
) -> list[LibraryBinding]:
    rows = conn.execute(
        """SELECT project_id, library_key, version, created_at
           FROM project_advisory_library_bindings
           WHERE project_id = ? ORDER BY library_key""",
        (project_id,),
    ).fetchall()
    return [LibraryBinding(**dict(row)) for row in rows]


def get_bound_library_revision(
    conn: sqlite3.Connection,
    project_id: str,
    key: str,
    version: int,
) -> LibraryRevisionRef:
    """只解析项目已经冻结的 revision，避免 Dispatcher 越界读取其他资料。"""
    normalized_key = validate_library_key(key)
    row = conn.execute(
        """SELECT version FROM project_advisory_library_bindings
           WHERE project_id = ? AND library_key = ? AND version = ?""",
        (project_id, normalized_key, version),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Project advisory library binding not found")
    selected = get_library_version(conn, normalized_key, version)
    return LibraryRevisionRef(
        library_key=normalized_key,
        version=selected.version,
        digest_sha256=selected.digest_sha256,
    )


def list_library_versions(conn: sqlite3.Connection, key: str) -> list[LibraryVersion]:
    get_library(conn, key, include_archived=True)
    rows = conn.execute(
        """
        SELECT version.*, COUNT(file.path) AS file_count
        FROM advisory_library_versions AS version
        LEFT JOIN advisory_library_files AS file
          ON file.library_key = version.library_key AND file.version = version.version
        WHERE version.library_key = ?
        GROUP BY version.library_key, version.version, version.digest_sha256, version.source,
                 version.message, version.created_by, version.created_at
        ORDER BY version.version DESC
        """,
        (key,),
    ).fetchall()
    return [_version_model(row) for row in rows]


def get_library_version(conn: sqlite3.Connection, key: str, version: int | None = None) -> LibraryVersion:
    versions = list_library_versions(conn, key)
    if version is None:
        if not versions:
            raise HTTPException(404, f"Library {key} has no versions")
        return versions[0]
    for item in versions:
        if item.version == version:
            return item
    raise HTTPException(404, f"Library {key} version {version} not found")


def create_library_version(
    conn: sqlite3.Connection,
    key: str,
    files: list[LibraryFileWrite],
    *,
    source: str = "api",
    message: str | None = None,
    created_by: str | None = None,
) -> LibraryVersion:
    get_library(conn, key)
    normalized: dict[str, bytes] = {}
    for item in files:
        path = validate_library_path(item.path)
        if path in normalized:
            raise HTTPException(422, f"Duplicate library file path: {path}")
        normalized[path] = item.content.encode("utf-8")
    digest = _library_digest(normalized)
    if source == "legacy_filesystem":
        existing = conn.execute(
            """SELECT version FROM advisory_library_versions
               WHERE library_key = ? AND digest_sha256 = ? AND source = 'legacy_filesystem'
               ORDER BY version DESC""",
            (key, digest),
        ).fetchone()
        if existing is not None:
            return get_library_version(conn, key, int(existing["version"]))
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM advisory_library_versions WHERE library_key = ?",
        (key,),
    ).fetchone()
    version = int(row["next_version"])
    conn.execute(
        """INSERT INTO advisory_library_versions
           (library_key, version, digest_sha256, source, message, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (key, version, digest, source, message, created_by, _utcnow()),
    )
    for path, content in sorted(normalized.items()):
        content_sha256 = sha256(content).hexdigest()
        storage_key = _store_content(content)
        conn.execute(
            """INSERT INTO advisory_library_files
               (library_key, version, path, storage_key, size_bytes, sha256)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (key, version, path, storage_key, len(content), content_sha256),
        )
    return get_library_version(conn, key, version)


def list_library_files(conn: sqlite3.Connection, key: str, version: int | None = None) -> list[LibraryFile]:
    selected = get_library_version(conn, key, version)
    rows = conn.execute(
        """SELECT path, size_bytes, sha256 FROM advisory_library_files
           WHERE library_key = ? AND version = ? ORDER BY path""",
        (key, selected.version),
    ).fetchall()
    return [LibraryFile(path=row["path"], size_bytes=row["size_bytes"], sha256=row["sha256"]) for row in rows]


def read_library_file(
    conn: sqlite3.Connection,
    key: str,
    path: str,
    version: int | None = None,
) -> LibraryFileContent:
    selected = get_library_version(conn, key, version)
    normalized = validate_library_path(path)
    row = conn.execute(
        """SELECT path, storage_key, size_bytes, sha256 FROM advisory_library_files
           WHERE library_key = ? AND version = ? AND path = ?""",
        (key, selected.version, normalized),
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"Library file {normalized} not found")
    storage_path = library_storage_path(row["storage_key"])
    try:
        content = storage_path.read_bytes()
    except FileNotFoundError as exc:
        raise RuntimeError(f"Advisory library content is missing: {row['storage_key']}") from exc
    if len(content) != row["size_bytes"]:
        raise RuntimeError(f"Advisory library size mismatch: {normalized}")
    if sha256(content).hexdigest() != row["sha256"]:
        raise RuntimeError(f"Advisory library sha256 mismatch: {normalized}")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"Stored library file {normalized} is not valid UTF-8") from exc
    return LibraryFileContent(path=normalized, content=text, size_bytes=row["size_bytes"], sha256=row["sha256"])


def list_library_revision_files(
    conn: sqlite3.Connection,
    revision: LibraryRevisionRef,
) -> list[LibraryFile]:
    return list_library_files(conn, revision.library_key, revision.version)


def read_library_revision_content(
    conn: sqlite3.Connection,
    revision: LibraryRevisionRef,
    path: str,
) -> bytes:
    item = read_library_file(conn, revision.library_key, path, revision.version)
    # Dispatcher 需要原始 UTF-8 字节，不能经过预览、脱敏或语义清洗。
    return item.content.encode("utf-8")


def replace_library_file(
    conn: sqlite3.Connection,
    key: str,
    path: str,
    content: str,
    *,
    created_by: str | None = None,
) -> LibraryFileContent:
    normalized = validate_library_path(path)
    files = _latest_file_writes(conn, key)
    files[normalized] = LibraryFileWrite(path=normalized, content=content)
    version = create_library_version(conn, key, list(files.values()), created_by=created_by)
    return read_library_file(conn, key, normalized, version.version)


def remove_library_file(conn: sqlite3.Connection, key: str, path: str, *, created_by: str | None = None) -> None:
    normalized = validate_library_path(path)
    files = _latest_file_writes(conn, key)
    if normalized not in files:
        return
    del files[normalized]
    create_library_version(conn, key, list(files.values()), created_by=created_by)


def import_legacy_knowledge(conn: sqlite3.Connection, root: Path) -> list[Library]:
    root = root.expanduser().resolve()
    if not root.exists():
        return []
    keys = ["knowledge_base"] + sorted(
        child.name
        for child in root.iterdir()
        if child.is_dir()
        and not child.is_symlink()
        and fullmatch(r"[a-z0-9_-]{1,128}", child.name)
    )
    imported: list[Library] = []
    child_roots = {(root / key).resolve() for key in keys if key != "knowledge_base"}
    for key in keys:
        source_root = root if key == "knowledge_base" else (root / key).resolve()
        existing = conn.execute(
            "SELECT archived_at FROM advisory_libraries WHERE key = ?", (key,)
        ).fetchone()
        if existing is None:
            create_library(conn, key, key)
        elif existing["archived_at"] is not None:
            continue
        files: list[LibraryFileWrite] = []
        for path in sorted(source_root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            resolved = path.resolve()
            if key == "knowledge_base" and any(resolved.is_relative_to(child) for child in child_roots):
                continue
            relative = path.relative_to(source_root).as_posix()
            try:
                normalized = validate_library_path(relative)
                content = path.read_bytes().decode("utf-8")
            except (HTTPException, UnicodeDecodeError):
                continue
            files.append(LibraryFileWrite(path=normalized, content=content))
        create_library_version(conn, key, files, source="legacy_filesystem", message="Imported from legacy knowledge filesystem")
        imported.append(get_library(conn, key))
    return imported


def _latest_file_writes(conn: sqlite3.Connection, key: str) -> dict[str, LibraryFileWrite]:
    try:
        version = get_library_version(conn, key)
    except HTTPException as exc:
        if exc.status_code == 404 and "has no versions" in str(exc.detail):
            return {}
        raise
    rows = conn.execute(
        "SELECT path FROM advisory_library_files WHERE library_key = ? AND version = ? ORDER BY path",
        (key, version.version),
    ).fetchall()
    return {
        row["path"]: LibraryFileWrite(
            path=row["path"],
            content=read_library_file(conn, key, row["path"], version.version).content,
        )
        for row in rows
    }


def _update_library_digest(digest, path: str, content: bytes) -> None:
    path_bytes = path.encode("utf-8")
    digest.update(len(path_bytes).to_bytes(8, "big"))
    digest.update(path_bytes)
    digest.update(len(content).to_bytes(8, "big"))
    digest.update(content)


def _library_digest(files: dict[str, bytes]) -> str:
    digest = sha256()
    for path, content in sorted(files.items()):
        _update_library_digest(digest, path, content)
    return digest.hexdigest()


def _library_model(row) -> Library:
    return Library(
        key=row["key"],
        name=row["name"],
        latest_version=row["latest_version"],
        file_count=row["file_count"],
        created_at=row["created_at"],
        archived_at=row["archived_at"],
    )


def _version_model(row) -> LibraryVersion:
    return LibraryVersion(
        library_key=row["library_key"],
        version=row["version"],
        digest_sha256=row["digest_sha256"],
        source=row["source"],
        message=row["message"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        file_count=row["file_count"],
    )


__all__ = [name for name in globals() if not name.startswith("_")]
