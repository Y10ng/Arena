"""Prompt 效果聚合。"""

from __future__ import annotations

from .common import *

def _prompt_effect_summary(conn) -> MonitoringPromptEffectSummary:
    # parse_error 同时来自 validation_result 和 task_run.error_type，兼容两种上报路径。
    validation_rows = conn.execute(
        """
        SELECT project_id, task_run_id, payload_json
        FROM agent_events
        WHERE kind = 'validation_result'
        """
    ).fetchall()
    parse_error_run_ids = {
        (row["project_id"], row["task_run_id"])
        for row in validation_rows
        if event_payload(row["payload_json"]).get("outcome") == "parse_error"
    }
    rows = conn.execute(
        """
        SELECT ae.task_run_id, ae.project_id, ae.ts, ae.payload_json,
               p.title AS project_title,
               tr.task_type AS task_type,
               tr.worker_name AS worker_name,
               tr.worker_type AS worker_type,
               tr.status AS task_status,
               tr.error_type AS task_error_type
        FROM agent_events ae
        LEFT JOIN task_runs tr ON tr.project_id = ae.project_id AND tr.id = ae.task_run_id
        LEFT JOIN projects p ON p.id = ae.project_id
        WHERE ae.kind = 'prompt_rendered'
        ORDER BY ae.ts DESC, ae.id DESC
        LIMIT ?
        """,
        (PROMPT_EFFECT_EVENT_LIMIT,),
    ).fetchall()
    grouped: dict[tuple[str, str, str], dict[str, object]] = {}
    for row in rows:
        payload = event_payload(row["payload_json"])
        # prompt hash 是版本粒度，避免把完整 prompt 内容放入监控聚合。
        key = (
            sanitized_prompt_label(payload.get("prompt_group")),
            sanitized_prompt_label(payload.get("prompt_name")),
            sanitized_prompt_hash(payload.get("prompt_hash")),
        )
        item = grouped.setdefault(
            key,
            {
                "prompt_group": key[0],
                "prompt_name": key[1],
                "prompt_hash": key[2],
                "task_count": 0,
                "succeeded_count": 0,
                "failed_count": 0,
                "parse_error_count": 0,
                "latest_task_run_id": row["task_run_id"],
                "latest_project_id": row["project_id"],
                "latest_project_title": row["project_title"],
                "latest_task_type": row["task_type"],
                "latest_worker_name": row["worker_name"],
                "latest_worker_type": row["worker_type"],
                "latest_task_status": row["task_status"],
                "latest_rendered_at": row["ts"],
            },
        )
        item["task_count"] = int(item["task_count"]) + 1
        status = str(row["task_status"] or "").strip().lower()
        if status == "succeeded":
            item["succeeded_count"] = int(item["succeeded_count"]) + 1
        elif status in {"failed", "timed_out", "cancelled"}:
            item["failed_count"] = int(item["failed_count"]) + 1
        if (row["project_id"], row["task_run_id"]) in parse_error_run_ids or standard_task_run_error_type(row["task_error_type"]) == "parse_error":
            item["parse_error_count"] = int(item["parse_error_count"]) + 1

    items = [
        MonitoringPromptEffectItem.model_validate(item)
        for item in sorted(
            grouped.values(),
            key=lambda item: (
                str(item.get("latest_rendered_at") or ""),
                str(item.get("prompt_group") or ""),
                str(item.get("prompt_name") or ""),
            ),
            reverse=True,
        )
    ]
    return MonitoringPromptEffectSummary(
        recent_event_limit=PROMPT_EFFECT_EVENT_LIMIT,
        version_count=len(items),
        total_task_count=len(rows),
        latest_rendered_at=rows[0]["ts"] if rows else None,
        items=items,
    )
