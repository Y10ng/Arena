"""Explore 任务执行逻辑，负责探索单个 Intent 并将结果收敛为 Fact。"""

from __future__ import annotations

from dataclasses import replace
import logging
import time

from pydantic import ValidationError
import requests

from cairn.assessment_contracts import GOAL_DRIVEN_PROTOCOL
from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.contracts import (
    extract_coverage_slots,
    extract_declared_artifacts,
    extract_evidence_refs,
    rejection_error_type,
    validate_explore_payload,
    validate_verifier_payload,
)
from cairn.dispatcher.prompting import load_prompt, render_prompt
from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager, ContainerRuntimeMismatchError, ContainerRuntimeOptions
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.tasks.common import (
    best_effort_release,
    build_coverage_result_for_conclude,
    build_assessment_record_for_conclude,
    build_discovered_surfaces_for_conclude,
    CoverageEvidenceInsufficientError,
    VerificationEvidenceInsufficientError,
    build_vulnerability_for_conclude,
    build_verification_result_for_conclude,
    conclude_or_release_on_failure,
    cancel_reason,
    did_oom,
    did_timeout,
    preview,
    record_declared_artifacts,
    record_decision_result,
    record_prompt_rendered,
    record_process_artifacts,
    record_validation_result,
    resolve_declared_artifact_evidence_refs,
    resolve_optional_fact_evidence_refs,
    UnresolvedArtifactEvidenceError,
    run_worker_process,
    worker_process_failure,
    update_task_run_session,
    write_conclude_result_with_fact_id,
)
from cairn.dispatcher.tasks.candidate_capture_outbox import CandidateCaptureOutbox
from cairn.dispatcher.tasks.explore_context import (
    BoundCoverageCell,
    explore_prompt_replacements,
    resolve_admitted_coverage_cell,
    selected_experiment_context,
    verifier_prompt_replacements,
)
from cairn.dispatcher.tasks.surface_policy import (
    blocker_cooldown_enabled,
    outcome_contract_version,
    surface_manifest_enabled,
)
from cairn.dispatcher.tasks.write_outcomes import (
    classify_write_response,
    response_detail_code,
)
from cairn.dispatcher.worker_result_gateway import (
    WorkerRequestError,
    WorkerResultFailure,
    decode_pi_result,
    worker_failure_error_type,
)
from cairn.dispatcher.tasks.delivery import DeliveryUnavailable, DELIVERY_PROMPT, register_delivery, resume_continuation, resume_delivery, remaining_delivery_seconds
from cairn.dispatcher.workers.registry import get_driver
from cairn.server.models import Intent, ProjectDetail, VulnerabilityVerifierTarget

LOG = logging.getLogger(__name__)


def _verifier_target_failure(
    exc: Exception,
    *,
    phase: str,
) -> WorkerResultFailure | None:
    """把 Verifier 前置读取失败投影到稳定领域类别，不泄露响应正文。"""
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        response = exc.response
        data = None
        try:
            data = response.json()
        except ValueError:
            pass
        result = ApiResult(status_code=response.status_code, data=data)
        disposition = classify_write_response(result)
        code = response_detail_code(result)
        if response.status_code == 404:
            # Observation 是已签发 Verifier Intent 的权威依赖；消失表示业务快照已陈旧。
            error_type = "stale_business_state"
            code = code or "vulnerability_observation_not_found"
        else:
            error_type = disposition.error_type or "business_acceptance"
            code = code or "verifier_target_rejected"
        failure_class = {
            "business_acceptance": "business_acceptance",
            "policy_denied": "permission",
            "server_write_failed": "server_write",
            "stale_business_state": "stale_business_state",
        }.get(error_type, "business_acceptance")
        message = {
            "business_acceptance": "Server rejected the verifier target lookup",
            "permission": "Verifier target lookup was denied",
            "server_write": "Server was unavailable while resolving the verifier target",
            "stale_business_state": "Authoritative verifier target changed",
        }[failure_class]
        return WorkerResultFailure(
            failure_class=failure_class,
            code=code,
            message=message,
            fingerprint="",
            retryable=disposition.retry_same_write,
            origin="server_admission",
            phase=phase,
        )
    if isinstance(exc, requests.RequestException):
        return WorkerResultFailure(
            failure_class="server_write",
            code="verifier_target_unavailable",
            message="Server was unavailable while resolving the verifier target",
            fingerprint="",
            retryable=True,
            origin="server_admission",
            phase=phase,
        )
    if isinstance(exc, (ValidationError, ValueError, TypeError)):
        return WorkerResultFailure(
            failure_class="request_contract",
            code="verifier_target_invalid",
            message="Server verifier target response is invalid",
            fingerprint="",
            origin="server_admission",
            phase=phase,
            path="verifier_target",
        )
    return None


