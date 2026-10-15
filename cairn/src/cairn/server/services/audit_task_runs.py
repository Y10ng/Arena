"""审计 task run 服务。"""

from __future__ import annotations

from cairn.assessment_contracts import GOAL_DRIVEN_PROTOCOL

from .common import *
from .assessment_attempts import finalize_non_retryable_assessment_execution
from .assessment_policy import parse_scenario_snapshot, policy_from_project_row, require_execution_contract
from .assessment_convergence import (
    advance_assessment_phase,
    advance_goal_driven_convergence,
    stop_assessment,
)
from .audit_events import insert_agent_event
from .ids import get_project_or_404, next_task_run_id
from .injection_payloads import interrupt_pending_injection_payloads
from .projects import (
    check_project_hint_writable,
    get_intent_or_404,
    task_run_to_model,
    validate_task_run_exists,
    validate_execution_admission_generation,
)
from .runtime_context import (
    normalize_task_run_runtime_context_artifact_root,
    raw_task_run_runtime_context,
    runtime_context_audit_keys,
    sanitize_task_run_runtime_context,
    task_run_artifact_root_uri,
    task_run_runtime_context,
)
from .time_utils import timestamp_delta_seconds, utcnow
from .payload_values import dump_json

from cairn.server.db import get_conn
from cairn.server.models import CreateTaskRunRequest, TaskRun, TaskRunStatus, UpdateTaskRunRequest
from cairn.server.schemas.candidates import canonical_json_bytes

SERVER_RESULT_CONTEXT_FIELDS = {
    "accepted_result_digest", "accepted_fact_id",
    "accepted_completion_digest", "accepted_completion_intent_id",
    "accepted_intent_submissions", "execution_generation", "pause_requested", "pause_interrupted", "assessment_phase",
    "structured_intent_binding", "structured_explore_binding", "continuation",
    "completion_rejection", "reason_decision_boundary",
}


TERMINAL_TASK_STATUSES: set[TaskRunStatus] = {
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
}


def authenticated_execution_owner(principal, declared_owner=None) -> str:
    """执行身份只取认证主体，拒绝配置 ID 冒充或客户端悄悄换归属。"""
    from cairn.server.auth import RequestPrincipal

    if not isinstance(principal, RequestPrincipal) or principal.kind != "machine" or not principal.subject_id:
        raise HTTPException(403, "execution_owner_machine_required")
    if declared_owner is not None and declared_owner != principal.subject_id:
        raise HTTPException(403, "execution_owner_mismatch")
    return principal.subject_id


