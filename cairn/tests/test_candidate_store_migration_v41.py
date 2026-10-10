"""v41/v47 CandidateStore 迁移、容量、原件保真和不可变约束回归。"""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

import psycopg
import pytest

from cairn.server import db, migrations
from test_assessment_migration_v34 import database, private_postgres


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
    conn: sqlite3.Connection | db.PostgresConnection,
    *,
    candidate_id: str = "cnd_parent",
    producer_key: str = "22222222-2222-4222-8222-222222222222:1:0",
    parent_id: str | None = None,
    raw: bytes = b"\x00{\r\n\xe4\xb8\xad\xe6\x96\x87}",
    digest: str = "a" * 64,
    artifact_count: int = 0,
    artifact_identities_json: str = "[]",
    manifest_json: str = "{}",
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
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
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
            artifact_identities_json,
            artifact_count,
            raw,
            len(raw),
            digest,
            digest,
            manifest_json,
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


def _insert_outcome(conn, *, manifest_json: str = "{}", sequence: int = 1) -> None:
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
            ?, 'cnd_parent', 'p', 'run', 'cairn-candidate-frame-v1',
            '22222222-2222-4222-8222-222222222222:1:0', ?, 'schema_invalid',
            'result.data', 'required', 2, 'not_evaluated', 'not_evaluated',
            ?, ?, 'mch_history', 1, 'historical_at_receipt',
            '2026-09-19T00:00:02Z'
        )
        """,
        (f"cno_{sequence}", sequence, "b" * 64, manifest_json),
    )


def _seed_project_and_run(conn) -> None:
    conn.execute(
        "INSERT INTO projects (id, title, status, created_at) "
        "VALUES ('p', 'capacity history', 'active', '2026-09-19T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO task_runs "
        "(id, project_id, task_type, worker_name, worker_type, status) "
        "VALUES ('run', 'p', 'explore', 'worker', 'pi', 'succeeded')"
    )


@contextmanager
def _rejected_write(conn, *, immutable: bool = False):
    # PostgreSQL 的约束错误会中止事务，用 savepoint 保留本次测试的历史种子。
    expected = (
        (sqlite3.IntegrityError, psycopg.errors.RaiseException)
        if immutable else (sqlite3.IntegrityError, psycopg.IntegrityError)
    )
    conn.execute("SAVEPOINT rejected_write")
    try:
        with pytest.raises(expected, match="immutable" if immutable else None):
            yield
    finally:
        conn.execute("ROLLBACK TO SAVEPOINT rejected_write")
        conn.execute("RELEASE SAVEPOINT rejected_write")


def _manifest_of_size(size: int) -> str:
    # 多字节内容用于区分 byte 上限与字符数上限。
    prefix = '{"资料":"' + "中" * 1500
    suffix = '"}'
    return prefix + "x" * (size - len((prefix + suffix).encode("utf-8"))) + suffix


def _assert_v47_capacity_and_guards(conn) -> None:
    for count in (21, 32):
        identities = json.dumps([
            {"handle": f"{index:03d}" + "x" * 125, "sha256": "a" * 64, "size": 1}
            for index in range(count)
        ])
        manifest = _manifest_of_size(16384 if count == 32 else 8192)
        _insert_candidate(
            conn,
            candidate_id=f"cnd_{count}",
            producer_key=f"capacity:{count}",
            artifact_count=count,
            artifact_identities_json=identities,
            manifest_json=manifest,
        )
        row = conn.execute(
            "SELECT artifact_count, artifact_identities_json, manifest_json "
            "FROM task_run_candidates WHERE id = ?", (f"cnd_{count}",)
        ).fetchone()
        assert dict(row) == {
            "artifact_count": count,
            "artifact_identities_json": identities,
            "manifest_json": manifest,
        }
    _insert_outcome(conn, sequence=2, manifest_json=_manifest_of_size(16384))
    assert conn.execute(
        "SELECT manifest_json FROM task_run_candidate_outcomes WHERE id = 'cno_2'"
    ).fetchone()["manifest_json"].encode("utf-8") == _manifest_of_size(16384).encode("utf-8")

    for invalid in (
        {"artifact_count": 33},
        {"artifact_count": -1},
        {"manifest_json": _manifest_of_size(16385)},
        {"manifest_json": "x"},
        {"digest": "A" * 64},
        {"parent_id": "cnd_missing"},
    ):
        with _rejected_write(conn):
            _insert_candidate(
                conn, candidate_id="cnd_invalid", producer_key="invalid:1", **invalid
            )
    with _rejected_write(conn):
        _insert_outcome(conn, sequence=3, manifest_json=_manifest_of_size(16385))
    with _rejected_write(conn):
        _insert_outcome(conn, sequence=65)
    with _rejected_write(conn, immutable=True):
        conn.execute("UPDATE task_run_candidates SET phase = 'report' WHERE id = 'cnd_parent'")
    with _rejected_write(conn, immutable=True):
        conn.execute("DELETE FROM task_run_candidate_outcomes WHERE id = 'cno_2'")
    with _rejected_write(conn):
        conn.execute("INSERT INTO task_run_candidates SELECT * FROM task_run_candidates WHERE id = 'cnd_parent'")
    with _rejected_write(conn):
        conn.execute("INSERT INTO task_run_candidate_outcomes SELECT * FROM task_run_candidate_outcomes WHERE id = 'cno_2'")
    conn.commit()


def _history_snapshot(conn):
    tables = (
        "projects", "intents", "task_runs", "assessment_attempts",
        "assessment_charge_compensations", "project_authorization_grants",
        "project_authorization_grants_v2", "task_run_candidates",
        "task_run_candidate_outcomes", "schema_migrations",
    )
    return {
        table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
        for table in tables
    }


def _candidate_guards(conn, backend):
    if backend == "sqlite":
        return {
            "tables": {
                row["name"]: row["sql"]
                .replace("artifact_count BETWEEN 0 AND 32", "artifact_count BETWEEN 0 AND 16")
                .replace("BETWEEN 2 AND 16384", "BETWEEN 2 AND 4096")
                for row in conn.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
                    "AND name IN ('task_run_candidates', 'task_run_candidate_outcomes')"
                )
            },
            "indexes_and_triggers": [dict(row) for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type IN ('index', 'trigger') "
                "AND tbl_name IN ('task_run_candidates', 'task_run_candidate_outcomes') ORDER BY name"
            )],
            "foreign_keys": {
                table: [dict(row) for row in conn.execute(f"PRAGMA foreign_key_list({table})")]
                for table in ("task_run_candidates", "task_run_candidate_outcomes")
            },
        }
    return {
        "indexes": [dict(row) for row in conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename IN "
            "('task_run_candidates', 'task_run_candidate_outcomes') ORDER BY indexname"
        )],
        "constraints": [dict(row) for row in conn.execute(
            "SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint "
            "WHERE conrelid IN ('task_run_candidates'::regclass, 'task_run_candidate_outcomes'::regclass) "
            "AND conname NOT IN ('task_run_candidates_artifact_count_check', "
            "'task_run_candidates_manifest_json_check', 'task_run_candidate_outcomes_manifest_json_check') "
            "ORDER BY conname"
        )],
        "triggers": [dict(row) for row in conn.execute(
            "SELECT tgname, pg_get_triggerdef(oid) AS definition FROM pg_trigger "
            "WHERE tgrelid IN ('task_run_candidates'::regclass, 'task_run_candidate_outcomes'::regclass) "
            "AND NOT tgisinternal ORDER BY tgname"
        )],
    }


def test_v47_upgrades_v46_preserving_bytes_lineage_fees_authorization_and_registry(
    database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, connect = database
    conn = connect()
    try:
        registry_name = f"{backend.upper()}_MIGRATIONS"
        with monkeypatch.context() as patch:
            patch.setattr(db, registry_name, tuple(
                migration for migration in getattr(migrations, registry_name)
                if migration.version <= 46
            ))
            db.run_migrations(conn, backend)
        _seed_project_and_run(conn)
        conn.execute(
            "UPDATE projects SET tenant_id = 'tenant_legacy', explore_budget = 40, "
            "explore_attempts_started = 1, authorization_epoch = 2, execution_generation = 3, "
            "authorization_expires_at = '2026-09-20T00:00:00Z' WHERE id = 'p'"
        )
        conn.execute(
            "INSERT INTO intents (id, project_id, description, creator, created_at) "
            "VALUES ('i', 'p', 'historical research', 'reason', '2026-09-19T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO assessment_attempts "
            "(id, project_id, intent_id, status, charged, execution_count, started_at, ended_at, outcome) "
            "VALUES ('attempt', 'p', 'i', 'completed', 1, 2, '2026-09-19T00:00:00Z', "
            "'2026-09-19T00:00:03Z', 'accepted')"
        )
        conn.execute(
            "UPDATE task_runs SET assessment_attempt_id = 'attempt', execution_generation = 1 "
            "WHERE id = 'run'"
        )
        conn.execute(
            "INSERT INTO assessment_charge_compensations "
            "(project_id, assessment_attempt_id, migration_version, predicate_version, reason_code, "
            "original_charged, charge_delta, project_counter_before, project_counter_after, "
            "source_reason_task_run_id, task_run_ids_json, proof_sha256, created_at) "
            "VALUES ('p', 'historical-compensated', 39, 'pre-execution-missing-structured-cell-v1', "
            "'structured_explore_missing_slot_pre_execution', 1, -1, 2, 1, 'run', ?, ?, "
            "'2026-09-19T00:00:01Z')", ('[ "run" ]\r\n', "c" * 64)
        )
        for version in (1, 2):
            expiry_columns = "window_seconds, new_deadline" if version == 1 else "authorization_expires_at"
            expiry_values = "86400, '2026-09-20T00:00:00Z'" if version == 1 else "'2026-09-20T00:00:00Z'"
            table = "project_authorization_grants" + ("_v2" if version == 2 else "")
            conn.execute(
                f"INSERT INTO {table} (project_id, authorization_epoch, protocol, source, actor_type, "
                f"actor_id, scope_digest, scope_descriptor_json, {expiry_columns}, execution_generation, authorized_at) "
                f"VALUES ('p', ?, ?, 'explicit_reauthorization', 'user', 'synthetic', ?, ?, "
                f"{expiry_values}, 1, '2026-09-19T00:00:00Z')",
                (version, f"cairn-project-authorization-v{version}",
                 "authorization-scope:" + "d" * 64, '{ "scope": "中文" }\r\n'),
            )
        raw = b'\x00\xff{\r\n"raw":"\xe4\xb8\xad\xe6\x96\x87"}\x00'
        manifest = '{ "z": 1, "资料": "原文", "a": 2 }\r\n'
        if backend == "sqlite":
            # child 先写入，确保迁移不依赖物理行序就是 lineage 拓扑顺序。
            conn.execute("PRAGMA defer_foreign_keys=ON")
            _insert_candidate(conn, candidate_id="cnd_child", producer_key="child:1", parent_id="cnd_parent")
        _insert_candidate(conn, raw=raw, manifest_json=manifest)
        if backend == "postgres":
            _insert_candidate(conn, candidate_id="cnd_child", producer_key="child:1", parent_id="cnd_parent")
        _insert_outcome(conn, manifest_json=manifest)
        conn.commit()
        before = _history_snapshot(conn)
        guards = _candidate_guards(conn, backend)
        for invalid in ({"artifact_count": 21}, {"manifest_json": _manifest_of_size(8192)}):
            with _rejected_write(conn):
                _insert_candidate(conn, candidate_id="cnd_old_overflow", producer_key="old:1", **invalid)
        with _rejected_write(conn):
            _insert_outcome(conn, sequence=2, manifest_json=_manifest_of_size(8192))
        conn.commit()

        db.run_migrations(conn, backend)
        conn.rollback()
        assert _history_snapshot(conn) == before
        assert _candidate_guards(conn, backend) == guards
        conn.commit()
        db.run_migrations(conn, backend)
        conn.commit()
        after = _history_snapshot(conn)
        new_registry = after.pop("schema_migrations")
        assert after == {table: rows for table, rows in before.items() if table != "schema_migrations"}
        assert new_registry[:-1] == before["schema_migrations"]
        assert new_registry[-1]["version"] == 47
        assert _candidate_guards(conn, backend) == guards
        _assert_v47_capacity_and_guards(conn)
        registry = _history_snapshot(conn)["schema_migrations"]
        conn.commit()
        db.run_migrations(conn, backend)
        conn.commit()
        assert _history_snapshot(conn)["schema_migrations"] == registry
        if backend == "sqlite":
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
            assert conn.execute("SELECT name FROM sqlite_temp_master").fetchall() == []
        conn.execute("DELETE FROM projects WHERE id = 'p'")
        conn.commit()
        for table in ("task_run_candidates", "task_run_candidate_outcomes"):
            assert conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] == 0
    finally:
        conn.close()


def test_v47_new_migrated_database_accepts_capacity(database) -> None:
    backend, connect = database
    conn = connect()
    try:
        db.run_migrations(conn, backend)
        _seed_project_and_run(conn)
        _insert_candidate(conn)
        _assert_v47_capacity_and_guards(conn)
    finally:
        conn.close()


def test_v47_fresh_sqlite_schema_accepts_capacity_without_changing_v41_checksums() -> None:
    assert migrations.SQLITE_TASK_RUN_CANDIDATE_STORE.checksum == (
        "34886f2fbc22cdfa8ec5fd9d53ea79d5c815e0fdcb453640b9e2f49389f3cefd"
    )
    assert migrations.POSTGRES_TASK_RUN_CANDIDATE_STORE.checksum == (
        "e5fdcbd9a774d351f339696c684090048185144d09359712536f215061a40e90"
    )
    conn = _sqlite_connection(":memory:")
    try:
        conn.executescript(db.SCHEMA)
        _seed_project_and_run(conn)
        _insert_candidate(conn)
        _assert_v47_capacity_and_guards(conn)
    finally:
        conn.close()