def run_explore_task(
    config: DispatchConfig,
    client: CairnClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    export_yaml: str,
    intent: Intent,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
    task_run_id: str | None = None,
    *,
    prompt_group: str | None = None,
    knowledge: str | None = None,
    container_runtime: ContainerRuntimeOptions | None = None,
    delivery_context: dict | None = None,
    continuation_context: dict | None = None,
    admission_context: dict[str, object] | None = None,
    checkpoint_staging: bool = False,
    candidate_outbox: CandidateCaptureOutbox | None = None,
) -> str | WorkerResultFailure:
    driver = get_driver(worker.type)
    task_started = time.perf_counter()
    effective_prompt_group = prompt_group or config.runtime.prompt_group
    assessment_execution = project.project.assessment_execution or {}
    structured_assessment = bool(assessment_execution.get("enabled"))
    goal_driven = (
        getattr(project.project, "assessment_protocol", "")
        == GOAL_DRIVEN_PROTOCOL
    )
    cell_assessment = structured_assessment and not goal_driven
    require_vulnerability_identity = (
        getattr(project.project, "vulnerability_fingerprint_version", None) is not None
    )
    manifest_enabled = surface_manifest_enabled(project)
    outcome_version = outcome_contract_version(project)
    blocker_cooldown = blocker_cooldown_enabled(project)
    failure_conclusion_enabled = structured_assessment
    verifier_assessment = structured_assessment and intent.assessment_role == "verifier"
    execute_phase = "verifier_execute" if verifier_assessment else "explore_execute"
    coverage_cell: BoundCoverageCell | None = None
    verifier_target: VulnerabilityVerifierTarget | None = None
    if not task_run_id:
        raise ValueError("explore task requires task_run_id for lease renewal")
    # Explore 续租同时绑定 Intent 与当前 running TaskRun，终态执行不能复活租约。
    lease = HeartbeatLease.for_intent(
        client,
        project.project.id,
        intent.id,
        worker.name,
        task_run_id,
        config.runtime.interval,
    )
    lease.start()
    execute_result = None
    try:
        if delivery_context is None and structured_assessment:
            if verifier_assessment:
                observation_id = str(intent.target_observation_id or "").strip()
                if not observation_id:
                    best_effort_release(
                        client,
                        project.project.id,
                        intent.id,
                        worker.name,
                        task_run_id=task_run_id,
                    )
                    return WorkerResultFailure(
                        failure_class="request_contract",
                        code="verifier_target_binding_invalid",
                        message="Verifier intent target binding is missing",
                        fingerprint="",
                        origin="dispatcher_admission",
                        phase=execute_phase,
                        path="binding.verification_observation_id",
                    )
                try:
                    verifier_target = client.get_observation_verifier_target(
                        project.project.id,
                        observation_id,
                    )
                except Exception as exc:
                    failure = _verifier_target_failure(exc, phase=execute_phase)
                    if failure is None:
                        raise
                    LOG.warning(
                        "authoritative verifier target resolution failed project=%s intent=%s worker=%s class=%s code=%s",
                        project.project.id,
                        intent.id,
                        worker.name,
                        failure.failure_class,
                        failure.code,
                    )
                    best_effort_release(
                        client,
                        project.project.id,
                        intent.id,
                        worker.name,
                        task_run_id=task_run_id,
                    )
                    return failure
            elif cell_assessment:
                try:
                    coverage_cell = resolve_admitted_coverage_cell(
                        admission_context, intent
                    )
                    if intent.depth_review_fact_id:
                        # 在创建容器或调用模型前拒绝残缺选题，不能当容器故障重复尝试。
                        selected_experiment_context(intent, project.facts)
                except Exception as exc:
                    LOG.warning(
                        "authoritative admission binding resolution failed project=%s intent=%s worker=%s error=%s",
                        project.project.id,
                        intent.id,
                        worker.name,
                        exc,
                    )
                    best_effort_release(
                        client,
                        project.project.id,
                        intent.id,
                        worker.name,
                        task_run_id=task_run_id,
                    )
                    return WorkerResultFailure(
                        failure_class="request_contract",
                        code="admission_contract_invalid",
                        message="Server admission contract is missing or invalid",
                        fingerprint="",
                        origin="dispatcher_admission",
                        phase=execute_phase,
                        path="runtime_context.structured_explore_binding",
                    )
        execution_timeout = config.tasks.explore.timeout
        if delivery_context is not None:
            container_name, frozen_delivery, execution_timeout = resume_delivery(
                container_manager,
                project.project.id,
                worker,
                delivery_context,
                ("explore_execute", "verifier_execute"),
            )
            # 交付重试以冻结 phase 为准；实时项目开关或 Intent 角色漂移不能改写原执行身份。
            execute_phase = frozen_delivery["phase"]
            verifier_assessment = execute_phase == "verifier_execute"
            session = frozen_delivery["session_id"]
            prompt = DELIVERY_PROMPT
            request_binding = frozen_delivery["binding"]
        else:
            if continuation_context is not None:
                container_name, session = resume_continuation(
                    client, container_manager, project.project.id, cancellation.generation,
                    container_runtime, continuation_context,
                )
            else:
                container_name = container_manager.ensure_running(project.project.id, container_runtime)
                session = driver.prepare_session()
            request_binding = None
            if coverage_cell is not None:
                request_binding = {
                    "slot_key": coverage_cell.slot_key,
                    "surface_key": coverage_cell.surface_key,
                    "test_family": coverage_cell.test_family,
                }
            elif verifier_assessment:
                request_binding = {"verification_observation_id": str(intent.target_observation_id)}
            elif goal_driven:
                # 协议来自项目冻结快照，并参与 schema hash；Worker 不能自行选择结果语义。
                request_binding = {"assessment_protocol": GOAL_DRIVEN_PROTOCOL}
            frozen_delivery = register_delivery(
                client, container_manager, container_name, project, task_run_id, worker,
                phase=execute_phase, session=session, timeout_seconds=execution_timeout,
                binding=request_binding, inputs=None,
            )

            # Explore prompt 使用图谱快照、当前 Intent 和知识库上下文，输出必须落到该 Intent 上。
            prompt_name = "verifier.md" if verifier_assessment else "explore.md"
            if verifier_assessment:
                assert verifier_target is not None
                prompt_replacements = verifier_prompt_replacements(
                    intent,
                    verifier_target,
                    knowledge=knowledge,
                    advisory=container_runtime,
                )
            else:
                prompt_replacements = explore_prompt_replacements(
                    container_manager,
                    container_name,
                    export_yaml,
                    intent,
                    phase=execute_phase,
                    knowledge=knowledge,
                    advisory=container_runtime,
                    coverage_cell=coverage_cell,
                    surface_manifest=manifest_enabled,
                    facts=project.facts,
                    project=project,
                )
            prompt = render_prompt(
                load_prompt(effective_prompt_group, prompt_name),
                prompt_replacements,
            )
            record_prompt_rendered(
                client,
                project.project.id,
                task_run_id,
                phase=execute_phase,
                prompt_group=effective_prompt_group,
                prompt_name=prompt_name,
                prompt=prompt,
                worker=worker,
                replacement_keys=list(prompt_replacements),
            )

        verifier_observation_id = ""
        if verifier_assessment and isinstance(request_binding, dict):
            frozen_observation_id = request_binding.get("verification_observation_id")
            if isinstance(frozen_observation_id, str):
                verifier_observation_id = frozen_observation_id
        execution_timeout = remaining_delivery_seconds(frozen_delivery)
        execute = driver.build_execute(
            worker, prompt, session, phase=execute_phase,
            project_id=project.project.id, task_run_id=task_run_id,
            timeout_seconds=execution_timeout,
            conclude_timeout_seconds=(
                0
                if delivery_context is not None
                else min(config.tasks.explore.conclude_timeout, execution_timeout)
            ),
            binding=request_binding,
            resume=delivery_context is not None or continuation_context is not None,
            conclude_only=delivery_context is not None,
            continuation=continuation_context,
            enable_checkpoints=checkpoint_staging,
        )
        session = execute.session
        # SDK 在原会话中提交和有界修正，不再由 Dispatcher 重跑探索。
        execute_started = time.perf_counter()
        first = run_worker_process(
            container_manager,
            container_name,
            worker,
            execute,
            phase=execute_phase,
            timeout_seconds=execution_timeout,
            lease=lease,
            cancellation=cancellation,
            client=client,
            project_id=project.project.id,
            task_run_id=task_run_id,
            payload_recorder_enabled=goal_driven,
            candidate_outbox=candidate_outbox,
        )
        execute_result = first
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        # session 提取与协议解析复用同一次完整读取；日志仍只使用 ProcessResult 预览字段。
        first_stdout = first.read_stdout()
        evidence = record_process_artifacts(
            client,
            project.project.id,
            task_run_id,
            phase=execute_phase,
            result=first,
        )
        cancelled = cancel_reason(first, cancellation)
        if cancelled is not None:
            LOG.info(
                "explore cancelled project=%s intent=%s worker=%s reason=%s execute_ms=%s",
                project.project.id,
                intent.id,
                worker.name,
                cancelled,
                execute_ms,
            )
            best_effort_release(client, project.project.id, intent.id, worker.name, task_run_id=task_run_id)
            return worker_process_failure(
                first, phase=execute_phase, project_id=project.project.id,
                task_run_id=task_run_id, session=session, cancelled=True,
            )
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during explore project=%s intent=%s worker=%s status=%s execute_ms=%s",
                project.project.id,
                intent.id,
                worker.name,
                lease.failure.status_code,
                execute_ms,
            )
            best_effort_release(client, project.project.id, intent.id, worker.name, task_run_id=task_run_id)
            return "heartbeat_lost"
        if not did_timeout(first) and not did_oom(first) and not first.transport_error and first.returncode == 0:
            # 成功退出不等于接受结果，仍须验证 runner 信封与业务约束。
            canonicalized_fields: list[str] = []

            def validate_result(payload):
                if verifier_assessment:
                    return validate_verifier_payload(
                        payload,
                        target_observation_id=verifier_observation_id,
                        expected_kind="verifier_result",
                    )
                return validate_explore_payload(
                    payload,
                    require_coverage_result=cell_assessment,
                    require_vulnerability_identity=require_vulnerability_identity,
                    surface_manifest=manifest_enabled,
                    strict_contract=structured_assessment,
                    expected_kind="explore_result",
                    outcome_contract_version=outcome_version,
                    blocker_cooldown=blocker_cooldown,
                    goal_driven=goal_driven,
                )

            decoded = decode_pi_result(
                first_stdout,
                project_id=project.project.id, task_run_id=task_run_id, session=session,
                phase=execute_phase,
                validator=validate_result,
            )
            if decoded.failure is not None:
                error_type = worker_failure_error_type(decoded.failure)
                LOG.warning(
                    "explore result failed project=%s intent=%s worker=%s class=%s code=%s fingerprint=%s error=%s execute_ms=%s total_ms=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    decoded.failure.failure_class,
                    decoded.failure.code,
                    decoded.failure.fingerprint,
                    decoded.failure.message,
                    execute_ms,
                    int((time.perf_counter() - task_started) * 1000),
                )
                record_validation_result(
                    client,
                    project.project.id,
                    task_run_id,
                    phase=execute_phase,
                    status="invalid",
                    outcome=error_type,
                    payload=decoded.payload,
                    error_message=decoded.failure.message,
                    failure=decoded.failure,
                    canonicalized_fields=canonicalized_fields,
                )
                conclude_or_release_on_failure(
                    client,
                    project.project.id,
                    intent.id,
                    worker.name,
                    f"explore result invalid: {decoded.failure.code}",
                    task_run_id=task_run_id,
                    enabled=failure_conclusion_enabled,
                    structured=structured_assessment,
                    slot_key=getattr(intent, "slot_key", None),
                )
                return replace(decoded.failure, exit_code=first.returncode)
            update_task_run_session(client, project.project.id, task_run_id, session)
            assert decoded.payload is not None
            assert decoded.kind is not None
            payload = decoded.payload
            kind = decoded.kind
            description = decoded.description
            if kind == "rejected":
                record_validation_result(
                    client,
                    project.project.id,
                    task_run_id,
                    phase=execute_phase,
                    status="rejected",
                    outcome=kind,
                    payload=payload,
                    canonicalized_fields=canonicalized_fields,
                )
                # rejected 是模型主动拒绝的业务失败，直接使用协议中的错误分类。
                error_type = rejection_error_type(payload)
                LOG.warning(
                    "explore rejected project=%s intent=%s worker=%s error_type=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    error_type,
                    execute_ms,
                    int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout),
                )
                conclude_or_release_on_failure(client, project.project.id, intent.id, worker.name, f"worker rejected: {error_type}", task_run_id=task_run_id, enabled=failure_conclusion_enabled, structured=structured_assessment, slot_key=getattr(intent, "slot_key", None))
                return error_type
            declared_artifacts = record_declared_artifacts(
                client,
                container_manager,
                container_name,
                project.project.id,
                task_run_id,
                phase=execute_phase,
                artifacts=extract_declared_artifacts(payload),
            )
            if declared_artifacts.write_failed:
                record_validation_result(
                    client,
                    project.project.id,
                    task_run_id,
                    phase=execute_phase,
                    status="invalid",
                    outcome="artifact_write_failed",
                    payload=payload,
                    error_message="declared artifact write failed",
                    canonicalized_fields=canonicalized_fields,
                )
                best_effort_release(client, project.project.id, intent.id, worker.name, task_run_id=task_run_id)
                return "artifact_write_failed"
            if declared_artifacts.collect_failed:
                record_validation_result(
                    client,
                    project.project.id,
                    task_run_id,
                    phase=execute_phase,
                    status="invalid",
                    outcome="artifact_error",
                    payload=payload,
                    error_message="declared artifact collection failed",
                    canonicalized_fields=canonicalized_fields,
                )
                best_effort_release(client, project.project.id, intent.id, worker.name, task_run_id=task_run_id)
                return "artifact_error"
            worker_evidence_refs = []
            validation_warnings: list[dict[str, object]] = []
            coverage_result = None
            coverage_evidence_refs = []
            verification_result = None
            verification_evidence_refs = []
            vulnerability = None
            assessment_record = None
            assessment_evidence_refs = []
            if verifier_assessment:
                try:
                    verification_result, verification_evidence_refs = (
                        build_verification_result_for_conclude(payload, declared_artifacts)
                    )
                except VerificationEvidenceInsufficientError as exc:
                    record_validation_result(
                        client,
                        project.project.id,
                        task_run_id,
                        phase=execute_phase,
                        status="invalid",
                        outcome="artifact_error",
                        payload=payload,
                        error_message=str(exc),
                        canonicalized_fields=canonicalized_fields,
                    )
                    conclude_or_release_on_failure(
                        client,
                        project.project.id,
                        intent.id,
                        worker.name,
                        "verifier evidence invalid",
                        task_run_id=task_run_id,
                        enabled=failure_conclusion_enabled,
                        structured=structured_assessment,
                        slot_key=getattr(intent, "slot_key", None),
                    )
                    return "artifact_error"
            else:
                fact_refs = extract_evidence_refs(payload, section="fact")
                if structured_assessment:
                    worker_evidence_refs = resolve_declared_artifact_evidence_refs(
                        fact_refs,
                        declared_artifacts,
                    )
                else:
                    worker_evidence_refs, validation_warnings = (
                        resolve_optional_fact_evidence_refs(
                            fact_refs,
                            declared_artifacts,
                        )
                    )
                try:
                    assessment_record, assessment_evidence_refs = build_assessment_record_for_conclude(
                        payload, declared_artifacts,
                    )
                    if cell_assessment:
                        coverage_result, coverage_evidence_refs = (
                            build_coverage_result_for_conclude(
                                payload,
                                declared_artifacts,
                                require_current_run_evidence=(
                                    outcome_version >= 2
                                ),
                            )
                        )
                        if coverage_result is not None and coverage_cell is not None:
                            mismatched = coverage_cell.bind_result_identity(
                                coverage_result,
                                strategy_key=intent.strategy_key,
                            )
                            if mismatched:
                                LOG.warning(
                                    "worker coverage identity overridden project=%s intent=%s worker=%s fields=%s",
                                    project.project.id,
                                    intent.id,
                                    worker.name,
                                    mismatched,
                                )
                    vulnerability = build_vulnerability_for_conclude(
                        payload,
                        declared_artifacts,
                        slot_key=getattr(intent, "slot_key", None),
                        require_current_run_reproduction=(
                            structured_assessment and outcome_version >= 2
                        ),
                    )
                except CoverageEvidenceInsufficientError as exc:
                    LOG.warning(
                        "explore evidence insufficient project=%s intent=%s worker=%s error=%s",
                        project.project.id,
                        intent.id,
                        worker.name,
                        exc,
                    )
                    record_validation_result(
                        client,
                        project.project.id,
                        task_run_id,
                        phase=execute_phase,
                        status="invalid",
                        outcome="evidence_insufficient",
                        payload=payload,
                        error_message=str(exc),
                        canonicalized_fields=canonicalized_fields,
                    )
                    conclude_or_release_on_failure(
                        client, project.project.id, intent.id, worker.name,
                        "explore evidence insufficient", task_run_id=task_run_id,
                        enabled=failure_conclusion_enabled, structured=structured_assessment,
                        slot_key=getattr(intent, "slot_key", None),
                    )
                    return "evidence_insufficient"
            discovered_surfaces = (
                build_discovered_surfaces_for_conclude(payload, declared_artifacts)
                if manifest_enabled and not verifier_assessment
                else None
            )
            record_validation_result(
                client,
                project.project.id,
                task_run_id,
                phase=execute_phase,
                status="valid",
                outcome=kind,
                payload=payload,
                canonicalized_fields=canonicalized_fields,
                warnings=validation_warnings,
            )
            # 当前运行证据与业务结论证据一并绑定到 Fact，Server 再校验来源归属。
            evidence_refs = (
                evidence.refs
                + declared_artifacts.refs
                + worker_evidence_refs
                + coverage_evidence_refs
                + verification_evidence_refs
                + assessment_evidence_refs
            )
            conclude = write_conclude_result_with_fact_id(
                client,
                project.project.id,
                intent.id,
                worker.name,
                description,
                source="explore_execute",
                phase_ms=execute_ms,
                total_ms=int((time.perf_counter() - task_started) * 1000),
                evidence=evidence_refs,
                task_run_id=task_run_id,
                vulnerability=vulnerability,
                coverage_result=coverage_result,
                discovered_surfaces=discovered_surfaces,
                verification_result=verification_result,
                assessment_record=assessment_record,
            )
            record_decision_result(
                client,
                project.project.id,
                task_run_id,
                phase=execute_phase,
                outcome="fact",
                from_ids=list(intent.from_),
                completed_fact_id=conclude.fact_id,
                evidence_count=len(evidence_refs),
                write_status=conclude.status,
            )
            # pentest 场景 Phase 2：explore 发现的新入口点登记为 pending 槽位，供后续 reason 派 intent。
            if conclude.status == "success" and not manifest_enabled and not verifier_assessment:
                discovered = extract_coverage_slots(payload, field="discovered_slots")
                if discovered:
                    slot_resp = client.upsert_coverage_slots(
                        project.project.id, discovered, source_intent_id=intent.id
                    )
                    if not slot_resp.ok and slot_resp.status_code not in (403, 409):
                        LOG.warning(
                            "explore discovered slots upsert failed project=%s intent=%s worker=%s status=%s body=%s",
                            project.project.id,
                            intent.id,
                            worker.name,
                            slot_resp.status_code,
                            slot_resp.text,
                        )
            return conclude.status
        failure = worker_process_failure(
            first, phase=execute_phase, project_id=project.project.id,
            task_run_id=task_run_id, session=session,
        )
        assert failure is not None
        # 硬截止或进程失败不能重新运行模型；结构化任务仅释放原有租约。
        conclude_or_release_on_failure(
            client, project.project.id, intent.id, worker.name, failure.message,
            task_run_id=task_run_id, enabled=failure_conclusion_enabled,
            structured=structured_assessment, slot_key=getattr(intent, "slot_key", None),
        )
        return failure
    except WorkerRequestError as exc:
        record_validation_result(
            client,
            project.project.id,
            task_run_id,
            phase=execute_phase,
            status="invalid",
            outcome="request_contract",
            failure=exc.failure,
            error_message=exc.failure.message,
        )
        conclude_or_release_on_failure(
            client,
            project.project.id,
            intent.id,
            worker.name,
            exc.failure.message,
            task_run_id=task_run_id,
            enabled=failure_conclusion_enabled,
            structured=structured_assessment,
            slot_key=getattr(intent, "slot_key", None),
        )
        return exc.failure
    except DeliveryUnavailable:
        best_effort_release(client, project.project.id, intent.id, worker.name, task_run_id=task_run_id)
        return "delivery_unavailable"
    except ContainerRuntimeMismatchError:
        LOG.warning("explore container runtime mismatch; workspace preserved project=%s", project.project.id)
        best_effort_release(client, project.project.id, intent.id, worker.name, task_run_id=task_run_id)
        return "container_runtime_mismatch"
    except UnresolvedArtifactEvidenceError as exc:
        LOG.warning(
            "explore artifact evidence invalid project=%s intent=%s worker=%s error=%s",
            project.project.id,
            intent.id,
            worker.name,
            exc,
        )
        record_validation_result(
            client,
            project.project.id,
            task_run_id,
            phase=execute_phase,
            status="invalid",
            outcome="artifact_error",
            payload=locals().get("payload"),
            error_message=str(exc),
            canonicalized_fields=locals().get("canonicalized_fields"),
        )
        conclude_or_release_on_failure(
            client,
            project.project.id,
            intent.id,
            worker.name,
            "artifact evidence invalid",
            task_run_id=task_run_id,
            enabled=failure_conclusion_enabled,
            structured=structured_assessment,
            slot_key=getattr(intent, "slot_key", None),
        )
        return "artifact_error"
    except Exception:
        LOG.exception("explore task crashed project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
        conclude_or_release_on_failure(client, project.project.id, intent.id, worker.name, "explore task crashed", task_run_id=task_run_id, enabled=failure_conclusion_enabled, structured=structured_assessment, slot_key=getattr(intent, "slot_key", None))
        return "container_failed"
    finally:
        # 所有终态和异常路径都由任务所有者确定性回收进程输出文件。
        if execute_result is not None:
            execute_result.cleanup()
        lease.stop()
