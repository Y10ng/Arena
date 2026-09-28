"""Reason 失租须原子撤销运行权，迟到续租不能复活旧执行。"""

from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3

from fastapi import HTTPException
import pytest

from cairn.server import db
from cairn.server.auth import RequestPrincipal
from cairn.server.models import CreateIntentRequest, HeartbeatRequest, ReasonClaimRequest
from cairn.server.routers import intents as intents_router
from cairn.server.services import project_reason
from cairn.server.services import projects as projects_service


@pytest.fixture
def reason_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    db.run_migrations(conn, "sqlite")
    snapshot = {
        "key": "default", "prompt_group": "goal-driven-v1",
        "features": {
            "execution_contract_version": 1, "execution_engine": "pi",
            "assessment_protocol": "goal-driven-v1", "assessment_contract_version": 2,
            "outcome_contract_version": 2, "verifier_contract_version": 1,
        },
    }
    conn.execute(
        """INSERT INTO projects (id, title, status, created_at, assessment_protocol,
            scenario_snapshot_json, execution_generation, reason_worker,
            reason_last_heartbeat_at, reason_heartbeat_boot_id, reason_heartbeat_boottime_ns)
            VALUES ('p', 'local test', 'active', '2026-09-28T00:00:00Z',
                    'goal-driven-v1', ?, 3, 'reasoner', '2026-09-28T00:00:00Z',
                    'boot-a', 1000000000)""",
        (json.dumps(snapshot),),
    )
    conn.execute(
        """INSERT INTO facts (id, project_id, description, creator, created_at)
           VALUES ('f1', 'p', 'existing observation', 'tester', '2026-09-28T00:00:00Z')"""
    )
    conn.execute(
        """INSERT INTO task_runs (id, project_id, task_type, worker_name, worker_type,
                status, execution_generation, started_at, runtime_context_json)
            VALUES ('run-reason', 'p', 'reason', 'reasoner', 'pi', 'running', 3,
                '2026-09-28T00:00:00Z', '{"dispatcher_id":"local"}')"""
    )
    conn.commit()

    @contextmanager
    def transaction():
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    monkeypatch.setattr(project_reason, "get_conn", transaction)
    monkeypatch.setattr(intents_router, "get_conn", transaction)
    monkeypatch.setattr(projects_service, "lease_clock", lambda: ("boot-a", 500_000_000_000))
    monkeypatch.setattr(project_reason, "lease_clock", lambda: ("boot-a", 500_000_000_000))
    monkeypatch.setattr(intents_router, "lease_clock", lambda: ("boot-a", 500_000_000_000))
    monkeypatch.setattr(project_reason, "advance_goal_driven_convergence", lambda *_: None)
    monkeypatch.setattr(intents_router, "advance_goal_driven_convergence", lambda *_: None)
    yield conn
    conn.close()


def test_late_heartbeat_commits_matching_reason_run_revocation(reason_db):
    conn = reason_db
    for run_id, generation, worker, task_type in (
        ("old-generation", 2, "reasoner", "reason"),
        ("different-worker", 3, "someone-else", "reason"),
        ("other-task", 3, "reasoner", "explore"),
    ):
        conn.execute(
            """INSERT INTO task_runs (id, project_id, task_type, worker_name, worker_type,
                status, execution_generation) VALUES (?, 'p', ?, ?, 'pi', 'running', ?)""",
            (run_id, task_type, worker, generation),
        )
    conn.commit()

    with pytest.raises(HTTPException) as rejected:
        project_reason.heartbeat_project_reason_record(
            "p", HeartbeatRequest(worker="reasoner", task_run_id="run-reason"),
        )
    assert rejected.value.status_code == 409
    assert conn.execute("SELECT reason_worker FROM projects WHERE id='p'").fetchone()[0] is None
    current = conn.execute(
        "SELECT status, error_type, runtime_context_json FROM task_runs WHERE id='run-reason'"
    ).fetchone()
    assert current["status"] == "failed"
    assert current["error_type"] == "heartbeat_lost"
    context = json.loads(current["runtime_context_json"])
    assert context["dispatcher_id"] == "local"
    assert context["error_type"] == "heartbeat_lost"
    for run_id in ("old-generation", "different-worker", "other-task"):
        assert conn.execute("SELECT status FROM task_runs WHERE id=?", (run_id,)).fetchone()[0] == "running"

    with pytest.raises(HTTPException):
        project_reason.heartbeat_project_reason_record(
            "p", HeartbeatRequest(worker="reasoner", task_run_id="run-reason"),
        )
    assert conn.execute("SELECT status FROM task_runs WHERE id='run-reason'").fetchone()[0] == "failed"


def test_late_claim_does_not_rebind_terminal_reason_run(reason_db, monkeypatch):
    conn = reason_db
    from cairn.server.services import project_authorization

    monkeypatch.setattr(project_authorization, "require_project_authorization_current", lambda *_: None)
    with pytest.raises(HTTPException) as rejected:
        project_reason.claim_project_reason_record(
            "p", ReasonClaimRequest(worker="reasoner", trigger="retry", task_run_id="run-reason"),
        )
    assert rejected.value.status_code == 409
    assert conn.execute("SELECT reason_worker FROM projects WHERE id='p'").fetchone()[0] is None
    assert conn.execute("SELECT status FROM task_runs WHERE id='run-reason'").fetchone()[0] == "failed"


def test_terminal_reason_run_cannot_be_renewed_without_task_id(reason_db):
    conn = reason_db
    conn.execute("UPDATE projects SET reason_heartbeat_boottime_ns = 499000000000 WHERE id='p'")
    conn.execute("UPDATE task_runs SET status = 'failed' WHERE id='run-reason'")
    conn.commit()
    with pytest.raises(HTTPException) as rejected:
        project_reason.heartbeat_project_reason_record("p", HeartbeatRequest(worker="reasoner"))
    assert rejected.value.status_code == 409
    assert conn.execute("SELECT reason_worker FROM projects WHERE id='p'").fetchone()[0] == "reasoner"


def test_create_and_claim_intent_writes_boot_clock_at_creation(reason_db):
    conn = reason_db
    conn.execute("UPDATE projects SET reason_heartbeat_boottime_ns = 499000000000 WHERE id='p'")
    conn.commit()
    created = intents_router.create_intent(
        "p",
        CreateIntentRequest(
            from_=["f1"], description="verify observed boundary", creator="reasoner",
            worker="reasoner", task_run_id="run-reason", submission_index=0,
        ),
        principal=RequestPrincipal(kind="machine", subject_id="dispatcher"),
    )
    lease = conn.execute(
        "SELECT worker, heartbeat_boot_id, heartbeat_boottime_ns FROM intents WHERE id=?",
        (created.id,),
    ).fetchone()
    assert lease["worker"] == "reasoner"
    assert lease["heartbeat_boot_id"] == "boot-a"
    assert lease["heartbeat_boottime_ns"] == 500_000_000_000
