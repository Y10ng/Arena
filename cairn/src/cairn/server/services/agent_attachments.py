"""聊天附件复用 Artifact 原件与项目资源校验，不导入研究目录。"""

from __future__ import annotations

import base64
from uuid import uuid4

from fastapi import HTTPException

from cairn.server.db import get_conn
from cairn.server.models import CreateRawArtifactRequest
from .audit_artifacts import _artifact_disk_path_from_row, publish_raw_artifact, stage_raw_artifact
from .project_resources import (
    _batch_member_disk_path, _check_batch_additions, copy_verified_resource_content,
    create_project_resource_batch, infer_project_resource_kind, project_resource_limits,
    validate_project_resource_path,
)
from .time_utils import utcnow
from .verification_requests import _artifact_deliverable


_SELECT = ("SELECT a.*, r.size, r.sha256 FROM agent_attachments a JOIN artifacts r "
           "ON r.project_id = a.project_id AND r.id = a.artifact_id ")


def public_attachment(row):
    return {"id": row["id"], "filename": row["filename"], "size": row["size"],
            "message_id": row["message_id"],
            "download_url": f"/projects/{row['project_id']}/agent-conversations/{row['conversation_id']}/attachments/{row['id']}/content"}


def list_attachments(conn, project_id, conversation_id, *, message_id=None, sent_only=False):
    where = "WHERE a.project_id = ? AND a.conversation_id = ? AND a.removed_at IS NULL"
    args = [project_id, conversation_id]
    if message_id is not None:
        where += " AND a.message_id = ?"
        args.append(message_id)
    if sent_only:
        where += " AND a.message_id IS NOT NULL"
    return [public_attachment(row) for row in conn.execute(
        _SELECT + where + " ORDER BY a.created_at, a.id", args).fetchall()]


def attachment_row(conn, project_id, conversation_id, attachment_id, *, sent_only=False):
    row = conn.execute(_SELECT + "WHERE a.project_id = ? AND a.conversation_id = ? AND a.id = ? AND a.removed_at IS NULL",
                       (project_id, conversation_id, attachment_id)).fetchone()
    if row is None or (sent_only and row["message_id"] is None):
        raise HTTPException(404, "当前会话中没有此附件。")
    return row


def attachment_file(conn, row):
    artifact = conn.execute("SELECT * FROM artifacts WHERE project_id = ? AND id = ?",
                            (row["project_id"], row["artifact_id"])).fetchone()
    if not _artifact_deliverable(conn, artifact):
        raise HTTPException(403, "附件包含受保护内容，不能读取或转交。")
    return _artifact_disk_path_from_row(artifact)


async def upload_attachment(project_id, conversation_id, filename, stream, principal):
    from .agent_conversations import _conversation

    try:
        validate_project_resource_path(filename)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    with get_conn() as conn:
        _conversation(conn, project_id, conversation_id)
    limits = project_resource_limits()

    async def bounded():
        size = 0
        async for chunk in stream:
            size += len(chunk)
            if size > limits.max_file_bytes:
                raise HTTPException(413, f"附件超过单文件 {limits.max_file_bytes} 字节限制。")
            yield chunk

    identifier = "att_" + uuid4().hex
    async with stage_raw_artifact(project_id, None, bounded(), project_owned=True) as upload:
        with get_conn() as conn:
            _conversation(conn, project_id, conversation_id, lock=True)
            usage = conn.execute(
                "SELECT count(*) AS count, COALESCE(sum(r.size), 0) AS size FROM agent_attachments a "
                "JOIN artifacts r ON r.project_id = a.project_id AND r.id = a.artifact_id "
                "WHERE a.conversation_id = ? AND a.removed_at IS NULL", (conversation_id,),
            ).fetchone()
            if usage["count"] >= limits.max_active_count or usage["size"] + upload.size > limits.max_active_total_bytes:
                raise HTTPException(413, "会话附件数量或总大小已达到项目资源限制。")
            artifact = publish_raw_artifact(conn, project_id, CreateRawArtifactRequest(
                kind="file", filename=identifier, metadata_json='{"agent_attachment":true}',
            ), upload, emit_event=False)
            conn.execute(
                "INSERT INTO agent_attachments(id, project_id, conversation_id, artifact_id, filename, uploaded_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (identifier, project_id, conversation_id, artifact.id, filename, principal.subject_id, utcnow()),
            )
            return public_attachment(attachment_row(conn, project_id, conversation_id, identifier))


