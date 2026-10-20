"""监控 summary 编排服务。"""

from __future__ import annotations

from cairn.server.auth import RequestPrincipal, verification_project_access_clause

from .common import *
from .agent_events import _agent_event_summary
from .artifacts import _artifact_summary
from .audit import _result_audit_summary
from .dispatcher import _dispatcher_summary, record_dispatcher_heartbeat
from .errors import _error_counts
from .prompts import _prompt_effect_summary
from .tasks import _active_task_summaries, _recent_failure_summaries
from .trends import _trend_summary
from cairn.server.schemas.admin import (
    TenantDispatcherLiveness,
    TenantMonitoringSummary,
    TenantTaskRunView,
)
from .workers import _worker_summaries
from ..vulnerabilities import vulnerabilities_summary


def build_monitoring_summary(
    conn,
    *,
    long_running_seconds: int = 1800,
    profile: SummaryProfile = "full",
) -> MonitoringSummary:
    now = utcnow()
    # summary 是读模型接口，读取前主动清理过期租约以减少前端二次推断。
    expire_workers(conn)
    expire_reason_leases(conn)
    dispatcher = _dispatcher_summary(conn, now)
    if profile == "dispatcher":
        return MonitoringSummary(
            generated_at=now,
            long_running_threshold_seconds=long_running_seconds,
            dispatcher=dispatcher,
        )

    intent_timeout = get_intent_timeout(conn)
    reason_timeout = get_reason_timeout(conn)
    running_tasks, long_running_tasks, queued_tasks = _active_task_summaries(
        conn,
        now,
        long_running_seconds=long_running_seconds,
        intent_timeout_seconds=intent_timeout,
        reason_timeout_seconds=reason_timeout,
    )
    # profile 控制聚合成本：overview/dispatcher 避开重型事件统计，full 才给完整审计视图。
    include_recent_failures = profile in {"full", "overview"}
    include_artifacts = profile in {"full", "monitoring"}
    include_agent_events = profile in {"full", "monitoring"}
    include_full_agent_events = profile == "full"
    include_deep_summaries = profile == "full"
    # 漏洞统计：overview（总览卡片）和 full 都填充，dispatcher/monitoring 省略以省开销。
    include_vulnerabilities = profile in {"full", "overview"}
    return MonitoringSummary(
        generated_at=now,
        long_running_threshold_seconds=long_running_seconds,
        dispatcher=dispatcher,
        queued_tasks=queued_tasks,
        running_tasks=running_tasks,
        long_running_tasks=long_running_tasks,
        workers=_worker_summaries(conn, now),
        artifacts=_artifact_summary(conn, now) if include_artifacts else MonitoringArtifactSummary(),
        result_audit=_result_audit_summary(conn) if include_deep_summaries else MonitoringResultAuditSummary(),
        prompt_effect=_prompt_effect_summary(conn) if include_deep_summaries else MonitoringPromptEffectSummary(),
        trends=_trend_summary(conn, now) if include_deep_summaries else MonitoringTrendSummary(),
        agent_events=_agent_event_summary(conn, full=include_full_agent_events)
        if include_agent_events
        else MonitoringAgentEventSummary(),
        recent_failures=_recent_failure_summaries(conn, now) if include_recent_failures else [],
        error_counts=_error_counts(conn, now),
        vulnerabilities=VulnerabilitiesSummary(
            **vulnerabilities_summary(conn)
        ) if include_vulnerabilities else VulnerabilitiesSummary(),
    )


def build_tenant_monitoring_summary(
    conn,
    *,
    tenant_id: str,
    long_running_seconds: int = 1800,
    principal: RequestPrincipal | None = None,
) -> TenantMonitoringSummary:
    """只聚合当前 Tenant 数据，并将共享 Dispatcher 降为无诊断 liveness。"""
    now = utcnow()
    # Tenant 读模型与全局 summary 使用相同的过期判定，避免展示已失效的运行状态。
    expire_workers(conn)
    expire_reason_leases(conn)
    access_clause, access_params = ("1 = 1", ()) if principal is None else verification_project_access_clause(principal)
    totals = conn.execute(
        f"""
        SELECT COUNT(*) AS project_count,
               SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active_project_count
        FROM projects p
        WHERE p.tenant_id = ? AND ({access_clause})
        """,
        (tenant_id, *access_params),
    ).fetchone()
    dispatcher = _dispatcher_summary(conn, now)
    running, long_running, queued = _active_task_summaries(
        conn,
        now,
        long_running_seconds=long_running_seconds,
        intent_timeout_seconds=get_intent_timeout(conn),
        reason_timeout_seconds=get_reason_timeout(conn),
        tenant_id=tenant_id,
        principal=principal,
    )

    def tenant_task(task: MonitoringTaskRun) -> TenantTaskRunView:
        return TenantTaskRunView(
            id=task.id,
            project_id=task.project_id,
            project_title=task.project_title,
            task_type=task.task_type,
            worker_name=task.worker_name,
            status=task.status,
            queued_at=task.queued_at,
            started_at=task.started_at,
            ended_at=task.ended_at,
            duration_seconds=task.duration_seconds,
            heartbeat_status=task.heartbeat_status,
        )

    return TenantMonitoringSummary(
        generated_at=now,
        long_running_threshold_seconds=long_running_seconds,
        project_count=totals["project_count"] or 0,
        active_project_count=totals["active_project_count"] or 0,
        dispatcher=TenantDispatcherLiveness(
            status=dispatcher.effective_status,
            stale=dispatcher.heartbeat_stale,
            updated_at=dispatcher.updated_at,
        ),
        queued_tasks=[tenant_task(item) for item in queued],
        running_tasks=[tenant_task(item) for item in running],
        long_running_tasks=[tenant_task(item) for item in long_running],
        recent_failures=[
            tenant_task(item)
            for item in _recent_failure_summaries(conn, now, tenant_id=tenant_id, principal=principal)
        ],
        vulnerabilities=VulnerabilitiesSummary(
            **vulnerabilities_summary(conn, tenant_id=tenant_id, principal=principal)
        ),
    )


__all__ = [
    "SummaryProfile",
    "build_monitoring_summary",
    "build_tenant_monitoring_summary",
    "record_dispatcher_heartbeat",
]