def insert_task_run_record(
    conn: sqlite3.Connection,
    project_id: str,
    body: CreateTaskRunRequest,
    *,
    assessment_attempt_id: str | None = None,
    server_runtime_context: dict[str, object] | None = None,
    principal,
) -> TaskRun:
    """在调用方事务内创建 TaskRun，供 claim/reservation 原子编排复用。"""
    owner = authenticated_execution_owner(principal, body.runtime_context.get("dispatcher_id"))
    body = body.model_copy(update={"runtime_context": {**body.runtime_context, "dispatcher_id": owner}})
    if (SERVER_RESULT_CONTEXT_FIELDS | {"execution_lifecycle", "delivery_retry", "execution_delivery", "external_verification_cleanup"}).intersection(body.runtime_context):
        raise HTTPException(422, "assessment_result_metadata_is_server_owned")
    project = get_project_or_404(conn, project_id)
    generation = int(project["execution_generation"])
    from .assessment_attempts import derive_delivery_retry_context

    delivery = derive_delivery_retry_context(conn, project, body, assessment_attempt_id=assessment_attempt_id)
    if "continuation" in delivery:
        source = validate_task_run_exists(conn, project_id, delivery["continuation"]["source_task_run_id"])
        body = body.model_copy(update={"session_id": source["session_id"]})
    elif "delivery_retry" in delivery:
        body = body.model_copy(update={"session_id": delivery["execution_delivery"]["session_id"]})
    if body.intent_id is not None:
        get_intent_or_404(conn, project_id, body.intent_id)

    server_context = dict(server_runtime_context or {})
    if not set(server_context) <= SERVER_RESULT_CONTEXT_FIELDS:
        raise HTTPException(500, "invalid_server_runtime_context")
    if body.task_type == "reason":
        from .structured_intent_bindings import issue_structured_intent_binding

        if "delivery_retry" in delivery:
            source_id = delivery["delivery_retry"]["source_task_run_id"]
            source = validate_task_run_exists(conn, project_id, source_id)
            source_context = raw_task_run_runtime_context(source)
            binding = source_context.get("structured_intent_binding")
        else:
            binding = issue_structured_intent_binding(conn, project)
        if binding is not None:
            server_context["structured_intent_binding"] = binding

    now = utcnow()
    task_run_id = next_task_run_id(conn, project_id)
    started_at = now if body.status != "queued" else None
    ended_at = now if body.status in TERMINAL_TASK_STATUSES else None
    queued_at = now if body.status == "queued" else None
    queue_wait_seconds = timestamp_delta_seconds(queued_at, started_at)
    # runtime_context 同时服务监控和审计导出，写入时补齐服务端可信字段。
    runtime_context = {
        **sanitize_task_run_runtime_context(body.runtime_context),
        **delivery,
        **server_context,
        "project_id": project_id,
        "task_run_id": task_run_id,
        "intent_id": body.intent_id,
        "task_type": body.task_type,
        "worker_name": body.worker_name,
        "worker_type": body.worker_type,
        "status": body.status,
        "session_id": body.session_id,
        "prompt_group": body.prompt_group,
        "prompt_version": body.prompt_version,
        "execution_profile": body.execution_profile,
        "queued_at": queued_at,
        "queue_wait_seconds": queue_wait_seconds,
        "started_at": started_at,
        "ended_at": ended_at,
        "exit_code": None,
        "error_type": None,
        "error_message": None,
        "assessment_attempt_id": assessment_attempt_id,
        "assessment_phase": parse_scenario_snapshot(project["assessment_progress_json"]).get("phase"),
        "execution_lifecycle": {"generation": generation, "process_started": False, "termination_confirmed": False, "cleanup_confirmed": False},
        "artifact_root": task_run_artifact_root_uri(project_id, task_run_id),
    }
    runtime_context = normalize_task_run_runtime_context_artifact_root(
        runtime_context, project_id, task_run_id
    )
    conn.execute(
        """
        INSERT INTO task_runs
            (id, project_id, intent_id, task_type, worker_name, worker_type, status,
             session_id, prompt_group, prompt_version, execution_profile,
             started_at, ended_at, exit_code, error_type, error_message,
             assessment_attempt_id, runtime_context_json, execution_generation)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?)
        """,
        (
            task_run_id,
            project_id,
            body.intent_id,
            body.task_type,
            body.worker_name,
            body.worker_type,
            body.status,
            body.session_id,
            body.prompt_group,
            body.prompt_version,
            body.execution_profile,
            started_at,
            ended_at,
            assessment_attempt_id,
            dump_json(runtime_context),
            generation,
        ),
    )
    row = validate_task_run_exists(conn, project_id, task_run_id)
    return task_run_to_model(row)


def create_task_run_record(project_id: str, body: CreateTaskRunRequest, *, principal) -> TaskRun:
    authenticated_execution_owner(principal, body.runtime_context.get("dispatcher_id"))
    with get_conn() as conn:
        from .projects import check_project_active

        conn.execute("UPDATE projects SET id = id WHERE id = ?", (project_id,))
        project = get_project_or_404(conn, project_id)
        if body.task_type == "external_verify" or conn.execute("SELECT 1 FROM verification_requests WHERE project_id = ?", (project_id,)).fetchone():
            raise HTTPException(409, "verification_claim_required")
        require_execution_contract(project)
        if body.worker_type != "pi":
            raise HTTPException(409, "execution_engine_unsupported")
        if body.task_type == "explore":
            raise HTTPException(409, "explore_attempt_admission_required")
        if body.task_type == "report":
            # 只有报告 claim 事务能创建 TaskRun，防止通用 admission 被收尾状态掩盖。
            raise HTTPException(409, "report_claim_required")
        if policy_from_project_row(project).protocol == GOAL_DRIVEN_PROTOCOL:
            stop_reason = advance_goal_driven_convergence(conn, project_id)
            if stop_reason is not None:
                conn.commit()
                raise HTTPException(409, "assessment_stopping")
        project = check_project_active(conn, project_id)
        from .project_authorization import require_project_authorization_current

        require_project_authorization_current(conn, project)
        validate_execution_admission_generation(project, body.expected_execution_generation)
        if body.expected_execution_generation is not None or project["execution_generation"] > 0:
            # 新协议须仍持有认领；同名旧请求另由 expected_generation 隔离。
            if body.task_type == "reason":
                claimed_worker = project["reason_worker"]
            elif body.intent_id is not None:
                claimed_worker = get_intent_or_404(conn, project_id, body.intent_id)["worker"]
            else:
                claimed_worker = None
            if claimed_worker != body.worker_name:
                raise HTTPException(409, "execution_lease_mismatch")
        from .assessment_attempts import require_execution_retry_eligible

        require_execution_retry_eligible(conn, project, body.task_type, intent_id=body.intent_id)
        if body.task_type == "reason" and conn.execute(
            "SELECT 1 FROM task_runs WHERE project_id = ? AND execution_generation = ? "
            "AND task_type = 'reason' AND status IN ('queued', 'running') LIMIT 1",
            (project_id, project["execution_generation"]),
        ).fetchone() is not None:
            raise HTTPException(409, "reason_execution_pending")
        return insert_task_run_record(conn, project_id, body, principal=principal)


