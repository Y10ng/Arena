"""会话直接消费者的 PostgreSQL 验证；不启动模型、研究或目标访问。"""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from cairn.server import db
from cairn.server.app import app
from cairn.server.auth import RequestPrincipal, create_machine_principal
from cairn.server.models import CreateVulnerabilityRequest
from cairn.server.services import agent_conversations as service
from cairn.server.services.vulnerabilities import create_vulnerability
from test_agent_task_actions import action_context
from test_postgres_bootstrap import empty_postgres_url, postgres_instance

pytestmark = pytest.mark.skipif(
    os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1", reason="仅使用一次性 PostgreSQL",
)


def _claim(row):
    machine = RequestPrincipal("machine", "controlled-chat-dispatcher")
    with db.get_conn() as conn:
        claim = service.claim_turn(conn, row["conversation_id"], str(uuid4()), machine)
    return machine, claim


def _tool(row, machine, claim, params, *, call_id="export"):
    return service.call_tool(row["conversation_id"], row["id"], claim["task_run_id"],
                             "export_vulnerability_report", params, machine, tool_call_id=call_id)


@pytest.mark.parametrize("revocation", ["disabled", "membership"])
def test_revocation_rejects_tools_but_owner_can_finish(action_context, revocation):
    actor, row = action_context
    machine, claim = _claim(row)
    with db.get_conn() as conn:
        if revocation == "disabled":
            conn.execute("UPDATE users SET disabled = 1 WHERE id = ?", (actor.subject_id,))
        else:
            conn.execute("DELETE FROM tenant_memberships WHERE user_id = ? AND tenant_id = ?", (actor.subject_id, actor.tenant_id))
    with pytest.raises(HTTPException) as denied:
        service.call_tool(row["conversation_id"], row["id"], claim["task_run_id"],
                          "query_project_progress", {}, machine, tool_call_id="revoked")
    assert denied.value.status_code in {403, 404}
    body = SimpleNamespace(task_run_id=claim["task_run_id"], status="failed", text="",
                           error_code="chat_claim_lost", session_initialized=False, usage={"input_tokens": 4, "output_tokens": 0},
                           usage_missing_responses=None)
    with db.get_conn() as conn:
        with pytest.raises(HTTPException) as wrong_owner:
            service.finish_turn(conn, row["conversation_id"], row["id"], body,
                                RequestPrincipal("machine", "other-dispatcher"))
        assert wrong_owner.value.status_code == 403
    with db.get_conn() as conn:
        assert service.heartbeat(conn, row["conversation_id"], row["id"],
                                 claim["task_run_id"], machine) == {"cancelled": False}
        result = service.finish_turn(conn, row["conversation_id"], row["id"], body, machine)
        assert result["status"] == "failed"
        assert service.finish_turn(conn, row["conversation_id"], row["id"], body, machine) == result
        stored = conn.execute("SELECT runtime_context_json FROM task_runs WHERE conversation_id = ? AND id = ?",
                              (row["conversation_id"], claim["task_run_id"])).fetchone()
        assert json.loads(stored["runtime_context_json"])["usage"] == body.usage


