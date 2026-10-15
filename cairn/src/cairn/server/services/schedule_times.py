"""预览和消费共用的时区规则；周期缺失时刻跳过，重复时刻取较早一次。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from cairn.server.schemas.project_schedules import ScheduleTiming

UTC = timezone.utc


def timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_candidates(day: date, clock: time, zone: ZoneInfo) -> list[datetime]:
    naive = datetime.combine(day, clock)
    candidates = set()
    for fold in (0, 1):
        local = naive.replace(tzinfo=zone, fold=fold)
        instant = local.astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) == naive:
            candidates.add(instant)
    return sorted(candidates)


def end_boundary(rule: ScheduleTiming) -> datetime | None:
    if rule.end_date is None:
        return None
    # 结束日期包含整日，下一日本地零点是排他的结束边界。
    day = date.fromisoformat(rule.end_date) + timedelta(days=1)
    return datetime.combine(day, time.min, ZoneInfo(rule.timezone)).astimezone(UTC)


def effective_boundary(rule: ScheduleTiming, project=None) -> datetime | None:
    boundaries = [end_boundary(rule)]
    if project is not None:
        boundaries.extend(timestamp(value) for value in (project.authorization_expires_at, project.explore_deadline) if value)
    return min((value for value in boundaries if value is not None), default=None)


def next_times(rule: ScheduleTiming, *, after: datetime, count: int = 3, inclusive: bool = True, boundary: datetime | None = None) -> list[dict]:
    lower = max(timestamp(rule.start_at), after.astimezone(UTC))
    stop = boundary if boundary is not None else end_boundary(rule)
    zone = ZoneInfo(rule.timezone)
    if rule.mode == "once":
        instant = timestamp(rule.start_at)
        if instant < after or (instant == after and not inclusive) or (stop and instant >= stop):
            return []
        return [{"at": iso(instant), "local": instant.astimezone(zone).isoformat(), "note": None}]
    clock = time.fromisoformat(rule.local_time)
    day = lower.astimezone(zone).date()
    results: list[dict] = []
    skipped_gap = False
    # 每周必有选中日；留出历史上时区跳过整日以及所需的三个未来时刻。
    for _ in range(370):
        if rule.mode == "daily" or day.weekday() in rule.weekdays:
            candidates = local_candidates(day, clock, zone)
            if not candidates:
                skipped_gap = True
            else:
                instant = candidates[0]
                if instant >= lower and (inclusive or instant != after):
                    if stop and instant >= stop:
                        return results
                    note = "重复本地时刻，固定取较早偏移" if len(candidates) > 1 else None
                    if skipped_gap:
                        note = "已跳过夏令时不存在的本地时刻" + (f"；{note}" if note else "")
                    results.append({"at": iso(instant), "local": instant.astimezone(zone).isoformat(), "note": note})
                    skipped_gap = False
                    if len(results) >= count:
                        return results
        day += timedelta(days=1)
    return results
