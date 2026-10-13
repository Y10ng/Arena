"""default-copy-v1 的 Coverage checkpoint 与有限收敛。"""

from __future__ import annotations

import sqlite3
import json
from hashlib import sha256
from dataclasses import asdict, dataclass

from fastapi import HTTPException

from cairn.assessment_contracts import GOAL_DRIVEN_PROTOCOL

from .assessment_policy import (
    execution_rejection_reason, parse_scenario_snapshot, policy_from_project_row, row_value,
)
from .coverage_scope import has_current_coverage_scope_disposition
from .ids import get_project_or_404
from .time_utils import utcnow


@dataclass(frozen=True)
class CoverageCheckpoint:
    surface_count: int
    cell_count: int
    terminal_count: int
    required_count: int = 0
    optional_count: int = 0
    required_terminal_count: int = 0
    manifest_surface_count: int = 0
    required_surface_count: int = 0
    scope_disposition_valid: bool = False
    minimum_coverage_enabled: bool = False

    @property
    def complete(self) -> bool:
        return self.unresolved_count == 0

    @property
    def unresolved_count(self) -> int:
        if not self.minimum_coverage_enabled:
            return max(0, self.cell_count - self.terminal_count)
        if self.manifest_surface_count == 0:
            return 0 if self.scope_disposition_valid else 1
        missing_required_surfaces = max(
            0,
            self.manifest_surface_count - self.required_surface_count,
        )
        open_required_cells = max(
            0,
            self.required_count - self.required_terminal_count,
        )
        return missing_required_surfaces + open_required_cells


def structured_coverage_checkpoint(
    conn: sqlite3.Connection,
    project_id: str,
) -> CoverageCheckpoint:
    project = get_project_or_404(conn, project_id)
    policy = policy_from_project_row(project)
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT NULLIF(surface_key, '')) AS surface_count,
               COUNT(*) AS cell_count,
               SUM(
                   CASE WHEN status IN ('explored', 'verified', 'skipped')
                        THEN 1 ELSE 0 END
               ) AS terminal_count,
               SUM(CASE WHEN coverage_requirement = 'required' THEN 1 ELSE 0 END)
                   AS required_count,
               SUM(CASE WHEN coverage_requirement = 'optional' THEN 1 ELSE 0 END)
                   AS optional_count,
               SUM(
                   CASE WHEN coverage_requirement = 'required'
                          AND status IN ('explored', 'verified', 'skipped')
                        THEN 1 ELSE 0 END
               ) AS required_terminal_count,
               COUNT(DISTINCT CASE
                   WHEN coverage_requirement = 'required' THEN surface_id
               END) AS required_surface_count
        FROM coverage_slots
        WHERE project_id = ? AND surface_key IS NOT NULL
        """,
        (project_id,),
    ).fetchone()
    manifest_surface_count = 0
    scope_disposition_valid = False
    if policy.minimum_coverage_enabled:
        manifest_row = conn.execute(
            """
            SELECT COUNT(DISTINCT surface_id) AS c
            FROM surface_manifest_entries
            WHERE project_id = ?
            """,
            (project_id,),
        ).fetchone()
        manifest_surface_count = int(manifest_row["c"] or 0)
        if manifest_surface_count == 0:
            scope_disposition_valid = has_current_coverage_scope_disposition(
                conn,
                project_id,
            )
    legacy_surface_count = int(row["surface_count"] or 0)
    return CoverageCheckpoint(
        surface_count=(
            manifest_surface_count
            if policy.minimum_coverage_enabled
            else legacy_surface_count
        ),
        cell_count=int(row["cell_count"] or 0),
        terminal_count=int(row["terminal_count"] or 0),
        required_count=int(row["required_count"] or 0),
        optional_count=int(row["optional_count"] or 0),
        required_terminal_count=int(row["required_terminal_count"] or 0),
        manifest_surface_count=manifest_surface_count,
        required_surface_count=int(row["required_surface_count"] or 0),
        scope_disposition_valid=scope_disposition_valid,
        minimum_coverage_enabled=policy.minimum_coverage_enabled,
    )


def _open_intent_counts(
    conn: sqlite3.Connection,
    project_id: str,
) -> tuple[int, int]:
    row = conn.execute(
        """
        SELECT SUM(CASE WHEN worker IS NULL THEN 1 ELSE 0 END) AS unclaimed,
               SUM(CASE WHEN worker IS NOT NULL THEN 1 ELSE 0 END) AS running
        FROM intents
        WHERE project_id = ?
          AND to_fact_id IS NULL
          AND concluded_at IS NULL
        """,
        (project_id,),
    ).fetchone()
    return int(row["unclaimed"] or 0), int(row["running"] or 0)


def _core_open_count(conn: sqlite3.Connection, project_id: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM coverage_slots
        WHERE project_id = ? AND surface_key IS NOT NULL
          AND tier = 'core' AND status IN ('pending', 'exploring')
        """,
        (project_id,),
    ).fetchone()
    return int(row["c"] or 0)


