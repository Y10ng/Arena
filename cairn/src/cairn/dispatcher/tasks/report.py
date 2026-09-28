"""Report 任务：项目完成后按用户请求生成漏洞复现包与 Markdown 汇总报告。

任务沿用 durable claim/lease 与 generation fencing，冻结模板和权威输入快照；任何失败只影响
报告 finalization，不改变项目完成状态或既有漏洞成果。
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from hashlib import sha256
import json
from dataclasses import dataclass, replace
import logging
import time

from cairn.assessment_contracts import GOAL_DRIVEN_PROTOCOL
from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.contracts import validate_report_payload
from cairn.dispatcher.prompting import (
    format_json_block,
    knowledge_base_prompt_context,
    knowledge_base_runtime_context,
    load_prompt,
    render_prompt,
)
from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.dispatcher.runtime.browser_sessions import notify_browser_task
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager, ContainerRuntimeMismatchError, ContainerRuntimeOptions
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks.candidate_capture_outbox import CandidateCaptureOutbox
from cairn.dispatcher.tasks.common import (
    record_process_artifacts,
    record_prompt_rendered,
    record_validation_result,
    run_worker_process,
    worker_process_failure,
    retry_graph_write,
    build_report_readonly_input_references,
)
from cairn.dispatcher.worker_protocol.submission import ReportData
from cairn.dispatcher.worker_result_gateway import (
    WorkerRequestError,
    WorkerResultFailure,
    decode_pi_result,
    worker_failure_error_type,
)
from cairn.dispatcher.tasks.delivery import DeliveryUnavailable, DELIVERY_PROMPT, register_delivery, resume_delivery, remaining_delivery_seconds
from cairn.dispatcher.tasks.report_finish_outbox import (
    PendingReportExecution,
    PendingReportFinish,
    PendingReportOutput,
    ReportFinishOutbox,
    ReportFinishOutboxConflict,
    can_fallback_report_finish,
    failed_finish_fallback,
    finish_request_sha256,
    pending_report_execution,
    pending_report_output,
    validate_report_artifact_receipt,
)
from cairn.dispatcher.tasks.write_outcomes import (
    WriteDisposition,
    classify_write_response,
    response_detail_code,
)
from cairn.dispatcher.workers.registry import get_driver
from cairn.server.models import AssessmentReportTaskRunResult, ProjectDetail

LOG = logging.getLogger(__name__)

# 仅保留旧直接调用入口；durable 手动报告由冻结 execution plan 授权。
REPORT_ARTIFACT_FILENAME = "vulnerability-report.md"


@dataclass(frozen=True, slots=True)
class ReportArtifactIdentity:
    artifact_id: str
    sha256: str


@dataclass(slots=True)
class ReportFinishState:
    task_run_result: dict[str, object] | None = None
    artifact: ReportArtifactIdentity | None = None
    input_snapshot: ReportArtifactIdentity | None = None
    delivery_pending: bool = False
    pending_entry: PendingReportExecution | PendingReportOutput | PendingReportFinish | None = None
    pending_finish: PendingReportFinish | None = None


_REPORT_FINISH_PROTOCOL = "cairn-assessment-report-finish-v1"
_REPORT_FINISH_RECEIPT_INVALID = "assessment_report_finish_receipt_invalid"


def _canonical_receipt_identifier(value: object, *, max_length: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= max_length
        and value == value.strip()
        and "\x00" not in value
    )


def _receipt_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _receipt_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _is_recovered_report_finish_receipt(
    data: object,
    *,
    project_id: str,
) -> bool:
    """严格识别 claim endpoint 返回的已完成 receipt，避免伪字段跳过 Worker。"""
    return (
        isinstance(data, dict)
        and data.get("protocol") == _REPORT_FINISH_PROTOCOL
        and data.get("project_id") == project_id
        and data.get("status") == "ready"
        and data.get("finish_disposition") in ("accepted", "recovered")
        and _canonical_receipt_identifier(data.get("task_run_id"), max_length=128)
        and _canonical_receipt_identifier(
            data.get("generation_token"), max_length=256
        )
        and _canonical_receipt_identifier(data.get("artifact_id"), max_length=128)
        and _canonical_receipt_identifier(
            data.get("input_snapshot_artifact_id"), max_length=128
        )
        and _receipt_sha256(data.get("input_snapshot_sha256"))
        and _receipt_sha256(data.get("request_sha256"))
        and _receipt_timestamp(data.get("accepted_at"))
    )


def _report_finish_request_sha256(
    project_id: str,
    authenticated_dispatcher_id: str,
    worker: str,
    *,
    claim_token: str,
    generation_token: str,
    task_run_id: str,
    status: str,
    artifact_id: str | None,
    artifact_sha256: str | None,
    input_snapshot_artifact_id: str | None,
    input_snapshot_sha256: str | None,
    task_run_result: dict[str, object],
) -> str:
    """按 Server 的 canonical semantic identity 计算 finish request digest。"""
    return finish_request_sha256(
        project_id=project_id,
        authenticated_dispatcher_id=authenticated_dispatcher_id,
        worker=worker,
        claim_sha256=sha256(claim_token.encode("utf-8")).hexdigest(),
        generation_token=generation_token,
        task_run_id=task_run_id,
        status=status,
        artifact_id=artifact_id,
        artifact_sha256=artifact_sha256,
        input_snapshot_artifact_id=input_snapshot_artifact_id,
        input_snapshot_sha256=input_snapshot_sha256,
        task_run_result=task_run_result,
    )


def _validate_report_finish_receipt(
    response: ApiResult,
    *,
    project_id: str,
    task_run_id: str,
    generation_token: str,
    status: str,
    artifact_id: str | None,
    input_snapshot_artifact_id: str | None,
    input_snapshot_sha256: str | None,
    request_sha256: str,
) -> ApiResult:
    """仅接受与本次 strict finish identity 完全一致的结构化收据。"""
    if not response.ok:
        return response
    data = response.data
    valid = (
        isinstance(data, dict)
        and data.get("protocol") == _REPORT_FINISH_PROTOCOL
        and data.get("project_id") == project_id
        and data.get("task_run_id") == task_run_id
        and data.get("generation_token") == generation_token
        and data.get("status") == status
        and data.get("artifact_id") == artifact_id
        and data.get("input_snapshot_artifact_id") == input_snapshot_artifact_id
        and data.get("input_snapshot_sha256") == input_snapshot_sha256
        and data.get("request_sha256") == request_sha256
        and data.get("finish_disposition") in ("accepted", "recovered")
    )
    if valid:
        return response
    LOG.warning(
        "report finalization returned invalid receipt project=%s task_run=%s status=%s",
        project_id,
        task_run_id,
        response.status_code,
    )
    # 2xx 但无法证明 ACK identity 时，按 response loss 处理，只允许重放同一 write。
    return ApiResult(
        status_code=0,
        data={"detail": _REPORT_FINISH_RECEIPT_INVALID},
    )


def _report_failure_error_type(outcome: str | WorkerResultFailure) -> str:
    if isinstance(outcome, WorkerResultFailure):
        return worker_failure_error_type(outcome)
    return {
        "container_failed": "container_failed",
        "delivery_unavailable": "delivery_unavailable",
        "report_packet_read_failed": "report_packet_read_failed",
        "server_write_failed": "server_write_failed",
        "worker_rejected": "worker_rejected",
        "report_incomplete": "result_schema",
        "artifact_error": "artifact_error",
        "business_acceptance": "business_acceptance",
        "policy_denied": "policy_denied",
        "receipt_conflict": "receipt_conflict",
        "request_contract": "request_contract",
        "stale_business_state": "stale_business_state",
    }.get(outcome, "request_contract")


def _report_task_run_result(
    outcome: str | WorkerResultFailure,
) -> dict[str, object]:
    if outcome == "success":
        return {
            "status": "succeeded",
            "exit_code": 0,
            "error_type": None,
            "error_message": "",
            "worker_failure": None,
        }
    if isinstance(outcome, WorkerResultFailure):
        error_type = worker_failure_error_type(outcome)
        status = (
            "timed_out"
            if error_type == "worker_timeout"
            else "cancelled"
            if error_type == "cancelled"
            else "failed"
        )
        return {
            "status": status,
            "exit_code": outcome.exit_code,
            "error_type": error_type,
            "error_message": outcome.message[:2000],
            "worker_failure": outcome.details(),
        }
    return {
        "status": "failed",
        "exit_code": None,
        "error_type": _report_failure_error_type(outcome),
        "error_message": outcome[:2000],
        "worker_failure": None,
    }


def _frozen_execution_error(
    plan_json: str | None,
    plan: dict[str, object] | None,
    plan_digest: str | None,
    prompt_markdown: str | None,
    prompt_digest: str | None,
) -> str | None:
    """在启动 Worker 前复核 Server 冻结的执行方案和 Prompt。"""
    if not all((plan_json, plan, plan_digest, prompt_markdown, prompt_digest)):
        return "report_execution_plan_missing"
    assert plan_json is not None
    assert plan is not None
    assert plan_digest is not None
    assert prompt_markdown is not None
    assert prompt_digest is not None
    canonical = json.dumps(
        plan,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if canonical != plan_json:
        return "report_execution_plan_not_canonical"
    if sha256(plan_json.encode("utf-8")).hexdigest() != plan_digest:
        return "report_execution_plan_digest_mismatch"
    actual_prompt_digest = sha256(prompt_markdown.encode("utf-8")).hexdigest()
    if actual_prompt_digest != prompt_digest:
        return "report_prompt_digest_mismatch"
    if plan.get("report_prompt_digest_sha256") != prompt_digest:
        return "report_execution_plan_prompt_mismatch"
    required_versions = {
        "version": 2,
        "execution_contract_version": 1,
        "execution_engine": "pi",
        "report_policy_version": 1,
        "packet_version": 2,
        "output_contract_version": 2,
        "completeness_rule_version": 1,
    }
    for key, expected in required_versions.items():
        if plan.get(key) != expected:
            return f"report_execution_plan_unsupported:{key}"
    if plan.get("report_prompt_name") != "report.md":
        return "report_execution_plan_prompt_name_invalid"
    prompt_group = plan.get("report_prompt_group")
    if not isinstance(prompt_group, str) or not prompt_group.strip():
        return "report_execution_plan_prompt_group_invalid"
    return None


def has_succeeded_report_run(
    client: CairnClient,
    project_id: str,
    *,
    not_before: str | None = None,
) -> bool:
    """幂等判定：当前完成代际已有成功 report TaskRun 时跳过。"""
    resp = client.list_task_runs(project_id)
    if not resp.ok or not isinstance(resp.data, list):
        # 查不到审计记录时按"未跑过"处理，宁可多跑一次也不漏报告。
        return False
    for run in resp.data:
        if not isinstance(run, dict):
            continue
        if run.get("task_type") != "report" or run.get("status") != "succeeded":
            continue
        started_at = run.get("started_at")
        if not_before is not None and (
            not isinstance(started_at, str) or started_at < not_before
        ):
            continue
        return True
    return False


def has_report_artifact(
    client: CairnClient,
    project_id: str,
    *,
    generation_token: str | None = None,
    not_before: str | None = None,
) -> bool:
    """按逻辑文件名检查当前完成代际的持久化报告 Artifact。"""
    resp = client.list_artifacts(project_id)
    if not resp.ok or not isinstance(resp.data, list):
        return False
    prefixed_suffix = f"-{REPORT_ARTIFACT_FILENAME}"
    for item in resp.data:
        if not isinstance(item, dict) or item.get("kind") != "report":
            continue
        uri = item.get("uri")
        if not isinstance(uri, str):
            continue
        created_at = item.get("created_at")
        if not_before is not None and (
            not isinstance(created_at, str) or created_at < not_before
        ):
            continue
        if generation_token is not None:
            metadata = item.get("metadata")
            if (
                not isinstance(metadata, dict)
                or metadata.get("report_generation_token") != generation_token
            ):
                continue
        filename = uri.rsplit("/", 1)[-1]
        if filename == REPORT_ARTIFACT_FILENAME or filename.endswith(prefixed_suffix):
            return True
    return False


def _finish_report_claim(
    client: CairnClient,
    container_manager: ContainerManager,
    project_id: str,
    worker: WorkerConfig,
    *,
    authenticated_dispatcher_id: str,
    task_run_id: str,
    generation_token: str,
    claim_token: str,
    outcome: str | WorkerResultFailure,
    finish_state: ReportFinishState,
    finish_outbox: ReportFinishOutbox | None = None,
    replace_entry: PendingReportExecution | PendingReportOutput | PendingReportFinish | None = None,
    retain_accepted_entry: bool = False,
) -> WriteDisposition:
    task_run_result = finish_state.task_run_result or _report_task_run_result(outcome)
    ready = outcome == "success"
    artifact = finish_state.artifact if ready else None
    snapshot = finish_state.input_snapshot
    if ready and (artifact is None or snapshot is None):
        task_run_result = _report_task_run_result("server_write_failed")
        ready = False
        artifact = None
    status = "ready" if ready else "failed"
    artifact_id = artifact.artifact_id if artifact is not None else None
    artifact_sha256 = artifact.sha256 if artifact is not None else None
    snapshot_id = snapshot.artifact_id if snapshot is not None else None
    snapshot_sha256 = snapshot.sha256 if snapshot is not None else None
    normalized_task_run_result = AssessmentReportTaskRunResult.model_validate(
        task_run_result
    ).model_dump(mode="json")
    claim_sha256 = sha256(claim_token.encode("utf-8")).hexdigest()
    request_sha256 = _report_finish_request_sha256(
        project_id,
        authenticated_dispatcher_id,
        worker.name,
        claim_token=claim_token,
        generation_token=generation_token,
        task_run_id=task_run_id,
        status=status,
        artifact_id=artifact_id,
        artifact_sha256=artifact_sha256,
        input_snapshot_artifact_id=snapshot_id,
        input_snapshot_sha256=snapshot_sha256,
        task_run_result=normalized_task_run_result,
    )
    pending_finish = PendingReportFinish(
        project_id=project_id,
        authenticated_dispatcher_id=authenticated_dispatcher_id,
        worker=worker.name,
        claim_sha256=claim_sha256,
        request_sha256=request_sha256,
        generation_token=generation_token,
        task_run_id=task_run_id,
        status=status,
        artifact_id=artifact_id,
        artifact_sha256=artifact_sha256,
        input_snapshot_artifact_id=snapshot_id,
        input_snapshot_sha256=snapshot_sha256,
        task_run_result=normalized_task_run_result,
    )
    if (
        finish_state.pending_finish is not None
        and finish_state.pending_finish.request_sha256
        != pending_finish.request_sha256
    ):
        return classify_write_response(
            ApiResult(
                status_code=409,
                data={"detail": "assessment_report_finish_receipt_conflict"},
            )
        )
    finish_state.pending_finish = pending_finish
    expected_entry = replace_entry or finish_state.pending_entry
    outbox_persisted = False
    if finish_outbox is not None:
        try:
            # execution→output→finish 以及 ready→failed 均在同一文件内原子推进。
            if expected_entry is None:
                finish_outbox.persist(pending_finish)
            else:
                finish_outbox.replace(expected_entry, pending_finish)
            finish_state.pending_entry = pending_finish
            outbox_persisted = True
        except ReportFinishOutboxConflict:
            LOG.error(
                "report finish outbox conflict project=%s task_run=%s code=assessment_report_finish_receipt_conflict",
                project_id,
                task_run_id,
            )
            return classify_write_response(
                ApiResult(
                    status_code=409,
                    data={"detail": "assessment_report_finish_receipt_conflict"},
                )
            )
        except OSError:
            LOG.error(
                "report finish outbox persistence failed project=%s task_run=%s code=report_finish_outbox_unavailable",
                project_id,
                task_run_id,
            )
            # 无 durable 副本时禁止发送；否则 uncertain ACK 后无法跨重启重放原始事实。
            return classify_write_response(
                ApiResult(
                    status_code=0,
                    data={"detail": "report_finish_outbox_unavailable"},
                )
            )

    def submit_finish() -> ApiResult:
        response = (
            pending_finish.replay(client)
            if outbox_persisted
            else client.finish_assessment_report_finalization(
                project_id,
                worker.name,
                claim_token=claim_token,
                generation_token=generation_token,
                task_run_id=task_run_id,
                status=status,
                artifact_id=artifact_id,
                artifact_sha256=artifact_sha256,
                input_snapshot_artifact_id=snapshot_id,
                input_snapshot_sha256=snapshot_sha256,
                task_run_result=normalized_task_run_result,
            )
        )
        return _validate_report_finish_receipt(
            response,
            project_id=project_id,
            task_run_id=task_run_id,
            generation_token=generation_token,
            status=status,
            artifact_id=artifact_id,
            input_snapshot_artifact_id=snapshot_id,
            input_snapshot_sha256=snapshot_sha256,
            request_sha256=request_sha256,
        )

    response = retry_graph_write(submit_finish)
    disposition = classify_write_response(response)
    if finish_outbox is not None and disposition.accepted:
        if retain_accepted_entry:
            # candidate terminal fence 尚未由调度器释放；journal 留作 ACK 见证。
            finish_state.pending_entry = pending_finish
        else:
            try:
                finish_outbox.remove(pending_finish)
                finish_state.pending_entry = None
            except ReportFinishOutboxConflict:
                # 并发恢复可能已推进同一 journal；不得删除替代事实。
                LOG.info(
                    "report finish outbox entry replaced after receipt project=%s task_run=%s",
                    project_id,
                    task_run_id,
                )
            except OSError:
                # strict receipt 已成立，保留 journal 供调度器幂等重放并完成清理。
                LOG.error(
                    "report finish outbox cleanup deferred project=%s task_run=%s code=report_finish_outbox_cleanup_failed",
                    project_id,
                    task_run_id,
                )
    if not disposition.accepted:
        LOG.warning(
            "report finalization finish failed project=%s task_run=%s status=%s code=%s disposition=%s",
            project_id,
            task_run_id,
            response.status_code,
            disposition.safe_code,
            disposition.disposition,
        )
        return disposition
    notify_browser_task(
        container_manager,
        project_id,
        task_run_id,
        terminal_confirmed=True,
    )
    return disposition


def run_post_completion_report(
    config: DispatchConfig,
    client: CairnClient,
    container_manager: ContainerManager,
    project_id: str,
    export_yaml: str,
    worker: WorkerConfig,
    *,
    prompt_group: str,
    knowledge: str | None = None,
    container_runtime: ContainerRuntimeOptions | None = None,
    durable_request: bool = False,
    finish_outbox: ReportFinishOutbox | None = None,
    candidate_outbox: CandidateCaptureOutbox | None = None,
) -> str | WorkerResultFailure:
    """仅执行显式请求、持有冻结计划和独立 claim 的报告。"""
    if not durable_request:
        return "skipped"
    if finish_outbox is None:
        # durable 请求缺少 journal 时必须在 claim 和模型执行前 fail closed。
        LOG.error(
            "report durable journal required project=%s code=report_finish_outbox_unavailable",
            project_id,
        )
        return "server_write_failed"
    try:
        project = client.get_project(project_id)
    except Exception:
        LOG.exception("report final project refresh failed project=%s", project_id)
        return "project_refresh_failed"
    allowed_statuses = {"completed", "stopped"} if durable_request else {"completed"}
    if project.project.status not in allowed_statuses:
        LOG.info(
            "report deferred because project lifecycle is ineligible project=%s status=%s durable=%s",
            project_id,
            project.project.status,
            durable_request,
        )
        return "deferred"

    durable = durable_request
    finish_finalization = getattr(
        client,
        "finish_assessment_report_finalization",
        None,
    )
    task_run_id: str | None = None
    generation_token: str | None = None
    claim_token: str | None = None
    template_key: str | None = None
    template_version: int | None = None
    template_digest_sha256: str | None = None
    template_markdown: str | None = None
    report_execution_plan_json: str | None = None
    report_execution_plan: dict[str, object] | None = None
    frozen_report_prompt: str | None = None
    report_execution_plan_sha256: str | None = None
    report_prompt_digest_sha256: str | None = None
    manual_request = False
    delivery_context = None
    authenticated_dispatcher_id = config.dispatcher_id
    if durable:
        claim_finalization = getattr(
            client,
            "claim_assessment_report_finalization",
            None,
        )
        if not callable(claim_finalization) or not callable(finish_finalization):
            LOG.warning("report durable API unavailable project=%s", project_id)
            return "server_write_failed"
        if finish_outbox is not None:
            try:
                if finish_outbox.has_project(project_id):
                    # direct 调用也必须服从 journal fence，不能绕过 scheduler 重跑模型。
                    LOG.warning(
                        "report durable journal already pending project=%s code=report_delivery_pending",
                        project_id,
                    )
                    return "server_write_failed"
            except OSError:
                LOG.error(
                    "report durable journal unavailable project=%s code=report_finish_outbox_invalid",
                    project_id,
                )
                return "server_write_failed"
        # 租约覆盖执行超时与有界收尾；超过 API 上限不能静默截短后继续执行。
        lease_seconds = max(300, config.tasks.report.timeout + 300)
        if lease_seconds > 1800:
            return "report_timeout_exceeds_lease"
        claim = claim_finalization(project_id, worker.name, lease_seconds=lease_seconds)
        claim_detail = (
            claim.data.get("detail") if isinstance(claim.data, dict) else None
        )
        if claim.status_code == 409:
            return (
                "skipped"
                if claim_detail == "assessment_report_not_claimable:ready"
                else "deferred"
            )
        if not claim.ok:
            claim_disposition = classify_write_response(claim)
            LOG.warning(
                "report finalization claim failed project=%s status=%s code=%s disposition=%s",
                project_id,
                claim.status_code,
                response_detail_code(claim),
                claim_disposition.disposition,
            )
            return claim_disposition.error_type or "business_acceptance"
        if isinstance(claim.data, dict):
            receipt_candidate = (
                claim.data.get("finish_disposition") in ("accepted", "recovered")
                or claim.data.get("status") == "ready"
            )
            if _is_recovered_report_finish_receipt(
                claim.data,
                project_id=project_id,
            ):
                return "skipped"
            if receipt_candidate:
                LOG.warning(
                    "report finalization claim returned invalid receipt project=%s code=%s",
                    project_id,
                    _REPORT_FINISH_RECEIPT_INVALID,
                )
            context = claim.data.get("task_run_context")
            if isinstance(context, dict):
                owner = context.get("dispatcher_id")
                if (
                    isinstance(owner, str)
                    and owner
                    and owner == owner.strip()
                    and "\x00" not in owner
                ):
                    # Server 从 machine principal 派生的 TaskRun owner 才是 receipt digest 身份。
                    authenticated_dispatcher_id = owner
                if "delivery_retry" in context:
                    delivery_context = context
            manual_request = bool(claim.data.get("requested_at"))
            value = claim.data.get("task_run_id")
            if isinstance(value, str) and value:
                task_run_id = value
            generation_value = claim.data.get("generation_token")
            claim_value = claim.data.get("claim_token")
            if isinstance(generation_value, str) and generation_value:
                generation_token = generation_value
            if isinstance(claim_value, str) and claim_value:
                claim_token = claim_value
            key_value = claim.data.get("template_key")
            version_value = claim.data.get("template_version")
            digest_value = claim.data.get("template_digest_sha256")
            markdown_value = claim.data.get("template_markdown")
            plan_value = claim.data.get("report_execution_plan_json")
            frozen_prompt_value = claim.data.get("report_prompt_markdown")
            plan_digest_value = claim.data.get("report_execution_plan_sha256")
            prompt_digest_value = claim.data.get("report_prompt_digest_sha256")
            if isinstance(key_value, str) and key_value:
                template_key = key_value
            if isinstance(version_value, int):
                template_version = version_value
            if isinstance(digest_value, str) and digest_value:
                template_digest_sha256 = digest_value
            if isinstance(markdown_value, str) and markdown_value:
                template_markdown = markdown_value
            if isinstance(plan_value, str) and plan_value:
                report_execution_plan_json = plan_value
                try:
                    parsed_plan = json.loads(plan_value)
                except ValueError:
                    parsed_plan = None
                if isinstance(parsed_plan, dict):
                    report_execution_plan = parsed_plan
            if isinstance(frozen_prompt_value, str) and frozen_prompt_value:
                frozen_report_prompt = frozen_prompt_value
            if isinstance(plan_digest_value, str) and plan_digest_value:
                report_execution_plan_sha256 = plan_digest_value
            if isinstance(prompt_digest_value, str) and prompt_digest_value:
                report_prompt_digest_sha256 = prompt_digest_value
        if generation_token is None or claim_token is None or task_run_id is None:
            LOG.warning("report finalization claim identity missing project=%s", project_id)
            return "server_write_failed"
        execution_error = _frozen_execution_error(
            report_execution_plan_json,
            report_execution_plan,
            report_execution_plan_sha256,
            frozen_report_prompt,
            report_prompt_digest_sha256,
        )
        if execution_error is not None:
            LOG.warning(
                "report frozen execution invalid project=%s error=%s",
                project_id,
                execution_error,
            )
            finish_disposition = _finish_report_claim(
                client,
                container_manager,
                project_id,
                worker,
                authenticated_dispatcher_id=authenticated_dispatcher_id,
                task_run_id=task_run_id,
                generation_token=generation_token,
                claim_token=claim_token,
                outcome=execution_error,
                finish_state=ReportFinishState(),
                finish_outbox=finish_outbox,
            )
            return execution_error if finish_disposition.accepted else (finish_disposition.error_type or "business_acceptance")
        assert report_execution_plan is not None
        prompt_group_value = report_execution_plan["report_prompt_group"]
        assert isinstance(prompt_group_value, str)
        prompt_group = prompt_group_value
        knowledge_value = report_execution_plan.get("knowledge_key")
        knowledge = knowledge_value if isinstance(knowledge_value, str) else None

    # 旧尝试即使同 generation 已写出 Artifact，也不能替代本次 claim 的结果。
    if durable and manual_request:
        if not all((template_key, template_version, template_digest_sha256, template_markdown)):
            LOG.warning("report finalization template snapshot missing project=%s", project_id)
            finish_disposition = _finish_report_claim(
                client,
                container_manager,
                project_id,
                worker,
                authenticated_dispatcher_id=authenticated_dispatcher_id,
                task_run_id=task_run_id,
                generation_token=generation_token,
                claim_token=claim_token,
                outcome="report_template_snapshot_missing",
                finish_state=ReportFinishState(),
                finish_outbox=finish_outbox,
            )
            return (
                "report_template_snapshot_missing"
                if finish_disposition.accepted
                else (finish_disposition.error_type or "business_acceptance")
            )
        if sha256(template_markdown.encode("utf-8")).hexdigest() != template_digest_sha256:
            LOG.warning("report finalization template digest mismatch project=%s", project_id)
            finish_disposition = _finish_report_claim(
                client,
                container_manager,
                project_id,
                worker,
                authenticated_dispatcher_id=authenticated_dispatcher_id,
                task_run_id=task_run_id,
                generation_token=generation_token,
                claim_token=claim_token,
                outcome="report_template_digest_mismatch",
                finish_state=ReportFinishState(),
                finish_outbox=finish_outbox,
            )
            return (
                "report_template_digest_mismatch"
                if finish_disposition.accepted
                else (finish_disposition.error_type or "business_acceptance")
            )

    finish_state = ReportFinishState()
    outcome = run_report_task(
        config,
        client,
        container_manager,
        project,
        export_yaml,
        worker,
        prompt_group=prompt_group,
        task_run_id=task_run_id,
        report_generation_token=generation_token,
        report_claim_token=claim_token,
        report_template_key=template_key,
        report_template_version=template_version,
        report_template_digest_sha256=template_digest_sha256,
        report_template_markdown=template_markdown,
        durable_request=durable,
        report_execution_plan=report_execution_plan,
        report_execution_plan_sha256=report_execution_plan_sha256,
        report_prompt_digest_sha256=report_prompt_digest_sha256,
        frozen_report_prompt=frozen_report_prompt,
        knowledge=knowledge,
        container_runtime=container_runtime,
        delivery_context=delivery_context,
        finish_state=finish_state,
        finish_outbox=finish_outbox,
        authenticated_dispatcher_id=authenticated_dispatcher_id,
        candidate_outbox=candidate_outbox,
    )
    if durable:
        assert task_run_id is not None
        assert generation_token is not None
        assert claim_token is not None
        if finish_state.delivery_pending:
            return outcome
        if outcome == "success" and (
            finish_state.artifact is None or finish_state.input_snapshot is None
        ):
            outcome = "server_write_failed"
            finish_state.task_run_result = _report_task_run_result(outcome)
        finish_disposition = _finish_report_claim(
            client,
            container_manager,
            project_id,
            worker,
            authenticated_dispatcher_id=authenticated_dispatcher_id,
            task_run_id=task_run_id,
            generation_token=generation_token,
            claim_token=claim_token,
            outcome=outcome,
            finish_state=finish_state,
            finish_outbox=finish_outbox,
            retain_accepted_entry=candidate_outbox is not None,
        )
        if finish_disposition.accepted:
            return outcome
        if outcome == "success" and can_fallback_report_finish(
            finish_disposition.safe_code
        ):
            ready_finish = finish_state.pending_finish
            if ready_finish is None:
                return "receipt_conflict"
            fallback_finish = failed_finish_fallback(
                ready_finish,
                safe_code=finish_disposition.safe_code,
            )
            fallback_outcome = fallback_finish.task_run_result.error_type
            assert fallback_outcome is not None
            fallback_state = ReportFinishState(
                task_run_result=fallback_finish.task_run_result.model_dump(
                    mode="json"
                ),
                input_snapshot=(
                    ReportArtifactIdentity(
                        fallback_finish.input_snapshot_artifact_id,
                        fallback_finish.input_snapshot_sha256,
                    )
                    if fallback_finish.input_snapshot_artifact_id is not None
                    and fallback_finish.input_snapshot_sha256 is not None
                    else None
                ),
                pending_finish=fallback_finish,
            )
            fallback_disposition = _finish_report_claim(
                client,
                container_manager,
                project_id,
                worker,
                authenticated_dispatcher_id=authenticated_dispatcher_id,
                task_run_id=task_run_id,
                generation_token=generation_token,
                claim_token=claim_token,
                outcome=fallback_outcome,
                finish_state=fallback_state,
                finish_outbox=finish_outbox,
                replace_entry=ready_finish,
                retain_accepted_entry=candidate_outbox is not None,
            )
            if fallback_disposition.accepted:
                return fallback_outcome
            return fallback_disposition.error_type or "business_acceptance"
        return finish_disposition.error_type or "business_acceptance"
    return outcome


def _collect_report_packet_json(client: CairnClient, project_id: str) -> str | None:
    """读取 Server 一次性聚合的权威 Report Packet。"""
    get_packet = getattr(client, "get_assessment_report_packet", None)
    if not callable(get_packet):
        return None
    resp = get_packet(project_id)
    if not resp.ok or not isinstance(resp.data, dict):
        LOG.warning(
            "report packet read failed project=%s status=%s code=%s",
            project_id,
            resp.status_code,
            response_detail_code(resp),
        )
        return None
    packet_json = format_json_block(resp.data)
    try:
        packet = _frozen_report_packet(packet_json)
        if packet["project"].get("id") != project_id:
            raise ValueError("report_packet_project_mismatch")
    except (TypeError, ValueError) as exc:
        LOG.warning("report packet invalid project=%s error=%s", project_id, exc)
        return None
    return packet_json


def _packet_list_json(report_packet_json: str, key: str) -> str:
    try:
        packet = json.loads(report_packet_json)
    except (TypeError, ValueError):
        return "[]"
    values = packet.get(key) if isinstance(packet, dict) else None
    return format_json_block(values if isinstance(values, list) else [])


def _packet_is_goal_driven(packet: dict[str, object]) -> bool:
    project = packet.get("project")
    policy = packet.get("policy")
    return (
        isinstance(project, dict)
        and project.get("assessment_protocol") == GOAL_DRIVEN_PROTOCOL
    ) or (
        isinstance(policy, dict)
        and policy.get("protocol") == GOAL_DRIVEN_PROTOCOL
    )


def _packet_coverage_json(report_packet_json: str) -> str:
    """新协议传递业务工作摘要；历史协议继续传递原 Coverage Cell。"""
    try:
        packet = json.loads(report_packet_json)
    except (TypeError, ValueError):
        return "[]"
    if not isinstance(packet, dict):
        return "[]"
    if _packet_is_goal_driven(packet):
        summary = packet.get("assessment_summary")
        coverage = summary.get("coverage") if isinstance(summary, dict) else None
        return format_json_block(coverage if isinstance(coverage, dict) else {})
    values = packet.get("coverage_cells")
    return format_json_block(values if isinstance(values, list) else [])


def _frozen_report_packet(report_packet_json: str) -> dict[str, object]:
    """缺失或损坏的冻结输入不能被当成零发现报告。"""
    packet = json.loads(report_packet_json)
    if not isinstance(packet, dict) or packet.get("version") != 2:
        raise ValueError("report_packet_unsupported")
    if not isinstance(packet.get("project"), dict):
        raise ValueError("report_packet_project_missing")
    summary = packet.get("assessment_summary")
    if not isinstance(summary, dict) or not isinstance(summary.get("execution"), dict):
        raise ValueError("report_packet_summary_missing")
    coverage = summary.get("coverage")
    if not isinstance(coverage, dict) or any(
        not isinstance(coverage.get(key), list)
        or any(not isinstance(item, dict) for item in coverage[key])
        for key in ("tested", "blocked", "untested")
    ):
        raise ValueError("report_packet_coverage_missing")
    for key in ("vulnerabilities", "observations", "observation_statuses"):
        values = packet.get(key)
        if not isinstance(values, list):
            raise ValueError(f"report_packet_list_missing:{key}")
        seen: set[str] = set()
        for item in values:
            if not isinstance(item, dict):
                raise ValueError(f"report_packet_item_invalid:{key}")
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id.strip():
                raise ValueError(f"report_packet_id_missing:{key}")
            if item_id in seen:
                raise ValueError(f"report_packet_id_duplicate:{key}:{item_id}")
            seen.add(item_id)
    observations = {item["id"]: item for item in packet["observations"]}
    statuses = {item["id"]: item for item in packet["observation_statuses"]}
    if observations.keys() != statuses.keys():
        raise ValueError("report_packet_observation_status_mismatch")
    vulnerability_ids = {item["id"] for item in packet["vulnerabilities"]}
    for observation_id, observation in observations.items():
        vulnerability_id = observation.get("vulnerability_id")
        status = statuses[observation_id]
        if (
            vulnerability_id not in vulnerability_ids
            or status.get("vulnerability_id") != vulnerability_id
            or status.get("verification_status") not in {
                "confirmed", "not_reproduced", "environment_changed",
                "insufficient_evidence", "conflicting", "run_failed", "not_run",
                "pending", "not_scheduled",
            }
        ):
            raise ValueError(f"report_packet_observation_invalid:{observation_id}")
    return packet


def _packet_vulnerability_context(
    report_packet_json: str,
) -> tuple[str, dict[str, str]]:
    packet = _frozen_report_packet(report_packet_json)
    vulnerabilities = packet["vulnerabilities"]
    return format_json_block(vulnerabilities), {
        item["id"]: str(item.get("title") or "") for item in vulnerabilities
    }


def _report_completeness_error(
    data: object,
    *,
    id_to_title: dict[str, str],
) -> str | None:
    """只验证结构与冻结 ID 完整性，不以篇幅、标题或关键词充当事实验收。"""
    try:
        result = ReportData.model_validate(data)
    except ValueError:
        return "report_data_invalid"
    if not result.executive_summary.strip():
        return "report_executive_summary_empty"
    seen: set[str] = set()
    for finding in result.findings:
        vulnerability_id = finding.vulnerability_id
        if vulnerability_id not in id_to_title:
            return f"report_vulnerability_unknown:{vulnerability_id}"
        if vulnerability_id in seen:
            return f"report_vulnerability_duplicate:{vulnerability_id}"
        seen.add(vulnerability_id)
    missing = sorted(id_to_title.keys() - seen)
    if missing:
        return f"report_vulnerability_missing:{missing[0]}"
    return None


def _report_table(
    headers: tuple[str, ...], rows: list[tuple[object, ...]],
) -> str:
    def cell(value: object) -> str:
        if value is None or value == "":
            return "未记录"
        return (
            str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace("|", "&#124;")
            .replace("\r", "").replace("\n", "<br>")
        )

    if not rows:
        return "冻结快照未记录此类条目。"
    return "\n".join([
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *("| " + " | ".join(cell(value) for value in row) + " |" for row in rows),
    ])


def _report_list_text(value: object) -> str:
    """将冻结快照中的字符串列表压缩为稳定、可读的单元格文本。"""
    if not isinstance(value, list):
        return ""
    return "; ".join(str(item).strip() for item in value if str(item).strip())


def _report_evidence_text(value: object) -> str:
    """展示服务端已解析的证据引用，不把句柄或缺失字段写成证据。"""
    if not isinstance(value, list):
        return ""
    references: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        artifact_id = str(item.get("artifact_id") or "").strip()
        if not artifact_id:
            continue
        reference = artifact_id
        pointer = str(item.get("pointer") or "").strip()
        verifier_run_id = str(item.get("verifier_run_id") or "").strip()
        if pointer:
            reference += f" {pointer}"
        if verifier_run_id:
            reference += f" (verifier {verifier_run_id})"
        references.append(reference)
    return "; ".join(references)


def _render_report_markdown(
    data: dict[str, object], *, report_packet_json: str,
) -> str:
    """事实区仅使用冻结快照，模型补充区不修改事实、复核结论或原始证据。"""
    packet = _frozen_report_packet(report_packet_json)
    vulnerabilities = packet["vulnerabilities"]
    error = _report_completeness_error(
        data, id_to_title={item["id"]: str(item.get("title") or "") for item in vulnerabilities},
    )
    if error is not None:
        raise ValueError(error)
    result = ReportData.model_validate(data)
    findings = {item.vulnerability_id: item for item in result.findings}
    project = packet["project"]
    summary = packet["assessment_summary"]
    execution = summary.get("execution") or {}
    observations = packet["observations"]
    statuses = packet["observation_statuses"]
    status_by_id = {item["id"]: item for item in statuses}
    status_counts = Counter(item["verification_status"] for item in statuses)
    severity_counts = Counter(str(item.get("severity") or "未记录") for item in vulnerabilities)
    stopped = project.get("status") == "stopped"
    goal_driven = _packet_is_goal_driven(packet)
    execution_identity = (
        _report_table(("项目 ID", "标题", "项目状态", "协议", "停止原因"), [(
            project.get("id"), project.get("title"), project.get("status"),
            project.get("assessment_protocol"), summary.get("stop_reason"),
        )])
        if goal_driven
        else _report_table(("项目 ID", "标题", "项目状态", "阶段", "停止原因"), [(
            project.get("id"), project.get("title"), project.get("status"),
            execution.get("phase"), summary.get("stop_reason"),
        )])
    )
    lines = [
        "# 安全评估部分报告" if stopped else "# 安全评估报告",
        "## 报告边界与执行状态（冻结快照）",
        (
            "项目已停止（stopped）；本报告为截止快照的部分报告，不代表评估完成或完整覆盖。"
            if stopped else "报告仅描述冻结快照内的评估记录；项目状态不等于完整覆盖或无剩余风险。"
        ),
        "报告交付与目标测试状态独立；本文生成不代表修复已实施或修复已验证。",
        "事实区由程序生成。模型解释与建议仅供分析，不是新的测试、源码审计或复核证据。",
        execution_identity,
        "## 执行摘要（模型解释）",
        result.executive_summary,
        "## 授权范围（冻结快照）",
        _report_table(("Target ID", "类型", "范围"), [
            (item.get("id"), item.get("type"), item.get("value"))
            for item in packet.get("targets", [])
        ]),
        "## 漏洞与复核计数（冻结快照）",
        f"Canonical 漏洞记录数：{len(vulnerabilities)}；Observation 数：{len(observations)}。",
        "同一漏洞的多个 Observation 不重复计入漏洞数；记录状态不替代逐 Observation 的独立复核。",
        _report_table(("记录严重度", "漏洞记录数"), sorted(severity_counts.items())),
        _report_table(("Observation 复核状态", "数量"), sorted(status_counts.items())),
    ]
    if not vulnerabilities:
        lines.append("冻结快照中没有漏洞记录；未发现已确认漏洞不等于证明目标不存在漏洞。")
    lines.extend([
        (
            "## 工作结果与未完成事项（冻结快照）"
            if goal_driven
            else "## 覆盖与未完成义务（冻结快照）"
        ),
        (
            "目标驱动工作按已接受的 ExperimentRecord 和 Intent 结论汇总；已测、阻塞、未测分别列出，不把未执行方向写成安全。"
            if goal_driven
            else (
                "已启用结构化 Coverage；已测、阻塞、未测分别列出，不将跳过项算作已测。"
                if execution.get("minimum_coverage_enabled")
                else "结构化 Coverage 未启用或未记录；缺失数据不能解释为零工作或完整覆盖。"
            )
        ),
    ])
    coverage = summary.get("coverage") or {}
    cells = {item.get("slot_key"): item for item in packet.get("coverage_cells", [])}
    for key, label in (("tested", "已测"), ("blocked", "阻塞"), ("untested", "未测/未完成")):
        entries = coverage.get(key, [])
        if goal_driven:
            table = _report_table(("Intent", "工作", "优先级", "结果", "原因", "Fact"), [
                (item.get("intent_id"), item.get("description"), item.get("priority"),
                 item.get("result"), item.get("reason"), item.get("fact_id"))
                for item in entries
            ])
        else:
            table = _report_table(("Cell", "结果", "原因", "Fact", "Surface", "测试族"), [
                (item.get("slot_key"), item.get("result"), item.get("reason"), item.get("fact_id"),
                 cells.get(item.get("slot_key"), {}).get("surface_key"),
                 cells.get(item.get("slot_key"), {}).get("test_family"))
                for item in entries
            ])
        lines.extend([f"### {label}（{len(entries)}）", table])
    if goal_driven:
        experiment_entries = [
            item
            for key in ("tested", "blocked", "untested")
            for item in coverage.get(key, [])
            if item.get("hypothesis")
        ]
        lines.extend([
            "### 已接受实验详情",
            "以下内容直接来自冻结的 ExperimentRecord；观察、限制和证据引用彼此独立，缺失项不由模型补写。",
            _report_table(("Fact", "假设", "观察", "限制", "尝试策略", "证据"), [
                (
                    item.get("fact_id"),
                    item.get("hypothesis"),
                    _report_list_text(item.get("observations")),
                    _report_list_text(item.get("limitations")),
                    _report_list_text(item.get("attempted_strategies")),
                    _report_evidence_text(item.get("evidence_refs")),
                )
                for item in experiment_entries
            ]),
        ])
    else:
        selected = summary.get("selected_deep", [])
        lines.extend([
            "### 已选择的深测义务",
            _report_table(("Intent", "Cell", "选择 Fact", "结果 Fact", "处置", "处置原因"), [
                (item.get("id"), item.get("slot_key"), item.get("depth_review_fact_id"),
                 item.get("to_fact_id"), item.get("conclusion"), item.get("conclusion_reason"))
                for item in selected
            ]),
        ])
    budget_fields = (
        "budget", "deadline", "authorization_expires_at", "attempts_started",
        "verification_reserve", "verification_attempts_started",
        "remaining_total", "remaining_discovery",
    ) if goal_driven else (
        "budget", "deadline", "attempts_started", "verification_reserve",
        "verification_attempts_started", "remaining_total", "remaining_discovery",
        "depth_review_fact_id", "accepted_input_digest",
    )
    lines.extend([
        "## 预算与执行限制（冻结快照）",
        "预算单位是 logical attempt，不是模型调用次数、token 或 API 费用。未使用的复核保留容量不等于已完成复核。",
        _report_table(("预算字段", "冻结值"), [
            (key, "无固定次数上限" if key == "budget" and key in execution and execution[key] is None else execution.get(key))
            for key in budget_fields
        ]),
        "未记录的预算不代表预算为零；budget=null 表示没有固定次数上限。",
        _report_table(("任务类型", "记录的执行次数"), [
            (item.get("task_type"), item.get("execution_count"))
            for item in summary.get("calls_by_task_type", [])
        ]),
        "## 逐 Observation 复核状态（冻结快照）",
        "run_failed、not_run、pending、not_scheduled 表示尚无对应复核结论；不能改写成 insufficient_evidence 或 confirmed。conflicting 保留相互冲突的实际结论。",
        _report_table(("Observation", "漏洞 ID", "复核状态", "来源 Fact", "发现 TaskRun", "证据包 Artifact"), [
            (item["id"], item.get("vulnerability_id"), status_by_id[item["id"]]["verification_status"],
             item.get("source_fact_id"), item.get("task_run_id"), item.get("packet_artifact_id"))
            for item in observations
        ]),
        _report_table(("Observation", "Verification", "实际结论", "Verifier TaskRun"), [
            (item["id"], verification.get("id"), verification.get("outcome"), verification.get("verifier_task_run_id"))
            for item in statuses for verification in item.get("verifications", [])
        ]),
        _report_table(("Observation", "Verifier Intent", "处置", "处置原因", "结果 Fact"), [
            (item["id"], intent.get("id"), intent.get("conclusion"), intent.get("conclusion_reason"), intent.get("to_fact_id"))
            for item in statuses for intent in item.get("intents", [])
        ]),
        "## 漏洞解释、修复建议与回归方法",
        "以下仅补充解释与建议。复现说明基于已有证据，报告阶段不执行目标测试；回归方法未执行，不能视为修复已验证。",
    ])
    severity_order = {name: index for index, name in enumerate(("critical", "high", "medium", "low", "info"))}
    for vulnerability in sorted(vulnerabilities, key=lambda item: (severity_order.get(item.get("severity"), 5), item["id"])):
        finding = findings[vulnerability["id"]]
        lines.extend([
            f"### 漏洞 {vulnerability['id']}",
            _report_table(("漏洞 ID", "标题", "严重度", "目标", "记录状态", "来源 Fact"), [(
                vulnerability["id"], vulnerability.get("title"), vulnerability.get("severity"),
                vulnerability.get("target"), vulnerability.get("status"), vulnerability.get("source_fact_id"),
            )]),
            "#### 业务影响（模型解释）", finding.impact or "未补充；以冻结证据为准。",
            "#### 根因分析（模型解释；推断不构成源码定位证明）", finding.root_cause or "证据不足，无法定位根因。",
            "#### 复现说明（报告阶段未执行）", finding.repro_steps,
            "#### 修复建议（未实施）", finding.remediation,
            "#### 修复后回归方法（未执行）", finding.regression_steps,
            "#### 本项限制（模型解释）", finding.limitations or "未补充；冻结快照中的限制仍适用。",
        ])
    lines.extend([
        "## 限制与剩余风险",
        "未测范围、阻塞条件、未运行或冲突的复核均保留为缺口，不能由文字补齐。黑盒证据不足以证明具体源码文件、行号或修复效果。",
        result.limitations or "模型未补充限制；这不表示不存在限制。",
        "## 证据索引（冻结快照）",
        "索引只列出输入快照中的 Artifact 与来源绑定，不把模型生成的说明作为新增实测证据。原始请求、响应和复现记录保持不变。",
        _report_table(("Artifact", "类型", "TaskRun", "URI", "SHA-256"), [
            (item.get("id"), item.get("kind"), item.get("task_run_id"), item.get("uri"), item.get("sha256"))
            for item in packet.get("artifacts", [])
        ]),
        _report_table(("Fact", "Artifact", "Pointer", "Verifier TaskRun"), [
            (item.get("fact_id"), item.get("artifact_id"), item.get("pointer"), item.get("verifier_run_id"))
            for item in packet.get("evidence_links", [])
        ]),
        _report_table(("Verification", "Observation", "Artifact", "Pointer", "Verifier TaskRun"), [
            (item.get("id"), item.get("observation_id"), evidence.get("artifact_id"), evidence.get("pointer"), item.get("verifier_task_run_id"))
            for item in packet.get("verifications", []) for evidence in item.get("evidence", [])
        ]),
        "### 漏洞链候选",
        f"快照包含 {len(packet.get('chain_candidates', []))} 项候选关联；候选关联不等于已复现的利用链。",
    ])
    return "\n\n".join(lines) + "\n"


def _prepare_report_output(
    project_id: str,
    *,
    data: dict[str, object],
    report_packet_json: str,
    report_generation_token: str | None = None,
    report_template_key: str | None = None,
    report_template_version: int | None = None,
    report_template_digest_sha256: str | None = None,
    report_policy_version: object = None,
    report_execution_plan_sha256: str | None = None,
    report_prompt_digest_sha256: str | None = None,
    completeness_rule_version: object = None,
    input_snapshot_artifact_id: str | None = None,
    input_snapshot_sha256: str | None = None,
) -> tuple[str, dict[str, object]] | str:
    """一次性渲染 exact Markdown 与 Artifact metadata，恢复时禁止再次渲染。"""
    try:
        packet = _frozen_report_packet(report_packet_json)
        if packet["project"].get("id") != project_id:
            raise ValueError("report_packet_project_mismatch")
        report_markdown = _render_report_markdown(
            data,
            report_packet_json=report_packet_json,
        )
    except (TypeError, ValueError) as exc:
        LOG.warning("report render rejected project=%s error=%s", project_id, exc)
        return "request_contract"
    return report_markdown, {
        "source": "report_task",
        "format": "markdown",
        "report_generation_token": report_generation_token,
        "report_template_key": report_template_key,
        "report_template_version": report_template_version,
        "report_template_digest_sha256": report_template_digest_sha256,
        "report_policy_version": report_policy_version,
        "report_execution_plan_sha256": report_execution_plan_sha256,
        "report_prompt_digest_sha256": report_prompt_digest_sha256,
        "input_snapshot_artifact_id": input_snapshot_artifact_id,
        "input_snapshot_sha256": input_snapshot_sha256,
        "completeness_rule_version": completeness_rule_version,
    }


def _write_report_results(
    client: CairnClient,
    project_id: str,
    task_run_id: str | None,
    *,
    data: dict[str, object],
    report_packet_json: str,
    report_generation_token: str | None = None,
    report_template_key: str | None = None,
    report_template_version: int | None = None,
    report_template_digest_sha256: str | None = None,
    report_policy_version: object = None,
    report_execution_plan_sha256: str | None = None,
    report_prompt_digest_sha256: str | None = None,
    completeness_rule_version: object = None,
    input_snapshot_artifact_id: str | None = None,
    input_snapshot_sha256: str | None = None,
) -> ReportArtifactIdentity | str:
    """兼容直接调用：渲染后立即写入 Artifact。durable 路径先写 journal。"""
    prepared = _prepare_report_output(
        project_id,
        data=data,
        report_packet_json=report_packet_json,
        report_generation_token=report_generation_token,
        report_template_key=report_template_key,
        report_template_version=report_template_version,
        report_template_digest_sha256=report_template_digest_sha256,
        report_policy_version=report_policy_version,
        report_execution_plan_sha256=report_execution_plan_sha256,
        report_prompt_digest_sha256=report_prompt_digest_sha256,
        completeness_rule_version=completeness_rule_version,
        input_snapshot_artifact_id=input_snapshot_artifact_id,
        input_snapshot_sha256=input_snapshot_sha256,
    )
    if isinstance(prepared, str):
        return prepared
    report_markdown, metadata = prepared
    expected_sha256 = sha256(report_markdown.encode("utf-8")).hexdigest()
    resp = client.create_artifact(
        project_id,
        task_run_id=task_run_id,
        kind="report",
        content=report_markdown,
        filename=REPORT_ARTIFACT_FILENAME,
        metadata=metadata,
        category="deliverable",
    )
    disposition = classify_write_response(resp)
    if not disposition.accepted:
        LOG.warning(
            "report artifact create failed project=%s status=%s code=%s disposition=%s",
            project_id,
            resp.status_code,
            disposition.safe_code,
            disposition.disposition,
        )
        return disposition.error_type or "business_acceptance"
    response_data = resp.data if isinstance(resp.data, dict) else {}
    artifact_id = response_data.get("id")
    persisted_sha256 = response_data.get("sha256")
    if (
        not isinstance(artifact_id, str)
        or not artifact_id
        or persisted_sha256 != expected_sha256
    ):
        LOG.warning(
            "report artifact receipt invalid project=%s status=%s",
            project_id,
            resp.status_code,
        )
        return "business_acceptance"
    LOG.info("report artifact created project=%s artifact=%s", project_id, artifact_id)
    return ReportArtifactIdentity(artifact_id, persisted_sha256)


def run_report_task(
    config: DispatchConfig,
    client: CairnClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    export_yaml: str,
    worker: WorkerConfig,
    *,
    prompt_group: str,
    task_run_id: str | None = None,
    report_generation_token: str | None = None,
    report_claim_token: str | None = None,
    report_template_key: str | None = None,
    report_template_version: int | None = None,
    report_template_digest_sha256: str | None = None,
    report_template_markdown: str | None = None,
    durable_request: bool = False,
    report_execution_plan: dict[str, object] | None = None,
    report_execution_plan_sha256: str | None = None,
    report_prompt_digest_sha256: str | None = None,
    frozen_report_prompt: str | None = None,
    knowledge: str | None = None,
    container_runtime: ContainerRuntimeOptions | None = None,
    delivery_context: dict | None = None,
    finish_state: ReportFinishState | None = None,
    finish_outbox: ReportFinishOutbox | None = None,
    authenticated_dispatcher_id: str | None = None,
    candidate_outbox: CandidateCaptureOutbox | None = None,
) -> str | WorkerResultFailure:
    """同步执行报告；程序失败返回固定诊断，调用方据此告警但不阻断 complete。"""
    project_id = project.project.id
    task_started = time.perf_counter()
    # TaskRun 由报告 claim 原子创建，不占用项目的 Reason 租约。
    input_snapshot_artifact_id: str | None = None
    input_snapshot_sha256: str | None = None
    result: ProcessResult | None = None
    terminal_state = finish_state if finish_state is not None else ReportFinishState()
    if durable_request and finish_outbox is None:
        # 低层入口也不能绕过 execution/output fence 直接运行模型。
        terminal_state.delivery_pending = True
        return "server_write_failed"
    # 所有可执行报告都必须持有新冻结计划和 claim，不再拼装 live 数据兼容报告。
    if not durable_request or report_execution_plan is None or frozen_report_prompt is None or report_claim_token is None:
        return "report_execution_plan_missing"
    if not task_run_id or not report_generation_token:
        return "report_claim_identity_missing"
    requires_authoritative_packet = True
    cancellation = TaskCancellation(generation=getattr(project.project, "execution_generation", 0))
    execution_timeout = config.tasks.report.timeout
    try:
        driver = get_driver(worker.type)
        advisory_context = knowledge_base_runtime_context(
            knowledge,
            advisory=container_runtime,
        )
        runtime_context = {
            "knowledge_base": advisory_context,
            "project_resources": {
                "bundle_digest": advisory_context.get("bundle_digest"),
                "manifest_path": advisory_context.get("manifest_path"),
                "read_only": True,
                "resources": [
                    item
                    for item in advisory_context.get("resources", [])
                    if isinstance(item, dict)
                    and item.get("source") == "project_resource"
                ],
            },
        }
        def _finish(
            status: str,
            *,
            error_type: str | None = None,
            error_message: str | None = None,
            failure: WorkerResultFailure | None = None,
        ) -> None:
            if failure is not None:
                terminal_state.task_run_result = _report_task_run_result(failure)
                return
            terminal_state.task_run_result = {
                "status": status,
                "exit_code": 0 if status == "succeeded" else None,
                "error_type": error_type,
                "error_message": (error_message or "")[:2000],
                "worker_failure": None,
            }

        report_packet_json: str | None = None
        if requires_authoritative_packet:
            if delivery_context is not None:
                container_name, frozen_delivery, execution_timeout = resume_delivery(
                    container_manager, project_id, worker, delivery_context, "report_execute",
                )
                frozen_inputs = frozen_delivery["inputs"]
                if frozen_inputs.get("generation_token") != report_generation_token:
                    raise DeliveryUnavailable
                try:
                    snapshot_json = container_manager.read_file(container_name, frozen_inputs["snapshot_path"]).decode("utf-8")
                    if sha256(snapshot_json.encode("utf-8")).hexdigest() != frozen_inputs["input_snapshot_sha256"]:
                        raise DeliveryUnavailable
                    frozen = json.loads(snapshot_json)
                    report_packet_json = json.dumps(frozen["report_packet"], ensure_ascii=False)
                    export_yaml = frozen["graph_yaml"]
                    if _frozen_report_packet(report_packet_json)["project"]["id"] != project_id:
                        raise DeliveryUnavailable
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    raise DeliveryUnavailable from exc
            else:
                report_packet_json = _collect_report_packet_json(client, project_id)
                if report_packet_json is None:
                    _finish(
                        "failed",
                        error_type="report_packet_read_failed",
                        error_message="权威 Report Packet 读取失败",
                    )
                    return "report_packet_read_failed"
                frozen_graph_yaml = export_yaml.strip()
                snapshot_json = format_json_block(
                    {
                        "version": 1,
                        "report_packet": json.loads(report_packet_json),
                        "graph_yaml": frozen_graph_yaml,
                    }
                )
            snapshot_digest = sha256(snapshot_json.encode("utf-8")).hexdigest()
            input_snapshot_sha256 = snapshot_digest
            if delivery_context is not None:
                snapshot_id = frozen_inputs.get("input_snapshot_artifact_id")
                if not isinstance(snapshot_id, str) or not snapshot_id:
                    raise DeliveryUnavailable
            else:
                snapshot_resp = client.create_artifact(
                    project_id,
                    task_run_id=task_run_id,
                    kind="report",
                    content=snapshot_json,
                    filename="report-input-snapshot.json",
                    metadata={
                        "source": "report_task",
                        "format": "json",
                        "report_generation_token": report_generation_token,
                        "report_template_key": report_template_key,
                        "report_template_version": report_template_version,
                        "report_template_digest_sha256": report_template_digest_sha256,
                        "report_execution_plan_sha256": report_execution_plan_sha256,
                        "report_prompt_digest_sha256": report_prompt_digest_sha256,
                        "report_policy_version": (
                            report_execution_plan.get("report_policy_version")
                            if report_execution_plan is not None
                            else None
                        ),
                        "packet_version": (
                            report_execution_plan.get("packet_version")
                            if report_execution_plan is not None
                            else None
                        ),
                        "snapshot_sha256": snapshot_digest,
                    },
                    category="deliverable",
                )
                snapshot_disposition = classify_write_response(snapshot_resp)
                if not snapshot_disposition.accepted:
                    snapshot_outcome = (
                        snapshot_disposition.error_type or "business_acceptance"
                    )
                    if snapshot_disposition.retry_same_write:
                        terminal_state.delivery_pending = True
                    else:
                        _finish(
                            "failed",
                            error_type=_report_failure_error_type(snapshot_outcome),
                            error_message="报告输入快照 Artifact 写入失败",
                        )
                    return snapshot_outcome
                snapshot_data = (
                    snapshot_resp.data
                    if isinstance(snapshot_resp.data, dict)
                    else {}
                )
                snapshot_id = snapshot_data.get("id")
                if (
                    not isinstance(snapshot_id, str)
                    or not snapshot_id
                    or snapshot_data.get("sha256") != snapshot_digest
                ):
                    _finish(
                        "failed",
                        error_type="business_acceptance",
                        error_message="报告输入快照 Artifact 收据无效",
                    )
                    return "business_acceptance"
                if report_claim_token is None:
                    _finish(
                        "failed",
                        error_type="request_contract",
                        error_message="报告 claim token 缺失",
                    )
                    return "request_contract"
                bind_snapshot = retry_graph_write(
                    lambda: client.bind_assessment_report_input_snapshot(
                        project_id,
                        worker.name,
                        claim_token=report_claim_token,
                        artifact_id=snapshot_id,
                        digest_sha256=snapshot_digest,
                    )
                )
                bind_disposition = classify_write_response(bind_snapshot)
                if not bind_disposition.accepted:
                    LOG.warning(
                        "report input snapshot bind failed project=%s artifact=%s status=%s code=%s disposition=%s",
                        project_id,
                        snapshot_id,
                        bind_snapshot.status_code,
                        bind_disposition.safe_code,
                        bind_disposition.disposition,
                    )
                    bind_outcome = bind_disposition.error_type or "business_acceptance"
                    if bind_disposition.retry_same_write:
                        terminal_state.delivery_pending = True
                    else:
                        _finish(
                            "failed",
                            error_type=_report_failure_error_type(bind_outcome),
                            error_message="报告输入快照绑定失败",
                        )
                    return bind_outcome
            input_snapshot_artifact_id = snapshot_id
            terminal_state.input_snapshot = ReportArtifactIdentity(
                snapshot_id,
                snapshot_digest,
            )
            runtime_context["report_inputs"] = {
                "input_snapshot_artifact_id": snapshot_id,
                "input_snapshot_sha256": snapshot_digest,
            }
            updated = retry_graph_write(
                lambda: client.update_task_run(
                    project_id,
                    task_run_id,
                    status="running",
                    runtime_context=runtime_context,
                )
            )
            update_disposition = classify_write_response(updated)
            if not update_disposition.accepted:
                update_outcome = (
                    update_disposition.error_type or "business_acceptance"
                )
                if update_disposition.retry_same_write:
                    terminal_state.delivery_pending = True
                else:
                    _finish(
                        "failed",
                        error_type=_report_failure_error_type(update_outcome),
                        error_message="报告 TaskRun 运行态写入失败",
                    )
                return update_outcome

        if delivery_context is None:
            container_name = container_manager.ensure_running(project_id, container_runtime)

        # 输入只使用已绑定快照的 Packet 与图谱，不重新读取 live 业务状态。
        assert report_packet_json is not None
        final_export_yaml = export_yaml.strip()
        packet_reference, graph_reference, readonly_inputs = (
            build_report_readonly_input_references(
                report_packet_json,
                final_export_yaml,
            )
        )
        vulns_json, id_to_title = _packet_vulnerability_context(report_packet_json)
        if delivery_context is not None:
            prompt = (
                f"{DELIVERY_PROMPT}\n\n"
                "如需恢复原冻结输入，只能通过以下只读引用重新读取：\n\n"
                f"{packet_reference}\n\n{graph_reference}"
            )
            session = frozen_delivery["session_id"]
        else:
            coverage_json = _packet_coverage_json(report_packet_json)
            chain_candidates_json = _packet_list_json(report_packet_json, "chain_candidates")
            replacements = {
                "project_summary": format_json_block(project.project.model_dump()),
                "coverage_json": coverage_json,
                "vulns_json": vulns_json,
                "chain_candidates_json": chain_candidates_json,
                "report_packet_json": packet_reference,
                # 图谱使用与 Packet 一起冻结的输入快照，不在生成过程中刷新。
                "graph_yaml": graph_reference,
                "knowledge_base": knowledge_base_prompt_context(
                    knowledge,
                    advisory=container_runtime,
                ),
            }
            prompt_source = (
                frozen_report_prompt
                if durable_request and frozen_report_prompt is not None
                else load_prompt(prompt_group, "report.md")
            )
            prompt = render_prompt(prompt_source, replacements)
            if report_template_markdown is not None:
                prompt += (
                    "\n\n---\n\n"
                    "以下是本次生成已冻结的 Markdown 展示模板。必须保留其章节意图，"
                    "但不得用模板覆盖 Report Packet 的事实、证据或验证状态：\n\n"
                    "<frozen_report_template>\n"
                    f"{report_template_markdown}\n"
                    "</frozen_report_template>\n"
                )
            record_prompt_rendered(
                client,
                project_id,
                task_run_id,
                phase="report_execute",
                prompt_group=prompt_group,
                prompt_name="report.md",
                prompt=prompt,
                worker=worker,
                replacement_keys=list(replacements),
            )

            session = driver.prepare_session()
            snapshot_path = f"/tmp/cairn-prompts/report-delivery-{task_run_id}.json"
            container_manager.write_text_file(container_name, snapshot_path, snapshot_json, owner="kali", mode=0o600)
            frozen_delivery = register_delivery(
                client, container_manager, container_name, project, task_run_id, worker,
                phase="report_execute", session=session, timeout_seconds=execution_timeout,
                inputs={"snapshot_path": snapshot_path, "input_snapshot_artifact_id": input_snapshot_artifact_id,
                        "input_snapshot_sha256": input_snapshot_sha256, "generation_token": report_generation_token},
            )
        execution_timeout = remaining_delivery_seconds(frozen_delivery)
        command = driver.build_execute(
            worker, prompt, session, phase="report_execute", project_id=project_id,
            task_run_id=task_run_id, timeout_seconds=execution_timeout,
            resume=delivery_context is not None, conclude_only=delivery_context is not None,
        )
        # Pi private request 必须保持第一个输入；冻结输入只追加到本次进程生命周期。
        command.input_files = (*command.input_files, *readonly_inputs)
        if finish_outbox is not None:
            assert input_snapshot_artifact_id is not None
            assert input_snapshot_sha256 is not None
            journal_owner = authenticated_dispatcher_id or config.dispatcher_id
            execution_entry = pending_report_execution(
                project_id=project_id,
                authenticated_dispatcher_id=journal_owner,
                worker=worker.name,
                claim_sha256=sha256(
                    report_claim_token.encode("utf-8")
                ).hexdigest(),
                generation_token=report_generation_token,
                task_run_id=task_run_id,
                input_snapshot_artifact_id=input_snapshot_artifact_id,
                input_snapshot_sha256=input_snapshot_sha256,
            )
            try:
                # fence 必须先 durable，之后才允许进入任何模型执行路径。
                finish_outbox.persist(execution_entry)
            except ReportFinishOutboxConflict:
                LOG.error(
                    "report execution journal conflict project=%s task_run=%s code=assessment_report_finish_receipt_conflict",
                    project_id,
                    task_run_id,
                )
                terminal_state.delivery_pending = True
                return "receipt_conflict"
            except OSError:
                LOG.error(
                    "report execution journal unavailable project=%s task_run=%s code=report_finish_outbox_unavailable",
                    project_id,
                    task_run_id,
                )
                terminal_state.delivery_pending = True
                return "server_write_failed"
            terminal_state.pending_entry = execution_entry
        execute_started = time.perf_counter()
        result = run_worker_process(
            container_manager,
            container_name,
            worker,
            command,
            phase="report_execute",
            timeout_seconds=execution_timeout,
            cancellation=cancellation,
            client=client,
            project_id=project_id,
            task_run_id=task_run_id,
            candidate_outbox=candidate_outbox,
        )
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        total_ms = int((time.perf_counter() - task_started) * 1000)
        record_process_artifacts(
            client,
            project_id,
            task_run_id,
            phase="report_execute",
            result=result,
        )
        failure = worker_process_failure(
            result, phase="report_execute", project_id=project_id,
            task_run_id=task_run_id, session=session,
        )
        if failure is not None:
            error_type = worker_failure_error_type(failure)
            status = "timed_out" if error_type == "worker_timeout" else "cancelled" if error_type == "cancelled" else "failed"
            _finish(status, failure=failure)
            return failure
        decoded = decode_pi_result(
            result.read_stdout(),
            phase="report_execute",
            project_id=project_id, task_run_id=task_run_id, session=session,
            validator=validate_report_payload,
        )
        if decoded.failure is not None:
            error_type = worker_failure_error_type(decoded.failure)
            LOG.warning(
                "report result failed project=%s worker=%s class=%s code=%s fingerprint=%s error=%s",
                project_id,
                worker.name,
                decoded.failure.failure_class,
                decoded.failure.code,
                decoded.failure.fingerprint,
                decoded.failure.message,
            )
            record_validation_result(
                client,
                project_id,
                task_run_id,
                phase="report_execute",
                status="invalid",
                outcome=error_type,
                payload=decoded.payload,
                error_message=decoded.failure.message,
                failure=decoded.failure,
            )
            failure = replace(decoded.failure, exit_code=result.returncode)
            _finish("failed", failure=failure)
            return failure
        assert decoded.payload is not None
        assert decoded.kind is not None
        payload = decoded.payload
        kind = decoded.kind
        data = decoded.validated
        record_validation_result(
            client,
            project_id,
            task_run_id,
            phase="report_execute",
            status="rejected" if kind == "rejected" else "valid",
            outcome=kind,
            payload=payload,
        )
        if kind == "rejected" or data is None:
            LOG.warning(
                "report rejected project=%s worker=%s execute_ms=%s total_ms=%s",
                project_id,
                worker.name,
                execute_ms,
                total_ms,
            )
            _finish("failed", error_type="worker_rejected", error_message="报告任务被 worker 拒绝")
            return "worker_rejected"
        completeness_error = _report_completeness_error(data, id_to_title=id_to_title)
        if completeness_error is not None:
            record_validation_result(
                client,
                project_id,
                task_run_id,
                phase="report_completeness",
                status="invalid",
                outcome="report_incomplete",
                payload=payload,
                error_message=completeness_error,
            )
            _finish(
                "failed",
                error_type="result_schema",
                error_message=completeness_error,
            )
            return "report_incomplete"
        output_arguments = {
            "data": data,
            "report_packet_json": report_packet_json,
            "report_generation_token": report_generation_token,
            "report_template_key": report_template_key,
            "report_template_version": report_template_version,
            "report_template_digest_sha256": report_template_digest_sha256,
            "report_policy_version": (
                report_execution_plan.get("report_policy_version")
                if report_execution_plan is not None
                else None
            ),
            "report_execution_plan_sha256": report_execution_plan_sha256,
            "report_prompt_digest_sha256": report_prompt_digest_sha256,
            "completeness_rule_version": (
                report_execution_plan.get("completeness_rule_version")
                if report_execution_plan is not None
                else 1
            ),
            "input_snapshot_artifact_id": input_snapshot_artifact_id,
            "input_snapshot_sha256": input_snapshot_sha256,
        }
        if isinstance(terminal_state.pending_entry, PendingReportExecution):
            prepared = _prepare_report_output(project_id, **output_arguments)
            if isinstance(prepared, str):
                _finish(
                    "failed",
                    error_type=_report_failure_error_type(prepared),
                    error_message="Markdown 报告渲染失败",
                )
                return prepared
            report_markdown, metadata = prepared
            execution_entry = terminal_state.pending_entry
            try:
                output_entry = pending_report_output(
                    execution_entry,
                    content=report_markdown,
                    metadata=metadata,
                )
                # validated output 先 durable，再允许第一次 Artifact POST。
                finish_outbox.replace(execution_entry, output_entry)
            except (OSError, ValueError, ReportFinishOutboxConflict):
                LOG.error(
                    "report output capture failed project=%s task_run=%s code=report_output_capture_failed",
                    project_id,
                    task_run_id,
                )
                terminal_state.delivery_pending = True
                return "server_write_failed"
            terminal_state.pending_entry = output_entry
            try:
                artifact_response = validate_report_artifact_receipt(
                    output_entry.replay(client),
                    output_entry,
                )
            except Exception:
                # validated output 已 durable，transport 异常只能留待 exact replay。
                LOG.exception(
                    "report output delivery failed project=%s task_run=%s code=report_output_delivery_failed",
                    project_id,
                    task_run_id,
                )
                terminal_state.delivery_pending = True
                return "server_write_failed"
            artifact_disposition = classify_write_response(artifact_response)
            if not artifact_disposition.accepted:
                LOG.warning(
                    "report output delivery pending project=%s task_run=%s status=%s code=%s disposition=%s",
                    project_id,
                    task_run_id,
                    artifact_response.status_code,
                    artifact_disposition.safe_code,
                    artifact_disposition.disposition,
                )
                # exact output 已持久化；任何 ACK 不确定或拒绝都只能重放，不能终结后重跑模型。
                terminal_state.delivery_pending = True
                return artifact_disposition.error_type or "business_acceptance"
            assert isinstance(artifact_response.data, dict)
            artifact_id = artifact_response.data["id"]
            assert isinstance(artifact_id, str)
            report_artifact: ReportArtifactIdentity | str = ReportArtifactIdentity(
                artifact_id,
                output_entry.artifact_sha256,
            )
        else:
            LOG.error(
                "report output journal missing project=%s task_run=%s code=report_output_capture_missing",
                project_id,
                task_run_id,
            )
            terminal_state.delivery_pending = True
            return "server_write_failed"
        if not isinstance(report_artifact, ReportArtifactIdentity):
            if report_artifact == "server_write_failed":
                terminal_state.delivery_pending = True
            else:
                _finish(
                    "failed",
                    error_type=_report_failure_error_type(report_artifact),
                    error_message="Markdown 报告 Artifact 写入失败",
                )
            return report_artifact
        terminal_state.artifact = report_artifact
        LOG.info(
            "report finished project=%s worker=%s execute_ms=%s total_ms=%s",
            project_id,
            worker.name,
            execute_ms,
            total_ms,
        )
        _finish("succeeded")
        return "success"
    except WorkerRequestError as exc:
        record_validation_result(
            client,
            project_id,
            task_run_id,
            phase="report_execute",
            status="invalid",
            outcome="request_contract",
            failure=exc.failure,
            error_message=exc.failure.message,
        )
        _finish("failed", failure=exc.failure)
        return exc.failure
    except DeliveryUnavailable:
        _finish("failed", error_type="delivery_unavailable", error_message="原报告交付上下文不可用")
        return "delivery_unavailable"
    except ContainerRuntimeMismatchError:
        LOG.warning("report container runtime mismatch; workspace preserved project=%s", project_id)
        _finish("failed", error_type="container_runtime_mismatch", error_message="运行环境与保留的项目容器不一致，工作区未删除；请恢复原容器配置")
        return "container_runtime_mismatch"
    except Exception:
        LOG.exception("report task crashed project=%s worker=%s", project_id, worker.name)
        _finish(
            "failed",
            error_type="container_failed",
            error_message="报告任务异常崩溃",
        )
        return "container_failed"
    finally:
        if result is not None:
            result.cleanup()
        # 只报告实际 exec 记录的终止证明；清理输出不代表删除原 Pi session。
        retry_graph_write(lambda: client.update_task_run(project_id, task_run_id, runtime_context={
            "execution_lifecycle": cancellation.execution_lifecycle(task_finished=True),
            "execution_container_ids": cancellation.execution_container_ids(),
        }))
        # claim 的 TaskRun 须等结果读取与文件回收结束，不能依赖外层 RunningTask 的空 ID。
        notify_browser_task(
            container_manager, project_id, task_run_id, local_finished=True,
        )
