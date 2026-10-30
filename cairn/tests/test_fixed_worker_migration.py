"""固定配置前向迁移保留用户设置、历史来源与请求身份。"""

import json
import os

import pytest

from cairn.server import db, migrations
from test_postgres_bootstrap import postgres_instance, empty_postgres_url

pytestmark = pytest.mark.skipif(
    os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1",
    reason="只使用显式启用的一次性 PostgreSQL",
)


def test_fixed_worker_migration_preserves_existing_settings_and_rejects_ambiguity(
    empty_postgres_url, tmp_path, monkeypatch,
):
    monkeypatch.setattr(db, "POSTGRES_MIGRATIONS", migrations.POSTGRES_MIGRATIONS[:-1])
    db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")
    now = "2026-10-27T00:00:00Z"
    with db.get_conn() as conn:
        conn.execute("INSERT INTO users(id,login,password_hash,role,created_at,updated_at) VALUES ('u','fixture','unused','user',?,?)", (now, now))
        conn.execute("INSERT INTO tenants(id,kind,name,created_at) VALUES ('t','personal','fixture',?)", (now,))
        conn.execute("INSERT INTO tenant_memberships(tenant_id,user_id,role,created_at) VALUES ('t','u','owner',?)", (now,))
        conn.execute("""INSERT INTO tenant_models(id,tenant_id,display_name,protocol,base_url,model_name,created_by,created_at,updated_at)
                        VALUES ('m','t','fixture','anthropic-messages','https://example.test','fixture','u',?,?)""", (now, now))
        conn.execute("""INSERT INTO tenant_workers(id,tenant_id,display_name,model_id,engine,task_types_json,
                        max_running,priority,enabled,revision,created_by,created_at,updated_at)
                        VALUES ('w','t','用户名称','m','pi','["reason", "explore"]',7,-9,0,8,'u',?,?)""", (now, now))
    with pytest.raises(Exception, match="多角色或重复类型"):
        with db.get_conn() as conn:
            migrations.POSTGRES_FIXED_GENERAL_WORKERS.apply(conn)
    with db.get_conn() as conn:
        assert conn.execute("SELECT model_id,max_running FROM tenant_workers WHERE id='w'").fetchone() == {"model_id": "m", "max_running": 7}
        conn.execute("UPDATE tenant_workers SET task_types_json='[\"reason\"]' WHERE id='w'")
        conn.execute("INSERT INTO projects(id,title,status,created_at,scenario_key,tenant_id) VALUES ('p','fixture','paused',?,'goal-driven-v1','t')", (now,))
        conn.execute("INSERT INTO project_tenant_workers(project_id,tenant_id,tenant_worker_id,tenant_worker_revision,position,created_at) VALUES ('p','t','w',2,0,?)", (now,))
        conn.execute("INSERT INTO creation_requests(tenant_id,user_id,operation,request_id,request_digest,project_id,created_at) VALUES ('t','u','project','request-fixture','immutable-digest','p',?)", (now,))
        payload = json.dumps({"tenant_worker_ids": ["w"], "origin": "保留原始输入", "test_egress": {"mode": "direct"}})
        conn.execute("""INSERT INTO project_schedules(id,tenant_id,creator_id,mode,status,version,timezone,
                        start_at,local_time,weekdays_json,next_run_at,project_json,created_at,updated_at)
                        VALUES ('s','t','u','daily','paused',1,'UTC',?,'12:00','[]',?,?,?,?)""", (now, now, payload, now, now))
        migrations.POSTGRES_FIXED_GENERAL_WORKERS.apply(conn)
        original = conn.execute("SELECT * FROM tenant_workers WHERE id='w'").fetchone()
        assert (original["model_id"], original["max_running"], original["priority"], original["enabled"], original["revision"]) == ("m", 7, -9, 0, 8)
        generated = conn.execute("SELECT task_types_json,model_id,max_running FROM tenant_workers WHERE tenant_id='t' AND id<>'w'").fetchall()
        assert sorted((json.loads(w["task_types_json"])[0], w["model_id"], w["max_running"]) for w in generated) == [("bootstrap", None, 1), ("explore", None, 3)]
        bindings = conn.execute("SELECT tenant_worker_id,tenant_worker_revision FROM project_tenant_workers WHERE project_id='p'").fetchall()
        assert len(bindings) == 3 and {"tenant_worker_id": "w", "tenant_worker_revision": 2} in bindings
        stored = json.loads(conn.execute("SELECT project_json FROM project_schedules WHERE id='s'").fetchone()["project_json"])
        assert stored == {"origin": "保留原始输入", "test_egress": {"mode": "direct"}}
        assert conn.execute("SELECT request_digest FROM creation_requests WHERE request_id='request-fixture'").fetchone()["request_digest"] == "immutable-digest"
