"""连续暂停期限只在隔离 PostgreSQL 与受控时钟中验证，不连接研究目标。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os

import pytest
from fastapi.testclient import TestClient

from cairn.server import db, migrations
from cairn.server.app import app
from cairn.server.auth import RequestPrincipal, create_machine_principal
from cairn.server.models import UpdateTaskRunRequest
from cairn.server.schemas.projects import CreateProjectRequest
from cairn.server.services.audit_task_runs import update_task_run_record
from cairn.server.services import project_execution_control as control, project_records
from cairn.server.services.project_runtime_diagnostics import build_project_runtime
from test_agent_task_actions import action_context
from test_postgres_bootstrap import empty_postgres_url, postgres_instance
from test_project_hot_execution_control_postgres import hot_db, project, run, confirm

pytestmark = pytest.mark.skipif(
    os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1", reason="仅使用一次性 PostgreSQL",
)


@pytest.fixture
def clock(monkeypatch):
    values = [datetime.now(timezone.utc).replace(microsecond=0)]

    def now(seconds=0):
        return (values[0] + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")

    class ControlledDateTime:
        @staticmethod
        def now(tz):
            return values[0]

    monkeypatch.setattr(control, "utcnow", now)
    monkeypatch.setattr(project_records, "utcnow", now)
    monkeypatch.setattr(control, "datetime", ControlledDateTime)
    monkeypatch.setattr(project_records, "datetime", ControlledDateTime)
    return values, now


def managed_project(principal):
    # 合法外键链全部放在本测试数据库，避免用不完整记录假装验证托管归属。
    with db.get_conn() as conn:
        conn.execute("INSERT INTO projects(id,title,status,created_at,tenant_id) VALUES ('managed','隔离托管','paused',?,?)",
                     ("2020-01-01T00:00:00Z", principal.tenant_id))
        conn.execute("INSERT INTO intents(id,project_id,description,creator,created_at) VALUES ('i','managed','合成验证','system',?)",
                     ("2020-01-01T00:00:00Z",))
        conn.execute("INSERT INTO project_resources(id,project_id,kind,path,size_bytes,sha256,created_by,created_at) "
                     "VALUES ('input','managed','note','input.txt',0,?,'system',?)",
                     ("0" * 64, "2020-01-01T00:00:00Z"))
        conn.execute("INSERT INTO verification_requests(id,schema_version,tenant_id,caller_type,caller_id,client_request_id,"
                     "request_digest,mode,project_id,intent_id,report_type,report_resource_id,created_at,updated_at) "
                     "VALUES ('request',1,?,'user',?,'synthetic',?,'reproduce','managed','i','text','input',?,?)",
                     (principal.tenant_id, principal.subject_id, "0" * 64, "2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z"))


def test_exact_threshold_finish_reason_and_independent_domains(hot_db, clock):
    values, now = clock
    run("research", "explore", "owner-a", intent_id="claimed")
    run("report", "report", "report-owner")
    run("chat", "agent_chat", "chat-owner")
    with db.get_conn() as conn:
        conn.execute("INSERT INTO intents(id,project_id,description,creator,created_at) VALUES ('open','p','未领取','system',?)", (now(),))
    paused = project_records.project_lifecycle_action("p", "pause").execution_control
    confirm(paused)
    assert project_records.expire_paused_projects(now=now(43199)) == {"finished": 0}
    assert project()["status"] == "paused" and project()["execution_generation"] == 7
    assert project_records.expire_paused_projects(now=now(43200)) == {"finished": 1}
    stopped = project()
    request = control.execution_control(stopped)
    assert stopped["status"] == "stopped" and stopped["coverage_stop_reason"] == "pause_timeout"
    assert stopped["execution_generation"] == 8 and request["action"] == "finish"
    assert request["dispatcher_ids"] == ["owner-a"] and request["state"] == "requested"
    with db.get_conn() as conn:
        runs = conn.execute("SELECT * FROM task_runs WHERE project_id='p'").fetchall()
        assert len(runs) == 3
        assert {row["id"]: row["pause_requested"] for row in runs} == {"research": 1, "report": 0, "chat": 0}
        intent = conn.execute("SELECT * FROM intents WHERE id='open' AND project_id='p'").fetchone()
        assert intent["conclusion_reason"] == "pause_timeout"
        claimed = conn.execute("SELECT * FROM intents WHERE id='claimed' AND project_id='p'").fetchone()
        assert claimed["concluded_at"] is None and claimed["worker"] is not None
        runtime = build_project_runtime(conn, stopped, schema_version=2)
        assert "连续暂停满12小时" in runtime.message
        assert runtime.code != "pause_timeout"
    values[0] += timedelta(seconds=43200)
    owner = RequestPrincipal("machine", "owner-a")
    update_task_run_record("p", "research", UpdateTaskRunRequest(
        status="cancelled", error_type="cancelled", error_message="pause_timeout",
    ), principal=owner)
    confirm(request, state="stopped")
    # 后续真实退出证明增量不能覆盖业务取消原因，也不能更改其他执行域。
    update_task_run_record("p", "research", UpdateTaskRunRequest(runtime_context={"execution_lifecycle": {
        "generation": 7, "process_started": True, "termination_confirmed": True, "cleanup_confirmed": True,
    }}), principal=owner)
    with db.get_conn() as conn:
        retained = conn.execute("SELECT * FROM task_runs WHERE project_id='p' AND id='research'").fetchone()
        assert retained["status"] == "cancelled" and retained["error_message"] == "pause_timeout"
        assert all(row["status"] == "running" for row in conn.execute("SELECT * FROM task_runs WHERE project_id='p' AND id IN ('report','chat')"))
        assert all(row["conclusion_reason"] == "pause_timeout" for row in conn.execute("SELECT * FROM intents WHERE project_id='p'"))
        runtime = build_project_runtime(conn, project(), schema_version=2)
        assert runtime.code == "pause_timeout" and "工作文件保留" in runtime.message
    assert project_records.expire_paused_projects(now=now(90000)) == {"finished": 0}
    assert project_records.project_lifecycle_action("p", "finish").coverage_stop_reason == "pause_timeout"
    assert project()["execution_generation"] == 8
    assert control.execution_control(project())["request_id"] == request["request_id"]


def test_repeated_pause_read_and_parallel_scanners_do_not_renew_or_duplicate(hot_db, clock):
    values, now = clock
    original = project_records.project_lifecycle_action("p", "pause").execution_control
    values[0] += timedelta(seconds=40000)
    assert project_records.project_lifecycle_action("p", "pause").execution_control == original
    assert project_records.list_project_summaries()[0].execution_control == original
    expiry = now(3200)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: project_records.expire_paused_projects(now=expiry), range(2)))
    assert sum(result["finished"] for result in results) == 1
    assert project()["execution_generation"] == 8


@pytest.mark.parametrize("confirm_resume", [False, True])
def test_resume_and_repause_recheck_exact_window_under_lock(hot_db, clock, confirm_resume):
    values, now = clock
    run("research", "explore", "owner-a")
    first = project_records.project_lifecycle_action("p", "pause").execution_control
    confirm(first)
    values[0] += timedelta(seconds=43200)
    with db.get_conn() as conn:
        conn.execute("UPDATE dispatcher_status SET updated_at=?", (now(),))
    resumed = project_records.project_lifecycle_action("p", "resume").execution_control
    assert project()["status"] == "active"
    assert control.execution_control(project())["paused_at"] == first["paused_at"]
    assert project_records.expire_paused_projects(now=now()) == {"finished": 0}
    assert not project_records._finish_expired_pause("p", first["paused_at"], now=now())
    if confirm_resume:
        confirm(resumed, state="running")
        assert control.execution_control(project())["paused_at"] is None
    again = project_records.project_lifecycle_action("p", "pause").execution_control
    assert (again["paused_at"] != first["paused_at"]) is confirm_resume
    assert project_records._finish_expired_pause("p", first["paused_at"], now=now()) is (not confirm_resume)
    assert project()["status"] == ("paused" if confirm_resume else "stopped")


def test_migration_opens_explicit_policy_window_once_and_excludes_managed(hot_db, clock, monkeypatch):
    _, now = clock
    managed_project(hot_db[0])
    preserved = {"paused_at": now(-50000), "action": "pause", "requested_at": now(-1)}
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET status='paused', execution_control_json=? WHERE id='p'", (json.dumps(preserved),))
        for pid, raw in (("missing", {}), ("invalid", {"paused_at": "invalid", "requested_at": "2020-01-01T00:00:00Z"})):
            conn.execute("INSERT INTO projects(id,title,status,created_at,tenant_id,execution_control_json) VALUES (?,?,'paused',?,?,?)",
                         (pid, pid, "2020-01-01T00:00:00Z", hot_db[0].tenant_id, json.dumps(raw)))
        conn.execute("DELETE FROM schema_migrations WHERE version=80")
        monkeypatch.setattr(migrations, "_utcnow", now)
        migrations.verify_or_apply(conn, migrations.POSTGRES_PROJECT_PAUSE_TIMEOUT_CLOCK, lambda _conn: None)
        assert control.execution_control(conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()) == preserved
        assert control.execution_control(conn.execute("SELECT * FROM projects WHERE id='managed'").fetchone()) == {}
        for pid in ("missing", "invalid"):
            current = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
            migrated = control.execution_control(current)
            assert migrated["paused_at"] == now() and migrated["paused_at_source"] == "policy_activation"
            assert not migrated.get("action") and not control.execution_dispatch_blocked(current)
        monkeypatch.setattr(migrations, "_utcnow", lambda: now(60000))
        migrations.verify_or_apply(conn, migrations.POSTGRES_PROJECT_PAUSE_TIMEOUT_CLOCK, lambda _conn: None)
        assert control.execution_control(conn.execute("SELECT * FROM projects WHERE id='missing'").fetchone())["paused_at"] == now()
    # 只有确有旧时钟的普通项目立即到期；历史缺失记录获得完整的新窗口。
    assert project_records.expire_paused_projects(now=now()) == {"finished": 1}
    assert not project_records._finish_expired_pause("managed", now(-50000), now=now())
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET execution_control_json=? WHERE id='managed'", (json.dumps(preserved),))
    assert project_records.expire_paused_projects(now=now(43199)) == {"finished": 0}
    assert project_records.expire_paused_projects(now=now(43200)) == {"finished": 2}
    with db.get_conn() as conn:
        assert conn.execute("SELECT status FROM projects WHERE id='managed'").fetchone()["status"] == "paused"


def test_clock_only_migration_does_not_block_dispatch_only_resume(hot_db, clock, monkeypatch):
    _, now = clock
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET status='paused' WHERE id='p'")
        conn.execute("DELETE FROM schema_migrations WHERE version=80")
        monkeypatch.setattr(migrations, "_utcnow", now)
        migrations.verify_or_apply(conn, migrations.POSTGRES_PROJECT_PAUSE_TIMEOUT_CLOCK, lambda _conn: None)
    before = control.execution_control(project())
    assert before == {"paused_at": now(), "paused_at_source": "policy_activation"}
    resumed = project_records.project_lifecycle_action("p", "resume")
    assert resumed.status == "active" and resumed.execution_control["state"] == "running"
    assert resumed.execution_control["paused_at"] is None
    assert "paused_at_source" not in resumed.execution_control


def test_initial_paused_creation_uses_same_clock_without_execution(action_context, clock):
    actor, _ = action_context
    values, now = clock
    detail = project_records.create_project_record(CreateProjectRequest(
        title="只创建不执行", origin="合成材料", goal="合成目标", authorization_expires_at=None,
    ), tenant_id=actor.tenant_id, creator=actor.subject_id, initial_status="paused")
    pid = detail.project.id
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        first = control.execution_control(row)
        assert first["paused_at"] == row["created_at"] == now()
        assert first["state"] == "paused" and first["dispatcher_ids"] == []
        for table in ("task_runs", "project_container_activations"):
            assert conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE project_id=?", (pid,)).fetchone()["n"] == 0
    values[0] += timedelta(seconds=43199)
    assert project_records.project_lifecycle_action(pid, "pause").execution_control == first
    assert project_records.expire_paused_projects(now=now()) == {"finished": 0}
    assert project_records.expire_paused_projects(now=now(1)) == {"finished": 1}


def test_machine_only_expiry_endpoint_uses_server_time(hot_db, clock):
    values, _ = clock
    project_records.project_lifecycle_action("p", "pause")
    _, token = create_machine_principal("timeout-scanner")
    client = TestClient(app)
    path = "/projects/pause-timeouts/expire"
    assert client.post(path).status_code == 401
    headers = {"Authorization": "Bearer " + token}
    assert client.post(path, headers=headers, json={"now": "2099-01-01T00:00:00Z"}).json() == {"finished": 0}
    values[0] += timedelta(seconds=43200)
    response = client.post(path, headers=headers)
    assert response.status_code == 200 and response.json() == {"finished": 1}
