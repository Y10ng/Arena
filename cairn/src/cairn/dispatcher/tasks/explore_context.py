"""Explore prompt 上下文构造器。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from cairn.assessment_contracts import DepthSelection
from cairn.dispatcher.prompting import format_targets, knowledge_base_prompt_context
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.tasks.common import write_graph_snapshot_reference
from cairn.dispatcher.tasks.surface_policy import surface_discovery_contract
from cairn.server.models import Fact, Intent, ProjectDetail, VulnerabilityVerifierTarget
from cairn.server.services.structured_intent_bindings import (
    parse_structured_explore_binding,
)


@dataclass(frozen=True, slots=True)
class BoundCoverageCell:
    """default-copy Explore 使用的权威 Coverage Cell 快照。"""

    slot_key: str
    surface_kind: str
    surface_key: str
    test_family: str
    applicability_basis: str
    completion_criteria: str

    def prompt_json(self) -> str:
        return json.dumps(
            {
                "slot_key": self.slot_key,
                "surface_kind": self.surface_kind,
                "surface_key": self.surface_key,
                "test_family": self.test_family,
                "applicability_basis": self.applicability_basis,
                "completion_criteria": self.completion_criteria,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def bind_result_identity(
        self,
        coverage_result: dict[str, object],
        *,
        strategy_key: str = "",
    ) -> list[str]:
        """以调度绑定覆盖模型回显的路由字段，并返回不一致字段供审计。"""
        authoritative = {
            "slot_key": self.slot_key,
            "surface_key": self.surface_key,
            "test_family": self.test_family,
        }
        if strategy_key:
            authoritative["strategy_key"] = strategy_key
        mismatched = [
            key
            for key, value in authoritative.items()
            if str(coverage_result.get(key) or "").strip() != value
        ]
        coverage_result.update(authoritative)
        return mismatched


def resolve_admitted_coverage_cell(
    admission_context: dict[str, object] | None,
    intent: Intent,
) -> BoundCoverageCell:
    """从 Server admission 快照恢复 Cell，不再按当前列表重新推导权限。"""
    if not isinstance(admission_context, dict):
        raise ValueError("structured explore admission context is missing")
    binding = parse_structured_explore_binding(
        admission_context.get("structured_explore_binding")
    )
    if binding.intent_id != intent.id:
        raise ValueError("structured explore binding intent does not match")
    if binding.cell.slot_key != str(intent.slot_key or "").strip():
        raise ValueError("structured explore binding slot does not match")
    if binding.strategy_key != str(intent.strategy_key or "").strip().lower():
        raise ValueError("structured explore binding strategy does not match")
    cell = binding.cell
    return BoundCoverageCell(
        slot_key=cell.slot_key,
        surface_kind=cell.surface_kind,
        surface_key=cell.surface_key,
        test_family=cell.test_family,
        applicability_basis=cell.applicability_basis,
        completion_criteria=cell.completion_criteria,
    )


def resolve_bound_coverage_cell(
    client: CairnClient,
    project_id: str,
    intent: Intent,
) -> BoundCoverageCell:
    """从 Server 读取并验证当前 Intent 唯一绑定的结构化 Cell。"""
    slot_key = str(intent.slot_key or "").strip()
    if not slot_key:
        raise ValueError("default-copy intent is missing slot_key")
    response = client.list_coverage_slots(project_id)
    if not response.ok:
        raise RuntimeError(
            f"coverage cell lookup failed with status {response.status_code}"
        )
    if not isinstance(response.data, list):
        raise ValueError("coverage cell lookup returned an invalid payload")
    matches = [
        item
        for item in response.data
        if isinstance(item, dict) and str(item.get("slot_key") or "").strip() == slot_key
    ]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one bound coverage cell for {slot_key}")
    item = matches[0]
    if (
        str(item.get("assigned_intent_id") or "").strip() != intent.id
        and not intent.depth_review_fact_id
    ):
        raise ValueError("coverage cell is not assigned to the current intent")
    # 深测由 Server 原子选题绑定既有 Cell，不改写基线 assigned_intent_id。
    surface_key = str(item.get("surface_key") or "").strip()
    test_family = str(item.get("test_family") or "").strip()
    if not surface_key or not test_family:
        raise ValueError("coverage cell identity is incomplete")
    return BoundCoverageCell(
        slot_key=slot_key,
        surface_kind=str(item.get("surface_kind") or item.get("kind") or "").strip(),
        surface_key=surface_key,
        test_family=test_family,
        applicability_basis=str(item.get("applicability_basis") or "").strip(),
        completion_criteria=str(item.get("completion_criteria") or "").strip(),
    )


def verifier_prompt_replacements(
    intent: Intent,
    target: VulnerabilityVerifierTarget,
    *,
    knowledge: str | None = None,
    advisory: Any | None = None,
) -> dict[str, str]:
    """只给复核会话中性复现输入，不泄露发现者的定级和完整论证。"""
    projection = target.neutral_projection
    if (
        not isinstance(projection, dict) or projection.get("version") != 1
        or projection.get("observation_id") != intent.target_observation_id
    ):
        raise ValueError("verifier neutral projection is missing or mismatched")
    allowed = {
        "version", "observation_id", "vulnerability_id", "identity", "preconditions",
        "baseline", "variants", "controls", "repro_request", "repro_response", "evidence_refs",
        "discovery_task_run_id", "discovery_session_id",
    }
    neutral = {key: value for key, value in projection.items() if key in allowed}
    return {
        "intent_id": intent.id,
        "intent_description": "独立验证指定 Observation；先检查前提，执行最小复现和正常对照，按实际证据提交结果。",
        "target_observation_id": intent.target_observation_id,
        "verifier_target": json.dumps(
            neutral,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "knowledge_base": knowledge_base_prompt_context(
            knowledge,
            advisory=advisory,
        ),
    }


def selected_experiment_context(intent: Intent, facts: list[Fact]) -> str:
    """只投影已接受选题中本 Intent 的实验，缺失时不退化为自由探索。"""
    if not intent.depth_review_fact_id:
        return intent.description
    matches = [fact for fact in facts if fact.id == intent.depth_review_fact_id]
    if len(matches) != 1 or intent.depth_review_fact_id not in intent.from_:
        raise ValueError("selected depth review fact is missing or unbound")
    record = matches[0].assessment_record
    if not isinstance(record, dict) or record.get("version") != 1 or record.get("kind") != "depth_review":
        raise ValueError("selected depth review record is unsupported")
    selections = record.get("selected")
    if not isinstance(selections, list):
        raise ValueError("selected depth review experiments are missing")
    selected = [
        item for item in selections if isinstance(item, dict)
        and item.get("slot_key") == intent.slot_key
        and item.get("strategy_key") == intent.strategy_key
    ]
    if len(selected) != 1:
        raise ValueError("selected depth experiment is missing or ambiguous")
    experiment = DepthSelection.model_validate(selected[0])
    if not set(experiment.from_).issubset(intent.from_):
        raise ValueError("selected depth experiment source facts do not match")
    if any(ref.artifact_handle is not None for ref in experiment.evidence_refs):
        raise ValueError("selected depth experiment evidence was not persisted")
    return (
        intent.description + "\n\nServer-accepted deep experiment (execute this hypothesis with its controls; "
        "report unavailable preconditions as blocked, not a negative result):\n"
        + json.dumps(experiment.model_dump(by_alias=True, exclude_none=True), ensure_ascii=False, sort_keys=True)
    )


def explore_prompt_replacements(
    container_manager: ContainerManager,
    container_name: str,
    export_yaml: str,
    intent: Intent,
    *,
    phase: str,
    knowledge: str | None = None,
    advisory: Any | None = None,
    coverage_cell: BoundCoverageCell | None = None,
    surface_manifest: bool = False,
    facts: list[Fact] | None = None,
    project: ProjectDetail | None = None,
) -> dict[str, str]:
    # 图谱快照写入容器文件后再引用，避免超长 YAML 直接塞进命令参数。
    # Explore prompt 只绑定当前 intent，协议校验会阻止 worker 写到其他分支。
    goal = ""
    authorized_targets: list[dict[str, object]] = []
    if project is not None:
        goal = next(
            (fact.description for fact in project.facts if fact.id == "goal"),
            "",
        )
        authorized_targets = [
            {
                "id": target.id,
                "value": target.value,
                "type": target.type,
                "status": target.status,
                "source_fact_id": target.source_fact_id,
            }
            for target in sorted(
                project.targets,
                key=lambda item: (item.created_at, item.id),
            )
            if target.status in {"seed", "approved"}
        ]
    replacements = {
        "goal": goal,
        "authorized_targets": format_targets(authorized_targets),
        "graph_yaml": write_graph_snapshot_reference(
            container_manager,
            container_name,
            export_yaml.strip(),
            phase=phase,
        ),
        "intent_id": intent.id,
        "intent_description": selected_experiment_context(intent, facts or []),
        "intent_strategy_key": intent.strategy_key,
        "surface_discovery_contract": surface_discovery_contract(
            phase="explore", enabled=surface_manifest
        ),
        "knowledge_base": knowledge_base_prompt_context(
            knowledge,
            advisory=advisory,
        ),
    }
    if coverage_cell is not None:
        replacements["coverage_cell"] = coverage_cell.prompt_json()
    return replacements
