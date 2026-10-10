"""项目、目标和 intent schema。"""

from __future__ import annotations

from datetime import datetime

from cairn.assessment_contracts import DepthReviewDecision, ExperimentRecord
from pydantic import ConfigDict

from .artifacts import Artifact, FactEvidence
from .common import *
from .groups import ProjectGroup
from .resources import CreateProjectResourceRequest, ProjectResource
from .tasks import TaskRun
from .tenant_workers import TenantWorker


class Fact(BaseModel):
    assessment_record: dict[str, Any] | None = None
    id: str
    description: str
    task_run_id: str | None = None
    creator: str | None = None
    created_at: str | None = None


class EvidenceRef(BaseModel):
    artifact_id: str
    pointer: str = ""
    verifier_run_id: str | None = None

    @field_validator("artifact_id")
    @classmethod
    def validate_artifact_id(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("pointer", "verifier_run_id")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text and value != "":
            raise ValueError("must not be blank")
        return text


class VerificationResultInput(BaseModel):
    observation_id: str
    outcome: Literal[
        "confirmed",
        "not_reproduced",
        "environment_changed",
        "insufficient_evidence",
    ]
    evidence_refs: list[EvidenceRef] = Field(min_length=1)
    notes: str = ""

    @field_validator("observation_id")
    @classmethod
    def validate_observation_id(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("notes")
    @classmethod
    def normalize_notes(cls, value: str) -> str:
        return value.strip()


class VulnerabilityVerification(BaseModel):
    id: str
    vulnerability_id: str
    observation_id: str
    intent_id: str
    fact_id: str
    verifier_task_run_id: str
    verifier_worker: str
    discovery_task_run_id: str
    discovery_worker: str | None = None
    independence_level: Literal["different_worker", "same_worker_new_session"]
    outcome: Literal[
        "confirmed",
        "not_reproduced",
        "environment_changed",
        "insufficient_evidence",
    ]
    evidence: list[EvidenceRef] = Field(default_factory=list)
    notes: str = ""
    created_at: str


class IntentWorkerHistoryEntry(BaseModel):
    worker: str | None = None
    reason: IntentWorkerHistoryReason
    cleared_at: str
    last_heartbeat_at: str | None = None


# Fact graph 的边：从若干 source fact 指向一个可为空的目标 fact。
class Intent(BaseModel):
    id: str
    from_: list[str] = Field(alias="from")
    to: str | None = None
    description: str
    creator: str
    task_run_id: str | None = None
    worker: str | None = None
    worker_history: list[IntentWorkerHistoryEntry] = Field(default_factory=list)
    last_heartbeat_at: str | None = None
    created_at: str
    concluded_at: str | None = None
    # conclusion 收敛方式：None=未收敛(open)；completed/abandoned/failed。
    conclusion: str | None = None
    # depends_on 执行顺序约束：引用同项目另一 intent id；None=无依赖，立即可派发。
    depends_on: str | None = None
    # slot_key: pentest 场景把 intent 绑定到 coverage_slots 的待覆盖槽位；其他场景始终 None。
    slot_key: str | None = None
    # strategy_key: 结构化 Cell 本轮测试策略身份，用于 blocker cooldown 与 attempt 审计。
    strategy_key: str = ""
    # verifier Intent 精确绑定一条 discovery observation；普通探索保持 discovery/None。
    assessment_role: Literal["discovery", "verifier"] = "discovery"
    priority: int = Field(default=1, ge=0, le=2, strict=True)
    target_observation_id: str | None = None
    depth_review_fact_id: str | None = None
    conclusion_reason: str | None = None

    model_config = {"populate_by_name": True}


class Hint(BaseModel):
    id: str
    content: str
    creator: str
    # 绑定到具体 fact 节点（触发式前置提示：从该 fact 出发的探索适用）。
    fact_id: str | None = None
    # 绑定到具体 intent 线索（直接引导该探索方向）；优先于 fact_id。
    intent_id: str | None = None
    created_at: str


# 运行目标的审阅状态机，seed/approved 可进入执行上下文。
class Target(BaseModel):
    id: str
    value: str
    type: TargetType
    status: TargetStatus
    source_fact_id: str | None = None
    discovered_by_run_id: str | None = None
    created_at: str

    @field_validator("value")
    @classmethod
    def validate_target_value(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("source_fact_id", "discovered_by_run_id")
    @classmethod
    def validate_optional_target_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ProjectReason(BaseModel):
    worker: str
    trigger: str
    started_at: str
    last_heartbeat_at: str


class ProjectWorkerBinding(BaseModel):
    id: str
    engine: str | None = None
    unavailable_reason: str | None = None
    display_name: str
    revision_at_bind: int
    task_types: list[str] = Field(default_factory=list)
    max_running: int = 1
    priority: int = 100
    enabled: bool = True
    currently_available: bool = False


class CoverageQualitySnapshot(BaseModel):
    total_count: int = 0
    qualified_count: int = 0
    unqualified_count: int = 0
    unresolved_count: int = 0
    finding_count: int = 0
    tested_no_finding_count: int = 0
    permanent_limitation_count: int = 0
    administrative_skip_count: int = 0


class AssessmentQualitySnapshotView(BaseModel):
    version: int
    status: Literal["observing", "ready"]
    required: CoverageQualitySnapshot
    optional: CoverageQualitySnapshot
    manifest_surface_count: int = 0
    missing_required_surface_count: int = 0
    charged_attempt_count: int = 0
    runtime_execution_count: int = 0
    runtime_retry_count: int = 0
    failed_attempt_count: int = 0
    active_blocker_count: int = 0
    active_cooldown_count: int = 0
    finding_count: int = 0
    observation_count: int = 0
    confirmed_finding_count: int = 0
    independently_verified_finding_count: int = 0
    pending_verifier_count: int = 0
    chain_candidate_count: int = 0
    chain_verification_ready_count: int = 0
    confirmed_chain_count: int = 0
    stop_reason: str | None = None
    residual_risks: list[str] = Field(default_factory=list)


class AssessmentReportFinalizationView(BaseModel):
    project_id: str
    version: int = 1
    status: Literal["pending", "running", "ready", "failed"]
    attempt_count: int = 0
    claimed_by: str | None = None
    lease_expires_at: str | None = None
    next_attempt_at: str | None = None
    delivery_retry_route: dict[str, Any] | None = None
    artifact_id: str | None = None
    last_error: str = ""
    generation_started_at: str
    generation_token: str
    claim_token: str | None = None
    requested_at: str | None = None
    requested_by: str | None = None
    template_key: str | None = None
    template_version: int | None = None
    template_digest_sha256: str | None = None
    input_snapshot_artifact_id: str | None = None
    input_snapshot_sha256: str | None = None
    report_policy_version: int | None = None
    report_execution_plan_sha256: str | None = None
    report_prompt_digest_sha256: str | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None


class AssessmentReportFinishReceiptView(AssessmentReportFinalizationView):
    protocol: Literal["cairn-assessment-report-finish-v1"]
    task_run_id: str
    request_sha256: str
    finish_disposition: Literal["accepted", "recovered"]
    accepted_at: str


class AssessmentReportClaimView(AssessmentReportFinalizationView):
    task_run_id: str
    task_run_context: dict[str, Any] = Field(default_factory=dict)
    template_markdown: str | None = None
    report_execution_plan_json: str | None = None
    report_prompt_markdown: str | None = None


class AssessmentReportGenerationRequest(BaseModel):
    template_key: str = Field(min_length=1, max_length=128)
    template_version: int = Field(ge=1)

    @field_validator("template_key")
    @classmethod
    def validate_template_key(cls, value: str) -> str:
        return value.strip()


class AssessmentReportClaimRequest(BaseModel):
    worker: str
    lease_seconds: int = Field(default=300, ge=60, le=1800)

    @field_validator("worker")
    @classmethod
    def validate_worker(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class AssessmentReportSnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    worker: str = Field(min_length=1, max_length=128)
    claim_token: str = Field(min_length=1, max_length=512)
    artifact_id: str = Field(min_length=1, max_length=128)
    digest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("worker", "claim_token", "artifact_id", "digest_sha256")
    @classmethod
    def validate_snapshot_text(cls, value: str) -> str:
        if value != value.strip() or "\0" in value:
            raise ValueError("identifier must be canonical text")
        return value


class AssessmentReportFinishIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    keyword: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1, max_length=512)

    @field_validator("keyword", "path")
    @classmethod
    def validate_issue_text(cls, value: str) -> str:
        if value != value.strip() or "\0" in value:
            raise ValueError("issue text must be canonical")
        return value


class AssessmentReportWorkerFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    origin: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=128)
    phase: str = Field(min_length=1, max_length=64)
    error_type: TaskRunErrorType
    message: str = Field(min_length=1, max_length=1000)
    path: str | None = Field(default=None, min_length=1, max_length=512)
    exit_code: int | None = Field(default=None, ge=-65535, le=65535)
    issue: AssessmentReportFinishIssue | None = None
    submission_attempts: int | None = Field(default=None, ge=0, le=1_000_000)
    repair_rounds: int | None = Field(default=None, ge=0, le=2)
    max_repair_rounds: int | None = Field(default=None, ge=0, le=2)

    @field_validator("origin", "code", "phase", "path")
    @classmethod
    def validate_failure_identifier(cls, value: str | None) -> str | None:
        if value is not None and (value != value.strip() or "\0" in value):
            raise ValueError("worker failure identifier must be canonical")
        return value

    @field_validator("message")
    @classmethod
    def validate_failure_message(cls, value: str) -> str:
        if "\0" in value:
            raise ValueError("worker failure message must not contain NUL")
        return value

    @model_validator(mode="after")
    def validate_repair_counts(self):
        counts = (
            self.submission_attempts,
            self.repair_rounds,
            self.max_repair_rounds,
        )
        if any(value is not None for value in counts):
            if any(value is None for value in counts):
                raise ValueError("submission repair counts must be complete")
            assert self.repair_rounds is not None
            assert self.max_repair_rounds is not None
            if self.repair_rounds > self.max_repair_rounds:
                raise ValueError("repair_rounds must not exceed max_repair_rounds")
        return self


class AssessmentReportTaskRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["succeeded", "failed", "cancelled", "timed_out"]
    exit_code: int | None = Field(default=None, ge=-65535, le=65535)
    error_type: TaskRunErrorType | None = None
    error_message: str = Field(default="", max_length=2000)
    worker_failure: AssessmentReportWorkerFailure | None = None

    @model_validator(mode="after")
    def validate_terminal_facts(self):
        if "\0" in self.error_message:
            raise ValueError("error_message must not contain NUL")
        if self.status == "succeeded":
            if (
                self.exit_code != 0
                or self.error_type is not None
                or self.error_message
                or self.worker_failure is not None
            ):
                raise ValueError("succeeded report task must contain only successful facts")
        elif self.error_type is None:
            raise ValueError("failed report task requires error_type")
        if self.status == "timed_out" and self.error_type != "worker_timeout":
            raise ValueError("timed_out report task requires worker_timeout")
        if self.status == "cancelled" and self.error_type != "cancelled":
            raise ValueError("cancelled report task requires cancelled error type")
        if self.worker_failure is not None:
            if self.worker_failure.error_type != self.error_type:
                raise ValueError("worker failure error_type must match task run result")
            if self.worker_failure.exit_code != self.exit_code:
                raise ValueError("worker failure exit_code must match task run result")
        return self


class AssessmentReportFinishPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    protocol: Literal["cairn-assessment-report-finish-v1"]
    worker: str = Field(min_length=1, max_length=128)
    generation_token: str = Field(min_length=1, max_length=256)
    task_run_id: str = Field(min_length=1, max_length=128)
    status: Literal["ready", "failed"]
    artifact_id: str | None = Field(default=None, min_length=1, max_length=128)
    artifact_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    input_snapshot_artifact_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    input_snapshot_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    task_run_result: AssessmentReportTaskRunResult

    @field_validator(
        "worker",
        "generation_token",
        "task_run_id",
        "artifact_id",
        "input_snapshot_artifact_id",
    )
    @classmethod
    def validate_exact_text(cls, value: str | None) -> str | None:
        if value is not None and (value != value.strip() or "\0" in value):
            raise ValueError("identifier must be canonical text")
        return value

    @model_validator(mode="after")
    def validate_finish_facts(self):
        artifact_pair = self.artifact_id is not None and self.artifact_sha256 is not None
        snapshot_pair = (
            self.input_snapshot_artifact_id is not None
            and self.input_snapshot_sha256 is not None
        )
        if (self.artifact_id is None) != (self.artifact_sha256 is None):
            raise ValueError("artifact identity must be complete")
        if (self.input_snapshot_artifact_id is None) != (
            self.input_snapshot_sha256 is None
        ):
            raise ValueError("input snapshot identity must be complete")
        if self.status == "ready":
            if not artifact_pair or not snapshot_pair:
                raise ValueError("ready report requires exact artifact identities")
            if self.task_run_result.status != "succeeded":
                raise ValueError("ready report requires a succeeded task run")
        else:
            if artifact_pair:
                raise ValueError("failed report must not declare a report artifact")
            if self.task_run_result.status == "succeeded":
                raise ValueError("failed report cannot declare a succeeded task run")
        return self


class AssessmentReportFinishRequest(AssessmentReportFinishPayload):
    claim_token: str = Field(min_length=1, max_length=512)

    @field_validator("claim_token")
    @classmethod
    def validate_claim_token(cls, value: str) -> str:
        if value != value.strip() or "\0" in value:
            raise ValueError("identifier must be canonical text")
        return value


class AssessmentReportFinishReplayRequest(AssessmentReportFinishPayload):
    """不落盘 claim token 的 durable finish replay 请求。"""

    claim_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class AssessmentReportPacket(BaseModel):
    version: int = 2
    assessment_summary: dict[str, Any] = Field(default_factory=dict)
    observation_statuses: list[dict[str, Any]] = Field(default_factory=list)
    project: dict[str, Any]
    policy: dict[str, Any] = Field(default_factory=dict)
    report_policy: dict[str, Any] = Field(default_factory=dict)
    targets: list[dict[str, Any]] = Field(default_factory=list)
    facts: list[dict[str, Any]] = Field(default_factory=list)
    intents: list[dict[str, Any]] = Field(default_factory=list)
    intent_sources: list[dict[str, Any]] = Field(default_factory=list)
    surface_manifest: list[dict[str, Any]] = Field(default_factory=list)
    coverage_scope_dispositions: list[dict[str, Any]] = Field(default_factory=list)
    coverage_cells: list[dict[str, Any]] = Field(default_factory=list)
    quality_snapshot: dict[str, Any] | None = None
    vulnerabilities: list[dict[str, Any]] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    verifications: list[dict[str, Any]] = Field(default_factory=list)
    evidence_links: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    chain_candidates: list[dict[str, Any]] = Field(default_factory=list)
    assessment_attempts: list[dict[str, Any]] = Field(default_factory=list)
    task_runs: list[dict[str, Any]] = Field(default_factory=list)


class ReportCapabilityView(BaseModel):
    supported: bool = False
    policy_version: int = 0
    template_profile: str | None = None
    reason: str | None = None


class ProjectMeta(BaseModel):
    id: str
    title: str
    status: Literal["active", "paused", "stopped", "completed"]
    execution_generation: int = 0
    authorization_epoch: int = 0
    authorization_scope_digest: str | None = None
    authorization_window_seconds: int | None = None
    authorization_block_reason: str | None = None
    created_at: str
    scenario_key: str = "default"
    # 新协议项目冻结完整场景；历史项目为空对象并继续按 scenario_key 兼容解析。
    scenario_snapshot: dict[str, Any] = Field(default_factory=dict)
    assessment_protocol: str = ""
    vulnerability_fingerprint_version: int | None = None
    report_capability: ReportCapabilityView = Field(default_factory=ReportCapabilityView)
    explore_budget: int | None = None
    explore_deadline: str | None = None
    authorization_expires_at: str | None = None
    explore_attempts_started: int = 0
    verification_attempts_started: int = 0
    assessment_progress: dict[str, Any] | None = None
    assessment_execution: dict[str, Any] = Field(default_factory=dict)
    coverage_checkpoint_cell_count: int = 0
    coverage_checkpoint_required_count: int = 0
    coverage_checkpoint_optional_count: int = 0
    coverage_checkpoint_required_terminal_count: int = 0
    coverage_stop_reason: str | None = None
    # v1 仅作为 detail read model shadow 暴露，不参与当前 completion gate。
    assessment_quality: AssessmentQualitySnapshotView | None = None
    # 新项目只使用同 Tenant 的 durable Worker binding；旧字段仅保留历史读取兼容。
    tenant_workers: list[ProjectWorkerBinding] = Field(default_factory=list)
    preferred_workers: list[str] = Field(default_factory=list)
    tenant_id: str = ""
    tenant_name: str = ""
    tenant_owner_login: str | None = None
    # 所属项目组 id，null 表示未分组；group_name 是稳定的只读展示值。
    group_id: str | None = None
    group_name: str | None = None
    reason: ProjectReason | None = None


class ProjectSummary(ProjectMeta):
    fact_count: int
    intent_count: int
    working_intent_count: int
    unclaimed_intent_count: int
    hint_count: int


class ProjectManagementOptions(BaseModel):
    groups: list[ProjectGroup] = Field(default_factory=list)
    tenant_workers: list[TenantWorker] = Field(default_factory=list)


class ProjectLibraryBinding(BaseModel):
    library_key: str
    library_name: str
    version: int
    digest_sha256: str
    file_count: int


class ProjectDetail(BaseModel):
    project: ProjectMeta
    facts: list[Fact]
    intents: list[Intent]
    hints: list[Hint]
    targets: list[Target]
    resources: list[ProjectResource] = Field(default_factory=list)
    library_bindings: list[ProjectLibraryBinding] = Field(default_factory=list)


class CreateTargetInline(BaseModel):
    value: str
    type: TargetType

    @field_validator("value")
    @classmethod
    def validate_target_value(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateHintInline(BaseModel):
    content: str

    @field_validator("content")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateProjectRequest(BaseModel):
    title: str
    origin: str
    goal: str
    scenario: str | None = None
    hints: list[CreateHintInline] | None = None
    targets: list[CreateTargetInline] | None = None
    # resources 保留旧版 JSON 创建兼容；大文件和 ZIP 使用临时批次。
    resources: list[CreateProjectResourceRequest] | None = None
    resource_batch_id: str | None = None
    library_keys: list[str] = Field(default_factory=list)
    # 新项目必须显式绑定至少一个同 Tenant 的可用 Worker ID。
    tenant_worker_ids: list[str] = Field(min_length=1)
    # 新建项目必须显式绑定同 Tenant 的项目组；存量项目生命周期仍允许为空。
    group_id: str
    explore_budget: int = Field(default=40, ge=1, le=10_000, strict=True)
    explore_deadline: str | None = None
    # 创建方必须显式选择未来期限或以 null 表示无限期，不能靠字段缺失推导授权。
    authorization_expires_at: str | None

    @field_validator("title", "origin", "goal", "scenario")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("resource_batch_id")
    @classmethod
    def validate_resource_batch_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("resource_batch_id must not be empty")
        return text

    @classmethod
    def _validate_absolute_timestamp(cls, value: str) -> str:
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("must be an absolute ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("must include a timezone")
        return text

    @field_validator("explore_deadline")
    @classmethod
    def validate_explore_deadline(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return cls._validate_absolute_timestamp(value)

    @field_validator("authorization_expires_at")
    @classmethod
    def validate_authorization_expires_at(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("must be an absolute ISO-8601 timestamp or null")
        return cls._validate_absolute_timestamp(value)

    @model_validator(mode="after")
    def validate_resource_sources(self):
        if self.resource_batch_id is not None and self.resources:
            raise ValueError("resources and resource_batch_id cannot be used together")
        return self

    @field_validator("library_keys")
    @classmethod
    def validate_library_keys(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = str(item).strip()
            if not text:
                raise ValueError("library keys must not be empty")
            if text in seen:
                raise ValueError(f"duplicate library key: {text}")
            seen.add(text)
            cleaned.append(text)
        return cleaned

    @field_validator("tenant_worker_ids")
    @classmethod
    def validate_tenant_worker_ids(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = str(item).strip()
            if not text:
                raise ValueError("worker ids must not be empty")
            if text in seen:
                continue
            seen.add(text)
            cleaned.append(text)
        if not cleaned:
            raise ValueError("at least one worker is required")
        return cleaned

    @field_validator("group_id")
    @classmethod
    def validate_group_id(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("group id must not be empty")
        return text


class CreateHintRequest(BaseModel):
    content: str
    creator: str | None = None
    # 可选：绑定到具体 fact 节点（触发式前置提示）。goal 不可选——无探索从 goal 出发。
    fact_id: str | None = None
    # 可选：绑定到具体 intent 线索，直接引导该探索方向。
    intent_id: str | None = None

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("fact_id", "intent_id")
    @classmethod
    def validate_optional_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @field_validator("fact_id")
    @classmethod
    def validate_fact_id_not_goal(cls, value: str | None) -> str | None:
        # goal 是终点，协议规定 intent 的 from 不能包含 goal，挂在 goal 上的提示无作用。
        if value is not None and value.strip() == "goal":
            raise ValueError("hints cannot target the goal fact")
        return value


class CreateTargetRequest(BaseModel):
    value: str
    type: TargetType
    status: TargetStatus = "discovered"
    source_fact_id: str | None = None
    discovered_by_run_id: str | None = None

    @field_validator("value")
    @classmethod
    def validate_target_value(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("source_fact_id", "discovered_by_run_id")
    @classmethod
    def validate_optional_target_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class UpdateTargetRequest(BaseModel):
    status: TargetStatus


# 外部或 dispatcher 创建待探索意图；from 使用别名以贴近协议语义。
class CreateIntentRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    creator: str | None = None
    task_run_id: str | None = None
    # Reason 单次结果内稳定的零基条目索引；与 TaskRun 共同定位 Server 幂等收据。
    submission_index: int | None = Field(default=None, ge=0, strict=True)
    worker: str | None = None
    # depends_on: 可选执行顺序约束，引用同项目另一 intent id。None=无依赖。
    depends_on: str | None = None
    priority: int = Field(default=1, ge=0, le=2, strict=True)
    # slot_key: pentest 场景把 intent 绑定到 coverage_slots 槽位；其他场景不传。
    slot_key: str | None = None
    strategy_key: str = ""

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @field_validator("description", "creator", "task_run_id", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("slot_key")
    @classmethod
    def validate_slot_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        return text

    @field_validator("strategy_key")
    @classmethod
    def validate_strategy_key(cls, value: str) -> str:
        text = value.strip().lower()
        if text and fullmatch(r"[a-z0-9][a-z0-9._:-]{0,127}", text) is None:
            raise ValueError("strategy_key has invalid format")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class HeartbeatRequest(BaseModel):
    worker: str
    task_run_id: str | None = None

    @field_validator("worker", "task_run_id")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class AbandonRequest(BaseModel):
    # 主动放弃一个 open intent：仅对未认领（worker=null）的 open intent 生效，
    # 让 reason 在 complete 前剪掉不再追的分支；运行中的 explore 不可放弃，须等其跑完。
    worker: str
    task_run_id: str | None = None

    @field_validator("worker", "task_run_id")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReasonClaimRequest(BaseModel):
    worker: str
    trigger: str
    task_run_id: str | None = Field(default=None, min_length=1)

    @field_validator("worker", "trigger")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class VulnerabilityAssetIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    protocol: str = ""
    host: str = Field(min_length=1, pattern=r"\S")
    port: int | None = Field(default=None, ge=1, le=65535)

    @field_validator("protocol")
    @classmethod
    def normalize_protocol(cls, value: str) -> str:
        return value.strip()

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class VulnerabilityEntrypointIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, json_schema_extra={
        # route 与 locus 可单独为空，但至少一处必须给出真实定位。
        "anyOf": [
            {"required": ["route"], "properties": {"route": {"pattern": r"\S"}}},
            {"required": ["locus"], "properties": {"locus": {"pattern": r"\S"}}},
        ],
    })

    method: str = ""
    route: str = ""
    locus: str = ""

    @model_validator(mode="after")
    def validate_location(self):
        if not self.route.strip() and not self.locus.strip():
            raise ValueError("route or locus is required")
        return self


class VulnerabilityVectorIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    parameter: str = ""
    object_type: str = ""
    source_role: str = ""
    target_role: str = ""
    state: str = ""
    variant: str = ""


class VulnerabilityIdentityInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    vulnerability_class: str = Field(min_length=1, pattern=r"\S")
    asset: VulnerabilityAssetIdentity
    entrypoint: VulnerabilityEntrypointIdentity
    vector: VulnerabilityVectorIdentity = Field(
        default_factory=VulnerabilityVectorIdentity
    )

    @field_validator("vulnerability_class")
    @classmethod
    def validate_class(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ConcludeVulnerabilityInput(BaseModel):
    """conclude 时自动确认漏洞（dispatcher 已 resolve packet_path→packet_artifact_id）。"""

    title: str
    severity: str
    vuln_type: str = ""
    target: str = ""
    remediation: str = ""
    confidence: float = 0.5
    packet_artifact_id: str | None = None
    slot_key: str | None = None
    # repro_request/repro_response: worker 内联返回的原始 HTTP 请求/响应包（固定格式文本），
    # 不依赖文件 artifact。空字符串=未提供。
    repro_request: str = ""
    repro_response: str = ""
    identity: VulnerabilityIdentityInput | None = None

    @field_validator("title", "severity")
    @classmethod
    def validate_non_empty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, value: str) -> str:
        text = value.strip().lower()
        if text not in VULNERABILITY_SEVERITIES:
            raise ValueError(f"severity must be one of {VULNERABILITY_SEVERITIES}")
        return text

    @field_validator("repro_request", "repro_response")
    @classmethod
    def validate_repro(cls, value: str) -> str:
        # 允许空（未提供）；非空则 trim 尾部空白，但保留内部格式（HTTP 包按行结构）
        if value is None:
            return ""
        return value.rstrip()

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, value: float) -> float:
        if not isinstance(value, (int, float)):
            raise ValueError("confidence must be a number 0-1")
        return max(0.0, min(1.0, float(value)))


class CoverageResultInput(BaseModel):
    slot_key: str
    surface_key: str
    test_family: str
    outcome: str
    baseline: str = ""
    variants: list[str] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    blocker_code: str = ""
    strategy_key: str = ""
    attempted_strategies: list[str] = Field(default_factory=list)
    limitation_reason: str = ""
    limitation_details: str = ""

    @field_validator("slot_key", "surface_key", "test_family", "outcome")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("test_family")
    @classmethod
    def validate_test_family(cls, value: str) -> str:
        from cairn.server.services.common import DEFAULT_COPY_TEST_FAMILIES

        text = value.strip().lower().replace("-", "_").replace(" ", "_")
        if text not in DEFAULT_COPY_TEST_FAMILIES:
            raise ValueError(f"test_family must be one of {sorted(DEFAULT_COPY_TEST_FAMILIES)}")
        return text

    @field_validator("outcome")
    @classmethod
    def validate_outcome(cls, value: str) -> str:
        from cairn.server.services.common import DEFAULT_COPY_COVERAGE_OUTCOMES

        text = value.strip().lower().replace("-", "_").replace(" ", "_")
        if text not in DEFAULT_COPY_COVERAGE_OUTCOMES:
            raise ValueError(f"outcome must be one of {sorted(DEFAULT_COPY_COVERAGE_OUTCOMES)}")
        return text

    @field_validator(
        "baseline", "blocker_code", "limitation_reason", "limitation_details"
    )
    @classmethod
    def validate_optional_text(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else ""

    @field_validator("strategy_key")
    @classmethod
    def validate_coverage_strategy_key(cls, value: str) -> str:
        text = value.strip().lower()
        if text and fullmatch(r"[a-z0-9][a-z0-9._:-]{0,127}", text) is None:
            raise ValueError("strategy_key has invalid format")
        return text

    @field_validator("variants", "attempted_strategies")
    @classmethod
    def validate_text_list(cls, value: list[str]) -> list[str]:
        return [text for item in value if (text := str(item).strip())]


class CoverageScopeDispositionInput(BaseModel):
    disposition_kind: Literal["no_in_scope_targets", "no_reachable_surfaces"]
    target_ids: list[str] = Field(default_factory=list)
    reason: str
    evidence_refs: list[EvidenceRef]

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("target_ids")
    @classmethod
    def validate_target_ids(cls, value: list[str]) -> list[str]:
        cleaned = sorted({str(item).strip() for item in value if str(item).strip()})
        return cleaned


class DepthReviewRequest(DepthReviewDecision):
    worker: str = Field(min_length=1)
    task_run_id: str = Field(min_length=1)


class DepthReviewResponse(BaseModel):
    fact: Fact
    intents: list[Intent]
    project: ProjectMeta


class ConcludeRequest(BaseModel):
    assessment_record: ExperimentRecord | None = None
    worker: str
    description: str
    evidence: list[EvidenceRef] = Field(default_factory=list)
    discovered_surfaces: list[dict[str, Any]] = Field(default_factory=list)
    coverage_scope_disposition: CoverageScopeDispositionInput | None = None
    task_run_id: str | None = None
    # conclusion: completed=正常结出 fact（默认）；failed=探索失败收尾（P1.5：消除死节点）。
    conclusion: str = "completed"
    # default-copy-v1 必须携带结构化测试结果，服务端在写 Fact 前校验并推进 Coverage Cell。
    coverage_result: CoverageResultInput | None = None
    # 漏洞自动确认：fact 带 vulnerability 子字段时 dispatcher resolve 后透传，事务内写漏洞库。
    vulnerability: ConcludeVulnerabilityInput | None = None
    # verifier Intent 只能提交针对绑定 observation 的独立复现结果。
    verification_result: VerificationResultInput | None = None

    @field_validator("worker", "description", "task_run_id")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("conclusion")
    @classmethod
    def validate_conclusion(cls, value: str) -> str:
        if value not in ("completed", "failed"):
            raise ValueError("conclusion must be 'completed' or 'failed'")
        return value


class CompleteRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    worker: str
    evidence: list[EvidenceRef] = Field(default_factory=list)
    task_run_id: str | None = None

    model_config = {"populate_by_name": True}

    @field_validator("description", "worker", "task_run_id")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class ConcludeResponse(BaseModel):
    fact: Fact
    intent: Intent


class UpdateProjectStatusRequest(BaseModel):
    status: Literal["active", "stopped"]


class UpdateProjectWorkersRequest(BaseModel):
    tenant_worker_ids: list[str] = Field(min_length=1)

    @field_validator("tenant_worker_ids")
    @classmethod
    def validate_tenant_worker_ids(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = str(item).strip()
            if not text:
                raise ValueError("worker ids must not be empty")
            if text in seen:
                continue
            seen.add(text)
            cleaned.append(text)
        if not cleaned:
            raise ValueError("at least one worker is required")
        return cleaned


class UpdateProjectTitleRequest(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class MoveProjectToGroupRequest(BaseModel):
    # 移动项目到指定分组；None/空表示移出分组（变为未分组）。
    # 注意：本类名与 schemas/groups.py 的 UpdateProjectGroupRequest（重命名分组，字段 name）
    # 含义不同，刻意改名避免在 server.models facade 里被后者覆盖导致端点拿错 schema。
    group_id: str | None = None

    @field_validator("group_id")
    @classmethod
    def validate_group_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None


class ReauthorizeProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    confirmed: Literal[True]
    expected_authorization_epoch: int = Field(ge=0)
    expected_scope_digest: str = Field(
        pattern=r"^authorization-scope:[a-f0-9]{64}$"
    )
    authorization_expires_at: str | None = None

    @field_validator("authorization_expires_at")
    @classmethod
    def validate_authorization_expiry(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("must be an absolute ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("must include a timezone")
        return text


class ReopenRequest(BaseModel):
    description: str

    @field_validator("description")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenResponse(BaseModel):
    project: ProjectMeta
    fact: Fact
    intent: Intent


# pentest 场景的待覆盖槽位：bootstrap 登记攻击面入口点，explore 跑过一轮置 explored。
# 完成门 coverage_gate 查 pending 槽位是否清零，决定能否 complete。
class CoverageSlotInput(BaseModel):
    kind: str
    key: str = ""
    description: str = ""
    # default-copy-v1 使用以下字段定义“攻击面 × 适用测试族”Cell；legacy pentest 可省略。
    surface_key: str | None = None
    surface_kind: str = ""
    test_family: str = ""
    applicability_basis: str = ""
    completion_criteria: str = ""

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, value: str) -> str:
        from cairn.server.services.common import COVERAGE_SLOT_KINDS
        text = value.strip()
        if text not in COVERAGE_SLOT_KINDS:
            raise ValueError(f"kind must be one of {sorted(COVERAGE_SLOT_KINDS)}")
        return text

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        # structured Cell 的权威 key 由服务端生成；legacy 是否必填在项目协议分支校验。
        return value.strip()

    @field_validator("surface_key")
    @classmethod
    def validate_surface_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @field_validator("surface_kind")
    @classmethod
    def validate_surface_kind(cls, value: str) -> str:
        from cairn.server.services.common import COVERAGE_SLOT_KINDS

        text = value.strip()
        if text and text not in COVERAGE_SLOT_KINDS:
            raise ValueError(f"surface_kind must be one of {sorted(COVERAGE_SLOT_KINDS)}")
        return text

    @field_validator("test_family")
    @classmethod
    def validate_test_family(cls, value: str) -> str:
        from cairn.server.services.common import DEFAULT_COPY_TEST_FAMILIES

        text = value.strip().lower().replace("-", "_").replace(" ", "_")
        if text and text not in DEFAULT_COPY_TEST_FAMILIES:
            raise ValueError(f"test_family must be one of {sorted(DEFAULT_COPY_TEST_FAMILIES)}")
        return text

    @field_validator(
        "description", "applicability_basis", "completion_criteria"
    )
    @classmethod
    def validate_description(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else ""


class SurfaceApplicabilityInput(BaseModel):
    test_family: str
    description: str = ""
    applicability_basis: str
    completion_criteria: str

    @field_validator("test_family")
    @classmethod
    def validate_test_family(cls, value: str) -> str:
        from cairn.server.services.common import DEFAULT_COPY_TEST_FAMILIES

        text = value.strip().lower().replace("-", "_").replace(" ", "_")
        if text not in DEFAULT_COPY_TEST_FAMILIES:
            raise ValueError(f"test_family must be one of {sorted(DEFAULT_COPY_TEST_FAMILIES)}")
        return text

    @field_validator("applicability_basis", "completion_criteria")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return value.strip()


class DiscoveredSurfaceInput(BaseModel):
    surface_kind: str
    surface_key: str
    description: str = ""
    traits: dict[str, str] = Field(default_factory=dict)
    applicable_tests: list[SurfaceApplicabilityInput] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)

    @field_validator("surface_kind")
    @classmethod
    def validate_surface_kind(cls, value: str) -> str:
        from cairn.server.services.common import COVERAGE_SLOT_KINDS

        text = value.strip().lower()
        if text not in COVERAGE_SLOT_KINDS:
            raise ValueError(f"surface_kind must be one of {sorted(COVERAGE_SLOT_KINDS)}")
        return text

    @field_validator("surface_key")
    @classmethod
    def validate_surface_key(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return value.strip()


class RegisterSurfaceManifestRequest(BaseModel):
    surfaces: list[DiscoveredSurfaceInput]
    evidence: list[EvidenceRef] = Field(default_factory=list)
    source_intent_id: str | None = None
    source_fact_id: str | None = None
    source_task_run_id: str | None = None
    tier: Literal["core", "extended"] = "extended"


class UpsertCoverageSlotsRequest(BaseModel):
    slots: list[CoverageSlotInput]
    source_intent_id: str | None = None
    # tier: core=bootstrap 首轮枚举（完成门必覆盖）；extended=过程发现（不卡完成）。
    tier: str = "extended"

    @field_validator("tier")
    @classmethod
    def validate_tier(cls, value: str) -> str:
        text = value.strip() if isinstance(value, str) else ""
        if text not in ("core", "extended"):
            raise ValueError("tier must be 'core' or 'extended'")
        return text


class SurfaceManifestEntry(BaseModel):
    entry_id: str
    project_id: str
    surface_id: str
    surface_kind: str
    surface_key: str
    description: str = ""
    traits: dict[str, Any] = Field(default_factory=dict)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    source_intent_id: str | None = None
    source_fact_id: str | None = None
    source_task_run_id: str | None = None
    observation_fingerprint: str
    manifest_version: int = 1
    status: str = "observed"
    created_at: str


class CoverageSlot(BaseModel):
    project_id: str
    slot_key: str
    kind: str
    description: str = ""
    status: str = "pending"
    # default-copy-v1 的 Cell 身份与测试适用依据；legacy pentest 行保持空值。
    surface_key: str | None = None
    surface_id: str | None = None
    surface_kind: str = ""
    test_family: str = ""
    coverage_requirement: str = ""
    coverage_rule_id: str = ""
    applicability_basis: str = ""
    completion_criteria: str = ""
    result: str = ""
    skip_reason: str = ""
    blocker_fingerprint: str | None = None
    blocker_strategy_key: str = ""
    blocker_cooldown_until: str | None = None
    # tier: core=bootstrap 首轮枚举（完成门必覆盖）；extended=过程发现（不卡完成候选）。
    tier: str = "extended"
    source_intent_id: str | None = None
    assigned_intent_id: str | None = None
    resulting_fact_id: str | None = None
    verified_run_id: str | None = None
    created_at: str
    updated_at: str


# 漏洞管理（所有场景通用）：worker 输出 fact 带 vulnerability 字段自动确认；
# pentest slot verified 时升级 confirmed；支持人工独立创建。
VULNERABILITY_SEVERITIES = ("critical", "high", "medium", "low", "info")
VULNERABILITY_STATUSES = ("new", "confirmed", "dismissed", "remediated")


class VulnerabilityInput(BaseModel):
    """worker 协议里 fact.vulnerability 子字段（dispatcher 提取后透传 server）。"""

    title: str
    severity: str
    vuln_type: str = ""
    target: str = ""
    # packet_path 由 dispatcher 解析成 packet_artifact_id 后透传，不直接传 server。
    remediation: str = ""
    confidence: float = 0.5

    @field_validator("title", "severity")
    @classmethod
    def validate_non_empty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, value: str) -> str:
        text = value.strip().lower()
        if text not in VULNERABILITY_SEVERITIES:
            raise ValueError(f"severity must be one of {VULNERABILITY_SEVERITIES}")
        return text

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, value: float) -> float:
        if not isinstance(value, (int, float)):
            raise ValueError("confidence must be a number 0-1")
        return max(0.0, min(1.0, float(value)))


class CreateVulnerabilityRequest(BaseModel):
    """人工独立创建漏洞（无 source_fact_id），或服务端 conclude 自动确认时内部构造。"""

    title: str
    severity: str
    vuln_type: str = ""
    target: str = ""
    packet_artifact_id: str | None = None
    remediation: str = ""
    confidence: float = 0.5
    repro_request: str = ""
    repro_response: str = ""
    # 自动确认时填；人工创建留空。
    source_fact_id: str | None = None
    slot_key: str | None = None

    @field_validator("title", "severity")
    @classmethod
    def validate_non_empty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, value: str) -> str:
        text = value.strip().lower()
        if text not in VULNERABILITY_SEVERITIES:
            raise ValueError(f"severity must be one of {VULNERABILITY_SEVERITIES}")
        return text

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, value: float) -> float:
        if not isinstance(value, (int, float)):
            raise ValueError("confidence must be a number 0-1")
        return max(0.0, min(1.0, float(value)))


class UpdateVulnerabilityRequest(BaseModel):
    status: str | None = None
    severity: str | None = None
    remediation: str | None = None
    confidence: float | None = None
    # 人工补录复现数据包（内联原始 HTTP 文本）；空字符串清除，非空覆盖。
    repro_request: str | None = None
    repro_response: str | None = None
    # 报告阶段回写的编号复现步骤（markdown）；None=不改动。
    repro_steps: str | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip().lower()
        if text not in VULNERABILITY_STATUSES:
            raise ValueError(f"status must be one of {VULNERABILITY_STATUSES}")
        return text

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip().lower()
        if text not in VULNERABILITY_SEVERITIES:
            raise ValueError(f"severity must be one of {VULNERABILITY_SEVERITIES}")
        return text

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not isinstance(value, (int, float)):
            raise ValueError("confidence must be a number 0-1")
        return max(0.0, min(1.0, float(value)))


class Vulnerability(BaseModel):
    id: str
    project_id: str
    title: str
    severity: str
    vuln_type: str = ""
    target: str = ""
    confidence: float = 0.5
    status: str = "new"
    source_fact_id: str | None = None
    packet_artifact_id: str | None = None
    remediation: str = ""
    repro_request: str = ""
    repro_response: str = ""
    # 报告阶段生成的编号复现步骤（markdown），空=未生成。
    repro_steps: str = ""
    discovered_by_run_id: str | None = None
    discovered_by_worker: str | None = None
    slot_key: str | None = None
    fingerprint: str | None = None
    identity: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class VulnerabilityEvidenceArtifact(BaseModel):
    """漏洞证据聚合：来源 fact 的 evidence、产出 task_run 的 stdout/diagnostic 日志等。"""

    id: str
    kind: str
    filename: str = ""
    # pointer 仅对 fact_evidence 有意义（指向 artifact 内的片段）；run 日志为空
    pointer: str = ""
    source: str = ""  # fact_evidence / discovered_run


class VulnerabilitySourceFact(BaseModel):
    """漏洞来源 fact：description 含完整复现分析（即使 packet 丢失也能展示复现步骤）。"""

    id: str
    description: str
    task_run_id: str | None = None
    evidence: list[VulnerabilityEvidenceArtifact] = []


class VulnerabilityObservation(BaseModel):
    """一次独立发现记录；canonical 合并后仍保留该次身份与复现证据。"""

    id: str
    source_fact_id: str | None = None
    task_run_id: str | None = None
    worker: str | None = None
    slot_key: str | None = None
    packet_artifact_id: str | None = None
    identity: dict[str, Any] = Field(default_factory=dict)
    severity: str
    confidence: float
    repro_request: str = ""
    repro_response: str = ""
    observed_at: str


class VerifierCanonicalVulnerability(BaseModel):
    """Verifier 只读的 canonical 漏洞身份，避免把可变详情当作复现目标。"""

    id: str
    title: str
    severity: str
    status: str
    vuln_type: str = ""
    target: str = ""
    identity: dict[str, Any] = Field(default_factory=dict)


class VulnerabilityVerifierTarget(BaseModel):
    """独立 Verifier 的权威 Observation 快照。"""

    neutral_projection: dict[str, Any] = Field(default_factory=dict)
    vulnerability: VerifierCanonicalVulnerability
    observation: VulnerabilityObservation
    source_fact: VulnerabilitySourceFact
    discovery_task_run_id: str
    discovery_worker: str | None = None
    discovery_session_id: str


class VulnerabilityChainNode(BaseModel):
    """候选漏洞链中的 canonical finding 节点。"""

    vulnerability_id: str
    title: str
    severity: str
    status: str
    vulnerability_class: str = ""
    identity: dict[str, Any] = Field(default_factory=dict)
    independently_verified: bool = False


class VulnerabilityChainEdge(BaseModel):
    """由确定性规则生成的有向关系，不等同于已确认利用链。"""

    source_vulnerability_id: str
    target_vulnerability_id: str
    relation: str
    basis: str


class VulnerabilityChainCandidate(BaseModel):
    """供独立 chain verifier 与报告审阅的软候选。"""

    id: str
    rule_version: int = 1
    rule_id: str
    status: Literal["candidate_correlation"] = "candidate_correlation"
    nodes: list[VulnerabilityChainNode] = Field(min_length=2)
    edges: list[VulnerabilityChainEdge] = Field(min_length=1)
    missing_independent_verification: list[str] = Field(default_factory=list)
    ready_for_chain_verification: bool = False


class DuplicateVulnerabilityCandidateMember(BaseModel):
    """重复候选中的只读漏洞摘要。"""

    vulnerability_id: str
    title: str
    status: str
    severity: str
    confidence: float
    observation_count: int = 0
    fingerprint: str | None = None
    identity_source: str


class DuplicateVulnerabilityCandidate(BaseModel):
    """确定性规则派生的 advisory 候选，不代表已合并或已确认重复。"""

    id: str
    project_id: str
    requested_vulnerability_id: str
    rule_version: int = 1
    rule_id: str
    status: Literal["advisory_candidate"] = "advisory_candidate"
    advisory: Literal[True] = True
    vulnerability_ids: list[str] = Field(min_length=2, max_length=2)
    members: list[DuplicateVulnerabilityCandidateMember] = Field(
        min_length=2,
        max_length=2,
    )
    reasons: list[str] = Field(default_factory=list)
    shared_dimensions: dict[str, Any] = Field(default_factory=dict)
    differing_dimensions: dict[str, dict[str, Any]] = Field(default_factory=dict)


class DuplicateVulnerabilityCandidatesResponse(BaseModel):
    """按项目和请求漏洞限定的重复候选响应。"""

    project_id: str
    requested_vulnerability_id: str
    rule_version: int = 1
    advisory: Literal[True] = True
    candidates: list[DuplicateVulnerabilityCandidate] = Field(default_factory=list)


class VulnerabilityDetail(Vulnerability):
    """漏洞详情：在基础字段上聚合来源 fact 与产出 run 的证据，供详情页展示复现信息。

    packet_artifact_id 仍指向唯一"复现数据包"，但即便它为空（worker 未声明或文件在目标侧
    未收集），source_fact.description 与 run 日志通常已包含完整复现分析，避免详情页一片空白。
    """

    source_fact: VulnerabilitySourceFact | None = None
    # 产出该漏洞的 task_run 关联 artifacts（stdout/diagnostic 日志），供展开查看完整交互
    run_artifacts: list[VulnerabilityEvidenceArtifact] = []
    observations: list[VulnerabilityObservation] = []
    verifications: list[VulnerabilityVerification] = Field(default_factory=list)


class VulnerabilityProvenanceRelation(BaseModel):
    """溯源实体间的显式关系；source 标识关系来自哪个持久化字段。"""

    source_id: str
    target_id: str
    relation: str
    source: str


class VulnerabilityProvenance(BaseModel):
    """以 Observation 为发现单位的规范化漏洞溯源集合。"""

    vulnerability: Vulnerability
    observations: list[VulnerabilityObservation] = Field(default_factory=list)
    verifications: list[VulnerabilityVerification] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    intents: list[Intent] = Field(default_factory=list)
    task_runs: list[TaskRun] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    evidence: list[FactEvidence] = Field(default_factory=list)
    relations: list[VulnerabilityProvenanceRelation] = Field(default_factory=list)
    completeness_warnings: list[str] = Field(default_factory=list)


class VulnerabilityListItem(Vulnerability):
    """跨项目成果列表项：在漏洞基础上附带项目标题，便于主菜单"成果列表"展示归属。"""

    project_title: str = ""


__all__ = [name for name in globals() if not name.startswith("__")]