def remove_attachment(conn, project_id, conversation_id, attachment_id, principal):
    from .agent_conversations import _conversation

    _conversation(conn, project_id, conversation_id, lock=True)
    row = attachment_row(conn, project_id, conversation_id, attachment_id)
    if row["uploaded_by"] != principal.subject_id:
        raise HTTPException(403, "只能移除自己尚未发送的附件。")
    if row["message_id"] is not None:
        raise HTTPException(409, "已发送附件属于消息审计，不能移除。")
    conn.execute("UPDATE agent_attachments SET removed_at = ? WHERE id = ?", (utcnow(), attachment_id))


def validate_attachment_ids(value):
    if (not isinstance(value, list) or len(value) > project_resource_limits().max_active_count
            or any(not isinstance(item, str) or not item.startswith("att_") or len(item) > 128 for item in value)
            or len(set(value)) != len(value)):
        raise HTTPException(422, "agent_tool_parameters_invalid")


def bind_attachments(conn, project_id, conversation_id, message_id, identifiers, principal):
    validate_attachment_ids(identifiers)
    for identifier in identifiers:
        row = attachment_row(conn, project_id, conversation_id, identifier)
        if row["uploaded_by"] != principal.subject_id:
            raise HTTPException(403, "只能发送自己上传的附件。")
        if row["message_id"] is not None:
            raise HTTPException(409, "附件已归属其他消息，请继续引用历史附件或重新上传。")
        conn.execute("UPDATE agent_attachments SET message_id = ? WHERE id = ?", (message_id, identifier))


def read_attachment(conn, turn, params, *, binary=False):
    row = attachment_row(conn, turn["project_id"], turn["conversation_id"], params["attachment_id"], sent_only=True)
    content = attachment_file(conn, row).read_bytes()
    if binary:
        return {"attachment": public_attachment(row), "content_base64": base64.b64encode(content).decode("ascii")}
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return {"attachment": public_attachment(row), "readable": False,
                "reason": "不是 UTF-8 文本；可下载到独立工作区，用现有工具处理，不代表已经解析。"}
    if "\0" in text:
        return {"attachment": public_attachment(row), "readable": False,
                "reason": "包含二进制内容；请下载到独立工作区处理。"}
    offset, size = params.get("offset", 0), params.get("chunk_size", 16384)
    end = min(offset + size, len(text))
    return {"attachment": public_attachment(row), "readable": True, "content": text[offset:end],
            "offset": offset, "next_offset": end if end < len(text) else None,
            "total_characters": len(text), "read_at": utcnow()}


def copy_attachments_to_batch(conn, turn, identifiers, actor):
    validate_attachment_ids(identifiers)
    if not identifiers:
        return None
    rows = [attachment_row(conn, turn["project_id"], turn["conversation_id"], identifier, sent_only=True)
            for identifier in identifiers]
    batch = create_project_resource_batch(tenant_id=actor.tenant_id, user_id=actor.subject_id, conn=conn)
    _check_batch_additions(conn, batch.id, [(row["filename"], row["size"]) for row in rows])
    for row in rows:
        member_id = "prm_" + uuid4().hex
        target = _batch_member_disk_path(batch.id, member_id, create_parent=True)
        copy_verified_resource_content(conn, attachment_file(conn, row), target,
                                       path=row["filename"], size_bytes=row["size"], digest_sha256=row["sha256"])
        conn.execute("INSERT INTO project_resource_batch_members(id,batch_id,kind,path,size_bytes,sha256,source,created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, 'file', ?)",
                     (member_id, batch.id, infer_project_resource_kind(row["filename"]), row["filename"], row["size"], row["sha256"], utcnow()))
    return batch.id
