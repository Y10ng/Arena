"""审计 artifact 与 fact evidence 服务。"""

from __future__ import annotations

from base64 import b64decode
from dataclasses import dataclass
from hashlib import sha256
import fcntl
import mimetypes
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from typing import AsyncIterable

from .common import *
from .artifacts import artifact_to_model, fact_evidence_to_model
from .audit_events import insert_agent_event
from .ids import get_project_or_404, lock_verification_project_write, next_artifact_id
from .payload_values import dump_json
from .projects import validate_facts_exist, validate_task_run_exists
from .runtime_context import task_run_runtime_context
from .time_utils import utcnow

from cairn.server.artifact_paths import UnsafeArtifactPath, resolve_artifact_path
from cairn.server.db import artifact_root, get_conn
from cairn.server.models import (
    Artifact,
    ArtifactPreview,
    CreateArtifactRequest,
    CreateRawArtifactRequest,
    FROZEN_ARTIFACT_KINDS,
    FROZEN_ARTIFACT_KIND_MESSAGE,
    FactEvidence,
)

SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
PREVIEWABLE_ARTIFACT_KINDS = {"raw_output", "log", "report", "patch", "file", "har", "hook_log"}
INLINE_CONTENT_ARTIFACT_KINDS = {"screenshot"}
INLINE_CONTENT_MEDIA_TYPES = {
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
ARTIFACT_PREVIEW_BYTES = 64 * 1024


@dataclass(slots=True)
class ArtifactContentFile:
    disk_path: Path
    media_type: str
    filename: str
    content_disposition_type: str


def create_artifact_record(project_id: str, body: CreateArtifactRequest) -> Artifact:
    # artifact 内容先落磁盘，再写数据库记录；digest/uri 是后续 evidence 绑定的稳定索引。
    if body.content_base64 is not None:
        content = b64decode(body.content_base64)
    else:
        assert body.content is not None
        content = body.content.encode("utf-8")
    _validate_registered_content(body.metadata, len(content), sha256(content).hexdigest())
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        task_row = None
        if body.task_run_id is not None:
            task_row = validate_task_run_exists(conn, project_id, body.task_run_id)

        artifact_id = next_artifact_id(conn, project_id)
        now = utcnow()
        digest = sha256(content).hexdigest()
        filename = _safe_artifact_filename(body.filename, artifact_id, body.kind)
        disk_path = _artifact_disk_path(project_id, body.task_run_id, filename)
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        disk_path.write_bytes(content)
        return _insert_artifact_record(
            conn,
            project_id=project_id,
            task_run_id=body.task_run_id,
            task_row=task_row,
            artifact_id=artifact_id,
            kind=body.kind,
            category=body.category,
            filename=filename,
            digest=digest,
            size=len(content),
            metadata=body.metadata,
            created_at=now,
        )


async def create_raw_artifact_record(
    project_id: str,
    body: CreateRawArtifactRequest,
    chunks: AsyncIterable[bytes],
) -> Artifact:
    """将 raw request body 分块持久化，并在完整落盘后创建审计记录。"""
    temporary_path: Path | None = None
    final_path: Path | None = None
    replaced = False
    artifact_file = None
    try:
        # 在所有者锁内创建可归属临时文件；接收 body 时只保留文件锁，不长期占用 DB 事务。
        with get_conn() as conn:
            lock_verification_project_write(conn, project_id)
            get_project_or_404(conn, project_id)
            if body.task_run_id is not None:
                validate_task_run_exists(conn, project_id, body.task_run_id)
            is_verification = conn.execute("SELECT 1 FROM verification_requests WHERE project_id = ?", (project_id,)).fetchone()
            upload_dir = (artifact_root() / "projects" / project_id / ".uploads"
                          if is_verification else artifact_root() / ".uploads")
            upload_dir.mkdir(parents=True, exist_ok=True)
            artifact_file = NamedTemporaryFile(mode="wb", dir=upload_dir, prefix=".raw-artifact-", suffix=".tmp", delete=False)
            temporary_path = Path(artifact_file.name)
            fcntl.flock(artifact_file.fileno(), fcntl.LOCK_EX)
        digest = sha256()
        size = 0
        with artifact_file:
            async for chunk in chunks:
                if not chunk:
                    continue
                artifact_file.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            artifact_file.flush()
            os.fsync(artifact_file.fileno())

        _validate_registered_content(body.metadata, size, digest.hexdigest())
        # 最终发布必须重取同一 Project 锁和删除 fence，不能凭入口检查重建已删目录。
        with get_conn() as conn:
            lock_verification_project_write(conn, project_id)
            get_project_or_404(conn, project_id)
            task_row = None
            if body.task_run_id is not None:
                task_row = validate_task_run_exists(conn, project_id, body.task_run_id)
            artifact_id = next_artifact_id(conn, project_id)
            filename = _safe_artifact_filename(body.filename, artifact_id, body.kind)
            final_path = _artifact_disk_path(project_id, body.task_run_id, filename)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary_path, final_path)
            replaced = True
            temporary_path = None

            return _insert_artifact_record(
                conn,
                project_id=project_id,
                task_run_id=body.task_run_id,
                task_row=task_row,
                artifact_id=artifact_id,
                kind=body.kind,
                category=body.category,
                filename=filename,
                digest=digest.hexdigest(),
                size=size,
                metadata=body.metadata,
                created_at=utcnow(),
            )
    except BaseException:
        # 先关闭活动句柄再解除目录中的归属，严格清理才能证明没有仍打开的在途文件。
        if artifact_file is not None:
            artifact_file.close()
        # 客户端断连、磁盘错误或数据库失败都不能留下临时文件或孤立的最终文件。
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        if replaced and final_path is not None:
            final_path.unlink(missing_ok=True)
        raise
    finally:
        if artifact_file is not None:
            artifact_file.close()


