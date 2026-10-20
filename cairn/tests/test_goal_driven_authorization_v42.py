"""v42 目标驱动持久化与授权 v2 回归。"""

from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3

from fastapi import HTTPException
import pytest

from cairn.project_authorization_contract import (
    AUTHORIZATION_V2_PROTOCOL,
    build_scope_descriptor_v3,
)
from cairn.server import db, db_schema, migrations
from cairn.server.models import ConcludeRequest
from cairn.server.routers import intents as intents_router
from cairn.server.schemas.projects import CreateProjectRequest, ReauthorizeProjectRequest
from cairn.server.services import project_authorization
from cairn.server.services.assessment_policy import (
    execution_rejection_reason,
    require_result_settlement_contract,
)


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    db.run_migrations(conn, "sqlite")
    conn.commit()
    return conn


def _goal_snapshot() -> str:
    return json.dumps(
        {
            "key": "default-copy-v2",
            "prompt_group": "goal-driven-v1",
            "features": {
                "execution_contract_version": 1,
                "execution_engine": "pi",
                "assessment_protocol": "goal-driven-v1",
                "assessment_contract_version": 2,
                "outcome_contract_version": 2,
                "verifier_contract_version": 1,
                "verification_reserve": 2,
            },
        },
        sort_keys=True,
    )


def _seed_goal_project(
    conn: sqlite3.Connection,
    *,
    project_id: str = "goal-project",
    status: str = "paused",
    authorization_expires_at: str | None = None,
) -> sqlite3.Row:
    conn.execute(
        """
        INSERT INTO projects (
            id, title, status, created_at, scenario_key,
            scenario_snapshot_json, assessment_protocol, explore_budget,
            explore_deadline, authorization_expires_at, execution_generation
        ) VALUES (?, 'goal driven', ?, '2026-09-21T00:00:00Z',
                  'default-copy-v2', ?, 'goal-driven-v1', 10,
                  '2026-09-30T00:00:00Z', ?, 3)
        """,
        (project_id, status, _goal_snapshot(), authorization_expires_at),
    )
    conn.execute(
        """
        INSERT INTO targets (
            id, project_id, value, type, status, created_at
        ) VALUES (?, ?, 'https://example.test/', 'url', 'seed',
                  '2026-09-21T00:00:00Z')
        """,
        (f"{project_id}-seed", project_id),
    )
    conn.execute(
        """
        INSERT INTO targets (
            id, project_id, value, type, status, created_at
        ) VALUES (?, ?, 'https://discovered.example.test/', 'url', 'discovered',
                  '2026-09-21T00:00:01Z')
        """,
        (f"{project_id}-discovered", project_id),
    )
    project_authorization.initialize_project_authorization(
        conn,
        project_id,
        actor_id="creator",
        authorized_at="2026-09-21T00:00:00Z",
    )
    conn.commit()
    return conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()


def test_v42_registry_and_frozen_v40_checksums() -> None:
    assert migrations.SQLITE_MIGRATIONS[-2] is migrations.SQLITE_GOAL_DRIVEN_EXECUTION
    assert migrations.POSTGRES_MIGRATIONS[-2] is migrations.POSTGRES_GOAL_DRIVEN_EXECUTION
    assert (migrations.SQLITE_MIGRATIONS[-2].version, migrations.SQLITE_MIGRATIONS[-2].name) == (
        42,
        "goal-driven-execution-v1",
    )
    assert (migrations.POSTGRES_MIGRATIONS[-2].version, migrations.POSTGRES_MIGRATIONS[-2].name) == (
        42,
        "goal-driven-execution-v1",
    )
    assert migrations.SQLITE_BASELINE.checksum == (
        "487a28b3bdc801b24ab0976e6a5c551c95fe26b0b439b117e861fff380c51f17"
    )
    assert migrations.POSTGRES_BASELINE.checksum == (
        "88453cf001766dd0d841f8da83a75a15d24a3cf538dd36b6a31966a0302be46a"
    )
    assert "authorization_expires_at" not in db_schema.SCHEMA
    assert "depends_on TEXT,\n    priority INTEGER" not in db_schema.SCHEMA
    assert migrations.SQLITE_PROJECT_AUTHORIZATION.checksum == (
        "cb7dc943246678b45c3c60c88c027935fe8c178d56d1f684ac619815b9f04143"
    )
    assert migrations.POSTGRES_PROJECT_AUTHORIZATION.checksum == (
        "22aa14a12da0cc839910afb3a1859b975d96305666dbd458b16c95a6e7d88cb8"
    )
    assert "project_authorization_grants_v2" not in migrations.SQLITE_PROJECT_AUTHORIZATION_V40
    assert "authorization_expires_at" not in migrations.SQLITE_PROJECT_AUTHORIZATION_V40
    assert "project_authorization_grants_v2" not in migrations.POSTGRES_PROJECT_AUTHORIZATION_V40
    assert "authorization_expires_at" not in migrations.POSTGRES_PROJECT_AUTHORIZATION_V40


