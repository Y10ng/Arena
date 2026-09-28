"""监控 agent event、hook、command 和 network_flow schema。"""

from __future__ import annotations

from .common import *


def _network_flow_breakdown_key(value: object, allowed: set[str]) -> str:
    # 历史 network_flow key 只接受短白名单 token，未知值归到 unknown 以防前端键爆炸。
    if not isinstance(value, str):
        return "unknown"
    text = value.strip().lower()
    if not text or len(text) > 128:
        return "unknown"
    if any(char not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for char in text):
        return "unknown"
    return text if text in allowed else "unknown"


def _network_flow_breakdown_count(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            count = int(text)
        except ValueError:
            return None
        return count if count > 0 else None
    return None


def _network_flow_breakdown(value: object, allowed: set[str]) -> dict[str, int]:
    # 监控接口面对历史 payload，要容忍非法 key/count 并收敛到 unknown。
    if not isinstance(value, dict):
        return {}
    counts: dict[str, int] = {}
    for raw_key, raw_count in value.items():
        count = _network_flow_breakdown_count(raw_count)
        if count is None:
            continue
        key = _network_flow_breakdown_key(raw_key, allowed)
        counts[key] = counts.get(key, 0) + count
    return dict(sorted(counts.items()))


class MonitoringAgentEventKindSummary(BaseModel):
    kind: str
    count: int = 0


class MonitoringCommandEventSummary(BaseModel):
    total_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    unknown_count: int = 0


class MonitoringNetworkFlowSummary(BaseModel):
    compatibility_status: Literal["read_only_compat"] = "read_only_compat"
    compatibility_message: str = "历史兼容摘要；当前主线不做网络流量采集。"
    total_count: int = 0
    observed_count: int = 0
    allowed_count: int = 0
    blocked_count: int = 0
    failed_count: int = 0
    unknown_count: int = 0
    latest_event_at: str | None = None
    by_network_plane: dict[str, int] = Field(default_factory=dict)
    by_protocol: dict[str, int] = Field(default_factory=dict)
    by_flow_id: dict[str, int] = Field(default_factory=dict)

    @field_validator("by_network_plane", mode="before")
    @classmethod
    def validate_network_plane_breakdown(cls, value: object) -> dict[str, int]:
        return _network_flow_breakdown(value, NETWORK_FLOW_NETWORK_PLANES)

    @field_validator("by_protocol", mode="before")
    @classmethod
    def validate_protocol_breakdown(cls, value: object) -> dict[str, int]:
        return _network_flow_breakdown(value, NETWORK_FLOW_PROTOCOLS)

    @field_validator("by_flow_id", mode="before")
    @classmethod
    def validate_flow_id_breakdown(cls, value: object) -> dict[str, int]:
        return _network_flow_breakdown(value, NETWORK_FLOW_IDS)


class MonitoringHookFailure(BaseModel):
    event_id: str
    task_run_id: str
    project_id: str
    project_title: str
    task_type: TaskType
    worker_name: str
    worker_type: WorkerType
    hook_event_name: str = "unknown"
    tool_name: str = "unknown"
    status: str = "unknown"
    risk_level: str = "unknown"
    summary: str | None = None
    artifact_id: str | None = None
    occurred_at: str


class MonitoringKnowledgeHit(BaseModel):
    file_path: str
    count: int = 0
    latest_task_run_id: str
    latest_project_id: str
    latest_project_title: str
    latest_task_type: TaskType
    latest_worker_name: str
    latest_worker_type: WorkerType
    latest_accessed_at: str


class MonitoringHookOperationSummary(BaseModel):
    # hook 汇总把工具失败、高风险操作和知识库命中拆开，方便监控页分别提示。
    total_count: int = 0
    failed_count: int = 0
    denied_count: int = 0
    high_risk_count: int = 0
    latest_event_at: str | None = None
    by_tool: dict[str, int] = Field(default_factory=dict)
    by_event: dict[str, int] = Field(default_factory=dict)
    knowledge_hit_count: int = 0
    knowledge_hits: list[MonitoringKnowledgeHit] = Field(default_factory=list)
    recent_failures: list[MonitoringHookFailure] = Field(default_factory=list)
    recent_high_risk: list[MonitoringHookFailure] = Field(default_factory=list)


class MonitoringCommandFailure(BaseModel):
    event_id: str
    task_run_id: str
    project_id: str
    project_title: str
    task_type: TaskType
    worker_name: str
    worker_type: WorkerType
    phase: str = "unknown"
    command_kind: str = "unknown"
    status: Literal["failed"] = "failed"
    exit_code: int | None = None
    returncode: int | None = None
    timed_out: bool = False
    cancelled: bool = False
    duration_seconds: float | None = None
    finished_at: str


class MonitoringRunningCommand(BaseModel):
    task_run_id: str
    project_id: str
    project_title: str
    task_type: TaskType
    worker_name: str
    worker_type: WorkerType
    phase: str
    command_kind: str = "unknown"
    started_at: str
    age_seconds: int | None = None


class MonitoringAgentEventSummary(BaseModel):
    recent_event_limit: int = 200
    event_count: int = 0
    latest_event_at: str | None = None
    by_kind: list[MonitoringAgentEventKindSummary] = Field(default_factory=list)
    command_finished: MonitoringCommandEventSummary = Field(default_factory=MonitoringCommandEventSummary)
    network_flow: MonitoringNetworkFlowSummary = Field(default_factory=MonitoringNetworkFlowSummary)
    hook_operation: MonitoringHookOperationSummary = Field(default_factory=MonitoringHookOperationSummary)
    running_commands: list[MonitoringRunningCommand] = Field(default_factory=list)
    recent_command_failures: list[MonitoringCommandFailure] = Field(default_factory=list)
