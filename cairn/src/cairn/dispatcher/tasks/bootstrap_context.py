"""Bootstrap prompt 上下文构造器。"""

from __future__ import annotations

import json
import logging
from typing import Any

from cairn.dispatcher.prompting import (
    authorization_scope_replacements,
    format_hints,
    format_targets,
    knowledge_base_prompt_context,
)
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.tasks.surface_policy import (
    coverage_scope_disposition_contract,
    surface_discovery_contract,
)
from cairn.server.models import ProjectDetail


LOG = logging.getLogger(__name__)
_MAX_RETRY_EVENTS = 6
_MAX_RETRY_MATERIALS = 8


def _bounded_text(value: object, *, limit: int = 256) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:limit] if text else None


def _bounded_digest(value: object) -> str | None:
    text = _bounded_text(value, limit=64)
    if text is None or len(text) != 64 or any(char not in "0123456789abcdef" for char in text.lower()):
        return None
    return text.lower()


def _api_list(response: object) -> list[dict[str, Any]]:
    if getattr(response, "ok", False) is not True:
        return []
    data = getattr(response, "data", None)
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _previous_bootstrap_failure(
    runs: list[dict[str, Any]],
    *,
    task_run_id: str,
    intent_id: str,
    execution_generation: int,
) -> dict[str, Any] | None:
    for run in reversed(runs):
        if (
            run.get("id") == task_run_id
            or run.get("task_type") != "bootstrap"
            or run.get("intent_id") != intent_id
            or run.get("execution_generation") != execution_generation
            or run.get("status") not in {"failed", "timed_out"}
            or run.get("error_type") == "cancelled"
            or run.get("pause_requested") is True
        ):
            continue
        return run
    return None


def _group_events(groups: list[dict[str, Any]], task_run_id: str) -> list[dict[str, Any]]:
    for group in groups:
        if group.get("task_run_id") == task_run_id and isinstance(group.get("events"), list):
            return [item for item in group["events"] if isinstance(item, dict)]
    return []


def _command_diagnostic(events: list[dict[str, Any]]) -> dict[str, object] | None:
    for event in reversed(events):
        if event.get("kind") != "command_finished" or event.get("phase") != "bootstrap_execute":
            continue
        diagnostic: dict[str, object] = {}
        for key in ("status", "returncode", "exit_code", "timed_out", "cancelled", "oom_killed", "duration_seconds"):
            value = event.get(key)
            if isinstance(value, str):
                text = _bounded_text(value)
                if text is not None:
                    diagnostic[key] = text
            elif isinstance(value, int | float | bool):
                diagnostic[key] = value
        return diagnostic or None
    return None


def _tool_diagnostic(events: list[dict[str, Any]]) -> dict[str, object] | None:
    active: dict[str, str] = {}
    recent: list[dict[str, str]] = []
    for event in events:
        operation_id = _bounded_text(event.get("operation_id"))
        tool_name = _bounded_text(event.get("tool_name"), limit=64)
        event_name = _bounded_text(event.get("hook_event_name"), limit=64)
        status = _bounded_text(event.get("status"), limit=32)
        if operation_id and tool_name:
            if event_name == "tool_execution_start":
                active[operation_id] = tool_name
            elif event_name == "tool_execution_end":
                active.pop(operation_id, None)
        if tool_name and status:
            item = {"tool_name": tool_name, "status": status}
            recorded_at = _bounded_text(event.get("recorded_at"), limit=64)
            if recorded_at:
                item["recorded_at"] = recorded_at
            recent.append(item)
    diagnostic: dict[str, object] = {}
    if active:
        diagnostic["last_unfinished_tool"] = next(reversed(active.values()))
    if recent:
        diagnostic["recent_events"] = recent[-_MAX_RETRY_EVENTS:]
    return diagnostic or None


def _runtime_materials(runtime_context: object) -> dict[str, object] | None:
    if not isinstance(runtime_context, dict):
        return None
    project_resources = runtime_context.get("project_resources")
    if not isinstance(project_resources, dict):
        return None
    materials: dict[str, object] = {"reference_only": True}
    bundle_digest = _bounded_digest(project_resources.get("bundle_digest"))
    manifest_path = _bounded_text(project_resources.get("manifest_path"), limit=512)
    if bundle_digest:
        materials["bundle_digest"] = bundle_digest
    if manifest_path:
        materials["manifest_path"] = manifest_path
    resources: list[dict[str, object]] = []
    raw_resources = project_resources.get("resources")
    if isinstance(raw_resources, list):
        for item in raw_resources:
            if not isinstance(item, dict) or item.get("source") != "project_resource":
                continue
            resource: dict[str, object] = {}
            for key in ("id", "kind", "path", "container_path"):
                value = _bounded_text(item.get(key), limit=512)
                if value:
                    resource[key] = value
            size = item.get("size_bytes")
            if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
                resource["size_bytes"] = size
            digest = _bounded_digest(item.get("sha256"))
            if digest:
                resource["sha256"] = digest
            if resource:
                resources.append(resource)
            if len(resources) >= _MAX_RETRY_MATERIALS:
                break
    if resources:
        materials["resources"] = resources
    return materials if len(materials) > 1 else None


def _artifact_materials(artifacts: list[dict[str, Any]]) -> list[dict[str, object]]:
    materials: list[dict[str, object]] = []
    for item in artifacts[-_MAX_RETRY_MATERIALS:]:
        material: dict[str, object] = {"reference_only": True}
        for source, target, limit in (
            ("id", "artifact_id", 256),
            ("kind", "kind", 64),
            ("category", "category", 64),
            ("uri", "uri", 512),
        ):
            value = _bounded_text(item.get(source), limit=limit)
            if value:
                material[target] = value
        size = item.get("size")
        if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
            material["size_bytes"] = size
        digest = _bounded_digest(item.get("sha256"))
        if digest:
            material["sha256"] = digest
        if len(material) > 1:
            materials.append(material)
    return materials


