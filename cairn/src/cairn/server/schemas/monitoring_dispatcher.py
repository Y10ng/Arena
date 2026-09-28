"""Dispatcher 与 task run 监控 schema。"""

from __future__ import annotations

from .common import *


# 监控页中的 task run 摘要，包含任务、artifact 和心跳三个维度。
class MonitoringTaskRun(BaseModel):
    id: str
    project_id: str
    project_title: str
    project_status: str
    intent_id: str | None = None
    task_type: TaskType
    worker_name: str
    worker_type: WorkerType
    status: TaskRunStatus
    session_id: str | None = None
    queued_at: str | None = None
    queue_wait_seconds: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_seconds: int | None = None
    error_type: TaskRunErrorType | None = None
    error_message: str | None = None
    artifact_count: int = 0
    artifact_root: str | None = None
    heartbeat_source: Literal["intent", "reason", "none"] = "none"
    heartbeat_status: Literal["active", "missing", "stale", "not_tracked"] = "not_tracked"
    heartbeat_at: str | None = None
    heartbeat_age_seconds: int | None = None
    heartbeat_timeout_seconds: int | None = None
    heartbeat_worker: str | None = None


class MonitoringWorkerSummary(BaseModel):
    # worker 摘要融合配置、运行中任务和最近健康检查，不把它当作单一心跳状态。
    worker_name: str
    worker_type: WorkerType
    health: Literal["idle", "running", "healthy", "unhealthy"]
    configured: bool = False
    task_types: list[TaskType] = Field(default_factory=list)
    priority: int | None = None
    max_running: int | None = None
    pool_running_count: int | None = None
    supports_resume: bool | None = None
    supports_json_mode: bool | None = None
    max_context: int | None = None
    running_count: int = 0
    queued_count: int = 0
    succeeded_count: int = 0
    failed_count: int = 0
    timed_out_count: int = 0
    cancelled_count: int = 0
    consecutive_failure_count: int = 0
    last_status: TaskRunStatus | None = None
    last_error_type: TaskRunErrorType | None = None
    last_started_at: str | None = None
    last_ended_at: str | None = None
    latest_healthcheck_status: Literal["success", "failed", "unknown"] | None = None
    latest_healthcheck_at: str | None = None
    latest_healthcheck_phase: str | None = None
    latest_healthcheck_command_kind: str | None = None
    latest_healthcheck_duration_seconds: float | None = None


class MonitoringProjectConcurrency(BaseModel):
    project_id: str
    project_title: str
    project_status: str
    running_task_count: int = 0
    queued_task_count: int = 0
    active_reason_worker: str | None = None


