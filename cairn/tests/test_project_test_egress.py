"""只验证隔离 PostgreSQL 的出口配置契约，不启动 Worker 或真实任务。"""

from datetime import datetime, timedelta, timezone
import json
import os
from uuid import uuid4

import pytest
from pydantic import ValidationError

from cairn.server import db
from cairn.server.auth import RequestPrincipal, create_machine_principal
from cairn.server.schemas.test_egress import TestEgressInput as EgressInput, egress_request_identity
from cairn.server.services import project_schedules
from cairn.server.services.test_egress import project_egress_input
from traffic_api_fixture import traffic_api, empty_postgres_url, postgres_instance  # noqa: F401


PROXY = {"mode": "http", "host": "proxy.invalid", "port": 8080,
         "auth_action": "replace", "username": "egress-user-unique", "password": "egress-password:unique"}


def test_secret_input_and_request_identity():
    first = EgressInput(**PROXY)
    assert "username" not in first.model_dump() and "password" not in first.model_dump(mode="json")
    assert PROXY["username"] not in repr(first) and PROXY["password"] not in repr(first)
    assert egress_request_identity(first) != egress_request_identity(EgressInput(**(PROXY | {"password": "other"})))
    for changes in ({"username": "invalid:name"}, {"password": "bad\r\nvalue"}, {"password": "bad\0value"},
                    {"username": None}, {"password": None}, {"auth_action": "keep"}, {"host": "http://user:pass@host"}):
        with pytest.raises(ValidationError):
            EgressInput(**(PROXY | changes))


