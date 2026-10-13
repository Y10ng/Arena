"""Reason prompt 上下文构造器。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from cairn.assessment_contracts import (
    GOAL_DRIVEN_PROTOCOL,
    goal_driven_decision_input_digest,
)
from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.prompting import (
    format_coverage_slots,
    format_fact_ids,
    format_open_intents,
    format_structured_coverage_slots,
    format_targets,
    knowledge_base_prompt_context,
)
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.tasks.common import write_graph_snapshot_reference
from cairn.dispatcher.tasks.surface_policy import surface_discovery_contract
from cairn.server.models import ProjectDetail
from cairn.server.services.structured_intent_bindings import (
    parse_structured_intent_binding,
)
from cairn.dispatcher.worker_result_gateway import WorkerRequestError

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class ReasonPromptContext:
    open_intents: list[dict[str, object]]
    allowed_fact_ids: list[str]
    coverage_slots: list[dict[str, object]]
    replacements: dict[str, str]
    binding: dict[str, object]


def goal_driven_reason_execution(project: ProjectDetail) -> bool:
    """只识别冻结到项目上的 goal-driven 执行能力。"""
    execution = getattr(project.project, "assessment_execution", None)
    return isinstance(execution, dict) and (
        execution.get("goal_driven") is True
        or getattr(project.project, "assessment_protocol", None)
        == GOAL_DRIVEN_PROTOCOL
    )


def _authorized_targets(project: ProjectDetail) -> list[dict[str, object]]:
    return [
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


def _goal_text(project: ProjectDetail) -> str:
    return next(
        (
            getattr(fact, "description", "")
            for fact in project.facts
            if fact.id == "goal"
        ),
        "",
    )


def ordinary_structured_reason_execution(project: ProjectDetail) -> bool:
    execution = getattr(project.project, "assessment_execution", None)
    return (
        not goal_driven_reason_execution(project)
        and isinstance(execution, dict)
        and execution.get("structured") is True
        and execution.get("phase") in {"mapping", "baseline"}
    )


def validate_reason_admission_context(
    project: ProjectDetail,
    admission_context: dict[str, object] | None,
) -> dict[str, object] | None:
    """普通结构化 Reason 必须消费 admission 原样签发的冻结 Cell 集合。"""
    if not ordinary_structured_reason_execution(project):
        return None
    if not isinstance(admission_context, dict):
        raise WorkerRequestError("binding.structured_intent_binding")
    try:
        binding = parse_structured_intent_binding(
            admission_context.get("structured_intent_binding")
        )
    except HTTPException as exc:
        raise WorkerRequestError("binding.structured_intent_binding") from exc
    execution = project.project.assessment_execution
    if binding.assessment_phase != execution.get("phase"):
        raise WorkerRequestError("binding.structured_intent_binding")
    return binding.model_dump(mode="json", exclude_none=True)


def build_reason_prompt_context(
    config: DispatchConfig,
    container_manager: ContainerManager,
    container_name: str,
    project: ProjectDetail,
    export_yaml: str,
    *,
    knowledge: str | None = None,
    advisory: Any | None = None,
    client: CairnClient | None = None,
    admission_context: dict[str, object] | None = None,
) -> ReasonPromptContext:
    # 只消费 Server 解析后的冻结执行合同；历史数据可读，但不能推断或升级为新执行。
    execution = getattr(project.project, "assessment_execution", None)
    if (
        not isinstance(execution, dict)
        or execution.get("version") != 2
        or execution.get("executable") is not True
        or execution.get("execution_contract_version") != 1
        or execution.get("execution_engine") != "pi"
    ):
        raise WorkerRequestError("assessment_execution")
    goal_driven = goal_driven_reason_execution(project)
    # 仅把开放 Intent 和非 goal fact 暴露给 Reason，避免模型重复完成已关闭分支。
    open_intents = [
        {
            "id": intent.id,
            "from": intent.from_,
            "description": intent.description,
            "worker": intent.worker,
            "assessment_role": intent.assessment_role,
            "priority": getattr(intent, "priority", 1),
            "target_observation_id": intent.target_observation_id,
            "slot_key": intent.slot_key,
            "strategy_key": intent.strategy_key,
            "depth_review_fact_id": intent.depth_review_fact_id,
            "depends_on": intent.depends_on,
        }
        for intent in project.intents
        if intent.to is None and intent.concluded_at is None
    ]
    allowed_fact_ids = [fact.id for fact in project.facts if fact.id != "goal"]
    max_intents = config.tasks.reason.max_intents
    review_digest = execution.get("review_input_digest")
    if review_digest is not None or (
        not goal_driven and execution.get("phase") == "depth_review"
    ):
        if (
            not isinstance(review_digest, str)
            or re.fullmatch(r"[a-f0-9]{64}", review_digest) is None
        ):
            raise WorkerRequestError("binding.review_input_digest")
    if not goal_driven and execution.get("phase") == "depth_review":
        max_intents = min(max_intents, 3)
    structured_intent_binding = validate_reason_admission_context(
        project, admission_context
    )
    binding: dict[str, object] = {
        "max_intents": max_intents,
        "allowed_fact_ids": allowed_fact_ids,
    }
    if structured_intent_binding is not None:
        binding["structured_intent_binding"] = structured_intent_binding
    # optional binding 无值时省略；领域快照中的 null 保持原样。
    if review_digest is not None:
        binding["review_input_digest"] = review_digest
    if goal_driven:
        binding["allowed_intent_ids"] = [intent.id for intent in project.intents]
        binding["decision_input_digest"] = goal_driven_decision_input_digest(
            project
        )
        assessment_context = dict(execution)
    else:
        assessment_context = {
            **execution,
            "selected_depth_obligations": [
                intent for intent in open_intents if intent["depth_review_fact_id"]
            ],
        }
    # 普通结构化调度只消费 admission 快照；其他阶段的索引仍用于展示而非授权。
    coverage_slots: list[dict[str, object]] = []
    verifier_enabled = execution.get("independent_verifier_enabled") is True
    if structured_intent_binding is not None:
        coverage_slots = list(structured_intent_binding["eligible_cells"])
    elif not goal_driven and client is not None:
        resp = client.list_coverage_slots(project.project.id)
        if resp.ok and isinstance(resp.data, list):
            coverage_slots = resp.data
        elif not resp.ok:
            LOG.warning(
                "list coverage slots failed project=%s status=%s body=%s",
                project.project.id,
                resp.status_code,
                resp.text,
            )
    # graph_yaml 写入容器文件并在 prompt 中引用，避免大型图谱挤占命令行参数长度。
    if goal_driven:
        coverage_context = (
            "Goal-driven scheduling does not use Coverage Cells; consume Facts, "
            "Intents, evidence, execution stop time, and authorized Targets."
        )
    elif structured_intent_binding is not None:
        coverage_context = (
            "Ordinary scheduling eligibility is defined only by the "
            "Server-issued Structured Intent Binding."
        )
    else:
        coverage_context = (
            format_structured_coverage_slots(coverage_slots)
            if execution.get("structured") is True
            else format_coverage_slots(coverage_slots, verifier=verifier_enabled)
        )
    if not goal_driven and execution.get("phase") == "depth_review":
        # 深测可选择 terminal Cell 的补充实验，不能只给待调度基线而隐藏已测前提。
        coverage_context += "\nDepth review Cell index (read graph evidence on demand):\n" + json.dumps(
            [
                {
                    key: slot.get(key)
                    for key in (
                        "slot_key", "surface_kind", "surface_key", "test_family",
                        "status", "result", "resulting_fact_id", "skip_reason",
                        "blocker_strategy_key", "blocker_cooldown_until",
                    )
                }
                for slot in coverage_slots
            ],
            ensure_ascii=False,
        )
    authorized_targets = _authorized_targets(project)
    decision_context = {
        "attempt_statistics": {
            "explore_attempts_started": getattr(
                project.project,
                "explore_attempts_started",
                0,
            ),
            "verification_attempts_started": getattr(
                project.project,
                "verification_attempts_started",
                0,
            ),
        },
        "execution_cutoff": getattr(project.project, "explore_deadline", None),
        "authorization_expires_at": getattr(
            project.project,
            "authorization_expires_at",
            None,
        ),
        "concurrency": {
            "open_intents": len(open_intents),
            "claimed_open_intents": sum(
                1 for intent in open_intents if intent["worker"] is not None
            ),
            "unclaimed_open_intents": sum(
                1 for intent in open_intents if intent["worker"] is None
            ),
        },
    }
    replacements = {
        "goal": _goal_text(project),
        "authorized_targets": format_targets(authorized_targets),
        "decision_context": json.dumps(
            decision_context,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "decision_input_digest": str(binding.get("decision_input_digest", "")),
        "assessment_execution": json.dumps(assessment_context, ensure_ascii=False),
        "structured_intent_binding": json.dumps(
            structured_intent_binding,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "graph_yaml": write_graph_snapshot_reference(
            container_manager,
            container_name,
            export_yaml.strip(),
            phase="reason_execute",
        ),
        "fact_ids": format_fact_ids(allowed_fact_ids),
        "open_intents": format_open_intents(open_intents),
        "max_intents": str(max_intents),
        "coverage_slots": coverage_context,
        "surface_discovery_contract": surface_discovery_contract(
            phase="reason",
            enabled=execution.get("surface_manifest_enabled") is True,
            allow_reason_intents=goal_driven,
        ),
        "knowledge_base": knowledge_base_prompt_context(
            knowledge,
            advisory=advisory,
        ),
    }
    return ReasonPromptContext(
        open_intents=open_intents,
        allowed_fact_ids=allowed_fact_ids,
        coverage_slots=coverage_slots,
        replacements=replacements,
        binding=binding,
    )
