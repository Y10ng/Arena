"""监控聚合 summary schema。"""

from __future__ import annotations

from .common import *
from .monitoring_audit import (
    MonitoringArtifactSummary,
    MonitoringPromptEffectSummary,
    MonitoringResultAuditSummary,
    MonitoringTrendSummary,
)
from .monitoring_dispatcher import MonitoringDispatcherSummary, MonitoringTaskRun, MonitoringWorkerSummary
from .monitoring_events import MonitoringAgentEventSummary


class MonitoringErrorAggregate(BaseModel):
    error_type: TaskRunErrorType
    count: int
    latest_task_run: MonitoringTaskRun | None = None


# 漏洞管理总览统计（所有场景通用，跨项目聚合）。
class VulnerabilitiesSummary(BaseModel):
    total: int = 0
    by_severity: dict[str, int] = Field(default_factory=dict)
    by_status: dict[str, int] = Field(default_factory=dict)
    per_project: list[dict[str, object]] = Field(default_factory=list)


# 监控总览的聚合读模型；各子对象可按 profile 选择性填充。
class MonitoringSummary(BaseModel):
    generated_at: str
    long_running_threshold_seconds: int
    dispatcher: MonitoringDispatcherSummary
    queued_tasks: list[MonitoringTaskRun] = Field(default_factory=list)
    running_tasks: list[MonitoringTaskRun] = Field(default_factory=list)
    long_running_tasks: list[MonitoringTaskRun] = Field(default_factory=list)
    workers: list[MonitoringWorkerSummary] = Field(default_factory=list)
    artifacts: MonitoringArtifactSummary = Field(default_factory=MonitoringArtifactSummary)
    result_audit: MonitoringResultAuditSummary = Field(default_factory=MonitoringResultAuditSummary)
    prompt_effect: MonitoringPromptEffectSummary = Field(default_factory=MonitoringPromptEffectSummary)
    trends: MonitoringTrendSummary = Field(default_factory=MonitoringTrendSummary)
    agent_events: MonitoringAgentEventSummary = Field(default_factory=MonitoringAgentEventSummary)
    recent_failures: list[MonitoringTaskRun] = Field(default_factory=list)
    error_counts: list[MonitoringErrorAggregate] = Field(default_factory=list)
    vulnerabilities: VulnerabilitiesSummary = Field(default_factory=VulnerabilitiesSummary)
