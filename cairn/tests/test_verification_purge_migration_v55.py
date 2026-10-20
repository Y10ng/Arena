"""v55 双后端追加迁移：不回填旧软删，不放松正常请求约束。"""
from __future__ import annotations

import json

import pytest

from cairn.server import db
from test_assessment_migration_v34 import database, private_postgres


def test_v54_upgrade_preserves_existing_rows_and_self_references(database, monkeypatch, tmp_path):
    backend, connect = database
    conn = connect()
    registry = "SQLITE_MIGRATIONS" if backend == "sqlite" else "POSTGRES_MIGRATIONS"
    try:
        with monkeypatch.context() as patch:
            patch.setattr(db, registry, tuple(migration for migration in getattr(db, registry) if migration.version <= 54))
            db.run_migrations(conn, backend)
        for name, previous, hidden in (("parent", None, None), ("child", "parent", None), ("legacy-hidden", None, "2026-10-01")):
            conn.execute("INSERT INTO projects(id,title,status,created_at,tenant_id) VALUES (?,?,'completed','2000','tenant_legacy')", (name, name))
            conn.execute("INSERT INTO intents(id,project_id,description,creator,created_at) VALUES ('i001',?,'synthetic','synthetic','2000')", (name,))
            conn.execute("INSERT INTO project_resources(id,project_id,kind,path,size_bytes,sha256,created_by,created_at) VALUES ('r001',?,'note','report.txt',1,?,'synthetic','2000')", (name, "a" * 64))
            result = json.dumps({"outcome": "insufficient_evidence", "summary": "保留原始历史 JSON", "notes": "\r\n \t"}, ensure_ascii=False)
            conn.execute("INSERT INTO verification_requests(id,schema_version,tenant_id,caller_type,caller_id,client_request_id,request_digest,mode,project_id,intent_id,report_type,report_resource_id,previous_request_id,terminal_kind,terminal_digest,result_json,terminal_at,created_at,updated_at,deleted_at) VALUES (?,1,'tenant_legacy','user','synthetic',?,?,'retest',?,'i001','text','r001',?,'result',?,?,'2000','2000','2000',?)", (name, name, "b" * 64, name, previous, "c" * 64, result, hidden))
        conn.commit()
        before = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM verification_requests").fetchall()}
        sentinel = tmp_path / "legacy-owned-file"
        sentinel.write_bytes(b"not authorized for automatic migration purge")
        with monkeypatch.context() as patch:
            patch.setattr(db, registry, tuple(migration for migration in getattr(db, registry) if migration.version <= 55))
            db.run_migrations(conn, backend)
        rows = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM verification_requests").fetchall()}
        assert all({key: rows[name][key] for key in original} == original for name, original in before.items())
        assert all(row["purge_state"] is None and row["purge_error"] is None and row["purge_batch_ids_json"] is None for row in rows.values())
        assert rows["child"]["previous_request_id"] == "parent" and rows["legacy-hidden"]["deleted_at"] == "2026-10-01"
        assert sentinel.read_bytes() == b"not authorized for automatic migration purge"
        assert conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()["version"] == 55
        for invalid in (
            "UPDATE verification_requests SET project_id = NULL WHERE id = 'parent'",
            "UPDATE verification_requests SET mode = NULL WHERE id = 'parent'",
            "UPDATE verification_requests SET result_json = NULL WHERE id = 'parent'",
            "UPDATE verification_requests SET purge_state = 'pending', deleted_at = '2001' WHERE id = 'parent'",
            "UPDATE verification_requests SET purge_state = 'completed', deleted_at = '2001', purge_batch_ids_json = NULL WHERE id = 'parent'",
        ):
            conn.execute("SAVEPOINT invalid_purge")
            with pytest.raises(Exception):
                conn.execute(invalid)
            conn.execute("ROLLBACK TO SAVEPOINT invalid_purge")
            conn.execute("RELEASE SAVEPOINT invalid_purge")
        conn.execute("UPDATE verification_requests SET deleted_at = '2001', purge_state = 'pending', purge_batch_ids_json = '[]' WHERE id = 'parent'")
        conn.execute("UPDATE verification_requests SET purge_state = 'completed', purge_batch_ids_json = NULL, external_id = NULL, mode = NULL, project_id = NULL, intent_id = NULL, report_type = NULL, report_resource_id = NULL, worker_template_id = NULL, current_task_run_id = NULL, claim_token = NULL, cancel_requested_at = NULL, terminal_kind = NULL, terminal_digest = NULL, result_json = NULL, error_json = NULL, accepted_at = NULL, terminal_at = NULL, source_project_id = NULL, source_vulnerability_id = NULL WHERE id = 'parent'")
        conn.execute("DELETE FROM projects WHERE id = 'parent'")
        assert conn.execute("SELECT previous_request_id FROM verification_requests WHERE id = 'child'").fetchone()["previous_request_id"] == "parent"
        assert conn.execute("SELECT project_id FROM verification_requests WHERE id = 'legacy-hidden'").fetchone()["project_id"] == "legacy-hidden"
        if backend == "sqlite":
            assert list(conn.execute("PRAGMA foreign_key_check")) == []
        conn.commit()
    finally:
        conn.close()
