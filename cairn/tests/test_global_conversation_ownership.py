"""G1-A 仅在隔离 PostgreSQL 与临时文件验证归属，不执行模型或研究。"""

import base64
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
import psycopg

from cairn.server import db, migrations
from cairn.server.app import app
from cairn.server.auth import RequestPrincipal, create_machine_principal, create_user_with_personal_tenant
from cairn.server.schemas.models import CreateTenantModelRequest
from cairn.server.services import agent_conversations as service, conversation_files
from cairn.server.services.model_execution import resolve_model_execution_binding
from cairn.server.services.project_agent_configuration import update_configuration
from cairn.server.services.tenant_models import create_tenant_model
from test_postgres_bootstrap import empty_postgres_url, postgres_instance

pytestmark = pytest.mark.skipif(os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1", reason="仅使用一次性 PostgreSQL")


@pytest.fixture
def owner(empty_postgres_url, tmp_path, monkeypatch):
    from cairn.server.services import model_credentials

    keyring = tmp_path / "keyring.json"
    keyring.write_text(json.dumps({"active_version": "test", "keys": {"test": base64.b64encode(b"t" * 32).decode()}}))
    keyring.chmod(0o600)
    monkeypatch.setenv(model_credentials.KEYRING_FILE_ENV, str(keyring))
    monkeypatch.setenv("CAIRN_TRUSTED_ORIGINS", "http://testserver")
    monkeypatch.setenv("CAIRN_PROJECT_RESOURCE_ROOT", str(tmp_path / "resources"))
    db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")
    uid, tenant = create_user_with_personal_tenant("global-chat-user", "local-test-password")
    actor = RequestPrincipal("user", uid, tenant_id=tenant, role="user")
    model = create_tenant_model(CreateTenantModelRequest(
        display_name="隔离模型", protocol="openai-chat-completions", base_url="http://127.0.0.1:1",
        model_name="controlled", api_key="synthetic-global-chat-key",
    ), principal=actor)
    with db.get_conn() as conn:
        update_configuration(conn, model.id, actor)
    return actor


def new_turn(actor, *, project_id=None):
    with db.get_conn() as conn:
        cid = service.create_conversation(conn, actor, project_id)["id"]
        message = service.submit_message(conn, cid, str(uuid4()), "受控材料会话", actor, project_id=project_id)
        machine = RequestPrincipal("machine", "controlled-chat-machine")
        claim = service.claim_turn(conn, cid, str(uuid4()), machine)
    return cid, message, machine, claim


def finish_body(claim, **updates):
    return SimpleNamespace(task_run_id=claim["task_run_id"], **{
        "status": "succeeded", "text": "受控回复", "error_code": None,
        "usage": {"input_tokens": 4, "output_tokens": 2}, "usage_missing_responses": None,
        "session_initialized": True, **updates})


def test_global_http_materials_ownership_and_no_project(owner):
    actor = owner
    client = TestClient(app)
    login = client.post("/auth/login", json={"login": "global-chat-user", "password": "local-test-password"})
    assert login.status_code == 200
    headers = {"Origin": "http://testserver", "X-Cairn-CSRF-Token": login.json()["csrf_token"]}
    _, token = create_machine_principal("global-http-machine")
    machine = {"Authorization": "Bearer " + token}
    response = client.post("/agent-conversations", json={}, headers=headers)
    assert response.status_code == 201
    cid = response.json()["id"]
    path = f"/agent-conversations/{cid}"
    original = "名称,数量\r\n甲,2\r\n".encode()
    response = client.post(path + "/attachments?filename=sample.csv", content=original, headers=headers)
    assert response.status_code == 201
    attachment = response.json()
    assert client.get(attachment["download_url"]).content == original
    internal_header = {"type": "session", "version": 1, "id": "synthetic", "timestamp": "2026-11-01T00:00:00Z",
                       "cwd": "/controlled", "cairn": {"project_id": "old-source", "session": "pi:synthetic"}}
    assert client.post(path + "/attachments?filename=copied.txt", content=json.dumps(internal_header).encode(), headers=headers).status_code == 403
    request = {"client_message_id": str(uuid4()), "text": "读取附件并保存报告", "attachment_ids": [attachment["id"]]}
    sent = client.post(path + "/messages", json=request, headers=headers)
    assert sent.status_code == 200
    assert client.post(path + "/messages", json=request, headers=headers).json() == sent.json()
    claim_request = {"request_id": str(uuid4())}
    response = client.post(path + "/claim", json=claim_request, headers=machine)
    assert response.status_code == 200
    claim = response.json()
    assert claim["project_id"] is None and claim["resume"] is False
    assert client.post(path + "/claim", json=claim_request, headers=machine).json() == claim
    turn = path + f"/turns/{claim['message_id']}"

    def tool(method, params, identity):
        return client.post(turn + "/tools", headers=machine, json={"task_run_id": claim["task_run_id"],
            "tool_call_id": identity, "method": method, "params": params})

    material = tool("read_conversation_attachment", {"attachment_id": attachment["id"]}, "read").json()
    assert material["content"].encode() == original
    params = {"title": "受控报告", "content": "# 受控材料\n仅使用合成数据。", "source_references": [material["source_reference"]]}
    response = tool("publish_custom_markdown", params, "publish")
    assert response.status_code == 200
    artifact = response.json()["artifact"]
    assert tool("publish_custom_markdown", params, "publish").json()["artifact"] == artifact
    assert tool("publish_custom_markdown", {**params, "content": "changed"}, "publish").status_code == 409
    assert client.get(artifact["download_url"]).text == params["content"]
    assert tool("publish_custom_markdown", {**params, "content": "synthetic-global-chat-key"}, "secret").status_code == 422
    for name in ("create_project", "append_project_intent", "request_vulnerability_retest", "bash"):
        assert tool(name, {}, name).status_code == 403
    body = vars(finish_body(claim))
    assert client.post(turn + "/finish", json=body, headers=machine).status_code == 200
    assert client.post(turn + "/finish", json=body, headers=machine).status_code == 200
    assert client.post(path + "/claim", json=claim_request, headers=machine).json()["status"] == "succeeded"
    with db.get_conn() as conn:
        assert conn.execute("SELECT count(*) AS n FROM projects").fetchone()["n"] == 0
        for stranger in (RequestPrincipal("user", "other", tenant_id=actor.tenant_id),
                         RequestPrincipal("user", actor.subject_id, tenant_id="another-tenant")):
            with pytest.raises(HTTPException) as denied:
                service.require_owner(conn, cid, stranger)
            assert denied.value.status_code == 404
    assert TestClient(app).get(artifact["download_url"]).status_code == 401
    assert client.delete(path, headers=headers).status_code == 204
    assert client.delete(path, headers=headers).status_code == 204
    assert client.get(path + "/messages").status_code == 404
    assert client.get(artifact["download_url"]).text == params["content"]
    client.close()


def test_claim_identity_machine_pin_and_source_failure_finish(owner):
    cid, message, machine, claim = new_turn(owner)
    with db.get_conn() as conn:
        service.submit_message(conn, cid, str(uuid4()), "排队第二条", owner)
        assert service.claim_turn(conn, cid, claim["request_id"], machine) == claim
        assert service.claim_turn(conn, cid, str(uuid4()), machine) is None
        with pytest.raises(HTTPException) as denied:
            service.claim_turn(conn, cid, claim["request_id"], RequestPrincipal("machine", "other"))
        assert denied.value.detail == "execution_owner_mismatch"
        conn.execute("UPDATE tenant_models SET custom_headers_json = ? WHERE id = ?", ('[{"name":"X-Test","value":"changed"}]', claim["model_id"]))
    with db.get_conn() as conn:
        with pytest.raises(HTTPException) as blocked:
            service.finish_turn(conn, cid, message["id"], finish_body(claim), machine)
        assert blocked.value.detail == "agent_execution_source_unavailable"
        receipt = service.claim_turn(conn, cid, claim["request_id"], machine)
        assert receipt["task_run_id"] == claim["task_run_id"] and receipt["claim_error"] == "agent_execution_source_unavailable"
        assert "execution_binding" not in receipt and "prompt" not in receipt
        assert conn.execute("SELECT answer FROM agent_messages WHERE id = ?", (message["id"],)).fetchone()["answer"] is None
        conn.execute("UPDATE users SET disabled = 1 WHERE id = ?", (owner.subject_id,))
    body = finish_body(claim, status="failed", text="", error_code="agent_execution_source_unavailable")
    with db.get_conn() as conn:
        assert service.heartbeat(conn, cid, message["id"], claim["task_run_id"], machine) == {"cancelled": False}
        assert service.finish_turn(conn, cid, message["id"], body, machine)["status"] == "failed"
        assert service.finish_turn(conn, cid, message["id"], body, machine)["status"] == "failed"
        run = conn.execute("SELECT * FROM task_runs WHERE conversation_id = ? AND id = ?", (cid, claim["task_run_id"])).fetchone()
        assert json.loads(run["runtime_context_json"])["usage"] == body.usage
        conn.execute("UPDATE users SET disabled = 0 WHERE id = ?", (owner.subject_id,))
        with pytest.raises(HTTPException) as denied:
            service.claim_turn(conn, cid, str(uuid4()), RequestPrincipal("machine", "other"))
        assert denied.value.detail == "agent_session_machine_mismatch"


def test_context_is_fixed_and_deleted_source_does_not_own_materials(owner):
    with db.get_conn() as conn:
        for pid in ("context-a", "context-b"):
            conn.execute("INSERT INTO projects(id,title,status,created_at,tenant_id) VALUES (?,?,'stopped','2026-11-01T00:00:00Z',?)",
                         (pid, pid, owner.tenant_id))
    cid, message, machine, claim = new_turn(owner, project_id="context-a")
    with db.get_conn() as conn:
        second = service.submit_message(conn, cid, str(uuid4()), "另一上下文", owner, project_id="context-b")
        assert service.claim_turn(conn, cid, claim["request_id"], machine)["project_id"] == "context-a"
    output = service.call_tool(cid, message["id"], claim["task_run_id"], "publish_custom_markdown",
                               {"title": "独立成品", "content": "# 已有材料"}, machine, tool_call_id="report")
    aid = output["artifact"]["id"]
    with db.get_conn() as conn:
        service.finish_turn(conn, cid, message["id"], finish_body(claim), machine)
    from cairn.server.services.project_records import delete_project_record
    delete_project_record("context-a")
    with db.get_conn() as conn:
        material = service.conversation_artifact(conn, cid, aid, owner)
        assert conversation_files.artifact_file(conn, material).read_text() == "# 已有材料"
        resumed = service.claim_turn(conn, cid, str(uuid4()), machine)
        assert resumed["resume"] is True and resumed["project_id"] == "context-b"
        assert resumed["source_task_run_id"] == claim["task_run_id"]
        # 没有初始化证明的失败保留未知，而不是自动新开；其他会话照常创建。
        service.finish_turn(conn, cid, second["id"], finish_body(resumed, status="failed", text="",
                            error_code="chat_execution_failed", session_initialized=None), machine)
        with pytest.raises(HTTPException) as blocked:
            service.submit_message(conn, cid, str(uuid4()), "不得重开", owner)
        assert blocked.value.detail == "session_verification_required"
        assert service.create_conversation(conn, owner)["available"] is True
    cid, message, machine, claim = new_turn(owner)
    with db.get_conn() as conn:
        service.finish_turn(conn, cid, message["id"], finish_body(claim, status="failed", text="",
                            error_code="chat_execution_failed", session_initialized=False), machine)
        service.submit_message(conn, cid, str(uuid4()), "实际未初始化可重试", owner)
        assert service.claim_turn(conn, cid, str(uuid4()), machine)["resume"] is False


@pytest.mark.parametrize("fault", ["write", "insert", "audit"])
def test_new_artifact_cleans_partial_write_or_database_failure(owner, monkeypatch, fault):
    cid, _, _, claim = new_turn(owner)
    root = db.artifact_root()
    original_execute = db.PostgresConnection.execute

    def fail_sql(self, sql, params=None):
        if fault == "insert" and sql.startswith("INSERT INTO artifacts"):
            raise RuntimeError("controlled-artifact-insert")
        if fault == "audit" and sql.startswith("INSERT INTO agent_events"):
            raise RuntimeError("controlled-artifact-audit")
        return original_execute(self, sql, params)

    class PartialSource:
        def __init__(self):
            self.reads = 0

        def read(self, size):
            self.reads += 1
            if self.reads == 1:
                return b"partial"
            raise RuntimeError("controlled-artifact-write")

    before = set(root.rglob("*"))
    monkeypatch.setattr(db.PostgresConnection, "execute", fail_sql)
    with pytest.raises(RuntimeError, match="controlled-artifact"):
        with db.get_conn() as conn:
            conversation_files.create_artifact(conn, cid, task_run_id=claim["task_run_id"], kind="report", filename="controlled.md",
                content=PartialSource() if fault == "write" else b"controlled", metadata={"title": "受控报告"})
    assert not [p for p in set(root.rglob("*")) - before if p.is_file()]
    with db.get_conn() as conn:
        assert conn.execute("SELECT count(*) AS n FROM artifacts WHERE conversation_id = ?", (cid,)).fetchone()["n"] == 0
    existing = root / "existing"
    existing.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        with db.get_conn() as conn:
            with conversation_files.new_file(conn, existing):
                pytest.fail("不能打开既有原件")
    assert existing.read_bytes() == b"original"


def test_migration_preserves_history_research_constraints_and_files(empty_postgres_url, tmp_path, monkeypatch):
    # 真实从 v75 升级，不在新 Schema 上伪造已迁移记录。
    all_migrations = migrations.POSTGRES_MIGRATIONS
    monkeypatch.setattr(db, "POSTGRES_MIGRATIONS", all_migrations[:-1])
    db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")
    uid, tenant = create_user_with_personal_tenant("historical-owner", "local-test-password")
    actor = RequestPrincipal("user", uid, tenant_id=tenant)
    now = "2026-11-01T00:00:00Z"
    runtime = json.dumps({"dispatcher_id": "historical-machine", "usage": {"input_tokens": 7}, "execution_binding": {"model_id": "historical-model"}})
    with db.get_conn() as conn:
        conn.execute("INSERT INTO projects(id,title,status,created_at,tenant_id) VALUES ('source','历史来源','stopped',?,?)", (now, tenant))
        for cid in ("history", "unknown", "empty"):
            conn.execute("INSERT INTO agent_conversations(id,project_id,created_by,session_id,created_at) VALUES (?,'source',?,?,?)",
                         (cid, uid, "pi:" + cid, now))
        for run, cid, status in (("tr1", "history", "succeeded"), ("tr2", "unknown", "failed")):
            conn.execute("INSERT INTO task_runs(id,project_id,task_type,worker_name,worker_type,status,session_id,runtime_context_json) VALUES (?,'source','agent_chat','project-agent','pi',?,?,?)",
                         (run, status, "pi:" + cid, runtime))
            conn.execute("INSERT INTO agent_messages(conversation_id,project_id,client_message_id,text,created_at,task_run_id,artifact_ids_json) VALUES (?,'source',?,?,?,?,?)",
                         (cid, str(uuid4()), "历史原文\r\n", now, run, '["report"]' if cid == "history" else '[]'))
        mid = conn.execute("SELECT id FROM agent_messages WHERE conversation_id='history'").fetchone()["id"]
        conn.execute("INSERT INTO agent_actions(id,message_id,tool_call_id,method,params_json,kind,generation_token) VALUES ('action',?,'call','request_project_report','{}','report','generation')", (mid,))
        for aid, run, content in (("report", "tr1", b"# historical\r\n"), ("attachment", None, b"name,count\r\na,1\r\n")):
            uri = f"artifact://projects/source/runs/{run or 'unscoped'}/{aid}"
            conn.execute("INSERT INTO artifacts(id,project_id,task_run_id,kind,uri,sha256,size,created_at,metadata_json) VALUES (?,'source',?,'file',?,?,?,?,?)",
                         (aid, run, uri, sha256(content).hexdigest(), len(content), now, json.dumps({"title": aid, "agent_conversation_id": "history"})))
            path = db.artifact_root() / "projects" / "source" / "runs" / (run or "unscoped") / aid
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        conn.execute("INSERT INTO agent_attachments(id,project_id,conversation_id,message_id,artifact_id,filename,uploaded_by,created_at) VALUES ('att_old','source','history',?,'attachment','sample.csv',?,?)", (mid, uid, now))
        conn.execute("INSERT INTO agent_events(id,project_id,task_run_id,ts,kind) VALUES ('event','source','tr1',?,'artifact_created')", (now,))
        # 保留研究外键验证用的已有记录，不调度或执行研究。
        conn.execute("INSERT INTO task_runs(id,project_id,task_type,worker_name,worker_type,status) VALUES ('research','source','reason','fixture','pi','failed')")
        conn.execute("INSERT INTO agent_events(id,project_id,task_run_id,ts,kind) VALUES ('research-event','source','research',?,'fixture')", (now,))
        before_messages = conn.execute("SELECT * FROM agent_messages ORDER BY id").fetchall()
        before_runs = conn.execute("SELECT * FROM task_runs ORDER BY id").fetchall()
        before_actions = conn.execute("SELECT * FROM agent_actions").fetchall()
        before_attachments = conn.execute("SELECT * FROM agent_attachments ORDER BY id").fetchall()
        before_artifacts = conn.execute("SELECT * FROM artifacts ORDER BY id").fetchall()
        fks = conn.execute("SELECT conrelid::regclass::text AS relation, conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint WHERE contype='f' AND confrelid IN ('task_runs'::regclass,'artifacts'::regclass)").fetchall()
    monkeypatch.setattr(db, "POSTGRES_MIGRATIONS", all_migrations)
    with db.get_conn() as conn:
        migrations.verify_or_apply(conn, migrations.POSTGRES_CONVERSATION_OWNERS, lambda _: None)
    source_file = db.artifact_root() / "projects/source/runs/unscoped/attachment"
    original_bytes = source_file.read_bytes()
    source_file.write_bytes(b"controlled-corruption")
    with pytest.raises(ValueError, match="digest mismatch"):
        with db.get_conn() as conn:
            conversation_files.migrate_conversation_files(conn)
    assert source_file.read_bytes() == b"controlled-corruption"
    assert not [p for p in (db.artifact_root() / "conversations").rglob("*") if p.is_file()]
    source_file.write_bytes(original_bytes)
    with db.get_conn() as conn:
        assert all(conn.execute("SELECT 1 FROM pg_constraint WHERE conrelid=?::regclass AND conname=? AND pg_get_constraintdef(oid)=?", (fk["relation"], fk["conname"], fk["definition"])).fetchone()
                   for fk in fks if fk["relation"] not in {"agent_messages", "agent_attachments"})
        for before, after in zip(before_messages, conn.execute("SELECT * FROM agent_messages ORDER BY id").fetchall()):
            assert {key: after[key] for key in before} == before
            assert after["submitted_by"] is None
        for before, after in zip(before_runs, conn.execute("SELECT * FROM task_runs ORDER BY id").fetchall()):
            assert all(after[key] == value for key, value in before.items() if key != "project_id")
        assert conn.execute("SELECT * FROM agent_actions").fetchall() == before_actions
        for before, after in zip(before_attachments, conn.execute("SELECT * FROM agent_attachments ORDER BY id").fetchall()):
            assert {key: value for key, value in before.items() if key != "project_id"} == after
        for before, after in zip(before_artifacts, conn.execute("SELECT * FROM artifacts ORDER BY id").fetchall()):
            assert all(after[key] == value for key, value in before.items() if key not in {"project_id", "uri"})
        history = service.require_owner(conn, "history", actor)
        assert history["tenant_id"] == tenant and history["session_machine_id"] == "historical-machine"
        assert history["session_initialized"] is None and history["unavailable_reason"] == "session_verification_required"
        assert conn.execute("SELECT session_initialized FROM agent_conversations WHERE id='empty'").fetchone()["session_initialized"] is False
        for cid in ("history", "unknown"):
            with pytest.raises(HTTPException) as blocked:
                service.submit_message(conn, cid, str(uuid4()), "不能重开", actor)
            assert blocked.value.detail == "session_verification_required"
            with pytest.raises(HTTPException) as blocked:
                service.claim_turn(conn, cid, str(uuid4()), RequestPrincipal("machine", "historical-machine"))
            assert blocked.value.detail == "session_verification_required"
        assert service.pending_conversations(conn) == []
        # 本轮不伪造 SDK 迁移完成；文件迁移可独立完成。
        stats = conversation_files.migrate_conversation_files(conn)
        assert stats == {"artifacts": 2, "bytes": 31}
        assert conversation_files.migrate_conversation_files(conn) == {"artifacts": 0, "bytes": 0}
        for row in conn.execute("SELECT * FROM artifacts WHERE conversation_id='history'").fetchall():
            path = conversation_files.resolve_artifact_path(db.artifact_root(), row)
            assert sha256(path.read_bytes()).hexdigest() == row["sha256"]
            assert row["migration_source_json"] is None
        conn.execute("DELETE FROM projects WHERE id='source'")
        assert conn.execute("SELECT count(*) AS n FROM agent_conversations").fetchone()["n"] == 3
        assert conn.execute("SELECT count(*) AS n FROM agent_messages").fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) AS n FROM artifacts").fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) AS n FROM task_runs").fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) AS n FROM agent_actions").fetchone()["n"] == 1
        assert service.action_works(conn, "history", actor)[0]["status"] == "unavailable"
        assert conn.execute("SELECT count(*) AS n FROM agent_events").fetchone()["n"] == 1
    assert len([p for p in (db.artifact_root() / "conversations").rglob("*") if p.is_file()]) == 2
    assert len([p for p in (db.artifact_root() / "projects").rglob("*") if p.is_file()]) == 2
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.get_conn() as conn:
            conn.execute("INSERT INTO task_runs(id,task_type,worker_name,worker_type,status) VALUES ('bad','agent_chat','fixture','pi','failed')")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with db.get_conn() as conn:
            conn.execute("INSERT INTO agent_events(id,project_id,task_run_id,ts,kind) VALUES ('bad','missing','missing',?,'fixture')", (now,))
