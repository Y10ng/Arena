"""v40 项目授权 epoch、显式重新授权与追加式审计回归。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

from fastapi import HTTPException
import pytest
import yaml

from cairn.server import db, migrations
from cairn.server.routers.export import _export_timeline, _export_yaml
from cairn.server.schemas.projects import (
    CreateTargetRequest,
    ReauthorizeProjectRequest,
    UpdateTargetRequest,
)
from cairn.server.services import project_authorization, project_targets
from cairn.server.services.project_records import project_lifecycle_actions


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _seed_v39_project(conn: sqlite3.Connection) -> None:
    snapshot = {
        "key": "default-copy-v2",
        "features": {
            "execution_contract_version": 1,
            "execution_engine": "pi",
            "assessment_protocol": "default-copy-v1",
            "assessment_contract_version": 2,
            "exploration_limit": "compact",
            "verification_reserve": 2,
        },
    }
    conn.execute(
        """
        INSERT INTO projects (
            id, title, status, created_at, tenant_id, scenario_key,
            scenario_snapshot_json, assessment_protocol,
            assessment_progress_json, explore_budget, explore_deadline,
            explore_attempts_started, verification_attempts_started,
            execution_generation
        ) VALUES (
            'p', 'synthetic', 'paused', '2026-09-18T00:00:00Z',
            'tenant_legacy', 'default-copy-v2', ?, 'default-copy-v1', ?,
            16, '2026-09-18T02:00:00Z', 3, 1, 4
        )
        """,
        (json.dumps(snapshot), json.dumps({"version": 1, "phase": "baseline"})),
    )
    for fact_id in ("origin", "goal"):
        conn.execute(
            "INSERT INTO facts (id, project_id, description, creator, created_at) "
            "VALUES (?, 'p', ?, 'system', '2026-09-18T00:00:00Z')",
            (fact_id, fact_id),
        )
    conn.execute(
        """
        INSERT INTO targets (
            id, project_id, value, type, status, source_fact_id,
            discovered_by_run_id, created_at
        ) VALUES (
            'target-1', 'p', 'https://example.test/app', 'url', 'seed',
            'origin', NULL, '2026-09-18T00:00:00Z'
        )
        """
    )


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch):
    connection = _connection()
    with monkeypatch.context() as patch:
        patch.setattr(
            db,
            "SQLITE_MIGRATIONS",
            tuple(item for item in migrations.SQLITE_MIGRATIONS if item.version <= 39),
        )
        db.run_migrations(connection, "sqlite")
    _seed_v39_project(connection)
    connection.commit()
    db.run_migrations(connection, "sqlite")
    connection.commit()

    @contextmanager
    def get_conn():
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    monkeypatch.setattr(project_authorization, "get_conn", get_conn)
    monkeypatch.setattr(project_targets, "get_conn", get_conn)
    yield connection
    connection.close()


def test_v40_backfills_non_user_legacy_grant_and_is_immutable(conn: sqlite3.Connection) -> None:
    project = conn.execute("SELECT * FROM projects WHERE id = 'p'").fetchone()
    grant = conn.execute(
        "SELECT * FROM project_authorization_grants WHERE project_id = 'p'"
    ).fetchone()

    assert project["authorization_epoch"] == 1
    assert project["authorization_scope_digest"].startswith("authorization-scope:")
    assert project["authorization_window_seconds"] == 7200
    assert grant["authorization_epoch"] == 1
    assert grant["source"] == "legacy_backfill"
    assert grant["actor_type"] == "system"
    assert grant["actor_id"] == "migration-v40"
    assert grant["new_deadline"] == "2026-09-18T02:00:00Z"
    projection = project_authorization.authorization_scope_projection(conn, project)
    assert projection.scope_digest == grant["scope_digest"]
    assert projection.descriptor == json.loads(grant["scope_descriptor_json"])
    assert projection.descriptor["explore_budget"] == 16
    assert projection.descriptor["verification_reserve"] == 2

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE project_authorization_grants SET actor_id = 'changed' "
            "WHERE project_id = 'p' AND authorization_epoch = 1"
        )
    conn.rollback()


def test_expired_project_requires_explicit_reauthorization_and_stays_paused(
    conn: sqlite3.Connection,
) -> None:
    before = conn.execute("SELECT * FROM projects WHERE id = 'p'").fetchone()
    actions = project_lifecycle_actions(conn, before)
    assert actions["resume"] == {
        "allowed": False,
        "reason": "assessment_protocol_historical",
    }
    assert actions["reauthorize"] == {
        "allowed": False,
        "reason": "assessment_protocol_historical",
    }

    request = ReauthorizeProjectRequest(
        confirmed=True,
        expected_authorization_epoch=before["authorization_epoch"],
        expected_scope_digest=project_authorization.authorization_scope_projection(
            conn, before
        ).scope_digest,
    )
    result = project_authorization.reauthorize_project_record(
        "p",
        request,
        actor_id="synthetic-user",
        now="2026-09-19T10:00:00Z",
    )

    assert result.status == "paused"
    assert result.authorization_epoch == 2
    assert result.execution_generation == 5
    assert result.explore_deadline == "2026-09-19T12:00:00Z"
    assert result.explore_attempts_started == 3
    assert result.verification_attempts_started == 1
    rows = conn.execute(
        "SELECT * FROM project_authorization_grants WHERE project_id = 'p' "
        "ORDER BY authorization_epoch"
    ).fetchall()
    assert len(rows) == 2
    assert rows[1]["source"] == "explicit_reauthorization"
    assert rows[1]["actor_type"] == "user"
    assert rows[1]["actor_id"] == "synthetic-user"
    assert rows[1]["previous_deadline"] == "2026-09-18T02:00:00Z"
    assert rows[1]["new_deadline"] == "2026-09-19T12:00:00Z"
    assert json.loads(rows[1]["scope_descriptor_json"])["target_ids"] == ["target-1"]

    with pytest.raises(HTTPException) as stale:
        project_authorization.reauthorize_project_record(
            "p",
            request,
            actor_id="synthetic-user",
            now="2026-09-19T10:00:01Z",
        )
    assert stale.value.status_code == 409
    assert stale.value.detail == "authorization_epoch_mismatch"


def test_scope_change_while_paused_invalidates_resume_until_reauthorized(
    conn: sqlite3.Connection,
) -> None:
    conn.execute(
        "UPDATE projects SET explore_deadline = '2099-01-01T00:00:00Z' WHERE id = 'p'"
    )
    conn.execute(
        "UPDATE targets SET status = 'rejected' WHERE project_id = 'p' AND id = 'target-1'"
    )
    conn.commit()
    project = conn.execute("SELECT * FROM projects WHERE id = 'p'").fetchone()

    actions = project_lifecycle_actions(conn, project)
    assert actions["resume"] == {
        "allowed": False,
        "reason": "assessment_protocol_historical",
    }
    assert actions["reauthorize"] == {
        "allowed": False,
        "reason": "assessment_protocol_historical",
    }


def test_executable_target_scope_can_expand_only_while_paused(
    conn: sqlite3.Connection,
) -> None:
    conn.execute("UPDATE projects SET status = 'active' WHERE id = 'p'")
    conn.commit()

    with pytest.raises(HTTPException) as create_error:
        project_targets.create_target_record(
            "p",
            CreateTargetRequest(
                value="https://new.example.test/",
                type="url",
                status="approved",
            ),
        )
    assert create_error.value.status_code == 409
    assert create_error.value.detail == "project_authorization_scope_change_requires_pause"

    discovered = project_targets.create_target_record(
        "p",
        CreateTargetRequest(
            value="https://review.example.test/",
            type="url",
            status="discovered",
        ),
    )
    with pytest.raises(HTTPException) as promote_error:
        project_targets.update_target_record(
            "p",
            discovered.id,
            UpdateTargetRequest(status="approved"),
        )
    assert promote_error.value.status_code == 409
    assert promote_error.value.detail == "project_authorization_scope_change_requires_pause"

    conn.execute("UPDATE projects SET status = 'paused' WHERE id = 'p'")
    conn.commit()
    promoted = project_targets.update_target_record(
        "p",
        discovered.id,
        UpdateTargetRequest(status="approved"),
    )
    assert promoted.status == "approved"
    project = conn.execute("SELECT * FROM projects WHERE id = 'p'").fetchone()
    assert project_lifecycle_actions(conn, project)["resume"] == {
        "allowed": False,
        "reason": "assessment_protocol_historical",
    }


def test_authorization_grant_delete_is_immutable_but_project_cascade_is_allowed(
    conn: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "DELETE FROM project_authorization_grants "
            "WHERE project_id = 'p' AND authorization_epoch = 1"
        )
    conn.rollback()

    conn.execute("DELETE FROM projects WHERE id = 'p'")
    assert conn.execute(
        "SELECT COUNT(*) AS count FROM project_authorization_grants WHERE project_id = 'p'"
    ).fetchone()["count"] == 0


def test_authorization_grants_are_visible_in_yaml_and_timeline(
    conn: sqlite3.Connection,
) -> None:
    before = conn.execute("SELECT * FROM projects WHERE id = 'p'").fetchone()
    request = ReauthorizeProjectRequest(
        confirmed=True,
        expected_authorization_epoch=before["authorization_epoch"],
        expected_scope_digest=project_authorization.authorization_scope_projection(
            conn, before
        ).scope_digest,
    )
    project_authorization.reauthorize_project_record(
        "p",
        request,
        actor_id="audit-user",
        now="2026-09-19T10:00:00Z",
    )

    exported = yaml.safe_load(_export_yaml(conn, "p"))
    grants = exported["project_authorization_grants"]
    assert [grant["authorization_epoch"] for grant in grants] == [1, 2]
    assert grants[0]["source"] == "legacy_backfill"
    assert grants[0]["actor_type"] == "system"
    assert grants[1]["source"] == "explicit_reauthorization"
    assert grants[1]["actor_type"] == "user"
    assert grants[1]["actor_id"] == "audit-user"
    assert grants[1]["window_seconds"] == 7200
    assert grants[1]["new_deadline"] == "2026-09-19T12:00:00Z"
    assert grants[1]["execution_generation"] == 5
    assert grants[1]["scope_descriptor"]["target_ids"] == ["target-1"]
    assert grants[1]["scope_digest"].startswith("authorization-scope:")

    timeline = _export_timeline(conn, "p")
    assert timeline.count("PROJECT AUTHORIZATION BACKFILLED") == 1
    assert timeline.count("PROJECT REAUTHORIZED") == 1
    assert "actor: user:audit-user" in timeline
    assert "window_seconds: 7200" in timeline
    assert "execution_generation: 5" in timeline