def _abandon_unclaimed_intents(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    reason: str,
) -> int:
    now = utcnow()
    cur = conn.execute(
        """
        UPDATE intents
        SET concluded_at = ?, conclusion = 'abandoned', conclusion_reason = ?
        WHERE project_id = ?
          AND to_fact_id IS NULL
          AND concluded_at IS NULL
          AND worker IS NULL
          AND NOT EXISTS (SELECT 1 FROM task_runs t WHERE t.project_id = intents.project_id
              AND t.intent_id = intents.id AND t.status IN ('queued','running'))
          AND NOT EXISTS (SELECT 1 FROM assessment_attempts a WHERE a.project_id = intents.project_id
              AND a.intent_id = intents.id AND a.status = 'active')
          AND ( ? IN ('deadline_exceeded', 'user_stopped')
                OR (assessment_role != 'verifier' AND depth_review_fact_id IS NULL))
        """,
        (now, reason, project_id, reason),
    )
    return int(cur.rowcount)


def _skip_open_cells(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    reason: str,
    tier: str | None = None,
    coverage_requirement: str | None = None,
) -> int:
    clauses = [
        "project_id = ?",
        "surface_key IS NOT NULL",
        "status IN ('pending', 'exploring')",
    ]
    params: list[object] = [project_id]
    if tier is not None:
        clauses.append("tier = ?")
        params.append(tier)
    if coverage_requirement is not None:
        clauses.append("coverage_requirement = ?")
        params.append(coverage_requirement)
    # 已接受的阻塞原因属于实验结果；项目级停止原因不能覆盖它，报告须分别呈现。
    cur = conn.execute(
        f"""
        UPDATE coverage_slots
        SET status = 'skipped',
            skip_reason = CASE
                WHEN result = 'temporarily_blocked' AND resulting_fact_id IS NOT NULL
                     AND COALESCE(skip_reason, '') != '' THEN skip_reason
                ELSE ?
            END,
            assigned_intent_id = NULL, updated_at = ?
        WHERE {' AND '.join(clauses)}
        """,
        (reason, utcnow(), *params),
    )
    return int(cur.rowcount)


def _persist_checkpoint(
    conn: sqlite3.Connection,
    project_id: str,
    checkpoint: CoverageCheckpoint,
    *,
    idle_streak: int,
    stop_reason: str | None,
) -> None:
    conn.execute(
        """
        UPDATE projects
        SET coverage_checkpoint_surface_count = ?,
            coverage_checkpoint_cell_count = ?,
            coverage_checkpoint_terminal_count = ?,
            coverage_checkpoint_required_count = ?,
            coverage_checkpoint_optional_count = ?,
            coverage_checkpoint_required_terminal_count = ?,
            reason_idle_streak = ?,
            coverage_stop_reason = COALESCE(?, coverage_stop_reason)
        WHERE id = ?
        """,
        (
            checkpoint.surface_count,
            checkpoint.cell_count,
            checkpoint.terminal_count,
            checkpoint.required_count,
            checkpoint.optional_count,
            checkpoint.required_terminal_count,
            idle_streak,
            stop_reason,
            project_id,
        ),
    )


