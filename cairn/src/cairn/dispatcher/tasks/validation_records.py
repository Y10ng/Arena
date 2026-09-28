"""Worker 输出校验事件记录器。"""

from __future__ import annotations

from .common_base import *
from cairn.dispatcher.worker_result_gateway import WorkerResultFailure


def record_validation_result(
    client: CairnClient,
    project_id: str,
    task_run_id: str | None,
    *,
    phase: str,
    status: str,
    outcome: str | None = None,
    payload: object | None = None,
    error_message: str | None = None,
    canonicalized_fields: list[str] | None = None,
    warnings: list[dict[str, object]] | None = None,
    failure: WorkerResultFailure | None = None,
) -> None:
    if task_run_id is None:
        return
    event_payload: dict[str, object] = {
        "phase": phase,
        "status": status,
    }
    if failure is not None:
        event_payload["failure"] = failure.details()
    if outcome is not None:
        event_payload["outcome"] = outcome
    result_kind = _result_kind_from_payload(payload)
    if result_kind is not None:
        event_payload["result_kind"] = result_kind
    accepted = _accepted_from_payload(payload)
    if accepted is not None:
        event_payload["accepted"] = accepted
    rejection_reason = _rejection_reason_from_payload(payload)
    if rejection_reason is not None:
        event_payload["rejection_reason"] = rejection_reason
    if failure is not None:
        # 失败诊断与 Gateway 的安全状态一致，不能被调用方原始异常正文覆盖。
        event_payload["error_message"] = failure.message
    elif error_message:
        event_payload["error_message"] = _compact_validation_text(error_message, limit=800)
    if canonicalized_fields:
        event_payload["canonicalization"] = {
            "fields": list(canonicalized_fields),
        }
    combined_warnings = list(warnings or [])
    combined_warnings.extend(_language_warnings_from_payload(payload))
    if combined_warnings:
        # 兼容性和语言警告只进入审计事件，不阻止协议写回。
        event_payload["warnings"] = combined_warnings
        event_payload["warning_count"] = len(combined_warnings)

    response = client.create_agent_event(
        project_id,
        task_run_id=task_run_id,
        kind="validation_result",
        payload=event_payload,
    )
    if response.ok:
        return
    LOG.warning(
        "validation event create failed project=%s task_run=%s phase=%s status=%s response_status=%s",
        project_id,
        task_run_id,
        phase,
        status,
        response.status_code,
    )


def _rejection_reason_from_payload(payload: object) -> str | None:
    if not isinstance(payload, dict) or payload.get("accepted") is not False:
        return None
    reason = payload.get("reason")
    if not isinstance(reason, str):
        return None
    text = _compact_validation_text(reason, limit=VALIDATION_REASON_LIMIT)
    return text or None


def _language_warnings_from_payload(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict) or payload.get("accepted") is False:
        return []
    # 对面向人类阅读的文本字段做轻量中文比例检查，避免 worker 漏掉中文输出要求。
    warnings: list[dict[str, object]] = []
    for path, text in _iter_human_text_fields(payload):
        score = _non_chinese_ratio(text)
        if score is None:
            continue
        warnings.append(
            {
                "type": "non_chinese_text_ratio",
                "field": path,
                "non_chinese_ratio": round(score, 2),
                "message": "面向人阅读的文本字段疑似未使用简体中文",
                "preview": _compact_validation_text(text, limit=LANGUAGE_WARNING_PREVIEW_LIMIT),
            }
        )
        if len(warnings) >= LANGUAGE_WARNING_MAX_COUNT:
            break
    return warnings


def _iter_human_text_fields(value: object, path: str = ""):
    if isinstance(value, dict):
        for key, item in value.items():
            key_path = f"{path}.{key}" if path else str(key)
            if isinstance(item, str) and _is_human_text_field(str(key), key_path):
                yield key_path, item
                continue
            yield from _iter_human_text_fields(item, key_path)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_human_text_fields(item, f"{path}[{index}]")


def _is_human_text_field(key: str, path: str) -> bool:
    normalized = key.strip().lower()
    if normalized in {"description", "rationale", "reasoning", "reflection"}:
        return True
    if normalized == "reason":
        return "rejected_options" in path
    return False


def _non_chinese_ratio(text: str) -> float | None:
    cleaned = _text_without_preserved_tokens(text)
    chinese_count = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", cleaned))
    latin_count = sum(
        len(match.group(0))
        for match in re.finditer(r"[A-Za-z]+", cleaned)
        if not _is_preserved_latin_token(match.group(0))
    )
    total = chinese_count + latin_count
    if latin_count < LANGUAGE_WARNING_MIN_LATIN_CHARS or total == 0:
        return None
    chinese_share = chinese_count / total
    if chinese_share > LANGUAGE_WARNING_MAX_CHINESE_SHARE:
        return None
    return latin_count / total


def _text_without_preserved_tokens(text: str) -> str:
    tokens = re.split(r"(\s+)", text)
    # 路径、URL、ID、全大写缩写等保留 token 不计入“非中文”比例。
    return " ".join(token for token in tokens if not _is_preserved_text_token(token))


def _is_preserved_text_token(token: str) -> bool:
    value = token.strip("`'\"“”‘’()[]{}<>,;，。.!！？")
    if not value:
        return False
    if re.search(r"[/\\._:@#=-]", value):
        return True
    if re.search(r"\d", value):
        return True
    if value.isupper() and len(value) > 1:
        return True
    return value in {"Cairn", "Fact", "Facts", "Intent", "Intents", "Goal", "JSON", "YAML", "API", "HTTP", "HTTPS"}


def _is_preserved_latin_token(token: str) -> bool:
    return token.isupper() and len(token) > 1


def _result_kind_from_payload(payload: object | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    result_kind = payload.get("kind")
    if isinstance(result_kind, str) and result_kind:
        return result_kind
    return None


def _accepted_from_payload(payload: object | None) -> bool | None:
    if not isinstance(payload, dict):
        return None
    accepted = payload.get("accepted")
    if isinstance(accepted, bool):
        return accepted
    return None
