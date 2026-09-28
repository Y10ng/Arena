"""Task run 趋势聚合。"""

from __future__ import annotations

from .common import *

from .time import _format_utc_timestamp, _parse_utc_timestamp

def _trend_summary(conn, now: str) -> MonitoringTrendSummary:
    now_dt = _parse_utc_timestamp(now) or datetime.now(timezone.utc)
    window_seconds = TREND_BUCKET_SECONDS * TREND_BUCKET_COUNT
    window_start_dt = now_dt - timedelta(seconds=window_seconds)
    # 趋势桶使用服务端 UTC 时间窗口，保证不同浏览器看到同一聚合边界。
    bucket_starts = [
        window_start_dt + timedelta(seconds=TREND_BUCKET_SECONDS * index)
        for index in range(TREND_BUCKET_COUNT)
    ]
    buckets = [
        {
            "bucket_start": _format_utc_timestamp(start),
            "bucket_end": _format_utc_timestamp(start + timedelta(seconds=TREND_BUCKET_SECONDS)),
            "started_count": 0,
            "finished_count": 0,
            "succeeded_count": 0,
            "failed_count": 0,
        }
        for start in bucket_starts
    ]

    rows = conn.execute(
        """
        SELECT status, started_at, ended_at
        FROM task_runs
        WHERE
            (started_at IS NOT NULL AND started_at >= ?)
            OR (ended_at IS NOT NULL AND ended_at >= ?)
        """,
        (_format_utc_timestamp(window_start_dt), _format_utc_timestamp(window_start_dt)),
    ).fetchall()

    def bucket_index(value: str | None) -> int | None:
        # 坏时间戳直接跳过，避免历史数据污染整段趋势。
        dt = _parse_utc_timestamp(value)
        if dt is None or dt < window_start_dt or dt > now_dt:
            return None
        elapsed = int((dt - window_start_dt).total_seconds())
        return min(TREND_BUCKET_COUNT - 1, max(0, elapsed // TREND_BUCKET_SECONDS))

    for row in rows:
        started_index = bucket_index(row["started_at"])
        if started_index is not None:
            buckets[started_index]["started_count"] += 1

        ended_index = bucket_index(row["ended_at"])
        if ended_index is None:
            continue
        status = str(row["status"] or "").strip().lower()
        if status not in {"succeeded", "failed", "timed_out", "cancelled"}:
            continue
        buckets[ended_index]["finished_count"] += 1
        if status == "succeeded":
            buckets[ended_index]["succeeded_count"] += 1
        else:
            buckets[ended_index]["failed_count"] += 1

    return MonitoringTrendSummary(
        bucket_seconds=TREND_BUCKET_SECONDS,
        bucket_count=TREND_BUCKET_COUNT,
        window_start=_format_utc_timestamp(window_start_dt),
        window_end=_format_utc_timestamp(now_dt),
        buckets=[
            MonitoringTrendBucket(
                **bucket,
                failure_rate=round(
                    bucket["failed_count"] / bucket["finished_count"],
                    3,
                )
                if bucket["finished_count"]
                else 0.0,
            )
            for bucket in buckets
        ],
    )
