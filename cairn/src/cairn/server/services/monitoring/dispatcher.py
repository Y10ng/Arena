"""Dispatcher heartbeat 与汇总聚合。"""

from __future__ import annotations

from .common import *

from cairn.server.services.infra import (
    DISPATCHER_HEARTBEAT_STALE_SECONDS,
    dispatcher_effective_status,
)
from .time import _remaining_seconds_until

def _dispatcher_summary(conn, now: str) -> MonitoringDispatcherSummary:
    # dispatcher summary 混合项目/任务统计和最新心跳，给控制台展示“系统当前是否在动”。
    totals = conn.execute(
        """
        SELECT
            COUNT(*) AS project_count,
            SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active_project_count
        FROM projects
        """
    ).fetchone()
    task_totals = conn.execute(
        """
        SELECT
            SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running_task_count,
            SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued_task_count,
            COUNT(DISTINCT CASE WHEN status IN ('queued', 'running') THEN project_id END) AS running_project_count
        FROM task_runs
        """
    ).fetchone()
    project_rows = conn.execute(
        """
        SELECT *
        FROM (
            SELECT p.id, p.title, p.status, p.reason_worker, p.created_at,
                   SUM(CASE WHEN tr.status = 'running' THEN 1 ELSE 0 END) AS running_task_count,
                   SUM(CASE WHEN tr.status = 'queued' THEN 1 ELSE 0 END) AS queued_task_count
            FROM projects p
            LEFT JOIN task_runs tr ON tr.project_id = p.id AND tr.status IN ('queued', 'running')
            GROUP BY p.id, p.title, p.status, p.reason_worker, p.created_at
        ) AS project_concurrency
        WHERE running_task_count > 0 OR queued_task_count > 0 OR reason_worker IS NOT NULL
        ORDER BY created_at, id
        """
    ).fetchall()
    dispatcher_row = _latest_dispatcher_row(conn)
    dispatcher_status = dispatcher_row["status"] if dispatcher_row is not None else None
    dispatcher_updated_at = dispatcher_row["updated_at"] if dispatcher_row is not None else None
    heartbeat_age_seconds = timestamp_delta_seconds(dispatcher_updated_at, now)
    effective_status = dispatcher_effective_status(conn, now=now)
    heartbeat_stale = effective_status == "stale"
    # effective_status 只在展示层标记 stale，不改写 dispatcher 上报的 last_status。
    return MonitoringDispatcherSummary(
        project_count=totals["project_count"] or 0,
        active_project_count=totals["active_project_count"] or 0,
        running_project_count=task_totals["running_project_count"] or 0,
        running_task_count=task_totals["running_task_count"] or 0,
        queued_task_count=task_totals["queued_task_count"] or 0,
        last_dispatcher_id=dispatcher_row["id"] if dispatcher_row is not None else None,
        last_status=dispatcher_status,
        effective_status=effective_status,
        started_at=dispatcher_row["started_at"] if dispatcher_row is not None else None,
        config_hash=dispatcher_row["config_hash"] if dispatcher_row is not None else None,
        host=dispatcher_row["host"] if dispatcher_row is not None else None,
        config_path=dispatcher_row["config_path"] if dispatcher_row is not None else None,
        config_mtime=dispatcher_row["config_mtime"] if dispatcher_row is not None else None,
        config_sha256=dispatcher_row["config_sha256"] if dispatcher_row is not None else None,
        runtime=_dispatcher_object(dispatcher_row["runtime_json"] if dispatcher_row is not None else None),
        container=_dispatcher_object(dispatcher_row["container_json"] if dispatcher_row is not None else None),
        server_reachable_path=dispatcher_row["server_reachable_path"] if dispatcher_row is not None else None,
        last_loop_started_at=dispatcher_row["last_loop_started_at"] if dispatcher_row is not None else None,
        last_loop_finished_at=dispatcher_row["last_loop_finished_at"] if dispatcher_row is not None else None,
        last_loop_duration_ms=dispatcher_row["last_loop_duration_ms"] if dispatcher_row is not None else None,
        last_error_message=runtime_context_text(dispatcher_row["error_message"]) if dispatcher_row is not None else None,
        updated_at=dispatcher_updated_at,
        heartbeat_age_seconds=heartbeat_age_seconds,
        heartbeat_stale=heartbeat_stale,
        worker_cooldowns=_dispatcher_window_items(
            dispatcher_row["worker_cooldowns_json"] if dispatcher_row is not None else None,
            DispatcherWorkerCooldown,
            now=now,
            active_only=True,
        ),
        worker_rejections=_dispatcher_window_items(
            dispatcher_row["worker_rejections_json"] if dispatcher_row is not None else None,
            DispatcherWorkerRejection,
            now=now,
            active_only=True,
        ),
        worker_pool=_dispatcher_window_items(
            dispatcher_row["worker_pool_json"] if dispatcher_row is not None else None,
            DispatcherWorkerPoolItem,
        ),
        container_images=_dispatcher_string_items(
            dispatcher_row["container_images_json"] if dispatcher_row is not None else None,
        ),
        cap_baseline=_dispatcher_string_items(
            dispatcher_row["cap_baseline_json"] if dispatcher_row is not None else None,
        ),
        declared_inventory=_dispatcher_object(
            dispatcher_row["declared_inventory_json"] if dispatcher_row is not None else None,
        ),
        projects=[
            MonitoringProjectConcurrency(
                project_id=row["id"],
                project_title=row["title"],
                project_status=row["status"],
                running_task_count=row["running_task_count"] or 0,
                queued_task_count=row["queued_task_count"] or 0,
                active_reason_worker=row["reason_worker"],
            )
            for row in project_rows
        ],
    )


