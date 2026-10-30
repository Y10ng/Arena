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
from cairn.server.models import CreateArtifactRequest, CreateTaskRunRequest, CreateVulnerabilityRequest
from cairn.server.schemas.models import CreateTenantModelRequest
from cairn.server.schemas.tenant_workers import UpdateTenantWorkerRequest
from cairn.server.schemas.verification_requests import UpdateVerificationConfiguration, VerificationClaimRequest
from cairn.server.services import model_credentials
from cairn.server.services.audit_artifacts import create_artifact_record
from cairn.server.services.tenant_models import create_tenant_model
from cairn.server.services.tenant_workers import list_tenant_workers, update_tenant_worker
from cairn.server.services.time_utils import utcnow
from cairn.server.services.verification_configuration import update_verification_configuration
from cairn.server.services.verification_requests import claim_verification_request, submit_verification_result
from cairn.server.services.vulnerabilities import create_vulnerability, read_vulnerability_evidence
from test_external_verification_evidence import _submission, _upload
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
    machine = RequestPrincipal("machine", machine_id)
    with db.get_conn() as conn:
        conn.execute("INSERT INTO dispatcher_status(id,status,updated_at,runtime_json) VALUES (?,'idle',?,?)",
                     (machine_id, utcnow(), '{"verification_capacity":2}'))
    model = create_tenant_model(CreateTenantModelRequest(
        display_name="受控模型", protocol="openai-chat-completions", base_url="http://127.0.0.1:1",
        model_name="controlled", api_key="synthetic-r2a-secret"), principal=owner)
    configuration = update_verification_configuration(UpdateVerificationConfiguration(model_id=model.id), owner)
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

    turn, run = start_turn(client, headers, "复测 v001，并生成项目综合报告")
    params = {"vulnerability_id": vuln.id, "observation_id": observation}
    first = call(turn, run, "request_vulnerability_retest", params, "sdk-retest")
    assert first.status_code == 200, first.text
    first = first.json()
    assert first["source_project_id"] == "p1" and first["project_id"] != "p1"
    assert first["source_observation_id"] == observation
    assert first["work"]["status"] == "queued"
    assert first["work"]["result"] is None  # 来源漏洞只是历史输入，不是本次复测结论。
    replay = call(turn, run, "request_vulnerability_retest", params, "sdk-retest")
    assert replay.status_code == 200 and replay.json()["verification_request_id"] == first["verification_request_id"]
    new_target = "https://new.example.test/instance"
    repeated = call(turn, run, "request_vulnerability_retest", {**params, "target_url": new_target}, "sdk-explicit-repeat")
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["verification_request_id"] != first["verification_request_id"]
    target_detail = client.get("/api/v1/verification-requests/" + repeated.json()["verification_request_id"])
    assert target_detail.status_code == 200 and target_detail.json()["target_url"] == new_target
    target_conflict = call(turn, run, "request_vulnerability_retest", {**params, "target_url": new_target}, "sdk-retest")
    assert target_conflict.status_code == 409 and target_conflict.json()["detail"]["code"] == "agent_action_conflict"
    conflict = call(turn, run, "request_vulnerability_retest", {"vulnerability_id": vuln.id}, "sdk-retest")
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "agent_action_conflict"
    with db.get_conn() as conn:
        requests = conn.execute("SELECT caller_id,tenant_id,mode,source_project_id,source_vulnerability_id FROM verification_requests ORDER BY id").fetchall()
        assert len(requests) == 2
        assert all(row["caller_id"] == uid and row["tenant_id"] == tenant and row["mode"] == "retest" for row in requests)
        assert all(row["source_project_id"] == "p1" and row["source_vulnerability_id"] == vuln.id for row in requests)
        message = conn.execute("SELECT submitted_by,answer FROM agent_messages WHERE id = ?", (run["message_id"],)).fetchone()
        assert message["submitted_by"] == uid and message["answer"] is None
        assert conn.execute("SELECT created_by FROM agent_conversations WHERE id = ?", (created.json()["id"],)).fetchone()["created_by"] == aid
    history = client.get(path + "/messages").json()
    assert len(history[0]["actions"]) == 2 and history[0]["status"] == "running"
    query = call(turn, run, "query_agent_work", {"action_id": first["action_id"]}, "sdk-query")
    assert query.status_code == 200 and query.json()["work"]["status"] == "queued"

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

    # 由真实复测 claim、原件登记和结果接受服务形成本轮结果，不调用执行模型。
    vr_claim = claim_verification_request(first["verification_request_id"], VerificationClaimRequest(task_run=CreateTaskRunRequest(
        task_type="external_verify", worker_type="pi", worker_name=configuration["tenant_worker_id"],
        session_id=f"pi:{uuid4()}", expected_execution_generation=0, runtime_context={"dispatcher_id": machine_id})), machine)
    artifact = _upload(first, vr_claim, "log", "r2a-observation.log", b"synthetic current-round observation")
    submission = _submission(first, vr_claim, [artifact])
    submission.result.outcome = "not_reproduced"
    submission.result.reason_code = "synthetic_scope_only"
    submit_verification_result(first["verification_request_id"], submission, machine)
    work = call(turn, run, "query_agent_work", {"action_id": first["action_id"]}, "sdk-result").json()["work"]
    assert work["download_url"]
    assert work["result"] == {key: submission.result.model_dump()[key] for key in
                              ("outcome", "summary", "tested_scope", "limitations", "reason_code")}
    pending = call(turn, run, "query_agent_work", {"action_id": repeated.json()["action_id"]}, "sdk-pending-result").json()["work"]
    assert pending["result"] is None and pending["download_url"] is None
    downloaded = client.get(work["download_url"])
    assert downloaded.status_code == 200, downloaded.text
    assert "合成目标响应" in downloaded.text
    assert TestClient(app).get(work["download_url"]).status_code == 401
    exported = call(turn, run, "export_vulnerability_report", {"vulnerability_id": vuln.id}, "sdk-export")
    assert exported.status_code == 200, exported.text
    assert client.get(exported.json()["artifact"]["download_url"]).status_code == 200
    with db.get_conn() as conn:
        assert conn.execute("SELECT status FROM vulnerabilities WHERE project_id='p1' AND id=?", (vuln.id,)).fetchone()["status"] == vuln.status
    finish(turn, run)

    visible = {item["id"]: item for item in client.get(path + "/actions").json()}
    assert visible[first["action_id"]] == work
    assert len(visible) == 3
    assert TestClient(app).get(path + "/actions").status_code == 401
    create_user_with_personal_tenant("r2a-outsider", "local-test-password")
    outsider, _ = login("r2a-outsider")
    assert outsider.get(path + "/actions").status_code == 404
    assert outsider.get(work["download_url"]).status_code == 404
    outsider.close()
    final_turn, final_run = start_turn(client, headers, "查询复测结果")
    restored = call(final_turn, final_run, "query_agent_work", {"action_id": first["action_id"]}, "sdk-restored-query")
    assert restored.status_code == 200 and restored.json()["work"] == work
    with db.get_conn() as conn:
        conn.execute("UPDATE agent_messages SET submitted_by=NULL,submitted_tenant_id=NULL WHERE id=?", (final_run["message_id"],))
    legacy = call(final_turn, final_run, "request_vulnerability_retest", params, "sdk-legacy")
    assert legacy.status_code == 403 and legacy.json()["detail"]["code"] == "agent_action_actor_unavailable"
    assert len(client.get(path + "/actions").json()) == 3
    client.close()
    admin_client.close()
