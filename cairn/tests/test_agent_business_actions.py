"""R2a 使用一次性 PostgreSQL 的真实业务服务；不调用模型或访问目标。"""

import base64
import json
import os
from time import perf_counter
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from cairn.server import db
from cairn.server.app import app
from cairn.server.auth import RequestPrincipal, create_machine_principal, create_user_with_personal_tenant
from cairn.server.models import CreateArtifactRequest, CreateVulnerabilityRequest
from cairn.server.schemas.models import CreateTenantModelRequest
from cairn.server.schemas.tenant_workers import UpdateTenantWorkerRequest
from cairn.server.services import model_credentials
from cairn.server.services.audit_artifacts import create_artifact_record
from cairn.server.services.tenant_models import create_tenant_model
from cairn.server.services.tenant_workers import list_tenant_workers, update_tenant_worker
from cairn.server.services.time_utils import utcnow
from cairn.server.services.vulnerabilities import create_vulnerability, read_vulnerability_evidence
from test_postgres_bootstrap import empty_postgres_url, postgres_instance
from test_report_templates import _seed_project

pytestmark = pytest.mark.skipif(os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1", reason="仅使用显式启用的一次性 PostgreSQL")


def test_agent_business_actions_use_real_actor_sources_and_generation(empty_postgres_url, tmp_path, monkeypatch):
    db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")
    monkeypatch.setenv("CAIRN_PROJECT_RESOURCE_ROOT", str(tmp_path / "resources"))
    monkeypatch.setenv("CAIRN_TRUSTED_ORIGINS", "http://testserver")
    keyring = tmp_path / "keyring.json"
    keyring.write_text(json.dumps({"active_version": "test", "keys": {"test": base64.b64encode(b"a" * 32).decode()}}))
    keyring.chmod(0o600)
    monkeypatch.setenv(model_credentials.KEYRING_FILE_ENV, str(keyring))
    uid, tenant = create_user_with_personal_tenant("r2a-owner", "local-test-password")
    aid, _ = create_user_with_personal_tenant("r2a-admin", "local-test-password", role="super_admin")
    owner = RequestPrincipal("user", uid, tenant_id=tenant, role="user")
    machine_id, machine_token = create_machine_principal("r2a-dispatcher")
    model = create_tenant_model(CreateTenantModelRequest(
        display_name="受控模型", protocol="openai-chat-completions", base_url="http://127.0.0.1:1",
        model_name="controlled", api_key="synthetic-r2a-secret"), principal=owner)
    worker = list_tenant_workers(tenant_id=tenant)[0]
    worker = update_tenant_worker(worker.id, UpdateTenantWorkerRequest(model_id=model.id,
        enabled=True, max_running=2, priority=1, expected_revision=worker.revision), principal=owner)
    with db.get_conn() as conn:
        _seed_project(conn)
        conn.execute("UPDATE projects SET tenant_id = ? WHERE id = 'p1'", (tenant,))
        conn.execute("INSERT INTO facts(id,project_id,description,creator,created_at) VALUES ('origin','p1','仅使用本地合成材料，不访问目标。','system',?)", (utcnow(),))
        conn.execute("INSERT INTO project_tenant_workers(project_id,tenant_id,tenant_worker_id,tenant_worker_revision,position,created_at) VALUES ('p1',?,?,?,0,?)",
                     (tenant, worker.id, worker.revision, utcnow()))
        conn.execute("INSERT INTO facts(id,project_id,description,creator,created_at) VALUES ('f001','p1','合成历史发现','system',?)", (utcnow(),))
        vuln = create_vulnerability(conn, "p1", CreateVulnerabilityRequest(title="受控发现", severity="high", source_fact_id="f001", remediation="仅合成修复建议"))
        observation = read_vulnerability_evidence(conn, "p1", vuln.id)["observation_id"]
    old_artifact = create_artifact_record("p1", CreateArtifactRequest(kind="report", filename="previous.md", content="# 历史受控报告\n"))
    with db.get_conn() as conn:
        conn.execute("INSERT INTO assessment_report_finalizations(project_id,version,status,generation_started_at,generation_token,accepted_generation_token,accepted_artifact_id,created_at,updated_at) VALUES ('p1',1,'ready',?,'previous-generation','previous-generation',?,?,?)",
                     (utcnow(), old_artifact.id, utcnow(), utcnow()))

    def login(name):
        client = TestClient(app)
        response = client.post("/auth/login", json={"login": name, "password": "local-test-password"})
        assert response.status_code == 200, response.text
        return client, {"Origin": "http://testserver", "X-Cairn-CSRF-Token": response.json()["csrf_token"]}

    client, headers = login("r2a-owner")
    admin_client, admin_headers = login("r2a-admin")
    machine_headers = {"Authorization": "Bearer " + machine_token}
    root = "/projects/p1/agent-conversations"
    configured = client.put("/project-agent/configuration", json={"model_id": model.id}, headers=headers)
    assert configured.status_code == 200, configured.text
    created = admin_client.post(root, json={}, headers=admin_headers)
    assert created.status_code == 201, created.text
    path = root + "/" + created.json()["id"]

    def start_turn(sender, sender_headers, text):
        response = sender.post(path + "/messages", json={"client_message_id": str(uuid4()), "text": text}, headers=sender_headers)
        assert response.status_code == 200, response.text
        claimed = client.post(path + "/claim", json={"task_run": {"task_type": "agent_chat", "worker_name": "project-agent", "worker_type": "pi"}}, headers=machine_headers)
        assert claimed.status_code == 200, claimed.text
        run = claimed.json()
        return path + f"/turns/{response.json()['id']}", run

    def call(turn, run, method, params, call_id):
        return client.post(turn + "/tools", json={"task_run_id": run["task_run_id"], "method": method,
            "params": params, "tool_call_id": call_id}, headers=machine_headers)

    def finish(turn, run):
        response = client.post(turn + "/finish", json={"task_run_id": run["task_run_id"], "status": "succeeded", "text": "已受理，可继续聊天。"}, headers=machine_headers)
        assert response.status_code == 200, response.text

    turn, run = start_turn(client, headers, "查询旧动作并生成项目综合报告")
    params = {"vulnerability_id": vuln.id, "observation_id": observation}
    with db.get_conn() as conn:
        # 直接放入历史回执，不调用已关闭的创建入口或任何复测执行服务。
        conn.execute("INSERT INTO agent_actions(id,message_id,tool_call_id,method,params_json,kind,verification_request_id,source_vulnerability_id) "
                     "VALUES ('old-retest',?,'old-retest','request_vulnerability_retest',?,'verification','deleted-request',?)",
                     (run["message_id"], json.dumps(params), vuln.id))
        conn.execute("INSERT INTO agent_actions(id,message_id,tool_call_id,method,params_json,kind,business_project_id) "
                     "VALUES ('old-project',?,'old-project','create_project','{}','project','p1')", (run["message_id"],))
        conn.execute("INSERT INTO agent_actions(id,message_id,tool_call_id,method,params_json,kind,business_project_id,intent_id) "
                     "VALUES ('old-intent',?,'old-intent','append_project_intent','{}','intent','p1','deleted-intent')", (run["message_id"],))
    old_works = {}
    for action_id in ('old-retest', 'old-project', 'old-intent'):
        query = call(turn, run, "query_agent_work", {"action_id": action_id}, "sdk-query-" + action_id)
        assert query.status_code == 200, query.text
        old_works[action_id] = query.json()["work"]
    assert old_works['old-retest']['status'] == 'unavailable'
    assert old_works['old-project']['status'] == 'succeeded'
    assert old_works['old-intent']['status'] == 'unavailable'
    # 历史调用 ID 也不能重放已关闭动作。
    denied = call(turn, run, "request_vulnerability_retest", params, "old-retest")
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "agent_tool_not_available"
    history = client.get(path + "/messages").json()
    assert len(history[0]["actions"]) == 3 and history[0]["status"] == "running"

    report = call(turn, run, "request_project_report", {}, "sdk-report")
    assert report.status_code == 200, report.text
    report = report.json()
    assert report["generation_token"] != "previous-generation"
    assert report["work"]["status"] == "queued" and report["work"]["download_url"] is None
    assert call(turn, run, "request_project_report", {}, "sdk-report").json()["generation_token"] == report["generation_token"]
    claim_report = client.post("/projects/p1/assessment-report-finalization/claim", json={"worker": worker.id, "generation_token": report["generation_token"]}, headers=machine_headers)
    assert claim_report.status_code == 200, claim_report.text
    started = perf_counter()
    packet = client.get("/projects/p1/assessment-report-packet", params={"generation_token": report["generation_token"]}, headers=machine_headers)
    elapsed = perf_counter() - started
    assert packet.status_code == 200, packet.text
    print(f"R2a synthetic real Packet HTTP: {elapsed:.3f}s")
    assert old_artifact.id not in {item["id"] for item in packet.json().get("artifacts", [])}
    def report_work():
        response = call(turn, run, "query_agent_work", {"action_id": report["action_id"]}, "sdk-report-query")
        assert response.status_code == 200, response.text
        projected = response.json()["work"]
        visible = {item["id"]: item for item in client.get(path + "/actions").json()}
        assert visible[report["action_id"]] == projected
        return projected

    def finish_report(claim, error_type):
        response = client.post("/projects/p1/assessment-report-finalization/finish", json={
            "protocol": "cairn-assessment-report-finish-v1", "worker": worker.id,
            "generation_token": claim["generation_token"], "claim_token": claim["claim_token"],
            "task_run_id": claim["task_run_id"], "status": "failed",
            "task_run_result": {"status": "failed", "exit_code": 1, "error_type": error_type,
                                "error_message": "synthetic-generation-failed"},
        }, headers=machine_headers)
        assert response.status_code == 200, response.text

    # 由原 finish 服务裁决可重试并保存退避；页面与工具只读取同一投影。
    finish_report(claim_report.json(), "container_failed")
    retrying = report_work()
    with db.get_conn() as conn:
        original = conn.execute("SELECT status,next_attempt_at,last_error FROM assessment_report_finalizations WHERE project_id='p1'").fetchone()
        assert original["status"] == "failed" and original["next_attempt_at"]
        assert retrying["next_attempt_at"] == original["next_attempt_at"]
        assert retrying["error"] == original["last_error"]
        conn.execute("UPDATE assessment_report_finalizations SET next_attempt_at='2000-01-01T00:00:00Z' WHERE project_id='p1'")
    assert retrying["status"] == "queued" and retrying["download_url"] is None
    assert retrying["accepted_generation_token"] == "previous-generation"
    assert report_work()["status"] == "queued"  # 到期但尚未 claim，不是最终失败。
    next_claim = client.post("/projects/p1/assessment-report-finalization/claim", json={
        "worker": worker.id, "generation_token": report["generation_token"],
    }, headers=machine_headers)
    assert next_claim.status_code == 200, next_claim.text
    running = report_work()
    assert running["status"] == "running" and running["next_attempt_at"] is None
    finish_report(next_claim.json(), "unknown")
    failed = report_work()
    assert failed["status"] == "failed" and failed["next_attempt_at"] is None
    assert failed["error"] == "synthetic-generation-failed" and failed["download_url"] is None
    assert failed["accepted_generation_token"] == "previous-generation"

    # 仅构造已接受／被替代的权威记录检查投影，不把合成报告当作真实生成验收。
    current_artifact = create_artifact_record("p1", CreateArtifactRequest(kind="report", filename="current.md", content="# 本轮受控报告\n"))
    with db.get_conn() as conn:
        conn.execute("UPDATE assessment_report_finalizations SET status='ready',accepted_generation_token=?,accepted_artifact_id=?,last_error='' WHERE project_id='p1'",
                     (report["generation_token"], current_artifact.id))
    succeeded = report_work()
    assert succeeded["status"] == "succeeded" and succeeded["error"] is None
    assert succeeded["next_attempt_at"] is None
    assert succeeded["download_url"] == f"/projects/p1/artifacts/{current_artifact.id}/content"
    with db.get_conn() as conn:
        conn.execute("UPDATE assessment_report_finalizations SET status='failed',generation_token='newer-generation',next_attempt_at='2099-01-01T00:00:00Z',accepted_generation_token='previous-generation',accepted_artifact_id=? WHERE project_id='p1'",
                     (old_artifact.id,))
    superseded = report_work()
    assert superseded["status"] == "superseded" and superseded["next_attempt_at"] is None
    assert superseded["download_url"] is None

    exported = call(turn, run, "export_vulnerability_report", {"vulnerability_id": vuln.id}, "sdk-export")
    assert exported.status_code == 200, exported.text
    assert client.get(exported.json()["artifact"]["download_url"]).status_code == 200
    with db.get_conn() as conn:
        assert conn.execute("SELECT status FROM vulnerabilities WHERE project_id='p1' AND id=?", (vuln.id,)).fetchone()["status"] == vuln.status
    finish(turn, run)

    visible = {item["id"]: item for item in client.get(path + "/actions").json()}
    assert all(visible[key] == value for key, value in old_works.items())
    assert len(visible) == 4
    assert TestClient(app).get(path + "/actions").status_code == 401
    create_user_with_personal_tenant("r2a-outsider", "local-test-password")
    outsider, _ = login("r2a-outsider")
    assert outsider.get(path + "/actions").status_code == 404
    assert outsider.get(exported.json()["artifact"]["download_url"]).status_code == 404
    outsider.close()
    final_turn, final_run = start_turn(client, headers, "查询复测结果")
    restored = call(final_turn, final_run, "query_agent_work", {"action_id": "old-project"}, "sdk-restored-query")
    assert restored.status_code == 200 and restored.json()["work"] == old_works["old-project"]
    with db.get_conn() as conn:
        conn.execute("UPDATE agent_messages SET submitted_by=NULL,submitted_tenant_id=NULL WHERE id=?", (final_run["message_id"],))
    legacy = call(final_turn, final_run, "request_vulnerability_retest", params, "sdk-legacy")
    assert legacy.status_code == 403 and legacy.json()["detail"]["code"] == "agent_action_actor_unavailable"
    assert len(client.get(path + "/actions").json()) == 4
    client.close()
    admin_client.close()
