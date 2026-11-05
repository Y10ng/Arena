"""G2：只用隔离 PostgreSQL、合成材料和现有会话 fixture，不调用模型／目标。"""

from hashlib import sha256
import json
import os
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from cairn.server import db
from cairn.server.app import app
from cairn.server.auth import create_user_with_personal_tenant, RequestPrincipal
from cairn.server.models import CreateVulnerabilityRequest
from cairn.server.services import agent_conversations as service, conversation_files, vulnerability_reports
from cairn.server.services.vulnerabilities import create_vulnerability
from test_global_conversation_ownership import owner, new_turn
from test_postgres_bootstrap import empty_postgres_url, postgres_instance

pytestmark = pytest.mark.skipif(os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1", reason="仅使用一次性 PostgreSQL")


def tools(owner):
    cid, message, machine, claim = new_turn(owner)

    def call(method, params, identity=None):
        return service.call_tool(cid, claim["message_id"], claim["task_run_id"], method, params,
                                 machine, tool_call_id=identity or str(uuid4()))
    return cid, message, call


def project(owner, status="paused"):
    with db.get_conn() as conn:
        conn.execute("INSERT INTO projects(id,title,status,created_at,tenant_id) VALUES ('source','合成来源',?,'2026-11-01T00:00:00Z',?)", (status, owner.tenant_id))
        for identifier, text in (("f1", "甲乙丙丁戊己"), ("f2", "另一条合成资料")):
            conn.execute("INSERT INTO facts(id,project_id,description) VALUES (?,'source',?)", (identifier, text))


def publish(call, refs=(), **kwargs):
    return call("publish_custom_markdown", {"title": "合成整理", "content": "# 合成材料\n不代表漏洞确认。",
                "source_references": list(refs), "limitations": ["未进行目标验证"], **kwargs})["artifact"]


def test_selected_scope_version_permissions_and_history(owner):
    cid, _, call = tools(owner)
    project(owner)
    params = {"project_id": "source", "source": "facts", "item_id": "f1", "offset": 0, "chunk_size": 50}
    material = call("read_research_context", params)
    reference = material["source_reference"]
    assert reference["params"] == params
    assert "[0, 50)" in material["source_scope"] and material["next_offset"] == 50
    index = call("read_research_context", {"project_id": "source", "source": "facts", "limit": 1})
    artifact = publish(call, [reference, index["source_reference"]], source_notes=["合成声明"])
    assert len(artifact["sources"]["verified"]) == 2
    assert "仅索引" in artifact["sources"]["verified"][1]["scope"]
    assert artifact["sources"]["declared"] == ["合成声明"]
    assert artifact["sources"]["declared_read_at"] is None
    assert artifact["sources"]["verified"][0]["verified_at"] >= material["read_at"]
    full = call("read_research_context", {**params, "chunk_size": 16384})["source_reference"]
    request = {"title": "完整合成整理", "content": "# 留存成品", "source_references": [full]}
    original = call("publish_custom_markdown", request, "same-call")
    with db.get_conn() as conn:
        conn.execute("UPDATE facts SET description='已变化的合成资料' WHERE project_id='source' AND id='f1'")
    with pytest.raises(HTTPException) as changed:
        call("publish_custom_markdown", request)
    assert changed.value.status_code == 409
    assert call("publish_custom_markdown", request, "same-call") == original
    with pytest.raises(HTTPException) as conflict:
        call("publish_custom_markdown", {**request, "source_notes": ["不同参数"]}, "same-call")
    assert conflict.value.status_code == 409
    uid, tenant = create_user_with_personal_tenant("g2-other-user", "local-test-password")
    other = RequestPrincipal("user", uid, tenant_id=tenant, role="user")
    with db.get_conn() as conn:
        with pytest.raises(HTTPException):
            service.conversation_artifact(conn, cid, artifact["id"], other)
        conn.execute("UPDATE projects SET tenant_id=? WHERE id='source'", (tenant,))
    with pytest.raises(HTTPException) as revoked:
        publish(call, [full])
    assert revoked.value.status_code == 404
    assert call("publish_custom_markdown", request, "same-call") == original
    client = TestClient(app)
    assert client.post("/auth/login", json={"login": "global-chat-user", "password": "local-test-password"}).status_code == 200
    assert client.get(original["artifact"]["download_url"]).text == request["content"]
    with db.get_conn() as conn:
        conn.execute("DELETE FROM projects WHERE id='source'")
    assert client.get(original["artifact"]["download_url"]).text == request["content"]
    with pytest.raises(HTTPException):
        publish(call, [full])
    client.close()


@pytest.mark.parametrize("status", ["paused", "stopped", "completed"])
def test_readonly_report_and_single_export_preserve_state(owner, status, monkeypatch):
    cid, _, call = tools(owner)
    project(owner, status)
    with db.get_conn() as conn:
        vuln = create_vulnerability(conn, "source", CreateVulnerabilityRequest(title="合成候选", severity="low", source_fact_id="f1"))
        before = {table: conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
                  for table in ("task_runs", "intents", "verification_requests", "agent_actions", "assessment_report_finalizations")}
    params = {"project_id": "source", "vulnerability_id": vuln.id}
    data = call("read_vulnerability", params)
    report = publish(call, [data["source_reference"]])
    assert report["sources"]["verified"][0]["limitations"] == data["readable"]["limitations_markdown"]
    # 渲染后若来源变化，保存的版本仍来自此次实际取材，不允许二次读取补成新版本。
    render = vulnerability_reports.render_report_template
    rendered = {}
    def change_after_material(template, context):
        from cairn.server.services.agent_material_sources import projection_version
        rendered.update(version=projection_version({"context": context, "template": template}),
                        template=projection_version(template), projection=projection_version(context))
        result = render(template, context)
        with db.get_conn() as conn:
            conn.execute("UPDATE facts SET description='渲染后的另一个版本' WHERE project_id='source' AND id='f1'")
        return result
    monkeypatch.setattr(vulnerability_reports, "render_report_template", change_after_material)
    exported = call("export_vulnerability_report", params, "export")
    source = exported["artifact"]["sources"]["verified"][0]
    assert source["reference"]["vulnerability_id"] == vuln.id
    assert source["version"] == rendered["version"]
    assert source["reference"]["template_sha256"] == rendered["template"]
    assert source["reference"]["projection_sha256"] == rendered["projection"]
    with db.get_conn() as conn:
        saved = service.conversation_artifact(conn, cid, exported["artifact"]["id"], owner)
        text = conversation_files.artifact_file(conn, saved).read_text()
        assert "渲染后的另一个版本" not in text
        assert "甲乙丙丁戊己" in text
        assert before == {table: conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] for table in before}
        assert conn.execute("SELECT status FROM projects WHERE id='source'").fetchone()["status"] == status
        conn.execute("DELETE FROM projects WHERE id='source'")
    monkeypatch.setattr(vulnerability_reports, "render_report_template", lambda *_: pytest.fail("重传不能重新渲染"))
    assert call("export_vulnerability_report", params, "export") == exported
    with pytest.raises(HTTPException):
        call("export_vulnerability_report", params, "new-export")


