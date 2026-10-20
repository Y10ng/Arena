"""v55 NULL 三值逻辑回归与 v56 追加约束修正只使用隔离数据库。"""
from __future__ import annotations

from hashlib import sha256
import sqlite3

import psycopg
import pytest

from cairn.server import db, migrations
from test_assessment_migration_v34 import database, private_postgres


def _migrate_to(conn, backend, monkeypatch, version):
    registry = "SQLITE_MIGRATIONS" if backend == "sqlite" else "POSTGRES_MIGRATIONS"
    with monkeypatch.context() as patch:
        patch.setattr(db, registry, tuple(item for item in getattr(db, registry) if item.version <= version))
        db.run_migrations(conn, backend)


def _seed_request(conn, name, *, previous=None, hidden=None):
    conn.execute("INSERT INTO projects(id,title,status,created_at,tenant_id) VALUES (?,?,'completed','2000','tenant_legacy')", (name, name))
    conn.execute("INSERT INTO intents(id,project_id,description,creator,created_at) VALUES ('i001',?,'synthetic','synthetic','2000')", (name,))
    conn.execute("INSERT INTO project_resources(id,project_id,kind,path,size_bytes,sha256,created_by,created_at) VALUES ('r001',?,'note','report.txt',1,?,'synthetic','2000')", (name, "a" * 64))
    conn.execute("INSERT INTO verification_requests(id,schema_version,tenant_id,caller_type,caller_id,client_request_id,request_digest,mode,project_id,intent_id,report_type,report_resource_id,previous_request_id,created_at,updated_at,deleted_at) VALUES (?,1,'tenant_legacy','user','synthetic',?,?,'retest',?,'i001','text','r001',?,'2000','2000',?)", (name, name, "b" * 64, name, previous, hidden))


_NULL_BYPASSES = (
    "UPDATE verification_requests SET mode = NULL, project_id = NULL, intent_id = NULL, report_type = NULL, report_resource_id = NULL WHERE id = 'normal'",
    "UPDATE verification_requests SET deleted_at = '2001', purge_batch_ids_json = '[]', purge_error = 'synthetic error without pending' WHERE id = 'normal'",
)


def test_v55_null_check_bypasses_are_reproducible(database, monkeypatch):
    backend, connect = database
    conn = connect()
    try:
        _migrate_to(conn, backend, monkeypatch, 55)
        _seed_request(conn, "normal")
        conn.commit()
        for update in _NULL_BYPASSES:
            conn.execute("SAVEPOINT null_bypass")
            # 冻结的 v55 确实错误放行；每次回滚，不把非法状态带到后续迁移测试。
            conn.execute(update)
            row = conn.execute("SELECT * FROM verification_requests WHERE id = 'normal'").fetchone()
            assert row["purge_state"] is None
            assert row["project_id"] is None or row["purge_error"] is not None
            conn.execute("ROLLBACK TO SAVEPOINT null_bypass")
            conn.execute("RELEASE SAVEPOINT null_bypass")
        conn.commit()
    finally:
        conn.close()


_COMPLETE_REQUEST = (
    "UPDATE verification_requests SET purge_state = 'completed', purge_error = NULL, purge_batch_ids_json = NULL, "
    "external_id = NULL, mode = NULL, project_id = NULL, intent_id = NULL, report_type = NULL, report_resource_id = NULL, "
    "worker_template_id = NULL, current_task_run_id = NULL, claim_token = NULL, cancel_requested_at = NULL, "
    "terminal_kind = NULL, terminal_digest = NULL, result_json = NULL, error_json = NULL, accepted_at = NULL, "
    "terminal_at = NULL, source_project_id = NULL, source_vulnerability_id = NULL WHERE id = 'completed'"
)


