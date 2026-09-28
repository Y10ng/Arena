"""基于项目冻结场景快照解析手动报告能力。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from typing import Any, Mapping

from cairn.dispatcher.prompt_resources import DEFAULT_PROMPT_GROUP, validate_report_prompt_resources
from cairn.dispatcher.prompting import load_prompt

REPORT_POLICY_VERSION = 1
REPORT_PACKET_VERSION = 2
REPORT_OUTPUT_CONTRACT_VERSION = 2
REPORT_COMPLETENESS_RULE_VERSION = 1
REPORT_TEMPLATE_PROFILE = "assessment-report-packet-v1"
DEFAULT_REPORT_PROMPT_GROUP = DEFAULT_PROMPT_GROUP


@dataclass(frozen=True)
class ReportPolicy:
    supported: bool = False
    version: int = 0
    report_prompt_group: str = ""
    packet_version: int = 0
    output_contract_version: int = 0
    completeness_rule_version: int = 0
    template_profile: str = ""
    execution_profile_id: str = ""
    knowledge_key: str | None = None
    source: str = ""
    reason: str | None = None


@dataclass(frozen=True)
class ReportExecutionPlan:
    version: int
    execution_contract_version: int
    execution_engine: str
    report_policy_version: int
    report_prompt_group: str
    report_prompt_name: str
    report_prompt_digest_sha256: str
    packet_version: int
    output_contract_version: int
    completeness_rule_version: int
    template_profile: str
    execution_profile_id: str
    knowledge_key: str | None
    source: str

    def canonical_json(self) -> str:
        return json.dumps(
            asdict(self),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def digest_sha256(self) -> str:
        return sha256(self.canonical_json().encode("utf-8")).hexdigest()


def _row_value(row: Mapping[str, Any], key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _unsupported(reason: str) -> ReportPolicy:
    return ReportPolicy(reason=reason)


def _strict_snapshot(row: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = _row_value(row, "scenario_snapshot_json")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        snapshot = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return snapshot if isinstance(snapshot, dict) else None


def _supported_policy(
    snapshot: dict[str, Any],
    *,
    report_prompt_group: str,
    source: str,
) -> ReportPolicy:
    knowledge = snapshot.get("knowledge")
    return ReportPolicy(
        supported=True,
        version=REPORT_POLICY_VERSION,
        report_prompt_group=report_prompt_group,
        packet_version=REPORT_PACKET_VERSION,
        output_contract_version=REPORT_OUTPUT_CONTRACT_VERSION,
        completeness_rule_version=REPORT_COMPLETENESS_RULE_VERSION,
        template_profile=REPORT_TEMPLATE_PROFILE,
        execution_profile_id=str(snapshot["profile_id"]),
        knowledge_key=knowledge if isinstance(knowledge, str) and knowledge else None,
        source=source,
    )


def report_policy_from_project_row(row: Mapping[str, Any]) -> ReportPolicy:
    """仅按冻结快照解析能力；历史缺失或损坏快照不读取实时场景兜底。"""
    snapshot = _strict_snapshot(row)
    if snapshot is None:
        return _unsupported("missing_or_invalid_scenario_snapshot")
    key = snapshot.get("key")
    prompt_group = snapshot.get("prompt_group")
    profile_id = snapshot.get("profile_id")
    features = snapshot.get("features")
    if not all(isinstance(value, str) and value for value in (key, prompt_group, profile_id)):
        return _unsupported("incomplete_scenario_snapshot")
    if not isinstance(features, dict):
        return _unsupported("invalid_scenario_features")

    policy_version = features.get("report_policy_version")
    if policy_version not in (None, "", "0", "1"):
        return _unsupported("unsupported_report_policy_version")
    if policy_version == "1":
        report_prompt_group = features.get("report_prompt_group")
        if not isinstance(report_prompt_group, str) or not report_prompt_group.strip():
            return _unsupported("report_prompt_group_missing")
        report_prompt_group = report_prompt_group.strip()
        return _supported_policy(
            snapshot,
            report_prompt_group=report_prompt_group,
            source="explicit-v1",
        )

    return _unsupported("report_policy_not_enabled")


def freeze_report_execution_plan(
    policy: ReportPolicy,
) -> tuple[ReportExecutionPlan, str]:
    """加载并冻结当前有效 Prompt，后续重试不再受资源变更影响。"""
    if not policy.supported:
        raise ValueError(policy.reason or "report policy is not supported")
    validate_report_prompt_resources(policy.report_prompt_group)
    prompt_markdown = load_prompt(policy.report_prompt_group, "report.md")
    prompt_digest = sha256(prompt_markdown.encode("utf-8")).hexdigest()
    plan = ReportExecutionPlan(
        version=2,
        execution_contract_version=1,
        execution_engine="pi",
        report_policy_version=policy.version,
        report_prompt_group=policy.report_prompt_group,
        report_prompt_name="report.md",
        report_prompt_digest_sha256=prompt_digest,
        packet_version=policy.packet_version,
        output_contract_version=policy.output_contract_version,
        completeness_rule_version=policy.completeness_rule_version,
        template_profile=policy.template_profile,
        execution_profile_id=policy.execution_profile_id,
        knowledge_key=policy.knowledge_key,
        source=policy.source,
    )
    return plan, prompt_markdown


def parse_report_execution_plan(value: str) -> ReportExecutionPlan:
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise ValueError("report execution plan must be an object")
    if loaded.get("version") != 2 or loaded.get("execution_contract_version") != 1 or loaded.get("execution_engine") != "pi" or loaded.get("packet_version") != REPORT_PACKET_VERSION:
        raise ValueError("report_execution_contract_unsupported")
    return ReportExecutionPlan(**loaded)


__all__ = [name for name in globals() if not name.startswith("__")]
