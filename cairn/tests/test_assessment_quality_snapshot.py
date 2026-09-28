from __future__ import annotations

import json
import sqlite3

import pytest

from cairn.server import db
from cairn.server.services.assessment_convergence import structured_coverage_checkpoint
from cairn.server.services.assessment_quality_snapshot import (
    build_assessment_quality_snapshot,
)


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(db.SCHEMA)
    db._ensure_schema_migrations(conn)
    return conn


def seed_project(
    conn: sqlite3.Connection,
    project_id: str = "p1",
    *,
    quality_gate_version: int = 1,
) -> None:
    snapshot = {
        "key": "default-copy-v2",
        "features": {
            "surface_manifest_version": 1,
            "minimum_coverage_version": 1,
            "quality_gate_version": quality_gate_version,
        },
    }
    conn.execute(
        """
        INSERT INTO projects (
            id, title, status, created_at, scenario_key, assessment_protocol,
            scenario_snapshot_json, explore_budget, explore_deadline
        ) VALUES (?, 'Default Copy', 'active', ?, 'default-copy-v2',
                  'default-copy-v1', ?, 40, ?)
        """,
        (
            project_id,
            "2026-08-17T00:00:00Z",
            json.dumps(snapshot),
            "2099-01-01T00:00:00Z",
        ),
    )


def seed_surface_and_cell(
    conn: sqlite3.Connection,
    *,
    project_id: str = "p1",
    status: str = "pending",
    result: str | None = None,
    requirement: str = "required",
    fact_id: str | None = None,
    skip_reason: str = "",
    cooldown_until: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO surface_manifest_entries (
            entry_id, project_id, surface_id, surface_kind, surface_key,
            description, traits_json, evidence_json, source_intent_id,
            source_fact_id, source_task_run_id, observation_fingerprint,
            manifest_version, status, created_at
        ) VALUES ('sm1', ?, 'surface:1', 'endpoint', 'GET https://example.test/a',
                  'endpoint', '{}', '[]', NULL, NULL, NULL,
                  'observation:1', 1, 'observed', ?)
        """,
        (project_id, "2026-08-17T00:00:00Z"),
    )
    conn.execute(
        """
        INSERT INTO coverage_slots (
            project_id, slot_key, kind, description, status, tier,
            resulting_fact_id, created_at, updated_at, surface_key,
            surface_kind, test_family, applicability_basis,
            completion_criteria, result, skip_reason, surface_id,
            coverage_requirement, coverage_rule_id, blocker_fingerprint,
            blocker_strategy_key, blocker_cooldown_until
        ) VALUES (?, 'cell:1', 'endpoint', 'authorization', ?, 'core', ?, ?, ?,
                  'GET https://example.test/a', 'endpoint', 'authorization',
                  '对象标识受角色约束', '比较不同角色访问', ?, ?, 'surface:1', ?,
                  'v1.object-role-boundary', ?, 'strategy:1', ?)
        """,
        (
            project_id,
            status,
            fact_id,
            "2026-08-17T00:00:00Z",
            "2026-08-17T00:00:00Z",
            result,
            skip_reason,
            requirement,
            "blocker:1" if result == "temporarily_blocked" else None,
            cooldown_until,
        ),
    )


def seed_finding(
    conn: sqlite3.Connection,
    *,
    project_id: str = "p1",
    vulnerability_id: str = "v1",
    observation_ids: tuple[str, ...] = ("o1",),
    status: str = "new",
) -> None:
    conn.execute(
        """
        INSERT INTO vulnerabilities (
            id, project_id, title, severity, confidence, status, created_at,
            updated_at
        ) VALUES (?, ?, 'IDOR', 'high', 0.8, ?, ?, ?)
        """,
        (
            vulnerability_id,
            project_id,
            status,
            "2026-08-17T00:02:00Z",
            "2026-08-17T00:02:00Z",
        ),
    )
    for index, observation_id in enumerate(observation_ids):
        conn.execute(
            """
            INSERT INTO vulnerability_observations (
                id, project_id, vulnerability_id, identity_json, severity,
                confidence, observed_at
            ) VALUES (?, ?, ?, '{}', 'high', 0.8, ?)
            """,
            (
                observation_id,
                project_id,
                vulnerability_id,
                f"2026-08-17T00:0{index + 3}:00Z",
            ),
        )


def seed_verification(
    conn: sqlite3.Connection,
    observation_id: str,
    outcome: str,
    *,
    project_id: str = "p1",
    vulnerability_id: str = "v1",
    suffix: str = "1",
) -> None:
    intent_id = f"verify-{suffix}"
    run_id = f"verify-run-{suffix}"
    fact_id = f"verify-fact-{suffix}"
    conn.execute(
        """
        INSERT INTO intents (
            id, project_id, description, creator, created_at,
            assessment_role, target_observation_id, verifier_schedule_key
        ) VALUES (?, ?, 'verify', 'system', ?, 'verifier', ?, ?)
        """,
        (
            intent_id,
            project_id,
            "2026-08-17T00:05:00Z",
            observation_id,
            observation_id,
        ),
    )
    conn.execute(
        """
        INSERT INTO task_runs (
            id, project_id, task_type, worker_name, worker_type, status,
            started_at, ended_at
        ) VALUES (?, ?, 'explore', ?, 'claudecode', 'succeeded', ?, ?)
        """,
        (
            run_id,
            project_id,
            f"worker-{suffix}",
            "2026-08-17T00:05:00Z",
            "2026-08-17T00:06:00Z",
        ),
    )
    conn.execute(
        """
        INSERT INTO facts (
            id, project_id, description, creator, task_run_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            fact_id,
            project_id,
            outcome,
            f"worker-{suffix}",
            run_id,
            "2026-08-17T00:06:00Z",
        ),
    )
    conn.execute(
        """
        INSERT INTO vulnerability_verifications (
            id, project_id, vulnerability_id, observation_id, intent_id,
            fact_id, verifier_task_run_id, verifier_worker,
            discovery_task_run_id, discovery_worker, independence_level,
            outcome, evidence_json, notes, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'discovery-run', 'discoverer',
                  'different_worker', ?, '[]', '', ?)
        """,
        (
            f"verification-{suffix}",
            project_id,
            vulnerability_id,
            observation_id,
            intent_id,
            fact_id,
            run_id,
            f"worker-{suffix}",
            outcome,
            "2026-08-17T00:06:00Z",
        ),
    )


