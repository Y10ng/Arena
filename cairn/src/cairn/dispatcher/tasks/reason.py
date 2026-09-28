"""Reason 任务执行逻辑，负责从当前图谱状态决定完成项目或创建新 Intent。"""

from __future__ import annotations

from dataclasses import replace
import logging
import time

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.contracts import (
    extract_declared_artifacts,
    rejection_error_type,
    validate_reason_payload,
)
from cairn.dispatcher.prompting import load_prompt, render_prompt
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager, ContainerRuntimeMismatchError, ContainerRuntimeOptions
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks.common import (
    best_effort_release_reason,
    cancel_reason,
    did_oom,
    preview,
    record_declared_artifacts,
    record_prompt_rendered,
    record_process_artifacts,
    record_validation_result,
    UnresolvedArtifactEvidenceError,
    run_worker_process,
    worker_process_failure,
    update_task_run_session,
)
from cairn.dispatcher.tasks.candidate_capture_outbox import CandidateCaptureOutbox
from cairn.dispatcher.tasks.reason_context import (
    ReasonPromptContext,
    build_reason_prompt_context,
    goal_driven_reason_execution,
    validate_reason_admission_context,
)
from cairn.dispatcher.tasks.reason_writes import write_reason_decision
from cairn.dispatcher.tasks.surface_policy import surface_manifest_enabled
from cairn.dispatcher.worker_result_gateway import (
    WorkerResultFailure,
    WorkerRequestError,
    decode_pi_result,
    worker_failure_error_type,
)
from cairn.dispatcher.tasks.delivery import DeliveryUnavailable, DELIVERY_PROMPT, register_delivery, resume_continuation, resume_delivery, remaining_delivery_seconds
from cairn.dispatcher.workers.registry import get_driver
from cairn.server.models import ProjectDetail

LOG = logging.getLogger(__name__)


