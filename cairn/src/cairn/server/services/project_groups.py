"""项目组服务：以 Tenant 隔离资源，owner 仅记录创建主体。"""

from __future__ import annotations

from cairn.server.db import get_conn
from cairn.server.models import (
    CreateProjectGroupRequest,
    ProjectGroup,
    UpdateProjectGroupRequest,
)

from .common import *
from .ids import next_project_group_id
from .time_utils import utcnow


def _require_tenant_id(tenant_id: str) -> str:
    if not tenant_id:
        raise ValueError("tenant_id is required")
    return tenant_id


def group_from_row(row: sqlite3.Row) -> ProjectGroup:
    return ProjectGroup(
        id=row["id"],
        name=row["name"],
        owner=row["owner"],
        created_at=row["created_at"],
    )


def get_project_group_or_404(
    conn: sqlite3.Connection,
    group_id: str,
    *,
    tenant_id: str,
) -> sqlite3.Row:
    tenant_id = _require_tenant_id(tenant_id)
    row = conn.execute(
        "SELECT * FROM project_groups WHERE id = ? AND tenant_id = ?",
        (group_id, tenant_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Project group not found")
    return row


def list_project_groups(*, tenant_id: str) -> list[ProjectGroup]:
    tenant_id = _require_tenant_id(tenant_id)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM project_groups WHERE tenant_id = ? ORDER BY created_at",
            (tenant_id,),
        ).fetchall()
        return [group_from_row(row) for row in rows]


def create_project_group(
    body: CreateProjectGroupRequest,
    *,
    tenant_id: str,
    actor_id: str,
) -> ProjectGroup:
    tenant_id = _require_tenant_id(tenant_id)
    if not actor_id:
        raise ValueError("actor_id is required")
    with get_conn() as conn:
        gid = next_project_group_id(conn)
        now = utcnow()
        # owner 仅作创建者归因，客户端提交值不参与写入或授权。
        conn.execute(
            "INSERT INTO project_groups (id, name, owner, created_at, tenant_id) VALUES (?, ?, ?, ?, ?)",
            (gid, body.name, actor_id, now, tenant_id),
        )
        row = get_project_group_or_404(conn, gid, tenant_id=tenant_id)
        return group_from_row(row)


def update_project_group(
    group_id: str,
    body: UpdateProjectGroupRequest,
    *,
    tenant_id: str,
) -> ProjectGroup:
    with get_conn() as conn:
        get_project_group_or_404(conn, group_id, tenant_id=tenant_id)
        conn.execute("UPDATE project_groups SET name = ? WHERE id = ?", (body.name, group_id))
        row = get_project_group_or_404(conn, group_id, tenant_id=tenant_id)
        return group_from_row(row)


def delete_project_group(group_id: str, *, tenant_id: str) -> None:
    # 组内项目 group_id 由 FK ON DELETE SET NULL 置空，不删项目。
    with get_conn() as conn:
        get_project_group_or_404(conn, group_id, tenant_id=tenant_id)
        conn.execute("DELETE FROM project_groups WHERE id = ?", (group_id,))
