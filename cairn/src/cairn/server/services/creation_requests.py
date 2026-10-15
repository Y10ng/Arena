"""创建请求身份与业务结果同事务保存；未知结果只按请求身份恢复。"""

from __future__ import annotations

import hashlib
import json

from fastapi import HTTPException

from cairn.server.db import get_conn, schedule_transaction
from cairn.server.schemas.projects import CreateProjectRequest

from .project_records import create_project_record, get_project_detail
from .time_utils import utcnow


def begin_creation_request(conn, *, tenant_id: str, user_id: str, operation: str, request_id: str, payload: dict):
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    key = (tenant_id, user_id, operation, request_id)
    inserted = conn.execute(
        """INSERT INTO creation_requests(tenant_id,user_id,operation,request_id,request_digest,created_at)
           VALUES(?,?,?,?,?,?) ON CONFLICT(tenant_id,user_id,operation,request_id) DO NOTHING""",
        (*key, digest, utcnow()),
    )
    row = conn.execute(
        "SELECT * FROM creation_requests WHERE tenant_id=? AND user_id=? AND operation=? AND request_id=?",
        key,
    ).fetchone()
    if row["request_digest"] != digest:
        raise HTTPException(409, "同一请求 ID 的内容已改变，请先核实原请求结果")
    if inserted.rowcount == 0 and row["project_id"] is None and row["schedule_id"] is None:
        raise HTTPException(409, "原请求已提交，但关联项目已被删除，不能再次创建")
    return row


def complete_creation_request(conn, *, tenant_id: str, user_id: str, operation: str, request_id: str, project_id=None, schedule_id=None):
    conn.execute(
        """UPDATE creation_requests SET project_id=?,schedule_id=?
           WHERE tenant_id=? AND user_id=? AND operation=? AND request_id=?""",
        (project_id, schedule_id, tenant_id, user_id, operation, request_id),
    )


def create_project_request(body: CreateProjectRequest, *, tenant_id: str, user_id: str):
    with schedule_transaction() as conn:
        replay = None
        if body.request_id is not None:
            replay = begin_creation_request(
                conn, tenant_id=tenant_id, user_id=user_id, operation="project",
                request_id=str(body.request_id), payload=body.model_dump(mode="json", exclude={"request_id"}),
            )
        if replay is not None and replay["project_id"] is not None:
            project_id = replay["project_id"]
        elif replay is not None and replay["schedule_id"] is not None:
            raise HTTPException(409, "创建请求结果类型不匹配")
        else:
            project_id = create_project_record(body, conn=conn, tenant_id=tenant_id, creator=user_id).project.id
            if body.request_id is not None:
                complete_creation_request(conn, tenant_id=tenant_id, user_id=user_id, operation="project", request_id=str(body.request_id), project_id=project_id)
    return get_project_detail(project_id, tenant_id=tenant_id)


def query_creation_request(*, tenant_id: str, user_id: str, operation: str, request_id: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT project_id,schedule_id FROM creation_requests WHERE tenant_id=? AND user_id=? AND operation=? AND request_id=?",
            (tenant_id, user_id, operation, request_id),
        ).fetchone()
    return {
        "request_id": request_id, "operation": operation,
        "status": "committed" if row is not None else "unknown",
        "project_id": row["project_id"] if row is not None else None,
        "schedule_id": row["schedule_id"] if row is not None else None,
    }
