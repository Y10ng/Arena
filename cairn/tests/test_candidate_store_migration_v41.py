"""v41 CandidateStore 迁移、二进制保真和不可变约束回归。"""

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


def _sqlite_connection(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA recursive_triggers=ON")
    return conn


def _insert_candidate(
    conn: sqlite3.Connection,
    *,
    candidate_id: str = "cnd_parent",
    producer_key: str = "22222222-2222-4222-8222-222222222222:1:0",
    parent_id: str | None = None,
    raw: bytes = b"\x00{\r\n\xe4\xb8\xad\xe6\x96\x87}",
    digest: str = "a" * 64,
) -> None:
    conn.execute(
        """
        INSERT INTO task_run_candidates (
            id, project_id, task_run_id, protocol, source_format,
            capture_id, producer_candidate_key, parent_candidate_id,
            assistant_turn, content_index, phase, session_id,
            binding_sha256, result_schema_sha256, delivery_sha256,
            artifact_identities_json, artifact_count,
            raw_bytes, raw_size_bytes, raw_sha256, request_sha256,
            manifest_json, verified_dispatcher_id, execution_generation,
            generation_disposition, received_at
        ) VALUES (
            ?, 'p', 'run', 'cairn-candidate-frame-v1',
            'pi-toolcall-delta-utf8-v1',
            '22222222-2222-4222-8222-222222222222', ?, ?,
            1, 0, 'explore_execute',
            'pi:11111111-1111-4111-8111-111111111111',
            ?, ?, ?, '[]', 0, ?, ?, ?, ?, '{}',
            'mch_history', 1, 'historical_at_receipt',
            '2026-09-19T00:00:01Z'
        )
        """,
        (
            candidate_id,
            producer_key,
            parent_id,
            digest,
            digest,
            digest,
            raw,
            len(raw),
            digest,
            digest,
        ),
    )


def test_v41_upgrades_v40_preserves_registry_blob_lineage_and_project_cascade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _sqlite_connection(tmp_path / "candidate-v41.db")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                db,
                "SQLITE_MIGRATIONS",
                tuple(
                    migration
                    for migration in migrations.SQLITE_MIGRATIONS
                    if migration.version <= 40
                ),
            )
            db.run_migrations(conn, "sqlite")
            conn.commit()

        conn.execute(
            """
            INSERT INTO projects (
                id, title, status, created_at, tenant_id, execution_generation
            ) VALUES ('p', 'history', 'active', '2026-09-19T00:00:00Z',
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
                session_id, runtime_context_json, execution_generation
            ) VALUES ('run', 'p', 'explore', 'worker', 'pi', 'succeeded',
                      'pi:11111111-1111-4111-8111-111111111111', ?, 1)
            """,
            (json.dumps(context),),
        )
        before = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM schema_migrations WHERE version <= 40 ORDER BY version"
            ).fetchall()
        ]
        conn.commit()

        db.run_migrations(conn, "sqlite")
        conn.commit()
        after_prefix = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM schema_migrations WHERE version <= 40 ORDER BY version"
            ).fetchall()
        ]
        assert after_prefix == before
        migration_v41 = conn.execute(
            "SELECT version, name, checksum FROM schema_migrations WHERE version = 41"
        ).fetchone()
        assert dict(migration_v41) == {
            "version": 41,
            "name": "task-run-candidate-store-v1",
            "checksum": migrations.SQLITE_TASK_RUN_CANDIDATE_STORE.checksum,
        }

        raw = b"\x00{\r\n\xe4\xb8\xad\xe6\x96\x87}"
        _insert_candidate(conn, raw=raw)
        stored = conn.execute(
            "SELECT raw_bytes, raw_size_bytes FROM task_run_candidates WHERE id = 'cnd_parent'"
        ).fetchone()
        assert stored["raw_bytes"] == raw
        assert stored["raw_size_bytes"] == len(raw)
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            _insert_candidate(
                conn,
                candidate_id="cnd_orphan",
                producer_key="33333333-3333-4333-8333-333333333333:2:0",
                parent_id="cnd_missing",
            )
        conn.rollback()
        assert conn.execute(
            "SELECT raw_bytes FROM task_run_candidates WHERE id = 'cnd_parent'"
        ).fetchone()["raw_bytes"] == raw

        _insert_candidate(
            conn,
            candidate_id="cnd_child",
            producer_key="33333333-3333-4333-8333-333333333333:2:0",
            parent_id="cnd_parent",
        )
        conn.execute(
            """
            INSERT INTO task_run_candidate_outcomes (
                id, candidate_id, project_id, task_run_id, protocol,
                producer_candidate_key, outcome_sequence, code,
                diagnostic_path, diagnostic_keyword, diagnostic_count,
                artifact_status, binding_status, request_sha256,
                manifest_json, verified_dispatcher_id, execution_generation,
                generation_disposition, received_at
            ) VALUES (
                'cno_parent', 'cnd_parent', 'p', 'run',
                'cairn-candidate-frame-v1',
                '22222222-2222-4222-8222-222222222222:1:0',
                1, 'schema_invalid', 'result.data', 'required', 2,
                'not_evaluated', 'not_evaluated', ?, '{}',
                'mch_history', 1, 'historical_at_receipt',
                '2026-09-19T00:00:02Z'
            )
            """,
            ("b" * 64,),
        )
        conn.commit()

        with pytest.raises(
            sqlite3.IntegrityError,
            match="task_run_candidates are immutable",
        ):
            conn.execute(
                """
                INSERT OR REPLACE INTO task_run_candidates
                SELECT * FROM task_run_candidates WHERE id = 'cnd_child'
                """
            )
        conn.rollback()
        with pytest.raises(
            sqlite3.IntegrityError,
            match="task_run_candidate_outcomes are immutable",
        ):
            conn.execute(
                """
                INSERT OR REPLACE INTO task_run_candidate_outcomes
                SELECT * FROM task_run_candidate_outcomes WHERE id = 'cno_parent'
                """
            )
        conn.rollback()
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM task_run_candidates"
        ).fetchone()["n"] == 2
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM task_run_candidate_outcomes"
        ).fetchone()["n"] == 1
        assert conn.execute(
            "SELECT raw_bytes FROM task_run_candidates WHERE id = 'cnd_parent'"
        ).fetchone()["raw_bytes"] == raw

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE task_run_candidates SET phase = 'report' WHERE id = 'cnd_parent'"
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "DELETE FROM task_run_candidate_outcomes WHERE id = 'cno_parent'"
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _insert_candidate(
                conn,
                candidate_id="cnd_upper",
                producer_key="44444444-4444-4444-8444-444444444444:3:0",
                digest="A" * 64,
            )
        conn.rollback()

        conn.execute("DELETE FROM projects WHERE id = 'p'")
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM task_run_candidates"
        ).fetchone()["n"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM task_run_candidate_outcomes"
        ).fetchone()["n"] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

        registry = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        db.run_migrations(conn, "sqlite")
        conn.commit()
        assert registry == [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
    finally:
        conn.close()


def test_fresh_sqlite_schema_contains_v41_candidate_store_without_changing_v1() -> None:
    expected_v1_checksum = migrations.SQLITE_BASELINE.checksum
    assert migrations.SQLITE_V1_SCHEMA is migrations.SCHEMA

    conn = _sqlite_connection(":memory:")
    try:
        conn.executescript(db.SCHEMA)
        tables = {
            row["name"]
            for row in conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name LIKE 'task_run_candidate%'
                """
            ).fetchall()
        }
        triggers = {
            row["name"]
            for row in conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'trigger' AND name LIKE 'trg_task_run_candidate%'
                """
            ).fetchall()
        }
    finally:
        conn.close()

    assert tables == {"task_run_candidates", "task_run_candidate_outcomes"}
    assert triggers == {
        "trg_task_run_candidates_no_update",
        "trg_task_run_candidates_no_delete",
        "trg_task_run_candidate_outcomes_no_update",
        "trg_task_run_candidate_outcomes_no_delete",
    }
    assert migrations.SQLITE_BASELINE.checksum == expected_v1_checksum
    assert "CREATE TABLE IF NOT EXISTS task_run_candidates" not in migrations.SQLITE_V1_SCHEMA
    assert "CREATE TABLE IF NOT EXISTS task_run_candidates" in db.SCHEMA
    assert any(item.version == 41 for item in migrations.SQLITE_MIGRATIONS)
    assert any(item.version == 41 for item in migrations.POSTGRES_MIGRATIONS)


def test_postgres_v41_uses_bytea_octet_constraints_and_immutable_triggers() -> None:
    sql = " ".join(migrations.POSTGRES_TASK_RUN_CANDIDATE_STORE_V41.split())

    assert "raw_bytes BYTEA NOT NULL" in sql
    assert "BLOB" not in sql
    assert "GLOB" not in sql
    assert "octet_length(manifest_json) BETWEEN 2 AND 4096" in sql
    assert "binding_sha256 ~ '^[0-9a-f]{64}$'" in sql
    assert "raw_sha256 ~ '^[0-9a-f]{64}$'" in sql
    assert "assistant_turn BETWEEN 1 AND 1000000" in sql
    assert "content_index BETWEEN 0 AND 1000000" in sql
    assert "BEFORE UPDATE OR DELETE ON task_run_candidates" in sql
    assert "BEFORE UPDATE OR DELETE ON task_run_candidate_outcomes" in sql
    assert "EXISTS (SELECT 1 FROM projects WHERE id = OLD.project_id)" in sql