def seed_fact_evidence(conn: sqlite3.Connection, project_id: str = "p1") -> None:
    conn.execute(
        """
        INSERT INTO task_runs (
            id, project_id, task_type, worker_name, worker_type, status,
            started_at, ended_at
        ) VALUES ('tr1', ?, 'explore', 'w1', 'claudecode', 'succeeded', ?, ?)
        """,
        (project_id, "2026-08-17T00:01:00Z", "2026-08-17T00:02:00Z"),
    )
    conn.execute(
        """
        INSERT INTO facts (
            id, project_id, description, creator, task_run_id, created_at
        ) VALUES ('f1', ?, 'tested', 'agent', 'tr1', ?)
        """,
        (project_id, "2026-08-17T00:02:00Z"),
    )
    conn.execute(
        """
        INSERT INTO artifacts (
            id, project_id, task_run_id, kind, category, uri, sha256, size,
            created_at
        ) VALUES ('a1', ?, 'tr1', 'packet', 'evidence', 'artifact://a1',
                  'digest', 1, ?)
        """,
        (project_id, "2026-08-17T00:02:00Z"),
    )
    conn.execute(
        """
        INSERT INTO fact_evidence (
            project_id, fact_id, artifact_id, pointer, created_at
        ) VALUES (?, 'f1', 'a1', 'exchange', ?)
        """,
        (project_id, "2026-08-17T00:02:00Z"),
    )


def test_quality_snapshot_is_not_applicable_to_legacy_scenarios() -> None:
    conn = make_conn()
    try:
        conn.execute(
            """
            INSERT INTO projects (id, title, status, created_at, scenario_key)
            VALUES ('legacy', 'Legacy', 'active', ?, 'pentest')
            """,
            ("2026-08-17T00:00:00Z",),
        )
        assert build_assessment_quality_snapshot(conn, "legacy") is None
    finally:
        conn.close()


