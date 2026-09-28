"""Agent event 及 task run 事件分组 schema。"""

from __future__ import annotations

from .common import *

class AgentEvent(BaseModel):
    id: str
    task_run_id: str
    project_id: str
    ts: str
    kind: AgentEventKind
    payload: dict[str, Any] = Field(default_factory=dict)


class TaskRunAgentEvents(BaseModel):
    task_run_id: str
    events: list[AgentEvent] = Field(default_factory=list)


class TaskRunResultEvent(BaseModel):
    # result 事件是审计链路中的高层结果，不包含完整 raw output。
    id: str
    task_run_id: str
    project_id: str
    ts: str
    kind: Literal["validation_result", "protocol_write", "network_flow", "decision_result"]
    payload: dict[str, Any] = Field(default_factory=dict)


class TaskRunResultEvents(BaseModel):
    task_run_id: str
    events: list[TaskRunResultEvent] = Field(default_factory=list)


class TaskRunCommandEvent(BaseModel):
    # command 事件保留状态和耗时，完整命令内容由 artifact/安全摘要承载。
    id: str
    task_run_id: str
    project_id: str
    ts: str
    kind: Literal["command_started", "command_finished"]
    phase: str = "unknown"
    command_kind: str = "unknown"
    status: Literal["success", "failed", "unknown"] = "unknown"
    exit_code: int | None = None
    returncode: int | None = None
    timed_out: bool = False
    cancelled: bool = False
    oom_killed: bool = False
    oom_evidence_source: str | None = None
    duration_seconds: float | None = None


class TaskRunCommandEvents(BaseModel):
    task_run_id: str
    events: list[TaskRunCommandEvent] = Field(default_factory=list)


class TaskRunPromptEvent(BaseModel):
    id: str
    task_run_id: str
    project_id: str
    ts: str
    kind: Literal["prompt_rendered"] = "prompt_rendered"
    phase: str = "unknown"
    prompt_group: str = "unknown"
    prompt_name: str = "unknown"
    prompt_hash: str = "unknown"
    prompt_bytes: int | None = None
    prompt_chars: int | None = None
    replacement_keys: list[str] = Field(default_factory=list)
    worker_name: str = "unknown"
    worker_type: str = "unknown"


class TaskRunPromptEvents(BaseModel):
    task_run_id: str
    events: list[TaskRunPromptEvent] = Field(default_factory=list)


# pi 原生工具事件与历史 hook 审计共用读模型，名称按来源原样保留。
# pi 不复制工具输入/响应正文；历史记录已有的操作输入仍可由授权用户读取。
class TaskRunHookEvent(BaseModel):
    id: str
    task_run_id: str
    project_id: str
    ts: str
    kind: Literal["hook_operation"] = "hook_operation"
    session_id: str | None = None
    hook_event_name: str = "unknown"
    tool_name: str = "unknown"
    operation_id: str | None = None
    status: str = "unknown"
    recorded_at: str | None = None
    phase: str = "unknown"
    cwd: str | None = None
    tool_input: dict[str, Any] = Field(default_factory=dict)
    tool_response: dict[str, Any] = Field(default_factory=dict)
    command: str | None = None
    command_kind: str | None = None
    file_path: str | None = None
    file_path_digest: str | None = None
    query: str | None = None
    url: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    artifact_id: str | None = None
    risk_level: str = "unknown"
    summary: str | None = None

    @field_validator("hook_event_name")
    @classmethod
    def validate_hook_event_name(cls, value: str) -> str:
        return value if value in HOOK_EVENT_NAMES else "unknown"

    @field_validator("tool_name")
    @classmethod
    def validate_tool_name(cls, value: str) -> str:
        return value if value in HOOK_TOOL_NAMES else "unknown"

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        text = value.strip().lower()
        return text if text in HOOK_OPERATION_STATUSES else "unknown"

    @field_validator("risk_level")
    @classmethod
    def validate_risk_level(cls, value: str) -> str:
        text = value.strip().lower()
        return text if text in HOOK_RISK_LEVELS else "unknown"

    @field_validator("file_path_digest")
    @classmethod
    def validate_file_path_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip().lower()
        if len(text) == 64 and all(char in "0123456789abcdef" for char in text):
            return text
        return sha256(text.encode("utf-8")).hexdigest() if text else None


class TaskRunHookEvents(BaseModel):
    task_run_id: str
    events: list[TaskRunHookEvent] = Field(default_factory=list)


class CreateAgentEventRequest(BaseModel):
    task_run_id: str
    kind: AgentEventKind
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("task_run_id")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


__all__ = [name for name in globals() if not name.startswith("__")]
