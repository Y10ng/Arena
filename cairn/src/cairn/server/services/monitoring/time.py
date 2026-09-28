"""监控时间窗口工具。"""

from __future__ import annotations

from .common import *

def _remaining_seconds_until(now: str, retry_until_at: str) -> int | None:
    now_dt = _parse_utc_timestamp(now)
    retry_until_dt = _parse_utc_timestamp(retry_until_at)
    if now_dt is None or retry_until_dt is None:
        return None
    return max(0, ceil((retry_until_dt - now_dt).total_seconds()))


def _parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
