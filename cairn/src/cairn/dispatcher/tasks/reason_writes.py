"""Reason 决策写回工具。"""

from __future__ import annotations

import logging

from cairn.assessment_contracts import DepthReviewDecision
from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.contracts import extract_coverage_slots, extract_evidence_refs
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.tasks.common import (
    ProcessArtifactRecord,
    UnresolvedArtifactEvidenceError,
    build_discovered_surfaces_for_conclude,
    record_decision_result,
    record_protocol_write,
    resolve_declared_artifact_evidence_refs,
)
from cairn.dispatcher.tasks.conclude_writer import retry_graph_write
from cairn.dispatcher.tasks.write_outcomes import classify_write_response
from cairn.dispatcher.tasks.reason_metadata import (
    intent_from_ids,
    reason_decision_metadata,
    reason_rationale,
    reason_rejected_options,
)

LOG = logging.getLogger(__name__)


def _dominant_write_error(error_types: list[str]) -> str | None:
    """部分写入失败时保留最需要人工区分的稳定错误类别。"""
    for candidate in (
        "receipt_conflict",
        "stale_business_state",
        "policy_denied",
        "request_contract",
        "business_acceptance",
        "server_write_failed",
    ):
        if candidate in error_types:
            return candidate
    return error_types[0] if error_types else None


def write_reason_decision(
    client: CairnClient,
    project_id: str,
    worker: WorkerConfig,
    task_run_id: str | None,
    *,
    kind: str,
    data,
    payload: object,
    evidence: ProcessArtifactRecord,
    declared_artifacts: ProcessArtifactRecord,
    execute_ms: int,
    total_ms: int,
    abandon: list[str] | None = None,
    surface_manifest: bool = False,
) -> str:
    # Reason 输出先拆出审计元数据，再按 decision kind 进入不同写回路径。
    decision_rationale = reason_rationale(payload)
    decision_rejected_options = reason_rejected_options(payload)
    decision_metadata = reason_decision_metadata(payload)
    if kind == "depth_review":
        # 必须在任何 abandon/Surface 写入前分流，避免改变摘要输入或掩盖未完成义务。
        return _write_depth_review_decision(
            client,
            project_id,
            worker,
            task_run_id,
            data=data,
            declared_artifacts=declared_artifacts,
            rationale=decision_rationale,
            rejected_options=decision_rejected_options,
            metadata=decision_metadata,
        )
    # 先执行 abandon：在 complete/intents 之前剪掉不再追的分支（complete 硬门要求无 open intent）。
    if abandon:
        abandon_error = _write_abandon_decisions(
            client,
            project_id,
            worker,
            task_run_id,
            intent_ids=abandon,
            rationale=decision_rationale,
            execute_ms=execute_ms,
            total_ms=total_ms,
        )
        if abandon_error is not None:
            return abandon_error
    if surface_manifest:
        # Reason 不创建 Fact；其 Surface 观察直接绑定本轮 TaskRun 与已采集 Evidence。
        discovered_surfaces = build_discovered_surfaces_for_conclude(
            payload, declared_artifacts
        )
        if discovered_surfaces:
            surface_resp = retry_graph_write(
                lambda: client.register_discovered_surfaces(
                    project_id,
                    discovered_surfaces,
                    evidence=evidence.refs + declared_artifacts.refs,
                    source_task_run_id=task_run_id,
                )
            )
            record_protocol_write(
                client,
                project_id,
                task_run_id,
                operation="register_discovered_surfaces",
                phase="reason_execute",
                response=surface_resp,
                evidence_count=len(discovered_surfaces),
            )
            if not surface_resp.ok:
                disposition = classify_write_response(surface_resp)
                LOG.warning(
                    "reason discovered surfaces registration failed project=%s worker=%s status=%s code=%s",
                    project_id,
                    worker.name,
                    surface_resp.status_code,
                    disposition.safe_code,
                )
                return disposition.error_type or "business_acceptance"
    else:
        # legacy/pentest 继续登记 discovered_slots，不改变冻结项目的协议。
        discovered_slots = extract_coverage_slots(payload, field="discovered_slots")
        if discovered_slots:
            slot_resp = retry_graph_write(
                lambda: client.upsert_coverage_slots(
                    project_id, discovered_slots, source_intent_id=None
                )
            )
            record_protocol_write(
                client,
                project_id,
                task_run_id,
                operation="upsert_coverage_slots",
                phase="reason_execute",
                response=slot_resp,
                evidence_count=len(discovered_slots),
            )
            if not slot_resp.ok:
                disposition = classify_write_response(slot_resp)
                LOG.warning(
                    "reason discovered slots upsert failed project=%s worker=%s status=%s code=%s",
                    project_id,
                    worker.name,
                    slot_resp.status_code,
                    disposition.safe_code,
                )
                return disposition.error_type or "business_acceptance"
    if kind == "complete":
        return _write_complete_decision(
            client,
            project_id,
            worker,
            task_run_id,
            data=data,
            evidence=evidence,
            declared_artifacts=declared_artifacts,
            payload=payload,
            execute_ms=execute_ms,
            total_ms=total_ms,
            rationale=decision_rationale,
            rejected_options=decision_rejected_options,
            metadata=decision_metadata,
        )
    if kind == "intents":
        return _write_intent_decisions(
            client,
            project_id,
            worker,
            task_run_id,
            intents=data,
            execute_ms=execute_ms,
            total_ms=total_ms,
            rationale=decision_rationale,
            rejected_options=decision_rejected_options,
            metadata=decision_metadata,
        )
    LOG.info(
        "reason finished without graph change project=%s worker=%s execute_ms=%s total_ms=%s",
        project_id,
        worker.name,
        execute_ms,
        total_ms,
    )
    record_decision_result(
        client,
        project_id,
        task_run_id,
        phase="reason_execute",
        outcome="noop",
        rationale=decision_rationale,
        rejected_options=decision_rejected_options,
        **decision_metadata,
        write_status="success",
    )
    return "success"


