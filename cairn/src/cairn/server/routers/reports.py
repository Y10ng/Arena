"""浏览器用户可访问的历史评估报告 inventory。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from cairn.server.auth import (
    RequestPrincipal,
    protect_cookie_request,
    write_security_audit,
)
from cairn.server.db import get_conn
from cairn.server.schemas.reports import ReportInventoryPage
from cairn.server.services.report_inventory import list_report_inventory

router = APIRouter(tags=["reports"])


def _utc_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@router.get("/reports", response_model=ReportInventoryPage)
def get_reports_route(
    q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    project_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    template_key: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    if not principal.is_super_admin and principal.tenant_id is None:
        raise HTTPException(403, "Tenant membership required")
    date_from_value = _utc_timestamp(date_from)
    date_to_value = _utc_timestamp(date_to)
    if (
        date_from_value is not None
        and date_to_value is not None
        and date_from_value > date_to_value
    ):
        raise HTTPException(422, "date_from must not be after date_to")

    # 普通用户只读所属 Tenant；超级管理员跨 Tenant 查询并留下安全审计。
    tenant_id = None if principal.is_super_admin else principal.tenant_id
    with get_conn() as conn:
        result = list_report_inventory(
            conn,
            tenant_id=tenant_id,
            q=q.strip() if q is not None else None,
            project_id=project_id.strip() if project_id is not None else None,
            template_key=template_key.strip() if template_key is not None else None,
            date_from=date_from_value,
            date_to=date_to_value,
            page=page,
            page_size=page_size,
        )
        if principal.is_super_admin:
            write_security_audit(
                conn,
                actor_type="user",
                actor_id=principal.subject_id,
                tenant_id=principal.tenant_id,
                action="admin.reports.read",
                resource_type="admin_read_model",
                resource_id=None,
                outcome="success",
            )
        return result


__all__ = ["router"]
