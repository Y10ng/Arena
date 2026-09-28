"""时间与时间戳工具。"""

from __future__ import annotations

from .common import *

def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def timestamp_delta_seconds(start: object, end: object) -> int | None:
    # 监控聚合面对历史空值/坏值时返回 None，而不是抛出导致整页失败。
    start_dt = parse_utc_timestamp(start)
    end_dt = parse_utc_timestamp(end)
    if start_dt is None or end_dt is None:
        return None
    return max(0, int((end_dt - start_dt).total_seconds()))


def parse_utc_timestamp(value: object) -> datetime | None:
    """将兼容的 ISO-8601 时间规范化为 UTC；坏值返回 None。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