@pytest.mark.skipif(os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1", reason="requires isolated PostgreSQL opt-in")
def test_project_schedule_verification_and_private_endpoint(traffic_api):
    api = traffic_api

    def request(method, path, body=None, expected=200, session=None):
        response = (session or api.user).request(method, api.base_url + path, json=body, timeout=15)
        assert response.status_code == expected, response.text
        return response

    def public_project(pid):
        return request("GET", f"/projects/{pid}").json()["project"]["test_egress"]

    def auth(pid):
        with db.get_conn() as conn:
            body = project_egress_input(conn, pid)
            return body.password.get_secret_value() if body.password else None

    pid = api.project_id
    run_id = api.task_runs[0]["id"]
    path = f"/projects/{pid}/test-egress"
    private_path = f"/projects/{pid}/task-runs/{run_id}/test-egress"
    assert public_project(pid)["mode"] == "direct"
    saved = request("PUT", path, PROXY).json()
    assert saved["auth_configured"] and set(saved) == {"mode", "host", "port", "revision", "auth_configured"}
    request("PUT", path, PROXY, expected=404, session=api.outsider)
    request("POST", private_path, expected=401)
    response = request("POST", private_path, session=api.machine._session())
    assert response.headers["Cache-Control"] == "no-store"
    assert api.machine.resolve_task_test_egress(pid, run_id)["password"] == PROXY["password"]
    _, other_token = create_machine_principal("egress-wrong-dispatcher")
    response = api.user.post(api.base_url + private_path, headers={"Authorization": f"Bearer {other_token}"}, timeout=15)
    assert response.status_code == 403
    request("POST", f"/projects/{api.other_project_id}/task-runs/{run_id}/test-egress", expected=404, session=api.machine._session())
    changed = request("PUT", path, {"mode": "socks5", "host": "new.invalid", "port": 1080}).json()
    assert changed["revision"] > saved["revision"] and auth(pid) == PROXY["password"]
    assert api.machine.resolve_task_test_egress(pid, api.task_runs[1]["id"])["mode"] == "socks5"
    for bad in (PROXY | {"username": "bad:name"}, PROXY | {"port": 0}, PROXY | {"auth_action": "clear"}):
        response = request("PUT", path, bad, expected=422)
        assert PROXY["password"] not in response.text and "egress-user-unique" not in response.text
        assert '"input"' not in response.text
    request("PUT", path, {"mode": "direct"})
    assert auth(pid) is None
    request("PUT", path, PROXY)

    # 创建的幂等摘要必须区分密码，但创建记录和公开响应不能保存只写明文。
    worker = request("GET", f"/projects/{pid}").json()["project"]["tenant_workers"][0]["id"]
    project_body = {"title": "出口契约", "origin": "仅隔离服务契约", "goal": "不执行模型",
                    "scenario": "goal-driven-v1", "tenant_worker_ids": [worker], "authorization_expires_at": None,
                    "test_egress": PROXY}
    create_body = project_body | {"request_id": str(uuid4())}
    created = request("POST", "/projects", create_body, expected=201).json()["project"]
    assert auth(created["id"]) == PROXY["password"]
    assert request("POST", "/projects", create_body, expected=201).json()["project"]["id"] == created["id"]
    request("POST", "/projects", create_body | {"test_egress": PROXY | {"password": "different"}}, expected=409)
    row = api.conn.execute("SELECT test_egress_auth,test_egress_json FROM projects WHERE id=?", (created["id"],)).fetchone()
    assert PROXY["password"] not in str(row) and PROXY["username"] not in str(row)
    # AAD 绑定项目身份，不能把其他项目的密文信封当成本项目认证。
    from cairn.server.services.test_egress import _decrypt
    from fastapi import HTTPException
    tenant = api.conn.execute("SELECT tenant_id FROM projects WHERE id=?", (pid,)).fetchone()["tenant_id"]
    with pytest.raises(HTTPException) as tampered:
        _decrypt(row["test_egress_auth"], tenant_id=tenant, owner_kind="project", owner_id="wrong-project")
    assert tampered.value.status_code == 503

    # 周期计划只保存公共 JSON，认证经独立密文列传给每轮新项目。
    now = datetime.now(timezone.utc)
    start = (now + timedelta(minutes=2)).replace(microsecond=0)
    timing = {"mode": "daily", "timezone": "UTC", "start_at": start.isoformat(), "local_time": start.strftime("%H:%M")}
    schedule = request("POST", "/project-schedules", timing | {"request_id": str(uuid4()), "project": project_body}, expected=201).json()
    sid = schedule["id"]
    assert "test_egress_auth" not in schedule and schedule["project"]["test_egress"]["auth_configured"]
    stored = api.conn.execute("SELECT * FROM project_schedules WHERE id=?", (sid,)).fetchone()
    assert PROXY["password"] not in stored["project_json"] and PROXY["username"] not in stored["project_json"]
    assert "auth_action" not in json.loads(stored["project_json"])["test_egress"]
    member = api.conn.execute("SELECT user_id FROM tenant_memberships WHERE tenant_id=?", (tenant,)).fetchone()
    principal = RequestPrincipal("user", member["user_id"], tenant_id=tenant)
    keep_body = project_body | {"test_egress": {"mode": "socks5", "host": "schedule.invalid", "port": 1080}}
    schedule = request("PUT", f"/project-schedules/{sid}", timing | {"version": schedule["version"], "project": keep_body}).json()
    with db.schedule_transaction() as conn:
        stored = project_schedules._lock_schedule(conn, sid)
        assert project_schedules._consume_locked(conn, stored, datetime.fromisoformat(stored["next_run_at"]), force=True)
    generated = project_schedules.get_schedule(sid, principal)["last_project_id"]
    assert public_project(generated)["mode"] == "socks5" and auth(generated) == PROXY["password"]
    schedule = request("PUT", f"/project-schedules/{sid}", timing | {"version": schedule["version"], "project": project_body | {
        "test_egress": PROXY | {"password": "rotated:password"},
    }}).json()
    stored = api.conn.execute("SELECT * FROM project_schedules WHERE id=?", (sid,)).fetchone()
    assert project_schedules._project_input(stored, with_auth=True).test_egress.password.get_secret_value() == "rotated:password"
    request("PUT", f"/project-schedules/{sid}", timing | {"version": schedule["version"], "project": keep_body | {
        "test_egress": keep_body["test_egress"] | {"auth_action": "clear"},
    }})
    assert api.conn.execute("SELECT test_egress_auth FROM project_schedules WHERE id=?", (sid,)).fetchone()["test_egress_auth"] is None
    once_timing = {"mode": "once", "timezone": "UTC", "start_at": start.isoformat()}
    once = request("POST", "/project-schedules", once_timing | {"request_id": str(uuid4()), "project": project_body}, expected=201).json()
    assert once["test_egress"]["auth_configured"]
    once = request("PUT", f"/project-schedules/{once['id']}", once_timing | {"version": once["version"], "test_egress": {"mode": "direct"}}).json()
    assert once["test_egress"]["mode"] == "direct" and auth(once["project_id"]) is None
    with db.schedule_transaction() as conn:
        assert project_schedules._consume_locked(conn, project_schedules._lock_schedule(conn, once["id"]), start, force=True)
    assert public_project(once["project_id"])["mode"] == "direct"

    # 受控平台来源复测只创建受理记录，不 claim、不运行任务。
    model_id = api.conn.execute("SELECT model_id FROM tenant_workers WHERE id=?", (worker,)).fetchone()["model_id"]
    api.conn.execute("UPDATE dispatcher_status SET runtime_json=?", (json.dumps({"verification_capacity": 1}),))
    request("PUT", "/api/v1/verification-configuration", {"model_id": model_id})
    vulnerability = request("POST", f"/projects/{pid}/vulnerabilities", {"title": "合成主张", "severity": "low"}, expected=201).json()
    preview = request("GET", f"/api/v1/verification-sources/projects/{pid}/vulnerabilities/{vulnerability['id']}/preview").json()
    assert preview["test_egress"]["mode"] == "http" and "test_egress" not in preview["project_context"]
    request("PUT", path, {"mode": "socks5", "host": "current.invalid", "port": 1080})
    verify_body = {"schema_version": 1, "request_id": str(uuid4()), "title": "仅受理", "mode": "retest",
                   "report": {"type": "platform_vulnerability", "project_id": pid, "vulnerability_id": vulnerability["id"]}}
    verified = request("POST", "/api/v1/verification-requests", verify_body, expected=202).json()
    assert verified["test_egress"]["mode"] == "socks5" and auth(verified["project_id"]) == PROXY["password"]
    assert "test_egress" not in verified["origin"] and PROXY["password"] not in json.dumps(verified)
    request("PUT", path, {"mode": "direct"})
    replay = request("POST", "/api/v1/verification-requests", verify_body).json()
    assert replay["test_egress"] == verified["test_egress"]
    request("PUT", path, PROXY)
    copied_batch = request("POST", "/api/v1/verification-resource-batches", {"source_request_id": verified["id"]}, expected=201).json()
    main = next(item for item in copied_batch["members"] if item["source_resource_id"] == verified["report"]["resource_id"])
    copied = request("POST", "/api/v1/verification-requests", verify_body | {
        "request_id": str(uuid4()), "previous_request_id": verified["id"], "resource_batch_id": copied_batch["id"],
        "report": {"type": "file", "batch_member_id": main["id"]},
    }, expected=202).json()
    assert copied["test_egress"]["mode"] == "http" and auth(copied["project_id"]) == PROXY["password"]
    overridden = request("POST", "/api/v1/verification-requests", verify_body | {
        "request_id": str(uuid4()), "test_egress": {"mode": "socks5", "host": "override.invalid", "port": 1080},
    }, expected=202).json()
    assert auth(overridden["project_id"]) == PROXY["password"]
    external = request("POST", "/api/v1/verification-requests", verify_body | {
        "request_id": str(uuid4()), "report": {"type": "text", "content": "仅合成外部资料"},
    }, expected=202).json()
    assert external["test_egress"]["mode"] == "direct"
    # 不把请求明文保存到项目事实、计划 JSON、创建回执或复测材料。
    for query in ("SELECT description FROM facts", "SELECT project_json FROM project_schedules", "SELECT request_digest FROM creation_requests"):
        assert PROXY["password"] not in str(api.conn.execute(query).fetchall())
    api.conn.execute("UPDATE task_runs SET status='succeeded' WHERE project_id=? AND id=?", (pid, run_id))
    request("POST", private_path, expected=409, session=api.machine._session())
