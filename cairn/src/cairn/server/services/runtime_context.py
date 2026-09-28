"""Task run runtime context 归一化服务。"""

from __future__ import annotations

from .common import *

from pathlib import PurePosixPath
from re import fullmatch

from .payload_values import (
    _drop_empty_payload_values,
    duration_payload_value,
    integer_payload_value,
    runtime_context_text,
)

def standard_task_run_error_type(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return "unknown"
    text = value.strip()
    if not text:
        return None
    if text in STANDARD_TASK_RUN_ERROR_TYPES:
        return text
    return "unknown"


def task_run_artifact_root_uri(project_id: str, task_run_id: str) -> str:
    return f"artifact://projects/{project_id}/runs/{task_run_id}"


def normalize_task_run_runtime_context_artifact_root(
    context: dict[str, Any],
    project_id: str,
    task_run_id: str,
) -> dict[str, Any]:
    artifact_root = task_run_artifact_root_uri(project_id, task_run_id)
    normalized = dict(context)
    normalized["artifact_root"] = artifact_root
    execution_environment = normalized.get("execution_environment")
    if isinstance(execution_environment, dict):
        environment = dict(execution_environment)
        container = environment.get("container")
        if isinstance(container, dict):
            environment["container"] = {**container, "artifact_root": artifact_root}
            normalized["execution_environment"] = environment
    return normalized


def raw_task_run_runtime_context(row: sqlite3.Row) -> dict[str, Any]:
    project_id = row["project_id"]
    task_run_id = row["id"]
    error_type = standard_task_run_error_type(row["error_type"])
    context = _loads_json(row["runtime_context_json"])
    # 数据库列是可信来源，覆盖 runtime_context_json 中可能过期或伪造的同名字段。
    context.update(
        {
            "project_id": project_id,
            "task_run_id": task_run_id,
            "intent_id": row["intent_id"],
            "task_type": row["task_type"],
            "worker_name": row["worker_name"],
            "worker_type": row["worker_type"],
            "status": row["status"],
            "session_id": row["session_id"],
            "prompt_group": row["prompt_group"],
            "prompt_version": row["prompt_version"],
            "execution_profile": row["execution_profile"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "exit_code": row["exit_code"],
            "error_type": error_type,
            "error_message": row["error_message"],
            "artifact_root": task_run_artifact_root_uri(project_id, task_run_id),
        }
    )
    return normalize_task_run_runtime_context_artifact_root(context, project_id, task_run_id)


def task_run_runtime_context(row: sqlite3.Row) -> dict[str, Any]:
    return raw_task_run_runtime_context(row)


def runtime_context_audit_keys(context: dict[str, Any]) -> list[str]:
    return sorted(key for key in context if key in RUNTIME_CONTEXT_AUDIT_FIELDS)


def task_run_runtime_context_payload(context: dict[str, Any]) -> dict[str, Any]:
    return sanitize_task_run_runtime_context(context)


def sanitize_task_run_runtime_context(context: dict[str, Any]) -> dict[str, Any]:
    """只清洗 advisory 审计字段，其余调度上下文保持向后兼容。"""
    sanitized = dict(context)
    for key, sanitizer in (
        ("knowledge_base", _runtime_knowledge_base),
        ("project_resources", _runtime_project_resources),
    ):
        if key not in sanitized:
            continue
        value = sanitizer(sanitized.get(key))
        if value is None:
            sanitized.pop(key, None)
        else:
            sanitized[key] = value
    failure = sanitized.get("worker_failure")
    if isinstance(failure, dict):
        from cairn.dispatcher.worker_protocol.submission import (
            PHASE_DATA_MODELS, SUBMISSION_FAILURE_CODES, sanitize_submission_diagnostics,
        )

        code, phase = failure.get("code"), failure.get("phase")
        if failure.get("origin") == "pi_submission" or (isinstance(code, str) and code in SUBMISSION_FAILURE_CODES):
            # healthcheck 也会产生同一组提交诊断，但它不是模型可执行阶段。
            known_result_phase = (
                isinstance(phase, str)
                and (phase == "healthcheck" or phase in PHASE_DATA_MODELS)
            )
            # 持久诊断也重新执行白名单校验，客户端不能借 message/issue 注入业务原文。
            if (isinstance(code, str) and code in SUBMISSION_FAILURE_CODES
                    and known_result_phase):
                sanitized["worker_failure"] = {
                    "origin": "pi_submission", "code": code, "phase": phase,
                    "error_type": standard_task_run_error_type(failure.get("error_type")),
                    "message": f"Pi 执行失败：{code}",
                    **sanitize_submission_diagnostics(failure, phase),
                }
            else:
                sanitized.pop("worker_failure", None)
    return sanitized


def _runtime_scalar_value(value: object) -> str | int | float | bool | None:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return runtime_context_text(value)
    return None


def _merge_legacy_container_runtime_fields(context: dict[str, Any], sanitized: dict[str, Any]) -> None:
    # 兼容早期 control/execution plane 字段，统一合并到当前 container_* 展示字段。
    legacy_fields = {
        "container_image": ("control_image", "execution_image"),
        "image_id": ("control_image_id", "execution_image_id"),
        "image_digest": ("control_image_digest", "execution_image_digest"),
    }
    for current_key, legacy_keys in legacy_fields.items():
        if current_key in sanitized and sanitized[current_key] is not None:
            continue
        for legacy_key in legacy_keys:
            if legacy_key not in context:
                continue
            value = _runtime_scalar_value(context.get(legacy_key))
            if value is not None:
                sanitized[current_key] = value
                break


def _runtime_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return runtime_context_text(text) if text else None


def _runtime_int(value: object) -> int | None:
    return integer_payload_value(value)


def _runtime_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _runtime_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        text = _runtime_text(item)
        if text:
            items.append(text)
    return items


def _runtime_execution_environment(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    container = _runtime_execution_plane(value.get("container"))
    if container is None:
        container = _runtime_legacy_execution_container(value)
    environment = _drop_empty_payload_values(
        {
            "profile": _runtime_allowed_label(value.get("profile"), RUNTIME_EXECUTION_ENVIRONMENT_PROFILES),
            "isolation": _runtime_allowed_label(value.get("isolation"), RUNTIME_EXECUTION_ENVIRONMENT_ISOLATION),
            "container": container,
        }
    )
    return environment or None


def _runtime_legacy_execution_container(value: dict[str, object]) -> dict[str, object] | None:
    control_plane = _runtime_execution_plane(value.get("control_plane"))
    execution_plane = _runtime_execution_plane(value.get("execution_plane"))
    if control_plane is None:
        return execution_plane
    if execution_plane is not None and "artifact_root" not in control_plane and "artifact_root" in execution_plane:
        control_plane = {**control_plane, "artifact_root": execution_plane["artifact_root"]}
    return control_plane


def _runtime_execution_plane(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    plane = _drop_empty_payload_values(
        {
            "container_name": _runtime_text(value.get("container_name")),
            "image": _runtime_text(value.get("image")),
            "network_mode": _runtime_text(value.get("network_mode")),
            "artifact_root": _runtime_artifact_uri(value.get("artifact_root")),
        }
    )
    return plane or None


def _runtime_allowed_label(value: object, allowed: set[str]) -> str | None:
    text = _runtime_text(value)
    if text is None:
        return None
    normalized = text.lower()
    return normalized if normalized in allowed else None


def _runtime_artifact_uri(value: object) -> str | None:
    text = _runtime_text(value)
    if text is None or not text.startswith("artifact://"):
        return None
    return text


def _runtime_target_scope(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    scope: dict[str, object] = {}
    # target_scope 是 dispatcher 的运行快照，按状态分组后供审计页解释执行边界。
    for group in ("active", "discovered", "rejected"):
        targets = value.get(group)
        if not isinstance(targets, list):
            scope[group] = []
            continue
        scope[group] = [_drop_empty_payload_values(_runtime_target(item)) for item in targets if isinstance(item, dict)]
    policy = _runtime_target_scope_policy(value.get("policy"))
    if policy is not None:
        scope["policy"] = policy
    return scope


def _runtime_target(item: dict[str, Any]) -> dict[str, object]:
    return {
        "id": _runtime_text(item.get("id")),
        "value": _runtime_text(item.get("value")),
        "type": _runtime_text(item.get("type")),
        "status": _runtime_text(item.get("status")),
    }


def _runtime_target_scope_policy(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    policy = _drop_empty_payload_values(
        {
            "version": _runtime_target_scope_label(value.get("version"), TARGET_SCOPE_POLICY_VERSIONS),
            "source": _runtime_target_scope_label(value.get("source"), TARGET_SCOPE_POLICY_SOURCES),
            "network_plane": _runtime_allowed_label(value.get("network_plane"), NETWORK_FLOW_NETWORK_PLANES),
            "active_statuses": _runtime_target_scope_statuses(value.get("active_statuses")),
            "review_statuses": _runtime_target_scope_statuses(value.get("review_statuses")),
            "blocked_statuses": _runtime_target_scope_statuses(value.get("blocked_statuses")),
        }
    )
    return policy or None


def _runtime_target_scope_statuses(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    statuses: list[str] = []
    seen: set[str] = set()
    for item in value:
        status = _runtime_target_scope_label(item, TARGET_SCOPE_STATUSES)
        if status is None or status in seen:
            continue
        statuses.append(status)
        seen.add(status)
    return statuses


def _runtime_target_scope_label(value: object, allowed: set[str]) -> str | None:
    text = _runtime_text(value)
    if text is None:
        return None
    normalized = text.lower()
    return normalized if normalized in allowed else None


def _runtime_worker_selection(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    # worker_selection 保留候选/阻塞原因，便于排查“为什么没选某个 worker”。
    selection: dict[str, object] = {
        "task_type": _runtime_text(value.get("task_type")),
        "chosen": _runtime_worker_candidate(value.get("chosen")),
        "candidate_order": [
            _runtime_worker_candidate(candidate)
            for candidate in value.get("candidate_order", [])
            if isinstance(candidate, dict)
        ] if isinstance(value.get("candidate_order"), list) else [],
        "blocked": _runtime_worker_selection_blocked(value.get("blocked")),
        "sort_keys": _runtime_string_list(value.get("sort_keys")),
    }
    return _drop_empty_payload_values(selection)


def _runtime_worker_candidate(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return _drop_empty_payload_values(
        {
            "name": _runtime_text(value.get("name")),
            "type": _runtime_text(value.get("type")),
            "priority": _runtime_int(value.get("priority")),
            "running": _runtime_int(value.get("running")),
            "max_running": _runtime_int(value.get("max_running")),
        }
    )


def _runtime_worker_selection_blocked(value: object) -> dict[str, list[object]]:
    if not isinstance(value, dict):
        return {}
    return {
        "busy": _runtime_worker_blocked_list(value.get("busy"), "busy"),
        "cooldown": _runtime_worker_blocked_list(value.get("cooldown"), "cooldown"),
        "rejected": _runtime_worker_blocked_list(value.get("rejected"), "rejected"),
        "task_type": _runtime_worker_blocked_list(value.get("task_type"), "task_type"),
    }


def _runtime_worker_blocked_list(value: object, default_reason: str) -> list[object]:
    if not isinstance(value, list):
        return []
    items: list[object] = []
    for item in value:
        if isinstance(item, dict):
            blocked = _runtime_worker_blocked_item(item, default_reason)
            if blocked:
                items.append(blocked)
            continue
        text = _runtime_text(item)
        if text:
            items.append(text)
    return items


def _runtime_worker_blocked_item(value: dict[str, Any], default_reason: str) -> dict[str, object]:
    error_type = standard_task_run_error_type(value.get("error_type")) if "error_type" in value else None
    reason = _runtime_worker_blocked_reason(value.get("reason"), default_reason)
    running = value.get("running") if "running" in value else value.get("running_count")
    return _drop_empty_payload_values(
        {
            "name": _runtime_text(value.get("name") or value.get("worker_name")),
            "type": _runtime_text(value.get("type") or value.get("worker_type")),
            "reason": reason,
            "priority": _runtime_int(value.get("priority")),
            "running": _runtime_int(running),
            "max_running": _runtime_int(value.get("max_running")),
            "error_type": error_type,
            "retry_after_seconds": duration_payload_value(value.get("retry_after_seconds")),
        }
    )


def _runtime_worker_blocked_reason(value: object, default_reason: str) -> str:
    allowed = {"busy", "cooldown", "rejected", "task_type"}
    text = _runtime_text(value)
    if text in allowed:
        return text
    return default_reason if default_reason in allowed else "unknown"


ADVISORY_CONTAINER_PATH = "/cairn/advisory"
ADVISORY_MANIFEST_PATH = f"{ADVISORY_CONTAINER_PATH}/manifest.json"
ADVISORY_MODE = "project_advisory_bundle"
ADVISORY_RESOURCE_SOURCES = {
    "advisory_library",
    "legacy_knowledge",
    "project_resource",
}
ADVISORY_RESOURCE_KINDS = {"note", "script", "file"}


def _runtime_knowledge_base(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    digest = _runtime_sha256(value.get("bundle_digest"))
    path = _runtime_exact_path(value.get("path"), ADVISORY_CONTAINER_PATH)
    manifest_path = _runtime_exact_path(value.get("manifest_path"), ADVISORY_MANIFEST_PATH)
    enabled = value.get("enabled") is True and digest is not None and path is not None
    resources = _runtime_advisory_resources(value.get("resources")) if enabled else []
    item = _drop_empty_payload_values(
        {
            "key": _runtime_text(value.get("key")),
            "enabled": enabled,
            "injected": enabled and value.get("injected") is True,
            "path": path if enabled else None,
            "manifest_path": manifest_path if enabled else None,
            "bundle_digest": digest if enabled else None,
            "mode": ADVISORY_MODE,
            "read_only": value.get("read_only") is True,
        }
    )
    item["resources"] = resources
    return item


def _runtime_project_resources(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    digest = _runtime_sha256(value.get("bundle_digest"))
    manifest_path = _runtime_exact_path(value.get("manifest_path"), ADVISORY_MANIFEST_PATH)
    resources = [
        item
        for item in _runtime_advisory_resources(value.get("resources"))
        if item.get("source") == "project_resource"
    ]
    item = _drop_empty_payload_values(
        {
            "bundle_digest": digest,
            "manifest_path": manifest_path if digest is not None else None,
            "read_only": value.get("read_only") is True,
        }
    )
    item["resources"] = resources
    return item


def _runtime_advisory_resources(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    resources: list[dict[str, object]] = []
    for raw in value:
        resource = _runtime_advisory_resource(raw)
        if resource is not None:
            resources.append(resource)
    return resources


def _runtime_advisory_resource(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    source = _runtime_allowed_label(value.get("source"), ADVISORY_RESOURCE_SOURCES)
    kind = _runtime_allowed_label(value.get("kind"), ADVISORY_RESOURCE_KINDS)
    path = _runtime_advisory_relative_path(value.get("path"))
    size_bytes = _runtime_nonnegative_int(value.get("size_bytes"))
    digest = _runtime_sha256(value.get("sha256"))
    resource_id = _runtime_text(value.get("id"))
    library_key = _runtime_library_key(value.get("library_key"))
    library_version = _runtime_positive_int(value.get("library_version"))
    library_digest = _runtime_sha256(value.get("library_digest"))
    container_path = _runtime_advisory_resource_path(
        value.get("container_path"),
        source=source,
        relative_path=path,
        library_key=library_key,
    )
    if (
        source is None
        or kind is None
        or path is None
        or size_bytes is None
        or digest is None
        or container_path is None
        or (source == "project_resource" and resource_id is None)
        or (
            source == "advisory_library"
            and (
                library_key is None
                or library_version is None
                or library_digest is None
            )
        )
    ):
        return None
    return _drop_empty_payload_values(
        {
            "source": source,
            "id": resource_id if source == "project_resource" else None,
            "kind": kind,
            "library_key": library_key if source == "advisory_library" else None,
            "library_version": (
                library_version if source == "advisory_library" else None
            ),
            "library_digest": (
                library_digest if source == "advisory_library" else None
            ),
            "path": path,
            "container_path": container_path,
            "size_bytes": size_bytes,
            "sha256": digest,
        }
    )


def _runtime_exact_path(value: object, expected: str) -> str | None:
    text = _runtime_text(value)
    return expected if text == expected else None


def _runtime_advisory_relative_path(value: object) -> str | None:
    text = _runtime_text(value)
    if text is None or "\\" in text or any(ord(char) < 32 for char in text):
        return None
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _runtime_advisory_resource_path(
    value: object,
    *,
    source: str | None,
    relative_path: str | None,
    library_key: str | None,
) -> str | None:
    text = _runtime_text(value)
    if text is None or source is None or relative_path is None:
        return None
    if source == "project_resource":
        expected = f"{ADVISORY_CONTAINER_PATH}/project/{relative_path}"
        return expected if text == expected else None
    if source == "advisory_library":
        if library_key is None:
            return None
        expected = f"{ADVISORY_CONTAINER_PATH}/library/{library_key}/{relative_path}"
        return expected if text == expected else None
    prefix = f"{ADVISORY_CONTAINER_PATH}/legacy/"
    if not text.startswith(prefix):
        return None
    remainder = _runtime_advisory_relative_path(text.removeprefix(prefix))
    if remainder is None:
        return None
    parts = PurePosixPath(remainder).parts
    return text if len(parts) >= 2 and "/".join(parts[1:]) == relative_path else None


def _runtime_library_key(value: object) -> str | None:
    text = _runtime_text(value)
    if text is None or len(text) > 128 or fullmatch(r"[a-z0-9_-]+", text) is None:
        return None
    return text


def _runtime_positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _runtime_sha256(value: object) -> str | None:
    text = _runtime_text(value)
    if text is None or len(text) != 64 or any(char not in "0123456789abcdefABCDEF" for char in text):
        return None
    return text.lower()


def _runtime_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _runtime_tool_versions(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    versions: dict[str, str] = {}
    for tool in RUNTIME_TOOL_VERSION_FIELDS:
        text = _runtime_text(value.get(tool))
        if text:
            versions[tool] = text
    return versions or None
