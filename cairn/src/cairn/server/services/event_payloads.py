"""Agent event payload 转换服务。"""

from __future__ import annotations

from .common import *

from .payload_values import (
    _drop_empty_payload_values,
    artifact_metadata_payload,
    command_finished_status,
    duration_payload_value,
    hook_event_name,
    hook_operation_status,
    hook_path_digest,
    hook_risk_level,
    hook_tool_name,
    integer_payload_value,
    list_payload_text_values,
    network_flow_artifact_role,
    network_flow_id,
    network_flow_network_plane,
    network_flow_protocol,
    network_flow_status,
    nonnegative_integer_payload_value,
    payload_message_text,
    payload_text,
    runtime_context_text,
    sanitized_command_kind,
    sanitized_prompt_hash,
    sanitized_prompt_keys,
    sanitized_prompt_label,
    sanitized_ref_text,
    _protocol_write_status,
    _validation_result_status,
)
from .runtime_context import task_run_runtime_context_payload

def agent_event_to_model(row: sqlite3.Row) -> AgentEvent:
    # agent event payload 先按 kind 清洗，再进入 API 响应，减少敏感/异常字段外泄。
    payload = _agent_event_payload(row["kind"], row["payload_json"])
    return AgentEvent(
        id=row["id"],
        task_run_id=row["task_run_id"],
        project_id=row["project_id"],
        ts=row["ts"],
        kind=row["kind"],
        payload=payload,
    )


def result_event_to_model(row: sqlite3.Row) -> TaskRunResultEvent:
    return TaskRunResultEvent(
        id=row["id"],
        task_run_id=row["task_run_id"],
        project_id=row["project_id"],
        ts=row["ts"],
        kind=row["kind"],
        payload=event_payload(row["payload_json"]),
    )


def command_event_to_model(row: sqlite3.Row) -> TaskRunCommandEvent:
    payload = event_payload(row["payload_json"])
    return TaskRunCommandEvent(
        id=row["id"],
        task_run_id=row["task_run_id"],
        project_id=row["project_id"],
        ts=row["ts"],
        kind=row["kind"],
        phase=payload_text(payload.get("phase")) or "unknown",
        command_kind=sanitized_command_kind(payload.get("command_kind")),
        status=command_finished_status(payload) if row["kind"] == "command_finished" else "unknown",
        exit_code=integer_payload_value(payload.get("exit_code")),
        returncode=integer_payload_value(payload.get("returncode")),
        timed_out=payload.get("timed_out") is True,
        cancelled=payload.get("cancelled") is True,
        oom_killed=payload.get("oom_killed") is True,
        oom_evidence_source=payload_text(payload.get("oom_evidence_source")),
        duration_seconds=duration_payload_value(payload.get("duration_seconds")),
    )


def prompt_event_to_model(row: sqlite3.Row) -> TaskRunPromptEvent:
    payload = event_payload(row["payload_json"])
    return TaskRunPromptEvent(
        id=row["id"],
        task_run_id=row["task_run_id"],
        project_id=row["project_id"],
        ts=row["ts"],
        phase=sanitized_prompt_label(payload.get("phase")),
        prompt_group=sanitized_prompt_label(payload.get("prompt_group")),
        prompt_name=sanitized_prompt_label(payload.get("prompt_name")),
        prompt_hash=sanitized_prompt_hash(payload.get("prompt_hash")),
        prompt_bytes=integer_payload_value(payload.get("prompt_bytes")),
        prompt_chars=integer_payload_value(payload.get("prompt_chars")),
        replacement_keys=sanitized_prompt_keys(payload.get("replacement_keys")),
        worker_name=sanitized_prompt_label(payload.get("worker_name")),
        worker_type=sanitized_prompt_label(payload.get("worker_type")),
    )