def _validate_registered_content(metadata: dict, size: int, digest: str) -> None:
    if metadata.get("declared_by_worker") is True and (
        metadata.get("registered_sha256") != digest or metadata.get("registered_size_bytes") != size
    ):
        raise HTTPException(422, "registered_content_version_mismatch")


def _insert_artifact_record(
    conn,
    *,
    project_id: str,
    task_run_id: str | None,
    task_row,
    artifact_id: str,
    kind: str,
    category: str | None,
    filename: str,
    digest: str,
    size: int,
    metadata: dict,
    created_at: str,
) -> Artifact:
    uri = f"artifact://projects/{project_id}/runs/{task_run_id or 'unscoped'}/{filename}"
    merged_metadata = _artifact_metadata(metadata, task_row)
    resolved_category = category or _default_artifact_category(kind)
    conn.execute(
        """
        INSERT INTO artifacts
            (id, project_id, task_run_id, kind, category, uri, sha256, size, created_at, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            artifact_id,
            project_id,
            task_run_id,
            kind,
            resolved_category,
            uri,
            digest,
            size,
            created_at,
            dump_json(merged_metadata),
        ),
    )
    if task_run_id is not None:
        insert_agent_event(
            conn,
            project_id,
            task_run_id,
            "artifact_created",
            {
                "artifact_id": artifact_id,
                "kind": kind,
                "category": resolved_category,
                "uri": uri,
                "sha256": digest,
                "size": size,
                "metadata": merged_metadata,
            },
            task_row=task_row,
        )
    row = conn.execute(
        "SELECT * FROM artifacts WHERE id = ? AND project_id = ?",
        (artifact_id, project_id),
    ).fetchone()
    return artifact_to_model(row)


def list_artifact_records(project_id: str, task_run_id: str | None = None) -> list[Artifact]:
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        if task_run_id is None:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE project_id = ? ORDER BY created_at, id",
                (project_id,),
            ).fetchall()
        else:
            validate_task_run_exists(conn, project_id, task_run_id)
            rows = conn.execute(
                """
                SELECT * FROM artifacts
                WHERE project_id = ? AND task_run_id = ?
                ORDER BY created_at, id
                """,
                (project_id, task_run_id),
            ).fetchall()
        return [artifact_to_model(row) for row in rows]


def artifact_content_file(project_id: str, artifact_id: str) -> ArtifactContentFile:
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        row = conn.execute(
            "SELECT * FROM artifacts WHERE id = ? AND project_id = ?",
            (artifact_id, project_id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, f"Artifact {artifact_id} not found")

    disk_path = _artifact_disk_path_from_row(row)
    if not disk_path.exists() or not disk_path.is_file():
        raise HTTPException(404, f"Artifact {artifact_id} content not found")
    # 只有明确安全的图片类 artifact inline 展示，其余默认下载，避免浏览器误执行内容。
    content_disposition_type = "inline" if row["kind"] in INLINE_CONTENT_ARTIFACT_KINDS else "attachment"
    return ArtifactContentFile(
        disk_path=disk_path,
        media_type=_artifact_content_media_type(disk_path),
        filename=disk_path.name,
        content_disposition_type=content_disposition_type,
    )


def read_artifact_preview_record(project_id: str, artifact_id: str, *, tail: bool) -> ArtifactPreview:
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        row = conn.execute(
            "SELECT * FROM artifacts WHERE id = ? AND project_id = ?",
            (artifact_id, project_id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, f"Artifact {artifact_id} not found")
    if row["kind"] not in PREVIEWABLE_ARTIFACT_KINDS:
        raise HTTPException(415, f"Artifact {artifact_id} is not previewable text")

    disk_path = _artifact_disk_path_from_row(row)
    if not disk_path.exists() or not disk_path.is_file():
        raise HTTPException(404, f"Artifact {artifact_id} content not found")

    # preview 只读固定窗口；tail 模式用于查看长 raw_output 的最新失败线索。
    with disk_path.open("rb") as artifact_file:
        if tail and row["size"] > ARTIFACT_PREVIEW_BYTES:
            artifact_file.seek(max(0, row["size"] - ARTIFACT_PREVIEW_BYTES))
        raw = artifact_file.read(ARTIFACT_PREVIEW_BYTES + 1)
    truncated = len(raw) > ARTIFACT_PREVIEW_BYTES or row["size"] > ARTIFACT_PREVIEW_BYTES
    raw = raw[-ARTIFACT_PREVIEW_BYTES:] if tail else raw[:ARTIFACT_PREVIEW_BYTES]
    content = raw.decode("utf-8", errors="replace")

    kind = row["kind"]
    kind_deprecated = kind in FROZEN_ARTIFACT_KINDS
    return ArtifactPreview(
        artifact_id=row["id"],
        project_id=row["project_id"],
        task_run_id=row["task_run_id"],
        kind=kind,
        kind_deprecated=kind_deprecated,
        deprecation_message=FROZEN_ARTIFACT_KIND_MESSAGE if kind_deprecated else None,
        content=content,
        size=row["size"],
        bytes_read=len(raw),
        truncated=truncated,
        sha256=row["sha256"],
    )


def list_fact_evidence_records(project_id: str, fact_id: str) -> list[FactEvidence]:
    with get_conn() as conn:
        validate_facts_exist(conn, project_id, [fact_id])
        # evidence 的有效性是联表派生状态，历史记录不因 artifact/run 删除而丢失。
        rows = conn.execute(
            """
            SELECT fe.*,
                   CASE WHEN a.id IS NULL THEN 0 ELSE 1 END AS artifact_exists,
                   CASE
                       WHEN fe.verifier_run_id IS NULL THEN NULL
                       WHEN tr.id IS NULL THEN 0
                       ELSE 1
                   END AS verifier_run_exists
            FROM fact_evidence fe
            LEFT JOIN artifacts a ON a.project_id = fe.project_id AND a.id = fe.artifact_id
            LEFT JOIN task_runs tr ON tr.project_id = fe.project_id AND tr.id = fe.verifier_run_id
            WHERE fe.project_id = ? AND fe.fact_id = ?
            ORDER BY fe.created_at, fe.artifact_id
            """,
            (project_id, fact_id),
        ).fetchall()
        return [fact_evidence_to_model(row) for row in rows]


def _artifact_disk_path(project_id: str, task_run_id: str | None, filename: str) -> Path:
    run_segment = task_run_id or "unscoped"
    return artifact_root() / "projects" / project_id / "runs" / run_segment / filename


def _artifact_disk_path_from_row(row) -> Path:
    try:
        return resolve_artifact_path(artifact_root(), dict(row))
    except UnsafeArtifactPath as exc:
        # HTTP 层保持历史 404 行为，不向调用方泄露磁盘布局或具体拒绝原因。
        raise HTTPException(404, f"Artifact {row['id']} content not found") from exc


def _artifact_content_media_type(disk_path: Path) -> str:
    media_type = INLINE_CONTENT_MEDIA_TYPES.get(disk_path.suffix.lower())
    if media_type is not None:
        return media_type
    guessed_media_type, _ = mimetypes.guess_type(disk_path.name)
    return guessed_media_type or "application/octet-stream"


def _safe_artifact_filename(filename: str | None, artifact_id: str, kind: str) -> str:
    if filename is None:
        filename = f"{artifact_id}-{kind}.txt"
    cleaned = SAFE_FILENAME_RE.sub("_", Path(filename).name).strip("._")
    if not cleaned:
        cleaned = f"{artifact_id}-{kind}.txt"
    if not cleaned.startswith(f"{artifact_id}-"):
        cleaned = f"{artifact_id}-{cleaned}"
    return cleaned


def _artifact_metadata(metadata: dict, task_row) -> dict:
    merged = dict(metadata)
    if task_row is not None:
        merged["runtime_context"] = task_run_runtime_context(task_row)
    return merged


def _default_artifact_category(kind: str) -> str:
    if kind in {"report", "screenshot", "patch", "file"}:
        return "deliverable"
    return "evidence"