def test_sqlite_v41_database_upgrades_to_v42_without_rewriting_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        monkeypatch.setattr(
            db,
            "SQLITE_MIGRATIONS",
            tuple(item for item in migrations.SQLITE_MIGRATIONS if item.version <= 41),
        )
        db.run_migrations(conn, "sqlite")
        conn.execute(
            """
            INSERT INTO projects (id, title, status, created_at)
            VALUES ('v41-project', 'v41', 'paused', '2026-09-21T00:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO intents (id, project_id, description, creator, created_at)
            VALUES ('v41-intent', 'v41-project', 'existing work', 'tester',
                    '2026-09-21T00:00:00Z')
            """
        )
        conn.commit()

        migrations.verify_or_apply(
            conn,
            migrations.SQLITE_GOAL_DRIVEN_EXECUTION,
            lambda _conn: None,
        )
        conn.commit()

        assert conn.execute(
            "SELECT priority FROM intents WHERE project_id = 'v41-project'"
        ).fetchone()["priority"] == 1
        project = conn.execute(
            "SELECT title, authorization_expires_at FROM projects "
            "WHERE id = 'v41-project'"
        ).fetchone()
        assert dict(project) == {"title": "v41", "authorization_expires_at": None}
        registry = conn.execute(
            "SELECT name, checksum FROM schema_migrations WHERE version = 42"
        ).fetchone()
        assert dict(registry) == {
            "name": "goal-driven-execution-v1",
            "checksum": migrations.SQLITE_GOAL_DRIVEN_EXECUTION.checksum,
        }
    finally:
        conn.close()


def test_v42_sqlite_schema_adds_priority_expiry_and_immutable_v2_grants() -> None:
    conn = _connection()
    try:
        project_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(projects)")
        }
        intent_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(intents)")
        }
        assert "authorization_expires_at" in project_columns
        assert "priority" in intent_columns
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_intents_goal_dispatch'"
        ).fetchone()

        conn.execute(
            """
            INSERT INTO projects (id, title, status, created_at)
            VALUES ('schema-project', 'schema', 'paused', '2026-09-21T00:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO intents (
                id, project_id, description, creator, created_at
            ) VALUES (
                'schema-intent', 'schema-project', 'work', 'tester',
                '2026-09-21T00:00:00Z'
            )
            """
        )
        assert conn.execute(
            "SELECT priority FROM intents WHERE project_id = 'schema-project'"
        ).fetchone()["priority"] == 1
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE intents SET priority = 3 WHERE project_id = 'schema-project'"
            )
        conn.rollback()

        conn.execute(
            """
            INSERT INTO project_authorization_grants_v2 (
                project_id, authorization_epoch, protocol, source,
                actor_type, actor_id, scope_digest, scope_descriptor_json,
                authorization_expires_at, execution_generation, authorized_at
            ) VALUES (
                'schema-project', 1, ?, 'project_creation', 'user', 'tester',
                ?, '{}', NULL, 0, '2026-09-21T00:00:00Z'
            )
            """,
            (AUTHORIZATION_V2_PROTOCOL, "authorization-scope:" + "a" * 64),
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE project_authorization_grants_v2 SET actor_id = 'changed' "
                "WHERE project_id = 'schema-project'"
            )
    finally:
        conn.close()


def test_fresh_schema_exposes_goal_driven_columns_and_grant_table() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(db.SCHEMA)
        assert "authorization_expires_at" in {
            row["name"] for row in conn.execute("PRAGMA table_info(projects)")
        }
        assert "priority" in {
            row["name"] for row in conn.execute("PRAGMA table_info(intents)")
        }
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'project_authorization_grants_v2'"
        ).fetchone()
    finally:
        conn.close()


