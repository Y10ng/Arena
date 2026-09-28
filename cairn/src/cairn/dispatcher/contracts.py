"""Worker 阶段结果的业务契约校验与 Server 写回数据提取。"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from re import fullmatch
from typing import Any

from cairn.assessment_contracts import (
    DepthReviewDecision,
    ExperimentRecord,
    StructuredIntentBinding,
)
from cairn.dispatcher.worker_protocol import validate_result_envelope
from cairn.dispatcher.worker_protocol.submission import validate_phase_data
from cairn.server.models import EvidenceRef

LOG = logging.getLogger(__name__)

ALLOWED_DECLARED_ARTIFACT_KINDS = {
    "log",
    "file",
    "report",
    "screenshot",
    "patch",
    "raw_output",
}

# LLM 可能使用更自然的 artifact kind，写入前统一映射到 Server 支持的类型。
DECLARED_ARTIFACT_KIND_ALIASES = {
    "code": "file",
    "exploit": "file",
    "heap_dump": "file",
    "heapdump": "file",
    "script": "file",
}

# pentest 场景 coverage slot 的入口点类型白名单。
COVERAGE_SLOT_KINDS = {
    "endpoint",
    "service",
    "host",
    "role",
    "path",
    "param",
    "flow_state",
}

# default-copy-v1 的测试族与 outcome 白名单；与 Server 协议常量保持一致。
DEFAULT_COPY_TEST_FAMILIES = {
    "configuration",
    "known_vulnerability",
    "authentication",
    "authorization",
    "session",
    "injection",
    "data_exposure",
    "ssrf",
    "file_handling",
    "business_logic",
    "concurrency",
}
VERIFIER_OUTCOMES = {
    "confirmed",
    "not_reproduced",
    "environment_changed",
    "insufficient_evidence",
}
DEFAULT_COPY_COVERAGE_OUTCOMES = {
    "tested_no_finding",
    "finding",
    "temporarily_blocked",
    "permanently_unreachable",
    "out_of_scope",
    "not_applicable",
}
GOAL_DRIVEN_ASSESSMENT_OUTCOMES = DEFAULT_COPY_COVERAGE_OUTCOMES | {
    "observation",
}

# 漏洞 severity 白名单（与 server schemas VULNERABILITY_SEVERITIES 保持一致）。
VULNERABILITY_SEVERITIES = {"critical", "high", "medium", "low", "info"}

REJECTION_REASON_ERROR_TYPES = {
    "worker_rejected": "worker_rejected",
    "policy_denied": "policy_denied",
    "policy_refusal": "policy_denied",
    "tool_missing": "tool_missing",
    "missing_tool": "tool_missing",
    "network_blocked": "network_blocked",
    "target_unreachable": "target_unreachable",
    "auth_failed": "auth_failed",
    "authentication_failed": "auth_failed",
    "evidence_insufficient": "evidence_insufficient",
    "insufficient_evidence": "evidence_insufficient",
}


class PhaseContractError(ValueError):
    """由规则源声明可公开原因；动态业务值和底层异常仅保留在原始异常中。"""

    def __init__(self, message: str, *, public_reason: str | None = None):
        super().__init__(message)
        # 默认文案只允许固定规则、字段名及受控索引，不得包含 Worker 输入值。
        self.public_reason = message if public_reason is None else public_reason


@dataclass(frozen=True, slots=True)
class DeclaredArtifact:
    handle: str
    kind: str
    path: str


def rejection_error_type(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return "worker_rejected"
    reason = payload.get("reason")
    if not isinstance(reason, str):
        return "worker_rejected"
    normalized = reason.strip().lower().replace("-", "_").replace(" ", "_")
    # 拒绝原因会进入 task_run.error_type，未知原因统一按 worker_rejected 聚合。
    return REJECTION_REASON_ERROR_TYPES.get(normalized, "worker_rejected")


def _unwrap_result_envelope(
    payload: dict[str, Any],
    *,
    allowed_kinds: set[str],
) -> tuple[bool, dict[str, Any] | None]:
    return validate_result_envelope(payload, allowed_kinds=allowed_kinds)


def _is_dict(value: Any) -> bool:
    return isinstance(value, dict)


def normalize_declared_artifact_kind(kind: str) -> str | None:
    normalized = kind.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in ALLOWED_DECLARED_ARTIFACT_KINDS:
        return normalized
    return DECLARED_ARTIFACT_KIND_ALIASES.get(normalized)


_SERVER_ARTIFACT_ID_PATTERN = r"a[0-9]+"


def _validate_artifact_handle(handle: object, *, path: str) -> str:
    if not isinstance(handle, str) or not handle:
        raise PhaseContractError(f"{path} is required")
    if handle != handle.strip():
        raise PhaseContractError(f"{path} must not contain surrounding whitespace")
    if fullmatch(_SERVER_ARTIFACT_ID_PATTERN, handle):
        raise PhaseContractError(f"{path} must not use a Server artifact id")
    return handle


def _validate_artifact_declarations(data: dict[str, Any]) -> None:
    artifacts = data.get("artifacts")
    if artifacts is None:
        return
    # Artifact 只是本轮文件声明；handle 是唯一可供 Worker 结果引用的身份。
    if not isinstance(artifacts, list):
        raise PhaseContractError("artifacts must be an array")
    seen_handles: set[str] = set()
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            raise PhaseContractError(f"invalid artifact at index {index}")
        unexpected = sorted(set(artifact) - {"handle", "kind", "path"})
        if unexpected:
            raise PhaseContractError(
                f"unexpected artifact fields at index {index}: {', '.join(unexpected)}",
                public_reason=f"unexpected artifact fields at index {index}",
            )
        handle = _validate_artifact_handle(
            artifact.get("handle"),
            path=f"artifact.handle at index {index}",
        )
        path = artifact.get("path")
        kind = artifact.get("kind")
        if handle in seen_handles:
            raise PhaseContractError(
                f"artifact.handle must be unique: {handle}",
                public_reason="artifact.handle must be unique",
            )
        seen_handles.add(handle)
        if not isinstance(path, str) or not path.strip():
            raise PhaseContractError(f"artifact.path is required at index {index}")
        if not path.startswith("/"):
            raise PhaseContractError(f"artifact.path must be absolute at index {index}")
        if not isinstance(kind, str) or kind not in ALLOWED_DECLARED_ARTIFACT_KINDS:
            raise PhaseContractError(
                f"unsupported artifact kind at index {index}: {kind}",
                public_reason=f"unsupported artifact kind at index {index}",
            )


def _parse_evidence_refs(
    node: dict[str, Any],
    *,
    path: str,
) -> list[EvidenceRef]:
    raw_refs = node.get("evidence_refs")
    if raw_refs is None:
        return []
    if not isinstance(raw_refs, list):
        raise PhaseContractError(f"{path}.evidence_refs must be an array")
    refs: list[EvidenceRef] = []
    for index, raw_ref in enumerate(raw_refs):
        if not isinstance(raw_ref, dict):
            raise PhaseContractError(f"{path}.evidence_refs[{index}] must be an object")
        unexpected = sorted(
            set(raw_ref)
            - {"artifact_handle", "artifact_id", "pointer", "verifier_run_id"}
        )
        if unexpected:
            raise PhaseContractError(
                f"unexpected {path}.evidence_refs[{index}] fields: {', '.join(unexpected)}",
                public_reason=f"unexpected {path}.evidence_refs[{index}] fields",
            )
        artifact_handle = raw_ref.get("artifact_handle")
        artifact_id = raw_ref.get("artifact_id")
        if artifact_handle is not None and artifact_id is not None:
            raise PhaseContractError(
                f"{path}.evidence_refs[{index}] cannot contain both artifact_handle and artifact_id"
            )
        if artifact_handle is not None:
            identity = _validate_artifact_handle(
                artifact_handle,
                path=f"{path}.evidence_refs[{index}].artifact_handle",
            )
        else:
            identity = artifact_id
            if not isinstance(identity, str) or not identity.strip():
                raise PhaseContractError(
                    f"{path}.evidence_refs[{index}] requires artifact_handle or artifact_id"
                )
        pointer = raw_ref.get("pointer", "")
        verifier_run_id = raw_ref.get("verifier_run_id")
        try:
            refs.append(
                EvidenceRef(
                    artifact_id=identity,
                    pointer=pointer,
                    verifier_run_id=verifier_run_id,
                )
            )
        except Exception as exc:
            raise PhaseContractError(
                f"invalid {path}.evidence_refs[{index}]: {exc}",
                public_reason=f"invalid {path}.evidence_refs[{index}]",
            ) from exc
    return refs


def _validate_result_evidence_refs(data: dict[str, Any]) -> None:
    if "evidence_refs" in data:
        _parse_evidence_refs(data, path="data")
    for key in ("fact", "complete"):
        node = data.get(key)
        if isinstance(node, dict):
            _parse_evidence_refs(node, path=key)


def _validate_reason_rejected_options(data: dict[str, Any]) -> None:
    options = data.get("rejected_options")
    if options is None:
        return
    if not isinstance(options, list):
        raise PhaseContractError("rejected_options must be an array")
    for index, option in enumerate(options):
        if not isinstance(option, dict):
            raise PhaseContractError(f"invalid rejected option at index {index}")
        description = option.get("description")
        reason = option.get("reason")
        if not isinstance(description, str) or not description.strip():
            raise PhaseContractError(f"rejected_options[{index}].description is required")
        if not isinstance(reason, str) or not reason.strip():
            raise PhaseContractError(f"rejected_options[{index}].reason is required")


def _validate_reason_decision_metadata(data: dict[str, Any]) -> None:
    confidence = data.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise PhaseContractError("confidence must be a number between 0 and 1")
        if confidence < 0 or confidence > 1:
            raise PhaseContractError("confidence must be between 0 and 1")

    for key in ("blocked_by", "depends_on_targets"):
        value = data.get(key)
        if value is None:
            continue
        if not isinstance(value, list):
            raise PhaseContractError(f"{key} must be an array")
        for index, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                raise PhaseContractError(f"{key}[{index}] must be a non-empty string")


def extract_declared_artifacts(payload: dict[str, Any]) -> list[DeclaredArtifact]:
    data = payload.get("data") if payload.get("accepted") is True else payload
    if not isinstance(data, dict):
        return []
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, list):
        return []
    result: list[DeclaredArtifact] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        handle = artifact.get("handle")
        path = artifact.get("path")
        kind = artifact.get("kind")
        if not all(isinstance(value, str) for value in (handle, path, kind)):
            continue
        if kind not in ALLOWED_DECLARED_ARTIFACT_KINDS:
            continue
        result.append(DeclaredArtifact(handle=handle, kind=kind, path=path))
    return result


def extract_evidence_refs(
    payload: dict[str, Any],
    *,
    section: str | None = None,
) -> list[EvidenceRef]:
    data = payload.get("data") if payload.get("accepted") is True else payload
    if not isinstance(data, dict):
        return []
    if section is not None:
        node = data.get(section)
        if isinstance(node, dict):
            refs = _parse_evidence_refs(node, path=section)
            if refs:
                return refs
        if section == "fact" and "evidence_refs" in data:
            return _parse_evidence_refs(data, path="data")
        return []
    refs: list[EvidenceRef] = []
    for key in ("fact", "complete"):
        node = data.get(key)
        if isinstance(node, dict):
            refs.extend(_parse_evidence_refs(node, path=key))
    return refs


def extract_verification_result(payload: dict[str, Any]) -> dict[str, Any] | None:
    data = payload.get("data") if payload.get("accepted") is True else payload
    if not isinstance(data, dict):
        return None
    result = data.get("verification_result")
    if not isinstance(result, dict):
        return None
    normalized = dict(result)
    normalized["evidence_refs"] = [
        ref.model_dump(exclude_none=True)
        for ref in _parse_evidence_refs(result, path="verification_result")
    ]
    return normalized


def _validate_coverage_slots(data: dict[str, Any], *, field: str = "slots") -> list[dict[str, Any]]:
    # legacy slot 与结构化 Cell 共用信封；出现任一结构化字段时要求完整 Cell 契约。
    raw = data.get(field)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PhaseContractError(f"{field} must be an array")
    slots: list[dict[str, Any]] = []
    seen_identities: set[tuple[str, ...]] = set()
    structured_fields = {
        "surface_kind",
        "test_family",
        "applicability_basis",
        "completion_criteria",
    }
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise PhaseContractError(f"{field}[{index}] must be an object")
        kind = item.get("kind")
        key = item.get("key", "")
        description = item.get("description", "")
        if not isinstance(description, str):
            description = ""

        structured = bool(structured_fields.intersection(item))
        if not structured:
            if not isinstance(kind, str) or kind not in COVERAGE_SLOT_KINDS:
                raise PhaseContractError(f"{field}[{index}].kind must be one of {sorted(COVERAGE_SLOT_KINDS)}")
            if not isinstance(key, str) or not key.strip():
                raise PhaseContractError(f"{field}[{index}].key must be a non-empty string")
            normalized_key = key.strip()
            identity = ("legacy", normalized_key)
            normalized = {
                "kind": kind,
                "key": normalized_key,
                "description": description.strip(),
            }
        else:
            surface_kind = item.get("surface_kind")
            surface_key = item.get("surface_key")
            test_family = item.get("test_family")
            applicability_basis = item.get("applicability_basis")
            completion_criteria = item.get("completion_criteria")
            if not isinstance(surface_kind, str) or surface_kind not in COVERAGE_SLOT_KINDS:
                raise PhaseContractError(f"{field}[{index}].surface_kind is invalid")
            if kind == "coverage":
                # 仅容忍模型对完整 structured Cell 使用的泛化标签；写回前统一为权威 surface_kind。
                kind = surface_kind
            elif not isinstance(kind, str) or kind != surface_kind:
                raise PhaseContractError(f"{field}[{index}].kind must equal surface_kind")
            if not isinstance(surface_key, str) or not surface_key.strip():
                raise PhaseContractError(f"{field}[{index}].surface_key is required")
            if not isinstance(test_family, str):
                raise PhaseContractError(f"{field}[{index}].test_family is required")
            test_family = test_family.strip().lower().replace("-", "_").replace(" ", "_")
            if test_family not in DEFAULT_COPY_TEST_FAMILIES:
                raise PhaseContractError(f"{field}[{index}].test_family is invalid")
            if not isinstance(applicability_basis, str) or not applicability_basis.strip():
                raise PhaseContractError(f"{field}[{index}].applicability_basis is required")
            if not isinstance(completion_criteria, str) or not completion_criteria.strip():
                raise PhaseContractError(f"{field}[{index}].completion_criteria is required")
            identity = ("structured", surface_kind, surface_key.strip(), test_family)
            normalized = {
                "kind": kind,
                "key": key.strip() if isinstance(key, str) else "",
                "description": description.strip(),
                "surface_key": surface_key.strip(),
                "surface_kind": surface_kind,
                "test_family": test_family,
                "applicability_basis": applicability_basis.strip(),
                "completion_criteria": completion_criteria.strip(),
            }
        if identity in seen_identities:
            continue
        seen_identities.add(identity)
        slots.append(normalized)
    return slots


def extract_coverage_slots(payload: dict[str, Any], *, field: str = "slots") -> list[dict[str, Any]]:
    # 从 worker 协议 payload 解包并提取 coverage slots（pentest 场景登记攻击面/新线索）。
    # payload 已通过对应 validate_*_payload 校验，此处幂等再校验一次并返回规整列表。
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if payload.get("accepted") is True else payload
    if not isinstance(data, dict):
        return []
    return _validate_coverage_slots(data, field=field)


def _normalize_surface_trait_value(value: object) -> str:
    """容忍常见 JSON scalar，同时拒绝会丢失结构语义的复合值。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return str(value)
    raise PhaseContractError("surface trait value must be a JSON scalar")


