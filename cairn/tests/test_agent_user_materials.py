"""真实 HTTP/业务事务验证附件与整理会话，模型由显式 finish 替身完成。"""

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from cairn.server import db
from cairn.server.app import app
from cairn.server.auth import create_machine_principal
from cairn.server.models import CreateVulnerabilityRequest
from cairn.server.schemas.verification_requests import UpdateVerificationConfiguration
from cairn.server.services.verification_configuration import update_verification_configuration
from cairn.server.services.vulnerabilities import create_vulnerability
from cairn.server.services.time_utils import utcnow
from test_agent_task_actions import action_context
from test_postgres_bootstrap import empty_postgres_url, postgres_instance

pytestmark = pytest.mark.skipif(os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1", reason="仅使用一次性 PostgreSQL")


def test_attachment_report_retest_and_deleted_history(action_context, tmp_path, monkeypatch):
    actor, initial = action_context
    monkeypatch.setenv("CAIRN_PROJECT_RESOURCE_ROOT", str(tmp_path / "resources"))
    monkeypatch.setenv("CAIRN_TRUSTED_ORIGINS", "http://testserver")
    client = TestClient(app)
    login = client.post("/auth/login", json={"login": "task-action-user", "password": "local-test-password"})
    assert login.status_code == 200
    headers = {"Origin": "http://testserver", "X-Cairn-CSRF-Token": login.json()["csrf_token"]}
    machine_id, token = create_machine_principal("materials-test")
    machine = {"Authorization": "Bearer " + token}
    pid, cid = initial["project_id"], initial["conversation_id"]
    root = f"/projects/{pid}/agent-conversations"
    path = root + "/" + cid
    claim_body = {"task_run": {"task_type": "agent_chat", "worker_name": "project-agent", "worker_type": "pi"}}

    def claim():
        response = client.post(path + "/claim", json=claim_body, headers=machine)
        assert response.status_code == 200, response.text
        return response.json()

    def finish(run):
        response = client.post(path + f"/turns/{run['message_id']}/finish", headers=machine,
                               json={"task_run_id": run["task_run_id"], "status": "succeeded", "text": "受控处理完成"})
        assert response.status_code == 200, response.text

    assert client.delete(path, headers=headers).status_code == 409
    finish(claim())
    original = "名称,数量\r\n甲,2\r\n乙,3\r\n".encode()
    upload = client.post(path + "/attachments?filename=sample.csv", content=original, headers=headers)
    assert upload.status_code == 201, upload.text
    attachment = upload.json()
    assert attachment["message_id"] is None
    assert client.get(attachment["download_url"]).content == original
    assert TestClient(app).get(attachment["download_url"]).status_code == 401
    assert client.get(path + "/attachments").json() == [attachment]
    discarded = client.post(path + "/attachments?filename=discard.txt", content=b"discard", headers=headers).json()
    assert client.delete(path + "/attachments/" + discarded["id"], headers=headers).status_code == 204
    assert client.get(discarded["download_url"]).status_code == 404
    # 上传到活动研究项目并不导入研究资源，也不要求暂停。
    with db.get_conn() as conn:
        assert conn.execute("SELECT status FROM projects WHERE id=?", (pid,)).fetchone()["status"] == "active"
        assert conn.execute("SELECT count(*) AS n FROM project_resources WHERE project_id=?", (pid,)).fetchone()["n"] == 0
    request = {"client_message_id": str(uuid4()), "text": "按附件计算总数并生成指定报告", "attachment_ids": [attachment["id"]]}
    sent = client.post(path + "/messages", json=request, headers=headers)
    assert sent.status_code == 200, sent.text
    assert client.post(path + "/messages", json=request, headers=headers).json()["id"] == sent.json()["id"]
    assert client.post(path + "/messages", json={**request, "attachment_ids": []}, headers=headers).status_code == 409
    assert client.delete(path, headers=headers).status_code == 409
    assert client.delete(path + "/attachments/" + attachment["id"], headers=headers).status_code == 409
    run = claim()
    assert run["attachments"][0]["id"] == attachment["id"]
    turn = path + f"/turns/{run['message_id']}"

    def tool(method, params, call_id):
        return client.post(turn + "/tools", headers=machine, json={"task_run_id": run["task_run_id"],
                           "method": method, "params": params, "tool_call_id": call_id})

    read = tool("read_conversation_attachment", {"attachment_id": attachment["id"]}, "read")
    assert read.status_code == 200, read.text
    assert read.json()["content"].encode() == original
    assert tool("list_conversation_attachments", {}, "list").json()["total"] == 1
    markdown = "# 合成统计\n\n## 总数\n5\n\n## 来源与范围\nsample.csv；仅合成材料，未访问目标。\n"
    params = {"title": "合成统计", "content": markdown, "sources": [attachment["id"]],
              "read_at": read.json()["read_at"], "limitations": ["仅合成材料，未访问目标"]}
    report = tool("publish_custom_markdown", params, "publish")
    assert report.status_code == 200, report.text
    artifact = report.json()["artifact"]
    assert tool("publish_custom_markdown", params, "publish").json()["artifact"] == artifact
    assert tool("publish_custom_markdown", {**params, "content": "changed"}, "publish").status_code == 409
    assert client.get(artifact["download_url"]).text == markdown
    assert TestClient(app).get(artifact["download_url"]).status_code == 401
    blocked = tool("publish_custom_markdown", {**params, "content": "synthetic-action-test-key"}, "blocked")
    assert blocked.status_code == 422
    with db.get_conn() as conn:
        model_id = conn.execute("SELECT model_id FROM agent_conversations WHERE id=?", (cid,)).fetchone()["model_id"]
        conn.execute("INSERT INTO dispatcher_status(id,status,updated_at,runtime_json) VALUES (?,'idle',?,?)",
                     (machine_id, utcnow(), '{"verification_capacity":2}'))
        vuln = create_vulnerability(conn, pid, CreateVulnerabilityRequest(title="合成发现", severity="low", source_fact_id="origin"))
    update_verification_configuration(UpdateVerificationConfiguration(model_id=model_id), actor)
    retest_params = {"vulnerability_id": vuln.id, "attachment_ids": [attachment["id"]]}
    retest = tool("request_vulnerability_retest", retest_params, "retest")
    assert retest.status_code == 200, retest.text
    result = retest.json()
    assert tool("request_vulnerability_retest", retest_params, "retest").json()["verification_request_id"] == result["verification_request_id"]
    with db.get_conn() as conn:
        rows = conn.execute("SELECT path,size_bytes FROM project_resources WHERE project_id=?", (result["project_id"],)).fetchall()
        assert any(row["path"] == "sample.csv" and row["size_bytes"] == len(original) for row in rows)
        assert conn.execute("SELECT count(*) AS n FROM verification_requests").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM task_runs WHERE project_id=?", (result["project_id"],)).fetchone()["n"] == 0
    # 终止待确认仍 running 时不可整理；只有独立复测排队不阻止已结束聊天。
    assert client.delete(path, headers=headers).status_code == 409
    finish(run)
    resumed = client.post(path + "/messages", headers=headers, json={"client_message_id": str(uuid4()), "text": "续聊读取之前附件"})
    assert resumed.status_code == 200
    run = claim()
    assert run["resume"] is True
    turn = path + f"/turns/{run['message_id']}"
    assert tool("read_conversation_attachment", {"attachment_id": attachment["id"]}, "read-later").json()["content"].encode() == original
    finish(run)
    with db.get_conn() as conn:
        counts = {table: conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
                  for table in ("agent_messages", "task_runs", "artifacts", "verification_requests")}
    assert client.delete(path, headers=headers).status_code == 204
    assert client.delete(path, headers=headers).status_code == 204
    assert client.get(root).json() == []
    for suffix in ("/messages", "/actions", "/events", "/attachments"):
        assert client.get(path + suffix).status_code == 404
    assert client.post(path + "/messages", json=request, headers=headers).status_code == 404
    assert client.post(path + "/claim", json=claim_body, headers=machine).status_code == 404
    assert all(item["conversation_id"] != cid for item in client.get("/agent-turns/pending", headers=machine).json())
    assert client.get(artifact["download_url"]).text == markdown
    with db.get_conn() as conn:
        assert counts == {table: conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] for table in counts}
    client.close()


def test_sse_rotation_and_explicit_auth_failure(action_context, monkeypatch):
    from cairn.server.routers import agent_conversations as routes

    _actor, initial = action_context
    monkeypatch.setenv("CAIRN_TRUSTED_ORIGINS", "http://testserver")
    client = TestClient(app)
    client.post("/auth/login", json={"login": "task-action-user", "password": "local-test-password"})
    path = f"/projects/{initial['project_id']}/agent-conversations/{initial['conversation_id']}/events"
    monkeypatch.setattr(routes.time, "sleep", lambda _: None)
    response = client.get(path)
    assert response.status_code == 200
    assert 'event: close\ndata: {"reason":"rotate"}' in response.text
    assert "event: error" not in response.text
    monkeypatch.setattr(routes, "resolve_session", lambda _token: None)
    response = client.get(path)
    assert 'event: error\ndata: {"status":401' in response.text
    assert "event: close" not in response.text
    client.close()
