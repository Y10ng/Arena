"""显式新删除动作的持久回收请求；不扫描历史 tombstone，不删除宿主材料。"""

import json
import time
from uuid import uuid4

from fastapi import HTTPException

from .time_utils import utcnow

MAX_ATTEMPTS = 6


def public_status(value):
    if value is None:
        return None
    return {key: value[key] for key in ("request_id", "state", "attempts", "retry_at", "error_code", "finished_at")}


def list_requests(conn, principal):
    rows = conn.execute(
        "SELECT id, cleanup_request_json FROM agent_conversations WHERE tenant_id = ? AND created_by = ? "
        "AND cleanup_request_json IS NOT NULL ORDER BY deleted_at DESC, id",
        (principal.tenant_id, principal.subject_id),
    ).fetchall()
    return [{"conversation_id": row["id"], "cleanup": public_status(json.loads(row["cleanup_request_json"]))}
            for row in rows]


def request_cleanup(conn, conversation):
    if conversation["deleted_at"] is not None:
        # 历史已删除会话不因升级或重试 DELETE 获得新的破坏性动作。
        return json.loads(conversation["cleanup_request_json"]) if conversation["cleanup_request_json"] else None
    request = {"request_id": "cleanup_" + uuid4().hex, "state": "requested", "targets": None,
               "attempts": 0, "retry_at": 0, "error_code": None, "finished_at": None}
    if conversation["session_machine_id"] is None:
        request.update(state="completed", targets=[], finished_at=utcnow())
    conn.execute("UPDATE agent_conversations SET deleted_at = ?, cleanup_request_json = ? WHERE id = ?",
                 (utcnow(), json.dumps(request), conversation["id"]))
    return request


def pending(conn, principal):
    rows = conn.execute("SELECT id, cleanup_request_json FROM agent_conversations WHERE session_machine_id = ? "
                        "AND deleted_at IS NOT NULL AND cleanup_request_json IS NOT NULL ORDER BY deleted_at, id",
                        (principal.subject_id,)).fetchall()
    items = []
    for row in rows:
        value = json.loads(row["cleanup_request_json"])
        if value["state"] != "completed" and value["attempts"] < MAX_ATTEMPTS and value["retry_at"] <= time.time():
            items.append({"conversation_id": row["id"], **value})
        if len(items) >= 20:
            break
    return items


def update(conn, conversation_id, request_id, action, targets, error_code, principal):
    from .agent_conversations import _conversation
    row = _conversation(conn, conversation_id, lock=True, include_deleted=True)
    if principal.kind != "machine" or row["session_machine_id"] != principal.subject_id:
        raise HTTPException(403, "conversation_cleanup_owner_mismatch")
    request = json.loads(row["cleanup_request_json"]) if row["cleanup_request_json"] else None
    if row["deleted_at"] is None or not request or request["request_id"] != request_id:
        raise HTTPException(409, "conversation_cleanup_request_mismatch")
    for target in targets or []:
        if target["conversation_id"] != conversation_id or target["machine_id"] != principal.subject_id:
            raise HTTPException(409, "conversation_cleanup_identity_conflict")
    if action == "bind":
        if targets is None:
            raise HTTPException(422, "conversation_cleanup_targets_required")
        if request["targets"] is not None and request["targets"] != targets:
            raise HTTPException(409, "conversation_cleanup_identity_conflict")
        if request["state"] == "completed":
            return request
        if request["attempts"] >= MAX_ATTEMPTS:
            raise HTTPException(409, "conversation_cleanup_exhausted")
        request.update(targets=targets, state="requested", error_code=None,
                       attempts=request["attempts"] + 1, retry_at=time.time() + 2 ** min(request["attempts"] + 1, 6))
    elif action in {"completed", "failed"}:
        if action == "failed" and request["targets"] is None and targets is None:
            request.update(attempts=request["attempts"] + 1, retry_at=time.time() + 2 ** min(request["attempts"] + 1, 6))
        elif request["targets"] is None or request["targets"] != targets:
            raise HTTPException(409, "conversation_cleanup_identity_conflict")
        if request["state"] == "completed":
            if action != "completed":
                raise HTTPException(409, "conversation_cleanup_finished")
            return request
        request.update(state=action, error_code=error_code if action == "failed" else None,
                       finished_at=utcnow() if action == "completed" else None)
    else:
        raise HTTPException(422, "conversation_cleanup_action_invalid")
    conn.execute("UPDATE agent_conversations SET cleanup_request_json = ? WHERE id = ?",
                 (json.dumps(request), conversation_id))
    return request
