"""项目完成与重新打开流程。"""

from __future__ import annotations

import json
from hashlib import sha256
from contextlib import nullcontext

from cairn.assessment_contracts import (
    GOAL_DRIVEN_PROTOCOL,
    goal_driven_decision_input_digest,
)
from cairn.server.db import get_conn
from cairn.server.models import (
    CompleteRequest,
    Fact,
    Hint,
    Intent,
    ProjectDetail,
    ReopenRequest,
    ReopenResponse,
)

from .assessment_convergence import prepare_structured_completion, require_assessment_completion
from .assessment_policy import (
    policy_from_project_row,
    require_execution_contract,
    require_result_settlement_contract,
    row_value,
)
from .common import *
from .coverage_slots import count_open_coverage_slots, mark_slots_skipped
from .ids import get_project_or_404, next_fact_id, next_intent_id
from .projects import (
    bind_fact_evidence,
    build_intents,
    check_project_active,
    check_project_completed,
    close_open_intents_for_project_completion,
    clear_project_reason,
    expire_reason_leases,
    get_completion_intent_or_409,
    intent_to_model,
    project_meta_from_row,
    validate_facts_exist,
    validate_goal_not_in_sources,
    validate_task_run_exists,
    validate_execution_generation,
)
from .targets import build_targets
from .time_utils import utcnow


def _goal_driven_completion_detail(conn, project_row) -> ProjectDetail:
    """在调用方事务内重建与 Reason 相同的业务输入，不触发读路径租约清理。"""
    project_id = project_row["id"]
    facts = conn.execute(
        """
        SELECT id, description, task_run_id, creator, created_at
        FROM facts
        WHERE project_id = ?
        """,
        (project_id,),
    ).fetchall()
    hints = conn.execute(
        """
        SELECT id, content, creator, fact_id, intent_id, created_at
        FROM hints
        WHERE project_id = ?
        """,
        (project_id,),
    ).fetchall()
    return ProjectDetail(
        project=project_meta_from_row(conn, project_row),
        facts=[Fact(**dict(row)) for row in facts],
        intents=build_intents(conn, project_id),
        hints=[Hint(**dict(row)) for row in hints],
        targets=build_targets(conn, project_id),
    )


class _GoalDrivenCompletionRejection(HTTPException):
    """受控动态拒绝须由事务所有者保存，不提交调用方的其他业务写入。"""


def _reject_goal_driven_completion(conn, run, run_context, code, decision_digest):
    """动态门禁拒绝不是业务接受；在当前事务内记录原冻结输入的拒绝回执。"""
    run_context["completion_rejection"] = {
        "code": code,
        "decision_input_digest": decision_digest,
    }
    conn.execute(
        "UPDATE task_runs SET runtime_context_json = ? WHERE project_id = ? AND id = ?",
        (json.dumps(run_context, sort_keys=True), run["project_id"], run["id"]),
    )
    raise _GoalDrivenCompletionRejection(409, code)


def _require_goal_driven_quiescence(
    conn,
    project_row,
    run,
    run_context: dict,
    *,
    worker: str,
) -> None:
    """只接受最新 Reason 建议；Server 在同一事务内复核真实静止状态。"""
    project_id = project_row["id"]
    if (
        run is None
        or run["task_type"] != "reason"
        or run["worker_name"] != worker
        or run["worker_type"] != "pi"
        or run["status"] != "running"
        or project_row["reason_worker"] != worker
    ):
        raise HTTPException(409, "goal_driven_completion_requires_reason_run")

    delivery = run_context.get("execution_delivery")
    if not isinstance(delivery, dict) or delivery.get("phase") != "reason_execute":
        raise HTTPException(409, "goal_driven_completion_input_missing")
    binding = delivery.get("binding")
    decision_digest = (
        binding.get("decision_input_digest")
        if isinstance(binding, dict)
        else None
    )
    if not isinstance(decision_digest, str):
        raise HTTPException(409, "goal_driven_completion_input_missing")
    current_digest = goal_driven_decision_input_digest(
        _goal_driven_completion_detail(conn, project_row)
    )
    if decision_digest != current_digest:
        _reject_goal_driven_completion(
            conn, run, run_context, "goal_driven_completion_input_stale", decision_digest,
        )

    if conn.execute(
        """
        SELECT 1
        FROM intents
        WHERE project_id = ? AND to_fact_id IS NULL AND concluded_at IS NULL
        LIMIT 1
        """,
        (project_id,),
    ).fetchone() is not None:
        _reject_goal_driven_completion(
            conn, run, run_context, "goal_driven_completion_open_intents", decision_digest,
        )

    if conn.execute(
        """
        SELECT 1
        FROM task_runs
        WHERE project_id = ? AND id <> ? AND task_type <> 'report'
          AND status IN ('queued', 'running')
        LIMIT 1
        """,
        (project_id, run["id"]),
    ).fetchone() is not None:
        raise HTTPException(409, "goal_driven_completion_execution_pending")

    if conn.execute(
        """
        SELECT 1
        FROM assessment_attempts
        WHERE project_id = ? AND status = 'active'
        LIMIT 1
        """,
        (project_id,),
    ).fetchone() is not None:
        raise HTTPException(409, "goal_driven_completion_attempt_active")

    if conn.execute(
        """
        SELECT 1
        FROM task_run_candidates AS candidate
        WHERE candidate.project_id = ?
          AND NOT EXISTS (
              SELECT 1
              FROM task_run_candidate_outcomes AS outcome
              WHERE outcome.candidate_id = candidate.id
                AND outcome.project_id = candidate.project_id
          )
        LIMIT 1
        """,
        (project_id,),
    ).fetchone() is not None:
        raise HTTPException(409, "goal_driven_completion_candidate_pending")