class DispatcherWorkerCooldown(BaseModel):
    # 全局 cooldown 解释 worker 级故障后的暂缓调度窗口。
    worker_name: str
    worker_type: WorkerType | None = None
    error_type: TaskRunErrorType
    retry_after_seconds: int = Field(ge=0)
    retry_until_at: str | None = None

    @field_validator("worker_name")
    @classmethod
    def validate_cooldown_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("error_type", mode="before")
    @classmethod
    def normalize_error_type(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        return value.strip()


class DispatcherWorkerRejection(BaseModel):
    # rejection 是 project/task/worker 维度的退避，不影响同一 worker 处理其他项目。
    project_id: str
    task_type: TaskType
    worker_name: str
    worker_type: WorkerType | None = None
    error_type: TaskRunErrorType
    retry_after_seconds: int = Field(ge=0)
    retry_until_at: str | None = None

    @field_validator("project_id", "worker_name")
    @classmethod
    def validate_rejection_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("error_type", mode="before")
    @classmethod
    def normalize_error_type(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        return value.strip()


class DispatcherWorkerPoolItem(BaseModel):
    # 心跳回显 worker 配置时只暴露 env key，不暴露实际环境变量值。
    worker_id: str | None = None
    worker_name: str
    engine: WorkerType | None = None
    worker_type: WorkerType
    model_id: str = ""
    task_types: list[TaskType] = Field(default_factory=list)
    priority: int = Field(ge=0)
    concurrency: int | None = Field(default=None, gt=0)
    max_running: int = Field(gt=0)
    running_count: int = Field(default=0, ge=0)
    enabled: bool = True
    supports_resume: bool | None = None
    supports_json_mode: bool | None = None
    max_context: int | None = Field(default=None, gt=0)
    env_keys: list[str] = Field(default_factory=list)

    @field_validator("worker_name")
    @classmethod
    def validate_worker_name(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("task_types")
    @classmethod
    def validate_task_types(cls, value: list[TaskType]) -> list[TaskType]:
        if len(set(value)) != len(value):
            raise ValueError("task_types must be unique")
        return value


class InfrastructureWorker(DispatcherWorkerPoolItem):
    source: Literal["dispatcher_report"] = "dispatcher_report"


class ProjectWorkerOption(BaseModel):
    """项目创建页使用的最小 Worker 选择协议，不暴露共享运行诊断。"""

    worker_name: str
    worker_type: WorkerType
    model_id: str = ""
    task_types: list[TaskType] = Field(default_factory=list)
    enabled: bool = True


class InfrastructureImage(BaseModel):
    image: str
    source: Literal["dispatcher_report"] = "dispatcher_report"


class InfrastructureCapability(BaseModel):
    name: str
    source: Literal["declared_inventory", "dispatcher_report", "unavailable"] = "dispatcher_report"


class MonitoringDispatcherSummary(BaseModel):
    project_count: int = 0
    active_project_count: int = 0
    running_project_count: int = 0
    running_task_count: int = 0
    queued_task_count: int = 0
    last_dispatcher_id: str | None = None
    last_status: str | None = None
    effective_status: str | None = None
    started_at: str | None = None
    config_hash: str | None = None
    host: str | None = None
    config_path: str | None = None
    config_mtime: int | None = None
    config_sha256: str | None = None
    runtime: dict[str, Any] = Field(default_factory=dict)
    container: dict[str, Any] = Field(default_factory=dict)
    server_reachable_path: str | None = None
    last_loop_started_at: str | None = None
    last_loop_finished_at: str | None = None
    last_loop_duration_ms: int | None = None
    last_error_message: str | None = None
    updated_at: str | None = None
    heartbeat_age_seconds: int | None = Field(default=None, ge=0)
    heartbeat_stale: bool = False
    worker_cooldowns: list[DispatcherWorkerCooldown] = Field(default_factory=list)
    worker_rejections: list[DispatcherWorkerRejection] = Field(default_factory=list)
    worker_pool: list[DispatcherWorkerPoolItem] = Field(default_factory=list)
    container_images: list[str] = Field(default_factory=list)
    cap_baseline: list[str] = Field(default_factory=list)
    declared_inventory: dict[str, Any] = Field(default_factory=dict)
    projects: list[MonitoringProjectConcurrency] = Field(default_factory=list)


# Dispatcher 心跳负载。worker/images/caps 是 dispatcher 已拉取配置的回显，仅用于监控漂移。
class DispatcherHeartbeatRequest(BaseModel):
    dispatcher_id: str
    status: Literal["starting", "running", "idle", "error", "stopping"] = "running"
    started_at: str | None = None
    config_hash: str | None = None
    host: str | None = None
    config_path: str | None = None
    config_mtime: int | None = Field(default=None, ge=0)
    config_sha256: str | None = None
    runtime: dict[str, Any] = Field(default_factory=dict)
    container: dict[str, Any] = Field(default_factory=dict)
    server_reachable_path: str | None = None
    loop_started_at: str | None = None
    loop_finished_at: str | None = None
    loop_duration_ms: int | None = Field(default=None, ge=0)
    running_task_count: int = Field(default=0, ge=0)
    active_project_count: int = Field(default=0, ge=0)
    worker_cooldowns: list[DispatcherWorkerCooldown] = Field(default_factory=list)
    worker_rejections: list[DispatcherWorkerRejection] = Field(default_factory=list)
    worker_pool: list[DispatcherWorkerPoolItem] = Field(default_factory=list)
    container_images: list[str] = Field(default_factory=list)
    cap_baseline: list[str] | None = None
    declared_inventory: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None

    @field_validator("dispatcher_id")
    @classmethod
    def validate_dispatcher_id(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator(
        "config_hash",
        "started_at",
        "host",
        "config_path",
        "config_sha256",
        "server_reachable_path",
        "loop_started_at",
        "loop_finished_at",
        "error_message",
    )
    @classmethod
    def validate_optional_dispatcher_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        return text

    @field_validator("container_images", "cap_baseline")
    @classmethod
    def validate_string_inventory(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = str(item).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            cleaned.append(text)
        return cleaned


class DispatcherHeartbeatStatus(BaseModel):
    dispatcher_id: str
    status: str
    started_at: str | None = None
    config_hash: str | None = None
    host: str | None = None
    config_path: str | None = None
    config_mtime: int | None = None
    config_sha256: str | None = None
    runtime: dict[str, Any] = Field(default_factory=dict)
    container: dict[str, Any] = Field(default_factory=dict)
    server_reachable_path: str | None = None
    loop_started_at: str | None = None
    loop_finished_at: str | None = None
    loop_duration_ms: int | None = None
    running_task_count: int = 0
    active_project_count: int = 0
    worker_cooldowns: list[DispatcherWorkerCooldown] = Field(default_factory=list)
    worker_rejections: list[DispatcherWorkerRejection] = Field(default_factory=list)
    worker_pool: list[DispatcherWorkerPoolItem] = Field(default_factory=list)
    container_images: list[str] = Field(default_factory=list)
    cap_baseline: list[str] = Field(default_factory=list)
    declared_inventory: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
    updated_at: str


class DispatcherYamlDocument(BaseModel):
    path: str
    content: str
    revision: str = ""


class DispatcherConfigDocument(BaseModel):
    path: str
    content: str
    config: dict[str, Any] = Field(default_factory=dict)
    revision: str = ""


class DispatcherConfigUpdate(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)
    revision: str | None = None


class DispatcherRevisionRequest(BaseModel):
    revision: str


class DispatcherCredentialUpdate(DispatcherRevisionRequest):
    credential: str = Field(min_length=1)

    @field_validator("credential")
    @classmethod
    def reject_blank_credential(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("credential must not be blank")
        return value


class DispatcherCredentialStatus(BaseModel):
    provider_id: str
    credential_ref: str
    configured: bool
    revision: str


class DispatcherProviderView(BaseModel):
    id: str
    base_url: str
    provider_api: str
    credential_ref: str
    credential_configured: bool = False
    credential_env_override: bool = False
    credential_warning: str | None = None


class DispatcherProviderCollection(BaseModel):
    revision: str
    items: list[DispatcherProviderView] = Field(default_factory=list)


class DispatcherProviderItem(BaseModel):
    revision: str
    item: DispatcherProviderView


class DispatcherControlCollection(BaseModel):
    revision: str
    items: list[dict[str, Any]] = Field(default_factory=list)


class DispatcherControlItem(BaseModel):
    revision: str
    item: dict[str, Any] = Field(default_factory=dict)


class DispatcherControlUpdate(DispatcherRevisionRequest):
    item: dict[str, Any] = Field(default_factory=dict)
    provider: dict[str, Any] | None = None
    model: dict[str, Any] | None = None
    worker: dict[str, Any] | None = None


ConnectivityTargetType = Literal["provider", "model", "worker"]
ConnectivityTestStatus = Literal["queued", "running", "completed"]
ConnectivityTestCategory = Literal[
    "dns",
    "connect",
    "tls",
    "auth",
    "not_found",
    "rate_limited",
    "model",
    "protocol",
    "timeout",
    "ok",
]


class DispatcherConnectivityTestStart(BaseModel):
    timeout_seconds: int = Field(default=10, ge=1, le=60)


class DispatcherConnectivityTestAccepted(BaseModel):
    test_id: str
    status: ConnectivityTestStatus
    poll_url: str


class DispatcherConnectivityTest(BaseModel):
    id: str
    target_type: ConnectivityTargetType
    target_id: str
    provider_id: str
    model_id: str | None = None
    scope: str
    status: ConnectivityTestStatus
    category: ConnectivityTestCategory | None = None
    detail: str = ""
    http_status: int | None = None
    latency_ms: int | None = None
    config_revision: str
    credential_fingerprint: str
    stale: bool = False
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