def advance_structured_convergence(
    conn: sqlite3.Connection,
    project_id: str,
) -> str | None:
    """在 Reason 周期末更新有效进展，并在硬限制或饱和时收尾。"""
    project = get_project_or_404(conn, project_id)
    policy = policy_from_project_row(project)
    if not policy.structured or project["status"] == "paused":
        return None
    if policy.phased:
        return advance_assessment_phase(conn, project_id)

    checkpoint = structured_coverage_checkpoint(conn, project_id)
    previous = CoverageCheckpoint(
        surface_count=int(row_value(project, "coverage_checkpoint_surface_count") or 0),
        cell_count=int(row_value(project, "coverage_checkpoint_cell_count") or 0),
        terminal_count=int(row_value(project, "coverage_checkpoint_terminal_count") or 0),
        required_count=int(
            row_value(project, "coverage_checkpoint_required_count") or 0
        ),
        optional_count=int(
            row_value(project, "coverage_checkpoint_optional_count") or 0
        ),
        required_terminal_count=int(
            row_value(project, "coverage_checkpoint_required_terminal_count") or 0
        ),
    )
    previous_idle = int(row_value(project, "reason_idle_streak") or 0)
    _, running = _open_intent_counts(conn, project_id)
    now = utcnow()

    stop_reason: str | None = None
    skip_reason: str | None = None
    if policy.explore_deadline and now >= policy.explore_deadline:
        stop_reason = "deadline_exceeded"
        skip_reason = "deadline_skipped"

    if stop_reason is not None:
        _abandon_unclaimed_intents(conn, project_id, reason=stop_reason)
        if running == 0:
            _skip_open_cells(conn, project_id, reason=skip_reason or stop_reason)
        checkpoint = structured_coverage_checkpoint(conn, project_id)
        _persist_checkpoint(
            conn,
            project_id,
            checkpoint,
            idle_streak=0,
            stop_reason=stop_reason,
        )
        return stop_reason

    if checkpoint.complete and running == 0:
        _abandon_unclaimed_intents(conn, project_id, reason="coverage_complete")
        if checkpoint.minimum_coverage_enabled:
            # required checkpoint 完成后，optional 尾部不应继续制造工作或保留失效 assignment。
            _skip_open_cells(
                conn,
                project_id,
                reason="coverage_complete_skipped",
                coverage_requirement="optional",
            )
            checkpoint = structured_coverage_checkpoint(conn, project_id)
        _persist_checkpoint(
            conn,
            project_id,
            checkpoint,
            idle_streak=0,
            stop_reason="coverage_complete",
        )
        return "coverage_complete"

    progressed = (
        checkpoint.surface_count > previous.surface_count
        or checkpoint.cell_count > previous.cell_count
        or checkpoint.terminal_count > previous.terminal_count
    )
    can_saturate = _core_open_count(conn, project_id) == 0 and running == 0
    idle_streak = 0 if progressed or not can_saturate else previous_idle + 1
    settings = conn.execute(
        "SELECT coverage_idle_rounds FROM settings WHERE id = 1"
    ).fetchone()
    limit = int(settings["coverage_idle_rounds"] or 3) if settings else 3
    if limit > 0 and idle_streak >= limit:
        _abandon_unclaimed_intents(conn, project_id, reason="saturated")
        _skip_open_cells(
            conn,
            project_id,
            reason="saturation_skipped",
            tier="extended",
        )
        checkpoint = structured_coverage_checkpoint(conn, project_id)
        _persist_checkpoint(
            conn,
            project_id,
            checkpoint,
            idle_streak=idle_streak,
            stop_reason="saturated",
        )
        return "saturated"

    _persist_checkpoint(
        conn,
        project_id,
        checkpoint,
        idle_streak=idle_streak,
        stop_reason=None,
    )
    return None


def assessment_progress(project) -> dict:
    return parse_scenario_snapshot(row_value(project, "assessment_progress_json"))