def complete_project_record(project_id: str, body: CompleteRequest) -> Intent:
    return _complete_project_record(project_id, body)


def complete_project_record_with_conn(
    conn,
    project_id: str,
    body: CompleteRequest,
) -> Intent:
    """复用调用方事务执行完整的项目完成校验与写入。"""
    return _complete_project_record(project_id, body, connection=conn)


def _complete_project_record(
    project_id: str,
    body: CompleteRequest,
    *,
    connection=None,
) -> Intent:
    context = get_conn() if connection is None else nullcontext(connection)
    with context as conn:
        conn.execute("UPDATE projects SET id = id WHERE id = ?", (project_id,))
        project_row = get_project_or_404(conn, project_id)
        require_result_settlement_contract(
            project_row,
            task_run_id=body.task_run_id,
        )
        submission_digest = sha256(json.dumps(
            body.model_dump(by_alias=True, exclude_none=True),
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()).hexdigest()
        run = None
        run_context = {}
        if body.task_run_id is not None:
            run = validate_task_run_exists(conn, project_id, body.task_run_id)
            run_context = json.loads(run["runtime_context_json"] or "{}")
            accepted_id = run_context.get("accepted_completion_intent_id")
            if accepted_id:
                if run["worker_name"] != body.worker or run_context.get("accepted_completion_digest") != submission_digest:
                    raise HTTPException(409, "assessment_completion_conflict")
                accepted = conn.execute(
                    "SELECT * FROM intents WHERE project_id = ? AND id = ? AND to_fact_id = 'goal'",
                    (project_id, accepted_id),
                ).fetchone()
                if accepted is None:
                    raise HTTPException(409, "assessment_completion_conflict")
                return intent_to_model(conn, accepted, project_id)
        check_project_active(conn, project_id)
        validate_execution_generation(conn, project_id, body.task_run_id)
        expire_reason_leases(conn, project_id)
        project_row = get_project_or_404(conn, project_id)
        policy = policy_from_project_row(project_row)
        goal_driven = (
            getattr(policy, "goal_driven", False) is True
            or policy.protocol == GOAL_DRIVEN_PROTOCOL
            or row_value(project_row, "assessment_protocol")
            == GOAL_DRIVEN_PROTOCOL
        )
        structured_checkpoint = None
        if run is not None and (
            run["worker_name"] != body.worker or run["status"] != "running"
            or run["worker_type"] != "pi" or run["task_type"] not in {"reason", "bootstrap"}
        ):
            raise HTTPException(409, "assessment_completion_run_mismatch")
        if goal_driven:
            limit_reason = row_value(project_row, "coverage_stop_reason")
            if (
                limit_reason in {"budget_exhausted", "deadline_exceeded", "user_stopped"}
                or (
                    policy.explore_deadline
                    and utcnow() >= policy.explore_deadline
                )
                or (
                    policy.explore_budget is not None
                    and int(row_value(project_row, "explore_attempts_started") or 0)
                    >= policy.explore_budget
                )
            ):
                raise HTTPException(409, "goal_driven_completion_stopping")
            try:
                _require_goal_driven_quiescence(
                    conn,
                    project_row,
                    run,
                    run_context,
                    worker=body.worker,
                )
            except _GoalDrivenCompletionRejection:
                if connection is None:
                    # HTTP complete 自有事务尚无完成写入；拒绝回执须在 409 回滚前提交。
                    conn.commit()
                raise
        if policy.phased:
            if run is not None and run["task_type"] != "reason":
                raise HTTPException(409, "assessment_completion_run_mismatch")
            require_assessment_completion(conn, project_id, task_run_id=body.task_run_id)
        if policy.structured:
            # v1 完成语义来自项目冻结协议，live Scenario 后续修改不得改变运行中项目。
            scenario_features = {
                "complete_gate": True,
                "coverage_gate": True,
                "verifier": False,
            }
            structured_checkpoint = prepare_structured_completion(conn, project_id)
        elif goal_driven:
            # 新协议只认上面的统一静止检查，不回落到 live Scenario 的 Coverage gate。
            scenario_features = {
                "complete_gate": False,
                "coverage_gate": False,
            }
        else:
            # Legacy 项目继续读取 live Scenario，保持既有行为。
            scenario_row = conn.execute(
                """
                SELECT s.features_json
                FROM projects p
                JOIN scenarios s ON p.scenario_key = s.key
                WHERE p.id = ?
                """,
                (project_id,),
            ).fetchone()
            scenario_features = (
                json.loads(scenario_row["features_json"])
                if scenario_row and scenario_row["features_json"]
                else {}
            )
        if scenario_features.get("complete_gate"):
            open_count = conn.execute(
                "SELECT COUNT(*) AS c FROM intents WHERE project_id = ? AND to_fact_id IS NULL AND concluded_at IS NULL",
                (project_id,),
            ).fetchone()["c"]
            if open_count:
                raise HTTPException(
                    409,
                    "Resolve open intents before complete: abandon unclaimed ones and wait for running explores to finish",
                )
        if policy.structured:
            if not policy.phased and (structured_checkpoint is None or not structured_checkpoint.complete):
                unresolved = (
                    0
                    if structured_checkpoint is None
                    else structured_checkpoint.unresolved_count
                )
                raise HTTPException(
                    409,
                    "Structured coverage has "
                    f"{unresolved} unresolved Cell(s)",
                )
        elif scenario_features.get("coverage_gate"):
            # Legacy Pentest 保留 A+B+D 与 verifier 语义，不与 v1 checkpoint 混用。
            require_verified = bool(scenario_features.get("verifier"))
            proj_row = conn.execute(
                """
                SELECT explore_budget, explore_deadline, reason_idle_streak
                FROM projects
                WHERE id = ?
                """,
                (project_id,),
            ).fetchone()
            budget = (
                proj_row["explore_budget"]
                if proj_row and "explore_budget" in proj_row.keys()
                else None
            )
            deadline = (
                proj_row["explore_deadline"]
                if proj_row and "explore_deadline" in proj_row.keys()
                else None
            )
            if budget is not None or deadline:
                succeeded = conn.execute(
                    """
                    SELECT COUNT(*) AS c
                    FROM task_runs
                    WHERE project_id = ? AND task_type = 'explore'
                      AND status = 'succeeded'
                    """,
                    (project_id,),
                ).fetchone()["c"]
                now = utcnow()
                if (budget is not None and succeeded >= budget) or (
                    deadline and now >= deadline
                ):
                    mark_slots_skipped(
                        conn,
                        project_id,
                        tier=None,
                        reason="budget coverage tail",
                    )

            core_open = count_open_coverage_slots(
                conn,
                project_id,
                require_verified=require_verified,
                tier="core",
            )
            if core_open:
                raise HTTPException(
                    409,
                    "Resolve core coverage slots before complete: "
                    f"{core_open} unresolved slot(s)",
                )

            extended_open = count_open_coverage_slots(
                conn,
                project_id,
                require_verified=require_verified,
                tier="extended",
            )
            if extended_open:
                idle_streak = (
                    proj_row["reason_idle_streak"]
                    if proj_row and "reason_idle_streak" in proj_row.keys()
                    else 0
                )
                k_row = conn.execute(
                    "SELECT coverage_idle_rounds FROM settings WHERE id = 1"
                ).fetchone()
                k_val = (
                    k_row["coverage_idle_rounds"]
                    if k_row and "coverage_idle_rounds" in k_row.keys()
                    else 3
                )
                if k_val > 0 and idle_streak >= k_val:
                    mark_slots_skipped(
                        conn,
                        project_id,
                        tier="extended",
                        reason="diminishing returns",
                    )
                else:
                    raise HTTPException(
                        409,
                        "Extended coverage not yet saturated: "
                        f"{extended_open} slot(s) open, "
                        f"idle streak {idle_streak}/{k_val}",
                    )
        validate_facts_exist(conn, project_id, body.from_)
        validate_goal_not_in_sources(body.from_)
        if body.task_run_id is not None:
            validate_task_run_exists(conn, project_id, body.task_run_id)

        now = utcnow()
        intent_id = next_intent_id(conn, project_id)

        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, task_run_id, worker, last_heartbeat_at, created_at, concluded_at, conclusion) VALUES (?, ?, 'goal', ?, ?, ?, ?, ?, ?, ?, 'completed')",
            (intent_id, project_id, body.description, body.worker, body.task_run_id, body.worker, now, now, now),
        )
        # 完成项目并不新建 goal fact，而是把一个 completion intent 收敛到固定 goal 节点。
        bind_fact_evidence(conn, project_id, "goal", body.evidence)
        for source_order, fact_id in enumerate(body.from_):
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id, source_order) VALUES (?, ?, ?, ?)",
                (intent_id, project_id, fact_id, source_order),
            )
        close_open_intents_for_project_completion(conn, project_id, intent_id, closed_at=now)
        completed_row = conn.execute(
            """
            UPDATE projects
            SET status = 'completed',
                reason_worker = NULL,
                reason_trigger = NULL,
                reason_started_at = NULL,
                reason_last_heartbeat_at = NULL
            WHERE id = ? AND status = 'active'
            RETURNING id
            """,
            (project_id,),
        ).fetchone()
        if completed_row is None:
            raise HTTPException(409, "Project completion raced with another state change")
        if run is not None:
            # 完成提交和回执同事务持久化；重发不会再创建 goal 的入边。
            run_context.update(
                accepted_completion_digest=submission_digest,
                accepted_completion_intent_id=intent_id,
            )
            conn.execute(
                "UPDATE task_runs SET runtime_context_json = ? WHERE project_id = ? AND id = ?",
                (json.dumps(run_context, sort_keys=True), project_id, body.task_run_id),
            )
        return Intent(
            id=intent_id,
            **{"from": body.from_},
            to="goal",
            description=body.description,
            creator=body.worker,
            task_run_id=body.task_run_id,
            worker=body.worker,
            last_heartbeat_at=now,
            created_at=now,
            concluded_at=now,
        )


