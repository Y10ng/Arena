"""Bootstrap 任务执行逻辑，负责新项目从 origin/goal 生成第一条事实链。"""

from __future__ import annotations

from dataclasses import replace
import logging
import time

from cairn.assessment_contracts import GOAL_DRIVEN_PROTOCOL
from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.contracts import (
    extract_declared_artifacts,
    extract_evidence_refs,
    rejection_error_type,
    validate_bootstrap_execute_payload,
)
from cairn.dispatcher.prompting import load_prompt, render_prompt
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager, ContainerRuntimeMismatchError, ContainerRuntimeOptions
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.tasks.bootstrap_context import (
    bootstrap_prompt_replacements,
    bootstrap_retry_prompt_context,
)
from cairn.dispatcher.tasks.candidate_capture_outbox import CandidateCaptureOutbox
from cairn.dispatcher.tasks.bootstrap_results import write_bootstrap_complete_result
from cairn.dispatcher.tasks.surface_policy import (
    minimum_coverage_enabled,
    surface_manifest_enabled,
)
from cairn.dispatcher.tasks.common import (
    best_effort_release,
    build_coverage_scope_disposition_for_conclude,
    build_discovered_surfaces_for_conclude,
    cancel_reason,
    did_oom,
    did_timeout,
    preview,
    record_declared_artifacts,
    record_prompt_rendered,
    record_process_artifacts,
    record_validation_result,
    resolve_declared_artifact_evidence_refs,
    resolve_optional_fact_evidence_refs,
    UnresolvedArtifactEvidenceError,
    run_worker_process,
    worker_process_failure,
    update_task_run_session,
)
from cairn.dispatcher.worker_result_gateway import (
    WorkerRequestError,
    WorkerResultFailure,
    decode_pi_result,
    worker_failure_error_type,
)
from cairn.dispatcher.tasks.delivery import DeliveryUnavailable, DELIVERY_PROMPT, register_delivery, resume_continuation, resume_delivery, remaining_delivery_seconds
from cairn.dispatcher.workers.registry import get_driver
from cairn.server.models import Intent, ProjectDetail

LOG = logging.getLogger(__name__)