def test_quality_snapshot_is_disabled_without_frozen_version() -> None:
    conn = make_conn()
    try:
        seed_project(conn, quality_gate_version=0)
        assert build_assessment_quality_snapshot(conn, "p1") is None
    finally:
        conn.close()


def test_quality_snapshot_distinguishes_qualified_and_administrative_terminal() -> None:
    conn = make_conn()
    try:
        seed_project(conn)
        seed_fact_evidence(conn)
        seed_surface_and_cell(
            conn,
            status="explored",
            result="tested_no_finding",
            fact_id="f1",
        )

        snapshot = build_assessment_quality_snapshot(conn, "p1")

        assert snapshot is not None
        assert snapshot.required.qualified_count == 1
        assert snapshot.required.unqualified_count == 0
        assert snapshot.required.unresolved_count == 0
        assert snapshot.residual_risks == ()

        conn.execute(
            """
            UPDATE coverage_slots
            SET status = 'skipped', result = NULL, resulting_fact_id = NULL,
                skip_reason = 'budget_skipped'
            WHERE project_id = 'p1' AND slot_key = 'cell:1'
            """
        )
        snapshot = build_assessment_quality_snapshot(conn, "p1")

        assert snapshot is not None
        assert snapshot.required.qualified_count == 0
        assert snapshot.required.unqualified_count == 1
        assert "required_cell_administratively_skipped" in snapshot.residual_risks
    finally:
        conn.close()


def test_shadow_quality_does_not_replace_current_completion_checkpoint() -> None:
    conn = make_conn()
    try:
        seed_project(conn)
        seed_surface_and_cell(
            conn,
            status="skipped",
            skip_reason="budget_skipped",
        )

        checkpoint = structured_coverage_checkpoint(conn, "p1")
        snapshot = build_assessment_quality_snapshot(conn, "p1")

        assert checkpoint.complete is True
        assert snapshot is not None
        assert snapshot.required.unqualified_count == 1
        assert snapshot.status == "observing"
    finally:
        conn.close()


def test_quality_snapshot_tracks_attempt_retries_blocker_and_pending_verifier() -> None:
    conn = make_conn()
    try:
        seed_project(conn)
        seed_surface_and_cell(
            conn,
            status="pending",
            result="temporarily_blocked",
            skip_reason="rate limited",
            cooldown_until="2099-01-01T00:00:00Z",
        )
        conn.execute(
            """
            INSERT INTO intents (
                id, project_id, description, creator, created_at
            ) VALUES ('i1', 'p1', 'test', 'agent', ?)
            """,
            ("2026-08-17T00:00:00Z",),
        )
        conn.execute(
            """
            INSERT INTO intents (
                id, project_id, description, creator, created_at
            ) VALUES ('i2', 'p1', 'queued test', 'agent', ?)
            """,
            ("2026-08-17T00:00:00Z",),
        )
        conn.execute(
            """
            INSERT INTO assessment_attempts (
                id, project_id, intent_id, slot_key, charged, execution_count,
                status, strategy_key, started_at
            ) VALUES ('aa1', 'p1', 'i1', 'cell:1', 1, 3, 'active',
                      'strategy:1', ?),
                     ('aa2', 'p1', 'i2', NULL, 1, 0, 'active',
                      'strategy:queued', ?)
            """,
            ("2026-08-17T00:01:00Z", "2026-08-17T00:01:00Z"),
        )
        conn.execute(
            """
            INSERT INTO vulnerabilities (
                id, project_id, title, severity, confidence, status, created_at,
                updated_at
            ) VALUES ('v1', 'p1', 'IDOR', 'high', 0.8, 'new', ?, ?)
            """,
            ("2026-08-17T00:02:00Z", "2026-08-17T00:02:00Z"),
        )

        snapshot = build_assessment_quality_snapshot(conn, "p1")

        assert snapshot is not None
        assert snapshot.charged_attempt_count == 2
        assert snapshot.runtime_execution_count == 3
        assert snapshot.runtime_retry_count == 2
        assert snapshot.active_blocker_count == 1
        assert snapshot.active_cooldown_count == 1
        assert snapshot.finding_count == 1
        assert snapshot.pending_verifier_count == 1
        assert snapshot.independently_verified_finding_count == 0
        assert "active_target_blocker" in snapshot.residual_risks
        assert "finding_pending_independent_verification" in snapshot.residual_risks
    finally:
        conn.close()