def _dispatcher_status(row) -> DispatcherHeartbeatStatus:
    return DispatcherHeartbeatStatus(
        dispatcher_id=row["id"],
        status=row["status"],
        started_at=row["started_at"],
        config_hash=row["config_hash"],
        host=row["host"],
        config_path=row["config_path"],
        config_mtime=row["config_mtime"],
        config_sha256=row["config_sha256"],
        runtime=_dispatcher_object(row["runtime_json"]),
        container=_dispatcher_object(row["container_json"]),
        server_reachable_path=row["server_reachable_path"],
        loop_started_at=row["last_loop_started_at"],
        loop_finished_at=row["last_loop_finished_at"],
        loop_duration_ms=row["last_loop_duration_ms"],
        running_task_count=row["running_task_count"] or 0,
        active_project_count=row["active_project_count"] or 0,
        worker_cooldowns=_dispatcher_window_items(row["worker_cooldowns_json"], DispatcherWorkerCooldown),
        worker_rejections=_dispatcher_window_items(row["worker_rejections_json"], DispatcherWorkerRejection),
        worker_pool=_dispatcher_window_items(row["worker_pool_json"], DispatcherWorkerPoolItem),
        container_images=_dispatcher_string_items(row["container_images_json"]),
        cap_baseline=_dispatcher_string_items(row["cap_baseline_json"]),
        declared_inventory=_dispatcher_object(row["declared_inventory_json"]),
        error_message=runtime_context_text(row["error_message"]),
        updated_at=row["updated_at"],
    )


def _latest_dispatcher_row(conn):
    return conn.execute(
        """
        SELECT *
        FROM dispatcher_status
        ORDER BY COALESCE(last_loop_finished_at, updated_at) DESC, updated_at DESC, id
        LIMIT 1
        """
    ).fetchone()


def _dispatcher_window_items(raw: str | None, model, *, now: str | None = None, active_only: bool = False):
    try:
        payload = loads(raw or "[]")
    except (JSONDecodeError, TypeError):
        return []
    if not isinstance(payload, list):
        return []
    items = []
    for item in payload:
        try:
            parsed = model.model_validate(item)
        except (TypeError, ValueError):
            # 心跳负载里的单项坏数据不应拖垮整个监控页。
            continue
        if active_only:
            parsed = _active_dispatcher_window_item(parsed, now)
            if parsed is None:
                continue
        items.append(parsed)
    return items


