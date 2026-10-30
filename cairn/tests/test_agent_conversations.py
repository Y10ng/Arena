"""R1 的非付费 PostgreSQL/API 闭环，不连接模型或目标。"""

import base64
import json
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from cairn.server import db, migrations
from cairn.server.app import app
from cairn.server.auth import RequestPrincipal, create_machine_principal, create_user_with_personal_tenant
from cairn.server.models import CreateVulnerabilityRequest
from cairn.server.schemas.models import CreateTenantModelRequest
from cairn.server.services import model_credentials
from cairn.server.services.agent_conversations import messages
from cairn.server.services.tenant_models import create_tenant_model
from cairn.server.services.tenant_workers import list_tenant_workers
from cairn.server.services.vulnerabilities import create_vulnerability
from test_postgres_bootstrap import postgres_instance, empty_postgres_url

pytestmark = pytest.mark.skipif(os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1", reason="仅使用显式启用的一次性 PostgreSQL")


def test_migrate_worker_bound_conversations_in_place(empty_postgres_url, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "POSTGRES_MIGRATIONS", tuple(m for m in migrations.POSTGRES_MIGRATIONS if m.version < 74))
    db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")
    uid, tenant = create_user_with_personal_tenant("agent-migration", "local-test-password")
    principal = RequestPrincipal("user", uid, tenant_id=tenant, role="user")
    model = create_tenant_model(CreateTenantModelRequest(display_name="迁移模型", protocol="openai-chat-completions",
        base_url="http://127.0.0.1:1", model_name="controlled"), principal=principal)
    workers = list_tenant_workers(tenant_id=tenant)
    now = "2026-10-29T00:00:00Z"
    session = "pi:" + str(uuid4())
    with db.get_conn() as conn:
        conn.execute("UPDATE tenant_workers SET model_id=? WHERE id=?", (model.id, workers[0].id))
        conn.execute("INSERT INTO projects(id,title,status,created_at,tenant_id) VALUES ('migration','迁移','completed',?,?)", (now, tenant))
        for cid, worker in (("old", workers[0]), ("unconfigured", workers[1])):
            conn.execute("INSERT INTO agent_conversations(id,project_id,worker_id,created_by,session_id,created_at) VALUES (?,'migration',?,?,?,?)",
                         (cid, worker.id, uid, session if cid == "old" else "pi:" + str(uuid4()), now))
        conn.execute("INSERT INTO task_runs(id,project_id,task_type,worker_name,worker_type,status,session_id,runtime_context_json) "
                     "VALUES ('tr-old','migration','agent_chat',?,'pi','succeeded',?,?)",
                     (workers[0].id, session, '{"usage":{"input_tokens":12,"output_tokens":8}}'))
        message_id = conn.execute("INSERT INTO agent_messages(conversation_id,project_id,client_message_id,text,created_at,task_run_id,answer) "
                                  "VALUES ('old','migration',?,'旧消息',?,'tr-old','旧回复') RETURNING id", (str(uuid4()), now)).fetchone()["id"]
        conn.execute("INSERT INTO agent_actions(id,message_id,tool_call_id,method,params_json,kind,generation_token) "
                     "VALUES ('old-action',?,'old-call','request_project_report','{}','report','generation-old')", (message_id,))
        before = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]
                  for table in ("agent_messages", "agent_actions", "task_runs")}
        migrations.POSTGRES_INDEPENDENT_PROJECT_AGENT.apply(conn)
        migrated = conn.execute("SELECT * FROM agent_conversations WHERE id='old'").fetchone()
        assert migrated["model_id"] == model.id and migrated["session_id"] == session
        assert migrated["created_by"] == uid and "worker_id" not in migrated
        assert conn.execute("SELECT model_id FROM agent_conversations WHERE id='unconfigured'").fetchone()["model_id"] is None
        assert conn.execute("SELECT count(*) AS n FROM project_agent_configurations").fetchone()["n"] == 0
        for table, rows in before.items():
            assert [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()] == rows


