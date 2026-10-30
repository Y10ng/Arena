"""Cookie 用户首页；业务读取与必要的全局管理读取审计分开。"""

from fastapi import APIRouter, Depends, Response

from cairn.server.auth import RequestPrincipal, protect_cookie_request, write_security_audit
from cairn.server.db import get_conn
from cairn.server.schemas.overview import OverviewResponse
from cairn.server.services.overview import get_overview

router = APIRouter(tags=["overview"])


@router.get("/overview", response_model=OverviewResponse)
def read_overview(response: Response, principal: RequestPrincipal = Depends(protect_cookie_request)):
    response.headers["Cache-Control"] = "no-store"
    with get_conn() as conn:
        result = get_overview(conn, principal)
        if principal.is_super_admin:
            write_security_audit(
                conn,
                actor_type="user",
                actor_id=principal.subject_id,
                tenant_id=principal.tenant_id,
                action="admin.overview.read",
                resource_type="admin_read_model",
                resource_id=None,
                outcome="success",
            )
        return result
