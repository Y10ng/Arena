"""错误类型聚合。"""

from __future__ import annotations

from .common import *

from .tasks import _task_run_summary
from .time import _format_utc_timestamp, _parse_utc_timestamp

def _error_counts(conn, now: str) -> list[MonitoringErrorAggregate]:
    placeholders = ", ".join("?" for _ in STANDARD_TASK_RUN_ERROR_TYPES)
    now_dt = _parse_utc_timestamp(now) or datetime.now(timezone.utc)
    window_start = _format_utc_timestamp(now_dt - timedelta(days=ERROR_COUNT_WINDOW_DAYS))
    # 错误聚合只取近期失败窗口，并为每种标准错误类型返回一条最新 task_run 示例。
    rows = conn.execute(
        f"""
        WITH recent_failed_runs AS (
            SELECT tr.*, p.title AS project_title, p.status AS project_status,
                   COALESCE(ac.artifact_count, 0) AS artifact_count,
                   CASE
                       WHEN tr.started_at IS NULL THEN NULL
                       ELSE CAST(MAX(0, (julianday(COALESCE(tr.ended_at, ?)) - julianday(tr.started_at)) * 86400) AS INTEGER)
                   END AS duration_seconds
            FROM task_runs tr
            JOIN projects p ON p.id = tr.project_id
            LEFT JOIN (
                SELECT project_id, task_run_id, COUNT(*) AS artifact_count
                FROM artifacts
                GROUP BY project_id, task_run_id
            ) ac ON ac.project_id = tr.project_id AND ac.task_run_id = tr.id
            WHERE tr.status IN ('failed', 'timed_out')
              AND COALESCE(tr.ended_at, tr.started_at, ?) >= ?
            ORDER BY COALESCE(tr.ended_at, tr.started_at, tr.id) DESC, tr.project_id, tr.id
            LIMIT ?
        ),
        failed_runs AS (
            SELECT tr.*,
                   CASE
                       WHEN tr.error_type IN ({placeholders}) THEN tr.error_type
                       ELSE 'unknown'
                   END AS aggregate_error_type
            FROM recent_failed_runs tr
        ),
        ranked AS (
            SELECT failed_runs.*,
                   COUNT(*) OVER (PARTITION BY aggregate_error_type) AS aggregate_count,
                   ROW_NUMBER() OVER (
                       PARTITION BY aggregate_error_type
                       ORDER BY COALESCE(ended_at, started_at, id) DESC, project_id, id
                   ) AS rn
            FROM failed_runs
        )
        SELECT *
        FROM ranked
        WHERE rn = 1
        ORDER BY aggregate_count DESC, aggregate_error_type
        """,
        (
            now,
            now,
            window_start,
            ERROR_COUNT_LIMIT,
            *sorted(STANDARD_TASK_RUN_ERROR_TYPES),
        ),
    ).fetchall()
    return [
        MonitoringErrorAggregate(
            error_type=standard_task_run_error_type(row["aggregate_error_type"]) or "unknown",
            count=row["aggregate_count"],
            latest_task_run=_task_run_summary(row),
        )
        for row in rows
    ]
