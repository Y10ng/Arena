"""没有 TaskRun 的项目容器也必须获得持久停止确认才能继续。"""

from contextlib import contextmanager
import json
import sqlite3

import pytest
from fastapi import HTTPException

from cairn.server import db
from cairn.server.services import project_container_pause, project_records, project_runtime_diagnostics
from cairn.server.services.time_utils import utcnow


@pytest.fixture
def pause_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    db.run_migrations(conn, "sqlite")
    conn.execute(
        """INSERT INTO projects
           (id, title, status, created_at, scenario_key, execution_generation,
            assessment_protocol, scenario_snapshot_json, explore_budget)
           VALUES ('p', 'unfinished', 'active', ?, 'default', 0,
                   'goal-driven-v1', ?, 8)""",
        (utcnow(), json.dumps({"features": {
            "execution_contract_version": 1, "execution_engine": "pi",
            "assessment_contract_version": 2, "outcome_contract_version": 2,
            "verifier_contract_version": 1,
        }})),
    )
    conn.commit()

    @contextmanager
    def same_connection():
        yield conn

    monkeypatch.setattr(project_container_pause, "get_conn", same_connection)
    monkeypatch.setattr(project_records, "get_conn", same_connection)
    yield conn
    conn.close()


def _project(conn):
    return conn.execute("SELECT * FROM projects WHERE id = 'p'").fetchone()


