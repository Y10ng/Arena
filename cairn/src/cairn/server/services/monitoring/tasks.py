"""Task run 监控读模型转换。"""

from __future__ import annotations

from cairn.server.auth import RequestPrincipal, verification_project_access_clause

from .common import *

def _active_task_summaries(
    conn,
    now: str,
    *,
    long_running_seconds: int,
    intent_timeout_seconds: int | None,
    reason_timeout_seconds: int | None,
    tenant_id: str | None = None,
    principal: RequestPrincipal | None = None,
) -> tuple[list[MonitoringTaskRun], list[MonitoringTaskRun], list[MonitoringTaskRun]]:
    # active task 同时读 intent/reason 心跳，监控页能区分进程运行和租约失联。
    tenant_clause = " AND p.tenant_id = ?" if tenant_id is not None else ""
    params = (tenant_id,) if tenant_id is not None else ()
    if principal is not None:
        access_clause, access_params = verification_project_access_clause(principal)
        tenant_clause += f" AND ({access_clause})"
        params += access_params
    running_rows = conn.execute(
        f"""
        SELECT tr.*, p.title AS project_title, p.status AS project_status,
               i.worker AS intent_worker,
               i.last_heartbeat_at AS intent_last_heartbeat_at,
               p.reason_worker AS reason_worker,
               p.reason_last_heartbeat_at AS reason_last_heartbeat_at,
               COALESCE(ac.artifact_count, 0) AS artifact_count,
               CAST(MAX(0, (julianday(?) - julianday(tr.started_at)) * 86400) AS INTEGER) AS duration_seconds,
               CASE
                   WHEN i.last_heartbeat_at IS NULL THEN NULL
                   ELSE CAST(MAX(0, (julianday(?) - julianday(i.last_heartbeat_at)) * 86400) AS INTEGER)
               END AS intent_heartbeat_age_seconds,
               CASE
                   WHEN p.reason_last_heartbeat_at IS NULL THEN NULL
                   ELSE CAST(MAX(0, (julianday(?) - julianday(p.reason_last_heartbeat_at)) * 86400) AS INTEGER)
               END AS reason_heartbeat_age_seconds
        FROM task_runs tr
        JOIN projects p ON p.id = tr.project_id
        LEFT JOIN intents i ON i.project_id = tr.project_id AND i.id = tr.intent_id
        LEFT JOIN (
            SELECT project_id, task_run_id, COUNT(*) AS artifact_count
            FROM artifacts
            GROUP BY project_id, task_run_id
        ) ac ON ac.project_id = tr.project_id AND ac.task_run_id = tr.id
        WHERE tr.status = 'running'{tenant_clause}
        ORDER BY tr.started_at, tr.project_id, tr.id
        """,
        (now, now, now, *params),
    ).fetchall()
    running_tasks = [
        _task_run_summary(
            row,
            intent_timeout_seconds=intent_timeout_seconds,
            reason_timeout_seconds=reason_timeout_seconds,
        )
        for row in running_rows
    ]
    long_running_tasks = [
        task
        for task in running_tasks
        if task.duration_seconds is not None and task.duration_seconds >= long_running_seconds
    ]
    queued_rows = conn.execute(
        f"""
        SELECT tr.*, p.title AS project_title, p.status AS project_status,
               COALESCE(ac.artifact_count, 0) AS artifact_count,
               NULL AS duration_seconds
        FROM task_runs tr
        JOIN projects p ON p.id = tr.project_id
        LEFT JOIN (
            SELECT project_id, task_run_id, COUNT(*) AS artifact_count
            FROM artifacts
            GROUP BY project_id, task_run_id
        ) ac ON ac.project_id = tr.project_id AND ac.task_run_id = tr.id
        WHERE tr.status = 'queued'{tenant_clause}
        ORDER BY p.created_at, tr.project_id, tr.id
        LIMIT 10
        """,
        params,
    ).fetchall()
    queued_tasks = [_task_run_summary(row, now=now) for row in queued_rows]
    return running_tasks, long_running_tasks, queued_tasks


def _recent_failure_summaries(
    conn,
    now: str,
    *,
    tenant_id: str | None = None,
    principal: RequestPrincipal | None = None,
) -> list[MonitoringTaskRun]:
    tenant_clause = " AND p.tenant_id = ?" if tenant_id is not None else ""
    params = (tenant_id,) if tenant_id is not None else ()
    if principal is not None:
        access_clause, access_params = verification_project_access_clause(principal)
        tenant_clause += f" AND ({access_clause})"
        params += access_params
    rows = conn.execute(
        f"""
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
        WHERE tr.status IN ('failed', 'timed_out', 'cancelled'){tenant_clause}
        ORDER BY COALESCE(tr.ended_at, tr.started_at, tr.id) DESC, tr.project_id, tr.id
        LIMIT 20
        """,
        (now, *params),
    ).fetchall()
    return [_task_run_summary(row) for row in rows]