def list_task_run_records(project_id: str) -> list[TaskRun]:
    from .assessment_attempts import delivery_retry_routes

    with get_conn() as conn:
        project = get_project_or_404(conn, project_id)
        rows = conn.execute(
            "SELECT * FROM task_runs WHERE project_id = ? "
            "ORDER BY COALESCE(started_at, ended_at), LENGTH(id), id",
            (project_id,),
        ).fetchall()
        routes = delivery_retry_routes(conn, project, executions=rows)
        return [task_run_to_model(row).model_copy(update={"delivery_retry_route": routes.get(row["id"])}) for row in rows]


def update_task_run_record(
    project_id: str, task_run_id: str, body: UpdateTaskRunRequest, *, principal,
) -> TaskRun:
    with get_conn() as conn:
        # 和 conclude/review/freeze 共用项目写锁，避免迟到审计更新覆盖已接受结果。
        conn.execute("UPDATE projects SET id = id WHERE id = ?", (project_id,))
        row = validate_task_run_exists(conn, project_id, task_run_id)
        updates = dict(body.runtime_context or {})
        if row["task_type"] == "external_verify" and {"execution_binding", "model", "worker_selection"}.intersection(updates):
            raise HTTPException(422, "verification_execution_binding_is_server_owned")
        if updates and SERVER_RESULT_CONTEXT_FIELDS.intersection(updates):
            raise HTTPException(422, "assessment_result_metadata_is_server_owned")
        runtime_context = raw_task_run_runtime_context(row)
        owner = runtime_context.get("dispatcher_id")
        if not owner or authenticated_execution_owner(principal) != owner:
            raise HTTPException(403, "execution_owner_mismatch")
        if "delivery_retry" in updates:
            raise HTTPException(422, "execution_delivery_is_server_owned")
        frozen_delivery = runtime_context.get("execution_delivery")
        if frozen_delivery is not None:
            if (
                "execution_delivery" in updates
                and canonical_json_bytes(updates["execution_delivery"])
                != canonical_json_bytes(frozen_delivery)
            ) or (
                body.session_id is not None
                and body.session_id != row["session_id"]
            ):
                raise HTTPException(422, "execution_delivery_is_server_owned")
            # 已冻结快照只允许 exact ACK，不再交给后续 merge 重写权威值。
            updates.pop("execution_delivery", None)
        elif "execution_delivery" in updates:
            from .assessment_attempts import validate_delivery_registration

            validate_delivery_registration(conn, get_project_or_404(conn, project_id), row, body, updates["execution_delivery"])
        if "dispatcher_id" in updates and updates["dispatcher_id"] != owner:
            raise HTTPException(403, "execution_owner_immutable")
        if "external_verification_cleanup" in updates:
            if row["task_type"] != "external_verify":
                raise HTTPException(422, "verification_cleanup_execution_required")
            from cairn.server.schemas.verification_requests import VerificationCleanup
            from pydantic import ValidationError
            try:
                cleanup = VerificationCleanup.model_validate(updates["external_verification_cleanup"]).model_dump(mode="json")
            except ValidationError:
                raise HTTPException(422, "verification_cleanup_invalid") from None
            prior_cleanup = runtime_context.get("external_verification_cleanup", {})
            if prior_cleanup.get("status") == "removed" and cleanup["status"] != "removed":
                raise HTTPException(409, "verification_cleanup_confirmation_conflict")
            if cleanup.get("error"):
                from .verification_requests import _validate_public_text
                _validate_public_text(cleanup["error"])
            updates["external_verification_cleanup"] = cleanup
        lifecycle = updates.get("execution_lifecycle")
        if lifecycle is not None:
            if principal is None or principal.kind != "machine":
                raise HTTPException(403, "execution_lifecycle_machine_required")
            if not owner or authenticated_execution_owner(principal) != owner:
                raise HTTPException(403, "execution_owner_mismatch")
            if lifecycle["generation"] != row["execution_generation"]:
                raise HTTPException(409, "execution_generation_mismatch")
            prior = runtime_context.get("execution_lifecycle", {})
            for key in ("process_started", "termination_confirmed", "cleanup_confirmed"):
                if prior.get(key) is True and lifecycle[key] is not True:
                    raise HTTPException(409, "execution_lifecycle_confirmation_conflict")
            if lifecycle["cleanup_confirmed"] and not lifecycle["termination_confirmed"]:
                raise HTTPException(422, "execution_cleanup_requires_termination")
        if updates.get("external_verification_cleanup", {}).get("status") == "removed":
            proof = lifecycle or runtime_context.get("execution_lifecycle", {})
            if proof.get("termination_confirmed") is not True or proof.get("cleanup_confirmed") is not True:
                raise HTTPException(422, "verification_cleanup_requires_confirmation")
        if row["task_type"] == "report" and (
            body.status in TERMINAL_TASK_STATUSES
            or body.exit_code is not None
            or body.error_type is not None
            or body.error_message is not None
        ):
            raise HTTPException(
                422,
                "assessment_report_terminal_fields_are_receipt_owned",
            )
        if (
            row["status"] in TERMINAL_TASK_STATUSES
            and body.status is not None
            and body.status != row["status"]
        ):
            if body.status not in TERMINAL_TASK_STATUSES or not lifecycle or not lifecycle["termination_confirmed"]:
                raise HTTPException(409, "Terminal task run status cannot change")
            # 心跳超时可先落终态；真实进程退出证明仍须接收，但绝不重写既有终态。
            body = UpdateTaskRunRequest(runtime_context=body.runtime_context)
        if row["pause_requested"] and body.status in {"queued", "running"}:
            raise HTTPException(409, "project_pause_draining")
        if row["task_type"] == "report" and updates:
            # claim 绑定由 Server 创建，客户端只能补充非权威审计字段。
            for key in ("report_claim", "report_generation"):
                if key in updates and updates[key] != runtime_context.get(key):
                    raise HTTPException(422, "report_claim_metadata_is_server_owned")
        if updates:
            runtime_context.update(sanitize_task_run_runtime_context(updates))
        values = {
            "status": body.status if body.status is not None else row["status"],
            "session_id": body.session_id if body.session_id is not None else row["session_id"],
            "exit_code": body.exit_code if body.exit_code is not None else row["exit_code"],
            "error_type": body.error_type if body.error_type is not None else row["error_type"],
            "error_message": body.error_message if body.error_message is not None else row["error_message"],
        }
        started_at = row["started_at"]
        if values["status"] != "queued" and started_at is None:
            # queued -> running/terminal 的首次更新负责补 started_at。
            started_at = utcnow()
        queued_at = runtime_context.get("queued_at")
        queue_wait_seconds = timestamp_delta_seconds(queued_at, started_at)
        ended_at = row["ended_at"]
        if values["status"] in TERMINAL_TASK_STATUSES and ended_at is None:
            ended_at = utcnow()
        runtime_context.update(
            {
                "project_id": project_id,
                "task_run_id": task_run_id,
                "intent_id": row["intent_id"],
                "task_type": row["task_type"],
                "worker_name": row["worker_name"],
                "worker_type": row["worker_type"],
                "status": values["status"],
                "session_id": values["session_id"],
                "prompt_group": row["prompt_group"],
                "prompt_version": row["prompt_version"],
                "execution_profile": row["execution_profile"],
                "queued_at": queued_at,
                "queue_wait_seconds": queue_wait_seconds,
                "started_at": started_at,
                "ended_at": ended_at,
                "exit_code": values["exit_code"],
                "error_type": values["error_type"],
                "error_message": values["error_message"],
                "assessment_attempt_id": (
                    row["assessment_attempt_id"]
                    if "assessment_attempt_id" in row.keys()
                    else None
                ),
                "artifact_root": task_run_artifact_root_uri(project_id, task_run_id),
            }
        )
        runtime_context = normalize_task_run_runtime_context_artifact_root(runtime_context, project_id, task_run_id)

        updated_cursor = conn.execute(
            """
            UPDATE task_runs
            SET status = ?,
                session_id = ?,
                started_at = ?,
                ended_at = ?,
                exit_code = ?,
                error_type = ?,
                error_message = ?,
                runtime_context_json = ?
            WHERE id = ? AND project_id = ? AND status = ?
            """,
            (
                values["status"],
                values["session_id"],
                started_at,
                ended_at,
                values["exit_code"],
                values["error_type"],
                values["error_message"],
                dump_json(runtime_context),
                task_run_id,
                project_id,
                row["status"],
            ),
        )
        if updated_cursor.rowcount == 0:
            raise HTTPException(409, "Task run changed concurrently")
        if (
            row["status"] not in TERMINAL_TASK_STATUSES
            and values["status"] in TERMINAL_TASK_STATUSES
        ):
            interrupt_pending_injection_payloads(conn, project_id, task_run_id)
            if row["task_type"] != "external_verify":
                finalize_non_retryable_assessment_execution(
                    conn,
                    project_id,
                    (
                        row["assessment_attempt_id"]
                        if "assessment_attempt_id" in row.keys()
                        else None
                    ),
                    task_run_id=task_run_id,
                    status=values["status"],
                    error_type=values["error_type"],
                )
            project = get_project_or_404(conn, project_id)
            policy = policy_from_project_row(project)
            if row["task_type"] not in {"report", "external_verify"} and policy.phased:
                if project["status"] == "stopped":
                    stop_assessment(conn, project_id, project["coverage_stop_reason"] or "user_stopped")
                elif project["status"] == "active" and not row["pause_requested"]:
                    advance_assessment_phase(conn, project_id)
        project = get_project_or_404(conn, project_id)
        if (
            row["task_type"] not in {"report", "external_verify"}
            and policy_from_project_row(project).protocol == GOAL_DRIVEN_PROTOCOL
            and (
                lifecycle is not None
                or (
                    row["status"] not in TERMINAL_TASK_STATUSES
                    and values["status"] in TERMINAL_TASK_STATUSES
                )
            )
        ):
            # 终态与后续 cleanup ACK 都可能解除最后一个停止阻塞条件。
            advance_goal_driven_convergence(conn, project_id)
            project = get_project_or_404(conn, project_id)
        if row["pause_requested"] and project["status"] == "paused" and values["status"] in TERMINAL_TASK_STATUSES:
            confirmation = runtime_context.get("execution_lifecycle", {})
            if confirmation.get("termination_confirmed") is True and confirmation.get("cleanup_confirmed") is True:
                # 仅旧代次确认退出后清除其 claim；恢复后迟到更新不能释放新租约。
                from .projects import clear_open_intent_worker, clear_project_reason
                if row["intent_id"]:
                    intent = get_intent_or_404(conn, project_id, row["intent_id"])
                    if intent["worker"] == row["worker_name"]:
                        clear_open_intent_worker(conn, project_id, row["intent_id"], "released")
                if row["task_type"] == "reason" and project["reason_worker"] == row["worker_name"]:
                    clear_project_reason(conn, project_id)
        updated = validate_task_run_exists(conn, project_id, task_run_id)
        if row["task_type"] == "external_verify":
            from .verification_requests import finalize_verification_run
            finalize_verification_run(conn, project_id, updated)
        if body.runtime_context:
            # runtime_context 的增量变更单独落 agent event，方便监控解释配置/环境变化。
            insert_agent_event(
                conn,
                project_id,
                task_run_id,
                "runtime_context_updated",
                {
                    "updated_keys": runtime_context_audit_keys(body.runtime_context),
                    "runtime_context": task_run_runtime_context(updated),
                },
                task_row=updated,
            )
        return task_run_to_model(updated)
