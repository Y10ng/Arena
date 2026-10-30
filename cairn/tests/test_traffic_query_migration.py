"""仅在新建私有 PostgreSQL 验证流量查询及地址派生迁移，不修改业务材料。"""

import json
import os

import pytest

from cairn.server import db, migrations
from test_postgres_bootstrap import empty_postgres_url, postgres_instance  # noqa: F401
from test_traffic_query import NOW, _record


pytestmark = pytest.mark.skipif(os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1",
                                reason="requires disposable PostgreSQL opt-in")


def test_query_migration_backfills_without_rewriting_records(empty_postgres_url, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "POSTGRES_MIGRATIONS", tuple(m for m in migrations.POSTGRES_MIGRATIONS if m.version <= 66))
    db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")
    scope = {"id": "scope", "project_id": "query-migration", "task_run_id": "run", "generation": 0,
             "container_id": "synthetic"}
    record = json.dumps(_record(scope, "flow"), ensure_ascii=False, indent=2)
    with db.get_conn() as conn:
        conn.execute("INSERT INTO projects(id, title, status, created_at, tenant_id) "
                     "VALUES ('query-migration', '合成迁移样本', 'paused', ?, 'tenant_legacy')", (NOW,))
        conn.execute("INSERT INTO task_runs(id, project_id, task_type, worker_name, worker_type, status) "
                     "VALUES ('run', 'query-migration', 'explore', 'test', 'pi', 'completed')")
        conn.execute("INSERT INTO http_capture_scopes(id, project_id, task_run_id, generation, container_id, dispatcher_id, created_at) "
                     "VALUES ('scope', 'query-migration', 'run', 0, 'synthetic', 'synthetic', ?)", (NOW,))
        conn.execute("INSERT INTO http_flows(id, project_id, capture_scope_id, revision, record_json, record_sha256, state, method, host, "
                     "created_cursor, updated_cursor, received_at, updated_at) "
                     "VALUES ('flow', 'query-migration', 'scope', 1, ?, ?, 'receiving', 'GET', 'Synthetic.Invalid', 1, 1, ?, ?)",
                     (record, "a" * 64, NOW, NOW))
        migrations.verify_or_apply(conn, migrations.POSTGRES_HTTP_TRAFFIC_QUERY, lambda _: None)
        flow = conn.execute("SELECT * FROM http_flows WHERE project_id = 'query-migration'").fetchone()
        assert flow["request_url"] == "http://Synthetic.Invalid:8080/api/Item?q=one%25"
        assert flow["request_path"] == "/api/Item"
        assert flow["record_json"] == record and flow["record_sha256"] == "a" * 64
        assert flow["received_at"] == NOW and flow["updated_at"] == NOW
        assert flow["created_cursor"] == flow["updated_cursor"] == 1
        assert conn.execute("SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1").fetchone()["version"] == 67
        indexes = conn.execute("SELECT indexname FROM pg_indexes WHERE indexname IN "
                               "('idx_http_flows_received', 'idx_http_flows_project_received', 'idx_http_observations_evidence')").fetchall()
        assert len(indexes) == 3
        conn.execute("UPDATE http_flows SET host = '192.0.2.1', request_url = 'http://192.0.2.1/old' "
                     "WHERE project_id = 'query-migration'")
        before = dict(conn.execute("SELECT * FROM http_flows WHERE project_id = 'query-migration'").fetchone())
        migrations.verify_or_apply(conn, migrations.POSTGRES_HTTP_TRAFFIC_ADDRESS, lambda _: None)
        after = dict(conn.execute("SELECT * FROM http_flows WHERE project_id = 'query-migration'").fetchone())
        assert after["host"] == "synthetic.invalid"
        assert after["request_url"] == "http://Synthetic.Invalid:8080/api/Item?q=one%25"
        assert {key: value for key, value in before.items() if key not in {"host", "request_url", "request_path"}} == {
            key: value for key, value in after.items() if key not in {"host", "request_url", "request_path"}}
        migrations.verify_or_apply(conn, migrations.POSTGRES_HTTP_TRAFFIC_ADDRESS, lambda _: None)
        assert conn.execute("SELECT max(version) AS version FROM schema_migrations").fetchone()["version"] == 68
