"""default-copy-v1 的 Evidence-aware 质量快照读模型。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .assessment_convergence import structured_coverage_checkpoint
from .assessment_policy import policy_from_project_row, row_value
from .ids import get_project_or_404
from .time_utils import parse_utc_timestamp, utcnow
from .vulnerability_chains import build_vulnerability_chain_candidates

_QUALIFIABLE_OUTCOMES = {
    "tested_no_finding",
    "finding",
    "permanently_unreachable",
    "out_of_scope",
    "not_applicable",
}
_PERMANENT_LIMIT_OUTCOMES = {
    "permanently_unreachable",
    "out_of_scope",
    "not_applicable",
}


@dataclass(frozen=True, slots=True)
class CoverageQuality:
    total_count: int
    qualified_count: int
    unqualified_count: int
    unresolved_count: int
    finding_count: int
    tested_no_finding_count: int
    permanent_limitation_count: int
    administrative_skip_count: int


@dataclass(frozen=True, slots=True)
class AssessmentQualitySnapshot:
    version: int
    status: str
    required: CoverageQuality
    optional: CoverageQuality
    manifest_surface_count: int
    missing_required_surface_count: int
    charged_attempt_count: int
    runtime_execution_count: int
    runtime_retry_count: int
    failed_attempt_count: int
    active_blocker_count: int
    active_cooldown_count: int
    finding_count: int
    observation_count: int
    confirmed_finding_count: int
    independently_verified_finding_count: int
    pending_verifier_count: int
    chain_candidate_count: int
    chain_verification_ready_count: int
    confirmed_chain_count: int
    stop_reason: str | None
    residual_risks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _FindingMetrics:
    finding_count: int
    observation_count: int
    confirmed_finding_count: int
    independently_verified_finding_count: int
    pending_verifier_count: int
    not_reproduced_count: int
    inconclusive_count: int
    conflicting_count: int


@dataclass(slots=True)
class _CoverageAccumulator:
    total_count: int = 0
    qualified_count: int = 0
    unqualified_count: int = 0
    unresolved_count: int = 0
    finding_count: int = 0
    tested_no_finding_count: int = 0
    permanent_limitation_count: int = 0
    administrative_skip_count: int = 0

    def freeze(self) -> CoverageQuality:
        return CoverageQuality(
            total_count=self.total_count,
            qualified_count=self.qualified_count,
            unqualified_count=self.unqualified_count,
            unresolved_count=self.unresolved_count,
            finding_count=self.finding_count,
            tested_no_finding_count=self.tested_no_finding_count,
            permanent_limitation_count=self.permanent_limitation_count,
            administrative_skip_count=self.administrative_skip_count,
        )


def _cell_has_current_run_evidence(
    conn: sqlite3.Connection,
    project_id: str,
    fact_id: str | None,
) -> bool:
    if not fact_id:
        return False
    row = conn.execute(
        """
        SELECT 1
        FROM facts f
        JOIN fact_evidence fe
          ON fe.project_id = f.project_id AND fe.fact_id = f.id
        JOIN artifacts a
          ON a.project_id = fe.project_id AND a.id = fe.artifact_id
        WHERE f.project_id = ? AND f.id = ?
          AND f.task_run_id IS NOT NULL
          AND a.task_run_id = f.task_run_id
        LIMIT 1
        """,
        (project_id, fact_id),
    ).fetchone()
    return row is not None


def _coverage_quality(
    conn: sqlite3.Connection,
    project_id: str,
) -> tuple[CoverageQuality, CoverageQuality]:
    accumulators = {
        "required": _CoverageAccumulator(),
        "optional": _CoverageAccumulator(),
    }
    rows = conn.execute(
        """
        SELECT coverage_requirement, status, result, resulting_fact_id
        FROM coverage_slots
        WHERE project_id = ? AND surface_key IS NOT NULL
        ORDER BY slot_key
        """,
        (project_id,),
    ).fetchall()
    for row in rows:
        requirement = str(row["coverage_requirement"] or "optional")
        accumulator = accumulators.get(requirement, accumulators["optional"])
        accumulator.total_count += 1
        status = str(row["status"] or "")
        outcome = str(row["result"] or "")
        terminal = status in {"explored", "verified", "skipped"}
        if not terminal:
            accumulator.unresolved_count += 1
            continue
        if outcome == "finding":
            accumulator.finding_count += 1
        elif outcome == "tested_no_finding":
            accumulator.tested_no_finding_count += 1
        elif outcome in _PERMANENT_LIMIT_OUTCOMES:
            accumulator.permanent_limitation_count += 1
        if not outcome:
            accumulator.administrative_skip_count += 1
            accumulator.unqualified_count += 1
            continue
        qualified = outcome in _QUALIFIABLE_OUTCOMES and _cell_has_current_run_evidence(
            conn,
            project_id,
            row["resulting_fact_id"],
        )
        if qualified:
            accumulator.qualified_count += 1
        else:
            accumulator.unqualified_count += 1
    return accumulators["required"].freeze(), accumulators["optional"].freeze()


def _attempt_metrics(
    conn: sqlite3.Connection,
    project_id: str,
) -> tuple[int, int, int, int]:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN charged = 1 THEN 1 ELSE 0 END), 0)
                   AS charged_count,
               COALESCE(SUM(execution_count), 0) AS execution_count,
               COALESCE(SUM(
                   CASE WHEN execution_count > 1 THEN execution_count - 1 ELSE 0 END
               ), 0) AS retry_count,
               COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0)
                   AS failed_count
        FROM assessment_attempts
        WHERE project_id = ?
        """,
        (project_id,),
    ).fetchone()
    return (
        int(row["charged_count"] or 0),
        int(row["execution_count"] or 0),
        int(row["retry_count"] or 0),
        int(row["failed_count"] or 0),
    )


