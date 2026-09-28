"""Tenant Worker 模板服务：以认证主体的 Tenant 隔离资源。"""

from __future__ import annotations

import json
from typing import Any

from cairn.server.db import get_conn
from cairn.server.models import (
    CreateWorkerTemplateRequest,
    UpdateWorkerTemplateRequest,
    WorkerTemplate,
)

from .common import *
from .ids import next_worker_template_id
from .tenant_workers import validate_tenant_worker_selection
from .time_utils import utcnow


def _require_tenant_id(tenant_id: str) -> str:
    if not tenant_id:
        raise ValueError("tenant_id is required")
    return tenant_id


def _parse_worker_names(raw: str | None) -> list[str]:
    # 历史全局 Worker 名称仅用于只读展示，不再参与授权或新写入。
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _template_worker_ids(conn: Any, template_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT tenant_worker_id
        FROM worker_template_tenant_workers
        WHERE template_id = ?
        ORDER BY position
        """,
        (template_id,),
    ).fetchall()
    return [str(row["tenant_worker_id"]) for row in rows]


def template_from_row(conn: Any, row: Any) -> WorkerTemplate:
    return WorkerTemplate(
        id=row["id"],
        name=row["name"],
        owner=row["owner"],
        tenant_worker_ids=_template_worker_ids(conn, row["id"]),
        worker_names=_parse_worker_names(row["worker_names_json"]),
        created_at=row["created_at"],
    )


def get_worker_template_or_404(
    conn: Any,
    template_id: str,
    *,
    tenant_id: str,
):
    tenant_id = _require_tenant_id(tenant_id)
    row = conn.execute(
        "SELECT * FROM worker_templates WHERE id = ? AND tenant_id = ?",
        (template_id, tenant_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Worker template not found")
    return row


def list_worker_templates(*, tenant_id: str) -> list[WorkerTemplate]:
    tenant_id = _require_tenant_id(tenant_id)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM worker_templates WHERE tenant_id = ? ORDER BY created_at",
            (tenant_id,),
        ).fetchall()
        return [template_from_row(conn, row) for row in rows]


def create_worker_template(
    body: CreateWorkerTemplateRequest,
    *,
    tenant_id: str,
    actor_id: str,
) -> WorkerTemplate:
    tenant_id = _require_tenant_id(tenant_id)
    if not actor_id:
        raise ValueError("actor_id is required")
    with get_conn() as conn:
        workers = validate_tenant_worker_selection(
            conn,
            tenant_id=tenant_id,
            worker_ids=body.tenant_worker_ids,
        )
        template_id = next_worker_template_id(conn)
        now = utcnow()
        # owner 仅作创建者归因，客户端提交值不参与写入或授权。
        conn.execute(
            "INSERT INTO worker_templates (id, name, owner, worker_names_json, created_at, tenant_id) "
            "VALUES (?, ?, ?, '[]', ?, ?)",
            (template_id, body.name, actor_id, now, tenant_id),
        )
        _replace_template_workers(
            conn,
            template_id=template_id,
            tenant_id=tenant_id,
            workers=workers,
            created_at=now,
        )
        row = get_worker_template_or_404(conn, template_id, tenant_id=tenant_id)
        return template_from_row(conn, row)


def update_worker_template(
    template_id: str,
    body: UpdateWorkerTemplateRequest,
    *,
    tenant_id: str,
) -> WorkerTemplate:
    with get_conn() as conn:
        get_worker_template_or_404(conn, template_id, tenant_id=tenant_id)
        workers = None
        if body.tenant_worker_ids is not None:
            workers = validate_tenant_worker_selection(
                conn,
                tenant_id=tenant_id,
                worker_ids=body.tenant_worker_ids,
            )
        if body.name is not None:
            conn.execute(
                "UPDATE worker_templates SET name = ? WHERE id = ? AND tenant_id = ?",
                (body.name, template_id, tenant_id),
            )
        if workers is not None:
            _replace_template_workers(
                conn,
                template_id=template_id,
                tenant_id=tenant_id,
                workers=workers,
                created_at=utcnow(),
            )
        row = get_worker_template_or_404(conn, template_id, tenant_id=tenant_id)
        return template_from_row(conn, row)


def delete_worker_template(template_id: str, *, tenant_id: str) -> None:
    with get_conn() as conn:
        get_worker_template_or_404(conn, template_id, tenant_id=tenant_id)
        conn.execute(
            "DELETE FROM worker_templates WHERE id = ? AND tenant_id = ?",
            (template_id, tenant_id),
        )


def _replace_template_workers(
    conn: Any,
    *,
    template_id: str,
    tenant_id: str,
    workers: list[Any],
    created_at: str,
) -> None:
    conn.execute(
        "DELETE FROM worker_template_tenant_workers WHERE template_id = ?",
        (template_id,),
    )
    for position, worker in enumerate(workers):
        conn.execute(
            """
            INSERT INTO worker_template_tenant_workers
                (template_id, tenant_id, tenant_worker_id, position, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (template_id, tenant_id, worker["id"], position, created_at),
        )