def test_pending_keyset_reaches_after_unclaimable_full_page(action_context):
    actor, row = action_context
    with db.get_conn() as conn:
        conversation = conn.execute("SELECT * FROM agent_conversations WHERE id = ?", (row["conversation_id"],)).fetchone()
        for index in range(101):
            cid = f"conv_0000_page_{index:03d}"
            conn.execute(
                "INSERT INTO agent_conversations(id,tenant_id,project_id,model_id,created_by,session_id,created_at) VALUES (?,?,?,?,?,?,?)",
                (cid, actor.tenant_id, row["project_id"], conversation["model_id"], actor.subject_id, "pi:" + str(uuid4()), conversation["created_at"]),
            )
            conn.execute(
                "INSERT INTO agent_messages(conversation_id,project_id,client_message_id,text,created_at) VALUES (?,?,?,?,?)",
                (cid, row["project_id"], str(uuid4()), "本地分页样本", conversation["created_at"]),
            )
        conn.execute("UPDATE users SET disabled = 1 WHERE id = ?", (actor.subject_id,))
        first = service.pending_conversations(conn)
        assert len(first) == 100
        assert first[-1]["conversation_id"] == "conv_0000_page_099"
        second = service.pending_conversations(conn, after=first[-1]["conversation_id"])
        assert second[0]["conversation_id"] == "conv_0000_page_100"
        assert not ({item["conversation_id"] for item in first} & {item["conversation_id"] for item in second})
        assert service.pending_conversations(conn, after=second[-1]["conversation_id"]) == []
    _, token = create_machine_principal("pagination-consumer")
    with TestClient(app) as client:
        assert client.get("/agent-turns/pending").status_code == 401
        response = client.get("/agent-turns/pending", params={"after": "conv_0000_page_099"},
                              headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200
        assert response.json() == second


def test_evidence_export_is_atomic_and_tool_call_idempotent(action_context, monkeypatch):
    _, row = action_context
    machine, claim = _claim(row)
    with db.get_conn() as conn:
        vulnerability = create_vulnerability(conn, row["project_id"], CreateVulnerabilityRequest(
            title="合成历史发现", severity="low", source_fact_id="origin",
        ))
    params = {"vulnerability_id": vulnerability.id}
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(lambda _: _tool(row, machine, claim, params), range(2)))
    assert outputs[0] == outputs[1]
    identifier = outputs[0]["artifact"]["id"]
    with db.get_conn() as conn:
        message = conn.execute("SELECT artifact_ids_json FROM agent_messages WHERE id = ?", (row["id"],)).fetchone()
        assert json.loads(message["artifact_ids_json"]) == [identifier]
        assert conn.execute("SELECT count(*) AS n FROM artifacts WHERE conversation_id = ? AND task_run_id = ?",
                            (row["conversation_id"], claim["task_run_id"])).fetchone()["n"] == 1
    with pytest.raises(HTTPException) as conflict:
        _tool(row, machine, claim, {**params, "template_key": "changed"})
    assert conflict.value.status_code == 409

    def do_not_render_again(*args, **kwargs):
        raise AssertionError("重传不能重新取材或渲染")

    monkeypatch.setattr(service, "render_vulnerability_report", do_not_render_again)
    assert _tool(row, machine, claim, params) == outputs[0]


def test_evidence_export_rolls_back_artifact_reference_and_file(action_context, monkeypatch):
    _, row = action_context
    machine, claim = _claim(row)
    with db.get_conn() as conn:
        vulnerability = create_vulnerability(conn, row["project_id"], CreateVulnerabilityRequest(
            title="合成事务发现", severity="low", source_fact_id="origin",
        ))
    before = set(db.artifact_root().rglob("*.md"))

    def fail_reference(*args, **kwargs):
        raise RuntimeError("controlled-reference-failure")

    original = service._artifacts
    monkeypatch.setattr(service, "_artifacts", fail_reference)
    with pytest.raises(RuntimeError, match="controlled-reference-failure"):
        _tool(row, machine, claim, {"vulnerability_id": vulnerability.id})
    with db.get_conn() as conn:
        assert conn.execute("SELECT count(*) AS n FROM artifacts WHERE conversation_id = ? AND task_run_id = ?",
                            (row["conversation_id"], claim["task_run_id"])).fetchone()["n"] == 0
        assert json.loads(conn.execute("SELECT artifact_ids_json FROM agent_messages WHERE id = ?", (row["id"],)).fetchone()["artifact_ids_json"]) == []
    assert set(db.artifact_root().rglob("*.md")) == before
    monkeypatch.setattr(service, "_artifacts", original)
    assert _tool(row, machine, claim, {"vulnerability_id": vulnerability.id})["status"] == "succeeded"