def _validate_discovered_surfaces(data: dict[str, Any]) -> list[dict[str, Any]]:
    """校验 Surface 级发现；Cell 适用性只作为服务端派生输入。"""
    raw = data.get("discovered_surfaces")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PhaseContractError("discovered_surfaces must be an array")
    surfaces: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise PhaseContractError(f"discovered_surfaces[{index}] must be an object")
        unexpected = set(item) - {
            "surface_kind",
            "surface_key",
            "description",
            "traits",
            "applicable_tests",
            "evidence_refs",
        }
        if unexpected:
            raise PhaseContractError(f"discovered_surfaces[{index}] has unexpected keys")
        surface_kind = item.get("surface_kind")
        surface_key = item.get("surface_key")
        if not isinstance(surface_kind, str) or surface_kind not in COVERAGE_SLOT_KINDS:
            raise PhaseContractError(f"discovered_surfaces[{index}].surface_kind is invalid")
        if not isinstance(surface_key, str) or not surface_key.strip():
            raise PhaseContractError(f"discovered_surfaces[{index}].surface_key is required")
        description = item.get("description", "")
        if not isinstance(description, str):
            raise PhaseContractError(f"discovered_surfaces[{index}].description must be a string")
        traits = item.get("traits", {})
        if not isinstance(traits, dict):
            raise PhaseContractError(f"discovered_surfaces[{index}].traits must be an object")
        normalized_traits: dict[str, str] = {}
        for key, value in traits.items():
            if not isinstance(key, str) or not key.strip():
                raise PhaseContractError(
                    f"discovered_surfaces[{index}].traits must contain string keys"
                )
            try:
                normalized_traits[key.strip()] = _normalize_surface_trait_value(value)
            except ValueError as exc:
                raise PhaseContractError(
                    f"discovered_surfaces[{index}].traits must contain scalar values"
                ) from exc
        raw_tests = item.get("applicable_tests", [])
        if not isinstance(raw_tests, list):
            raise PhaseContractError(
                f"discovered_surfaces[{index}].applicable_tests must be an array"
            )
        applicable_tests: list[dict[str, Any]] = []
        seen_families: set[str] = set()
        for test_index, test in enumerate(raw_tests):
            if not isinstance(test, dict):
                raise PhaseContractError(
                    f"discovered_surfaces[{index}].applicable_tests[{test_index}] must be an object"
                )
            if set(test) - {
                "test_family",
                "description",
                "applicability_basis",
                "completion_criteria",
            }:
                raise PhaseContractError(
                    f"discovered_surfaces[{index}].applicable_tests[{test_index}] has unexpected keys"
                )
            family = test.get("test_family")
            family = (
                family.strip().lower().replace("-", "_").replace(" ", "_")
                if isinstance(family, str)
                else ""
            )
            if family not in DEFAULT_COPY_TEST_FAMILIES:
                raise PhaseContractError(
                    f"discovered_surfaces[{index}].applicable_tests[{test_index}].test_family is invalid"
                )
            basis = test.get("applicability_basis")
            criteria = test.get("completion_criteria")
            if not isinstance(basis, str) or not basis.strip():
                raise PhaseContractError(
                    f"discovered_surfaces[{index}].applicable_tests[{test_index}].applicability_basis is required"
                )
            if not isinstance(criteria, str) or not criteria.strip():
                raise PhaseContractError(
                    f"discovered_surfaces[{index}].applicable_tests[{test_index}].completion_criteria is required"
                )
            if family in seen_families:
                continue
            seen_families.add(family)
            test_description = test.get("description", "")
            applicable_tests.append(
                {
                    "test_family": family,
                    "description": (
                        test_description.strip()
                        if isinstance(test_description, str)
                        else ""
                    ),
                    "applicability_basis": basis.strip(),
                    "completion_criteria": criteria.strip(),
                }
            )
        evidence_refs = item.get("evidence_refs", [])
        if not isinstance(evidence_refs, list):
            raise PhaseContractError(f"discovered_surfaces[{index}].evidence_refs must be an array")
        normalized_evidence = _parse_evidence_refs(
            {"evidence_refs": evidence_refs},
            path=f"discovered_surfaces[{index}]",
        )
        identity = (surface_kind, surface_key.strip())
        if identity in seen:
            continue
        seen.add(identity)
        surfaces.append(
            {
                "surface_kind": surface_kind,
                "surface_key": surface_key.strip(),
                "description": description.strip(),
                "traits": normalized_traits,
                "applicable_tests": applicable_tests,
                "evidence_refs": [ref.model_dump(exclude_none=True) for ref in normalized_evidence],
            }
        )
    return surfaces