def hook_event_to_model(row: sqlite3.Row) -> TaskRunHookEvent:
    payload = _hook_operation_payload(event_payload(row["payload_json"]))
    return TaskRunHookEvent(
        id=row["id"],
        task_run_id=row["task_run_id"],
        project_id=row["project_id"],
        ts=row["ts"],
        session_id=sanitized_ref_text(payload.get("session_id")),
        hook_event_name=hook_event_name(payload.get("hook_event_name")),
        tool_name=hook_tool_name(payload.get("tool_name")),
        operation_id=sanitized_ref_text(payload.get("operation_id")),
        status=hook_operation_status(payload.get("status")),
        recorded_at=payload_text(payload.get("recorded_at")),
        phase=payload_text(payload.get("phase")) or "unknown",
        cwd=runtime_context_text(payload.get("cwd")),
        tool_input=payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {},
        tool_response=payload.get("tool_response") if isinstance(payload.get("tool_response"), dict) else {},
        command=payload_text(payload.get("command")),
        command_kind=sanitized_command_kind(payload.get("command_kind"))
        if payload.get("command_kind") is not None
        else None,
        file_path=payload_text(payload.get("file_path")),
        file_path_digest=hook_path_digest(payload.get("file_path_digest")),
        query=payload_text(payload.get("query")),
        url=payload_text(payload.get("url")),
        exit_code=integer_payload_value(payload.get("exit_code")),
        duration_ms=nonnegative_integer_payload_value(payload.get("duration_ms")),
        artifact_id=sanitized_ref_text(payload.get("artifact_id")),
        risk_level=hook_risk_level(payload.get("risk_level")),
        summary=runtime_context_text(payload.get("summary")),
    )


def event_payload(payload_json: str | None) -> dict[str, Any]:
    try:
        payload = loads(payload_json or "{}")
    except (TypeError, ValueError):
        # 事件 payload 损坏时返回空对象，监控聚合继续可用。
        return {}
    return payload if isinstance(payload, dict) else {}


def _agent_event_payload(kind: str, payload_json: str | None) -> dict[str, Any]:
    return event_payload(payload_json)


def _prompt_rendered_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _drop_empty_payload_values(
        {
            "phase": sanitized_prompt_label(payload.get("phase")),
            "prompt_group": sanitized_prompt_label(payload.get("prompt_group")),
            "prompt_name": sanitized_prompt_label(payload.get("prompt_name")),
            "prompt_hash": sanitized_prompt_hash(payload.get("prompt_hash")),
            "prompt_bytes": integer_payload_value(payload.get("prompt_bytes")),
            "prompt_chars": integer_payload_value(payload.get("prompt_chars")),
            "replacement_keys": sanitized_prompt_keys(payload.get("replacement_keys")),
            "worker_name": sanitized_prompt_label(payload.get("worker_name")),
            "worker_type": sanitized_prompt_label(payload.get("worker_type")),
        }
    )


def _command_event_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    cancel_reason = payload_text(payload.get("cancel_reason"))
    return _drop_empty_payload_values(
        {
            "phase": sanitized_prompt_label(payload.get("phase")),
            "worker_name": sanitized_prompt_label(payload.get("worker_name")),
            "worker_type": sanitized_prompt_label(payload.get("worker_type")),
            "command_kind": sanitized_command_kind(payload.get("command_kind")),
            "status": command_finished_status(payload) if kind == "command_finished" else "unknown",
            "exit_code": integer_payload_value(payload.get("exit_code")),
            "returncode": integer_payload_value(payload.get("returncode")),
            "timed_out": payload.get("timed_out") is True,
            "cancelled": payload.get("cancelled") is True,
            "oom_killed": payload.get("oom_killed") is True,
            "oom_evidence_source": payload_text(payload.get("oom_evidence_source")),
            "duration_seconds": duration_payload_value(payload.get("duration_seconds")),
            "cancel_reason": sanitized_prompt_label(cancel_reason) if cancel_reason is not None else None,
        }
    )


def _artifact_created_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _drop_empty_payload_values(
        {
            "artifact_id": sanitized_ref_text(payload.get("artifact_id")),
            "kind": sanitized_prompt_label(payload.get("kind")),
            "uri": runtime_context_text(payload.get("uri")),
            "sha256": sanitized_prompt_hash(payload.get("sha256")),
            "size": integer_payload_value(payload.get("size")),
            "metadata": artifact_metadata_payload(payload.get("metadata")),
        }
    )


