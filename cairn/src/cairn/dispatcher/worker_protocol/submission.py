"""pi 结果工具的唯一 Schema 来源；业务状态与证据归属仍由原有服务校验。"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cairn.assessment_contracts import (
    AssessmentEvidenceRef,
    DepthReviewDecision,
    ExperimentRecord,
    GOAL_DRIVEN_PROTOCOL,
    bind_goal_driven_experiment_schema,
    StructuredIntentBinding,
    Text,
)
from cairn.evidence_contracts import (
    VerificationKeyEvidence, VerificationTestStep, WorkerEvidenceItem, evidence_refs,
    validate_verification_evidence_links,
)
from cairn.server.schemas.candidates import CANDIDATE_MAX_ARTIFACT_BYTES
from cairn.server.schemas.projects import VulnerabilityIdentityInput
from .envelope import ASSESSMENT_PROTOCOL_ID, PHASE_RESULT_KINDS, validate_result_envelope

REQUEST_PROTOCOL = "cairn-agent-request-v1"
RESULT_PROTOCOL = "cairn-agent-result-v1"
RESULT_KINDS = {**PHASE_RESULT_KINDS, "healthcheck": "healthcheck_result"}
TestFamily = Literal[
    "configuration", "known_vulnerability", "authentication", "authorization",
    "session", "injection", "data_exposure", "ssrf", "file_handling",
    "business_logic", "concurrency",
]
ArtifactKind = Literal["log", "file", "report", "screenshot", "patch", "raw_output"]
ARTIFACT_KINDS = frozenset({
    "log", "file", "report", "screenshot", "patch", "raw_output",
})


class ResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True, allow_inf_nan=False)


class ArtifactDeclaration(ResultModel):
    handle: Text
    kind: ArtifactKind
    path: str = Field(pattern=r"^/")
    source_path: str = Field(pattern=r"^/")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0, le=CANDIDATE_MAX_ARTIFACT_BYTES)


class Finding(ResultModel):
    schema_version: Literal[2]
    title: Text
    severity: Literal["critical", "high", "medium", "low", "info"]
    vuln_type: str = ""
    target: str = ""
    confidence: float = Field(default=0.5, ge=0, le=1)
    remediation: str = ""
    identity: VulnerabilityIdentityInput
    evidence_items: list[WorkerEvidenceItem]


class FactResult(ResultModel):
    model_config = ConfigDict(json_schema_extra={
        "allOf": [{
            "if": {"required": ["vulnerability"], "properties": {"vulnerability": {"type": "object"}}},
            "then": {"not": {"required": ["evidence_refs"]}},
        }],
    })

    description: Text
    vulnerability: Finding | None = None
    evidence_refs: list[AssessmentEvidenceRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_finding_evidence(self) -> FactResult:
        if self.vulnerability is not None and "evidence_refs" in self.model_fields_set:
            raise ValueError("finding fact evidence_refs are projected from vulnerability.evidence_items")
        return self


class CompleteResult(ResultModel):
    description: Text
    from_: list[Text] = Field(default_factory=list, alias="from")
    evidence_refs: list[AssessmentEvidenceRef] = Field(default_factory=list)


class ApplicableTest(ResultModel):
    test_family: TestFamily
    description: str = ""
    applicability_basis: Text
    completion_criteria: Text


class SurfaceResult(ResultModel):
    surface_kind: Literal["endpoint", "service", "host", "role", "path", "param", "flow_state"]
    surface_key: Text
    description: str = ""
    traits: dict[str, str | bool | int | float] = Field(default_factory=dict)
    applicable_tests: list[ApplicableTest] = Field(default_factory=list)
    evidence_refs: list[AssessmentEvidenceRef] = Field(default_factory=list)


class ScopeDisposition(ResultModel):
    disposition_kind: Literal["no_in_scope_targets", "no_reachable_surfaces"]
    target_ids: list[Text]
    reason: Text
    evidence_refs: list[AssessmentEvidenceRef] = Field(min_length=1)


class CoverageResult(ResultModel):
    slot_key: Text
    surface_key: Text
    test_family: TestFamily
    outcome: Literal[
        "tested_no_finding", "finding", "temporarily_blocked", "permanently_unreachable",
        "out_of_scope", "not_applicable",
    ]
    baseline: str = ""
    variants: list[Text] = Field(default_factory=list)
    evidence_refs: list[AssessmentEvidenceRef] = Field(default_factory=list)
    blocker_code: str = ""
    strategy_key: str = ""
    limitation_reason: str = ""
    limitation_details: str = ""
    attempted_strategies: list[Text] = Field(default_factory=list)


_FINDING_DATA_EVIDENCE_CONSTRAINT = {
    "if": {
        "required": ["fact"],
        "properties": {"fact": {
            "required": ["vulnerability"],
            "properties": {"vulnerability": {"type": "object"}},
        }},
    },
    "then": {"not": {"required": ["evidence_refs"]}},
}


class BootstrapData(ResultModel):
    model_config = ConfigDict(json_schema_extra={
        "allOf": [_FINDING_DATA_EVIDENCE_CONSTRAINT, {
            "if": {"required": ["coverage_scope_disposition"]},
            "then": {
                "properties": {
                    "discovered_surfaces": {"maxItems": 0},
                },
            },
        }],
    })

    fact: FactResult
    complete: CompleteResult
    discovered_surfaces: list[SurfaceResult] = Field(default_factory=list)
    coverage_scope_disposition: ScopeDisposition | None = None
    evidence_refs: list[AssessmentEvidenceRef] = Field(default_factory=list)
    artifacts: list[ArtifactDeclaration] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_scope_disposition(self) -> BootstrapData:
        if self.fact.vulnerability is not None and "evidence_refs" in self.model_fields_set:
            raise ValueError("finding data evidence_refs are projected from vulnerability.evidence_items")
        # 显式声明零表面处置时，不能同时提交已发现表面。
        if ("coverage_scope_disposition" in self.model_fields_set
                and self.discovered_surfaces):
            raise ValueError(
                "coverage_scope_disposition cannot coexist with discovered surfaces"
            )
        return self


class BootstrapConcludeData(BootstrapData):
    complete: CompleteResult | None = None


class ExploreData(ResultModel):
    model_config = ConfigDict(json_schema_extra={
        "allOf": [_FINDING_DATA_EVIDENCE_CONSTRAINT],
    })

    fact: FactResult
    coverage_result: CoverageResult | None = None
    assessment_record: ExperimentRecord | None = None
    discovered_surfaces: list[SurfaceResult] = Field(default_factory=list)
    evidence_refs: list[AssessmentEvidenceRef] = Field(default_factory=list)
    artifacts: list[ArtifactDeclaration] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_finding_evidence(self) -> ExploreData:
        if self.fact.vulnerability is not None and "evidence_refs" in self.model_fields_set:
            raise ValueError("finding data evidence_refs are projected from vulnerability.evidence_items")
        return self


class VerificationResult(ResultModel):
    observation_id: Text
    outcome: Literal["confirmed", "not_reproduced", "environment_changed", "insufficient_evidence"]
    evidence_refs: list[AssessmentEvidenceRef] = Field(min_length=1)
    notes: str = ""


class VerifierData(ResultModel):
    description: Text
    verification_result: VerificationResult
    artifacts: list[ArtifactDeclaration] = Field(default_factory=list)


class ExternalVerificationData(ResultModel):
    """新复测只填写一套有序原件关系；底层引用由条目投影。"""

    model_config = ConfigDict(json_schema_extra={
        "allOf": [{
            "if": {"properties": {"outcome": {"const": "insufficient_evidence"}}, "required": ["outcome"]},
            "then": {"properties": {"limitations": {"minItems": 1}}},
            "else": {"properties": {"evidence_items": {
                "contains": {"anyOf": [
                    {"required": [name], "properties": {name: {"type": "object"}}}
                    for name in ("request", "response", "artifact")
                ]},
            }}},
        }],
    })

    schema_version: Literal[3]
    outcome: Literal["confirmed", "not_reproduced", "environment_changed", "insufficient_evidence"]
    summary: Text
    tested_scope: Text
    limitations: list[Text]
    reason_code: Text | None = None
    key_evidence: list[VerificationKeyEvidence]
    test_steps: list[VerificationTestStep]
    evidence_items: list[WorkerEvidenceItem]
    artifacts: list[ArtifactDeclaration] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_outcome_evidence(self) -> ExternalVerificationData:
        if self.outcome == "insufficient_evidence":
            if not self.limitations:
                raise ValueError("insufficient_evidence requires explicit limitations")
        elif not evidence_refs(self.evidence_items):
            raise ValueError("tested outcomes require current execution evidence")
        validate_verification_evidence_links(self.evidence_items, self.key_evidence, self.test_steps)
        return self


class IntentDecision(ResultModel):
    from_: list[Text] = Field(alias="from", min_length=1)
    description: Text
    priority: Literal[0, 1, 2] = 1
    slot_key: Text | None = None
    strategy_key: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._:-]{0,127}$")
    depends_on: Text | None = None


class RejectedOption(ResultModel):
    description: Text
    reason: Text


class ReasonData(ResultModel):
    model_config = ConfigDict(json_schema_extra={
        "allOf": [{
            "if": {"required": ["complete"], "properties": {"complete": {"type": "object"}}},
            "then": {"properties": {
                "intents": {"anyOf": [{"type": "null"}, {"type": "array", "maxItems": 0}]},
                "abandon": {"maxItems": 0},
                "discovered_surfaces": {"maxItems": 0},
                "depth_review": {"type": "null"},
            }},
        }],
    })

    intents: list[IntentDecision] | None = None
    complete: CompleteResult | None = None
    depth_review: DepthReviewDecision | None = None
    abandon: list[Text] = Field(default_factory=list)
    discovered_surfaces: list[SurfaceResult] = Field(default_factory=list)
    rationale: str = ""
    confidence: float | None = Field(default=None, ge=0, le=1)
    rejected_options: list[RejectedOption] = Field(default_factory=list)
    blocked_by: list[Text] = Field(default_factory=list)
    depends_on_targets: list[Text] = Field(default_factory=list)
    evidence_refs: list[AssessmentEvidenceRef] = Field(default_factory=list)
    artifacts: list[ArtifactDeclaration] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_completion_actions(self) -> ReasonData:
        # 空数组／null 不产生业务动作；完成建议只能与真正的新动作互斥。
        if self.complete is not None:
            if self.intents:
                raise ValueError("complete and intents cannot coexist")
            if self.abandon:
                raise ValueError("completion cannot discard obligations in the same decision")
            if self.discovered_surfaces:
                raise ValueError("completion cannot add discovered surfaces")
            if self.depth_review is not None:
                raise ValueError("depth_review must be an atomic standalone decision")
        return self


class ReportFinding(ResultModel):
    vulnerability_id: Text
    impact: str = ""
    root_cause: str = ""
    repro_steps: Text
    remediation: Text
    regression_steps: Text
    limitations: str = ""


REPORT_DATA_SCHEMA_VERSION = 2
REPORT_FACT_REFS = (
    "scope", "counts", "work_results", "execution_status", "limitations", "evidence_index",
)


class ReportMarkdownFragment(ResultModel):
    type: Literal["markdown"]
    markdown: Annotated[str, Field(min_length=1, pattern=r"\S")]


class ReportFactReference(ResultModel):
    type: Literal["fact"]
    ref: Text


class ReportData(ResultModel):
    schema_version: Literal[2]
    executive_summary: Annotated[str, Field(min_length=1, max_length=20000, pattern=r"\S")]
    findings: list[ReportFinding]
    layout: list[ReportMarkdownFragment | ReportFactReference] = Field(min_length=1)
    limitations: str = ""
    artifacts: list[ArtifactDeclaration] = Field(default_factory=list)


class HealthcheckData(ResultModel):
    status: Literal["ok"]


PHASE_DATA_MODELS: dict[str, type[ResultModel]] = {
    "bootstrap_execute": BootstrapData,
    "bootstrap_conclude": BootstrapConcludeData,
    "reason_execute": ReasonData,
    "explore_execute": ExploreData,
    "explore_conclude": ExploreData,
    "verifier_execute": VerifierData,
    "verifier_conclude": VerifierData,
    "external_verify_execute": ExternalVerificationData,
    "report_execute": ReportData,
}
_RESULT_DATA_MODELS: dict[str, type[ResultModel]] = {
    **PHASE_DATA_MODELS,
    "healthcheck": HealthcheckData,
}


def validate_phase_data(
    phase: str,
    data: dict[str, Any],
    *,
    assessment_protocol: str | None = None,
) -> dict[str, Any]:
    """按项目冻结协议校验阶段数据，不扩充模型没有提供的事实。"""
    model = _RESULT_DATA_MODELS.get(phase)
    if model is None:
        raise ValueError("unsupported result phase")
    if (
        assessment_protocol == GOAL_DRIVEN_PROTOCOL
        and phase in {"bootstrap_execute", "bootstrap_conclude"}
    ):
        # 新协议 Bootstrap 只创建初始 Fact；历史 execute 仍必须提交 Complete。
        model = BootstrapConcludeData
    validated = model.model_validate(data)
    return validated.model_dump(by_alias=True, exclude_none=True, exclude_unset=True)


def _hide_fields(schema: dict[str, Any], names: list[str]) -> None:
    for name in names:
        schema.get("properties", {}).pop(name, None)
    schema["required"] = [key for key in schema.get("required", []) if key not in names]


def _array_schema(node: dict[str, Any]) -> dict[str, Any] | None:
    for variant in node.get("anyOf", [node]):
        if variant.get("type") == "array":
            return variant
    return None


def _required_non_null_field(
    schema: dict[str, Any],
    name: str,
    *,
    ref: str | None = None,
) -> None:
    """为冻结协议收紧 optional 字段，不改变历史模型的解码能力。"""
    required = schema.setdefault("required", [])
    if name not in required:
        required.append(name)
    if ref is not None:
        schema["properties"][name] = {"$ref": ref}
        return
    node = schema["properties"][name]
    variants = [
        item for item in node.get("anyOf", [])
        if item.get("type") != "null"
    ]
    if len(variants) == 1:
        schema["properties"][name] = variants[0]


def _bind_structured_reason_intents(
    data_schema: dict[str, Any],
    definitions: dict[str, Any],
    binding: dict[str, Any],
) -> None:
    raw = binding.get("structured_intent_binding")
    if raw is None:
        return
    contract = StructuredIntentBinding.model_validate(raw)
    if contract.assessment_phase not in {"mapping", "baseline"}:
        return
    intent_schema = definitions["IntentDecision"]
    required = intent_schema.setdefault("required", [])
    for name in ("slot_key", "strategy_key"):
        if name not in required:
            required.append(name)
    cells = {cell.slot_key: cell for cell in contract.eligible_cells}
    slot_schema: dict[str, Any] = {"type": "string"}
    if cells:
        slot_schema["enum"] = list(cells)
    else:
        # 空 enum 在部分 Schema 编译器中不兼容；零长度 intents 已使该字段不可达。
        slot_schema["not"] = {}
    intent_schema["properties"]["slot_key"] = slot_schema
    intent_schema["properties"]["strategy_key"] = {
        "type": "string",
        "pattern": r"^[a-z0-9][a-z0-9._:-]{0,127}$",
    }
    constraints = intent_schema.setdefault("allOf", [])
    for cell in contract.eligible_cells:
        if cell.strategy.mode != "must_differ":
            continue
        constraints.append(
            {
                "if": {
                    "properties": {"slot_key": {"const": cell.slot_key}},
                    "required": ["slot_key"],
                },
                "then": {
                    "properties": {
                        "strategy_key": {
                            "not": {
                                "const": cell.strategy.excluded_strategy_key
                            }
                        }
                    }
                },
            }
        )
    intents = _array_schema(data_schema.get("properties", {}).get("intents", {}))
    if intents is not None:
        configured = binding.get("max_intents")
        limit = len(cells)
        if type(configured) is int and configured >= 0:
            limit = min(limit, configured)
        intents["maxItems"] = limit


def _build_submit_result_schema(
    phase: str,
    binding: dict[str, Any] | None,
    *,
    data_models: dict[str, type[ResultModel]],
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    model = data_models.get(phase)
    if model is None:
        raise ValueError("unsupported result phase")
    data_schema = model.model_json_schema(by_alias=True)
    definitions = data_schema.pop("$defs", {})
    _hide_fields(data_schema, ["artifacts"])
    binding = binding or {}
    goal_driven = binding.get("assessment_protocol") == GOAL_DRIVEN_PROTOCOL
    if phase in {"bootstrap_execute", "bootstrap_conclude"}:
        # Bootstrap 的 Complete 只引用 conclude 新建的 Fact，模型不能声明该身份。
        _hide_fields(definitions["CompleteResult"], ["from"])
        if goal_driven:
            # 新协议只接受初始 Fact；项目完成统一由后续 Reason 建议并由 Server 复核。
            _hide_fields(data_schema, ["complete"])
    if "CoverageResult" in definitions:
        if goal_driven:
            # 新普通 Explore 只有 Fact.assessment_record 一份实验权威表达。
            _hide_fields(data_schema, ["coverage_result"])
            _required_non_null_field(
                data_schema,
                "assessment_record",
                ref="#/$defs/ExperimentRecord",
            )
            bind_goal_driven_experiment_schema(definitions["ExperimentRecord"])
            # finding 与漏洞声明一一对应，不能靠 outcome 推测或补造 identity。
            data_schema.setdefault("allOf", []).append({
                "if": {
                    "properties": {
                        "assessment_record": {
                            "properties": {"outcome": {"const": "finding"}},
                            "required": ["outcome"],
                        },
                    },
                    "required": ["assessment_record"],
                },
                "then": {
                    "properties": {
                        "fact": {
                            "required": ["vulnerability"],
                            "properties": {"vulnerability": {"$ref": "#/$defs/Finding"}},
                        },
                    },
                },
                "else": {
                    "properties": {
                        "fact": {"properties": {"vulnerability": {"type": "null"}}},
                    },
                },
            })
        else:
            fixed = [key for key in ("slot_key", "surface_key", "test_family") if binding.get(key)]
            _hide_fields(definitions["CoverageResult"], fixed)
            if len(fixed) == 3:
                data_schema.setdefault("required", []).append("coverage_result")
                data_schema["properties"]["coverage_result"] = {"$ref": "#/$defs/CoverageResult"}
    if binding.get("verification_observation_id") and "VerificationResult" in definitions:
        _hide_fields(definitions["VerificationResult"], ["observation_id"])
    if binding.get("review_input_digest") and "DepthReviewDecision" in definitions:
        _hide_fields(definitions["DepthReviewDecision"], ["input_digest"])
    allowed_facts = binding.get("allowed_fact_ids")
    if isinstance(allowed_facts, list):
        for name in ("IntentDecision", "CompleteResult", "DepthSelection"):
            node = definitions.get(name, {}).get("properties", {}).get("from")
            if node is not None:
                node["items"] = {"type": "string", "enum": allowed_facts}
    max_intents = binding.get("max_intents")
    if type(max_intents) is int and max_intents >= 0:
        intents = _array_schema(
            data_schema.get("properties", {}).get("intents", {})
        )
        if intents is not None:
            intents["maxItems"] = max_intents
    if phase == "reason_execute":
        # decision_input_digest 仅由 goal-driven Reason 冻结；开放工作取原交付输入，不能用实时图谱替换。
        if binding.get("decision_input_digest") is not None:
            open_intent_ids = inputs.get("open_intent_ids") if isinstance(inputs, dict) else None
            if not isinstance(open_intent_ids, list):
                raise ValueError("goal-driven Reason requires frozen open_intent_ids")
            if not open_intent_ids:
                data_schema["anyOf"] = [
                    {"required": ["intents"], "properties": {"intents": {"type": "array", "minItems": 1}}},
                    {"required": ["complete"], "properties": {"complete": {"$ref": "#/$defs/CompleteResult"}}},
                    {"required": ["abandon"], "properties": {"abandon": {"type": "array", "minItems": 1}}},
                ]
        _bind_structured_reason_intents(data_schema, definitions, binding)
        allowed_intents = binding.get("allowed_intent_ids")
        if isinstance(allowed_intents, list):
            definitions["IntentDecision"]["properties"]["depends_on"] = {
                "anyOf": [
                    ({"type": "string", "enum": allowed_intents} if allowed_intents
                     else {"not": {}}),
                    {"type": "string", "pattern": r"^@[1-9][0-9]*$"},
                    {"type": "null"},
                ],
                "description": "Existing Intent ID, or @N for an earlier item in this batch (1-based).",
            }
            intents_schema = _array_schema(data_schema["properties"]["intents"])
            if intents_schema is not None and type(max_intents) is int:
                # prefixItems 覆盖的位置不再由 items 校验，须同时保留完整结构和依赖顺序约束。
                intents_schema["prefixItems"] = []
                for index in range(max_intents):
                    choices = [*allowed_intents, *(f"@{i + 1}" for i in range(index))]
                    dependency_schema = (
                        {"anyOf": [{"type": "string", "enum": choices}, {"type": "null"}]}
                        if choices else {"type": "null"}
                    )
                    intents_schema["prefixItems"].append({
                        "allOf": [
                            intents_schema["items"],
                            {"properties": {"depends_on": dependency_schema}},
                        ],
                    })
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "accepted": {"type": "boolean"},
            "data": data_schema,
            "reason": {"type": "string", "minLength": 1},
        },
        "required": ["accepted"],
        "allOf": [{
            "if": {"properties": {"accepted": {"const": True}}},
            "then": {"required": ["data"]},
            "else": {"required": ["reason"], "not": {"required": ["data"]}},
        }],
        "$defs": definitions,
    }


def build_submit_result_schema(
    phase: str,
    binding: dict[str, Any] | None = None,
    *,
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """从当前阶段及冻结交付输入构造实际 submit_result Schema。"""
    return _build_submit_result_schema(
        phase,
        binding,
        data_models=PHASE_DATA_MODELS,
        inputs=inputs,
    )


SUBMISSION_FAILURE_CODES = frozenset({
    "submission_missing", "submission_invalid", "submission_conflict",
    "submission_injected_too_large", "artifact_unknown", "artifact_changed",
    "artifact_invalid", "artifact_unreadable", "artifact_permission", "tool_permission",
})
CANDIDATE_MAX_OUTCOMES_PER_CANDIDATE = 64
CANDIDATE_OUTCOME_CODES = SUBMISSION_FAILURE_CODES | frozenset({
    "candidate_admission_rejected", "candidate_manifest_invalid",
    "candidate_outcome_invalid", "candidate_spool_conflict",
    "candidate_spool_invalid", "candidate_spool_limit",
    "candidate_spool_unavailable", "locally_validated", "provider_auth",
    "provider_error", "provider_rate_limit", "result_schema", "runner_error",
})
SUBMISSION_ISSUE_KEYWORDS = frozenset({
    "schema", "strict_json", "input_size", "type", "required", "additionalProperties",
    "const", "enum", "anyOf", "oneOf", "allOf", "not", "if", "minLength", "maxLength",
    "pattern", "format", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "multipleOf", "minItems", "maxItems", "uniqueItems", "minProperties", "maxProperties",
})


@lru_cache(maxsize=len(_RESULT_DATA_MODELS))
def submission_schema_paths(phase: str) -> frozenset[str]:
    """从对应阶段 Schema 推导历史诊断路径白名单。"""
    if phase not in _RESULT_DATA_MODELS:
        return frozenset()
    root = _build_submit_result_schema(
        phase,
        None,
        data_models=_RESULT_DATA_MODELS,
    )
    paths = {"$"}

    def walk(node: object, path: str, depth: int = 0) -> None:
        if not isinstance(node, dict) or depth > 32:
            return
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            walk(root.get("$defs", {}).get(ref[8:]), path, depth + 1)
        for key, child in node.get("properties", {}).items():
            child_path = path + "/" + key.replace("~", "~0").replace("/", "~1")
            paths.add(child_path)
            walk(child, child_path, depth + 1)
        if isinstance(node.get("items"), dict):
            paths.add(path + "/*")
            walk(node["items"], path + "/*", depth + 1)
        for keyword in ("anyOf", "oneOf", "allOf"):
            for child in node.get(keyword, []):
                walk(child, path, depth + 1)

    walk(root, "")
    return frozenset(paths)


def sanitize_submission_diagnostics(value: object, phase: str) -> dict[str, Any]:
    """Worker 自报的诊断只是 advisory；独立验证固定 keyword、声明路径及有界计数。"""
    if not isinstance(value, dict):
        return {}
    safe: dict[str, Any] = {}
    issue = value.get("issue")
    if isinstance(issue, dict):
        keyword, path = issue.get("keyword"), issue.get("path")
        if (isinstance(keyword, str) and keyword in SUBMISSION_ISSUE_KEYWORDS
                and isinstance(path, str) and path in submission_schema_paths(phase)):
            safe["issue"] = {"keyword": keyword, "path": path}
    attempts, repairs, maximum = (value.get(key) for key in (
        "submission_attempts", "repair_rounds", "max_repair_rounds",
    ))
    if (type(attempts) is int and 0 <= attempts <= 1_000_000
            and type(repairs) is int and type(maximum) is int and 0 <= repairs <= maximum <= 2):
        safe.update(submission_attempts=attempts, repair_rounds=repairs, max_repair_rounds=maximum)
    return safe


def validate_healthcheck_payload(payload: dict[str, Any]) -> tuple[str, object | None]:
    accepted, data = validate_result_envelope(payload, allowed_kinds={"healthcheck_result"})
    if not accepted:
        return "rejected", None
    assert data is not None
    HealthcheckData.model_validate(data)
    return "healthcheck", data


def exported_contracts() -> dict[str, Any]:
    return {
        "request_protocol": REQUEST_PROTOCOL,
        "result_protocol": RESULT_PROTOCOL,
        "assessment_protocol": ASSESSMENT_PROTOCOL_ID,
        "phase_result_kinds": PHASE_RESULT_KINDS,
        "submit_result_schemas": {
            phase: build_submit_result_schema(phase)
            for phase in PHASE_DATA_MODELS
        },
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="导出 pi 结果工具的固定 Schema")
    parser.add_argument("output", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = json.dumps(exported_contracts(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.check:
        if args.output.read_text(encoding="utf-8") != content:
            raise SystemExit("pi contract export is stale")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