def bootstrap_retry_prompt_context(
    client: CairnClient,
    project_id: str,
    task_run_id: str,
    intent_id: str,
    execution_generation: int,
) -> str:
    """为 fresh retry 注入上一轮有界诊断，不把历史材料升级为当前证据。"""
    try:
        runs = _api_list(client.list_task_runs(project_id))
    except Exception as exc:
        LOG.warning(
            "bootstrap retry history unavailable project=%s task_run=%s error=%s",
            project_id,
            task_run_id,
            type(exc).__name__,
        )
        return ""
    previous = _previous_bootstrap_failure(
        runs,
        task_run_id=task_run_id,
        intent_id=intent_id,
        execution_generation=execution_generation,
    )
    if previous is None:
        return ""
    previous_id = _bounded_text(previous.get("id"))
    if previous_id is None:
        return ""

    command_events: list[dict[str, Any]] = []
    tool_events: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    for label, reader in (
        ("command events", lambda: client.list_task_run_command_events(project_id, [previous_id])),
        ("tool events", lambda: client.list_task_run_hook_events(project_id, [previous_id])),
        ("artifacts", lambda: client.list_artifacts(project_id, task_run_id=previous_id)),
    ):
        try:
            values = _api_list(reader())
        except Exception as exc:
            LOG.warning(
                "bootstrap retry %s unavailable project=%s source_task_run=%s error=%s",
                label,
                project_id,
                previous_id,
                type(exc).__name__,
            )
            continue
        if label == "command events":
            command_events = _group_events(values, previous_id)
        elif label == "tool events":
            tool_events = _group_events(values, previous_id)
        else:
            artifacts = values

    diagnostic: dict[str, object] = {
        "source_task_run_id": previous_id,
        "status": _bounded_text(previous.get("status"), limit=32),
        "error_type": _bounded_text(previous.get("error_type"), limit=64),
        "exit_code": previous.get("exit_code") if isinstance(previous.get("exit_code"), int) else None,
        "started_at": _bounded_text(previous.get("started_at"), limit=64),
        "ended_at": _bounded_text(previous.get("ended_at"), limit=64),
    }
    worker_failure = previous.get("runtime_context", {}).get("worker_failure") if isinstance(previous.get("runtime_context"), dict) else None
    if isinstance(worker_failure, dict):
        failure: dict[str, object] = {}
        for key in ("origin", "code", "phase", "error_type", "path"):
            value = _bounded_text(worker_failure.get(key), limit=256)
            if value:
                failure[key] = value
        if isinstance(worker_failure.get("exit_code"), int):
            failure["exit_code"] = worker_failure["exit_code"]
        if failure:
            diagnostic["worker_failure"] = failure
    command = _command_diagnostic(command_events)
    if command:
        diagnostic["worker_command"] = command
    tools = _tool_diagnostic(tool_events)
    if tools:
        diagnostic["tools"] = tools
    runtime_materials = _runtime_materials(previous.get("runtime_context"))
    if runtime_materials:
        diagnostic["project_resources"] = runtime_materials
    artifact_materials = _artifact_materials(artifacts)
    if artifact_materials:
        diagnostic["prior_artifacts"] = artifact_materials
    diagnostic = {key: value for key, value in diagnostic.items() if value is not None}

    return (
        "## Previous bounded Bootstrap attempt\n"
        "This is advisory diagnostic context for a finite fresh retry. Do not repeat the same "
        "blocked method unchanged. Historical Artifact IDs, URIs, and resource entries are an index "
        "only; they are not current evidence handles and must not be cited unless the underlying "
        "file is available now and is registered in this run.\n"
        "```json\n"
        f"{json.dumps(diagnostic, ensure_ascii=False, indent=2)}\n"
        "```\n"
        "Use the failure and tool state to choose a bounded alternative or record the limitation. "
        "Keep Bootstrap at the minimum understanding needed for Reason; defer deep analysis of large "
        "minified or very long single-line scripts to a later Reason/Explore decision."
    )


def bootstrap_prompt_replacements(
    project: ProjectDetail,
    *,
    knowledge: str | None = None,
    advisory: Any | None = None,
) -> dict[str, str]:
    # 将项目初始上下文规范化为纯字符串替换项，避免 prompt 模板直接依赖 Pydantic 结构。
    facts = {fact.id: fact.description for fact in project.facts}
    hints = [
        {
            "id": hint.id,
            "content": hint.content,
            "creator": hint.creator,
            "fact_id": hint.fact_id,
            "intent_id": hint.intent_id,
            "created_at": hint.created_at,
        }
        for hint in project.hints
    ]
    targets = [
        {
            "id": target.id,
            "value": target.value,
            "type": target.type,
            "status": target.status,
            "source_fact_id": target.source_fact_id,
            "discovered_by_run_id": target.discovered_by_run_id,
            "created_at": target.created_at,
        }
        for target in project.targets
    ]
    # 范围来源与原文由共用投影提供，Target 索引不覆盖 Origin 授权。
    return {
        **authorization_scope_replacements(project),
        "goal": facts.get("goal", ""),
        "hints": format_hints(hints),
        "targets": format_targets(targets),
        "surface_discovery_contract": surface_discovery_contract(
            project, phase="bootstrap"
        ),
        "coverage_scope_disposition_contract": (
            coverage_scope_disposition_contract(project)
        ),
        "knowledge_base": knowledge_base_prompt_context(
            knowledge,
            advisory=advisory,
        ),
    }