def _set_phase(conn, project_id: str, phase: str, **updates) -> dict:
    project = get_project_or_404(conn, project_id)
    progress = assessment_progress(project)
    if progress.get("phase") != phase:
        progress.update(phase=phase, phase_started_at=utcnow())
    progress.update(updates)
    conn.execute(
        "UPDATE projects SET assessment_progress_json = ? WHERE id = ?",
        (json.dumps(progress, ensure_ascii=False, sort_keys=True), project_id),
    )
    return progress


def assessment_review_input(conn, project_id: str) -> dict:
    """摘要绑定实际前提、Manifest traits 与已接受实验，不包含租约噪声。"""
    def rows(query):
        return [dict(row) for row in conn.execute(query, (project_id,)).fetchall()]

    return {
        "version": 1,
        "project_id": project_id,
        "targets": rows("SELECT id, value, type, status FROM targets WHERE project_id = ? ORDER BY id"),
        "manifest": rows("SELECT entry_id, surface_id, traits_json, evidence_json, observation_fingerprint FROM surface_manifest_entries WHERE project_id = ? ORDER BY entry_id"),
        "cells": rows("SELECT slot_key, status, result, skip_reason, resulting_fact_id, coverage_requirement FROM coverage_slots WHERE project_id = ? ORDER BY slot_key"),
        "facts": rows("SELECT id, description, task_run_id, assessment_record_json FROM facts WHERE project_id = ? AND id != 'goal' ORDER BY id"),
        "evidence": rows("SELECT fact_id, artifact_id, pointer FROM fact_evidence WHERE project_id = ? ORDER BY fact_id, artifact_id, pointer"),
        "observations": rows("SELECT id, source_fact_id, task_run_id, identity_json FROM vulnerability_observations WHERE project_id = ? ORDER BY id"),
    }