def _dispatcher_string_items(raw: str | None) -> list[str]:
    try:
        payload = loads(raw or "[]")
    except (JSONDecodeError, TypeError):
        return []
    if not isinstance(payload, list):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for item in payload:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
    return items


def _dispatcher_object(raw: str | None) -> dict[str, object]:
    try:
        payload = loads(raw or "{}")
    except (JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _active_dispatcher_window_item(item, now: str | None):
    retry_after_seconds = getattr(item, "retry_after_seconds", 0) or 0
    retry_until_at = getattr(item, "retry_until_at", None)
    if now is None or not retry_until_at:
        return item
    remaining_seconds = _remaining_seconds_until(now, retry_until_at)
    if remaining_seconds is None:
        return item
    if remaining_seconds <= 0:
        return None
    # 返回时刷新剩余秒数，让前端不用基于 retry_until_at 再做本地时间推算。
    return item.model_copy(update={"retry_after_seconds": remaining_seconds})

def record_dispatcher_heartbeat(conn, body: DispatcherHeartbeatRequest) -> DispatcherHeartbeatStatus:
    now = utcnow()
    error_message = runtime_context_text(body.error_message)
    # heartbeat 以 dispatcher_id upsert，保留最后一次配置/运行态快照供 infra 和 monitoring 共用。
    conn.execute(
        """
        INSERT INTO dispatcher_status
            (id, status, started_at, config_hash, host, config_path, config_mtime, config_sha256,
             runtime_json, container_json, server_reachable_path, last_loop_started_at, last_loop_finished_at,
             last_loop_duration_ms, running_task_count, active_project_count,
             worker_cooldowns_json, worker_rejections_json, worker_pool_json,
             container_images_json, cap_baseline_json, declared_inventory_json,
             error_message, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            status = excluded.status,
            started_at = COALESCE(excluded.started_at, dispatcher_status.started_at),
            config_hash = excluded.config_hash,
            host = excluded.host,
            config_path = excluded.config_path,
            config_mtime = excluded.config_mtime,
            config_sha256 = excluded.config_sha256,
            runtime_json = excluded.runtime_json,
            container_json = excluded.container_json,
            server_reachable_path = excluded.server_reachable_path,
            last_loop_started_at = excluded.last_loop_started_at,
            last_loop_finished_at = excluded.last_loop_finished_at,
            last_loop_duration_ms = excluded.last_loop_duration_ms,
            running_task_count = excluded.running_task_count,
            active_project_count = excluded.active_project_count,
            worker_cooldowns_json = excluded.worker_cooldowns_json,
            worker_rejections_json = excluded.worker_rejections_json,
            worker_pool_json = excluded.worker_pool_json,
            container_images_json = excluded.container_images_json,
            cap_baseline_json = excluded.cap_baseline_json,
            declared_inventory_json = excluded.declared_inventory_json,
            error_message = excluded.error_message,
            updated_at = excluded.updated_at
        """,
        (
            body.dispatcher_id,
            body.status,
            body.started_at,
            body.config_hash,
            body.host,
            body.config_path,
            body.config_mtime,
            body.config_sha256,
            dumps(body.runtime, ensure_ascii=False, sort_keys=True),
            dumps(body.container, ensure_ascii=False, sort_keys=True),
            body.server_reachable_path,
            body.loop_started_at,
            body.loop_finished_at,
            body.loop_duration_ms,
            body.running_task_count,
            body.active_project_count,
            dumps([item.model_dump(mode="json") for item in body.worker_cooldowns], ensure_ascii=False),
            dumps([item.model_dump(mode="json") for item in body.worker_rejections], ensure_ascii=False),
            dumps([item.model_dump(mode="json") for item in body.worker_pool], ensure_ascii=False),
            dumps(body.container_images, ensure_ascii=False),
            dumps(body.cap_baseline, ensure_ascii=False) if body.cap_baseline is not None else None,
            dumps(body.declared_inventory, ensure_ascii=False, sort_keys=True),
            error_message,
            now,
        ),
    )
    row = conn.execute(
        "SELECT * FROM dispatcher_status WHERE id = ?",
        (body.dispatcher_id,),
    ).fetchone()
    return _dispatcher_status(row)