def _task_run_summary(
    row,
    *,
    intent_timeout_seconds: int | None = None,
    reason_timeout_seconds: int | None = None,
    now: str | None = None,
) -> MonitoringTaskRun:
    runtime_context = task_run_runtime_context(row)
    queued_at = runtime_context_text(runtime_context.get("queued_at"))
    # queue_wait_seconds 优先使用调度写入值，缺失时按当前/开始时间即时推算。
    heartbeat = _task_run_heartbeat(row, intent_timeout_seconds, reason_timeout_seconds)
    return MonitoringTaskRun(
        id=row["id"],
        project_id=row["project_id"],
        project_title=row["project_title"],
        project_status=row["project_status"],
        intent_id=row["intent_id"],
        task_type=row["task_type"],
        worker_name=row["worker_name"],
        worker_type=row["worker_type"],
        status=row["status"],
        session_id=row["session_id"],
        queued_at=queued_at,
        queue_wait_seconds=_task_run_queue_wait_seconds(row, runtime_context, queued_at, now),
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        duration_seconds=row["duration_seconds"],
        error_type=standard_task_run_error_type(row["error_type"]),
        error_message=runtime_context_text(row["error_message"]),
        artifact_count=row["artifact_count"] or 0,
        artifact_root=runtime_context.get("artifact_root"),
        **heartbeat,
    )


def _task_run_queue_wait_seconds(
    row,
    runtime_context: dict[str, object],
    queued_at: str | None,
    now: str | None,
) -> int | None:
    value = integer_payload_value(runtime_context.get("queue_wait_seconds"))
    if value is not None and value >= 0:
        return value
    if row["status"] == "queued":
        return timestamp_delta_seconds(queued_at, now)
    return timestamp_delta_seconds(queued_at, row["started_at"])


def _task_run_heartbeat(
    row,
    intent_timeout_seconds: int | None,
    reason_timeout_seconds: int | None,
) -> dict[str, object]:
    source = "none"
    heartbeat_at = None
    heartbeat_age_seconds = None
    heartbeat_timeout_seconds = None
    heartbeat_worker = None

    if row["status"] == "running" and row["task_type"] == "reason":
        # reason 没有 intent_id，心跳来自 projects 表的项目级租约字段。
        source = "reason"
        heartbeat_at = _row_value(row, "reason_last_heartbeat_at")
        heartbeat_age_seconds = _row_value(row, "reason_heartbeat_age_seconds")
        heartbeat_timeout_seconds = reason_timeout_seconds
        heartbeat_worker = _row_value(row, "reason_worker")
    elif row["status"] == "running" and row["intent_id"] is not None:
        # bootstrap/explore 等 intent 任务使用 intent 行上的 worker/heartbeat 字段。
        source = "intent"
        heartbeat_at = _row_value(row, "intent_last_heartbeat_at")
        heartbeat_age_seconds = _row_value(row, "intent_heartbeat_age_seconds")
        heartbeat_timeout_seconds = intent_timeout_seconds
        heartbeat_worker = _row_value(row, "intent_worker")

    return {
        "heartbeat_source": source,
        "heartbeat_status": _heartbeat_status(
            source,
            task_worker=row["worker_name"],
            heartbeat_worker=heartbeat_worker,
            heartbeat_at=heartbeat_at,
            heartbeat_age_seconds=heartbeat_age_seconds,
            heartbeat_timeout_seconds=heartbeat_timeout_seconds,
        ),
        "heartbeat_at": heartbeat_at,
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "heartbeat_timeout_seconds": heartbeat_timeout_seconds,
        "heartbeat_worker": heartbeat_worker,
    }


def _heartbeat_status(
    source: str,
    *,
    task_worker: str,
    heartbeat_worker: object,
    heartbeat_at: object,
    heartbeat_age_seconds: object,
    heartbeat_timeout_seconds: object,
) -> str:
    if source == "none":
        return "not_tracked"
    if heartbeat_worker is None:
        return "missing"
    if heartbeat_at is None:
        return "missing"
    if heartbeat_worker != task_worker:
        return "stale"
    if (
        isinstance(heartbeat_age_seconds, int)
        and isinstance(heartbeat_timeout_seconds, int)
        and heartbeat_age_seconds > heartbeat_timeout_seconds
    ):
        return "stale"
    return "active"


def _row_value(row, key: str, default=None):
    return row[key] if key in row.keys() else default
