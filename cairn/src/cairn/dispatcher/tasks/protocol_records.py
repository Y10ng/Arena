"""Server 协议写回和决策事件记录器。"""

from __future__ import annotations

from .common_base import *
from .write_outcomes import WriteDisposition

def record_protocol_write(
    client: CairnClient,
    project_id: str,
    task_run_id: str | None,
    *,
    operation: str,
    phase: str,
    response: ApiResult,
    status: str | None = None,
    intent_id: str | None = None,
    fact_id: str | None = None,
    from_ids: list[str] | None = None,
    created_id: str | None = None,
    evidence_count: int | None = None,
    disposition: WriteDisposition | None = None,
) -> None:
    if task_run_id is None:
        return
    write_status = status or ("success" if response.ok else "failed")
    # 协议写回事件记录 Server 响应，方便区分模型输出有效但写库失败的情况。
    event_payload: dict[str, object] = {
        "operation": operation,
        "phase": phase,
        "status": write_status,
        "status_code": response.status_code,
        "ok": response.ok,
    }
    # 结构化回执只保存分类和短码，不能让拒绝正文回显证据或秘密。
    if disposition is not None:
        event_payload["disposition"] = disposition.disposition
        if disposition.error_type is not None:
            event_payload["error_type"] = disposition.error_type
        if disposition.safe_code is not None:
            event_payload["safe_code"] = disposition.safe_code
    elif operation != "conclude" and not response.ok and response.text:
        event_payload["response_text"] = preview(response.text, limit=VALIDATION_REASON_LIMIT)
    if intent_id is not None:
        event_payload["intent_id"] = intent_id
    if fact_id is not None:
        event_payload["fact_id"] = fact_id
    if from_ids is not None:
        event_payload["from_ids"] = from_ids
    if created_id is not None:
        event_payload["created_id"] = created_id
    if evidence_count is not None:
        event_payload["evidence_count"] = evidence_count

    event_response = client.create_agent_event(
        project_id,
        task_run_id=task_run_id,
        kind="protocol_write",
        payload=event_payload,
    )
    if event_response.ok:
        return
    LOG.warning(
        "protocol write event create failed project=%s task_run=%s operation=%s phase=%s status=%s response_status=%s",
        project_id,
        task_run_id,
        operation,
        phase,
        write_status,
        event_response.status_code,
    )


def record_decision_result(
    client: CairnClient,
    project_id: str,
    task_run_id: str | None,
    *,
    phase: str,
    outcome: str,
    rationale: str | None = None,
    from_ids: list[str] | None = None,
    created_intent_ids: list[str] | None = None,
    completed_fact_id: str | None = None,
    evidence_count: int | None = None,
    rejected_options: list[dict[str, object]] | None = None,
    confidence: float | None = None,
    blocked_by: list[str] | None = None,
    depends_on_targets: list[str] | None = None,
    write_status: str | None = None,
    error_message: str | None = None,
) -> None:
    if task_run_id is None:
        return
    # decision_result 是高层语义审计，和 validation_result/protocol_write 形成三段链路。
    event_payload: dict[str, object] = {
        "phase": phase,
        "outcome": outcome,
    }
    if rationale:
        event_payload["rationale"] = _compact_validation_text(rationale, limit=1200)
    if from_ids is not None:
        event_payload["from_ids"] = from_ids
    if created_intent_ids is not None:
        event_payload["created_intent_ids"] = created_intent_ids
    if completed_fact_id is not None:
        event_payload["completed_fact_id"] = completed_fact_id
    if evidence_count is not None:
        event_payload["evidence_count"] = evidence_count
    if rejected_options:
        event_payload["rejected_options"] = [
            {
                "description": _compact_validation_text(option.get("description"), limit=400),
                "reason": _compact_validation_text(option.get("reason"), limit=400),
            }
            for option in rejected_options
            if isinstance(option, dict)
        ]
    if confidence is not None:
        event_payload["confidence"] = confidence
    if blocked_by:
        event_payload["blocked_by"] = [
            _compact_validation_text(item, limit=160)
            for item in blocked_by
            if isinstance(item, str) and item.strip()
        ]
    if depends_on_targets:
        event_payload["depends_on_targets"] = [
            _compact_validation_text(item, limit=160)
            for item in depends_on_targets
            if isinstance(item, str) and item.strip()
        ]
    if write_status is not None:
        event_payload["write_status"] = write_status
    if error_message:
        event_payload["error_message"] = _compact_validation_text(error_message, limit=800)

    event_response = client.create_agent_event(
        project_id,
        task_run_id=task_run_id,
        kind="decision_result",
        payload=event_payload,
    )
    if event_response.ok:
        return
    LOG.warning(
        "decision result event create failed project=%s task_run=%s outcome=%s phase=%s response_status=%s body=%s",
        project_id,
        task_run_id,
        outcome,
        phase,
        event_response.status_code,
        event_response.text,
    )


def update_task_run_session(
    client: CairnClient,
    project_id: str,
    task_run_id: str | None,
    session_id: str | None,
) -> None:
    if task_run_id is None or session_id is None:
        return
    # session id 一拿到就回写，后续 conclude/resume 失败时仍能从审计中排查。
    response = client.update_task_run(
        project_id,
        task_run_id,
        status="running",
        session_id=session_id,
    )
    if response.ok:
        return
    if response.status_code in (403, 404):
        LOG.info(
            "task run session update skipped project=%s task_run=%s status=%s",
            project_id,
            task_run_id,
            response.status_code,
        )
        return
    LOG.warning(
        "task run session update failed project=%s task_run=%s status=%s body=%s",
        project_id,
        task_run_id,
        response.status_code,
        response.text,
    )
