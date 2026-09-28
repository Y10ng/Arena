"""Reason 决策元数据提取工具。"""

from __future__ import annotations


def reason_rationale(payload: object) -> str | None:
    # 决策理由允许几个历史字段名，便于兼容不同 prompt 版本的输出。
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    for key in ("rationale", "reasoning", "reflection"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def reason_rejected_options(payload: object) -> list[dict[str, object]]:
    # rejected_options 只作为审计元数据保存，不参与图谱写入。
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    options = data.get("rejected_options")
    if not isinstance(options, list):
        return []
    return [option for option in options if isinstance(option, dict)]


def reason_decision_metadata(payload: object) -> dict[str, object]:
    # 只提取受控字段，避免把模型输出中的任意结构写入决策记录。
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}

    metadata: dict[str, object] = {}
    confidence = data.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and 0 <= confidence <= 1:
        metadata["confidence"] = float(confidence)

    for key in ("blocked_by", "depends_on_targets"):
        value = data.get(key)
        if not isinstance(value, list):
            continue
        items = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        if items:
            metadata[key] = items
    return metadata


def intent_from_ids(intents: list[dict[str, object]]) -> list[str]:
    # 多个新 Intent 可能引用同一 fact，这里去重后用于决策审计。
    seen: set[str] = set()
    from_ids: list[str] = []
    for intent in intents:
        raw = intent.get("from")
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                continue
            fact_id = item.strip()
            if fact_id not in seen:
                seen.add(fact_id)
                from_ids.append(fact_id)
    return from_ids
