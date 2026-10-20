"""面向项目用户的运行事实白名单，不复用管理员完整诊断。"""
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from .common import ProjectStatus


class RuntimeAction(BaseModel):
    allowed: bool
    reason: str | None = None


class RuntimeActions(BaseModel):
    pause: RuntimeAction
    resume: RuntimeAction
    reauthorize: RuntimeAction
    reopen: RuntimeAction
    finish: RuntimeAction


class RuntimeDispatcher(BaseModel):
    status: str
    freshness: Literal["fresh", "stale", "unknown"]
    updated_at: str | None = None
    last_loop_finished_at: str | None = None


class RuntimeScheduling(BaseModel):
    code: str
    observed_at: str
    retry_at: str | None = None


class RuntimeAttempt(BaseModel):
    id: str
    task_type: str
    status: str
    worker_name: str
    execution_stage: str
    started_at: str | None = None


class RuntimeSubmissionIssue(BaseModel):
    keyword: str
    path: str


class RuntimeSubmission(BaseModel):
    code: str
    issue: RuntimeSubmissionIssue | None = None
    submission_attempts: int | None = Field(default=None, ge=0, le=1_000_000)
    repair_rounds: int | None = Field(default=None, ge=0, le=2)
    max_repair_rounds: int | None = Field(default=None, ge=0, le=2)


class RuntimeError(BaseModel):
    code: str
    message: str
    task_run_id: str | None = None
    submission: RuntimeSubmission | None = None


class RuntimeActivity(BaseModel):
    kind: str
    recorded_at: str
    task_run_id: str


class _ProjectRuntimeBase(BaseModel):
    project_id: str
    observed_at: str
    execution_generation: int
    project_status: ProjectStatus
    state: str
    code: str
    message: str
    dispatcher: RuntimeDispatcher
    scheduling: RuntimeScheduling | None = None
    active_runs: list[RuntimeAttempt] = Field(default_factory=list)
    last_error: RuntimeError | None = None
    last_activity: RuntimeActivity | None = None
    actions: RuntimeActions


class ProjectRuntime(_ProjectRuntimeBase):
    schema_version: Literal[1] = 1


class RuntimeAxis(BaseModel):
    state: str
    code: str | None = None


class RuntimeAcceptanceAxis(RuntimeAxis):
    state: Literal[
        "accepted",
        "partially_accepted",
        "not_evaluated",
        "rejected",
    ]
    evidence: Literal[
        "conclusion_receipt",
        "completion_receipt",
        "intent_submission_prefix",
        "depth_review_receipt",
        "report_finish_receipt",
    ] | None = None


class RuntimeProcessAxis(RuntimeAxis):
    termination_confirmed: bool = False


class RuntimeResourceAxis(RuntimeAxis):
    cleanup_confirmed: bool = False


class RuntimeAxes(BaseModel):
    task_run_id: str | None = None
    execution: RuntimeAxis
    delivery: RuntimeAxis
    acceptance: RuntimeAcceptanceAxis
    process: RuntimeProcessAxis
    resources: RuntimeResourceAxis


class RuntimeCheckpointStaging(BaseModel):
    state: Literal["none", "staged"]
    received_count: int = Field(ge=0)
    current_generation_count: int = Field(ge=0)
    historical_generation_count: int = Field(ge=0)
    last_received_at: str | None = None
    last_task_run_id: str | None = None
    business_acceptance: Literal["not_evaluated"] = "not_evaluated"


class RuntimeRecovery(BaseModel):
    route: Literal[
        "none", "wait", "automatic_retry", "resume", "reauthorize", "manual"
    ]
    code: str | None = None


class ProjectRuntimeV2(_ProjectRuntimeBase):
    schema_version: Literal[2] = 2
    axes: RuntimeAxes
    checkpoint_staging: RuntimeCheckpointStaging
    recovery: RuntimeRecovery


ProjectRuntimeResponse = Annotated[
    ProjectRuntime | ProjectRuntimeV2,
    Field(discriminator="schema_version"),
]


class ProjectRuntimeBatch(BaseModel):
    items: list[ProjectRuntimeResponse] = Field(default_factory=list)
