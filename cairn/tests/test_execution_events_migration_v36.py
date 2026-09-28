"""v36 执行事件账本迁移与新库 schema 回归。"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from cairn.server import db, migrations


@pytest.fixture(autouse=True)
def reset_database_configuration():
    db._reset_for_tests()
    yield
    db._reset_for_tests()


def _sqlite_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def test_v36_upgrades_v35_preserves_registry_and_preserves_project_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _sqlite_connection(tmp_path / "execution-event-v36.db")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                db,
                "SQLITE_MIGRATIONS",
                tuple(migration for migration in migrations.SQLITE_MIGRATIONS if migration.version <= 35),
            )
            db.run_migrations(conn, "sqlite")
            conn.commit()

        conn.execute(
            """
            INSERT INTO projects (
                id, title, status, created_at, tenant_id, execution_generation
            ) VALUES ('p', 'history', 'active', '2026-09-17T00:00:00Z',
                      'tenant_legacy', 2)
            """
        )
        context = {
            "dispatcher_id": "mch_history",
            "execution_delivery": {"phase": "explore_execute"},
        }
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, task_type, worker_name, worker_type, status,
                runtime_context_json, execution_generation
            ) VALUES ('run', 'p', 'explore', 'worker', 'pi', 'succeeded', ?, 1)
            """,
            (json.dumps(context),),
        )
        before = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM schema_migrations WHERE version <= 35 ORDER BY version"
            ).fetchall()
        ]
        conn.commit()

        db.run_migrations(conn, "sqlite")
        conn.commit()
        after_prefix = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM schema_migrations WHERE version <= 35 ORDER BY version"
            ).fetchall()
        ]
        assert after_prefix == before
        migration_v36 = conn.execute(
            "SELECT version, name, checksum FROM schema_migrations WHERE version = 36"
        ).fetchone()
        assert dict(migration_v36) == {
            "version": 36,
            "name": "execution-event-ledger-v1",
            "checksum": migrations.SQLITE_EXECUTION_EVENT_LEDGER.checksum,
        }
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(execution_events)").fetchall()
        }
        assert columns == {
            "id",
            "project_id",
            "task_run_id",
            "protocol",
            "producer_event_key",
            "event_kind",
            "phase",
            "category",
            "producer_recorded_at",
            "received_at",
            "verified_dispatcher_id",
            "execution_generation",
            "generation_disposition",
            "payload_digest_sha256",
            "payload_json",
        }
        assert conn.execute("SELECT title FROM projects WHERE id = 'p'").fetchone()["title"] == "history"
        assert conn.execute(
            "SELECT execution_generation FROM task_runs WHERE id = 'run' AND project_id = 'p'"
        ).fetchone()["execution_generation"] == 1

        conn.execute(
            """
            INSERT INTO execution_events (
                id, project_id, task_run_id, protocol, producer_event_key,
                event_kind, phase, category, producer_recorded_at, received_at,
                verified_dispatcher_id, execution_generation,
                generation_disposition, payload_digest_sha256, payload_json
            ) VALUES (
                'xev_history', 'p', 'run', 'cairn-execution-event-v1',
                'checkpoint-1', 'checkpoint', 'explore_execute', 'progress',
                '2026-09-17T00:00:00.000Z', '2026-09-17T00:00:01Z',
                'mch_history', 1, 'historical_at_receipt', ?, '{}'
            )
            """,
            ("a" * 64,),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE execution_events SET category = 'handoff' "
                "WHERE id = 'xev_history' AND project_id = 'p'"
            )
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) AS n FROM execution_events").fetchone()["n"] == 1

        # Ledger 行在项目存续期间不可改写；项目删除仍沿用现有审计数据的级联策略。
        conn.execute("DELETE FROM projects WHERE id = 'p'")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) AS n FROM execution_events").fetchone()["n"] == 0

        registry = [dict(row) for row in conn.execute("SELECT * FROM schema_migrations ORDER BY version")]
        db.run_migrations(conn, "sqlite")
        conn.commit()
        assert registry == [dict(row) for row in conn.execute("SELECT * FROM schema_migrations ORDER BY version")]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_fresh_sqlite_schema_contains_v36_ledger_without_changing_v1_checksum() -> None:
    expected_v1_checksum = migrations.SQLITE_BASELINE.checksum
    assert migrations.SQLITE_V1_SCHEMA is migrations.SCHEMA

    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(db.SCHEMA)
        table = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'execution_events'"
        ).fetchone()
        triggers = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'trg_execution_events_%'"
            ).fetchall()
        }
    finally:
        conn.close()

    assert table is not None
    assert triggers == {"trg_execution_events_no_update"}
    assert migrations.SQLITE_BASELINE.checksum == expected_v1_checksum
    assert "CREATE TABLE IF NOT EXISTS execution_events" not in migrations.SQLITE_V1_SCHEMA
    assert "CREATE TABLE IF NOT EXISTS execution_events" in db.SCHEMA
    assert migrations.SQLITE_MIGRATIONS[-1].version == 41
    assert migrations.POSTGRES_MIGRATIONS[-1].version == 41


def test_postgres_v36_preserves_project_cascade_and_blocks_only_updates() -> None:
    sql = " ".join(migrations.POSTGRES_EXECUTION_EVENT_LEDGER_V36.split())

    assert (
        "FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE"
        in sql
    )
    assert (
        "FOREIGN KEY (task_run_id, project_id) "
        "REFERENCES task_runs(id, project_id) ON DELETE CASCADE"
        in sql
    )
    assert "BEFORE UPDATE ON execution_events" in sql
    assert "BEFORE DELETE ON execution_events" not in sql
    assert "trg_execution_events_no_delete" not in sql
