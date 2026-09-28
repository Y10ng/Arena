"""结果审计和 decision 聚合。"""

from __future__ import annotations

from .common import *

def _result_audit_summary(conn) -> MonitoringResultAuditSummary:
    # 审计 summary 只看近期结果事件，聚焦“输出是否有效、协议是否写入、决策是否推进图谱”。
    rows = conn.execute(
        """
        SELECT project_id, ts, kind, payload_json
        FROM agent_events
        WHERE kind IN ('validation_result', 'decision_result', 'protocol_write')
        ORDER BY ts DESC, id DESC
        LIMIT ?
        """,
        (RESULT_AUDIT_EVENT_LIMIT,),
    ).fetchall()
    validation = {
        "valid_count": 0,
        "invalid_count": 0,
        "rejected_count": 0,
        "unknown_count": 0,
    }
    protocol_write = {
        "success_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "unknown_count": 0,
    }
    decision = {
        "complete_count": 0,
        "fact_count": 0,
        "intents_count": 0,
        "noop_count": 0,
        "unknown_count": 0,
        "low_confidence_count": 0,
        "blocked_count": 0,
        "target_dependency_count": 0,
        "created_intent_count": 0,
        "concluded_created_intent_count": 0,
        "goal_path_created_intent_count": 0,
        "open_created_intent_count": 0,
        "missing_created_intent_count": 0,
    }
    created_intent_refs: set[tuple[str, str]] = set()
    validation_total = 0
    decision_total = 0
    protocol_total = 0
    latest_event_at = rows[0]["ts"] if rows else None

    for row in rows:
        payload = event_payload(row["payload_json"])
        if row["kind"] == "validation_result":
            validation_total += 1
            status = normalized_payload_status(payload.get("status"))
            if status in {"valid", "invalid", "rejected"}:
                validation[f"{status}_count"] += 1
            else:
                validation["unknown_count"] += 1
            continue

        if row["kind"] == "decision_result":
            decision_total += 1
            outcome = _decision_result_outcome(payload)
            if outcome in {"complete", "fact", "intents", "noop"}:
                decision[f"{outcome}_count"] += 1
            else:
                decision["unknown_count"] += 1
            confidence = _decision_confidence(payload.get("confidence"))
            if confidence is not None and confidence < DECISION_LOW_CONFIDENCE_THRESHOLD:
                decision["low_confidence_count"] += 1
            if _has_decision_list_items(payload.get("blocked_by")):
                decision["blocked_count"] += 1
            if _has_decision_list_items(payload.get("depends_on_targets")):
                decision["target_dependency_count"] += 1
            # created_intent_ids 需要回查当前 graph，判断这些决策后续是否被收敛或进入 goal 路径。
            created_intent_refs.update(_decision_created_intent_refs(row["project_id"], payload))
            continue

        protocol_total += 1
        status = _protocol_write_status(payload)
        if status in {"success", "failed", "skipped"}:
            protocol_write[f"{status}_count"] += 1
        else:
            protocol_write["unknown_count"] += 1

    decision.update(_decision_created_intent_counts(conn, created_intent_refs))
    return MonitoringResultAuditSummary(
        recent_event_limit=RESULT_AUDIT_EVENT_LIMIT,
        event_count=len(rows),
        latest_event_at=latest_event_at,
        validation=MonitoringValidationResultSummary(
            total_count=validation_total,
            **validation,
        ),
        protocol_write=MonitoringProtocolWriteSummary(
            total_count=protocol_total,
            **protocol_write,
        ),
        decision=MonitoringDecisionResultSummary(
            total_count=decision_total,
            **decision,
        ),
    )


def _protocol_write_status(payload: dict) -> str:
    status = normalized_payload_status(payload.get("status"))
    if status in {"success", "failed", "skipped"}:
        return status
    ok = payload.get("ok")
    if ok is True:
        return "success"
    if ok is False:
        return "failed"
    return status


def _decision_result_outcome(payload: dict) -> str:
    outcome = normalized_payload_status(payload.get("outcome"))
    if outcome in {"complete", "fact", "intents", "noop"}:
        return outcome
    return "unknown"


def _decision_created_intent_refs(project_id: str, payload: dict) -> set[tuple[str, str]]:
    value = payload.get("created_intent_ids")
    if not isinstance(value, list):
        return set()
    refs: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        intent_id = item.strip()
        if intent_id:
            refs.add((project_id, intent_id))
    return refs


def _decision_created_intent_counts(conn, refs: set[tuple[str, str]]) -> dict[str, int]:
    # intent 可能被后续任务关闭、仍开放，或因清理/竞态缺失；监控分开计数而不修复数据。
    counts = {
        "created_intent_count": len(refs),
        "concluded_created_intent_count": 0,
        "goal_path_created_intent_count": 0,
        "open_created_intent_count": 0,
        "missing_created_intent_count": 0,
    }
    goal_path_intents = _completed_goal_path_intent_refs(conn, {project_id for project_id, _ in refs})
    for project_id, intent_id in sorted(refs):
        row = conn.execute(
            """
            SELECT to_fact_id
            FROM intents
            WHERE project_id = ? AND id = ?
            """,
            (project_id, intent_id),
        ).fetchone()
        if row is None:
            counts["missing_created_intent_count"] += 1
        elif row["to_fact_id"] is None:
            counts["open_created_intent_count"] += 1
        else:
            counts["concluded_created_intent_count"] += 1
            if (project_id, intent_id) in goal_path_intents:
                counts["goal_path_created_intent_count"] += 1
    return counts


def _completed_goal_path_intent_refs(conn, project_ids: set[str]) -> set[tuple[str, str]]:
    refs: set[tuple[str, str]] = set()
    for project_id in sorted(project_ids):
        project = conn.execute(
            "SELECT status FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if project is None or project["status"] != "completed":
            continue
        rows = conn.execute(
            """
            SELECT i.id, i.to_fact_id, s.fact_id
            FROM intents i
            LEFT JOIN intent_sources s
              ON s.project_id = i.project_id
             AND s.intent_id = i.id
            WHERE i.project_id = ?
              AND i.to_fact_id IS NOT NULL
            """,
            (project_id,),
        ).fetchall()
        by_target: dict[str, set[str]] = {}
        for row in rows:
            by_target.setdefault(row["to_fact_id"], set()).add(row["id"])
        # 从 goal 反向沿 intent_sources 走回事实链，标出真正参与完成路径的 intent。
        stack = ["goal"]
        seen_facts: set[str] = set()
        while stack:
            fact_id = stack.pop()
            if fact_id in seen_facts:
                continue
            seen_facts.add(fact_id)
            for intent_id in sorted(by_target.get(fact_id, set())):
                if (project_id, intent_id) in refs:
                    continue
                refs.add((project_id, intent_id))
                for source in rows:
                    if source["id"] == intent_id and source["fact_id"]:
                        stack.append(source["fact_id"])
    return refs


def _decision_confidence(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        confidence = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            confidence = float(text)
        except ValueError:
            return None
    else:
        return None
    if 0 <= confidence <= 1:
        return confidence
    return None


def _has_decision_list_items(value: object) -> bool:
    return isinstance(value, list) and any(isinstance(item, str) and item.strip() for item in value)
