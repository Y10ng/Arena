"""会话自有 Artifact 与显式文件迁移，不借用项目目录或复制研究原件。"""

from contextlib import contextmanager
from hashlib import sha256
import json
import os
import shutil
from uuid import uuid4

from fastapi import HTTPException

from cairn.server.artifact_paths import resolve_artifact_path
from cairn.server.db import artifact_root, register_after_rollback
from .time_utils import utcnow


@contextmanager
def new_file(conn, path):
    # 首次写入前承担清理责任；排他创建失败不能误删既有文件。
    created = False

    def cleanup():
        if created:
            path.unlink(missing_ok=True)

    register_after_rollback(conn, cleanup)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            created = True
            yield stream
    except BaseException:
        cleanup()
        raise


def create_artifact(conn, conversation_id, *, content, kind, filename, metadata, task_run_id=None, allow_deleted=False):
    from .agent_conversations import _conversation

    # 已登记 scope 的迟到补传不因聊天软删除而丢失；普通成品仍拒绝新写已删除会话。
    _conversation(conn, conversation_id, lock=True, include_deleted=allow_deleted)
    identifier = "art_" + uuid4().hex
    row = {"id": identifier, "project_id": None, "conversation_id": conversation_id,
           "task_run_id": task_run_id,
           "uri": f"artifact://conversations/{conversation_id}/runs/{task_run_id or 'unscoped'}/{identifier}-{filename}"}
    path = resolve_artifact_path(artifact_root(), row)
    digest, size = sha256(), 0
    with new_file(conn, path) as stream:
        chunks = (content,) if isinstance(content, bytes) else iter(lambda: content.read(1024 * 1024), b"")
        for chunk in chunks:
            stream.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        stream.flush()
        os.fsync(stream.fileno())
    directory = path.parent
    while directory != artifact_root().parent:
        descriptor = os.open(directory, os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory = directory.parent
    now = utcnow()
    conn.execute(
        "INSERT INTO artifacts(id, conversation_id, task_run_id, kind, category, uri, sha256, size, created_at, metadata_json) "
        "VALUES (?, ?, ?, ?, 'evidence', ?, ?, ?, ?, ?)",
        (identifier, conversation_id, task_run_id, kind, row["uri"], digest.hexdigest(), size, now,
         json.dumps(metadata, ensure_ascii=False)),
    )
    if task_run_id:
        conn.execute("INSERT INTO agent_events(id, conversation_id, task_run_id, ts, kind, payload_json) VALUES (?, ?, ?, ?, 'artifact_created', ?)",
                     ("evt_" + uuid4().hex, conversation_id, task_run_id, now, json.dumps({"artifact_id": identifier})))
    return conn.execute("SELECT * FROM artifacts WHERE conversation_id = ? AND id = ?", (conversation_id, identifier)).fetchone()


def artifact_file(conn, row):
    from .agent_conversations import conversation_secrets
    from .verification_requests import _artifact_deliverable

    if row["migration_source_json"] is not None:
        raise HTTPException(409, "conversation_file_migration_required")
    path = resolve_artifact_path(artifact_root(), row)
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise HTTPException(404, "conversation_file_unavailable") from exc
    if len(content) != row["size"] or sha256(content).hexdigest() != row["sha256"]:
        raise HTTPException(409, "conversation_file_digest_mismatch")
    # 来源错误保留明确分类，普通准出规则仍由既有实现唯一维护。
    conversation_secrets(conn, row["conversation_id"], row["task_run_id"])
    if not _artifact_deliverable(conn, row, content=content):
        raise HTTPException(403, "agent_artifact_not_deliverable")
    return path


def migrate_conversation_files(conn, *, conversation_id=None):
    """由受控迁移入口调用；提交前验证新旧字节，失败回滚新件，原件始终保留。"""
    rows = conn.execute(
        "SELECT * FROM artifacts WHERE migration_source_json IS NOT NULL" +
        (" AND conversation_id = ?" if conversation_id else "") + " ORDER BY record_id FOR UPDATE",
        (conversation_id,) if conversation_id else (),
    ).fetchall()
    stats = {"artifacts": 0, "bytes": 0}
    for row in rows:
        source = resolve_artifact_path(artifact_root(), json.loads(row["migration_source_json"]))
        target = resolve_artifact_path(artifact_root(), row)
        if source == target:
            raise ValueError("conversation migration source equals destination")
        _verify_migration_file(source, row)
        with source.open("rb") as original, new_file(conn, target) as output:
            shutil.copyfileobj(original, output)
        _verify_migration_file(target, row)
        conn.execute("UPDATE artifacts SET migration_source_json = NULL WHERE record_id = ?", (row["record_id"],))
        stats["artifacts"] += 1
        stats["bytes"] += row["size"]
    return stats


def _verify_migration_file(path, row):
    digest, size = sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    if size != row["size"] or digest.hexdigest() != row["sha256"]:
        raise ValueError("conversation migration digest mismatch")
