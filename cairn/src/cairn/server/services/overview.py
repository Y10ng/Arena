"""首页轻量业务读取，不执行租约维护、过期复测或项目详情组装。"""

from cairn.server.auth import RequestPrincipal
from cairn.server.schemas.overview import OverviewResponse

from .consumption import overview_consumption, project_consumption
from .time_utils import utcnow
from .vulnerabilities import vulnerability_counts, vulnerability_read_scope


def get_overview(conn, principal: RequestPrincipal) -> OverviewResponse:
    tenant_id = None if principal.is_super_admin else principal.tenant_id
    project_scope = "NOT EXISTS (SELECT 1 FROM verification_requests vr WHERE vr.project_id = p.id)"
    project_params: tuple = ()
    if tenant_id is not None:
        project_scope += " AND p.tenant_id = ?"
        project_params = (tenant_id,)
    by_status = {
        row["status"]: int(row["c"])
        for row in conn.execute(
            f"SELECT p.status, COUNT(*) AS c FROM projects p WHERE {project_scope} GROUP BY p.status",
            project_params,
        ).fetchall()
    }
    projects = conn.execute(
        f"""
        SELECT p.id, p.title, p.status, p.created_at, p.tenant_id FROM projects p
        WHERE {project_scope}
        ORDER BY CASE p.status WHEN 'active' THEN 0 WHEN 'pending' THEN 1
                 WHEN 'paused' THEN 2 ELSE 3 END, p.created_at DESC, p.id DESC
        LIMIT 6
        """,
        project_params,
    ).fetchall()
    counts = vulnerability_counts(conn, tenant_id=tenant_id, principal=principal)
    scope, params = vulnerability_read_scope(tenant_id=tenant_id, principal=principal)
    vulnerabilities = conn.execute(
        f"""
        SELECT v.id, v.project_id, p.title AS project_title, v.title,
               v.severity, v.status, v.created_at
        FROM vulnerabilities v JOIN projects p ON p.id = v.project_id
        WHERE {scope}
        ORDER BY v.created_at DESC, v.id DESC, v.project_id DESC
        LIMIT 5
        """,
        params,
    ).fetchall()
    consumption = project_consumption(conn, {row["id"]: row["tenant_id"] for row in projects})
    return OverviewResponse(
        generated_at=utcnow(),
        consumption=overview_consumption(conn, principal),
        projects={"total": sum(by_status.values()), "by_status": by_status},
        vulnerabilities=counts,
        project_previews=[{**dict(row), "consumption": consumption[row["id"]]} for row in projects],
        vulnerability_previews=[dict(row) for row in vulnerabilities],
    )