def run_reason_task(
    config: DispatchConfig,
    client: CairnClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    export_yaml: str,
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
    if not task_run_id:
        raise ValueError("reason task requires task_run_id")
    driver = get_driver(worker.type)
    task_started = time.perf_counter()
    effective_prompt_group = prompt_group or config.runtime.prompt_group
    manifest_enabled = surface_manifest_enabled(project)
    # Reason 是项目级决策：租约防止多个 worker 同时创建下一批 Intent 或完成目标。
    lease = HeartbeatLease.for_reason(client, project.project.id, worker.name, config.runtime.interval, task_run_id)
    result: ProcessResult | None = None
    lease.start()
    try:
        execution_timeout = config.tasks.reason.timeout
        if delivery_context is not None:
            container_name, frozen_delivery, execution_timeout = resume_delivery(
                container_manager, project.project.id, worker, delivery_context, "reason_execute",
            )
            session = frozen_delivery["session_id"]
            prompt = DELIVERY_PROMPT
            reason_context = ReasonPromptContext(
                open_intents=frozen_delivery["inputs"]["open_intent_ids"],
                allowed_fact_ids=frozen_delivery["binding"]["allowed_fact_ids"],
                coverage_slots=[], replacements={}, binding=frozen_delivery["binding"],
            )
        else:
            structured_intent_binding = validate_reason_admission_context(
                project, admission_context
            )
            prompt_template = load_prompt(effective_prompt_group, "reason.md")
            if (
                structured_intent_binding is not None
                and "{structured_intent_binding}" not in prompt_template
            ):
                raise WorkerRequestError(
                    "prompt.structured_intent_binding",
                    code="structured_prompt_binding_unsupported",
                    message="Reason prompt does not support the structured admission binding",
                )
            if continuation_context is not None:
                container_name, session = resume_continuation(
                    client, container_manager, project.project.id, cancellation.generation,
                    container_runtime, continuation_context,
                )
            else:
                container_name = container_manager.ensure_running(project.project.id, container_runtime)
                session = driver.prepare_session()
            reason_context = build_reason_prompt_context(
                config, container_manager, container_name, project, export_yaml,
                knowledge=knowledge, advisory=container_runtime, client=client,
                admission_context=admission_context,
            )

            frozen_delivery = register_delivery(
                client, container_manager, container_name, project, task_run_id, worker,
                phase="reason_execute", session=session, timeout_seconds=execution_timeout,
                binding=reason_context.binding, inputs={"open_intent_ids": [item["id"] for item in reason_context.open_intents]},
            )
            LOG.debug(
                "reason context prepared project=%s worker=%s facts=%s allowed_fact_ids=%s hints=%s open_intents=%s coverage_slots=%s",
                project.project.id,
                worker.name,
                len(project.facts),
                len(reason_context.allowed_fact_ids),
                len(project.hints),
                len(reason_context.open_intents),
                len(reason_context.coverage_slots),
            )
            prompt = render_prompt(
                prompt_template,
                reason_context.replacements,
            )
            record_prompt_rendered(
                client,
                project.project.id,
                task_run_id,
                phase="reason_execute",
                prompt_group=effective_prompt_group,
                prompt_name="reason.md",
                prompt=prompt,
                worker=worker,
                replacement_keys=list(reason_context.replacements),
            )

        execution_timeout = remaining_delivery_seconds(frozen_delivery)
        command = driver.build_execute(
            worker, prompt, session, phase="reason_execute",
            project_id=project.project.id, task_run_id=task_run_id,
            timeout_seconds=execution_timeout, binding=reason_context.binding,
            resume=delivery_context is not None or continuation_context is not None,
            conclude_only=delivery_context is not None,
            continuation=continuation_context,
            enable_checkpoints=checkpoint_staging,
        )
        execute_started = time.perf_counter()
        result = run_worker_process(
            container_manager,
            container_name,
            worker,
            command,
            phase="reason_execute",
            timeout_seconds=execution_timeout,
            lease=lease,
            cancellation=cancellation,
            client=client,
            project_id=project.project.id,
            task_run_id=task_run_id,
            candidate_outbox=candidate_outbox,
        )
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        total_ms = int((time.perf_counter() - task_started) * 1000)
        stdout = result.read_stdout()
        evidence = record_process_artifacts(
            client,
            project.project.id,
            task_run_id,
            phase="reason_execute",
            result=result,
        )
        cancelled = cancel_reason(result, cancellation)
        if cancelled is not None:
            LOG.info(
                "reason cancelled project=%s worker=%s reason=%s execute_ms=%s",
                project.project.id,
                worker.name,
                cancelled,
                execute_ms,
            )
            return worker_process_failure(
                result, phase="reason_execute", project_id=project.project.id,
                task_run_id=task_run_id, session=session, cancelled=True,
            )
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during reason project=%s worker=%s status=%s execute_ms=%s",
                project.project.id,
                worker.name,
                lease.failure.status_code,
                execute_ms,
            )
            return "heartbeat_lost"
        failure = worker_process_failure(
            result, phase="reason_execute", project_id=project.project.id,
            task_run_id=task_run_id, session=session,
        )
        if failure is not None:
            return failure
        if result.candidate_capture_pending:
            # 完成或派发建议都必须等候选先取得 Server 收据，不能越过恢复 fence 写业务结果。
            return WorkerResultFailure(
                failure_class="candidate_integrity",
                code="candidate_spool_unavailable",
                message="Pi candidate 尚未取得 Server 收据",
                fingerprint="",
                origin="dispatcher_candidate_capture",
                phase="reason_execute",
                exit_code=result.returncode,
            )
        def validate_result(payload):
            kind, data, abandon = validate_reason_payload(
                payload,
                open_intents_empty=not reason_context.open_intents,
                max_intents=reason_context.binding["max_intents"],
                surface_manifest=manifest_enabled,
                structured_intent_binding=reason_context.binding.get(
                    "structured_intent_binding"
                ),
                goal_driven=goal_driven_reason_execution(project),
            )
            return kind, (data, abandon)

        decoded = decode_pi_result(
            stdout,
            project_id=project.project.id, task_run_id=task_run_id, session=session,
            phase="reason_execute",
            validator=validate_result,
        )
        if decoded.failure is not None:
            error_type = worker_failure_error_type(decoded.failure)
            LOG.warning(
                "reason result failed project=%s worker=%s class=%s code=%s fingerprint=%s error=%s execute_ms=%s total_ms=%s",
                project.project.id,
                worker.name,
                decoded.failure.failure_class,
                decoded.failure.code,
                decoded.failure.fingerprint,
                decoded.failure.message,
                execute_ms,
                total_ms,
            )
            record_validation_result(
                client,
                project.project.id,
                task_run_id,
                phase="reason_execute",
                status="invalid",
                outcome=error_type,
                payload=decoded.payload,
                error_message=decoded.failure.message,
                failure=decoded.failure,
            )
            return replace(decoded.failure, exit_code=result.returncode)
        update_task_run_session(client, project.project.id, task_run_id, session)
        assert decoded.payload is not None
        assert decoded.kind is not None
        assert isinstance(decoded.validated, tuple)
        payload = decoded.payload
        kind = decoded.kind
        data, abandon = decoded.validated
        # 校验通过的 payload 也会完整落审计表，便于复盘模型为什么写入或拒绝。
        record_validation_result(
            client,
            project.project.id,
            task_run_id,
            phase="reason_execute",
            status="rejected" if kind == "rejected" else "valid",
            outcome=kind,
            payload=payload,
        )
        # 模型声明的 artifact 先登记为 evidence，随后再按 fact/complete section 绑定到写入动作。
        declared_artifacts = record_declared_artifacts(
            client,
            container_manager,
            container_name,
            project.project.id,
            task_run_id,
            phase="reason_execute",
            artifacts=extract_declared_artifacts(payload),
        )
        if declared_artifacts.write_failed:
            return "artifact_write_failed"
        if declared_artifacts.collect_failed:
            return "artifact_error"
        if kind == "rejected":
            error_type = rejection_error_type(payload)
            LOG.warning(
                "reason rejected project=%s worker=%s error_type=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                project.project.id,
                worker.name,
                error_type,
                execute_ms,
                total_ms,
                preview(result.stdout),
            )
            return error_type
        return write_reason_decision(
            client,
            project.project.id,
            worker,
            task_run_id,
            kind=kind,
            data=data,
            payload=payload,
            evidence=evidence,
            declared_artifacts=declared_artifacts,
            execute_ms=execute_ms,
            total_ms=total_ms,
            abandon=abandon,
            surface_manifest=manifest_enabled,
        )
    except WorkerRequestError as exc:
        record_validation_result(
            client, project.project.id, task_run_id, phase="reason_execute", status="invalid",
            outcome="request_contract", failure=exc.failure, error_message=exc.failure.message,
        )
        return exc.failure
    except DeliveryUnavailable:
        return "delivery_unavailable"
    except ContainerRuntimeMismatchError:
        LOG.warning("reason container runtime mismatch; workspace preserved project=%s", project.project.id)
        return "container_runtime_mismatch"
    except UnresolvedArtifactEvidenceError as exc:
        LOG.warning(
            "reason artifact evidence invalid project=%s worker=%s error=%s",
            project.project.id,
            worker.name,
            exc,
        )
        return "artifact_error"
    except Exception:
        LOG.exception("reason task crashed project=%s worker=%s", project.project.id, worker.name)
        return "container_failed"
    finally:
        if result is not None:
            result.cleanup()
        # 无论任务如何结束，都停止 heartbeat 并释放 reason worker 槽位。
        lease.stop()
        best_effort_release_reason(client, project.project.id, worker.name, task_run_id=task_run_id)