def _write_depth_review_decision(
    client: CairnClient,
    project_id: str,
    worker: WorkerConfig,
    task_run_id: str | None,
    *,
    data,
    declared_artifacts: ProcessArtifactRecord,
    rationale: str | None,
    rejected_options: list[dict[str, object]],
    metadata: dict[str, object],
) -> str:
    if not task_run_id:
        LOG.warning("reason depth review requires task_run_id project=%s", project_id)
        return "request_contract"
    decision = DepthReviewDecision.model_validate(data).model_dump(
        by_alias=True, exclude_none=True
    )
    evidence_count = 0
    for section in [decision, *decision["selected"]]:
        for ref in section["evidence_refs"]:
            # 显式 handle 必须来自本轮上传映射，即使长得像 Server ID 也不能绕过解析。
            handle = ref.pop("artifact_handle", None)
            if handle is not None:
                artifact_id = declared_artifacts.artifact_ids_by_handle.get(handle)
                if not artifact_id:
                    raise UnresolvedArtifactEvidenceError(
                        f"unresolved artifact handle: {handle}"
                    )
                ref["artifact_id"] = artifact_id
            evidence_count += 1
    response = retry_graph_write(lambda: client.submit_depth_review(
        project_id, decision, worker=worker.name, task_run_id=task_run_id
    ))
    disposition = classify_write_response(response)
    accepted = response.data if response.ok and isinstance(response.data, dict) else {}
    fact = accepted.get("fact") or {}
    fact_id = fact.get("id") if isinstance(fact, dict) else None
    created_ids = [
        item["id"]
        for item in accepted.get("intents", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    record_protocol_write(
        client,
        project_id,
        task_run_id,
        operation="depth_review",
        phase="reason_execute",
        response=response,
        fact_id=fact_id,
        evidence_count=evidence_count,
    )
    record_decision_result(
        client,
        project_id,
        task_run_id,
        phase="reason_execute",
        outcome="depth_review",
        rationale=rationale or decision["description"],
        from_ids=intent_from_ids(decision["selected"]),
        created_intent_ids=created_ids,
        completed_fact_id=fact_id,
        evidence_count=evidence_count,
        rejected_options=rejected_options,
        **metadata,
        write_status="success" if response.ok else "failed",
        error_message=None if response.ok else disposition.safe_code or "depth_review_not_accepted",
    )
    # 选零个同样是成功决策；权限拒绝、过期摘要或租约冲突都不是已接受。
    if not response.ok:
        LOG.warning(
            "reason depth review write failed project=%s worker=%s status=%s code=%s",
            project_id, worker.name, response.status_code, disposition.safe_code,
        )
        return disposition.error_type or "business_acceptance"
    return "success"


def _write_complete_decision(
    client: CairnClient,
    project_id: str,
    worker: WorkerConfig,
    task_run_id: str | None,
    *,
    data,
    evidence: ProcessArtifactRecord,
    declared_artifacts: ProcessArtifactRecord,
    payload: object,
    execute_ms: int,
    total_ms: int,
    rationale: str | None,
    rejected_options: list[dict[str, object]],
    metadata: dict[str, object],
) -> str:
    # complete 只有经 Server 接受才算成功；没有 TaskRun 收据绑定不能安全重试。
    if not task_run_id:
        return "request_contract"
    worker_evidence_refs = resolve_declared_artifact_evidence_refs(
        extract_evidence_refs(
            payload,
            section="complete",
        ),
        declared_artifacts,
    )
    complete_refs = evidence.refs + declared_artifacts.refs + worker_evidence_refs
    response = retry_graph_write(lambda: client.complete_with_evidence(
        project_id,
        data["from"],
        data["description"],
        worker.name,
        complete_refs,
        task_run_id=task_run_id,
    ))
    disposition = classify_write_response(response)
    record_protocol_write(
        client,
        project_id,
        task_run_id,
        operation="complete",
        phase="reason_execute",
        response=response,
        fact_id="goal" if response.ok else None,
        from_ids=data["from"],
        evidence_count=len(complete_refs),
    )
    write_status = "success" if response.ok else "failed"
    record_decision_result(
        client,
        project_id,
        task_run_id,
        phase="reason_execute",
        outcome="complete",
        rationale=rationale,
        from_ids=data["from"],
        completed_fact_id="goal" if response.ok else None,
        evidence_count=len(complete_refs),
        rejected_options=rejected_options,
        **metadata,
        write_status=write_status,
        error_message=None if response.ok else disposition.safe_code or "complete_not_accepted",
    )
    if not response.ok:
        LOG.warning(
            "reason complete write failed project=%s worker=%s status=%s code=%s",
            project_id,
            worker.name,
            response.status_code,
            disposition.safe_code,
        )
        return disposition.error_type or "business_acceptance"
    LOG.info(
        "project completed project=%s worker=%s from=%s execute_ms=%s total_ms=%s",
        project_id,
        worker.name,
        data["from"],
        execute_ms,
        total_ms,
    )
    return "success"


def _write_intent_decisions(
    client: CairnClient,
    project_id: str,
    worker: WorkerConfig,
    task_run_id: str | None,
    *,
    intents: list[dict[str, object]],
    execute_ms: int,
    total_ms: int,
    rationale: str | None,
    rejected_options: list[dict[str, object]],
    metadata: dict[str, object],
) -> str:
    # 一轮 Reason 的 TaskRun + 原始条目索引固定；任何未接受条目都不能冒充整批成功。
    if not task_run_id:
        return "request_contract"
    for index, item in enumerate(intents):
        dependency = item.get("depends_on")
        if dependency is not None and (
            not isinstance(dependency, str)
            or not dependency.strip()
            or (dependency.startswith("@") and dependency not in {f"@{i + 1}" for i in range(index)})
        ):
            # 整批预检禁止前向/不存在占位符及数组猜测，不能抹掉依赖后立即派发。
            record_decision_result(
                client, project_id, task_run_id, phase="reason_execute", outcome="intents",
                rationale=rationale, from_ids=intent_from_ids(intents), created_intent_ids=[],
                rejected_options=rejected_options, **metadata, write_status="failed",
                error_message="intent_dependency_invalid",
            )
            return "request_contract"
    created = 0
    created_ids: list[str] = []
    write_failed = False
    write_error_type: str | None = None
    error_message: str | None = None
    # 占位符映射：@1/@2 引用同批次内已被 Server 接受的 Intent。
    placeholder_map: dict[str, str] = {}
    for index, intent_data in enumerate(intents):
        dependency = intent_data.get("depends_on")
        depends_on = placeholder_map[dependency] if dependency and dependency.startswith("@") else dependency
        # 绑定显式 coverage slot，不改变原有依赖和覆盖语义。
        slot_key_raw = intent_data.get("slot_key")
        slot_key = slot_key_raw.strip() if isinstance(slot_key_raw, str) and slot_key_raw.strip() else None
        strategy_key_raw = intent_data.get("strategy_key")
        strategy_key = (
            strategy_key_raw.strip().lower()
            if isinstance(strategy_key_raw, str) and strategy_key_raw.strip()
            else ""
        )
        priority_raw = intent_data.get("priority")
        priority = int(priority_raw) if priority_raw is not None else None
        response = retry_graph_write(lambda: client.create_intent(
            project_id,
            intent_data["from"],
            intent_data["description"],
            worker.name,
            task_run_id=task_run_id,
            submission_index=index,
            depends_on=depends_on,
            slot_key=slot_key,
            strategy_key=strategy_key,
            priority=priority,
        ))
        created_id: str | None = None
        if response.ok and isinstance(response.data, dict):
            candidate = response.data.get("id")
            if isinstance(candidate, str) and candidate:
                created_id = candidate
                created_ids.append(candidate)
                # 记录占位符映射，供后续 intent 引用。
                placeholder_map[f"@{index + 1}"] = candidate
        record_protocol_write(
            client,
            project_id,
            task_run_id,
            operation="create_intent",
            phase="reason_execute",
            response=response,
            status="success" if created_id is not None else "failed",
            from_ids=intent_data["from"],
            created_id=created_id,
        )
        if not response.ok or created_id is None:
            disposition = classify_write_response(response)
            LOG.warning(
                "reason intent write failed project=%s worker=%s status=%s code=%s",
                project_id,
                worker.name,
                response.status_code,
                disposition.safe_code,
            )
            write_failed = True
            write_error_type = disposition.error_type or "business_acceptance"
            error_message = disposition.safe_code or "intent_create_not_accepted"
            # 保留已接受收据，但停止剩余条目，避免依赖失败后另建无约束任务。
            break
        created += 1
        LOG.info(
            "reason created intent project=%s worker=%s from=%s description=%s",
            project_id,
            worker.name,
            intent_data["from"],
            intent_data["description"],
        )
    LOG.info(
        "reason finished project=%s worker=%s created_intents=%s/%s execute_ms=%s total_ms=%s",
        project_id,
        worker.name,
        created,
        len(intents),
        execute_ms,
        total_ms,
    )
    if created == 0:
        record_decision_result(
            client,
            project_id,
            task_run_id,
            phase="reason_execute",
            outcome="intents",
            rationale=rationale,
            from_ids=intent_from_ids(intents),
            created_intent_ids=created_ids,
            rejected_options=rejected_options,
            **metadata,
            write_status="failed" if write_failed else "skipped",
            error_message="未创建新的 Intent",
        )
        LOG.warning(
            "reason created no intents project=%s worker=%s attempted=%s execute_ms=%s total_ms=%s",
            project_id,
            worker.name,
            len(intents),
            execute_ms,
            total_ms,
        )
        return (write_error_type or "business_acceptance") if write_failed else "evidence_insufficient"
    record_decision_result(
        client,
        project_id,
        task_run_id,
        phase="reason_execute",
        outcome="intents",
        rationale=rationale,
        from_ids=intent_from_ids(intents),
        created_intent_ids=created_ids,
        rejected_options=rejected_options,
        **metadata,
        write_status="partial" if write_failed else "success",
        error_message=error_message,
    )
    return (write_error_type or "business_acceptance") if write_failed else "success"


def _write_abandon_decisions(
    client: CairnClient,
    project_id: str,
    worker: WorkerConfig,
    task_run_id: str | None,
    *,
    intent_ids: list[str],
    rationale: str | None,
    execute_ms: int,
    total_ms: int,
) -> str | None:
    # 辅助写入没有收据幂等，不重试；任一未接受项都必须阻断后续图变更。
    abandoned = 0
    failed_ids: list[str] = []
    error_types: list[str] = []
    for intent_id in intent_ids:
        response = client.abandon_intent(project_id, intent_id, worker.name, task_run_id=task_run_id)
        record_protocol_write(
            client,
            project_id,
            task_run_id,
            operation="abandon_intent",
            phase="reason_execute",
            response=response,
            created_id=intent_id,
        )
        if not response.ok:
            failed_ids.append(intent_id)
            disposition = classify_write_response(response)
            error_types.append(disposition.error_type or "business_acceptance")
            LOG.warning(
                "reason abandon failed project=%s worker=%s intent=%s status=%s code=%s",
                project_id,
                worker.name,
                intent_id,
                response.status_code,
                disposition.safe_code,
            )
            continue
        abandoned += 1
        LOG.info(
            "reason abandoned intent project=%s worker=%s intent=%s",
            project_id,
            worker.name,
            intent_id,
        )
    if abandoned:
        LOG.info(
            "reason abandoned intents project=%s worker=%s abandoned=%s/%s execute_ms=%s total_ms=%s",
            project_id,
            worker.name,
            abandoned,
            len(intent_ids),
            execute_ms,
            total_ms,
        )
    record_decision_result(
        client, project_id, task_run_id, phase="reason_execute", outcome="abandon",
        rationale=rationale,
        write_status=("partial" if abandoned else "failed") if failed_ids else "success",
        error_message=f"未接受的 Intent 数量: {len(failed_ids)}" if failed_ids else None,
    )
    return _dominant_write_error(error_types)
