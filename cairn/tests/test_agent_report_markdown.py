"""当前全局会话入口的普通模板、编号与报告代次；仅使用一次性 PG。"""
import base64
import json
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from cairn.server import db
from cairn.server.app import app
from cairn.server.auth import RequestPrincipal, create_machine_principal, create_user_with_personal_tenant
from cairn.server.models import CreateArtifactRequest, CreateVulnerabilityRequest
from cairn.server.schemas.models import CreateTenantModelRequest
from cairn.server.services import model_credentials, report_templates
from cairn.server.services.audit_artifacts import create_artifact_record
from cairn.server.services.tenant_models import create_tenant_model
from cairn.server.services.vulnerabilities import create_vulnerability
from cairn.server.services.time_utils import utcnow
from test_postgres_bootstrap import empty_postgres_url, postgres_instance
from test_report_templates import _seed_project

pytestmark = pytest.mark.skipif(os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1", reason="仅显式启用一次性 PostgreSQL")


@pytest.mark.parametrize("status", ["active", "paused"])
def test_agent_report_template_selection_number_and_failed_generation(empty_postgres_url, tmp_path, monkeypatch, status):
    db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")
    monkeypatch.setenv("CAIRN_PROJECT_RESOURCE_ROOT", str(tmp_path / "resources"))
    monkeypatch.setenv("CAIRN_TRUSTED_ORIGINS", "http://testserver")
    keyring = tmp_path / "keyring.json"
    keyring.write_text(json.dumps({"active_version": "test", "keys": {"test": base64.b64encode(b"a" * 32).decode()}}))
    keyring.chmod(0o600)
    monkeypatch.setenv(model_credentials.KEYRING_FILE_ENV, str(keyring))
    uid, tenant = create_user_with_personal_tenant("report-owner", "synthetic-password")
    owner = RequestPrincipal("user", uid, tenant_id=tenant, role="user")
    _, token = create_machine_principal("report-dispatcher")
    model = create_tenant_model(CreateTenantModelRequest(display_name="合成模型", protocol="openai-chat-completions",
        base_url="http://127.0.0.1:1", model_name="controlled", api_key="synthetic-only"), principal=owner)
    with db.get_conn() as conn:
        _seed_project(conn)
        conn.execute("UPDATE projects SET tenant_id = ?, status = ? WHERE id = 'p1'", (tenant, status))
        vuln = create_vulnerability(conn, "p1", CreateVulnerabilityRequest(title="合成发现", severity="high", remediation="合成建议"))
        template = report_templates.create_report_template(conn, None, "普通报告.md", "", "# 普通范例\n\n自由段落，没有槽位。\n")
        assert template.compatibility_profiles == []
    previous = create_artifact_record("p1", CreateArtifactRequest(kind="report", filename="previous.md", content="# 历史文件\n"))
    with db.get_conn() as conn:
        conn.execute("INSERT INTO assessment_report_finalizations(project_id,version,status,generation_started_at,generation_token,accepted_generation_token,accepted_artifact_id,created_at,updated_at) VALUES ('p1',1,'ready',?,'previous','previous',?,?,?)", (utcnow(), previous.id, utcnow(), utcnow()))
    with TestClient(app) as client:
        login = client.post("/auth/login", json={"login": "report-owner", "password": "synthetic-password"})
        assert login.status_code == 200, login.text
        headers = {"Origin": "http://testserver", "X-Cairn-CSRF-Token": login.json()["csrf_token"]}
        machine_headers = {"Authorization": "Bearer " + token}
        configured = client.put("/project-agent/configuration", json={"model_id": model.id}, headers=headers)
        assert configured.status_code == 200, configured.text
        created = client.post("/agent-conversations", json={}, headers=headers)
        assert created.status_code == 201, created.text
        path = "/agent-conversations/" + created.json()["id"]
        message = client.post(path + "/messages", json={"client_message_id": str(uuid4()), "project_id": "p1", "text": "使用普通报告范例生成已有成果报告"}, headers=headers)
        assert message.status_code == 200, message.text
        claim = client.post(path + "/claim", json={"request_id": str(uuid4())}, headers=machine_headers)
        assert claim.status_code == 200, claim.text
        run = claim.json()

        def call(method, params, call_id):
            return client.post(path + f"/turns/{message.json()['id']}/tools", json={"task_run_id": run["task_run_id"], "method": method, "params": params, "tool_call_id": call_id}, headers=machine_headers)

        read = call("read_vulnerability", {"vulnerability_id": vuln.number}, "read-number")
        assert read.status_code == 200, read.text
        assert read.json()["number"] == vuln.number
        assert read.json()["vulnerability_id"] == vuln.id and read.json()["project_id"] == "p1"
        catalog = call("list_report_templates", {}, "templates")
        assert catalog.status_code == 200, catalog.text
        selected = next(item for item in catalog.json()["items"] if item["key"] == template.key)
        assert not selected["validation_error"]
        absent = call("request_project_report", {}, "missing-template")
        assert absent.status_code == 422
        params = {"template_key": selected["key"]}
        report = call("request_project_report", params, "generate")
        assert report.status_code == 200, report.text
        result = report.json()
        assert result["generation_token"] != "previous"
        assert result["work"]["status"] == "queued" and result["work"]["download_url"] is None
        assert call("request_project_report", params, "generate").json()["generation_token"] == result["generation_token"]
        # 页面入口对同一模板复用同一次受理，不生成第二代或切换来源。
        page = client.post("/projects/p1/assessment-report-finalization/request", json=params, headers=headers)
        assert page.status_code == 202, page.text
        assert page.json()["generation_token"] == result["generation_token"]
        with db.get_conn() as conn:
            finalization = conn.execute("SELECT * FROM assessment_report_finalizations WHERE project_id = 'p1'").fetchone()
            assert finalization["template_key"] == template.key
            assert conn.execute("SELECT status FROM projects WHERE id='p1'").fetchone()["status"] == status
        claimed = client.post("/projects/p1/assessment-report-finalization/claim", json={"worker": "synthetic", "generation_token": result["generation_token"]}, headers=machine_headers)
        assert claimed.status_code == 200, claimed.text
        report_run = claimed.json()
        failed = client.post("/projects/p1/assessment-report-finalization/finish", json={
            "protocol": "cairn-assessment-report-finish-v1", "worker": "synthetic",
            "generation_token": result["generation_token"], "claim_token": report_run["claim_token"],
            "task_run_id": report_run["task_run_id"], "status": "failed",
            "task_run_result": {"status": "failed", "exit_code": 1, "error_type": "unknown", "error_message": "synthetic-failure"},
        }, headers=machine_headers)
        assert failed.status_code == 200, failed.text
        work = call("query_agent_work", {"action_id": result["action_id"]}, "query").json()["work"]
        assert work["status"] == "failed" and work["download_url"] is None
        assert work["accepted_generation_token"] == "previous"
        filtered = call("list_verification_requests", {"source_number": vuln.number, "external_id": "independent-external-id"}, "find-retests")
        assert filtered.status_code == 200, filtered.text
        assert filtered.json()["items"] == []