def _seed_valid_states(conn):
    _seed_request(conn, "normal")
    _seed_request(conn, "historical", hidden="2000")
    _seed_request(conn, "pending")
    _seed_request(conn, "completed")
    for name in ("pending", "completed"):
        conn.execute("UPDATE verification_requests SET deleted_at = '2001', purge_state = 'pending', purge_batch_ids_json = '[]', purge_error = 'synthetic retry', deleted_execution_owner_id = 'synthetic-dispatcher' WHERE id = ?", (name,))
    conn.execute(_COMPLETE_REQUEST)
    _seed_request(conn, "successor", previous="completed")
    _seed_request(conn, "result")
    conn.execute("UPDATE verification_requests SET terminal_kind = 'result', terminal_digest = ?, result_json = ?, accepted_at = '2001', terminal_at = '2001' WHERE id = 'result'", ("c" * 64, '{"summary":"保留原始字节","notes":"\\r\\n \\t"}'))


@pytest.mark.parametrize("start_version", [0, 55])
def test_v56_rejects_null_bypasses_and_preserves_valid_states(database, monkeypatch, start_version):
    backend, connect = database
    conn = connect()
    try:
        if start_version:
            _migrate_to(conn, backend, monkeypatch, start_version)
            _seed_valid_states(conn)
            conn.commit()
            before = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM verification_requests").fetchall()}
        else:
            before = None
        _migrate_to(conn, backend, monkeypatch, 56)
        if before is None:
            _seed_valid_states(conn)
        else:
            assert {row["id"]: dict(row) for row in conn.execute("SELECT * FROM verification_requests").fetchall()} == before
        conn.commit()
        assert conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()["version"] == 56
        for update in (*_NULL_BYPASSES,
            "UPDATE verification_requests SET deleted_at = '2001', deleted_execution_owner_id = 'synthetic-owner' WHERE id = 'normal'",
            "UPDATE verification_requests SET mode = NULL WHERE id = 'normal'",
            "UPDATE verification_requests SET result_json = NULL WHERE id = 'result'",
            "UPDATE verification_requests SET deleted_at = NULL WHERE id = 'pending'",
            "UPDATE verification_requests SET purge_batch_ids_json = NULL WHERE id = 'pending'",
            "UPDATE verification_requests SET project_id = NULL WHERE id = 'pending'",
            "UPDATE verification_requests SET purge_state = NULL WHERE id = 'completed'",
            "UPDATE verification_requests SET result_json = '{}' WHERE id = 'completed'",
        ):
            conn.execute("SAVEPOINT invalid_purge")
            try:
                with pytest.raises((sqlite3.IntegrityError, psycopg.errors.CheckViolation)):
                    conn.execute(update)
            finally:
                conn.execute("ROLLBACK TO SAVEPOINT invalid_purge")
                conn.execute("RELEASE SAVEPOINT invalid_purge")
        conn.execute("DELETE FROM projects WHERE id = 'completed'")
        assert conn.execute("SELECT previous_request_id FROM verification_requests WHERE id = 'successor'").fetchone()["previous_request_id"] == "completed"
        assert conn.execute("SELECT project_id,deleted_at,purge_state FROM verification_requests WHERE id = 'historical'").fetchone()["purge_state"] is None
        assert conn.execute("SELECT 1 FROM projects WHERE id = 'historical'").fetchone()
        if backend == "sqlite":
            assert list(conn.execute("PRAGMA foreign_key_check")) == []
        conn.commit()
    finally:
        conn.close()


def test_v55_checksum_remains_frozen():
    expected = "70a2a722b92d63a56b804315d29a7fbf95c901b0de8ef724ffc2b4664a597cbe"
    assert migrations.SQLITE_VERIFICATION_REQUEST_PURGE.checksum == expected
    assert migrations.POSTGRES_VERIFICATION_REQUEST_PURGE.checksum == expected
    assert sha256(migrations.VERIFICATION_REQUEST_PURGE_V55.encode("utf-8")).hexdigest() == expected
    assert migrations.SQLITE_VERIFICATION_REQUEST_PURGE_NULL_CHECKS.checksum != expected
