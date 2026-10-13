"""项目级 reason 租约服务。"""

from __future__ import annotations

import json

from cairn.assessment_contracts import GOAL_DRIVEN_PROTOCOL
from cairn.server.db import get_conn
from cairn.server.models import CompleteRequest, HeartbeatRequest, ProjectMeta, ReasonClaimRequest

from .assessment_convergence import (
    advance_goal_driven_convergence,
    advance_structured_convergence,
)
from .assessment_policy import (
    policy_from_project_row,
    require_execution_contract,
    require_result_settlement_contract,
)
from .common import *
from .coverage_slots import mark_slots_skipped
from .ids import get_project_or_404
from .project_completion import complete_project_record_with_conn
from .projects import check_project_active, clear_project_reason, expire_reason_leases, lease_clock, project_meta_from_row, validate_task_run_exists, validate_execution_generation, validate_execution_lease
from .time_utils import utcnow


def _goal_driven_policy(row) -> bool:
    policy = policy_from_project_row(row)
    return (
        getattr(policy, "goal_driven", False) is True
        or policy.protocol == GOAL_DRIVEN_PROTOCOL
    )


def claim_project_reason_record(project_id: str, body: ReasonClaimRequest) -> ProjectMeta:
    with get_conn() as conn:
        conn.execute("UPDATE projects SET id = id WHERE id = ?", (project_id,))
        row = check_project_active(conn, project_id)
        require_execution_contract(row)
        if _goal_driven_policy(row):
            stop_reason = advance_goal_driven_convergence(conn, project_id)
            if stop_reason is not None:
                conn.commit()
                raise HTTPException(409, "assessment_stopping")
            row = get_project_or_404(conn, project_id)
            from .project_authorization import require_project_authorization_current

            require_project_authorization_current(conn, row)
        lease_expired = expire_reason_leases(conn, project_id)
        try:
            row = get_project_or_404(conn, project_id)
            from .assessment_attempts import require_execution_retry_eligible

            require_execution_retry_eligible(conn, row, "reason")
            # release 可能早于异步结果写回；已有执行终态落库前不能重新开始同一 Reason。
            if conn.execute(
                "SELECT 1 FROM task_runs WHERE project_id = ? AND execution_generation = ? AND task_type = 'reason' AND status IN ('queued', 'running') LIMIT 1",
                (project_id, row["execution_generation"]),
            ).fetchone() is not None:
                raise HTTPException(409, "reason_execution_pending")
            if body.task_run_id is not None or row["reason_worker"] == body.worker:
                validate_execution_lease(conn, project_id, body.task_run_id, body.worker)
            if body.task_run_id is not None:
                run = validate_task_run_exists(conn, project_id, body.task_run_id)
                if run["status"] != "running":
                    raise HTTPException(409, "Reason TaskRun is no longer active")
            advance_structured_convergence(conn, project_id)
            row = get_project_or_404(conn, project_id)
            if row["status"] != "active":
                conn.commit()
                raise HTTPException(409, "assessment_stopped")
            current_worker = row["reason_worker"]
            # reason 是项目级全局推理任务，同一时间只允许一个 worker 持有。
            if current_worker is not None and current_worker != body.worker:
                raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")
            if current_worker == body.worker:
                return project_meta_from_row(conn, row)

            now = utcnow()
            boot_id, now_ns = lease_clock()
            updated = conn.execute(
                """
                UPDATE projects
                SET reason_worker = ?,
                    reason_trigger = ?,
                    reason_started_at = ?,
                    reason_last_heartbeat_at = ?,
                    reason_heartbeat_boot_id = ?,
                    reason_heartbeat_boottime_ns = ?
                WHERE id = ? AND reason_worker IS NULL
                RETURNING *
                """,
                (body.worker, body.trigger, now, now, boot_id, now_ns, project_id),
            ).fetchone()
            if updated is None:
                # 条件 UPDATE 关闭并发校验窗口，避免两个 reason worker 同时认领。
                latest = get_project_or_404(conn, project_id)
                raise HTTPException(
                    409,
                    f"Project reason is currently claimed by {latest['reason_worker']}",
                )
            return project_meta_from_row(conn, updated)
        except HTTPException:
            if lease_expired:
                # 过期失租和 Run 终态须先提交，再向迟到的 claim 返回拒绝。
                conn.commit()
            raise