def test_attachment_references_rollback_and_unverified_history(owner, monkeypatch):
    cid, message, call = tools(owner)
    with db.get_conn() as conn:
        artifact = conversation_files.create_artifact(conn, cid, content="苹果2，梨3".encode(), kind="file", filename="fixture.txt", metadata={"agent_attachment": True})
        conn.execute("INSERT INTO agent_attachments(id,conversation_id,message_id,artifact_id,filename,uploaded_by,created_at) VALUES ('att_g2',?,?,?,?,?,'2026-11-01T00:00:00Z')",
                     (cid, message["id"], artifact["id"], "合成附件.txt", owner.subject_id))
    page = call("read_conversation_attachment", {"attachment_id": "att_g2", "chunk_size": 2})
    binary = call("_read_conversation_attachment_bytes", {"attachment_id": "att_g2"})
    assert "[0, 2)" in page["source_scope"] and "完整附件字节" in binary["source_scope"]
    assert page["sha256"] == binary["sha256"] == sha256("苹果2，梨3".encode()).hexdigest()
    published = publish(call, [page["source_reference"], binary["source_reference"]])
    assert all(source["material_version"]["sha256"] == page["sha256"] for source in published["sources"]["verified"])
    other_cid, _, other_call = tools(owner)
    assert other_cid != cid
    with pytest.raises(HTTPException):
        publish(other_call, [page["source_reference"]])
    with pytest.raises(HTTPException):
        publish(call, [page["source_reference"]], content="synthetic-global-chat-key")
    with pytest.raises(HTTPException):
        publish(call, sources=["旧参数不能静默变成核验来源"])
    with pytest.raises(HTTPException):
        publish(call, [{"method": "read_research_session", "params": {"task_run_id": "none"}, "expected_version": "a" * 64}])
    ordinary = publish(call, source_notes=["未核验自由声明"])
    assert not ordinary["sources"]["verified"]
    with db.get_conn() as conn:
        old = service.conversation_artifact(conn, cid, ordinary["id"], owner)
        metadata = json.loads(old["metadata_json"])
        metadata.pop("source_notes")
        metadata.pop("verified_sources")
        metadata.update(sources=["历史模型声明"], read_at="2001-01-01T00:00:00Z")
        conn.execute("UPDATE artifacts SET metadata_json=? WHERE id=?", (json.dumps(metadata), ordinary["id"]))
        row = conn.execute("SELECT * FROM agent_messages WHERE id=?", (message["id"],)).fetchone()
        history = next(item for item in service._artifacts(conn, row) if item["id"] == ordinary["id"])
        assert history["sources"] == {"verified": [], "declared": ["历史模型声明"], "declared_read_at": "2001-01-01T00:00:00Z", "limitations": ["未进行目标验证"]}
        assert json.loads(service.conversation_artifact(conn, cid, ordinary["id"], owner)["metadata_json"]) == metadata
        counts = {table: conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] for table in ("artifacts", "agent_events")}
    files = {str(path): sha256(path.read_bytes()).hexdigest() for path in db.artifact_root().rglob("*") if path.is_file()}
    check = conversation_files.artifact_file
    def reject_created(conn, row):
        if json.loads(row["metadata_json"]).get("title") == "失败回滚.md":
            raise HTTPException(422, "合成准出失败")
        return check(conn, row)
    monkeypatch.setattr(conversation_files, "artifact_file", reject_created)
    with pytest.raises(HTTPException):
        publish(call, [page["source_reference"]], title="失败回滚")
    assert files == {str(path): sha256(path.read_bytes()).hexdigest() for path in db.artifact_root().rglob("*") if path.is_file()}
    with db.get_conn() as conn:
        assert counts == {table: conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] for table in counts}
