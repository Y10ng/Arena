"""Bootstrap 结果写回工具。"""

from __future__ import annotations

import logging

from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.tasks.common import (
    record_decision_result,
    record_protocol_write,
    write_conclude_result_with_fact_id,
)
from cairn.server.models import EvidenceRef
from cairn.dispatcher.tasks.conclude_writer import retry_graph_write

LOG = logging.getLogger(__name__)


def write_bootstrap_complete_result(
    client: CairnClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    fact_description: str,
    complete_description: str | None,
    *,
    source: str,
    complete_project: bool = True,
    phase_ms: int,
    total_ms: int | None = None,
    shared_evidence: list[EvidenceRef] | None = None,
    fact_evidence: list[EvidenceRef] | None = None,
    complete_evidence: list[EvidenceRef] | None = None,
    task_run_id: str | None = None,
    slots: list[dict[str, str]] | None = None,
    discovered_surfaces: list[dict[str, object]] | None = None,
    coverage_scope_disposition: dict[str, object] | None = None,
) -> str:
    base_evidence = shared_evidence or []
    conclude_evidence = base_evidence + (fact_evidence or [])
    # 第一步先 conclude 启动 Intent，拿到新 fact id 后才能安全地作为 complete 的 from。
    conclude = write_conclude_result_with_fact_id(
        client,
        project_id,
        intent_id,
        worker_name,
        fact_description,
        source=source,
        phase_ms=phase_ms,
        total_ms=total_ms,
        evidence=conclude_evidence,
        task_run_id=task_run_id,
        discovered_surfaces=discovered_surfaces,
        coverage_scope_disposition=coverage_scope_disposition,
    )
    if conclude.status != "success":
        record_decision_result(
            client,
            project_id,
            task_run_id,
            phase=source,
            outcome="complete" if complete_project else "fact",
            evidence_count=len(base_evidence + (complete_evidence or [])),
            write_status=conclude.status,
        )
        return conclude.status
    # pentest 场景：bootstrap 攻击面枚举产出的 slots 在 fact 写入成功后登记为 pending 槽位。
    # 即使后续 complete 被 coverage_gate 拦下（409），slots 已落地，下轮 reason 即可消费。
    # bootstrap 首轮登记为 core 层（完成门必覆盖）。
    if slots:
        slot_resp = client.upsert_coverage_slots(project_id, slots, source_intent_id=intent_id, tier="core")
        record_protocol_write(
            client,
            project_id,
            task_run_id,
            operation="upsert_coverage_slots",
            phase=source,
            response=slot_resp,
            status="skipped" if slot_resp.status_code in (403, 409) else None,
            intent_id=intent_id,
            evidence_count=len(slots),
        )
        if not slot_resp.ok and slot_resp.status_code not in (403, 409):
            LOG.warning(
                "bootstrap coverage slots upsert failed project=%s intent=%s worker=%s status=%s body=%s",
                project_id,
                intent_id,
                worker_name,
                slot_resp.status_code,
                slot_resp.text,
            )
    if not complete_project:
        record_decision_result(
            client,
            project_id,
            task_run_id,
            phase=source,
            outcome="fact",
            from_ids=["origin"],
            completed_fact_id=conclude.fact_id,
            evidence_count=len(conclude_evidence),
            write_status="success",
        )
        LOG.info(
            "bootstrap fact accepted project=%s intent=%s worker=%s source=%s fact_id=%s phase_ms=%s total_ms=%s",
            project_id,
            intent_id,
            worker_name,
            source,
            conclude.fact_id,
            phase_ms,
            total_ms,
        )
        return "success"
    if conclude.fact_id is None:
        LOG.warning(
            "bootstrap complete deferred because conclude response omitted fact id project=%s intent=%s worker=%s source=%s",
            project_id,
            intent_id,
            worker_name,
            source,
        )
        record_decision_result(
            client,
            project_id,
            task_run_id,
            phase=source,
            outcome="complete",
            evidence_count=len(base_evidence + (complete_evidence or [])),
            write_status="skipped",
            error_message="conclude 响应未返回 fact id，无法继续 complete 写入",
        )
        return "success"

    if not isinstance(complete_description, str) or not complete_description.strip():
        raise ValueError("complete_description is required when complete_project is true")
    complete_from_ids = [conclude.fact_id]
    complete_refs = base_evidence + (complete_evidence or [])
    # complete 有独立的 Server 回执；只重发相同请求，不重新运行 bootstrap。
    response = retry_graph_write(lambda: client.complete_with_evidence(
        project_id,
        complete_from_ids,
        complete_description,
        worker_name,
        complete_refs,
        task_run_id=task_run_id,
    ))
    record_protocol_write(
        client,
        project_id,
        task_run_id,
        operation="complete",
        phase=source,
        response=response,
        status="skipped" if response.status_code in (403, 409) else None,
        intent_id=intent_id,
        fact_id="goal",
        from_ids=complete_from_ids,
        evidence_count=len(complete_refs),
    )
    decision_write_status = "skipped" if response.status_code in (403, 409) else ("success" if response.ok else "failed")
    record_decision_result(
        client,
        project_id,
        task_run_id,
        phase=source,
        outcome="complete",
        from_ids=complete_from_ids,
        completed_fact_id="goal",
        evidence_count=len(complete_refs),
        write_status=decision_write_status,
    )
    if response.status_code in (403, 409):
        LOG.info(
            "bootstrap complete deferred project=%s intent=%s worker=%s source=%s status=%s fact_id=%s",
            project_id,
            intent_id,
            worker_name,
            source,
            response.status_code,
            conclude.fact_id,
        )
        return "success"
    if not response.ok:
        LOG.warning(
            "bootstrap complete write failed project=%s intent=%s worker=%s source=%s fact_id=%s status=%s body=%s",
            project_id,
            intent_id,
            worker_name,
            source,
            conclude.fact_id,
            response.status_code,
            response.text,
        )
        return "server_write_failed"
    if total_ms is None:
        LOG.info(
            "bootstrap completed project=%s intent=%s worker=%s source=%s from=%s phase_ms=%s",
            project_id,
            intent_id,
            worker_name,
            source,
            [conclude.fact_id],
            phase_ms,
        )
    else:
        LOG.info(
            "bootstrap completed project=%s intent=%s worker=%s source=%s from=%s phase_ms=%s total_ms=%s",
            project_id,
            intent_id,
            worker_name,
            source,
            [conclude.fact_id],
            phase_ms,
            total_ms,
        )
    return "success"
