"""评估实验与深测选择的共享结构；工具 Schema 和服务写入使用同一份定义。"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1)]
GOAL_DRIVEN_PROTOCOL = "goal-driven-v1"


def goal_driven_decision_input(project: Any) -> dict[str, object]:
    """构造稳定业务输入；排除 worker 租约、心跳和其他瞬时运行噪声。"""
    execution = project.project.assessment_execution
    goal = next(
        (
            getattr(fact, "description", "")
            for fact in project.facts
            if fact.id == "goal"
        ),
        "",
    )
    targets = [
        {
            "id": target.id,
            "value": target.value,
            "type": target.type,
            "status": target.status,
            "source_fact_id": target.source_fact_id,
            "discovered_by_run_id": target.discovered_by_run_id,
        }
        for target in sorted(
            getattr(project, "targets", []),
            key=lambda item: (getattr(item, "created_at", ""), item.id),
        )
        if target.status in {"seed", "approved"}
    ]
    return {
        "version": 1,
        "project": {
            "id": project.project.id,
            "assessment_protocol": project.project.assessment_protocol,
            "scenario_key": project.project.scenario_key,
            "execution_generation": project.project.execution_generation,
            "vulnerability_fingerprint_version": (
                project.project.vulnerability_fingerprint_version
            ),
            "goal": goal,
        },
        "policy": {
            key: execution.get(key)
            for key in (
                "version",
                "execution_contract_version",
                "execution_engine",
                "goal_driven",
                "surface_manifest_enabled",
                "independent_verifier_enabled",
                "outcome_contract_version",
            )
        },
        "budget": {
            "explore_budget": project.project.explore_budget,
            "explore_deadline": project.project.explore_deadline,
            "explore_attempts_started": project.project.explore_attempts_started,
            "verification_attempts_started": (
                project.project.verification_attempts_started
            ),
            "remaining_total": execution.get("remaining_total"),
            "remaining_discovery": execution.get("remaining_discovery"),
            "remaining_verification": execution.get("remaining_verification"),
        },
        "facts": [
            {
                "id": fact.id,
                "description": fact.description,
                "task_run_id": fact.task_run_id,
                "creator": fact.creator,
            }
            for fact in sorted(
                project.facts,
                key=lambda item: (item.created_at, item.id),
            )
        ],
        "intents": [
            {
                "id": intent.id,
                "from": intent.from_,
                "to": intent.to,
                "description": intent.description,
                "assessment_role": intent.assessment_role,
                "priority": getattr(intent, "priority", 1),
                "target_observation_id": intent.target_observation_id,
                "depends_on": intent.depends_on,
                "conclusion": intent.conclusion,
                "conclusion_reason": intent.conclusion_reason,
            }
            for intent in sorted(
                project.intents,
                key=lambda item: (item.created_at, item.id),
            )
        ],
        "hints": [
            {
                "id": hint.id,
                "content": hint.content,
                "fact_id": hint.fact_id,
                "intent_id": hint.intent_id,
            }
            for hint in sorted(
                project.hints,
                key=lambda item: (item.created_at, item.id),
            )
        ],
        "targets": targets,
    }


def goal_driven_decision_input_digest(project: Any) -> str:
    material = json.dumps(
        goal_driven_decision_input(project),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(material).hexdigest()


class AssessmentRecordModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class AssessmentEvidenceRef(AssessmentRecordModel):
    """上传前可使用句柄，服务端写入前必须解析为项目内 Artifact ID。"""

    model_config = ConfigDict(
        extra="forbid", strict=True, populate_by_name=True,
        json_schema_extra={"oneOf": [
            {"required": ["artifact_id"], "properties": {"artifact_id": {"type": "string", "minLength": 1}, "artifact_handle": {"type": "null"}}},
            {"required": ["artifact_handle"], "properties": {"artifact_handle": {"type": "string", "minLength": 1}, "artifact_id": {"type": "null"}}},
        ]},
    )

    artifact_id: Text | None = None
    artifact_handle: Text | None = None
    pointer: str = ""
    verifier_run_id: Text | None = None

    @model_validator(mode="after")
    def require_one_reference(self) -> AssessmentEvidenceRef:
        if (self.artifact_id is None) == (self.artifact_handle is None):
            raise ValueError("exactly one artifact_id or artifact_handle is required")
        return self


class ExperimentRecord(AssessmentRecordModel):
    version: Literal[1] = 1
    kind: Literal["experiment"] = "experiment"
    hypothesis: Text
    outcome: Literal[
        "tested_no_finding",
        "finding",
        "temporarily_blocked",
        "permanently_unreachable",
        "out_of_scope",
        "not_applicable",
        "observation",
    ] | None = None
    preconditions: list[Text] = Field(default_factory=list)
    baseline: str = ""
    variants: list[Text] = Field(default_factory=list)
    controls: list[Text] = Field(default_factory=list)
    observations: list[Text] = Field(default_factory=list)
    impact: str = ""
    limitations: list[Text] = Field(default_factory=list)
    blocker_code: str = ""
    limitation_reason: str = ""
    limitation_details: str = ""
    attempted_strategies: list[Text] = Field(default_factory=list)
    evidence_refs: list[AssessmentEvidenceRef] = Field(default_factory=list)


# 仅 goal-driven 使用；可选对照保留原值，确定性准入只检查结论所需材料。
GOAL_DRIVEN_OUTCOME_FIELDS = {
    "finding": ("observations",),
    "tested_no_finding": ("observations",),
    "temporarily_blocked": (
        "blocker_code", "limitation_reason", "limitation_details", "attempted_strategies",
    ),
    "permanently_unreachable": (
        "limitation_reason", "limitation_details", "attempted_strategies",
    ),
    "out_of_scope": ("limitation_reason", "limitation_details"),
    "not_applicable": ("limitation_reason", "limitation_details"),
}


def validate_goal_driven_experiment(record: ExperimentRecord) -> None:
    """检查与目标驱动工具 Schema 相同的静态字段规则；证据归属仍由 Server 检查。"""
    if record.outcome is None:
        raise ValueError("assessment_record.outcome is required")
    if not record.hypothesis.strip():
        raise ValueError("assessment_record.hypothesis is required")
    if not record.evidence_refs:
        raise ValueError("assessment_record.evidence_refs is required")
    for field in GOAL_DRIVEN_OUTCOME_FIELDS.get(record.outcome, ()):
        value = getattr(record, field)
        if not value or (isinstance(value, str) and not value.strip()):
            raise ValueError(f"{record.outcome} requires assessment_record.{field}")
    if record.outcome == "observation" and not (record.observations or record.limitations):
        raise ValueError("observation requires assessment_record.observations or limitations")


def bind_goal_driven_experiment_schema(schema: dict[str, Any]) -> None:
    """从同一组 outcome 字段要求生成本地修正环使用的条件 Schema。"""
    schema.setdefault("required", []).extend(["outcome", "evidence_refs"])
    schema["properties"]["outcome"] = {"type": "string", "enum": [
        *GOAL_DRIVEN_OUTCOME_FIELDS, "observation",
    ]}
    schema["properties"]["hypothesis"]["pattern"] = r"\S"
    schema["properties"]["evidence_refs"]["minItems"] = 1
    conditions = schema.setdefault("allOf", [])
    for outcome, fields in GOAL_DRIVEN_OUTCOME_FIELDS.items():
        conditions.append({
            "if": {"properties": {"outcome": {"const": outcome}}, "required": ["outcome"]},
            "then": {
                "required": list(fields),
                "properties": {
                    field: ({"minItems": 1} if field in {"observations", "attempted_strategies"}
                            else {"pattern": r"\S"})
                    for field in fields
                },
            },
        })
    conditions.append({
        "if": {"properties": {"outcome": {"const": "observation"}}, "required": ["outcome"]},
        "then": {"anyOf": [
            {"required": [field], "properties": {field: {"minItems": 1}}}
            for field in ("observations", "limitations")
        ]},
    })


def valid_reason_dependency(dependency: object, index: int, allowed_intent_ids: list[str]) -> bool:
    """现有 ID 来自冻结快照；同批 @N 只能指向已提交的前项。"""
    if not isinstance(dependency, str) or not dependency:
        return False
    if dependency.startswith("@"):
        return dependency in {f"@{position}" for position in range(1, index + 1)}
    return dependency in allowed_intent_ids


class DepthSelection(AssessmentRecordModel):
    from_: list[Text] = Field(alias="from", min_length=1)
    description: Text
    slot_key: Text
    strategy_key: str = Field(pattern=r"^[a-z0-9][a-z0-9._:-]{0,127}$")
    hypothesis: Text
    preconditions: list[Text] = Field(default_factory=list)
    baseline: Text
    variants: list[Text] = Field(min_length=1)
    controls: list[Text] = Field(min_length=1)
    impact: str = ""
    limitations: list[Text] = Field(default_factory=list)
    evidence_refs: list[AssessmentEvidenceRef] = Field(min_length=1)


class DepthReviewDecision(AssessmentRecordModel):
    input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    description: Text
    evidence_refs: list[AssessmentEvidenceRef] = Field(min_length=1)
    selected: list[DepthSelection] = Field(max_length=3)


AssessmentPhase = Literal[
    "mapping", "baseline", "depth_review", "deep", "finalizing",
]
StrategyKey = Annotated[
    str,
    Field(pattern=r"^[a-z0-9][a-z0-9._:-]{0,127}$"),
]
Sha256Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class StructuredStrategyConstraint(AssessmentRecordModel):
    """Reason 为单个 Coverage Cell 冻结的 strategy 约束。"""

    mode: Literal["any", "must_differ"] = "any"
    excluded_strategy_key: StrategyKey | None = None

    @model_validator(mode="after")
    def validate_exclusion(self) -> StructuredStrategyConstraint:
        if self.mode == "must_differ" and self.excluded_strategy_key is None:
            raise ValueError("must_differ requires excluded_strategy_key")
        if self.mode == "any" and self.excluded_strategy_key is not None:
            raise ValueError("any must not include excluded_strategy_key")
        return self


class StructuredCoverageCellBinding(AssessmentRecordModel):
    """结构化 Reason 与 Explore 共用的稳定 Cell 身份和执行条件。"""

    slot_key: Text
    surface_kind: Text
    surface_key: Text
    test_family: Text
    applicability_basis: Text
    completion_criteria: Text
    coverage_requirement: Literal["", "required", "optional"] = ""
    tier: Literal["core", "extended"] = "extended"
    strategy: StructuredStrategyConstraint = Field(
        default_factory=StructuredStrategyConstraint
    )


class StructuredIntentBinding(AssessmentRecordModel):
    """Server 在 Reason admission 时签发的 eligible Cell 集合。"""

    version: Literal[1] = 1
    assessment_phase: AssessmentPhase
    issued_at: Text
    eligible_cells: list[StructuredCoverageCellBinding]
    digest: Sha256Digest

    @model_validator(mode="after")
    def require_unique_cells(self) -> StructuredIntentBinding:
        slot_keys = [cell.slot_key for cell in self.eligible_cells]
        if len(slot_keys) != len(set(slot_keys)):
            raise ValueError("eligible_cells must contain unique slot_key values")
        return self


class StructuredExploreBinding(AssessmentRecordModel):
    """Explore admission 冻结的单个 Intent/Cell 执行快照。"""

    version: Literal[1] = 1
    assessment_phase: AssessmentPhase
    admitted_at: Text
    intent_id: Text
    strategy_key: StrategyKey
    source_binding_digest: Sha256Digest | None = None
    cell: StructuredCoverageCellBinding
    digest: Sha256Digest
