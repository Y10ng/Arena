"""Worker 健康与容量聚合。"""

from __future__ import annotations

from .common import *

from .dispatcher import _dispatcher_window_items, _latest_dispatcher_row

def _worker_summaries(conn, now: str) -> list[MonitoringWorkerSummary]:
    # worker summary 合并 task_runs 历史、dispatcher 当前配置和最近 healthcheck 事件。
    pool_items = _latest_worker_pool(conn)
    worker_cooldown_keys, worker_cooldown_names = _latest_worker_cooldown_refs(conn, now)
    consecutive_failure_counts = _worker_consecutive_failure_counts(conn)
    latest_healthchecks = _latest_worker_healthchecks(conn)
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT worker_name, worker_type, status, error_type, started_at, ended_at
            FROM (
                SELECT tr.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY worker_name, worker_type
                           ORDER BY COALESCE(ended_at, started_at, id) DESC, id DESC
                       ) AS rn
                FROM task_runs tr
            )
            WHERE rn = 1
        )
        SELECT tr.worker_name, tr.worker_type,
               SUM(CASE WHEN tr.status = 'running' THEN 1 ELSE 0 END) AS running_count,
               SUM(CASE WHEN tr.status = 'queued' THEN 1 ELSE 0 END) AS queued_count,
               SUM(CASE WHEN tr.status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded_count,
               SUM(CASE WHEN tr.status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
               SUM(CASE WHEN tr.status = 'timed_out' THEN 1 ELSE 0 END) AS timed_out_count,
               SUM(CASE WHEN tr.status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled_count,
               latest.status AS last_status,
               latest.error_type AS last_error_type,
               latest.started_at AS last_started_at,
               latest.ended_at AS last_ended_at
        FROM task_runs tr
        JOIN latest ON latest.worker_name = tr.worker_name AND latest.worker_type = tr.worker_type
        GROUP BY tr.worker_name, tr.worker_type,
                 latest.status, latest.error_type, latest.started_at, latest.ended_at
        ORDER BY running_count DESC, failed_count DESC, tr.worker_name
        """
    ).fetchall()
    summaries: dict[tuple[str, str], MonitoringWorkerSummary] = {}
    for row in rows:
        key = (row["worker_name"], row["worker_type"])
        pool_item = pool_items.get(key)
        latest_healthcheck = latest_healthchecks.get(key)
        running_count = row["running_count"] or 0
        last_error_type = standard_task_run_error_type(row["last_error_type"])
        pool_running_count = pool_item.running_count if pool_item is not None else None
        summaries[key] = MonitoringWorkerSummary(
            worker_name=row["worker_name"],
            worker_type=row["worker_type"],
            health=_worker_health(
                running_count=running_count,
                pool_running_count=pool_running_count,
                last_error_type=last_error_type,
                last_status=row["last_status"],
                latest_healthcheck=latest_healthcheck,
                in_worker_cooldown=_worker_in_cooldown(key, worker_cooldown_keys, worker_cooldown_names),
            ),
            **_worker_pool_summary_fields(pool_item),
            running_count=running_count,
            queued_count=row["queued_count"] or 0,
            succeeded_count=row["succeeded_count"] or 0,
            failed_count=row["failed_count"] or 0,
            timed_out_count=row["timed_out_count"] or 0,
            cancelled_count=row["cancelled_count"] or 0,
            consecutive_failure_count=consecutive_failure_counts.get(key, 0),
            last_status=row["last_status"],
            last_error_type=last_error_type,
            last_started_at=row["last_started_at"],
            last_ended_at=row["last_ended_at"],
            **_worker_healthcheck_summary_fields(latest_healthcheck),
        )
    for key, pool_item in pool_items.items():
        if key in summaries:
            continue
        latest_healthcheck = latest_healthchecks.get(key)
        summaries[key] = MonitoringWorkerSummary(
            worker_name=pool_item.worker_name,
            worker_type=pool_item.worker_type,
            health=_worker_health(
                running_count=0,
                pool_running_count=pool_item.running_count,
                last_error_type=None,
                last_status=None,
                latest_healthcheck=latest_healthcheck,
                in_worker_cooldown=_worker_in_cooldown(key, worker_cooldown_keys, worker_cooldown_names),
            ),
            **_worker_pool_summary_fields(pool_item),
            consecutive_failure_count=consecutive_failure_counts.get(key, 0),
            **_worker_healthcheck_summary_fields(latest_healthcheck),
        )
    return sorted(
        summaries.values(),
        key=lambda worker: (
            -(worker.running_count or worker.pool_running_count or 0),
            not worker.configured,
            -worker.failed_count,
            -worker.timed_out_count,
            worker.worker_name,
            worker.worker_type,
        ),
    )


def _worker_health(
    *,
    running_count: int,
    pool_running_count: int | None,
    last_error_type: str | None,
    last_status: str | None,
    latest_healthcheck: dict[str, object] | None,
    in_worker_cooldown: bool,
) -> str:
    # 健康状态偏向“可调度性”：运行中优先，其次健康检查/cooldown/近期错误。
    if running_count > 0 or (pool_running_count or 0) > 0:
        return "running"
    if latest_healthcheck is not None and latest_healthcheck.get("status") == "failed":
        return "unhealthy"
    if in_worker_cooldown:
        return "unhealthy"
    if last_error_type in UNHEALTHY_ERROR_TYPES:
        return "unhealthy"
    if last_status is None:
        return "idle"
    return "healthy"


def _worker_consecutive_failure_counts(conn) -> dict[tuple[str, str], int]:
    # 连续失败只看最近关闭任务，遇到成功/取消就截断失败链。
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT worker_name, worker_type, status,
                   ROW_NUMBER() OVER (
                       PARTITION BY worker_name, worker_type
                       ORDER BY COALESCE(ended_at, started_at, id) DESC, id DESC
                   ) AS rn
            FROM task_runs
            WHERE status IN ('succeeded', 'failed', 'timed_out', 'cancelled')
        )
        SELECT worker_name, worker_type, status
        FROM ranked
        WHERE rn <= ?
        ORDER BY worker_name, worker_type,
                 rn
        """,
        (WORKER_RECENT_CLOSED_TASK_LIMIT,),
    ).fetchall()
    counts: dict[tuple[str, str], int] = {}
    closed: set[tuple[str, str]] = set()
    for row in rows:
        key = (row["worker_name"], row["worker_type"])
        if key in closed:
            continue
        if row["status"] in {"failed", "timed_out"}:
            counts[key] = counts.get(key, 0) + 1
            continue
        closed.add(key)
        counts.setdefault(key, 0)
    return counts


def _latest_worker_healthchecks(conn) -> dict[tuple[str, str], dict[str, object]]:
    # healthcheck 是 command_finished 的特殊 phase，失败会影响 worker 健康展示。
    rows = conn.execute(
        """
        SELECT ae.ts, ae.payload_json, tr.worker_name, tr.worker_type
        FROM agent_events ae
        JOIN task_runs tr ON tr.project_id = ae.project_id AND tr.id = ae.task_run_id
        WHERE ae.kind = 'command_finished'
        ORDER BY ae.ts DESC, ae.id DESC
        LIMIT ?
        """,
        (WORKER_HEALTHCHECK_EVENT_LIMIT,),
    ).fetchall()
    latest: dict[tuple[str, str], dict[str, object]] = {}
    for row in rows:
        key = (row["worker_name"], row["worker_type"])
        if key in latest:
            continue
        payload = event_payload(row["payload_json"])
        phase = payload_text(payload.get("phase"))
        if phase is None:
            continue
        if phase != "healthcheck" and not phase.endswith("_healthcheck"):
            continue
        latest[key] = {
            "status": command_finished_status(payload),
            "at": row["ts"],
            "phase": phase,
            "command_kind": sanitized_command_kind(payload.get("command_kind")),
            "duration_seconds": duration_payload_value(payload.get("duration_seconds")),
        }
    return latest


def _worker_healthcheck_summary_fields(healthcheck: dict[str, object] | None) -> dict[str, object]:
    if healthcheck is None:
        return {}
    return {
        "latest_healthcheck_status": healthcheck.get("status"),
        "latest_healthcheck_at": healthcheck.get("at"),
        "latest_healthcheck_phase": healthcheck.get("phase"),
        "latest_healthcheck_command_kind": healthcheck.get("command_kind"),
        "latest_healthcheck_duration_seconds": healthcheck.get("duration_seconds"),
    }


def _latest_worker_pool(conn) -> dict[tuple[str, str], DispatcherWorkerPoolItem]:
    dispatcher_row = _latest_dispatcher_row(conn)
    if dispatcher_row is None:
        return {}
    items = _dispatcher_window_items(dispatcher_row["worker_pool_json"], DispatcherWorkerPoolItem)
    return {(item.worker_name, item.worker_type): item for item in items}


def _latest_worker_cooldown_refs(conn, now: str) -> tuple[set[tuple[str, str]], set[str]]:
    dispatcher_row = _latest_dispatcher_row(conn)
    if dispatcher_row is None:
        return set(), set()
    items = _dispatcher_window_items(
        dispatcher_row["worker_cooldowns_json"],
        DispatcherWorkerCooldown,
        now=now,
        active_only=True,
    )
    worker_keys: set[tuple[str, str]] = set()
    worker_names: set[str] = set()
    for item in items:
        if item.retry_after_seconds <= 0:
            continue
        if item.worker_type is None:
            worker_names.add(item.worker_name)
        else:
            worker_keys.add((item.worker_name, item.worker_type))
    return worker_keys, worker_names


def _worker_in_cooldown(
    key: tuple[str, str],
    worker_cooldown_keys: set[tuple[str, str]],
    worker_cooldown_names: set[str],
) -> bool:
    worker_name, _worker_type = key
    return key in worker_cooldown_keys or worker_name in worker_cooldown_names


def _worker_pool_summary_fields(pool_item: DispatcherWorkerPoolItem | None) -> dict[str, object]:
    if pool_item is None:
        return {"configured": False}
    return {
        "configured": True,
        "task_types": pool_item.task_types,
        "priority": pool_item.priority,
        "max_running": pool_item.max_running,
        "pool_running_count": pool_item.running_count,
        "supports_resume": pool_item.supports_resume,
        "supports_json_mode": pool_item.supports_json_mode,
        "max_context": pool_item.max_context,
    }