def _blocker_metrics(
    conn: sqlite3.Connection,
    project_id: str,
) -> tuple[int, int]:
    rows = conn.execute(
        """
        SELECT blocker_cooldown_until
        FROM coverage_slots
        WHERE project_id = ? AND result = 'temporarily_blocked'
          AND status IN ('pending', 'exploring')
        """,
        (project_id,),
    ).fetchall()
    now = parse_utc_timestamp(utcnow())
    cooldown_count = 0
    for row in rows:
        cooldown = parse_utc_timestamp(row["blocker_cooldown_until"])
        if cooldown is not None and now is not None and cooldown > now:
            cooldown_count += 1
    return len(rows), cooldown_count


def _verification_state(outcomes: set[str]) -> str:
    """按单个 Observation 的 append-only 结果派生权威状态。"""
    if not outcomes:
        return "pending"
    if outcomes == {"confirmed"}:
        return "confirmed"
    if outcomes == {"not_reproduced"}:
        return "not_reproduced"
    if outcomes <= {"environment_changed", "insufficient_evidence"}:
        return "inconclusive"
    return "conflicting"


def _finding_metrics(
    conn: sqlite3.Connection,
    project_id: str,
) -> _FindingMetrics:
    vulnerabilities = conn.execute(
        """
        SELECT id, status
        FROM vulnerabilities
        WHERE project_id = ? AND status != 'dismissed'
        ORDER BY id
        """,
        (project_id,),
    ).fetchall()
    vulnerability_ids = {str(row["id"]) for row in vulnerabilities}
    observations = conn.execute(
        """
        SELECT o.id, o.vulnerability_id
        FROM vulnerability_observations o
        JOIN vulnerabilities v
          ON v.project_id = o.project_id AND v.id = o.vulnerability_id
        WHERE o.project_id = ? AND v.status != 'dismissed'
        ORDER BY o.observed_at, o.id
        """,
        (project_id,),
    ).fetchall()
    outcomes_by_observation: dict[str, set[str]] = {
        str(row["id"]): set() for row in observations
    }
    verification_rows = conn.execute(
        """
        SELECT vv.observation_id, vv.outcome
        FROM vulnerability_verifications vv
        JOIN vulnerability_observations o
          ON o.project_id = vv.project_id AND o.id = vv.observation_id
        JOIN vulnerabilities v
          ON v.project_id = o.project_id AND v.id = o.vulnerability_id
        WHERE vv.project_id = ? AND v.status != 'dismissed'
          AND vv.independence_level IN (
              'different_worker', 'same_worker_new_session'
          )
        ORDER BY vv.created_at, vv.id
        """,
        (project_id,),
    ).fetchall()
    for row in verification_rows:
        observation_id = str(row["observation_id"])
        if observation_id in outcomes_by_observation:
            outcomes_by_observation[observation_id].add(str(row["outcome"]))

    observations_by_vulnerability: dict[str, int] = {}
    independently_verified: set[str] = set()
    state_counts = {
        "pending": 0,
        "not_reproduced": 0,
        "inconclusive": 0,
        "conflicting": 0,
    }
    for row in observations:
        vulnerability_id = str(row["vulnerability_id"])
        observations_by_vulnerability[vulnerability_id] = (
            observations_by_vulnerability.get(vulnerability_id, 0) + 1
        )
        state = _verification_state(outcomes_by_observation[str(row["id"])])
        if state == "confirmed":
            independently_verified.add(vulnerability_id)
        else:
            state_counts[state] += 1

    # 尚无 Observation 的 finding 仍需要独立验证，不能因缺少子记录而消失。
    state_counts["pending"] += len(
        vulnerability_ids - observations_by_vulnerability.keys()
    )
    return _FindingMetrics(
        finding_count=len(vulnerabilities),
        observation_count=len(observations),
        confirmed_finding_count=sum(
            1 for row in vulnerabilities if row["status"] == "confirmed"
        ),
        independently_verified_finding_count=len(independently_verified),
        pending_verifier_count=state_counts["pending"],
        not_reproduced_count=state_counts["not_reproduced"],
        inconclusive_count=state_counts["inconclusive"],
        conflicting_count=state_counts["conflicting"],
    )


