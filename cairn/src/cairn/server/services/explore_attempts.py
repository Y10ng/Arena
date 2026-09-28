"""Explore attempt 的原子认领、预算预约与 TaskRun 创建。"""

from __future__ import annotations

import sqlite3

from fastapi import HTTPException

from cairn.assessment_contracts import GOAL_DRIVEN_PROTOCOL
from cairn.server.models import CreateTaskRunRequest, TaskRun

from .assessment_attempts import (
    DELIVERY_RETRY_ERROR_TYPES,
    EXTENDED_MAX_RUNTIME_RETRIES,
    attempt_has_accepted_result,
    execution_retry_eligibility,
)
from .assessment_policy import policy_from_project_row, require_execution_contract
from .audit_task_runs import authenticated_execution_owner, insert_task_run_record
from .ids import next_assessment_attempt_id
from .projects import check_project_active, get_claimable_open_intent_or_404, lease_clock, validate_execution_admission_generation
from .runtime_context import raw_task_run_runtime_context
from .structured_intent_bindings import (
    admit_structured_explore_binding,
    parse_structured_explore_binding,
)
from .time_utils import utcnow


def start_explore_attempt(
    conn: sqlite3.Connection,
    project_id: str,
    intent_id: str,
    body: CreateTaskRunRequest,
    *,
    principal,
) -> TaskRun:
    """同一事务完成 claim、attempt 计数与 TaskRun 插入。"""
    authenticated_execution_owner(principal, body.runtime_context.get("dispatcher_id"))
    project = check_project_active(conn, project_id)
    validate_execution_admission_generation(project, body.expected_execution_generation)
    require_execution_contract(project)
    if body.worker_type != "pi":
        raise HTTPException(409, "execution_engine_unsupported")
    policy = policy_from_project_row(project)
    goal_driven = policy.protocol == GOAL_DRIVEN_PROTOCOL
    if goal_driven and project["coverage_stop_reason"] == "user_stopped":
        from .assessment_convergence import advance_goal_driven_convergence

        advance_goal_driven_convergence(
            conn,
            project_id,
            reason="user_stopped",
        )
        conn.commit()
        raise HTTPException(409, "assessment_stopping")
    if (
        policy.phased
        and policy.explore_deadline
        and policy.explore_deadline <= utcnow()
    ):
        from .assessment_convergence import stop_assessment

        stop_assessment(conn, project_id, "deadline_exceeded")
        conn.commit()
        raise HTTPException(409, "explore_deadline_exceeded")
    from .project_authorization import require_project_authorization_current

    require_project_authorization_current(conn, project)
    if body.intent_id != intent_id:
        raise HTTPException(422, "task run intent_id must match the path intent")
    if body.task_type != "explore":
        raise HTTPException(422, "task_type must be explore")
    if body.status != "running":
        raise HTTPException(422, "Explore attempt must start in running status")

    intent = get_claimable_open_intent_or_404(
        conn, project_id, intent_id, body.worker_name
    )
    if intent["worker"] is not None:
        raise HTTPException(409, "Explore attempt has already started")

    policy = policy_from_project_row(project)
    goal_driven = policy.protocol == GOAL_DRIVEN_PROTOCOL
    if (
        intent["assessment_role"] == "verifier"
        and not policy.independent_verifier_enabled
    ):
        raise HTTPException(409, "independent_verifier_not_enabled")
    # 旧协议仍由 Cell 决定准入；新协议直接对 Intent 预约同一套逻辑 Attempt。
    structured_explore_binding = (
        admit_structured_explore_binding(conn, project, intent)
        if policy.structured
        else None
    )
    runtime_context = dict(body.runtime_context)
    assessment_attempt_id: str | None = None
    if policy.structured or goal_driven:
        now = utcnow()
        reconciled_attempt = False
        active_attempt = conn.execute(
            """
            SELECT * FROM assessment_attempts
            WHERE project_id = ? AND intent_id = ? AND status = 'active'
            ORDER BY started_at DESC, id DESC
            LIMIT 1
            """,
            (project_id, intent_id),
        ).fetchone()
        if active_attempt is not None:
            if attempt_has_accepted_result(conn, project_id, active_attempt["id"]):
                raise HTTPException(409, "assessment_result_already_accepted")
            last_execution = conn.execute(
                """
                SELECT * FROM task_runs
                WHERE project_id = ? AND assessment_attempt_id = ?
                ORDER BY COALESCE(started_at, ended_at) DESC, LENGTH(id) DESC, id DESC
                LIMIT 1
                """,
                (project_id, active_attempt["id"]),
            ).fetchone()
            if last_execution is None:
                raise HTTPException(409, "assessment_attempt_has_no_execution")
            last_status = last_execution["status"]
            last_error = last_execution["error_type"]
            if last_status in ("queued", "running"):
                # 租约释放不证明旧进程已停止；先 reconciliation，不能并行启动免费 retry。
                raise HTTPException(409, "assessment_execution_not_quiescent")
            paused_execution = bool(last_execution["pause_interrupted"]) or (
                last_execution["execution_generation"] != project["execution_generation"]
            )
            if last_error in DELIVERY_RETRY_ERROR_TYPES and not paused_execution:
                # 交付重试必须沿用原 admission 快照，不能重新扩大 Cell 权限。
                source_context = raw_task_run_runtime_context(last_execution)
                if structured_explore_binding is not None:
                    source_binding = parse_structured_explore_binding(
                        source_context.get("structured_explore_binding")
                    )
                    if (
                        source_binding.intent_id != intent_id
                        or source_binding.strategy_key != str(intent["strategy_key"] or "")
                    ):
                        raise HTTPException(409, "structured_intent_binding_stale")
                    structured_explore_binding = source_binding.model_dump(
                        mode="json", exclude_none=True
                    )
            retryable = paused_execution
            retry_reason = None
            if not paused_execution and last_status in ("failed", "timed_out"):
                runs = conn.execute(
                    "SELECT * FROM task_runs WHERE project_id = ? AND assessment_attempt_id = ? "
                    "AND execution_generation = ? AND pause_interrupted = 0 "
                    "ORDER BY COALESCE(started_at, ended_at), LENGTH(id), id",
                    (project_id, active_attempt["id"], project["execution_generation"]),
                ).fetchall()
                eligibility = execution_retry_eligibility(
                    conn, project, "explore", intent_id=intent_id, executions=runs,
                    max_runtime_retries=int(active_attempt["max_runtime_retries"]), now=now,
                )
                retryable = eligibility["allowed"]
                retry_reason = eligibility["reason"]
                if retry_reason in {"execution_retry_cooldown", "execution_delivery_unavailable"}:
                    raise HTTPException(409, retry_reason)
            deadline_exceeded = (
                policy.explore_deadline is not None
                and policy.explore_deadline <= now
            )
            if deadline_exceeded and retryable:
                conn.execute(
                    """
                    UPDATE assessment_attempts
                    SET status = 'failed', ended_at = ?, outcome = 'explore_deadline_exceeded'
                    WHERE id = ? AND project_id = ? AND status = 'active'
                    """,
                    (now, active_attempt["id"], project_id),
                )
                if goal_driven:
                    from .assessment_convergence import advance_goal_driven_convergence

                    advance_goal_driven_convergence(
                        conn,
                        project_id,
                        reason="deadline_exceeded",
                    )
                # admission 必须返回 409，但这项收敛不能被请求级异常回滚。
                conn.commit()
                raise HTTPException(409, "explore_deadline_exceeded")
            if not retryable:
                outcome = (
                    f"runtime_retry_exhausted:{last_error}"
                    if retry_reason == "execution_retry_exhausted"
                    else (last_error or last_status)
                )
                closed = conn.execute(
                    """
                    UPDATE assessment_attempts
                    SET status = 'failed', ended_at = ?, outcome = ?
                    WHERE id = ? AND project_id = ? AND status = 'active'
                    """,
                    (now, outcome, active_attempt["id"], project_id),
                )
                reconciled_attempt = reconciled_attempt or closed.rowcount > 0
                active_attempt = None
        if policy.explore_deadline and policy.explore_deadline <= now:
            if goal_driven:
                from .assessment_convergence import advance_goal_driven_convergence

                advance_goal_driven_convergence(
                    conn,
                    project_id,
                    reason="deadline_exceeded",
                )
            if reconciled_attempt or goal_driven:
                # 请求会返回 409；先保留旧 execution/attempt 与停止原因的收敛结果。
                conn.commit()
            raise HTTPException(409, "explore_deadline_exceeded")

        if active_attempt is None:
            prior = conn.execute(
                "SELECT outcome FROM assessment_attempts WHERE project_id = ? AND intent_id = ? AND status != 'active' LIMIT 1",
                (project_id, intent_id),
            ).fetchone()
            if prior is not None:
                conn.execute(
                    "UPDATE intents SET concluded_at = ?, conclusion = 'failed', conclusion_reason = ? WHERE project_id = ? AND id = ? AND concluded_at IS NULL AND worker IS NULL",
                    (now, prior["outcome"] or "attempt_already_closed", project_id, intent_id),
                )
                conn.commit()
                raise HTTPException(409, "assessment_attempt_already_closed")
            verifier_charge = int(intent["assessment_role"] == "verifier")
            reserve = policy.verification_reserve
            reserved = conn.execute(
                """
                UPDATE projects
                SET explore_attempts_started = explore_attempts_started + 1,
                    verification_attempts_started = verification_attempts_started + ?
                WHERE id = ? AND status = 'active'
                  AND (explore_budget IS NULL OR explore_attempts_started < explore_budget -
                       CASE WHEN ? = 1 THEN 0
                            WHEN ? > verification_attempts_started THEN ? - verification_attempts_started
                            ELSE 0 END)
                  AND (explore_deadline IS NULL OR explore_deadline > ?)
                RETURNING explore_attempts_started
                """,
                (verifier_charge, project_id, verifier_charge, reserve, reserve, now),
            ).fetchone()
            if reserved is None:
                if reconciled_attempt:
                    # 新 attempt 被限制拒绝时，也必须持久化旧 attempt 的收敛。
                    conn.commit()
                current = conn.execute(
                    """
                    SELECT explore_attempts_started,
                           explore_budget,
                           explore_deadline
                    FROM projects
                    WHERE id = ?
                    """,
                    (project_id,),
                ).fetchone()
                if (
                    current is not None
                    and current["explore_deadline"]
                    and current["explore_deadline"] <= now
                ):
                    if goal_driven:
                        from .assessment_convergence import advance_goal_driven_convergence

                        advance_goal_driven_convergence(
                            conn,
                            project_id,
                            reason="deadline_exceeded",
                        )
                        conn.commit()
                    raise HTTPException(409, "explore_deadline_exceeded")
                if current is not None and current["explore_budget"] is not None and current["explore_attempts_started"] < current["explore_budget"]:
                    raise HTTPException(409, "discovery_budget_exhausted")
                if goal_driven:
                    from .assessment_convergence import advance_goal_driven_convergence

                    advance_goal_driven_convergence(
                        conn,
                        project_id,
                        reason="budget_exhausted",
                    )
                    conn.commit()
                raise HTTPException(409, "explore_budget_exhausted")
            assessment_attempt_id = next_assessment_attempt_id(conn, project_id)
            conn.execute(
                """
                INSERT INTO assessment_attempts
                    (id, project_id, intent_id, slot_key, assessment_role,
                     status, charged, execution_count, max_runtime_retries,
                     strategy_key, started_at)
                VALUES (?, ?, ?, ?, ?, 'active', 1, 0, ?, ?, ?)
                """,
                (
                    assessment_attempt_id,
                    project_id,
                    intent_id,
                    intent["slot_key"] if "slot_key" in intent.keys() else None,
                    (
                        str(intent["assessment_role"] or "discovery")
                        if "assessment_role" in intent.keys()
                        else "discovery"
                    ),
                    EXTENDED_MAX_RUNTIME_RETRIES,
                    (
                        (intent["strategy_key"] or "")
                        if "strategy_key" in intent.keys()
                        else ""
                    ),
                    now,
                ),
            )
            runtime_context["explore_attempt_number"] = int(
                reserved["explore_attempts_started"]
            )
        else:
            assessment_attempt_id = active_attempt["id"]

        execution = conn.execute(
            """
            UPDATE assessment_attempts
            SET execution_count = execution_count + 1
            WHERE id = ? AND project_id = ? AND status = 'active'
            RETURNING execution_count
            """,
            (assessment_attempt_id, project_id),
        ).fetchone()
        if execution is None:
            raise HTTPException(409, "assessment_attempt_changed_concurrently")
        runtime_context["assessment_execution_number"] = int(
            execution["execution_count"]
        )

    boot_id, now_ns = lease_clock()
    claimed = conn.execute(
        """
        UPDATE intents
        SET worker = ?, last_heartbeat_at = ?, heartbeat_boot_id = ?, heartbeat_boottime_ns = ?
        WHERE id = ? AND project_id = ?
          AND to_fact_id IS NULL AND concluded_at IS NULL AND worker IS NULL
        RETURNING *
        """,
        (body.worker_name, utcnow(), boot_id, now_ns, intent_id, project_id),
    ).fetchone()
    if claimed is None:
        # 抛异常会让同事务中的 attempt reservation 一并回滚。
        raise HTTPException(409, "Intent claim changed concurrently")

    task_body = body.model_copy(update={"runtime_context": runtime_context})
    return insert_task_run_record(
        conn,
        project_id,
        task_body,
        assessment_attempt_id=assessment_attempt_id,
        server_runtime_context=(
            {"structured_explore_binding": structured_explore_binding}
            if structured_explore_binding is not None
            else None
        ),
        principal=principal,
    )