def extract_discovered_surfaces(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if payload.get("accepted") is True else payload
    if not isinstance(data, dict):
        return []
    return _validate_discovered_surfaces(data)


def _validate_coverage_scope_disposition(
    data: dict[str, Any],
) -> dict[str, Any] | None:
    raw = data.get("coverage_scope_disposition")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PhaseContractError("coverage_scope_disposition must be an object")
    extra_keys = set(raw) - {
        "disposition_kind",
        "target_ids",
        "reason",
        "evidence_refs",
    }
    if extra_keys:
        raise PhaseContractError("coverage_scope_disposition has unexpected keys")
    disposition_kind = raw.get("disposition_kind")
    if disposition_kind not in {"no_in_scope_targets", "no_reachable_surfaces"}:
        raise PhaseContractError("coverage_scope_disposition.disposition_kind is invalid")
    target_ids = raw.get("target_ids")
    if not isinstance(target_ids, list):
        raise PhaseContractError("coverage_scope_disposition.target_ids must be an array")
    normalized_target_ids: list[str] = []
    for index, target_id in enumerate(target_ids):
        if not isinstance(target_id, str) or not target_id.strip():
            raise PhaseContractError(
                f"coverage_scope_disposition.target_ids[{index}] must be a non-empty string"
            )
        normalized_target_ids.append(target_id.strip())
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise PhaseContractError("coverage_scope_disposition.reason is required")
    refs = _parse_evidence_refs(raw, path="coverage_scope_disposition")
    if not refs:
        raise PhaseContractError("coverage_scope_disposition.evidence_refs must not be empty")
    return {
        "disposition_kind": disposition_kind,
        "target_ids": sorted(set(normalized_target_ids)),
        "reason": reason.strip(),
        "evidence_refs": [ref.model_dump(exclude_none=True) for ref in refs],
    }


def extract_coverage_scope_disposition(
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if payload.get("accepted") is True else payload
    if not isinstance(data, dict):
        return None
    return _validate_coverage_scope_disposition(data)


def _validate_coverage_result(
    data: dict[str, Any],
    *,
    strict_contract: bool = False,
    outcome_contract_version: int = 1,
    blocker_cooldown: bool = False,
) -> dict[str, Any] | None:
    raw = data.get("coverage_result")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PhaseContractError("coverage_result must be an object")
    if strict_contract:
        allowed_keys = {
            "slot_key",
            "surface_key",
            "test_family",
            "outcome",
            "baseline",
            "variants",
            "evidence_refs",
            "blocker_code",
            "strategy_key",
            "limitation_reason",
            "limitation_details",
            "attempted_strategies",
        }
        unexpected = sorted(set(raw) - allowed_keys)
        if unexpected:
            raise PhaseContractError(
                f"coverage_result has unexpected keys: {', '.join(unexpected)}",
                public_reason="coverage_result has unexpected keys",
            )
    required = ("slot_key", "surface_key", "test_family", "outcome")
    normalized: dict[str, Any] = {}
    for key in required:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise PhaseContractError(f"coverage_result.{key} is required")
        normalized[key] = value.strip()
    normalized["test_family"] = (
        normalized["test_family"].lower().replace("-", "_").replace(" ", "_")
    )
    normalized["outcome"] = (
        normalized["outcome"].lower().replace("-", "_").replace(" ", "_")
    )
    if normalized["test_family"] not in DEFAULT_COPY_TEST_FAMILIES:
        raise PhaseContractError("coverage_result.test_family is invalid")
    if normalized["outcome"] not in DEFAULT_COPY_COVERAGE_OUTCOMES:
        raise PhaseContractError("coverage_result.outcome is invalid")
    for key in (
        "baseline",
        "blocker_code",
        "strategy_key",
        "limitation_reason",
        "limitation_details",
    ):
        value = raw.get(key, "")
        normalized[key] = value.strip() if isinstance(value, str) else ""
    normalized["strategy_key"] = normalized["strategy_key"].lower()
    if normalized["strategy_key"] and fullmatch(
        r"[a-z0-9][a-z0-9._:-]{0,127}", normalized["strategy_key"]
    ) is None:
        raise PhaseContractError("coverage_result.strategy_key is invalid")
    for key in ("variants", "attempted_strategies"):
        values = raw.get(key, [])
        if not isinstance(values, list):
            raise PhaseContractError(f"coverage_result.{key} must be an array")
        if strict_contract and any(
            not isinstance(item, str) or not item.strip() for item in values
        ):
            raise PhaseContractError(
                f"coverage_result.{key} must contain non-empty strings"
            )
        normalized[key] = [
            text for item in values if (text := str(item).strip())
        ]
    normalized["evidence_refs"] = _parse_evidence_refs(raw, path="coverage_result")
    if strict_contract:
        outcome = normalized["outcome"]
        if outcome == "tested_no_finding":
            if not normalized["baseline"] or not normalized["variants"]:
                raise PhaseContractError(
                    "tested_no_finding requires baseline and variants"
                )
            if outcome_contract_version >= 2 and not normalized["evidence_refs"]:
                raise PhaseContractError("tested_no_finding requires evidence_refs")
        elif outcome == "finding" and outcome_contract_version >= 2:
            if not normalized["baseline"] or not normalized["variants"]:
                raise PhaseContractError("finding requires baseline and variants")
            if not normalized["evidence_refs"]:
                raise PhaseContractError("finding requires evidence_refs")
        elif outcome == "temporarily_blocked":
            if not normalized["limitation_reason"]:
                raise PhaseContractError(
                    "temporarily_blocked requires limitation_reason"
                )
            if outcome_contract_version >= 2:
                for key in ("blocker_code", "limitation_details"):
                    if not normalized[key]:
                        raise PhaseContractError(f"temporarily_blocked requires {key}")
                if not normalized["attempted_strategies"]:
                    raise PhaseContractError(
                        "temporarily_blocked requires attempted_strategies"
                    )
            if blocker_cooldown and not normalized["strategy_key"]:
                raise PhaseContractError("temporarily_blocked requires strategy_key")
    return normalized


def extract_coverage_result(payload: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if payload.get("accepted") is True else payload
    if not isinstance(data, dict):
        return None
    return _validate_coverage_result(data)


def _validate_goal_driven_assessment_record(
    data: dict[str, Any],
) -> ExperimentRecord:
    """按结果语义校验唯一实验记录，不强迫限制类结果伪造对照。"""
    if "coverage_result" in data:
        raise PhaseContractError("coverage_result is not supported by goal-driven-v1")
    raw = data.get("assessment_record")
    if not isinstance(raw, dict):
        raise PhaseContractError("assessment_record is required")
    record = ExperimentRecord.model_validate(raw)
    outcome = record.outcome
    if outcome not in GOAL_DRIVEN_ASSESSMENT_OUTCOMES:
        raise PhaseContractError("assessment_record.outcome is required")
    if not record.evidence_refs:
        raise PhaseContractError(
            f"{outcome} requires assessment_record evidence_refs"
        )
    if outcome in {"finding", "tested_no_finding"}:
        if not record.baseline.strip() or not record.variants:
            raise PhaseContractError(
                f"{outcome} requires assessment_record baseline and variants"
            )
        if not record.observations:
            raise PhaseContractError(
                f"{outcome} requires assessment_record observations"
            )
    elif outcome == "temporarily_blocked":
        for key, value in (
            ("blocker_code", record.blocker_code),
            ("limitation_reason", record.limitation_reason),
            ("limitation_details", record.limitation_details),
        ):
            if not value.strip():
                raise PhaseContractError(
                    f"temporarily_blocked requires assessment_record {key}"
                )
        if not record.attempted_strategies:
            raise PhaseContractError(
                "temporarily_blocked requires assessment_record attempted_strategies"
            )
    elif outcome in {
        "permanently_unreachable",
        "out_of_scope",
        "not_applicable",
    }:
        if not record.limitation_reason.strip() or not record.limitation_details.strip():
            raise PhaseContractError(
                f"{outcome} requires assessment_record limitation_reason and limitation_details"
            )
        if outcome == "permanently_unreachable" and not record.attempted_strategies:
            raise PhaseContractError(
                "permanently_unreachable requires assessment_record attempted_strategies"
            )
    elif not record.observations and not record.limitations:
        raise PhaseContractError(
            "observation requires assessment_record observations or limitations"
        )
    return record


def _validate_vulnerability_identity(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PhaseContractError("vulnerability.identity must be an object")
    vulnerability_class = value.get("vulnerability_class")
    if not isinstance(vulnerability_class, str) or not vulnerability_class.strip():
        raise PhaseContractError("vulnerability.identity.vulnerability_class is required")
    asset = value.get("asset")
    entrypoint = value.get("entrypoint")
    vector = value.get("vector", {})
    if not isinstance(asset, dict):
        raise PhaseContractError("vulnerability.identity.asset must be an object")
    if not isinstance(entrypoint, dict):
        raise PhaseContractError("vulnerability.identity.entrypoint must be an object")
    if not isinstance(vector, dict):
        raise PhaseContractError("vulnerability.identity.vector must be an object")
    host = asset.get("host")
    if not isinstance(host, str) or not host.strip():
        raise PhaseContractError("vulnerability.identity.asset.host is required")
    port = asset.get("port")
    if port is not None and (
        not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65535
    ):
        raise PhaseContractError("vulnerability.identity.asset.port must be 1-65535")
    route = entrypoint.get("route", "")
    locus = entrypoint.get("locus", "")
    if not isinstance(route, str) or not isinstance(locus, str):
        raise PhaseContractError("vulnerability identity route/locus must be strings")
    if not route.strip() and not locus.strip():
        raise PhaseContractError("vulnerability identity requires route or locus")

    def optional_text(node: dict[str, Any], key: str) -> str:
        raw = node.get(key, "")
        if not isinstance(raw, str):
            raise PhaseContractError(f"vulnerability.identity.{key} must be a string")
        return raw.strip()

    return {
        "vulnerability_class": vulnerability_class.strip(),
        "asset": {
            "protocol": optional_text(asset, "protocol"),
            "host": host.strip(),
            "port": port,
        },
        "entrypoint": {
            "method": optional_text(entrypoint, "method"),
            "route": route.strip(),
            "locus": locus.strip(),
        },
        "vector": {
            key: optional_text(vector, key)
            for key in (
                "parameter",
                "object_type",
                "source_role",
                "target_role",
                "state",
                "variant",
            )
        },
    }


def _validate_vulnerability(fact: dict[str, Any]) -> dict[str, Any] | None:
    # 校验 fact.vulnerability 子字段（所有场景通用，worker 声明漏洞时自动确认）。
    # fact 无 vulnerability 字段返回 None（向后兼容）；有则校验结构并返回规整 dict。
    vuln = fact.get("vulnerability")
    if vuln is None:
        return None
    if not isinstance(vuln, dict):
        raise PhaseContractError("vulnerability must be an object")
    title = vuln.get("title")
    if not isinstance(title, str) or not title.strip():
        raise PhaseContractError("vulnerability.title is required")
    severity = vuln.get("severity")
    if not isinstance(severity, str) or severity.strip().lower() not in VULNERABILITY_SEVERITIES:
        raise PhaseContractError(f"vulnerability.severity must be one of {sorted(VULNERABILITY_SEVERITIES)}")
    packet_artifact_handle = vuln.get("packet_artifact_handle")
    if packet_artifact_handle is not None:
        packet_artifact_handle = _validate_artifact_handle(
            packet_artifact_handle,
            path="vulnerability.packet_artifact_handle",
        )
    if "packet_path" in vuln:
        raise PhaseContractError(
            "vulnerability.packet_path is not supported; use packet_artifact_handle"
        )
    confidence = vuln.get("confidence", 0.5)
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise PhaseContractError("vulnerability.confidence must be a number 0-1")
    return {
        "title": title.strip(),
        "severity": severity.strip().lower(),
        "vuln_type": (vuln.get("vuln_type") or "").strip() if isinstance(vuln.get("vuln_type"), str) else "",
        "target": (vuln.get("target") or "").strip() if isinstance(vuln.get("target"), str) else "",
        "packet_artifact_handle": (
            packet_artifact_handle.strip()
            if isinstance(packet_artifact_handle, str)
            else None
        ),
        "remediation": vuln.get("remediation") or "" if isinstance(vuln.get("remediation"), str) else "",
        "confidence": max(0.0, min(1.0, float(confidence))),
        # repro_request/repro_response: worker 内联返回的原始 HTTP 请求/响应包（固定格式文本），
        # 不依赖文件 artifact；空字符串=未提供。仅 trim 尾部空白，保留内部行结构。
        "repro_request": vuln.get("repro_request", "").rstrip() if isinstance(vuln.get("repro_request"), str) else "",
        "repro_response": vuln.get("repro_response", "").rstrip() if isinstance(vuln.get("repro_response"), str) else "",
        "identity": _validate_vulnerability_identity(vuln.get("identity")),
    }


def extract_vulnerability(payload: dict[str, Any]) -> dict[str, Any] | None:
    # 从 worker 协议 payload 解包并提取 fact.vulnerability（自动确认用）。
    # payload 已通过 validate_*_payload 校验，此处幂等再校验一次。
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if payload.get("accepted") is True else payload
    if not isinstance(data, dict):
        return None
    fact = data.get("fact")
    if not isinstance(fact, dict):
        return None
    return _validate_vulnerability(fact)


def _looks_like_reason_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload)
    if keys == {"complete"}:
        complete = payload["complete"]
        return isinstance(complete, dict) and "from" in complete and "description" in complete
    if keys == {"intents"}:
        return isinstance(payload["intents"], list)
    if keys == {"intent"}:
        intent = payload["intent"]
        return isinstance(intent, dict) and "from" in intent and "description" in intent
    if keys in ({"discovered_slots"}, {"discovered_surfaces"}):
        return isinstance(payload[next(iter(keys))], list)
    if keys == {"intents", "artifacts"}:
        return isinstance(payload["intents"], list) and isinstance(payload["artifacts"], list)
    if keys == {"complete", "artifacts"}:
        complete = payload["complete"]
        return isinstance(complete, dict) and "from" in complete and "description" in complete and isinstance(payload["artifacts"], list)
    if keys == {"intent", "artifacts"}:
        intent = payload["intent"]
        return isinstance(intent, dict) and "from" in intent and "description" in intent and isinstance(payload["artifacts"], list)
    return False


def _looks_like_bootstrap_execute_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload)
    if not {"fact", "complete"}.issubset(keys) or not keys.issubset(
        {
            "fact",
            "complete",
            "artifacts",
            "slots",
            "discovered_surfaces",
            "coverage_scope_disposition",
        }
    ):
        return False
    artifacts = payload.get("artifacts")
    if artifacts is not None and not isinstance(artifacts, list):
        return False
    return _is_dict(payload.get("fact")) and _is_dict(payload.get("complete"))


def _looks_like_bootstrap_conclude_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload)
    if "fact" not in keys or not keys.issubset(
        {
            "fact",
            "complete",
            "artifacts",
            "slots",
            "discovered_surfaces",
            "coverage_scope_disposition",
        }
    ):
        return False
    artifacts = payload.get("artifacts")
    if artifacts is not None and not isinstance(artifacts, list):
        return False
    return _is_dict(payload.get("fact"))


def _looks_like_explore_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    allowed = {
        "description",
        "fact",
        "artifacts",
        "evidence_refs",
        "discovered_slots",
        "discovered_surfaces",
        "coverage_result",
    }
    if not set(payload).issubset(allowed):
        return False
    if "fact" in payload and not _is_dict(payload.get("fact")):
        return False
    if "artifacts" in payload and not isinstance(payload.get("artifacts"), list):
        return False
    if "evidence_refs" in payload and not isinstance(payload.get("evidence_refs"), list):
        return False
    return "description" in payload or "fact" in payload


def validate_reason_payload(
    payload: dict[str, Any],
    open_intents_empty: bool,
    max_intents: int,
    *,
    surface_manifest: bool = False,
    structured_intent_binding: dict[str, Any] | None = None,
    goal_driven: bool = False,
) -> tuple[str, dict[str, Any] | list[dict[str, Any]] | None, list[str]]:
    accepted, data = _unwrap_result_envelope(
        payload,
        allowed_kinds={"reason_result"},
    )
    if accepted is False:
        return "rejected", None, []
    if not isinstance(data, dict):
        raise PhaseContractError("accepted must be true or false")
    structured_cells = None
    if structured_intent_binding is not None:
        contract = StructuredIntentBinding.model_validate(
            structured_intent_binding
        )
        if contract.assessment_phase not in {"mapping", "baseline"}:
            raise PhaseContractError(
                "structured intent binding phase is invalid"
            )
        structured_cells = {
            cell.slot_key: cell for cell in contract.eligible_cells
        }
    allowed_fields = {
        "abandon",
        "artifacts",
        "blocked_by",
        "complete",
        "confidence",
        "depth_review",
        "depends_on_targets",
        "discovered_slots",
        "discovered_surfaces",
        "evidence_refs",
        "intents",
        "rationale",
        "rejected_options",
        "slots",
    }
    unexpected_fields = sorted(set(data) - allowed_fields)
    if unexpected_fields:
        raise PhaseContractError(
            f"unexpected reason data fields: {', '.join(unexpected_fields)}",
            public_reason="unexpected reason data fields",
        )
    _validate_artifact_declarations(data)
    _validate_result_evidence_refs(data)
    _validate_reason_rejected_options(data)
    _validate_reason_decision_metadata(data)
    abandon = _validate_abandon_ids(data)
    if data.get("depth_review") is not None:
        if abandon or any(data.get(field) for field in ("intents", "complete", "discovered_slots", "discovered_surfaces")):
            raise PhaseContractError("depth_review must be an atomic standalone decision")
        decision = DepthReviewDecision.model_validate(data["depth_review"])
        return "depth_review", decision.model_dump(by_alias=True, exclude_none=True), []
    if data.get("complete") is not None and abandon:
        raise PhaseContractError("completion cannot discard obligations in the same decision")
    if "slots" in data:
        raise PhaseContractError(
            "reason data.slots is invalid; use data.discovered_slots or data.discovered_surfaces"
        )
    if "discovered_slots" in data and "discovered_surfaces" in data:
        raise PhaseContractError("discovered_slots and discovered_surfaces cannot coexist")
    if surface_manifest and "discovered_slots" in data:
        raise PhaseContractError("reason data.discovered_slots is invalid in Surface Manifest mode")
    if not surface_manifest and "discovered_surfaces" in data:
        raise PhaseContractError("reason data.discovered_surfaces is invalid in legacy mode")
    # 新 Surface/Cell 必须先登记，不能在同轮让模型猜测其派生 slot_key。
    discovered_slots = _validate_coverage_slots(data, field="discovered_slots")
    discovered_surfaces = _validate_discovered_surfaces(data)
    structured_discovery = any(slot.get("surface_kind") for slot in discovered_slots)
    registration_only = structured_discovery or bool(discovered_surfaces)
    complete = data.get("complete")
    intents = data.get("intents")
    if registration_only and not goal_driven:
        if complete is not None or intents is not None:
            raise PhaseContractError("surface discovery must be registration-only")
        return "noop", None, abandon
    if goal_driven and discovered_surfaces and complete is not None:
        raise PhaseContractError("completion cannot add discovered surfaces")
    if complete is not None:
        if intents is not None:
            raise PhaseContractError("complete and intents cannot coexist")
        if not isinstance(complete, dict) or "from" not in complete or "description" not in complete:
            raise PhaseContractError("invalid complete payload")
        return "complete", complete, abandon
    if intents is not None:
        if not isinstance(intents, list):
            raise PhaseContractError("intents must be an array")
        seen_structured_slots: set[str] = set()
        for i, intent in enumerate(intents):
            if not isinstance(intent, dict) or "from" not in intent or "description" not in intent:
                raise PhaseContractError(f"invalid intent at index {i}")
            priority = intent.get("priority", 1)
            if type(priority) is not int or priority not in {0, 1, 2}:
                raise PhaseContractError(f"intent[{i}].priority must be 0, 1, or 2")
            intent["priority"] = priority
            # depends_on: 可选，可以是 @N 占位符（引用同批次第 N 个）或现有 intent id。
            depends_on = intent.get("depends_on")
            if depends_on is not None:
                if not isinstance(depends_on, str) or not depends_on.strip():
                    raise PhaseContractError(f"intent[{i}].depends_on must be a non-empty string")
            # legacy 项目仍允许无 Cell Intent；普通结构化 Reason 必须选择冻结合同内的唯一 Cell。
            slot_key = intent.get("slot_key")
            if slot_key is not None:
                if not isinstance(slot_key, str) or not slot_key.strip():
                    raise PhaseContractError(f"intent[{i}].slot_key must be a non-empty string")
                slot_key = slot_key.strip()
                intent["slot_key"] = slot_key
            strategy_key = intent.get("strategy_key")
            normalized_strategy = None
            if strategy_key is not None:
                normalized_strategy = (
                    strategy_key.strip().lower()
                    if isinstance(strategy_key, str)
                    else ""
                )
                if not normalized_strategy or fullmatch(
                    r"[a-z0-9][a-z0-9._:-]{0,127}", normalized_strategy
                ) is None:
                    raise PhaseContractError(f"intent[{i}].strategy_key is invalid")
                intent["strategy_key"] = normalized_strategy
            if structured_cells is not None:
                if slot_key is None or normalized_strategy is None:
                    raise PhaseContractError(
                        f"intent[{i}] requires slot_key and strategy_key"
                    )
                cell = structured_cells.get(slot_key)
                if cell is None:
                    raise PhaseContractError(
                        f"intent[{i}].slot_key is outside the structured binding"
                    )
                if slot_key in seen_structured_slots:
                    raise PhaseContractError(
                        f"intent[{i}].slot_key must be unique within the batch"
                    )
                seen_structured_slots.add(slot_key)
                if (
                    cell.strategy.mode == "must_differ"
                    and normalized_strategy
                    == cell.strategy.excluded_strategy_key
                ):
                    raise PhaseContractError(
                        f"intent[{i}].strategy_key violates the Cell constraint"
                    )
        if (
            not intents
            and open_intents_empty
            and not abandon
            and (structured_cells is None or structured_cells)
        ):
            raise PhaseContractError("intents must not be empty when open_intents is empty")
        # 超出本轮预算属于确定性契约错误，不能静默丢弃 Worker 决策。
        if len(intents) > max_intents:
            raise PhaseContractError(f"intents must contain at most {max_intents} items")
        if not intents and not abandon:
            return "noop", None, []
        return "intents", intents, abandon
    if (
        open_intents_empty
        and not abandon
        and (structured_cells is None or structured_cells)
    ):
        raise PhaseContractError("intents is required when open_intents is empty")
    return "noop", None, abandon


def _validate_abandon_ids(data: dict[str, Any]) -> list[str]:
    raw = data.get("abandon")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PhaseContractError("abandon must be an array of intent ids")
    ids: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise PhaseContractError(f"abandon[{index}] must be a non-empty intent id")
        ids.append(item.strip())
    return ids


def validate_bootstrap_execute_payload(
    payload: dict[str, Any],
    *,
    require_surface_resolution: bool = False,
    goal_driven: bool = False,
) -> tuple[str, dict[str, Any] | None]:
    accepted, data = _unwrap_result_envelope(
        payload,
        allowed_kinds={"bootstrap_result"},
    )
    if accepted is False:
        return "rejected", None
    if not isinstance(data, dict):
        raise PhaseContractError("accepted must be true or false")
    allowed_fields = {
        "artifacts",
        "complete",
        "coverage_scope_disposition",
        "discovered_surfaces",
        "evidence_refs",
        "fact",
        "slots",
    }
    unexpected_fields = sorted(set(data) - allowed_fields)
    if unexpected_fields:
        raise PhaseContractError(
            f"unexpected bootstrap data fields: {', '.join(unexpected_fields)}",
            public_reason="unexpected bootstrap data fields",
        )
    _validate_artifact_declarations(data)
    _validate_result_evidence_refs(data)

    fact = data.get("fact")
    if not isinstance(fact, dict):
        raise PhaseContractError("fact is required")
    # fact 可选带 vulnerability 子字段（自动确认），校验结构。
    _validate_vulnerability(fact)
    fact_description = fact.get("description")
    if not isinstance(fact_description, str) or not fact_description.strip():
        raise PhaseContractError("fact.description is required")

    result = {"fact_description": fact_description.strip()}
    complete = data.get("complete")
    if goal_driven:
        if complete is not None:
            raise PhaseContractError("complete is not supported by goal-driven bootstrap")
    else:
        if complete is None:
            raise PhaseContractError("complete is required")
        if not isinstance(complete, dict):
            raise PhaseContractError("complete must be an object")
        complete_description = complete.get("description")
        if not isinstance(complete_description, str) or not complete_description.strip():
            raise PhaseContractError("complete.description is required")
        result["complete_description"] = complete_description.strip()
    if "slots" in data and "discovered_surfaces" in data:
        raise PhaseContractError("slots and discovered_surfaces cannot coexist")
    if "coverage_scope_disposition" in data and (
        bool(data.get("slots")) or bool(data.get("discovered_surfaces"))
    ):
        raise PhaseContractError(
            "coverage_scope_disposition cannot coexist with discovered surfaces"
        )
    # legacy 场景登记 slots；Manifest v1 由任务层选择 discovered_surfaces。
    result["slots"] = _validate_coverage_slots(data)
    result["discovered_surfaces"] = _validate_discovered_surfaces(data)
    result["coverage_scope_disposition"] = _validate_coverage_scope_disposition(data)
    if require_surface_resolution and not (
        result["discovered_surfaces"] or result["coverage_scope_disposition"]
    ):
        raise PhaseContractError(
            "minimum coverage bootstrap requires discovered_surfaces or coverage_scope_disposition"
        )
    return ("fact" if goal_driven else "complete"), result


def validate_bootstrap_conclude_payload(
    payload: dict[str, Any],
    *,
    require_surface_resolution: bool = False,
) -> tuple[str, str | None]:
    accepted, data = _unwrap_result_envelope(
        payload,
        allowed_kinds={"bootstrap_conclude_result"},
    )
    if accepted is False:
        return "rejected", None
    if not isinstance(data, dict):
        raise PhaseContractError("accepted must be true or false")
    _validate_artifact_declarations(data)
    _validate_result_evidence_refs(data)
    extra_keys = set(data) - {
        "fact",
        "complete",
        "artifacts",
        "slots",
        "discovered_surfaces",
        "coverage_scope_disposition",
    }
    if extra_keys:
        raise PhaseContractError("unexpected keys in conclude payload")
    if "slots" in data and "discovered_surfaces" in data:
        raise PhaseContractError("slots and discovered_surfaces cannot coexist")
    if "coverage_scope_disposition" in data and (
        bool(data.get("slots")) or bool(data.get("discovered_surfaces"))
    ):
        raise PhaseContractError(
            "coverage_scope_disposition cannot coexist with discovered surfaces"
        )
    # fallback 可携带 legacy slots、Manifest v1 Surface 或证据化零 Surface 处置。
    _validate_coverage_slots(data)
    discovered_surfaces = _validate_discovered_surfaces(data)
    scope_disposition = _validate_coverage_scope_disposition(data)
    if require_surface_resolution and not (
        discovered_surfaces or scope_disposition
    ):
        raise PhaseContractError(
            "minimum coverage bootstrap requires discovered_surfaces or coverage_scope_disposition"
        )
    fact = data.get("fact")
    if not isinstance(fact, dict):
        raise PhaseContractError("fact is required")
    _validate_vulnerability(fact)
    fact_description = fact.get("description")
    if not isinstance(fact_description, str) or not fact_description.strip():
        raise PhaseContractError("fact.description is required")
    return "fact", fact_description.strip()


def validate_verifier_payload(
    payload: dict[str, Any],
    *,
    target_observation_id: str,
    expected_kind: str | None = None,
) -> tuple[str, str | None]:
    accepted, data = _unwrap_result_envelope(
        payload,
        allowed_kinds=(
            {expected_kind}
            if expected_kind is not None
            else {"verifier_result", "verifier_conclude_result"}
        ),
    )
    if accepted is False:
        return "rejected", None
    if not isinstance(data, dict):
        raise PhaseContractError("accepted must be true or false")
    _validate_artifact_declarations(data)
    extra_keys = set(data) - {"description", "artifacts", "verification_result"}
    if extra_keys:
        raise PhaseContractError("unexpected keys in verifier payload")
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise PhaseContractError("description is required")
    result = data.get("verification_result")
    if not isinstance(result, dict):
        raise PhaseContractError("verification_result is required")
    result_extra = set(result) - {
        "observation_id",
        "outcome",
        "evidence_refs",
        "notes",
    }
    if result_extra:
        raise PhaseContractError("unexpected keys in verification_result")
    observation_id = result.get("observation_id")
    if not isinstance(observation_id, str) or observation_id.strip() != target_observation_id:
        raise PhaseContractError("verification observation_id does not match target")
    outcome = result.get("outcome")
    if not isinstance(outcome, str) or outcome.strip() not in VERIFIER_OUTCOMES:
        raise PhaseContractError("verification_result.outcome is invalid")
    refs = _parse_evidence_refs(result, path="verification_result")
    if not refs:
        raise PhaseContractError("verification_result.evidence_refs is required")
    notes = result.get("notes", "")
    if not isinstance(notes, str):
        raise PhaseContractError("verification_result.notes must be a string")
    return "fact", description.strip()


def validate_explore_payload(
    payload: dict[str, Any],
    *,
    require_coverage_result: bool = False,
    require_vulnerability_identity: bool = False,
    surface_manifest: bool | None = None,
    strict_contract: bool = False,
    expected_kind: str | None = None,
    outcome_contract_version: int = 1,
    blocker_cooldown: bool = False,
    goal_driven: bool = False,
) -> tuple[str, str | None]:
    if strict_contract and payload.get("accepted") is True:
        if "artifacts" in payload:
            raise PhaseContractError("artifacts must be nested under data")
        unexpected = sorted(
            set(payload) - {"protocol", "accepted", "kind", "data", "reason"}
        )
        if unexpected:
            raise PhaseContractError(
                f"unexpected top-level result keys: {', '.join(unexpected)}",
                public_reason="unexpected top-level result keys",
            )
        kind = payload.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            raise PhaseContractError("kind must be a non-empty string")
        if expected_kind is not None and kind != expected_kind:
            raise PhaseContractError(
                f"unexpected result kind: {kind}",
                public_reason="unexpected result kind",
            )
    allowed_kinds = (
        {expected_kind}
        if expected_kind is not None
        else {"explore_result", "explore_conclude_result"}
    )
    accepted, data = _unwrap_result_envelope(
        payload,
        allowed_kinds=allowed_kinds,
    )
    if accepted is False:
        return "rejected", None
    if not isinstance(data, dict):
        raise PhaseContractError("accepted must be true or false")
    _validate_artifact_declarations(data)
    _validate_result_evidence_refs(data)
    if "discovered_slots" in data and "discovered_surfaces" in data:
        raise PhaseContractError("discovered_slots and discovered_surfaces cannot coexist")
    if surface_manifest is True and "discovered_slots" in data:
        raise PhaseContractError("discovered_slots is invalid when Surface Manifest is enabled")
    if surface_manifest is False and "discovered_surfaces" in data:
        raise PhaseContractError("discovered_surfaces is invalid when Surface Manifest is disabled")
    # explore 可提交相邻 Surface 或 legacy Cell，并同时结束当前 Cell。
    _validate_coverage_slots(data, field="discovered_slots")
    _validate_discovered_surfaces(data)
    assessment_record = (
        _validate_goal_driven_assessment_record(data)
        if goal_driven
        else None
    )
    coverage_result = None
    if not goal_driven:
        coverage_result = _validate_coverage_result(
            data,
            strict_contract=strict_contract,
            outcome_contract_version=outcome_contract_version,
            blocker_cooldown=blocker_cooldown,
        )
        if require_coverage_result and coverage_result is None:
            raise PhaseContractError("coverage_result is required")
    extra_keys = set(data) - {
        "description",
        "fact",
        "artifacts",
        "evidence_refs",
        "discovered_slots",
        "discovered_surfaces",
        "coverage_result",
        "assessment_record",
    }
    if not goal_driven and data.get("assessment_record") is not None:
        ExperimentRecord.model_validate(data["assessment_record"])
    if extra_keys:
        # explore 只能产出一个 fact，复杂分叉应回到 reason 阶段创建新 intent。
        raise PhaseContractError("unexpected keys in explore payload")
    fact = data.get("fact")
    if fact is not None and not isinstance(fact, dict):
        raise PhaseContractError("fact must be an object")
    if isinstance(fact, dict):
        has_vulnerability = fact.get("vulnerability") is not None
        outcome = (
            assessment_record.outcome
            if assessment_record is not None
            else coverage_result.get("outcome") if coverage_result else None
        )
        if strict_contract and outcome != "finding" and has_vulnerability:
            subject = "assessment" if goal_driven else "coverage"
            raise PhaseContractError(
                f"non-finding {subject} outcome must not include vulnerability"
            )
        vulnerability = _validate_vulnerability(fact) if has_vulnerability else None
        if strict_contract and outcome == "finding" and vulnerability is None:
            raise PhaseContractError("finding requires vulnerability")
        if (
            (require_vulnerability_identity or strict_contract)
            and vulnerability is not None
            and vulnerability.get("identity") is None
        ):
            raise PhaseContractError("vulnerability identity is required")
    description = fact.get("description") if isinstance(fact, dict) else data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise PhaseContractError("description is required")
    return "fact", description.strip()


def validate_report_payload(payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """报告只生成解释与修复建议，不能更新 canonical 原始证据。"""
    accepted, data = _unwrap_result_envelope(payload, allowed_kinds={"report_result"})
    if accepted is False:
        return "rejected", None
    if not isinstance(data, dict):
        raise PhaseContractError("report data must be an object")
    validated = validate_phase_data("report_execute", data)
    ids = [item["vulnerability_id"] for item in validated["findings"]]
    if len(ids) != len(set(ids)):
        raise PhaseContractError("report contains duplicate vulnerability IDs")
    _validate_artifact_declarations(data)
    return "report", validated