def _worker_stream_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _drop_empty_payload_values(
        {
            "artifact_id": sanitized_ref_text(payload.get("artifact_id")),
            "phase": sanitized_prompt_label(payload.get("phase")),
            "stream": sanitized_prompt_label(payload.get("stream")),
            "size": integer_payload_value(payload.get("size")),
            "returncode": integer_payload_value(payload.get("returncode")),
            "exit_code": integer_payload_value(payload.get("exit_code")),
            "timed_out": payload.get("timed_out") is True,
            "cancelled": payload.get("cancelled") is True,
            "duration_seconds": duration_payload_value(payload.get("duration_seconds")),
        }
    )


def _network_flow_payload(payload: dict[str, Any]) -> dict[str, Any]:
    artifact_sha256 = None
    if payload.get("artifact_sha256") is not None:
        artifact_sha256 = sanitized_prompt_hash(payload.get("artifact_sha256"))
    return _drop_empty_payload_values(
        {
            "phase": sanitized_prompt_label(payload.get("phase")),
            "network_plane": network_flow_network_plane(payload.get("network_plane") or payload.get("plane")),
            "protocol": network_flow_protocol(
                payload.get("protocol")
                or payload.get("transport")
                or payload.get("scheme")
            ),
            "status": network_flow_status(payload),
            "flow_id": network_flow_id(payload.get("flow_id")) if payload.get("flow_id") is not None else None,
            "artifact_id": sanitized_ref_text(payload.get("artifact_id")),
            "artifact_role": network_flow_artifact_role(payload.get("artifact_role"))
            if payload.get("artifact_role") is not None
            else None,
            "artifact_sha256": artifact_sha256,
            "bytes_received": nonnegative_integer_payload_value(payload.get("bytes_received")),
            "byte_count": nonnegative_integer_payload_value(
                _first_present_payload_value(payload, "byte_count", "bytes")
            ),
            "error_message": payload_message_text(payload.get("error_message")),
        }
    )


def _hook_operation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = payload.get("input") if isinstance(payload.get("input"), dict) else {}
    tool_response = payload.get("tool_response")
    if not isinstance(tool_response, dict):
        tool_response = payload.get("response") if isinstance(payload.get("response"), dict) else {}

    hook_name = hook_event_name(
        _first_present_payload_value(payload, "hook_event_name", "hook_event", "event_name", "event")
    )
    tool_name = hook_tool_name(_first_present_payload_value(payload, "tool_name", "tool"))
    status = hook_operation_status(payload.get("status"))
    if status == "unknown":
        # 部分 hook 日志没有显式 status，按 hook 事件类型和 response 推导。
        status = _hook_status_from_event(hook_name, payload, tool_response)
    file_path_value = _first_present_payload_value(payload, "file_path_digest", "file_digest", "path_digest")
    raw_file_path_value = _first_present_payload_value(payload, "file_path", "path")
    if raw_file_path_value is None:
        raw_file_path_value = _first_present_payload_value(tool_input, "file_path", "path")
    if file_path_value is None:
        file_path_value = raw_file_path_value
    # digest 仅用于稳定关联；授权审计界面同时保留并展示原始路径。
    file_digest = hook_path_digest(file_path_value)
    duration_value = _first_present_payload_value(payload, "duration_ms", "durationMs")
    if duration_value is None:
        duration_value = _first_present_payload_value(tool_response, "duration_ms", "durationMs")
    exit_value = _first_present_payload_value(payload, "exit_code", "returncode")
    if exit_value is None:
        exit_value = _first_present_payload_value(tool_response, "exit_code", "returncode")
    summary_value = _first_present_payload_value(payload, "summary", "redacted_summary", "message")
    if summary_value is None:
        summary_value = tool_response.get("error")

    return _drop_empty_payload_values(
        {
            "session_id": sanitized_ref_text(payload.get("session_id")),
            "hook_event_name": hook_name,
            "tool_name": tool_name,
            "operation_id": sanitized_ref_text(
                _first_present_payload_value(payload, "operation_id", "tool_use_id", "tool_call_id", "id")
            ),
            "status": status,
            "recorded_at": payload_text(payload.get("recorded_at")),
            "phase": payload_text(payload.get("phase")),
            "cwd": runtime_context_text(payload.get("cwd")),
            "tool_input": tool_input,
            "tool_response": tool_response,
            "command": payload_text(payload.get("command") or tool_input.get("command")),
            "command_kind": _hook_command_kind(tool_name, payload, tool_input),
            "file_path": payload_text(raw_file_path_value),
            "file_path_digest": file_digest,
            "query": payload_text(payload.get("query") or tool_input.get("query")),
            "url": payload_text(payload.get("url") or tool_input.get("url")),
            "exit_code": integer_payload_value(exit_value),
            "duration_ms": nonnegative_integer_payload_value(duration_value),
            "artifact_id": sanitized_ref_text(payload.get("artifact_id")),
            "risk_level": hook_risk_level(payload.get("risk_level")),
            "summary": runtime_context_text(summary_value),
        }
    )