def test_quality_snapshot_accepts_only_conflict_free_confirmation() -> None:
    conn = make_conn()
    try:
        seed_project(conn)
        seed_finding(conn)
        seed_verification(conn, "o1", "confirmed")

        snapshot = build_assessment_quality_snapshot(conn, "p1")

        assert snapshot is not None
        assert snapshot.observation_count == 1
        assert snapshot.independently_verified_finding_count == 1
        assert snapshot.pending_verifier_count == 0
        assert not any(
            risk.startswith("finding_independent_verification_")
            for risk in snapshot.residual_risks
        )
    finally:
        conn.close()


def test_quality_snapshot_keeps_not_reproduced_finding_in_observing_state() -> None:
    conn = make_conn()
    try:
        seed_project(conn)
        seed_finding(conn)
        seed_verification(conn, "o1", "not_reproduced")

        snapshot = build_assessment_quality_snapshot(conn, "p1")

        assert snapshot is not None
        assert snapshot.independently_verified_finding_count == 0
        assert snapshot.pending_verifier_count == 0
        assert snapshot.status == "observing"
        assert (
            "finding_independent_verification_not_reproduced"
            in snapshot.residual_risks
        )
    finally:
        conn.close()


@pytest.mark.parametrize("outcome", ["environment_changed", "insufficient_evidence"])
def test_quality_snapshot_keeps_inconclusive_verification_risk(outcome: str) -> None:
    conn = make_conn()
    try:
        seed_project(conn)
        seed_finding(conn)
        seed_verification(conn, "o1", outcome)

        snapshot = build_assessment_quality_snapshot(conn, "p1")

        assert snapshot is not None
        assert snapshot.independently_verified_finding_count == 0
        assert snapshot.pending_verifier_count == 0
        assert (
            "finding_independent_verification_inconclusive"
            in snapshot.residual_risks
        )
    finally:
        conn.close()


def test_quality_snapshot_surfaces_conflicting_append_only_results() -> None:
    conn = make_conn()
    try:
        seed_project(conn)
        seed_finding(conn)
        seed_verification(conn, "o1", "confirmed", suffix="confirmed")
        conn.execute("DROP INDEX uq_intents_verifier_schedule")
        conn.execute("DROP TRIGGER trg_intents_verifier_schedule_key_insert")
        conn.execute("DROP TRIGGER trg_intents_verifier_schedule_key_update")
        seed_verification(conn, "o1", "not_reproduced", suffix="rejected")

        snapshot = build_assessment_quality_snapshot(conn, "p1")

        assert snapshot is not None
        assert snapshot.independently_verified_finding_count == 0
        assert snapshot.pending_verifier_count == 0
        assert (
            "finding_independent_verification_conflicting"
            in snapshot.residual_risks
        )
    finally:
        conn.close()


def test_quality_snapshot_tracks_each_observation_authoritatively() -> None:
    conn = make_conn()
    try:
        seed_project(conn)
        seed_finding(conn, observation_ids=("o1", "o2"))
        seed_verification(conn, "o1", "confirmed")

        snapshot = build_assessment_quality_snapshot(conn, "p1")

        assert snapshot is not None
        assert snapshot.observation_count == 2
        assert snapshot.independently_verified_finding_count == 1
        assert snapshot.pending_verifier_count == 1
        assert "finding_pending_independent_verification" in snapshot.residual_risks
    finally:
        conn.close()


def test_quality_snapshot_excludes_dismissed_findings_from_verifier_metrics() -> None:
    conn = make_conn()
    try:
        seed_project(conn)
        seed_finding(conn, status="dismissed")
        seed_verification(conn, "o1", "not_reproduced")

        snapshot = build_assessment_quality_snapshot(conn, "p1")

        assert snapshot is not None
        assert snapshot.finding_count == 0
        assert snapshot.observation_count == 0
        assert snapshot.independently_verified_finding_count == 0
        assert snapshot.pending_verifier_count == 0
        assert not any(
            risk.startswith("finding_independent_verification_")
            for risk in snapshot.residual_risks
        )
    finally:
        conn.close()