def test_postgres_v42_contains_additive_columns_index_and_immutable_grant() -> None:
    sql = migrations.POSTGRES_GOAL_DRIVEN_EXECUTION_V42
    assert "ADD COLUMN IF NOT EXISTS authorization_expires_at TEXT" in sql
    assert "ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 1" in sql
    assert "idx_intents_goal_dispatch" in sql
    assert "CREATE TABLE IF NOT EXISTS project_authorization_grants_v2" in sql
    assert "BEFORE UPDATE OR DELETE ON project_authorization_grants_v2" in sql


def test_v3_scope_binds_only_authorized_target_identity() -> None:
    conn = _connection()
    try:
        project = _seed_goal_project(conn)
        original = project_authorization.authorization_scope_projection(conn, project)
        assert original.descriptor == build_scope_descriptor_v3(
            project_id="goal-project",
            tenant_id="",
            target_digest=original.descriptor["target_scope_digest"],
            target_ids=["goal-project-seed"],
        )

        conn.execute(
            "UPDATE projects SET explore_budget = 999, "
            "explore_deadline = '2026-10-31T00:00:00Z' WHERE id = 'goal-project'"
        )
        conn.execute(
            "UPDATE targets SET value = 'https://ignored.example.test/' "
            "WHERE id = 'goal-project-discovered' AND project_id = 'goal-project'"
        )
        changed_non_scope = conn.execute(
            "SELECT * FROM projects WHERE id = 'goal-project'"
        ).fetchone()
        assert (
            project_authorization.authorization_scope_projection(
                conn, changed_non_scope
            ).scope_digest
            == original.scope_digest
        )

        conn.execute(
            "UPDATE targets SET value = 'https://changed.example.test/' "
            "WHERE id = 'goal-project-seed' AND project_id = 'goal-project'"
        )
        changed_target = conn.execute(
            "SELECT * FROM projects WHERE id = 'goal-project'"
        ).fetchone()
        assert (
            project_authorization.authorization_scope_projection(
                conn, changed_target
            ).scope_digest
            != original.scope_digest
        )
    finally:
        conn.close()


def test_explore_admission_survives_executable_target_status_change() -> None:
    from cairn.server.auth import RequestPrincipal
    from cairn.server.models import CreateTaskRunRequest
    from cairn.server.services.explore_attempts import start_explore_attempt

    conn = _connection()
    try:
        project = _seed_goal_project(conn, status="active")
        original_grant = dict(conn.execute(
            "SELECT * FROM project_authorization_grants_v2 WHERE project_id = 'goal-project'"
        ).fetchone())
        conn.execute(
            "INSERT INTO facts (id, project_id, description, creator, created_at) "
            "VALUES ('f1', 'goal-project', 'accepted bootstrap', 'worker', '2026-09-21T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO intents (id, project_id, description, creator, created_at) "
            "VALUES ('i1', 'goal-project', 'existing reason intent', 'worker', '2026-09-21T00:00:01Z')"
        )
        conn.execute(
            "INSERT INTO intent_sources (project_id, intent_id, fact_id) "
            "VALUES ('goal-project', 'i1', 'f1')"
        )
        conn.execute("UPDATE projects SET explore_deadline = NULL WHERE id = 'goal-project'")
        conn.execute("UPDATE targets SET status = 'approved' WHERE id = 'goal-project-seed'")
        body = CreateTaskRunRequest(
            intent_id="i1", task_type="explore", worker_name="worker", worker_type="pi",
            status="running", expected_execution_generation=3,
            runtime_context={"dispatcher_id": "dispatcher"},
        )
        principal = RequestPrincipal(kind="machine", subject_id="dispatcher")
        run = start_explore_attempt(conn, "goal-project", "i1", body, principal=principal)
        assert run.assessment_attempt_id
        assert conn.execute(
            "SELECT explore_attempts_started FROM projects WHERE id = 'goal-project'"
        ).fetchone()["explore_attempts_started"] == 1
        assert dict(conn.execute(
            "SELECT * FROM project_authorization_grants_v2 WHERE project_id = 'goal-project'"
        ).fetchone()) == original_grant

        for column, value in (("value", "https://changed.test/"), ("type", "domain"), ("status", "excluded")):
            with conn:
                conn.execute(f"UPDATE targets SET {column} = ? WHERE id = 'goal-project-seed'", (value,))
                with pytest.raises(HTTPException) as denied:
                    start_explore_attempt(conn, "goal-project", "i1", body, principal=principal)
                assert denied.value.detail == "project_reauthorization_required"
                conn.execute(
                    "UPDATE targets SET value = 'https://example.test/', type = 'url', status = 'approved' "
                    "WHERE id = 'goal-project-seed'"
                )
        conn.execute("UPDATE targets SET status = 'approved' WHERE id = 'goal-project-discovered'")
        assert not project_authorization.project_authorization_is_current(conn, project)
        conn.execute("UPDATE targets SET status = 'discovered' WHERE id = 'goal-project-discovered'")
        conn.execute("UPDATE targets SET status = 'seed' WHERE id = 'goal-project-seed'")
        assert project_authorization.project_authorization_is_current(conn, project)
        with pytest.raises(HTTPException) as fenced:
            start_explore_attempt(
                conn, "goal-project", "i1",
                body.model_copy(update={"expected_execution_generation": 2}), principal=principal,
            )
        assert fenced.value.detail == "execution_generation_mismatch"
    finally:
        conn.close()