def build_assessment_quality_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
) -> AssessmentQualitySnapshot | None:
    """从已提交数据派生 shadow 质量视图，不参与当前完成门决策。"""
    project = get_project_or_404(conn, project_id)
    policy = policy_from_project_row(project)
    if not policy.quality_snapshot_enabled:
        return None

    checkpoint = structured_coverage_checkpoint(conn, project_id)
    required, optional = _coverage_quality(conn, project_id)
    charged, executions, retries, failed = _attempt_metrics(conn, project_id)
    active_blockers, active_cooldowns = _blocker_metrics(conn, project_id)
    finding_metrics = _finding_metrics(conn, project_id)
    chain_candidates = build_vulnerability_chain_candidates(conn, project_id)
    chain_verification_ready = sum(
        1 for candidate in chain_candidates if candidate.ready_for_chain_verification
    )
    missing_required_surfaces = max(
        0,
        checkpoint.manifest_surface_count - checkpoint.required_surface_count,
    )
    risks: set[str] = set()
    if checkpoint.manifest_surface_count == 0 and not checkpoint.scope_disposition_valid:
        risks.add("coverage_scope_disposition_missing")
    if missing_required_surfaces:
        risks.add("manifest_surface_without_required_cell")
    if required.unresolved_count:
        risks.add("required_cell_unresolved")
    if required.unqualified_count:
        risks.add("required_cell_unqualified")
    if required.administrative_skip_count:
        risks.add("required_cell_administratively_skipped")
    if required.permanent_limitation_count:
        risks.add("permanent_limitation")
    if active_blockers:
        risks.add("active_target_blocker")
    if failed:
        risks.add("failed_assessment_attempt")
    if finding_metrics.pending_verifier_count:
        risks.add("finding_pending_independent_verification")
    if finding_metrics.not_reproduced_count:
        risks.add("finding_independent_verification_not_reproduced")
    if finding_metrics.inconclusive_count:
        risks.add("finding_independent_verification_inconclusive")
    if finding_metrics.conflicting_count:
        risks.add("finding_independent_verification_conflicting")
    stop_reason = row_value(project, "coverage_stop_reason")
    if stop_reason in {"budget_exhausted", "deadline_exceeded", "saturated"}:
        risks.add(f"coverage_stopped:{stop_reason}")

    status = "ready" if not risks else "observing"
    return AssessmentQualitySnapshot(
        version=policy.quality_gate_version,
        status=status,
        required=required,
        optional=optional,
        manifest_surface_count=checkpoint.manifest_surface_count,
        missing_required_surface_count=missing_required_surfaces,
        charged_attempt_count=charged,
        runtime_execution_count=executions,
        runtime_retry_count=retries,
        failed_attempt_count=failed,
        active_blocker_count=active_blockers,
        active_cooldown_count=active_cooldowns,
        finding_count=finding_metrics.finding_count,
        observation_count=finding_metrics.observation_count,
        confirmed_finding_count=finding_metrics.confirmed_finding_count,
        independently_verified_finding_count=(
            finding_metrics.independently_verified_finding_count
        ),
        pending_verifier_count=finding_metrics.pending_verifier_count,
        chain_candidate_count=len(chain_candidates),
        chain_verification_ready_count=chain_verification_ready,
        # v1 只生成 soft candidate；没有独立 chain verifier 记录时不得宣称 confirmed。
        confirmed_chain_count=0,
        stop_reason=str(stop_reason) if stop_reason else None,
        residual_risks=tuple(sorted(risks)),
    )
