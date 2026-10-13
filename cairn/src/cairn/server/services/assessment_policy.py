"""版本化探索评估协议与项目级快照。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping

from cairn.assessment_contracts import GOAL_DRIVEN_PROTOCOL
from cairn.server.schemas.scenarios import Scenario

DEFAULT_COPY_PROTOCOL = "default-copy-v1"
DEFAULT_COPY_FINGERPRINT_VERSION = 1
DEFAULT_COPY_OUTCOME_CONTRACT_VERSION = 2
DEFAULT_COPY_BLOCKER_COOLDOWN_VERSION = 1
DEFAULT_COPY_BLOCKER_COOLDOWN_SECONDS = 900

@dataclass(frozen=True)
class AssessmentPolicy:
    protocol: str = ""
    fingerprint_version: int | None = None
    outcome_contract_version: int = 1
    blocker_cooldown_version: int = 0
    surface_manifest_version: int = 0
    minimum_coverage_version: int = 0
    quality_gate_version: int = 0
    verifier_contract_version: int = 0
    explore_deadline: str | None = None
    contract_version: int = 0

    @property
    def goal_driven(self) -> bool:
        return self.protocol == GOAL_DRIVEN_PROTOCOL

    @property
    def phased(self) -> bool:
        return self.structured and self.contract_version == 2

    @property
    def structured(self) -> bool:
        return self.protocol == DEFAULT_COPY_PROTOCOL

    @property
    def logical_attempts_enabled(self) -> bool:
        return self.structured or self.goal_driven

    @property
    def vulnerability_identity_enabled(self) -> bool:
        return self.fingerprint_version is not None

    @property
    def blocker_cooldown_enabled(self) -> bool:
        return self.structured and self.blocker_cooldown_version >= 1

    @property
    def surface_manifest_enabled(self) -> bool:
        return self.structured and self.surface_manifest_version >= 1

    @property
    def minimum_coverage_enabled(self) -> bool:
        return (
            self.surface_manifest_enabled
            and self.minimum_coverage_version >= 1
        )

    @property
    def quality_snapshot_enabled(self) -> bool:
        return self.minimum_coverage_enabled and self.quality_gate_version >= 1

    @property
    def independent_verifier_enabled(self) -> bool:
        return (
            self.structured or self.goal_driven
        ) and self.verifier_contract_version >= 1


def frozen_execution_features(scenario: Scenario) -> dict[str, Any]:
    """新项目只冻结目标驱动主线；历史项目继续读取自己的旧快照。"""
    features = dict(scenario.features)
    for key in (
        "blocker_cooldown_version",
        "complete_gate",
        "coverage_gate",
        "failure_conclude",
        "minimum_coverage_version",
        "quality_gate_version",
        "surface_manifest_version",
        "verifier",
        "explore_budget",
        "exploration_limit",
        "verification_reserve",
    ):
        features.pop(key, None)
    features.update(
        execution_contract_version=1,
        execution_engine="pi",
        assessment_protocol=GOAL_DRIVEN_PROTOCOL,
        assessment_contract_version=2,
        vulnerability_fingerprint_version=1,
        outcome_contract_version=2,
        verifier_contract_version=1,
        report_policy_version="1",
        report_prompt_group=features.get("report_prompt_group") or GOAL_DRIVEN_PROTOCOL,
    )
    return features


def scenario_snapshot(scenario: Scenario) -> dict[str, Any]:
    """冻结会影响 Dispatcher 与服务端协议的场景字段。"""
    return {
        "key": scenario.key,
        "label": scenario.label,
        "is_default": scenario.is_default,
        "profile_id": scenario.profile_id,
        "image": scenario.image,
        "prompt_group": scenario.prompt_group,
        "knowledge": scenario.knowledge,
        "cap_add": list(scenario.cap_add),
        "features": frozen_execution_features(scenario),
    }


def _feature_version(features: Mapping[str, Any], key: str, default: int) -> int:
    """仅缺失值使用默认版本；显式 0 表示冻结关闭。"""
    value = features.get(key)
    return default if value is None or value == "" else int(value)


def policy_for_scenario(
    scenario: Scenario,
    *,
    created_at: str,
    explore_deadline: str | None = None,
) -> AssessmentPolicy:
    del scenario, created_at  # 新协议不从场景或创建时间推导次数限制和执行截止。
    return AssessmentPolicy(
        protocol=GOAL_DRIVEN_PROTOCOL,
        fingerprint_version=DEFAULT_COPY_FINGERPRINT_VERSION,
        outcome_contract_version=DEFAULT_COPY_OUTCOME_CONTRACT_VERSION,
        verifier_contract_version=1,
        explore_deadline=explore_deadline,
        contract_version=2,
    )


def execution_rejection_reason(row: Mapping[str, Any]) -> str | None:
    snapshot = parse_scenario_snapshot(row_value(row, "scenario_snapshot_json"))
    features = snapshot.get("features")
    if not isinstance(features, dict):
        return "execution_contract_unsupported"
    if features.get("execution_contract_version") != 1 or features.get("execution_engine") != "pi":
        return "execution_contract_unsupported"
    policy = policy_from_project_row(row)
    if not policy.goal_driven:
        return (
            "assessment_protocol_historical"
            if policy.protocol == DEFAULT_COPY_PROTOCOL
            else "assessment_contract_unsupported"
        )
    if (
        policy.contract_version != 2
        or policy.outcome_contract_version < 2
        or policy.verifier_contract_version < 1
    ):
        return "assessment_contract_unsupported"
    return None


def require_execution_contract(row: Mapping[str, Any]) -> None:
    from fastapi import HTTPException

    reason = execution_rejection_reason(row)
    if reason:
        raise HTTPException(409, reason)


def require_result_settlement_contract(
    row: Mapping[str, Any],
    *,
    task_run_id: str | None,
) -> None:
    """允许历史在途执行结算结果，但绝不据此开放新的执行 admission。"""
    if not policy_from_project_row(row).goal_driven:
        if task_run_id is None:
            from fastapi import HTTPException

            raise HTTPException(409, "historical_result_task_run_required")
        return
    require_execution_contract(row)


def row_value(row: Mapping[str, Any], key: str) -> Any | None:
    """读取加性迁移列；历史测试行缺列时返回空值。"""
    return row[key] if key in row.keys() else None


def policy_from_project_row(row: Mapping[str, Any]) -> AssessmentPolicy:
    try:
        return _policy_from_project_row(row)
    except (TypeError, ValueError, OverflowError):
        # 损坏历史快照仍可列表展示，但负版本绝不能进入执行路径。
        return AssessmentPolicy(protocol=str(row_value(row, "assessment_protocol") or ""), contract_version=-1)


def _policy_from_project_row(row: Mapping[str, Any]) -> AssessmentPolicy:
    keys = set(row.keys())
    protocol = str(row["assessment_protocol"] or "") if "assessment_protocol" in keys else ""
    fingerprint_version = (
        int(row["vulnerability_fingerprint_version"])
        if "vulnerability_fingerprint_version" in keys
        and row["vulnerability_fingerprint_version"] is not None
        else None
    )
    snapshot = parse_scenario_snapshot(
        row["scenario_snapshot_json"] if "scenario_snapshot_json" in keys else None
    )
    features = (
        snapshot.get("features")
        if isinstance(snapshot.get("features"), dict)
        else {}
    )
    deadline = (
        str(row["explore_deadline"])
        if "explore_deadline" in keys and row["explore_deadline"]
        else None
    )
    if protocol == GOAL_DRIVEN_PROTOCOL:
        contract_version = int(features.get("assessment_contract_version") or 0)
        return AssessmentPolicy(
            protocol=protocol,
            fingerprint_version=(
                fingerprint_version
                if fingerprint_version is not None
                else DEFAULT_COPY_FINGERPRINT_VERSION
            ),
            outcome_contract_version=int(
                features.get("outcome_contract_version") or 1
            ),
            verifier_contract_version=int(
                features.get("verifier_contract_version") or 0
            ),
            contract_version=contract_version,
            explore_deadline=deadline,
        )
    if protocol != DEFAULT_COPY_PROTOCOL:
        return AssessmentPolicy(
            protocol=protocol,
            fingerprint_version=fingerprint_version,
            contract_version=int(features.get("assessment_contract_version") or 0),
        )
    return AssessmentPolicy(
        protocol=protocol,
        fingerprint_version=(
            fingerprint_version
            if fingerprint_version is not None
            else DEFAULT_COPY_FINGERPRINT_VERSION
        ),
        outcome_contract_version=int(features.get("outcome_contract_version") or 1),
        blocker_cooldown_version=int(
            features.get("blocker_cooldown_version") or 0
        ),
        surface_manifest_version=int(features.get("surface_manifest_version") or 0),
        minimum_coverage_version=int(features.get("minimum_coverage_version") or 0),
        quality_gate_version=int(features.get("quality_gate_version") or 0),
        verifier_contract_version=int(
            features.get("verifier_contract_version") or 0
        ),
        contract_version=int(features.get("assessment_contract_version") or 0),
        explore_deadline=(
            str(row["explore_deadline"])
            if "explore_deadline" in keys and row["explore_deadline"]
            else None
        ),
    )


def parse_scenario_snapshot(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
