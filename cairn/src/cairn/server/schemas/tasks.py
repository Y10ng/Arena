"""Task run 审计记录 schema。"""

from __future__ import annotations

from .common import *

# 一次 worker 执行的审计主记录，runtime_context 承载可扩展环境快照。
class TaskRun(BaseModel):
    id: str
    project_id: str
    intent_id: str | None = None
    task_type: TaskType
    worker_name: str
    worker_type: WorkerType
    status: TaskRunStatus
    session_id: str | None = None
    prompt_group: str | None = None
    prompt_version: str | None = None
    execution_profile: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    exit_code: int | None = None
    error_type: TaskRunErrorType | None = None
    error_message: str | None = None
    assessment_attempt_id: str | None = None
    execution_generation: int = 0
    pause_requested: bool = False
    # 只读派生提示，不进入持久 runtime_context，实际资格仍由 admission 原子核验。
    delivery_retry_route: dict[str, Any] | None = None
    runtime_context: dict[str, Any] = Field(default_factory=dict)


class CreateTaskRunRequest(BaseModel):
    # 仅作认领时的代次前置条件；实际 execution_generation 始终由服务端写入。
    expected_execution_generation: int | None = Field(default=None, ge=0, strict=True)
    # dispatcher 创建记录时可先写计划态 runtime_context，结束时再补真实运行探测。
    intent_id: str | None = None
    task_type: TaskType
    worker_name: str
    worker_type: WorkerType
    status: TaskRunStatus = "running"
    session_id: str | None = None
    prompt_group: str | None = None
    prompt_version: str | None = None
    execution_profile: str | None = None
    runtime_context: dict[str, Any] = Field(default_factory=dict)

    @field_validator("worker_name")
    @classmethod
    def validate_worker_name(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("intent_id", "session_id", "prompt_group", "prompt_version", "execution_profile")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ExecutionLifecycle(BaseModel):
    generation: int = Field(ge=0, strict=True)
    process_started: bool = Field(strict=True)
    termination_confirmed: bool = Field(strict=True)
    cleanup_confirmed: bool = Field(strict=True)

    model_config = {"extra": "forbid"}


class UpdateTaskRunRequest(BaseModel):
    # 更新请求用于 running -> terminal，也允许只补 session/error/runtime_context 的增量信息。
    status: TaskRunStatus | None = None
    session_id: str | None = None
    exit_code: int | None = None
    error_type: TaskRunErrorType | None = None
    error_message: str | None = None
    runtime_context: dict[str, Any] | None = None

    @field_validator("runtime_context")
    @classmethod
    def validate_execution_lifecycle(cls, value):
        if value is not None and "execution_lifecycle" in value:
            value = dict(value)
            value["execution_lifecycle"] = ExecutionLifecycle.model_validate(value["execution_lifecycle"]).model_dump()
        return value

    @field_validator("session_id", "error_message")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip()

    @field_validator("error_type", mode="before")
    @classmethod
    def normalize_error_type(cls, value: str | None) -> str | None:
        # 空错误类型视作未上报，由服务层按状态/exit_code 再做兜底分类。
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return None
        return text


__all__ = [name for name in globals() if not name.startswith("__")]
