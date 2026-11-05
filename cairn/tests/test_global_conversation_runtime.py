"""G1-B：隔离 PostgreSQL、真实 SDK 文件与合成传输，不调用模型或被测目标。"""
from concurrent.futures import Future
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.dispatcher.scheduler.agent_chat import AgentChatMixin
from cairn.dispatcher.scheduler.worker_runtime import WorkerRuntimeMixin
from cairn.dispatcher.runtime.conversation_sessions import conversation_initialized, session_evidence
from cairn.server import db
from cairn.server.auth import RequestPrincipal
from cairn.server.schemas.models import CreateTenantModelRequest
from cairn.server.services import agent_conversations as service
from cairn.server.services.conversation_sessions import migrate_local_conversation_session
from cairn.server.services.model_execution import resolve_model_execution_binding
from cairn.server.services.tenant_models import create_tenant_model
from test_agent_chat_execution import finish_storage
from test_global_conversation_ownership import owner, new_turn, finish_body
from test_postgres_bootstrap import empty_postgres_url, postgres_instance

PG = pytest.mark.skipif(os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1", reason="仅使用一次性 PostgreSQL")


def loop_fixture(tmp_path):
    class Loop(AgentChatMixin, WorkerRuntimeMixin):
        pass
    loop = finish_storage(Loop(), tmp_path)
    loop.futures = {}
    loop.config = SimpleNamespace(container=SimpleNamespace(image="fixture", network_mode="bridge", dns=[]))
    loop.container_manager = SimpleNamespace()
    loop.agent_executor = SimpleNamespace(submit=Mock())
    return loop


def test_global_client_paths_and_request_identity():
    client = CairnClient.__new__(CairnClient)
    client._request_json = Mock(return_value=ApiResult(200))
    client.claim_agent_turn("c1", "request-id")
    assert client._request_json.call_args.args == ("POST", "/agent-conversations/c1/claim")
    assert client._request_json.call_args.kwargs == {"json": {"request_id": "request-id"}}
    client.agent_turn_action("c1", 3, "finish", {"task_run_id": "t1"})
    assert client._request_json.call_args.args[1] == "/agent-conversations/c1/turns/3/finish"


@pytest.mark.parametrize("outcome", ["succeeded", "claim_error", "running"])
def test_lost_claim_response_reuses_request_and_never_starts_terminal_or_error(tmp_path, outcome):
    loop = loop_fixture(tmp_path)
    requests = []
    result = {"status": "succeeded", "text": "合成回复", "error_code": None,
              "usage": {"input_tokens": 2}, "session_initialized": True}
    future = Future()
    future.set_result(result)
    loop.agent_executor.submit.return_value = future

    def claim(cid, rid):
        requests.append(rid)
        if len(requests) == 1:
            return ApiResult(0)
        value = {"conversation_id": cid, "request_id": rid, "message_id": 1, "task_run_id": "t1",
                 "status": "succeeded" if outcome == "succeeded" else "running"}
        if outcome == "claim_error":
            value["claim_error"] = "agent_execution_source_unavailable"
        elif outcome == "running":
            value.update(session_machine_id="d1", project_id=None, execution_binding={
                "model_id": "model1", "model_revision": 1, "credential_version_id": "cred1",
                "credential_version": 1, "credential_fingerprint": "fixture", "protocol": "openai-chat-completions",
                "model_name": "fixture", "base_url": "http://provider.invalid/v1",
                "inference_endpoint": "http://provider.invalid/v1/chat/completions",
                "auth_header": "authorization-bearer", "api_key": "synthetic-model-secret"})
        return ApiResult(200, value)

    loop.client = SimpleNamespace(claim_agent_turn=claim,
        pending_agent_turns=Mock(side_effect=[ApiResult(200, [{"conversation_id": "c1", "session_machine_id": None}]), ApiResult(200, [])]),
        agent_turn_action=Mock(side_effect=[ApiResult(409, {"detail": "agent_execution_source_unavailable"}), ApiResult(200)]))
    loop._dispatch_agent_turns()
    # 已受理轮次不再位于 pending，仍从持久请求恢复；重启也不换 request_id。
    resumed = loop_fixture(tmp_path)
    resumed.client, resumed.agent_executor = loop.client, loop.agent_executor
    resumed._dispatch_agent_turns()
    assert len(requests) == 2 and requests[0] == requests[1]
    assert resumed.agent_executor.submit.call_count == (1 if outcome == "running" else 0)
    if outcome == "succeeded":
        assert not resumed.futures
    else:
        active, task = next(iter(resumed.futures.items()))
        assert task.project_id is None
        resumed._reap_agent_turn(active, task)
        resumed._reap_agent_turn(active, task)
        assert not resumed.futures
        sent = resumed.client.agent_turn_action.call_args.args[-1]
        assert sent["text"] == "" and sent["status"] == "failed"
        if outcome == "running":
            assert sent["usage"] == result["usage"] and sent["session_initialized"] is True


def test_claim_retry_budget_and_foreign_machine_do_not_hide_later_page(tmp_path):
    loop = loop_fixture(tmp_path)
    loop.client = SimpleNamespace(claim_agent_turn=Mock(return_value=ApiResult(0)),
        pending_agent_turns=Mock(return_value=ApiResult(200, [{"conversation_id": f"c{i:03}", "session_machine_id": "elsewhere"} for i in range(100)])))
    loop._dispatch_agent_turns()
    assert loop._agent_pending_after == "c099"
    loop.client.claim_agent_turn.assert_not_called()
    loop.client.pending_agent_turns.return_value = ApiResult(200, [{"conversation_id": "c100", "session_machine_id": None}])
    for _ in range(8):
        loop._dispatch_agent_turns()
    assert loop.client.claim_agent_turn.call_count == 3
    assert len({call.args[1] for call in loop.client.claim_agent_turn.call_args_list}) == 1
    loop.agent_executor.submit.assert_not_called()


@PG
def test_explicit_project_reads_use_actor_and_real_source_secrets(owner):
    cid, _, machine, claim = new_turn(owner)
    with db.get_conn() as conn:
        for project, tenant in (("a", owner.tenant_id), ("b", owner.tenant_id), ("private", "other-tenant")):
            if tenant == "other-tenant":
                conn.execute("INSERT INTO tenants(id,kind,name,created_at) VALUES ('other-tenant','legacy','other','2026-11-01T00:00:00Z')")
            conn.execute("INSERT INTO projects(id,title,status,created_at,tenant_id) VALUES (?,?,'stopped','2026-11-01T00:00:00Z',?)", (project, project, tenant))
            conn.execute("INSERT INTO facts(id,project_id,description) VALUES ('f1',?,?)", (project, "合成材料-" + project))
    model = create_tenant_model(CreateTenantModelRequest(display_name="材料来源", protocol="openai-chat-completions",
        base_url="http://127.0.0.1:1", model_name="source", api_key="synthetic-source-b-key"), principal=owner)
    with db.get_conn() as conn:
        binding = resolve_model_execution_binding(conn, tenant_id=owner.tenant_id, model_id=model.id)
        conn.execute("INSERT INTO task_runs(id,project_id,task_type,worker_name,worker_type,status,runtime_context_json) VALUES ('r1','b','reason','fixture','pi','succeeded',?)", (json.dumps({"execution_binding": binding.execution_provenance()}),))
        for fid, content in (("source-secret", "synthetic-source-b-key"), ("chat-secret", "synthetic-global-chat-key")):
            conn.execute("INSERT INTO facts(id,project_id,description,task_run_id) VALUES (?,'b',?,'r1')", (fid, content))

    def tool(method, params):
        return service.call_tool(cid, claim["message_id"], claim["task_run_id"], method, params, machine, tool_call_id=str(uuid4()))

    index = tool("list_authorized_projects", {})
    assert [item["id"] for item in index["items"]] == ["a", "b"]
    for project in ("a", "b"):
        data = tool("read_research_context", {"project_id": project, "source": "facts", "item_id": "f1"})
        assert json.loads(data["content"])["description"] == "合成材料-" + project
    with pytest.raises(HTTPException):
        tool("read_research_context", {"project_id": "private", "source": "facts", "item_id": "f1"})
    with pytest.raises(HTTPException):
        tool("read_research_context", {"source": "facts"})
    protected = tool("read_research_context", {"project_id": "b", "source": "facts", "item_id": "source-secret"})
    assert protected["availability"] == "unavailable" and "synthetic-source-b-key" not in json.dumps(protected)
    with pytest.raises(HTTPException):
        tool("read_research_context", {"project_id": "b", "source": "facts", "item_id": "chat-secret"})
    from cairn.server.models import CreateVulnerabilityRequest
    from cairn.server.services.vulnerabilities import create_vulnerability
    from cairn.server.services import conversation_files
    with db.get_conn() as conn:
        finding = create_vulnerability(conn, "b", CreateVulnerabilityRequest(
            title="受控既有材料", severity="low", source_fact_id="f1"))
        conn.execute("UPDATE vulnerabilities SET discovered_by_run_id='r1' WHERE project_id='b' AND id=?", (finding.id,))
    params = {"project_id": "b", "vulnerability_id": finding.id}
    exported = tool("export_vulnerability_report", params)
    assert exported["project_id"] == exported["artifact"]["source_project_id"] == "b"
    with db.get_conn() as conn:
        artifact = service.conversation_artifact(conn, cid, exported["artifact"]["id"], owner)
        assert json.loads(artifact["metadata_json"])["source_project_id"] == "b"
        saved = conversation_files.artifact_file(conn, artifact).read_bytes()
        conn.execute("UPDATE vulnerabilities SET title='synthetic-source-b-key' WHERE project_id='b' AND id=?", (finding.id,))
    with pytest.raises(HTTPException):
        tool("list_vulnerabilities", {"project_id": "b"})
    with db.get_conn() as conn:
        conn.execute("UPDATE vulnerabilities SET title='受控既有材料', source_fact_id='source-secret' WHERE project_id='b' AND id=?", (finding.id,))
    for method in ("read_vulnerability", "export_vulnerability_report"):
        with pytest.raises(HTTPException):
            tool(method, params)
    with db.get_conn() as conn:
        assert conn.execute("SELECT count(*) AS n FROM artifacts WHERE conversation_id=?", (cid,)).fetchone()["n"] == 1
        conn.execute("UPDATE projects SET tenant_id = 'other-tenant' WHERE id = 'b'")
    with pytest.raises(HTTPException):
        tool("read_research_context", {"project_id": "b", "source": "facts", "item_id": "f1"})
    with db.get_conn() as conn:
        # 已准出的不可变会话成品不重新读取已撤权的原项目，也不丢失真实来源引用。
        assert conversation_files.artifact_file(conn, artifact).read_bytes() == saved


@PG
@pytest.mark.parametrize("initialized", [True, False])
def test_explicit_local_sdk_migration_preserves_entries_and_unknown_stays_blocked(owner, tmp_path, monkeypatch, initialized):
    with db.get_conn() as conn:
        conn.execute("INSERT INTO projects(id,title,status,created_at,tenant_id) VALUES ('old','合成来源','stopped','2026-11-01T00:00:00Z',?)", (owner.tenant_id,))
    cid, _, machine, claim = new_turn(owner, project_id="old")
    with db.get_conn() as conn:
        service.finish_turn(conn, cid, claim["message_id"], finish_body(claim, status="failed", text="", error_code="historical", session_initialized=None), machine)
    old_root = tmp_path / "traffic-chat" / "old"
    directory = old_root / "control" / "pi" / claim["session_id"][3:]
    directory.mkdir(parents=True)
    # 使用已安装的真实 SDK 产生原始 ID、消息与 entry 链，不使用模型自述或 TaskRun 默认值。
    sdk = Path(__file__).resolve().parents[2] / "container/pi-agent/node_modules/@mariozechner/pi-coding-agent/dist/index.js"
    script = r'''
import { createHash } from 'node:crypto';
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
const [modulePath, directory, session, run, initialized] = process.argv.slice(1);
const { SessionManager } = await import(modulePath);
const manager = SessionManager.open(join(directory, 'session.jsonl'), directory, '/home/kali/workspace');
Object.assign(manager.getHeader(), { cairn: { project_id: 'old', session } });
manager.appendMessage({ role: 'user', content: '受控原始消息正文', timestamp: 1 });
if (initialized === 'true') manager.appendMessage({ role: 'assistant', content: [{ type: 'text', text: '受控已存回复' }], timestamp: 2 });
const raw = [manager.getHeader(), ...manager.getEntries()].map(JSON.stringify).join('\n') + '\n';
writeFileSync(join(directory, 'session.jsonl'), raw);
writeFileSync(join(directory, 'manifest.json'), JSON.stringify({ protocol: 'cairn-pi-session-v1', project_id: 'old', session, sdk_session_id: manager.getSessionId(), cwd: '/home/kali/workspace', phase_family: 'chat', binding_sha256: createHash('sha256').update('{}').digest('hex'), last_task_run_id: run, sha256: createHash('sha256').update(raw).digest('hex') }));
'''
    subprocess.run(["node", "--input-type=module", "-e", script, sdk.as_uri(), str(directory), claim["session_id"], claim["task_run_id"], str(initialized).lower()], check=True, capture_output=True)
    original = (directory / "session.jsonl").read_bytes()
    source_manifest = (directory / "manifest.json").read_bytes()
    workspace = old_root / "workspace" / cid
    workspace.mkdir(parents=True)
    (workspace / "user.md").write_bytes(b"# controlled material\n")
    target_root = tmp_path / "traffic-conversations" / cid
    container = SimpleNamespace(reload=lambda: None, attrs={"Config": {"Labels": {
        "cairn.pi.instance_id": "fixture", "cairn.engine": "pi", "cairn.project_id": "old", "cairn.scenario_key": "project-chat"}},
        "State": {"Status": "exited"}, "Mounts": [{"Destination": dest, "Source": str(old_root / child)} for child, dest in (("control", "/home/kali/.cairn"), ("workspace", "/home/kali/workspace"))]})
    manager = SimpleNamespace(instance_id="fixture", traffic_execution=SimpleNamespace(root=tmp_path / "traffic"),
        chat_directory=lambda _: target_root, container_name=lambda project: "fixture-" + project,
        _client=SimpleNamespace(containers=SimpleNamespace(get=lambda _: container)))
    source_lock = directory.parent / (claim["session_id"][3:] + ".lock")
    target_lock = target_root / "control/pi" / source_lock.name

    def still_unknown():
        assert (directory / "session.jsonl").read_bytes() == original
        assert (directory / "manifest.json").read_bytes() == source_manifest
        with db.get_conn() as conn:
            stored = conn.execute("SELECT * FROM agent_conversations WHERE id=?", (cid,)).fetchone()
            assert stored["session_initialized"] is None and stored["unavailable_reason"] == "session_verification_required"
            assert stored["session_id"] == claim["session_id"]

    with pytest.raises(ValueError, match="machine_mismatch"), db.get_conn() as conn:
        migrate_local_conversation_session(conn, manager, cid, machine_id="another-machine")
    source_lock.write_text("controlled existing writer")
    with pytest.raises(ValueError, match="locked"), db.get_conn() as conn:
        migrate_local_conversation_session(conn, manager, cid, machine_id=machine.subject_id)
    assert source_lock.read_text() == "controlled existing writer"
    source_lock.unlink()
    container.attrs["State"]["Status"] = "running"
    with pytest.raises(ValueError, match="container_unverified"), db.get_conn() as conn:
        migrate_local_conversation_session(conn, manager, cid, machine_id=machine.subject_id)
    container.attrs["State"]["Status"] = "exited"
    still_unknown()

    # 中途写入失败与数据库回滚都不解除不可用状态；同字节目标可显式重试，原件始终保留。
    from cairn.server.services import conversation_sessions as migration
    original_persist = migration._persist
    def fail_manifest(path, content):
        if path.name == "manifest.json":
            raise OSError("controlled write failure")
        original_persist(path, content)
    with monkeypatch.context() as patch:
        patch.setattr(migration, "_persist", fail_manifest)
        with pytest.raises(OSError), db.get_conn() as conn:
            migrate_local_conversation_session(conn, manager, cid, machine_id=machine.subject_id)
    assert not source_lock.exists() and not target_lock.exists()
    assert conversation_initialized(manager, cid, claim["session_id"]) is None
    still_unknown()
    with pytest.raises(RuntimeError, match="controlled rollback"), db.get_conn() as conn:
        migrate_local_conversation_session(conn, manager, cid, machine_id=machine.subject_id)
        assert target_lock.exists()
        raise RuntimeError("controlled rollback")
    assert not source_lock.exists() and not target_lock.exists()
    still_unknown()
    target_manifest = target_root / "control/pi" / claim["session_id"][3:] / "manifest.json"
    valid_manifest = target_manifest.read_bytes()
    target_manifest.write_bytes(b"controlled conflicting target")
    with pytest.raises(ValueError, match="destination_conflict"), db.get_conn() as conn:
        migrate_local_conversation_session(conn, manager, cid, machine_id=machine.subject_id)
    assert target_manifest.read_bytes() == b"controlled conflicting target"
    still_unknown()
    target_manifest.write_bytes(valid_manifest)
    with db.get_conn() as conn:
        result = migrate_local_conversation_session(conn, manager, cid, machine_id=machine.subject_id)
        assert target_lock.exists()
    assert result["session_initialized"] is initialized
    manifest, migrated, state = session_evidence(target_root / "control/pi", claim["session_id"], conversation_id=cid)
    assert state is initialized and migrated.split(b"\n", 1)[1] == original.split(b"\n", 1)[1]
    assert manifest["sdk_session_id"] == json.loads(source_manifest)["sdk_session_id"]
    assert (directory / "session.jsonl").read_bytes() == original
    assert (directory / "manifest.json").read_bytes() == source_manifest
    assert (target_root / "workspace" / cid / "user.md").read_bytes() == (workspace / "user.md").read_bytes()
    assert conversation_initialized(manager, cid, claim["session_id"]) is initialized
    with db.get_conn() as conn:
        assert conn.execute("SELECT unavailable_reason FROM agent_conversations WHERE id = ?", (cid,)).fetchone()["unavailable_reason"] is None
        # 单独未知会话缺来源：保留 session 身份、历史失败以及局部续聊拒绝。
        unknown = service.create_conversation(conn, owner)["id"]
        conn.execute("UPDATE agent_conversations SET session_initialized=NULL, unavailable_reason='session_verification_required', session_machine_id=? WHERE id=?", (machine.subject_id, unknown))
        identity = conn.execute("SELECT session_id FROM agent_conversations WHERE id=?", (unknown,)).fetchone()["session_id"]
    with pytest.raises(ValueError), db.get_conn() as conn:
        migrate_local_conversation_session(conn, manager, unknown, machine_id=machine.subject_id)
    with db.get_conn() as conn:
        with pytest.raises(HTTPException):
            service.submit_message(conn, unknown, str(uuid4()), "不能静默重开", owner)
        row = conn.execute("SELECT * FROM agent_conversations WHERE id=?", (unknown,)).fetchone()
        assert row["session_id"] == identity and row["session_initialized"] is None


def test_claim_journal_failure_is_local_and_accepted_restart_never_reexecutes(tmp_path, caplog):
    loop = loop_fixture(tmp_path)
    root = loop._agent_claim_directory()
    invalid = root / (sha256(b"broken").hexdigest() + ".json")
    invalid.write_text("{")
    accepted = root / (sha256(b"accepted").hexdigest() + ".json")
    accepted.write_text(json.dumps({"conversation_id": "accepted", "request_id": str(uuid4()), "attempts": 1, "state": "accepted"}))
    loop.client = SimpleNamespace(claim_agent_turn=Mock(return_value=ApiResult(204)),
        pending_agent_turns=Mock(return_value=ApiResult(200, [
            {"conversation_id": cid, "session_machine_id": None} for cid in ("broken", "accepted", "healthy")
        ])))
    loop._dispatch_agent_turns()
    assert [call.args[0] for call in loop.client.claim_agent_turn.call_args_list] == ["healthy"]
    loop.agent_executor.submit.assert_not_called()
    assert invalid.read_text() == "{" and json.loads(accepted.read_bytes())["state"] == "accepted"
    assert "chat_claim_receipt_invalid" in caplog.text and "chat_claim_execution_unknown" in caplog.text


@pytest.mark.parametrize("damage", ["missing", "locked", "owner", "digest", "chain", "shape"])
def test_invalid_persistent_session_evidence_stays_unknown(tmp_path, damage):
    session = "pi:" + str(uuid4())
    root = tmp_path / "conversation/control/pi"
    directory = root / session[3:]
    directory.mkdir(parents=True)
    owner = {"conversation_id": "c1", "session": session}
    header = {"type": "session", "version": 3, "id": "sdk", "cwd": "/home/kali/workspace", "cairn": owner}
    raw = (json.dumps(header) + "\n").encode()
    manifest = {"protocol": "cairn-pi-session-v1", **owner, "cwd": header["cwd"], "sdk_session_id": "sdk",
                "phase_family": "chat", "binding_sha256": sha256(b'{"conversation_id":"c1"}').hexdigest(),
                "sha256": sha256(raw).hexdigest(), "initialized": False}
    if damage == "locked":
        (root / (session[3:] + ".lock")).write_text("existing writer")
    elif damage == "owner":
        manifest["conversation_id"] = "other"
    elif damage == "digest":
        manifest["sha256"] = "0" * 64
    elif damage in {"chain", "shape"}:
        entry = {"id": "entry", "timestamp": "test", "type": "message", "message": {"role": "assistant"}}
        raw += (json.dumps(entry if damage == "chain" else []) + "\n").encode()
        manifest["sha256"] = sha256(raw).hexdigest()
    (directory / "session.jsonl").write_bytes(raw)
    if damage != "missing":
        (directory / "manifest.json").write_text(json.dumps(manifest))
    manager = SimpleNamespace(chat_directory=lambda _: tmp_path / "conversation")
    assert conversation_initialized(manager, "c1", session) is None
    assert (directory / "session.jsonl").read_bytes() == raw
