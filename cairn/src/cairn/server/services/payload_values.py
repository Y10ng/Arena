"""事件 payload 与安全展示字段清洗。"""

from __future__ import annotations

from .common import *

def payload_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def sanitized_command_kind(value: object) -> str:
    text = payload_text(value)
    if text is None:
        return "unknown"
    # command_kind 只允许短标签，真实命令内容通过 artifact/raw_output 追踪。
    if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in text):
        return "unknown"
    return text[:64]


def sanitized_prompt_label(value: object) -> str:
    text = payload_text(value)
    if text is None:
        return "unknown"
    if len(text) > 128:
        return "unknown"
    if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in text):
        return "unknown"
    return text


def sanitized_prompt_hash(value: object) -> str:
    text = payload_text(value)
    if text is None:
        return "unknown"
    if len(text) != 64:
        return "unknown"
    if any(char not in "0123456789abcdefABCDEF" for char in text):
        return "unknown"
    return text.lower()


def sanitized_prompt_keys(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    keys: list[str] = []
    seen: set[str] = set()
    for item in value:
        key = sanitized_prompt_label(item)
        if key == "unknown" or key in seen:
            continue
        keys.append(key)
        seen.add(key)
    return keys


def command_finished_status(payload: dict[str, Any]) -> str:
    status = normalized_payload_status(payload.get("status"))
    if status in {"success", "succeeded", "ok", "completed"}:
        return "success"
    if status in {"failed", "failure", "error", "timed_out", "timeout", "cancelled", "canceled"}:
        return "failed"

    # 兼容不同 worker 上报风格：ok、timed_out、cancelled、exit_code 都能归一为三态。
    ok = payload.get("ok")
    if ok is True:
        return "success"
    if ok is False:
        return "failed"
    if payload.get("timed_out") is True or payload.get("cancelled") is True:
        return "failed"

    exit_code = integer_payload_value(payload.get("exit_code"))
    if exit_code is None:
        exit_code = integer_payload_value(payload.get("returncode"))
    if exit_code == 0:
        return "success"
    if exit_code is not None:
        return "failed"
    return "unknown"


def network_flow_status(payload: dict[str, Any]) -> str:
    # network_flow 是历史兼容事件，字段名可能来自 status/outcome/action 多种版本。
    status = normalized_payload_status(payload.get("status") or payload.get("outcome") or payload.get("action"))
    if status in NETWORK_FLOW_STATUSES:
        return status
    if status in {"observed", "captured", "logged", "recorded"}:
        return "observed"
    if status in {"allowed", "allow", "success", "succeeded", "ok"}:
        return "allowed"
    if status in {"blocked", "block", "denied", "deny", "rejected", "policy_denied"}:
        return "blocked"
    if status in {"failed", "failure", "error", "timeout", "timed_out", "unreachable"}:
        return "failed"

    if payload.get("blocked") is True:
        return "blocked"
    if payload.get("allowed") is True:
        return "allowed"
    if payload.get("allowed") is False:
        return "blocked"
    ok = payload.get("ok")
    if ok is True:
        return "allowed"
    if ok is False:
        return "failed"
    return "unknown"


def network_flow_network_plane(value: object) -> str:
    text = sanitized_prompt_label(value).lower()
    return text if text in NETWORK_FLOW_NETWORK_PLANES else "unknown"


def network_flow_protocol(value: object) -> str:
    text = sanitized_prompt_label(value).lower()
    return text if text in NETWORK_FLOW_PROTOCOLS else "unknown"


def network_flow_id(value: object) -> str:
    text = sanitized_prompt_label(value).lower()
    return text if text in NETWORK_FLOW_IDS else "unknown"


def network_flow_artifact_role(value: object) -> str:
    text = sanitized_prompt_label(value).lower()
    return text if text in NETWORK_FLOW_ARTIFACT_ROLES else "unknown"


def hook_event_name(value: object) -> str:
    text = payload_text(value)
    return text if text in HOOK_EVENT_NAMES else "unknown"


def hook_tool_name(value: object) -> str:
    # 区分 pi 的 bash/read 等原生名称与历史 Bash/Read，不做引擎伪装。
    text = payload_text(value)
    return text if text in HOOK_TOOL_NAMES else "unknown"


def hook_operation_status(value: object) -> str:
    status = normalized_payload_status(value)
    if status in HOOK_OPERATION_STATUSES:
        return status
    if status in {"success", "succeeded", "ok", "complete", "completed"}:
        return "succeeded"
    if status in {"failure", "error", "errored"}:
        return "failed"
    if status in {"deny", "denied", "blocked", "policy_denied"}:
        return "denied"
    if status in {"allow", "approved"}:
        return "allowed"
    return "unknown"


def hook_risk_level(value: object) -> str:
    text = normalized_payload_status(value)
    return text if text in HOOK_RISK_LEVELS else "unknown"


def hook_path_digest(value: object) -> str | None:
    text = payload_text(value)
    if text is None:
        return None
    lowered = text.strip().lower()
    if len(lowered) == 64 and all(char in "0123456789abcdef" for char in lowered):
        return lowered
    # 未预先散列的路径在服务端统一 sha256，保证同一路径仍可关联。
    return sha256(text.encode("utf-8")).hexdigest()


def artifact_metadata_role(value: object) -> str:
    text = sanitized_prompt_label(value).lower()
    return text if text in ARTIFACT_METADATA_ROLES else "unknown"


def normalized_payload_status(value: object) -> str:
    if isinstance(value, str):
        return value.strip().lower()
    return ""


def integer_payload_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    return None


def boolean_payload_value(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def nonnegative_integer_payload_value(value: object) -> int | None:
    number = integer_payload_value(value)
    if number is None or number < 0:
        return None
    return number


def duration_payload_value(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return round(max(float(value), 0.0), 3)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return round(max(float(text), 0.0), 3)
        except ValueError:
            return None
    return None


def list_payload_text_values(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    values: list[str] = []
    for item in value:
        text = sanitized_ref_text(item)
        if text is not None:
            values.append(text)
    return values


def _validation_result_status(value: object) -> str:
    status = normalized_payload_status(value)
    return status if status in {"valid", "invalid", "rejected"} else "unknown"


def _protocol_write_status(payload: dict[str, Any]) -> str:
    status = normalized_payload_status(payload.get("status"))
    if status in {"success", "failed", "skipped"}:
        return status
    ok = payload.get("ok")
    if ok is True:
        return "success"
    if ok is False:
        return "failed"
    return "unknown"


def sanitized_ref_text(value: object) -> str | None:
    text = payload_text(value)
    if text is None:
        return None
    if len(text) > 128:
        return "unknown"
    if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in text):
        return "unknown"
    return text[:128]


def payload_message_text(value: object) -> str | None:
    return payload_text(value)


def runtime_context_text(value: object) -> str | None:
    return payload_text(value)


def artifact_metadata_payload(metadata: object) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    # metadata 当前只做浅拷贝，具体安全字段在写入 artifact 时按调用场景约束。
    return dict(metadata)


def _drop_empty_payload_values(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if value is not None and value != [] and value != ""
    }