def heartbeat_project_reason_record(project_id: str, body: HeartbeatRequest) -> ProjectMeta:
    with get_conn() as conn:
        row = check_project_active(conn, project_id)
        if _goal_driven_policy(row):
            stop_reason = advance_goal_driven_convergence(conn, project_id)
            if stop_reason is not None:
                conn.commit()
                raise HTTPException(409, "assessment_stopping")
        lease_expired = expire_reason_leases(conn, project_id)
        try:
            row = get_project_or_404(conn, project_id)
            if _goal_driven_policy(row) and body.task_run_id is None:
                raise HTTPException(409, "execution_task_run_required")
            validate_execution_lease(conn, project_id, body.task_run_id, body.worker)
            current_worker = row["reason_worker"]
            if current_worker is None:
                raise HTTPException(409, "Project reason is not currently claimed")
            if current_worker != body.worker:
                raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")
            if body.task_run_id is not None:
                run = validate_task_run_exists(conn, project_id, body.task_run_id)
                if run["status"] != "running":
                    raise HTTPException(409, "Reason TaskRun is no longer active")

            now = utcnow()
            boot_id, now_ns = lease_clock()
            conn.execute(
                """UPDATE projects SET reason_last_heartbeat_at = ?,
                   reason_heartbeat_boot_id = ?, reason_heartbeat_boottime_ns = ? WHERE id = ?""",
                (now, boot_id, now_ns, project_id),
            )
            updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            return project_meta_from_row(conn, updated)
        except HTTPException:
            if lease_expired:
                # 拒绝迟到心跳不能回滚刚刚写入的失租和 TaskRun 终态。
                conn.commit()
            raise


def release_project_reason_record(project_id: str, body: HeartbeatRequest) -> ProjectMeta:
    with get_conn() as conn:
        check_project_active(conn, project_id)
        validate_execution_lease(conn, project_id, body.task_run_id, body.worker)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["reason_worker"]
        if current_worker is None:
            return project_meta_from_row(conn, row)
        if current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")

        clear_project_reason(conn, project_id)
        if not _goal_driven_policy(row):
            # 历史覆盖路径保留原收敛行为；goal-driven 的租约释放绝不等于完成。
            stop_reason = _maybe_converge_coverage(conn, project_id, row)
            if (
                stop_reason == "coverage_complete"
                and not policy_from_project_row(row).phased
            ):
                source_rows = conn.execute(
                    """
                    SELECT id
                    FROM facts
                    WHERE project_id = ? AND id <> 'goal'
                    ORDER BY created_at, id
                    """,
                    (project_id,),
                ).fetchall()
                source_ids = [str(source["id"]) for source in source_rows]
                if not source_ids:
                    raise HTTPException(
                        409,
                        "Structured completion has no source facts",
                    )
                complete_project_record_with_conn(
                    conn,
                    project_id,
                    CompleteRequest(
                        from_=source_ids,
                        description=(
                            "Structured assessment coverage converged; all required "
                            "Coverage Cells are terminal and no open work remains."
                        ),
                        worker=body.worker,
                        task_run_id=body.task_run_id,
                    ),
                )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(conn, updated)


