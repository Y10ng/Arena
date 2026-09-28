"""监控页 artifact、审计结果、prompt 效果和趋势 schema。"""

from __future__ import annotations

from .common import *
from .monitoring_dispatcher import MonitoringTaskRun


class MonitoringArtifactSummary(BaseModel):
    # artifact 摘要同时描述存量规模和写入失败，便于定位 evidence 链路是否缺失。
    total_count: int = 0
    total_size: int = 0
    raw_output_count: int = 0
    raw_output_size: int = 0
    largest_raw_output_artifact_id: str | None = None
    largest_raw_output_project_id: str | None = None
    largest_raw_output_task_run_id: str | None = None
    largest_raw_output_size: int = 0
    evidence_count: int = 0
    deliverable_count: int = 0
    declared_count: int = 0
    declared_by_kind: dict[str, int] = Field(default_factory=dict)
    latest_artifact_id: str | None = None
    latest_created_at: str | None = None
    latest_uri: str | None = None
    write_failed_task_count: int = 0
    recent_write_failures: list[MonitoringTaskRun] = Field(default_factory=list)


class MonitoringValidationResultSummary(BaseModel):
    total_count: int = 0
    valid_count: int = 0
    invalid_count: int = 0
    rejected_count: int = 0
    unknown_count: int = 0


class MonitoringProtocolWriteSummary(BaseModel):
    total_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    unknown_count: int = 0


class MonitoringDecisionResultSummary(BaseModel):
    # decision 结果按图谱动作拆分，区分完成目标、创建 Intent 和无动作/阻塞。
    total_count: int = 0
    complete_count: int = 0
    fact_count: int = 0
    intents_count: int = 0
    noop_count: int = 0
    unknown_count: int = 0
    low_confidence_count: int = 0
    blocked_count: int = 0
    target_dependency_count: int = 0
    created_intent_count: int = 0
    concluded_created_intent_count: int = 0
    goal_path_created_intent_count: int = 0
    open_created_intent_count: int = 0
    missing_created_intent_count: int = 0


class MonitoringResultAuditSummary(BaseModel):
    recent_event_limit: int = 100
    event_count: int = 0
    latest_event_at: str | None = None
    validation: MonitoringValidationResultSummary = Field(default_factory=MonitoringValidationResultSummary)
    protocol_write: MonitoringProtocolWriteSummary = Field(default_factory=MonitoringProtocolWriteSummary)
    decision: MonitoringDecisionResultSummary = Field(default_factory=MonitoringDecisionResultSummary)


class MonitoringPromptEffectItem(BaseModel):
    # prompt_hash 代表一次渲染版本，监控按版本聚合任务成败而不暴露完整 prompt。
    prompt_group: str = "unknown"
    prompt_name: str = "unknown"
    prompt_hash: str = "unknown"
    task_count: int = 0
    succeeded_count: int = 0
    failed_count: int = 0
    parse_error_count: int = 0
    latest_task_run_id: str | None = None
    latest_project_id: str | None = None
    latest_project_title: str | None = None
    latest_task_type: TaskType | None = None
    latest_worker_name: str | None = None
    latest_worker_type: WorkerType | None = None
    latest_task_status: TaskRunStatus | None = None
    latest_rendered_at: str | None = None


class MonitoringPromptEffectSummary(BaseModel):
    recent_event_limit: int = 100
    version_count: int = 0
    total_task_count: int = 0
    latest_rendered_at: str | None = None
    items: list[MonitoringPromptEffectItem] = Field(default_factory=list)


class MonitoringTrendBucket(BaseModel):
    bucket_start: str
    bucket_end: str
    started_count: int = 0
    finished_count: int = 0
    succeeded_count: int = 0
    failed_count: int = 0
    failure_rate: float = 0.0


class MonitoringTrendSummary(BaseModel):
    # 趋势桶由服务端固定窗口生成，前端只负责展示，不再重新计算失败率。
    bucket_seconds: int = 7200
    bucket_count: int = 12
    window_start: str | None = None
    window_end: str | None = None
    buckets: list[MonitoringTrendBucket] = Field(default_factory=list)