def _runtime_context_updated_payload(payload: dict[str, Any]) -> dict[str, Any]:
    updated_keys = payload.get("updated_keys")
    safe_keys = [
        key
        for key in updated_keys
        if isinstance(key, str) and key in RUNTIME_CONTEXT_AUDIT_FIELDS
    ] if isinstance(updated_keys, list) else []
    return _drop_empty_payload_values(
        {
            "updated_keys": sorted(dict.fromkeys(safe_keys)),
            "runtime_context": task_run_runtime_context_payload(payload.get("runtime_context"))
            if isinstance(payload.get("runtime_context"), dict)
            else None,
        }
    )


def _first_present_payload_value(payload: dict[str, Any], *keys: str) -> object:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _hook_status_from_event(hook_name: str, payload: dict[str, Any], tool_response: dict[str, Any]) -> str:
    if hook_name == "SessionStart":
        return "started"
    if hook_name == "UserPromptSubmit":
        return "submitted"
    if hook_name == "PreToolUse":
        if payload.get("blocked") is True or payload.get("denied") is True:
            return "denied"
        if payload.get("allowed") is True:
            return "allowed"
        return "planned"
    if hook_name == "PostToolUse":
        if tool_response.get("error") is not None or payload.get("error") is not None:
            return "failed"
        return "succeeded"
    if hook_name == "PostToolUseFailure":
        return "failed"
    if hook_name == "Stop":
        return "stopped"
    if hook_name == "SessionEnd":
        return "ended"
    return "unknown"


def _hook_command_kind(tool_name: str, payload: dict[str, Any], tool_input: dict[str, Any]) -> str | None:
    if payload.get("command_kind") is not None:
        return sanitized_command_kind(payload.get("command_kind"))
    if tool_name != "Bash":
        return None
    command = payload_text(tool_input.get("command") or payload.get("command"))
    if command is None:
        return None
    first_token = command.strip().split(maxsplit=1)[0] if command.strip() else ""
    return sanitized_command_kind(first_token)


def _validation_result_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _drop_empty_payload_values(
        {
            "phase": sanitized_prompt_label(payload.get("phase")),
            "status": _validation_result_status(payload.get("status")),
            "outcome": sanitized_prompt_label(payload.get("outcome")),
            "result_kind": sanitized_prompt_label(payload.get("result_kind")),
            "accepted": boolean_payload_value(payload.get("accepted")),
            "rejection_reason": runtime_context_text(payload.get("rejection_reason")),
            "error_message": payload_message_text(payload.get("error_message")),
        }
    )


def _protocol_write_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _drop_empty_payload_values(
        {
            "operation": sanitized_prompt_label(payload.get("operation")),
            "phase": sanitized_prompt_label(payload.get("phase")),
            "status": _protocol_write_status(payload),
            "status_code": integer_payload_value(payload.get("status_code")),
            "ok": boolean_payload_value(payload.get("ok")),
            "intent_id": sanitized_ref_text(payload.get("intent_id")),
            "fact_id": sanitized_ref_text(payload.get("fact_id")),
            "from_ids": list_payload_text_values(payload.get("from_ids")),
            "created_id": sanitized_ref_text(payload.get("created_id")),
            "evidence_count": integer_payload_value(payload.get("evidence_count")),
            "error_message": payload_message_text(payload.get("error_message")),
        }
    )
