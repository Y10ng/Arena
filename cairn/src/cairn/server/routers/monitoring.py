"""监控聚合接口，把 dispatcher 心跳、task run 与 agent events 汇总为控制台视图。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from cairn.server.auth import (
    RequestPrincipal,
    protect_cookie_request,
    require_dispatcher,
    require_user_tenant_project,
)
from cairn.server.db import get_conn
from cairn.server.models import (
    DispatcherHeartbeatRequest,
    DispatcherHeartbeatStatus,
    MonitoringSummary,
    TenantMonitoringSummary,
)
from cairn.server.services.monitoring.agent_events import _agent_event_summary
from cairn.server.services.monitoring.artifacts import _artifact_summary
from cairn.server.services.monitoring.audit import _result_audit_summary
from cairn.server.services.monitoring.dispatcher import _dispatcher_summary
from cairn.server.services.monitoring.errors import _error_counts
from cairn.server.services.monitoring.prompts import _prompt_effect_summary
from cairn.server.services.monitoring import (
    SummaryProfile,
    build_monitoring_summary,
    build_tenant_monitoring_summary,
    record_dispatcher_heartbeat,
)
from cairn.server.services.monitoring.trends import _trend_summary
from cairn.server.services.monitoring.workers import _worker_summaries
from cairn.server.services.projects import expire_reason_leases, expire_workers

router = APIRouter(tags=["monitoring"])


# dispatcher heartbeat 是控制台的运行态来源，同时也承载配置漂移和退避窗口快照。
@router.post("/dispatcher/heartbeat", response_model=DispatcherHeartbeatStatus)
def update_dispatcher_heartbeat(
    body: DispatcherHeartbeatRequest,
    principal: RequestPrincipal = Depends(require_dispatcher),
):
    # Dispatcher 身份来自 bearer principal，忽略请求体中的可伪造 ID。
    trusted_body = body.model_copy(update={"dispatcher_id": principal.subject_id})
    with get_conn() as conn:
        return record_dispatcher_heartbeat(conn, trusted_body)


@router.get("/monitoring/summary", response_model=TenantMonitoringSummary)
def get_monitoring_summary(
    long_running_seconds: Annotated[int, Query(ge=0)] = 1800,
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    if principal.tenant_id is None:
        # 用户会话必须绑定个人 Tenant；缺失时按认证状态异常拒绝。
        from fastapi import HTTPException

        raise HTTPException(403, "Tenant membership required")
    with get_conn() as conn:
        return build_tenant_monitoring_summary(
            conn,
            tenant_id=principal.tenant_id,
            long_running_seconds=long_running_seconds,
            principal=principal,
        )


@router.get(
    "/projects/{project_id}/monitoring/summary",
    response_model=TenantMonitoringSummary,
)
def get_project_monitoring_summary(
    project_id: str,
    long_running_seconds: Annotated[int, Query(ge=0)] = 1800,
    principal: RequestPrincipal = Depends(require_user_tenant_project),
):
    """按目标项目 Tenant 返回监控，供超级管理员查看外部项目。"""
    with get_conn() as conn:
        row = conn.execute("SELECT tenant_id FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            from fastapi import HTTPException

            raise HTTPException(404, "Project not found")
        return build_tenant_monitoring_summary(
            conn,
            tenant_id=str(row["tenant_id"]),
            long_running_seconds=long_running_seconds,
            principal=principal,
        )


@router.get("/dispatcher/monitoring/summary", response_model=MonitoringSummary)
def get_dispatcher_monitoring_summary(
    long_running_seconds: Annotated[int, Query(ge=0)] = 1800,
    _principal: RequestPrincipal = Depends(require_dispatcher),
):
    """Dispatcher machine 只读取 dispatcher profile，不能选择全局审计聚合。"""
    with get_conn() as conn:
        return build_monitoring_summary(
            conn,
            long_running_seconds=long_running_seconds,
            profile="dispatcher",
        )