def test_create_project_requires_explicit_null_or_nonblank_authorization_expiry() -> None:
    base = {
        "title": "authorized",
        "origin": "https://example.test/",
        "goal": "verify boundaries",
        "tenant_worker_ids": ["worker-1"],
    }
    unlimited = CreateProjectRequest(
        **base,
        authorization_expires_at=None,
        explore_deadline=" ",
    )
    assert unlimited.authorization_expires_at is None
    assert unlimited.explore_deadline is None

    with pytest.raises(ValueError, match="absolute ISO-8601 timestamp or null"):
        CreateProjectRequest(
            **base,
            authorization_expires_at=" ",
        )


def test_null_expiry_is_unlimited_only_for_v2_and_invalid_expiry_is_not_unlimited() -> None:
    conn = _connection()
    try:
        goal = _seed_goal_project(conn)
        assert project_authorization.project_authorization_is_current(
            conn, goal, now="2099-01-01T00:00:00Z"
        )

        conn.execute(
            "UPDATE projects SET authorization_expires_at = 'not-a-time' "
            "WHERE id = 'goal-project'"
        )
        malformed = conn.execute(
            "SELECT * FROM projects WHERE id = 'goal-project'"
        ).fetchone()
        assert not project_authorization.project_authorization_is_current(
            conn, malformed, now="2026-09-21T00:00:01Z"
        )

        conn.execute(
            """
            INSERT INTO projects (
                id, title, status, created_at, scenario_key,
                scenario_snapshot_json, assessment_protocol, explore_budget,
                explore_deadline, execution_generation
            ) VALUES (
                'legacy-project', 'legacy', 'paused', '2026-09-21T00:00:00Z',
                'default-copy-v2', ?, 'default-copy-v1', 10, NULL, 0
            )
            """,
            (
                json.dumps(
                    {
                        "features": {
                            "assessment_protocol": "default-copy-v1",
                            "assessment_contract_version": 2,
                            "verification_reserve": 2,
                            "exploration_limit": "balanced",
                        }
                    }
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO targets (id, project_id, value, type, status, created_at)
            VALUES ('legacy-seed', 'legacy-project', 'https://legacy.test/',
                    'url', 'seed', '2026-09-21T00:00:00Z')
            """
        )
        legacy = conn.execute(
            "SELECT * FROM projects WHERE id = 'legacy-project'"
        ).fetchone()
        projection = project_authorization.authorization_scope_projection(conn, legacy)
        conn.execute(
            "UPDATE projects SET authorization_epoch = 1, "
            "authorization_scope_digest = ?, authorization_window_seconds = ? "
            "WHERE id = 'legacy-project'",
            (projection.scope_digest, projection.window_seconds),
        )
        legacy = conn.execute(
            "SELECT * FROM projects WHERE id = 'legacy-project'"
        ).fetchone()
        assert not project_authorization.project_authorization_is_current(
            conn, legacy, now="2026-09-21T00:00:01Z"
        )
    finally:
        conn.close()


def test_goal_driven_reauthorization_preserves_execution_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _connection()
    try:
        project = _seed_goal_project(
            conn,
            authorization_expires_at="2026-09-21T00:00:01Z",
        )
        projection = project_authorization.authorization_scope_projection(conn, project)

        @contextmanager
        def get_conn():
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        monkeypatch.setattr(project_authorization, "get_conn", get_conn)
        result = project_authorization.reauthorize_project_record(
            "goal-project",
            ReauthorizeProjectRequest(
                confirmed=True,
                expected_authorization_epoch=1,
                expected_scope_digest=projection.scope_digest,
                authorization_expires_at="2026-10-15T00:00:00Z",
            ),
            actor_id="reauthorizer",
            now="2026-09-22T00:00:00Z",
        )
        assert result.explore_deadline == "2026-09-30T00:00:00Z"
        assert result.authorization_expires_at == "2026-10-15T00:00:00Z"
        grant = conn.execute(
            "SELECT * FROM project_authorization_grants_v2 "
            "WHERE project_id = 'goal-project' AND authorization_epoch = 2"
        ).fetchone()
        assert grant["source"] == "explicit_reauthorization"
        assert grant["authorization_expires_at"] == "2026-10-15T00:00:00Z"
    finally:
        conn.close()


def test_legacy_scope_requires_explicit_confirmation_without_rewriting_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hashlib import sha256
    from cairn.project_authorization_contract import canonical_json

    conn = _connection()
    expires_at = "2026-10-15T00:00:00Z"
    try:
        # 按历史记录构造 v2 原件，不让生产准入保留旧算法或尝试状态组合。
        target_digest = "target-scope:" + sha256((
            "cairn-target-scope-v2\n" + canonical_json([
                {"id": "goal-project-seed", "type": "url", "value": "https://example.test/", "status": "seed"},
            ])
        ).encode()).hexdigest()
        descriptor = {
            "version": 2, "project_id": "goal-project", "tenant_id": "",
            "target_scope_digest": target_digest, "target_ids": ["goal-project-seed"],
        }
        digest = "authorization-scope:" + sha256((
            "cairn-project-authorization-scope-v2\n" + canonical_json(descriptor)
        ).encode()).hexdigest()
        with monkeypatch.context() as legacy:
            legacy.setattr(
                project_authorization, "authorization_scope_projection",
                lambda *_: project_authorization.AuthorizationScopeProjection(digest, descriptor, None),
            )
            _seed_goal_project(conn, authorization_expires_at=expires_at)
        original = dict(conn.execute("SELECT * FROM project_authorization_grants_v2").fetchone())
        conn.execute("UPDATE targets SET status = 'approved' WHERE id = 'goal-project-seed'")
        project = conn.execute("SELECT * FROM projects WHERE id = 'goal-project'").fetchone()
        assert project_authorization.project_authorization_block_reason(
            conn, project, now="2026-09-22T00:00:00Z"
        ) == "project_authorization_scope_upgrade_required"
        assert not project_authorization.project_authorization_is_current(conn, project)
        projection = project_authorization.authorization_scope_projection(conn, project)

        @contextmanager
        def same_connection():
            with conn:
                yield conn

        monkeypatch.setattr(project_authorization, "get_conn", same_connection)
        result = project_authorization.reauthorize_project_record(
            "goal-project",
            ReauthorizeProjectRequest(
                confirmed=True, expected_authorization_epoch=1,
                expected_scope_digest=projection.scope_digest,
            ),
            actor_id="operator", now="2026-09-22T00:00:00Z",
        )
        assert result.authorization_epoch == 2
        assert result.authorization_expires_at == expires_at
        assert result.execution_generation == 4
        assert result.status == "paused"
        assert result.authorization_block_reason is None
        grants = conn.execute(
            "SELECT * FROM project_authorization_grants_v2 ORDER BY authorization_epoch"
        ).fetchall()
        assert len(grants) == 2
        assert dict(grants[0]) == original
        assert grants[1]["source"] == "explicit_reauthorization"
        assert grants[1]["actor_id"] == "operator"
        assert grants[1]["authorization_expires_at"] == expires_at
        assert json.loads(grants[1]["scope_descriptor_json"])["version"] == 3
        project = conn.execute("SELECT * FROM projects WHERE id = 'goal-project'").fetchone()
        assert project_authorization.project_authorization_block_reason(
            conn, project, now=expires_at
        ) == "project_authorization_expired"
        conn.execute("UPDATE targets SET value = 'https://different.test/' WHERE id = 'goal-project-seed'")
        assert project_authorization.project_authorization_block_reason(
            conn, project, now="2026-09-22T00:00:01Z"
        ) == "project_authorization_scope_changed"
    finally:
        conn.close()


def test_historical_execution_is_not_admitted_but_inflight_results_can_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _connection()

    @contextmanager
    def get_conn():
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    monkeypatch.setattr(intents_router, "get_conn", get_conn)
    try:
        conn.execute(
            """
            INSERT INTO projects (
                id, title, status, created_at, scenario_snapshot_json,
                assessment_protocol
            ) VALUES (
                'historical', 'historical', 'active', '2026-09-21T00:00:00Z',
                ?, 'default-copy-v1'
            )
            """,
            (
                json.dumps(
                    {
                        "features": {
                            "execution_contract_version": 1,
                            "execution_engine": "pi",
                            "assessment_protocol": "default-copy-v1",
                            "assessment_contract_version": 2,
                        }
                    }
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO facts (
                id, project_id, description, creator, created_at
            ) VALUES (
                'historical-source', 'historical', 'existing source', 'system',
                '2026-09-21T00:00:00Z'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO intents (
                id, project_id, description, creator, worker,
                last_heartbeat_at, created_at
            ) VALUES (
                'historical-intent', 'historical', 'existing in-flight work',
                'reasoner', 'historical-worker', '2026-09-21T00:00:01Z',
                '2026-09-21T00:00:01Z'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO intent_sources (project_id, intent_id, fact_id)
            VALUES ('historical', 'historical-intent', 'historical-source')
            """
        )
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, intent_id, task_type, worker_name, worker_type,
                status, started_at, execution_generation
            ) VALUES (
                'historical-run', 'historical', 'historical-intent', 'explore',
                'historical-worker', 'pi', 'running',
                '2026-09-21T00:00:02Z', 0
            )
            """
        )
        conn.commit()

        historical = conn.execute(
            "SELECT * FROM projects WHERE id = 'historical'"
        ).fetchone()
        assert execution_rejection_reason(historical) == "assessment_protocol_historical"
        with pytest.raises(HTTPException) as missing_run:
            require_result_settlement_contract(historical, task_run_id=None)
        assert missing_run.value.status_code == 409
        assert missing_run.value.detail == "historical_result_task_run_required"

        response = intents_router.conclude(
            "historical",
            "historical-intent",
            ConcludeRequest(
                worker="historical-worker",
                task_run_id="historical-run",
                description="settled historical in-flight result",
            ),
        )
        assert response.fact.task_run_id == "historical-run"
        assert response.intent.to == response.fact.id
        accepted = conn.execute(
            "SELECT task_run_id, description FROM facts "
            "WHERE project_id = 'historical' AND id = ?",
            (response.fact.id,),
        ).fetchone()
        assert dict(accepted) == {
            "task_run_id": "historical-run",
            "description": "settled historical in-flight result",
        }
        run_context = json.loads(
            conn.execute(
                "SELECT runtime_context_json FROM task_runs "
                "WHERE project_id = 'historical' AND id = 'historical-run'"
            ).fetchone()["runtime_context_json"]
        )
        assert run_context["accepted_fact_id"] == response.fact.id
        assert run_context["accepted_result_digest"]

        conn.execute(
            "UPDATE projects SET assessment_protocol = 'goal-driven-v1', "
            "scenario_snapshot_json = ? WHERE id = 'historical'",
            (
                json.dumps(
                    {
                        "features": {
                            "execution_contract_version": 1,
                            "execution_engine": "pi",
                            "assessment_protocol": "goal-driven-v1",
                            "assessment_contract_version": 1,
                        }
                    }
                ),
            ),
        )
        malformed_goal = conn.execute(
            "SELECT * FROM projects WHERE id = 'historical'"
        ).fetchone()
        with pytest.raises(HTTPException, match="assessment_contract_unsupported"):
            require_result_settlement_contract(
                malformed_goal,
                task_run_id="historical-run",
            )
    finally:
        conn.close()