def run_bootstrap_task(
    config: DispatchConfig,
    client: CairnClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
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
    checkpoint_staging: bool = False,
    candidate_outbox: CandidateCaptureOutbox | None = None,
) -> str | WorkerResultFailure:
    driver = get_driver(worker.type)
    task_started = time.perf_counter()
    effective_prompt_group = prompt_group or config.runtime.prompt_group
    structured_assessment = bool((project.project.assessment_execution or {}).get("enabled"))
    goal_driven = (
        getattr(project.project, "assessment_protocol", "")
        == GOAL_DRIVEN_PROTOCOL
    )
    manifest_enabled = surface_manifest_enabled(project)
    minimum_coverage = minimum_coverage_enabled(project)
    if not task_run_id:
        raise ValueError("bootstrap task requires task_run_id for lease renewal")
    # Bootstrap 也是 Intent 级任务；续租必须绑定当前 running TaskRun，不能退化为再次认领。
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
        execution_timeout = config.tasks.bootstrap.timeout
        if delivery_context is not None:
            container_name, frozen_delivery, execution_timeout = resume_delivery(
                container_manager, project.project.id, worker, delivery_context, "bootstrap_execute",
            )
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
            request_binding = (
                {"assessment_protocol": GOAL_DRIVEN_PROTOCOL}
                if goal_driven
                else None
            )
            frozen_delivery = register_delivery(
                client, container_manager, container_name, project, task_run_id, worker,
                phase="bootstrap_execute", session=session, timeout_seconds=execution_timeout,
                binding=request_binding, inputs=None,
            )

            # Bootstrap prompt 只需要项目初始事实、hint、target 和知识库，不依赖完整 YAML 快照。
            prompt_replacements = bootstrap_prompt_replacements(
                project,
                knowledge=knowledge,
                advisory=container_runtime,
            )
            prompt = render_prompt(
                load_prompt(effective_prompt_group, "bootstrap.md"),
                prompt_replacements,
            )
            retry_context = bootstrap_retry_prompt_context(
                client,
                project.project.id,
                task_run_id,
                intent.id,
                getattr(project.project, "execution_generation", 0),
            )
            if retry_context:
                prompt = f"{prompt.rstrip()}\n\n{retry_context}\n"
            record_prompt_rendered(
                client,
                project.project.id,
                task_run_id,
                phase="bootstrap_execute",
                prompt_group=effective_prompt_group,
                prompt_name="bootstrap.md",
                prompt=prompt,
                worker=worker,
                replacement_keys=list(prompt_replacements),
            )

        execution_timeout = remaining_delivery_seconds(frozen_delivery)
        execute = driver.build_execute(
            worker, prompt, session, phase="bootstrap_execute",
            project_id=project.project.id, task_run_id=task_run_id,
            timeout_seconds=execution_timeout,
            conclude_timeout_seconds=(
                0
                if delivery_context is not None
                else min(config.tasks.bootstrap.conclude_timeout, execution_timeout)
            ),
            binding=request_binding,
            resume=delivery_context is not None or continuation_context is not None,
            conclude_only=delivery_context is not None,
            continuation=continuation_context,
            enable_checkpoints=checkpoint_staging,
        )
        session = execute.session
        # 结果修正仅在 SDK 的同一会话与原截止内进行。
        execute_started = time.perf_counter()
        first = run_worker_process(
            container_manager,
            container_name,
            worker,
            execute,
            phase="bootstrap_execute",
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
        # session 与协议解析共享同一次完整输出读取，预览字段只用于日志。
        stdout = first.read_stdout()
        evidence = record_process_artifacts(
            client,
            project.project.id,
            task_run_id,
            phase="bootstrap_execute",
            result=first,
        )
        cancelled = cancel_reason(first, cancellation)
        if cancelled is not None:
            LOG.info(
                "bootstrap cancelled project=%s intent=%s worker=%s reason=%s execute_ms=%s",
                project.project.id,
                intent.id,
                worker.name,
                cancelled,
                execute_ms,
            )
            best_effort_release(client, project.project.id, intent.id, worker.name, task_run_id=task_run_id)
            return worker_process_failure(
                first, phase="bootstrap_execute", project_id=project.project.id,
                task_run_id=task_run_id, session=session, cancelled=True,
            )
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during bootstrap project=%s intent=%s worker=%s status=%s execute_ms=%s",
                project.project.id,
                intent.id,
                worker.name,
                lease.failure.status_code,
                execute_ms,
            )
            best_effort_release(client, project.project.id, intent.id, worker.name, task_run_id=task_run_id)
            return "heartbeat_lost"
        if not did_timeout(first) and not did_oom(first) and not first.transport_error and first.returncode == 0:
            # Bootstrap 的 execute 协议比普通 explore 多一个 complete 段，校验后才允许双写。
            canonicalized_fields: list[str] = []
            decoded = decode_pi_result(
                stdout,
                project_id=project.project.id, task_run_id=task_run_id, session=session,
                phase="bootstrap_execute",
                assessment_protocol=(GOAL_DRIVEN_PROTOCOL if goal_driven else None),
                validator=lambda payload: validate_bootstrap_execute_payload(
                    payload,
                    require_surface_resolution=minimum_coverage,
                    goal_driven=goal_driven,
                ),
            )
            if decoded.failure is not None:
                error_type = worker_failure_error_type(decoded.failure)
                LOG.warning(
                    "bootstrap result failed project=%s intent=%s worker=%s class=%s code=%s fingerprint=%s error=%s execute_ms=%s total_ms=%s",
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
                    phase="bootstrap_execute",
                    status="invalid",
                    outcome=error_type,
                    payload=decoded.payload,
                    error_message=decoded.failure.message,
                    failure=decoded.failure,
                    canonicalized_fields=canonicalized_fields,
                )
                best_effort_release(
                    client,
                    project.project.id,
                    intent.id,
                    worker.name,
                    task_run_id=task_run_id,
                )
                return replace(decoded.failure, exit_code=first.returncode)
            update_task_run_session(client, project.project.id, task_run_id, session)
            assert decoded.payload is not None
            assert decoded.kind is not None
            payload = decoded.payload
            kind = decoded.kind
            if kind == "rejected":
                record_validation_result(
                    client,
                    project.project.id,
                    task_run_id,
                    phase="bootstrap_execute",
                    status="rejected",
                    outcome=kind,
                    payload=payload,
                    canonicalized_fields=canonicalized_fields,
                )
                # 模型主动拒绝时保留错误分类，不进入 fallback，避免重复消耗同一上下文。
                error_type = rejection_error_type(payload)
                LOG.warning(
                    "bootstrap rejected project=%s intent=%s worker=%s error_type=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    error_type,
                    execute_ms,
                    int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout),
                )
                best_effort_release(client, project.project.id, intent.id, worker.name, task_run_id=task_run_id)
                return error_type
            assert isinstance(decoded.validated, dict)
            data = decoded.validated
            declared_artifacts = record_declared_artifacts(
                client,
                container_manager,
                container_name,
                project.project.id,
                task_run_id,
                phase="bootstrap_execute",
                artifacts=extract_declared_artifacts(payload),
            )
            if declared_artifacts.write_failed:
                record_validation_result(
                    client,
                    project.project.id,
                    task_run_id,
                    phase="bootstrap_execute",
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
                    phase="bootstrap_execute",
                    status="invalid",
                    outcome="artifact_error",
                    payload=payload,
                    error_message="declared artifact collection failed",
                    canonicalized_fields=canonicalized_fields,
                )
                best_effort_release(client, project.project.id, intent.id, worker.name, task_run_id=task_run_id)
                return "artifact_error"

            fact_refs = extract_evidence_refs(payload, section="fact")
            fact_warnings: list[dict[str, object]] = []
            if structured_assessment:
                fact_evidence = resolve_declared_artifact_evidence_refs(
                    fact_refs,
                    declared_artifacts,
                )
            else:
                fact_evidence, fact_warnings = resolve_optional_fact_evidence_refs(
                    fact_refs,
                    declared_artifacts,
                )
            complete_evidence = resolve_declared_artifact_evidence_refs(
                extract_evidence_refs(payload, section="complete"),
                declared_artifacts,
            )
            discovered_surfaces = (
                build_discovered_surfaces_for_conclude(payload, declared_artifacts)
                if manifest_enabled
                else None
            )
            coverage_scope_disposition = (
                build_coverage_scope_disposition_for_conclude(
                    payload,
                    declared_artifacts,
                )
                if minimum_coverage
                else None
            )
            record_validation_result(
                client,
                project.project.id,
                task_run_id,
                phase="bootstrap_execute",
                status="valid",
                outcome=kind,
                payload=payload,
                canonicalized_fields=canonicalized_fields,
                warnings=fact_warnings,
            )
            # 历史协议保留两步写入；新协议只接受初始 Fact，随后交给 Reason 决定继续或收尾。
            return write_bootstrap_complete_result(
                client,
                project.project.id,
                intent.id,
                worker.name,
                data["fact_description"],
                data.get("complete_description"),
                source="bootstrap",
                complete_project=not goal_driven,
                phase_ms=execute_ms,
                total_ms=int((time.perf_counter() - task_started) * 1000),
                shared_evidence=evidence.refs + declared_artifacts.refs,
                fact_evidence=fact_evidence,
                complete_evidence=complete_evidence,
                task_run_id=task_run_id,
                slots=data.get("slots") if not manifest_enabled else None,
                discovered_surfaces=discovered_surfaces,
                coverage_scope_disposition=coverage_scope_disposition,
            )
        best_effort_release(client, project.project.id, intent.id, worker.name, task_run_id=task_run_id)
        failure = worker_process_failure(
            first, phase="bootstrap_execute", project_id=project.project.id,
            task_run_id=task_run_id, session=session,
        )
        assert failure is not None
        return failure
    except WorkerRequestError as exc:
        record_validation_result(
            client,
            project.project.id,
            task_run_id,
            phase="bootstrap_execute",
            status="invalid",
            outcome="request_contract",
            failure=exc.failure,
            error_message=exc.failure.message,
        )
        best_effort_release(client, project.project.id, intent.id, worker.name, task_run_id=task_run_id)
        return exc.failure
    except DeliveryUnavailable:
        best_effort_release(client, project.project.id, intent.id, worker.name, task_run_id=task_run_id)
        return "delivery_unavailable"
    except ContainerRuntimeMismatchError:
        LOG.warning("bootstrap container runtime mismatch; workspace preserved project=%s", project.project.id)
        best_effort_release(client, project.project.id, intent.id, worker.name, task_run_id=task_run_id)
        return "container_runtime_mismatch"
    except UnresolvedArtifactEvidenceError as exc:
        LOG.warning(
            "bootstrap artifact evidence invalid project=%s intent=%s worker=%s error=%s",
            project.project.id,
            intent.id,
            worker.name,
            exc,
        )
        record_validation_result(
            client,
            project.project.id,
            task_run_id,
            phase="bootstrap_execute",
            status="invalid",
            outcome="artifact_error",
            payload=locals().get("payload"),
            error_message=str(exc),
            canonicalized_fields=locals().get("canonicalized_fields"),
        )
        best_effort_release(client, project.project.id, intent.id, worker.name, task_run_id=task_run_id)
        return "artifact_error"
    except Exception:
        LOG.exception("bootstrap task crashed project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
        best_effort_release(client, project.project.id, intent.id, worker.name, task_run_id=task_run_id)
        return "container_failed"
    finally:
        # ProcessResult 持有临时输出文件，所有返回和异常路径都必须显式回收。
        if execute_result is not None:
            execute_result.cleanup()
        lease.stop()