def test_migration_blocks_legacy_no_run_resume_before_owner_inventory(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    with monkeypatch.context() as patch:
        patch.setattr(db, "SQLITE_MIGRATIONS", db.SQLITE_MIGRATIONS[:-1])
        db.run_migrations(conn, "sqlite")
    conn.execute(
        """INSERT INTO projects (id, title, status, created_at, scenario_key,
               execution_generation, assessment_protocol, scenario_snapshot_json, explore_budget)
           VALUES ('p', 'legacy', 'paused', ?, 'default', 1, 'goal-driven-v1', ?, 8)""",
        (utcnow(), json.dumps({"features": {
            "execution_contract_version": 1, "execution_engine": "pi",
            "assessment_contract_version": 2, "outcome_contract_version": 2,
            "verifier_contract_version": 1,
        }})),
    )
    conn.execute(
        "INSERT INTO dispatcher_status (id, status, updated_at) VALUES ('dispatcher-a', 'idle', ?)",
        (utcnow(),),
    )
    conn.commit()
    db.run_migrations(conn, "sqlite")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0
    assert project_container_pause.project_container_pause_draining(conn, _project(conn))

    @contextmanager
    def same_connection():
        yield conn

    monkeypatch.setattr(project_container_pause, "get_conn", same_connection)
    monkeypatch.setattr(project_records, "get_conn", same_connection)
    with pytest.raises(HTTPException) as blocked:
        project_records.project_lifecycle_action("p", "resume")
    assert blocked.value.detail == "project_pause_draining"
    assert project_container_pause.container_activation("p", "dispatcher-a")["state"] == "inventory"
    for current_id in (None, "current-container"):
        with pytest.raises(HTTPException) as unknown:
            project_container_pause.adopt_container_pause(
                "p", "dispatcher-a", 1, current_id, "current-daemon",
            )
        assert unknown.value.detail == "legacy_container_origin_unknown"
    assert project_container_pause.project_container_pause_draining(conn, _project(conn))
    diagnostic = project_runtime_diagnostics.build_project_runtime(conn, _project(conn))
    assert diagnostic.state == "pausing"
    assert diagnostic.code == "container_inventory_unresolved"
    assert diagnostic.actions.resume.allowed is False
    conn.close()


def test_migration_with_no_owner_keeps_unknown_legacy_project_blocked(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    with monkeypatch.context() as patch:
        patch.setattr(db, "SQLITE_MIGRATIONS", db.SQLITE_MIGRATIONS[:-1])
        db.run_migrations(conn, "sqlite")
    conn.execute(
        "INSERT INTO projects (id, title, status, created_at, execution_generation) "
        "VALUES ('p', 'legacy', 'paused', ?, 2)", (utcnow(),),
    )
    conn.commit()
    db.run_migrations(conn, "sqlite")
    row = conn.execute(
        "SELECT dispatcher_id, state, docker_daemon_id FROM project_container_activations WHERE project_id = 'p'",
    ).fetchone()
    assert dict(row) == {
        "dispatcher_id": "__legacy_unknown_owner__", "state": "inventory", "docker_daemon_id": None,
    }
    assert project_container_pause.project_container_pause_draining(conn, _project(conn))
    conn.close()


def test_legacy_inventory_blocks_new_owner_activation(pause_db):
    conn = pause_db
    conn.execute(
        """INSERT INTO project_container_activations
           (project_id, dispatcher_id, activation_generation, activation_token, state, updated_at)
           VALUES ('p', 'legacy-owner', 0, 'legacy-inventory', 'inventory', ?)""",
        (utcnow(),),
    )
    with pytest.raises(HTTPException) as blocked:
        project_container_pause.register_container_activation("p", "different-owner", 0, "new-daemon")
    assert blocked.value.detail == "container_inventory_unresolved"


def test_no_task_run_pause_requires_exact_durable_container_ack(pause_db):
    conn = pause_db
    registration = project_container_pause.register_container_activation("p", "dispatcher-a", 0, "daemon-a")
    assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0
    assert project_records.project_lifecycle_action("p", "pause").execution_generation == 1
    assert project_records.project_pause_draining(conn, _project(conn))
    with pytest.raises(HTTPException) as blocked:
        project_records.project_lifecycle_action("p", "resume")
    assert blocked.value.detail == "project_pause_draining"

    project_container_pause.finish_container_activation(
        "p", "dispatcher-a", registration["activation_token"], "exact-container",
    )
    claim = project_container_pause.claim_container_pause("p", "dispatcher-a", 1, "daemon-a")
    with pytest.raises(HTTPException):
        project_container_pause.confirm_container_pause(
            "p", "dispatcher-a", 1, claim["stop_token"], "replacement", "daemon-a",
        )
    assert project_records.project_pause_draining(conn, _project(conn))
    project_container_pause.confirm_container_pause(
        "p", "dispatcher-a", 1, claim["stop_token"], "exact-container", "daemon-a",
    )
    assert not project_records.project_pause_draining(conn, _project(conn))
    assert project_container_pause.container_activation("p", "dispatcher-a")["state"] == "stopped"


def test_daemon_change_cannot_reuse_activation_or_stop_proof(pause_db):
    conn = pause_db
    registration = project_container_pause.register_container_activation("p", "dispatcher-a", 0, "daemon-a")
    project_container_pause.finish_container_activation(
        "p", "dispatcher-a", registration["activation_token"], "original-container",
    )
    with pytest.raises(HTTPException) as changed:
        project_container_pause.register_container_activation("p", "dispatcher-a", 0, "daemon-b")
    assert changed.value.detail == "docker_daemon_changed"
    project_records.project_lifecycle_action("p", "pause")
    with pytest.raises(HTTPException):
        project_container_pause.claim_container_pause("p", "dispatcher-a", 1, "daemon-b")
    claim = project_container_pause.claim_container_pause("p", "dispatcher-a", 1, "daemon-a")
    with pytest.raises(HTTPException):
        project_container_pause.confirm_container_pause(
            "p", "dispatcher-a", 1, claim["stop_token"], "original-container", "daemon-b",
        )
    assert project_container_pause.project_container_pause_draining(conn, _project(conn))


def test_second_pause_and_other_owner_cannot_reuse_old_stop_proof(pause_db):
    conn = pause_db
    first = project_container_pause.register_container_activation("p", "dispatcher-a", 0, "daemon-a")
    project_container_pause.finish_container_activation("p", "dispatcher-a", first["activation_token"], "container-a")
    other = project_container_pause.register_container_activation("p", "dispatcher-b", 0, "daemon-b")
    project_container_pause.finish_container_activation("p", "dispatcher-b", other["activation_token"], "container-b")
    project_records.project_lifecycle_action("p", "pause")
    a = project_container_pause.claim_container_pause("p", "dispatcher-a", 1, "daemon-a")
    project_container_pause.confirm_container_pause("p", "dispatcher-a", 1, a["stop_token"], "container-a", "daemon-a")
    assert project_records.project_pause_draining(conn, _project(conn))
    b = project_container_pause.claim_container_pause("p", "dispatcher-b", 1, "daemon-b")
    project_container_pause.confirm_container_pause("p", "dispatcher-b", 1, b["stop_token"], "container-b", "daemon-b")
    assert not project_records.project_pause_draining(conn, _project(conn))

    # 模拟经完整 Server 门禁继续；下一次激活即使使用同一容器也必须重新停止。
    conn.execute("UPDATE projects SET status = 'active' WHERE id = 'p'")
    restarted = project_container_pause.register_container_activation("p", "dispatcher-a", 1, "daemon-a")
    project_container_pause.finish_container_activation("p", "dispatcher-a", restarted["activation_token"], "container-a")
    project_records.project_lifecycle_action("p", "pause")
    assert project_records.project_pause_draining(conn, _project(conn))
    with pytest.raises(HTTPException):
        project_container_pause.confirm_container_pause("p", "dispatcher-a", 1, a["stop_token"], "container-a", "daemon-a")
    with pytest.raises(HTTPException):
        project_container_pause.register_container_activation("p", "dispatcher-a", 2, "daemon-a")
    assert project_container_pause.claim_container_pause("p", "dispatcher-a", 2, "daemon-a")["state"] == "stopping"


def test_activation_pending_and_stale_cleanup_never_claim_stop(pause_db):
    conn = pause_db
    registration = project_container_pause.register_container_activation("p", "dispatcher-a", 0, "daemon-a")
    project_records.project_lifecycle_action("p", "pause")
    with pytest.raises(HTTPException) as pending:
        project_container_pause.claim_container_pause("p", "dispatcher-a", 1, "daemon-a")
    assert pending.value.detail == "container_activation_pending"
    assert project_records.project_pause_draining(conn, _project(conn))
    project_container_pause.finish_container_activation("p", "dispatcher-a", registration["activation_token"], None)
    claim = project_container_pause.claim_container_pause("p", "dispatcher-a", 1, "daemon-a")
    project_container_pause.confirm_container_pause("p", "dispatcher-a", 1, claim["stop_token"], None, "daemon-a")
    assert not project_records.project_pause_draining(conn, _project(conn))
    conn.execute("UPDATE projects SET status = 'active' WHERE id = 'p'")
    with pytest.raises(HTTPException):
        project_container_pause.claim_container_pause("p", "dispatcher-a", 1, "daemon-a")
