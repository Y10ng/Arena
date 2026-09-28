from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3

import pytest
from fastapi import HTTPException

from cairn.server import db
from cairn.server.models import (
    ConcludeRequest,
    CoverageScopeDispositionInput,
    EvidenceRef,
)
from cairn.server.routers import intents as intents_router
from cairn.server.services.assessment_convergence import structured_coverage_checkpoint
from cairn.server.services.coverage_scope import register_coverage_scope_disposition
from cairn.server.services.surface_manifest import (
    register_discovered_surfaces,
    register_surface_manifest_entry,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(db.SCHEMA)
    db._ensure_schema_migrations(conn)
    return conn


def _seed(conn: sqlite3.Connection, *, task_type: str = "bootstrap") -> None:
    snapshot = json.dumps(
        {
            "features": {
                "execution_contract_version": 1,
                "execution_engine": "pi",
                "assessment_contract_version": 2,
                "surface_manifest_version": 1,
                "minimum_coverage_version": 1,
            }
        }
    )
    conn.execute(
        """
        INSERT INTO projects (
            id, title, status, created_at, scenario_key, assessment_protocol,
            scenario_snapshot_json, assessment_progress_json
        ) VALUES ('dc1', 'Default Copy', 'active', '2026-08-17T00:00:00Z',
                  'default-copy-v2', 'default-copy-v1', ?, '{"version":1,"phase":"mapping"}')
        """,
        (snapshot,),
    )
    conn.executemany(
        """
        INSERT INTO facts (id, project_id, description, creator, created_at)
        VALUES (?, 'dc1', ?, 'system', '2026-08-17T00:00:00Z')
        """,
        [("origin", "origin"), ("goal", "goal"), ("fscope", "scope result")],
    )
    conn.execute(
        """
        INSERT INTO targets (
            id, project_id, value, type, status, created_at
        ) VALUES ('target1', 'dc1', 'https://example.com', 'url', 'approved',
                  '2026-08-17T00:00:00Z')
        """
    )
    conn.execute(
        """
        INSERT INTO intents (
            id, project_id, description, creator, worker, created_at
        ) VALUES ('boot1', 'dc1', 'Bootstrap', 'dispatcher-bootstrap', 'worker1',
                  '2026-08-17T00:00:00Z')
        """
    )
    conn.execute(
        """
        INSERT INTO task_runs (
            id, project_id, intent_id, task_type, worker_name, worker_type,
            status, started_at
        ) VALUES ('tr1', 'dc1', 'boot1', ?, 'worker1', 'pi', 'running',
                  '2026-08-17T00:00:00Z')
        """,
        (task_type,),
    )
    conn.execute(
        """
        INSERT INTO artifacts (
            id, project_id, task_run_id, kind, category, uri, sha256, size,
            created_at, metadata_json
        ) VALUES ('a1', 'dc1', 'tr1', 'http_response', 'evidence',
                  'artifact://dc1/a1-response.txt', 'abc', 3,
                  '2026-08-17T00:00:00Z', '{}')
        """
    )


def _disposition() -> CoverageScopeDispositionInput:
    return CoverageScopeDispositionInput(
        disposition_kind="no_reachable_surfaces",
        target_ids=["target1"],
        reason="The approved target consistently returned an upstream unreachable response.",
        evidence_refs=[EvidenceRef(artifact_id="a1")],
    )


def test_zero_cell_completion_requires_current_evidence_backed_disposition() -> None:
    conn = _conn()
    try:
        _seed(conn)
        assert structured_coverage_checkpoint(conn, "dc1").complete is False

        register_coverage_scope_disposition(
            conn,
            "dc1",
            _disposition(),
            source_intent_id="boot1",
            source_fact_id="fscope",
            source_task_run_id="tr1",
        )

        checkpoint = structured_coverage_checkpoint(conn, "dc1")
        assert checkpoint.scope_disposition_valid is True
        assert checkpoint.complete is True

        conn.execute("UPDATE targets SET status = 'rejected' WHERE id = 'target1'")
        stale = structured_coverage_checkpoint(conn, "dc1")
        assert stale.scope_disposition_valid is False
        assert stale.complete is False
    finally:
        conn.close()


def test_conclude_atomically_registers_scope_disposition_and_fact_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _conn()

    @contextmanager
    def fake_get_conn():
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()

    monkeypatch.setattr(intents_router, "get_conn", fake_get_conn)
    try:
        _seed(conn)
        response = intents_router.conclude(
            "dc1",
            "boot1",
            ConcludeRequest(
                worker="worker1",
                description="No reachable attack surface was observed.",
                task_run_id="tr1",
                coverage_scope_disposition=_disposition(),
            ),
        )

        row = conn.execute(
            """
            SELECT source_fact_id, source_intent_id, source_task_run_id
            FROM coverage_scope_dispositions
            WHERE project_id = 'dc1'
            """
        ).fetchone()
        assert row is not None
        assert row["source_fact_id"] == response.fact.id
        assert row["source_intent_id"] == "boot1"
        assert row["source_task_run_id"] == "tr1"
        evidence = conn.execute(
            """
            SELECT artifact_id FROM fact_evidence
            WHERE project_id = 'dc1' AND fact_id = ?
            """,
            (response.fact.id,),
        ).fetchall()
        assert [item["artifact_id"] for item in evidence] == ["a1"]
        assert structured_coverage_checkpoint(conn, "dc1").complete is True
    finally:
        conn.close()


def test_discovered_targets_remain_review_only_for_scope_disposition() -> None:
    conn = _conn()
    try:
        _seed(conn)
        conn.execute("UPDATE targets SET status = 'discovered' WHERE id = 'target1'")
        disposition = CoverageScopeDispositionInput(
            disposition_kind="no_in_scope_targets",
            target_ids=[],
            reason="The discovered target has not been approved for active testing.",
            evidence_refs=[EvidenceRef(artifact_id="a1")],
        )

        register_coverage_scope_disposition(
            conn,
            "dc1",
            disposition,
            source_intent_id="boot1",
            source_fact_id="fscope",
            source_task_run_id="tr1",
        )

        checkpoint = structured_coverage_checkpoint(conn, "dc1")
        assert checkpoint.scope_disposition_valid is True
        assert checkpoint.complete is True
    finally:
        conn.close()


def test_scope_disposition_requires_bootstrap_current_run_evidence() -> None:
    conn = _conn()
    try:
        _seed(conn, task_type="reason")
        with pytest.raises(HTTPException) as exc_info:
            register_coverage_scope_disposition(
                conn,
                "dc1",
                _disposition(),
                source_intent_id="boot1",
                source_fact_id="fscope",
                source_task_run_id="tr1",
            )
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail == "coverage_scope_disposition_bootstrap_required"
    finally:
        conn.close()


def test_manifest_surface_without_required_cell_is_not_complete() -> None:
    conn = _conn()
    try:
        _seed(conn)
        register_surface_manifest_entry(
            conn,
            "dc1",
            surface_kind="endpoint",
            surface_key="GET https://example.com/",
        )

        checkpoint = structured_coverage_checkpoint(conn, "dc1")
        assert checkpoint.manifest_surface_count == 1
        assert checkpoint.required_surface_count == 0
        assert checkpoint.unresolved_count == 1
        assert checkpoint.complete is False
    finally:
        conn.close()


def test_old_generation_cannot_register_scope_or_surface_business_state() -> None:
    conn = _conn()
    try:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN execution_generation INTEGER NOT NULL DEFAULT 0"
        )
        conn.execute(
            "ALTER TABLE task_runs ADD COLUMN execution_generation INTEGER NOT NULL DEFAULT 0"
        )
        _seed(conn)
        conn.execute("UPDATE projects SET execution_generation = 1 WHERE id = 'dc1'")

        with pytest.raises(HTTPException) as scope_error:
            register_coverage_scope_disposition(
                conn,
                "dc1",
                _disposition(),
                source_intent_id="boot1",
                source_fact_id="fscope",
                source_task_run_id="tr1",
            )
        assert scope_error.value.status_code == 409
        assert scope_error.value.detail == "execution_generation_mismatch"

        with pytest.raises(HTTPException) as surface_error:
            register_discovered_surfaces(
                conn,
                "dc1",
                [
                    {
                        "surface_kind": "endpoint",
                        "surface_key": "GET https://example.com/",
                        "description": "stale observation",
                    }
                ],
                tier="core",
                source_intent_id="boot1",
                source_fact_id="fscope",
                source_task_run_id="tr1",
            )
        assert surface_error.value.status_code == 409
        assert surface_error.value.detail == "execution_generation_mismatch"
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM coverage_scope_dispositions WHERE project_id = 'dc1'"
        ).fetchone()["count"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM surface_manifest_entries WHERE project_id = 'dc1'"
        ).fetchone()["count"] == 0
    finally:
        conn.close()
