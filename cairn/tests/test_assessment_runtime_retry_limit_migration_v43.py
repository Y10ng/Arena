"""v43 运行重试上限迁移回归。"""

from __future__ import annotations

import sqlite3

import pytest

from cairn.server import db, migrations


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def test_v43_updates_only_active_attempts_using_the_old_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _connection()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                db,
                "SQLITE_MIGRATIONS",
                tuple(item for item in migrations.SQLITE_MIGRATIONS if item.version <= 42),
            )
            db.run_migrations(conn, "sqlite")

        conn.execute(
            "INSERT INTO projects "
            "(id, title, status, created_at, explore_budget, explore_attempts_started, "
            "authorization_epoch, execution_generation) "
            "VALUES ('p', 'retry migration', 'active', '2026-09-23T00:00:00Z', 40, 15, 1, 1)"
        )
        attempts = (
            ("a-active-default", "i-active-default", "active", 3, 2, None, None),
            ("a-active-custom", "i-active-custom", "active", 2, 7, None, None),
            (
                "a-completed-default",
                "i-completed-default",
                "completed",
                1,
                2,
                "2026-09-23T00:10:00Z",
                "accepted",
            ),
            (
                "a-failed-default",
                "i-failed-default",
                "failed",
                3,
                2,
                "2026-09-23T00:11:00Z",
                "provider_error",
            ),
        )
        for _attempt_id, intent_id, *_ in attempts:
            conn.execute(
                "INSERT INTO intents (id, project_id, description, creator, created_at) "
                "VALUES (?, 'p', ?, 'reason', '2026-09-23T00:00:01Z')",
                (intent_id, intent_id),
            )
        for attempt_id, intent_id, status, execution_count, limit, ended_at, outcome in attempts:
            conn.execute(
                """
                INSERT INTO assessment_attempts (
                    id, project_id, intent_id, status, execution_count,
                    max_runtime_retries, started_at, ended_at, outcome
                ) VALUES (?, 'p', ?, ?, ?, ?, '2026-09-23T00:00:02Z', ?, ?)
                """,
                (
                    attempt_id,
                    intent_id,
                    status,
                    execution_count,
                    limit,
                    ended_at,
                    outcome,
                ),
            )
        conn.execute(
            "INSERT INTO task_runs "
            "(id, project_id, intent_id, task_type, worker_name, worker_type, status, "
            "error_type, execution_generation) "
            "VALUES ('run-accepted', 'p', 'i-completed-default', 'explore', 'worker', "
            "'pi', 'succeeded', NULL, 0)"
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description, creator, task_run_id, created_at) "
            "VALUES ('fact-accepted', 'p', 'accepted evidence', 'worker', 'run-accepted', "
            "'2026-09-23T00:10:00Z')"
        )
        conn.execute(
            "INSERT INTO task_runs "
            "(id, project_id, intent_id, task_type, worker_name, worker_type, status, "
            "error_type, execution_generation) "
            "VALUES ('run-failed', 'p', 'i-active-default', 'explore', 'worker', "
            "'pi', 'failed', 'provider_error', 0)"
        )
        prefix = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM schema_migrations WHERE version <= 42 ORDER BY version"
            )
        ]
        preserved = {
            "project": dict(conn.execute(
                "SELECT explore_budget, explore_attempts_started, authorization_epoch, "
                "execution_generation FROM projects WHERE id='p'"
            ).fetchone()),
            "facts": [dict(row) for row in conn.execute(
                "SELECT id, task_run_id FROM facts WHERE project_id='p' ORDER BY id"
            )],
            "runs": [dict(row) for row in conn.execute(
                "SELECT id, error_type, execution_generation FROM task_runs "
                "WHERE project_id='p' ORDER BY id"
            )],
        }
        conn.commit()

        db.run_migrations(conn, "sqlite")
        conn.commit()

        assert [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM schema_migrations WHERE version <= 42 ORDER BY version"
            )
        ] == prefix
        assert dict(
            conn.execute(
                "SELECT version, name, checksum FROM schema_migrations WHERE version = 43"
            ).fetchone()
        ) == {
            "version": 43,
            "name": "assessment-runtime-retry-limit-v1",
            "checksum": migrations.SQLITE_ASSESSMENT_RUNTIME_RETRY_LIMIT.checksum,
        }
        rows = {
            row["id"]: dict(row)
            for row in conn.execute(
                "SELECT id, status, execution_count, max_runtime_retries, ended_at, outcome "
                "FROM assessment_attempts ORDER BY id"
            )
        }
        assert rows["a-active-default"] == {
            "id": "a-active-default",
            "status": "active",
            "execution_count": 3,
            "max_runtime_retries": 10,
            "ended_at": None,
            "outcome": None,
        }
        assert rows["a-active-custom"]["max_runtime_retries"] == 7
        assert rows["a-completed-default"]["max_runtime_retries"] == 2
        assert rows["a-failed-default"]["max_runtime_retries"] == 2
        assert rows["a-completed-default"]["outcome"] == "accepted"
        assert rows["a-failed-default"]["outcome"] == "provider_error"
        assert preserved == {
            "project": dict(conn.execute(
                "SELECT explore_budget, explore_attempts_started, authorization_epoch, "
                "execution_generation FROM projects WHERE id='p'"
            ).fetchone()),
            "facts": [dict(row) for row in conn.execute(
                "SELECT id, task_run_id FROM facts WHERE project_id='p' ORDER BY id"
            )],
            "runs": [dict(row) for row in conn.execute(
                "SELECT id, error_type, execution_generation FROM task_runs "
                "WHERE project_id='p' ORDER BY id"
            )],
        }
    finally:
        conn.close()


def test_v43_is_latest_for_both_databases_and_postgres_is_equally_scoped() -> None:
    assert migrations.SQLITE_MIGRATIONS[-1] is migrations.SQLITE_ASSESSMENT_RUNTIME_RETRY_LIMIT
    assert migrations.POSTGRES_MIGRATIONS[-1] is migrations.POSTGRES_ASSESSMENT_RUNTIME_RETRY_LIMIT
    assert migrations.SQLITE_MIGRATIONS[-1].version == 43
    assert migrations.POSTGRES_MIGRATIONS[-1].version == 43

    sql = migrations.POSTGRES_ASSESSMENT_RUNTIME_RETRY_LIMIT_V43
    assert "UPDATE assessment_attempts" in sql
    assert "status = 'active'" in sql
    assert "max_runtime_retries = 2" in sql
    assert "SET max_runtime_retries = 10" in sql