def accept_depth_review(project_id: str, body):
    """成功选题 Fact 与最多三个深测义务原子接受；租约释放不等于选题成功。"""
    from hashlib import sha256
    from cairn.server.models import DepthReviewResponse, EvidenceRef, Fact
    from .assessment_convergence import assessment_progress, assessment_review_digest, _set_phase
    from .ids import next_fact_id, next_intent_id
    from .projects import bind_fact_evidence, intent_to_model, validate_facts_exist, validate_goal_not_in_sources

    candidate = body.model_dump(by_alias=True, exclude_none=True)
    submission_digest = sha256(json.dumps(candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    with get_conn() as conn:
        # 先核对已接受摘要；停止/完成只能挡新写入，不能否认原事务的收据。
        conn.execute("UPDATE projects SET id = id WHERE id = ?", (project_id,))
        project = get_project_or_404(conn, project_id)
        require_result_settlement_contract(
            project,
            task_run_id=body.task_run_id,
        )
        progress = assessment_progress(project)
        accepted_id = progress.get("depth_review_fact_id")
        if accepted_id:
            accepted = conn.execute("SELECT * FROM facts WHERE id = ? AND project_id = ?", (accepted_id, project_id)).fetchone()
            record = json.loads(accepted["assessment_record_json"])
            if record.get("submission_digest") != submission_digest:
                raise HTTPException(409, "depth_review_already_accepted")
            intents = conn.execute("SELECT * FROM intents WHERE project_id = ? AND depth_review_fact_id = ? ORDER BY id", (project_id, accepted_id)).fetchall()
            return DepthReviewResponse(fact=Fact(**dict(accepted), assessment_record=record), intents=[intent_to_model(conn, row, project_id) for row in intents], project=project_meta_from_row(conn, project))
        check_project_active(conn, project_id)
        if not policy_from_project_row(project).phased or progress.get("phase") != "depth_review":
            raise HTTPException(409, "depth_review_not_pending")
        expire_reason_leases(conn, project_id)
        project = get_project_or_404(conn, project_id)
        if project["reason_worker"] != body.worker:
            raise HTTPException(409, "depth_review_reason_lease_mismatch")
        run = validate_execution_generation(conn, project_id, body.task_run_id)
        if run["task_type"] != "reason" or run["worker_name"] != body.worker or run["status"] != "running" or run["worker_type"] != "pi":
            raise HTTPException(409, "depth_review_reason_run_mismatch")
        if body.input_digest != assessment_review_digest(conn, project_id):
            raise HTTPException(409, "depth_review_input_stale")
        policy = policy_from_project_row(project)
        if policy.explore_deadline and utcnow() >= policy.explore_deadline:
            raise HTTPException(409, "explore_deadline_exceeded")
        keys = [(item.slot_key, item.strategy_key) for item in body.selected]
        if len(set(keys)) != len(keys):
            raise HTTPException(422, "depth_review_duplicate_hypothesis")
        all_refs = [*body.evidence_refs, *(ref for item in body.selected for ref in item.evidence_refs)]
        if any(ref.artifact_id is None or ref.artifact_handle is not None for ref in all_refs):
            raise HTTPException(422, "assessment_evidence_handle_unresolved")
        refs = [EvidenceRef(artifact_id=ref.artifact_id, pointer=ref.pointer, verifier_run_id=ref.verifier_run_id) for ref in all_refs]
        for item in body.selected:
            validate_facts_exist(conn, project_id, item.from_)
            validate_goal_not_in_sources(item.from_)
            cell = conn.execute("SELECT * FROM coverage_slots WHERE project_id = ? AND slot_key = ?", (project_id, item.slot_key)).fetchone()
            if cell is None:
                raise HTTPException(422, "depth_review_cell_missing")
            source_evidence = {
                (row["artifact_id"], row["pointer"] or "")
                for source in item.from_
                for row in conn.execute("SELECT artifact_id, pointer FROM fact_evidence WHERE project_id = ? AND fact_id = ?", (project_id, source))
            }
            for observation in conn.execute("SELECT evidence_json FROM surface_manifest_entries WHERE project_id = ? AND surface_id = ?", (project_id, cell["surface_id"])):
                source_evidence.update((ref.get("artifact_id"), ref.get("pointer") or "") for ref in json.loads(observation["evidence_json"] or "[]"))
            if any((ref.artifact_id, ref.pointer or "") not in source_evidence for ref in item.evidence_refs):
                raise HTTPException(422, "depth_review_evidence_not_linked_to_input")
            if conn.execute("SELECT 1 FROM intents WHERE project_id = ? AND slot_key = ? AND strategy_key = ? AND depth_review_fact_id IS NOT NULL LIMIT 1", (project_id, item.slot_key, item.strategy_key)).fetchone():
                raise HTTPException(409, "depth_review_strategy_already_selected")
            if cell["blocker_cooldown_until"] and cell["blocker_cooldown_until"] > utcnow() and cell["blocker_strategy_key"] == item.strategy_key:
                raise HTTPException(409, "coverage_cell_blocker_cooldown_active")
        now = utcnow()
        fid = next_fact_id(conn, project_id)
        record = {"version": 1, "kind": "depth_review", **candidate, "submission_digest": submission_digest}
        conn.execute(
            "INSERT INTO facts (id,project_id,description,task_run_id,creator,created_at,assessment_record_json) VALUES (?,?,?,?,?,?,?)",
            (fid, project_id, body.description, body.task_run_id, body.worker, now, json.dumps(record, ensure_ascii=False, sort_keys=True)),
        )
        bind_fact_evidence(conn, project_id, fid, refs)
        selected = []
        for item in body.selected:
            iid = next_intent_id(conn, project_id)
            conn.execute(
                "INSERT INTO intents (id,project_id,description,creator,task_run_id,created_at,slot_key,strategy_key,depth_review_fact_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (iid, project_id, item.description, body.worker, body.task_run_id, now, item.slot_key, item.strategy_key, fid),
            )
            for order, source in enumerate(dict.fromkeys([*item.from_, fid])):
                conn.execute("INSERT INTO intent_sources (intent_id,project_id,fact_id,source_order) VALUES (?,?,?,?)", (iid, project_id, source, order))
            # 补充实验不占用或重写基线 Cell；深测义务以显式 Fact 外键投影。
            selected.append(intent_to_model(conn, conn.execute("SELECT * FROM intents WHERE project_id = ? AND id = ?", (project_id, iid)).fetchone(), project_id))
        _set_phase(conn, project_id, "deep", depth_review_fact_id=fid, accepted_input_digest=body.input_digest)
        advance_structured_convergence(conn, project_id)
        return DepthReviewResponse(
            fact=Fact(id=fid, description=body.description, task_run_id=body.task_run_id, creator=body.worker, created_at=now, assessment_record=record),
            intents=selected,
            project=project_meta_from_row(conn, get_project_or_404(conn, project_id)),
        )


def _maybe_converge_coverage(conn, project_id: str, row) -> str | None:
    # default-copy-v1 只认冻结协议和结构化 checkpoint，不沿用 legacy Fact/succeeded 计数。
    if policy_from_project_row(row).structured:
        return advance_structured_convergence(conn, project_id)

    # 仅 legacy coverage_gate 场景执行 ABD；其他场景跳过，加性无副作用。
    scenario_row = conn.execute(
        "SELECT s.features_json FROM projects p JOIN scenarios s ON p.scenario_key = s.key WHERE p.id = ?",
        (project_id,),
    ).fetchone()
    features = json.loads(scenario_row["features_json"]) if scenario_row and scenario_row["features_json"] else {}
    if not features.get("coverage_gate"):
        return

    # idle streak：本轮 reason 周期内（自上次 release）是否有新 fact 产出（含 explore）。
    fact_count = conn.execute(
        "SELECT COUNT(*) AS c FROM facts WHERE project_id = ?", (project_id,)
    ).fetchone()["c"]
    last = row["reason_last_fact_count"] if "reason_last_fact_count" in row.keys() else 0
    prev_idle = row["reason_idle_streak"] if "reason_idle_streak" in row.keys() else 0
    idle = 0 if fact_count > last else prev_idle + 1
    conn.execute(
        "UPDATE projects SET reason_idle_streak = ?, reason_last_fact_count = ? WHERE id = ?",
        (idle, fact_count, project_id),
    )

    # 历史覆盖路径仍保留执行截止收口，不再以完成次数强制结算。
    deadline = row["explore_deadline"] if "explore_deadline" in row.keys() else None
    if deadline and utcnow() >= deadline:
        mark_slots_skipped(conn, project_id, tier=None, reason="deadline coverage tail")
        return

    # B 收益递减：连续 K 轮无新 fact → 收尾 extended（core 仍由完成门 A 保证）。
    k_row = conn.execute("SELECT coverage_idle_rounds FROM settings WHERE id = 1").fetchone()
    k_val = k_row["coverage_idle_rounds"] if k_row and "coverage_idle_rounds" in k_row.keys() else 3
    if k_val > 0 and idle >= k_val:
        mark_slots_skipped(conn, project_id, tier="extended", reason="diminishing returns")