def test_durable_conversation_tools_and_authorization(empty_postgres_url, tmp_path, monkeypatch):
    db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")
    keyring = tmp_path / "keyring.json"
    keyring.write_text(json.dumps({"active_version": "test", "keys": {"test": base64.b64encode(b"r" * 32).decode()}}))
    keyring.chmod(0o600)
    monkeypatch.setenv(model_credentials.KEYRING_FILE_ENV, str(keyring))
    monkeypatch.setenv("CAIRN_TRUSTED_ORIGINS", "http://testserver")
    uid, tenant = create_user_with_personal_tenant("r1-local", "local-test-password")
    principal = RequestPrincipal("user", uid, tenant_id=tenant, role="user")
    secret = "simulated-provider-secret-R1"
    model = create_tenant_model(CreateTenantModelRequest(display_name="受控模型", protocol="openai-chat-completions",
        base_url="http://127.0.0.1:1", model_name="controlled", api_key=secret), principal=principal)
    _, machine_token = create_machine_principal("r1-test")
    _, wrong_token = create_machine_principal("r1-other")
    now = "2026-10-28T00:00:00Z"
    with db.get_conn() as conn:
        for pid in ("r1", "r1-other"):
            conn.execute("INSERT INTO projects(id,title,status,created_at,tenant_id,scenario_key) VALUES (?,?,'completed',?,?,'goal-driven-v1')", (pid, pid, now, tenant))
        conn.execute("INSERT INTO facts(id,project_id,description,creator,created_at) VALUES ('f001','r1','已保存发现的原始说明','system',?)", (now,))
        vuln = create_vulnerability(conn, "r1", CreateVulnerabilityRequest(title="受控发现", severity="high", source_fact_id="f001", remediation="针对性建议"))
    client = TestClient(app)
    login = client.post("/auth/login", json={"login": "r1-local", "password": "local-test-password"})
    assert login.status_code == 200
    headers = {"Origin": "http://testserver", "X-Cairn-CSRF-Token": login.json()["csrf_token"]}
    machine = {"Authorization": "Bearer " + machine_token}
    wrong = {"Authorization": "Bearer " + wrong_token}
    root = "/projects/r1/agent-conversations"
    assert client.post(root, json={}).status_code == 403
    assert client.post(root, json={}, headers=headers).json()["detail"]["code"] == "agent_model_unconfigured"
    configured = client.put("/project-agent/configuration", json={"model_id": model.id}, headers=headers)
    assert configured.status_code == 200 and configured.json()["available"]
    response = client.post(root, json={}, headers=headers)
    assert response.status_code == 201, response.text
    assert response.json()["model_id"] == model.id and "worker_id" not in response.json()
    cid = response.json()["id"]
    path = root + "/" + cid
    assert client.get("/projects/r1-other/agent-conversations/" + cid + "/messages").status_code == 404
    assert TestClient(app).get(path + "/messages").status_code == 401
    request = {"client_message_id": str(uuid4()), "text": "查询进展并导出已有发现"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(path + "/messages", json=request, headers=headers), range(2)))
    assert all(r.status_code == 200 for r in responses)
    first = responses[0].json()
    assert responses[1].json()["id"] == first["id"]
    assert client.post(path + "/messages", json={**request, "text": "不同内容"}, headers=headers).status_code == 409
    assert client.post(path + "/messages", json={"client_message_id": str(uuid4()), "text": secret}, headers=headers).status_code == 422
    second = client.post(path + "/messages", json={"client_message_id": str(uuid4()), "text": "接着解释"}, headers=headers).json()
    claim_body = {"task_run": {"task_type": "agent_chat", "worker_name": "project-agent", "worker_type": "pi"}}
    claim = client.post(path + "/claim", json=claim_body, headers=machine)
    assert claim.status_code == 200, claim.text
    run = claim.json()
    assert run["message_id"] == first["id"] and run["resume"] is False
    assert run["execution_binding"]["model_id"] == model.id and "worker_id" not in run["execution_binding"]
    with db.get_conn() as conn:
        assert conn.execute("SELECT count(*) AS n FROM project_tenant_workers WHERE project_id='r1'").fetchone()["n"] == 0
    assert client.post(path + "/claim", json=claim_body, headers=machine).status_code == 204
    turn = path + f"/turns/{first['id']}"
    body = {"task_run_id": run["task_run_id"]}
    assert client.post(turn + "/heartbeat", json=body, headers=wrong).status_code == 403
    assert client.post(turn + "/heartbeat", json=body, headers=machine).json() == {"cancelled": False}
    def tool(method, params):
        response = client.post(turn + "/tools", json={**body, "method": method, "params": params, "tool_call_id": str(uuid4())}, headers=machine)
        assert response.status_code == 200, response.text
        assert secret not in response.text
        return response.json()
    assert tool("query_project_progress", {})["status"] == "completed"
    assert tool("list_vulnerabilities", {})["total"] == 1
    detail = tool("read_vulnerability", {"vulnerability_id": vuln.id})
    assert "原始说明" in detail["readable"]["description_markdown"]
    exported = tool("export_vulnerability_report", {"vulnerability_id": vuln.id, "observation_id": detail["observation_id"]})
    download = client.get(exported["artifact"]["download_url"])
    assert download.status_code == 200, download.text
    assert "受控发现" in download.text and "原始说明" in download.text
    assert TestClient(app).get(exported["artifact"]["download_url"]).status_code == 401
    assert client.post(turn + "/tools", json={**body, "method": "read_vulnerability", "params": {"vulnerability_id": vuln.id, "project_id": "r1-other"}, "tool_call_id": str(uuid4())}, headers=machine).status_code == 422
    finish = {**body, "status": "succeeded", "text": "已读取真实进度，成品已保存。", "usage": {"input_tokens": 12, "output_tokens": 8}}
    assert client.post(turn + "/finish", json={**finish, "text": secret}, headers=machine).status_code == 422
    response = client.post(turn + "/finish", json=finish, headers=machine)
    assert response.status_code == 200, response.text
    assert client.post(turn + "/finish", json=finish, headers=machine).status_code == 200
    history = client.get(path + "/messages").json()
    assert [(m["id"], m["status"]) for m in history] == [(first["id"], "succeeded"), (second["id"], "queued")]
    assert history[0]["artifacts"][0] == exported["artifact"]
    with db.get_conn() as conn:
        changes = messages(conn, "r1", cid, revision=first["revision"])
        assert changes[0]["answer"] == finish["text"]
        assert conn.execute("SELECT count(*) AS count FROM task_runs WHERE project_id='r1'").fetchone()["count"] == 1
    # ASGI 实际事件帧按 revision 续读；限定连接寿命，避免测试等待生产轮换周期。
    from cairn.server.routers import agent_conversations as routes
    original_resolve = routes.resolve_session
    with monkeypatch.context() as patch:
        remaining = iter((True, False))
        patch.setattr(routes, "resolve_session", lambda token: original_resolve(token) if next(remaining) else None)
        patch.setattr(routes.time, "sleep", lambda _: None)
        events = client.get(path + "/events", headers={"Last-Event-ID": str(first["revision"])})
    assert events.status_code == 200 and events.headers["content-type"].startswith("text/event-stream")
    frames = events.text.splitlines()
    payload = json.loads(next(line[6:] for line in frames if line.startswith("data: ")))
    assert payload[0]["answer"] == finish["text"]
    assert payload[0]["artifacts"][0] == exported["artifact"]
    assert int(next(line[4:] for line in frames if line.startswith("id: "))) > first["revision"]
    next_run = client.post(path + "/claim", json=claim_body, headers=machine).json()
    assert next_run["message_id"] == second["id"] and next_run["resume"] is True
    assert next_run["session_id"] == run["session_id"]
    assert next_run["source_task_run_id"] == run["task_run_id"]
    finished_second = client.post(path + f"/turns/{second['id']}/finish", json={
        "task_run_id": next_run["task_run_id"], "status": "succeeded", "text": "续聊已完成",
    }, headers=machine)
    assert finished_second.status_code == 200
    queued = client.post(path + "/messages", json={"client_message_id": str(uuid4()), "text": "等待认领"}, headers=headers)
    assert queued.status_code == 200
    with db.get_conn() as conn:
        conn.execute("UPDATE tenant_models SET enabled=0 WHERE id=?", (model.id,))
    unavailable = client.get(root).json()[0]
    assert unavailable["available"] is False and unavailable["unavailable_reason"] == "model_disabled"
    history = client.get(path + "/messages").json()
    assert history[0]["answer"] == finish["text"]
    assert history[-1]["status"] == "queued" and history[-1]["error_code"] == "model_disabled"
    rejected_claim = client.post(path + "/claim", json=claim_body, headers=machine)
    assert rejected_claim.status_code == 422 and rejected_claim.json()["detail"]["code"] == "model_disabled"
    rejected = client.post(path + "/messages", json={"client_message_id": str(uuid4()), "text": "模型停用后发送"}, headers=headers)
    assert rejected.status_code == 422 and rejected.json()["detail"]["code"] == "model_disabled"
    client.close()