def project_reopen_reason(conn, project) -> str | None:
    """返回重新打开的权威拒绝原因；None 表示 legacy 项目可以重新打开。"""
    if project["status"] != "completed":
        return "project_status_invalid"
    try:
        require_execution_contract(project)
    except HTTPException as exc:
        return str(exc.detail)
    if policy_from_project_row(project).phased:
        return "assessment_new_project_required"
    report = conn.execute(
        "SELECT status FROM assessment_report_finalizations WHERE project_id = ?",
        (project["id"],),
    ).fetchone()
    if report is not None and report["status"] != "ready":
        return "assessment_report_finalization_outstanding"
    try:
        completion = get_completion_intent_or_409(conn, project["id"])
    except HTTPException as exc:
        return str(exc.detail)
    source = conn.execute(
        "SELECT 1 FROM intent_sources "
        "WHERE intent_id = ? AND project_id = ? LIMIT 1",
        (completion["id"], project["id"]),
    ).fetchone()
    if source is None:
        return "completion_intent_sources_missing"
    return None


def reopen_project_record(
    project_id: str,
    body: ReopenRequest,
    *,
    creator: str,
) -> ReopenResponse:
    with get_conn() as conn:
        # 与报告 request/claim 使用同一项目行写锁，避免 reopen 穿过冻结中的报告代次。
        conn.execute("UPDATE projects SET id = id WHERE id = ?", (project_id,))
        expire_reason_leases(conn, project_id)
        project = check_project_completed(conn, project_id)
        reason = project_reopen_reason(conn, project)
        if reason is not None:
            raise HTTPException(409, reason)
        completion = get_completion_intent_or_409(conn, project_id)

        source_rows = conn.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY source_order, fact_id",
            (completion["id"], project_id),
        ).fetchall()
        source_ids = [row["fact_id"] for row in source_rows]
        if not source_ids:
            raise HTTPException(409, "Completion intent is missing its source facts")

        now = utcnow()
        # reopen 会删除 completion intent，并把外部反馈写成新的 fact/intent，保留原来源链路。
        fact_id = next_fact_id(conn, project_id)
        intent_id = next_intent_id(conn, project_id)
        description = body.description

        conn.execute(
            "DELETE FROM intents WHERE id = ? AND project_id = ?",
            (completion["id"], project_id),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description, task_run_id, creator, created_at) VALUES (?, ?, ?, NULL, ?, ?)",
            (fact_id, project_id, description, creator, now),
        )
        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, task_run_id, worker, last_heartbeat_at, created_at, concluded_at) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
            (intent_id, project_id, fact_id, "external_feedback", creator, creator, now, now, now),
        )
        for source_order, source_id in enumerate(source_ids):
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id, source_order) VALUES (?, ?, ?, ?)",
                (intent_id, project_id, source_id, source_order),
            )
        clear_project_reason(conn, project_id)
        conn.execute(
            "UPDATE projects SET status = 'active' WHERE id = ?",
            (project_id,),
        )

        updated_project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        updated_intent = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()
        assert updated_project is not None
        assert updated_intent is not None
        return ReopenResponse(
            project=project_meta_from_row(conn, updated_project),
            fact=Fact(id=fact_id, description=description, creator=creator, created_at=now),
            intent=intent_to_model(conn, updated_intent, project_id),
        )