def assessment_review_digest(conn, project_id: str) -> str:
    canonical = json.dumps(assessment_review_input(conn, project_id), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(canonical.encode()).hexdigest()


def assessment_execution_view(conn, project) -> dict:
    policy = policy_from_project_row(project)
    progress = assessment_progress(project)
    total = int(row_value(project, "explore_attempts_started") or 0)
    verified = int(row_value(project, "verification_attempts_started") or 0)
    rejection = execution_rejection_reason(project)
    return {
        "version": 2,
        "enabled": policy.logical_attempts_enabled,
        "goal_driven": policy.goal_driven,
        "executable": rejection is None,
        "rejection_reason": rejection,
        "execution_contract_version": 1,
        "execution_engine": "pi",
        "contract_version": policy.contract_version,
        "structured": policy.structured,
        "outcome_contract_version": policy.outcome_contract_version,
        "blocker_cooldown_enabled": policy.blocker_cooldown_enabled,
        "vulnerability_identity_enabled": policy.vulnerability_identity_enabled,
        "surface_manifest_enabled": policy.surface_manifest_enabled,
        "minimum_coverage_enabled": policy.minimum_coverage_enabled,
        "independent_verifier_enabled": policy.independent_verifier_enabled,
        "phase": progress.get("phase"),
        "phase_started_at": progress.get("phase_started_at"),
        "depth_review_fact_id": progress.get("depth_review_fact_id"),
        "accepted_input_digest": progress.get("accepted_input_digest"),
        "review_input_digest": assessment_review_digest(conn, project["id"]) if policy.phased and progress.get("phase") == "depth_review" else None,
        "deadline": policy.explore_deadline,
        "authorization_expires_at": row_value(project, "authorization_expires_at"),
        "attempts_started": total,
        "verification_attempts_started": verified,
        "stop_reason": row_value(project, "coverage_stop_reason"),
        "current_tasks": [dict(row) for row in conn.execute(
            "SELECT id, intent_id, task_type, status FROM task_runs WHERE project_id = ? AND status IN ('queued', 'running') ORDER BY id", (project["id"],)
        )],
    }


def assessment_has_running_work(conn, project_id: str, *, exclude_task_run_id: str | None = None) -> bool:
    return conn.execute(
        "SELECT 1 FROM task_runs WHERE project_id = ? AND task_type != 'report' AND status IN ('queued', 'running') AND (CAST(? AS TEXT) IS NULL OR id != ?) LIMIT 1",
        (project_id, exclude_task_run_id, exclude_task_run_id),
    ).fetchone() is not None


_GOAL_DRIVEN_STOP_REASONS = {
    "deadline_exceeded",
    "user_stopped",
}


def _goal_driven_pending_candidate(conn, project_id: str) -> bool:
    return conn.execute(
        """
        SELECT 1
        FROM task_run_candidates AS candidate
        WHERE candidate.project_id = ?
          AND NOT EXISTS (
              SELECT 1
              FROM task_run_candidate_outcomes AS outcome
              WHERE outcome.project_id = candidate.project_id
                AND outcome.candidate_id = candidate.id
          )
        LIMIT 1
        """,
        (project_id,),
    ).fetchone() is not None


def _goal_driven_pending_cleanup(conn, project_id: str) -> bool:
    """人工停止必须等待对应代次的进程终止与资源清理正向证明。"""
    for row in conn.execute(
        """
        SELECT execution_generation, runtime_context_json
        FROM task_runs
        WHERE project_id = ? AND task_type != 'report' AND pause_requested = 1
        """,
        (project_id,),
    ).fetchall():
        context = parse_scenario_snapshot(row["runtime_context_json"])
        lifecycle = context.get("execution_lifecycle")
        if not (
            isinstance(lifecycle, dict)
            and type(lifecycle.get("generation")) is int
            and lifecycle["generation"] == row["execution_generation"]
            and lifecycle.get("termination_confirmed") is True
            and lifecycle.get("cleanup_confirmed") is True
        ):
            return True
    return False


def _conclude_goal_driven_unrunnable_intents(
    conn,
    project_id: str,
    *,
    reason: str,
) -> None:
    """仅关闭没有物理执行或逻辑 Attempt 归属的剩余工作。"""
    conn.execute(
        """
        UPDATE intents
        SET concluded_at = ?, conclusion = 'abandoned',
            conclusion_reason = ?, worker = NULL
        WHERE project_id = ? AND concluded_at IS NULL AND to_fact_id IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM task_runs AS task
              WHERE task.project_id = intents.project_id
                AND task.intent_id = intents.id
                AND task.status IN ('queued', 'running')
          )
          AND NOT EXISTS (
              SELECT 1 FROM assessment_attempts AS attempt
              WHERE attempt.project_id = intents.project_id
                AND attempt.intent_id = intents.id
                AND attempt.status = 'active'
          )
        """,
        (utcnow(), reason, project_id),
    )


def advance_goal_driven_convergence(
    conn,
    project_id: str,
    *,
    reason: str | None = None,
) -> str | None:
    """记录新协议硬停止原因，并在已接受的在途结果收口后转为 stopped。"""
    project = get_project_or_404(conn, project_id)
    policy = policy_from_project_row(project)
    if policy.protocol != GOAL_DRIVEN_PROTOCOL or project["status"] == "completed":
        return row_value(project, "coverage_stop_reason")

    existing = row_value(project, "coverage_stop_reason")
    if existing in _GOAL_DRIVEN_STOP_REASONS:
        reason = str(existing)
    elif reason is None:
        now = utcnow()
        if policy.explore_deadline and now >= policy.explore_deadline:
            reason = "deadline_exceeded"
    if reason not in _GOAL_DRIVEN_STOP_REASONS:
        return None

    conn.execute(
        """
        UPDATE projects
        SET coverage_stop_reason = COALESCE(coverage_stop_reason, ?)
        WHERE id = ? AND status != 'completed'
        """,
        (reason, project_id),
    )

    if reason in {"deadline_exceeded", "user_stopped"}:
        # 截止和人工停止不再启动免费 retry；仍在运行的 TaskRun 保留结果/清理归属。
        conn.execute(
            """
            UPDATE assessment_attempts
            SET status = 'failed', ended_at = ?, outcome = ?
            WHERE project_id = ? AND status = 'active'
              AND NOT EXISTS (
                  SELECT 1 FROM task_runs AS task
                  WHERE task.project_id = assessment_attempts.project_id
                    AND task.assessment_attempt_id = assessment_attempts.id
                    AND task.status IN ('queued', 'running')
              )
            """,
            (utcnow(), reason, project_id),
        )

    _conclude_goal_driven_unrunnable_intents(
        conn,
        project_id,
        reason=reason,
    )
    active_attempt = conn.execute(
        """
        SELECT 1 FROM assessment_attempts
        WHERE project_id = ? AND status = 'active'
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    if (
        not assessment_has_running_work(conn, project_id)
        and active_attempt is None
        and not _goal_driven_pending_candidate(conn, project_id)
        and not _goal_driven_pending_cleanup(conn, project_id)
    ):
        conn.execute(
            """
            UPDATE projects
            SET status = 'stopped',
                reason_worker = NULL,
                reason_trigger = NULL,
                reason_started_at = NULL,
                reason_last_heartbeat_at = NULL
            WHERE id = ? AND status = 'active'
            """,
            (project_id,),
        )
    return reason


def stop_assessment(conn, project_id: str, reason: str) -> None:
    """仅处置未运行义务；在途执行必须由 Dispatcher 停止并 reconciliation。"""
    project = get_project_or_404(conn, project_id)
    policy = policy_from_project_row(project)
    conn.execute(
        "UPDATE intents SET conclusion_reason = ? WHERE project_id = ? AND concluded_at IS NULL",
        (reason, project_id),
    )
    conn.execute(
        "UPDATE intents SET concluded_at = ?, conclusion = 'abandoned', worker = NULL WHERE project_id = ? AND concluded_at IS NULL AND NOT EXISTS (SELECT 1 FROM task_runs t WHERE t.project_id = intents.project_id AND t.intent_id = intents.id AND t.status IN ('queued','running'))",
        (utcnow(), project_id),
    )
    conn.execute(
        "UPDATE projects SET status = 'stopped', coverage_stop_reason = ? WHERE id = ? AND status != 'completed'",
        (reason, project_id),
    )
    if policy.phased:
        _set_phase(conn, project_id, "finalizing", stop_reason=reason)
    # 等待重试但已无在途 execution 的逻辑尝试不能永远保持 active。
    conn.execute(
        """UPDATE assessment_attempts SET status = 'failed', ended_at = ?, outcome = ?
           WHERE project_id = ? AND status = 'active' AND NOT EXISTS (
               SELECT 1 FROM task_runs t WHERE t.project_id = assessment_attempts.project_id
                 AND t.assessment_attempt_id = assessment_attempts.id
                 AND t.status IN ('queued', 'running')
           )""",
        (utcnow(), reason, project_id),
    )
    if policy.protocol == GOAL_DRIVEN_PROTOCOL:
        _conclude_goal_driven_unrunnable_intents(
            conn,
            project_id,
            reason=reason,
        )
    elif not assessment_has_running_work(conn, project_id):
        _skip_open_cells(conn, project_id, reason=reason)


def _baseline_accounted(conn, project_id: str) -> bool:
    checkpoint = structured_coverage_checkpoint(conn, project_id)
    if checkpoint.complete:
        return True
    if checkpoint.manifest_surface_count == 0 or checkpoint.required_surface_count < checkpoint.manifest_surface_count:
        return False
    return conn.execute(
        "SELECT 1 FROM coverage_slots WHERE project_id = ? AND coverage_requirement = 'required' AND status NOT IN ('explored','verified','skipped') AND NOT (result = 'temporarily_blocked' AND resulting_fact_id IS NOT NULL) LIMIT 1",
        (project_id,),
    ).fetchone() is None


def advance_assessment_phase(conn, project_id: str) -> str | None:
    conn.execute("UPDATE projects SET id = id WHERE id = ?", (project_id,))
    if get_project_or_404(conn, project_id)["status"] == "paused":
        return None
    result = _advance_assessment_phase(conn, project_id)
    project = get_project_or_404(conn, project_id)
    if policy_from_project_row(project).phased:
        # 新阶段取代旧饱和逻辑，但公开覆盖计数仍必须反映处置后的真实 Cell。
        _persist_checkpoint(
            conn, project_id, structured_coverage_checkpoint(conn, project_id),
            idle_streak=0, stop_reason=result,
        )
    return result


def _advance_assessment_phase(conn, project_id: str) -> str | None:
    project = get_project_or_404(conn, project_id)
    policy = policy_from_project_row(project)
    progress = assessment_progress(project)
    if not policy.phased or project["status"] != "active":
        return row_value(project, "coverage_stop_reason")
    # finalizing 已封闭探索义务；探索截止时间不能把恢复后的同阶段收尾改为停止。
    if progress.get("phase") == "finalizing":
        return row_value(project, "coverage_stop_reason")
    if policy.explore_deadline and utcnow() >= policy.explore_deadline:
        stop_assessment(conn, project_id, "deadline_exceeded")
        return "deadline_exceeded"
    _, running = _open_intent_counts(conn, project_id)
    active_explore = conn.execute(
        "SELECT 1 FROM task_runs WHERE project_id = ? AND task_type IN ('explore', 'bootstrap') AND status IN ('queued','running') LIMIT 1", (project_id,)
    ).fetchone() is not None
    active_attempt = conn.execute(
        "SELECT 1 FROM assessment_attempts WHERE project_id = ? AND status = 'active' LIMIT 1", (project_id,)
    ).fetchone() is not None
    # 已计量且等待 bounded retry 的 Attempt 仍是未结算义务。
    quiescent = not active_explore and not active_attempt and running == 0
    phase = progress.get("phase")
    if phase == "mapping":
        mapped = conn.execute(
            "SELECT 1 FROM facts f JOIN task_runs t ON t.id = f.task_run_id AND t.project_id = f.project_id WHERE f.project_id = ? AND t.task_type = 'bootstrap' LIMIT 1", (project_id,)
        ).fetchone()
        if mapped and not active_explore:
            _set_phase(conn, project_id, "baseline")
            phase = "baseline"
    if phase == "baseline" and quiescent:
        if _baseline_accounted(conn, project_id):
            _abandon_unclaimed_intents(conn, project_id, reason="baseline_accounted")
            _set_phase(conn, project_id, "depth_review")
    if phase == "deep" and quiescent:
        open_work = conn.execute(
            "SELECT 1 FROM intents WHERE project_id = ? AND to_fact_id IS NULL AND concluded_at IS NULL LIMIT 1", (project_id,)
        ).fetchone()
        accounted = _baseline_accounted(conn, project_id)
        if open_work is None and progress.get("depth_review_fact_id") and accounted:
            _set_phase(conn, project_id, "finalizing")
    return None


def require_assessment_completion(conn, project_id: str, *, task_run_id: str | None = None) -> None:
    project = get_project_or_404(conn, project_id)
    policy = policy_from_project_row(project)
    if not policy.phased:
        return
    advance_assessment_phase(conn, project_id)
    project = get_project_or_404(conn, project_id)
    progress = assessment_progress(project)
    if project["status"] != "active":
        raise HTTPException(409, "assessment_stopped_not_completed")
    if not progress.get("depth_review_fact_id") or progress.get("phase") != "finalizing":
        raise HTTPException(409, "assessment_depth_review_or_deep_work_pending")
    if not _baseline_accounted(conn, project_id):
        raise HTTPException(409, "assessment_baseline_unresolved")
    if assessment_has_running_work(conn, project_id, exclude_task_run_id=task_run_id):
        raise HTTPException(409, "assessment_tasks_not_quiescent")


def prepare_structured_completion(
    conn: sqlite3.Connection,
    project_id: str,
) -> CoverageCheckpoint | None:
    """完成请求前刷新硬限制；仍有运行任务时只记录 stop reason，不跳过其 Cell。"""
    project = get_project_or_404(conn, project_id)
    if not policy_from_project_row(project).structured:
        return None
    advance_structured_convergence(conn, project_id)
    return structured_coverage_checkpoint(conn, project_id)
