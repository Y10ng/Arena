"""仅验证聊天计量载荷；连接、发送及材料读取均为内存替身，不产生业务写入。"""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.scheduler.agent_chat import AgentChatMixin, _PendingAgentFinish
from cairn.dispatcher.tasks.agent_chat import _public_result
from cairn.server.routers.agent_conversations import Finish
from cairn.server.services import agent_conversations as service


USAGE = {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": None, "cache_write_tokens": None}


def test_chat_usage_metadata_survives_validation_and_both_public_rejections():
    claim = {"conversation_id": "c1", "project_id": None, "task_run_id": "tr1", "session_id": "s1"}
    envelope = {"protocol": "cairn-agent-result-v1", "engine": "pi", "phase": "chat_turn",
                "conversation_id": "c1", "project_id": None, "task_run_id": "tr1", "session": "s1",
                "status": "failed", "submission_id": None, "result": None,
                "error": {"code": "provider_error"}, "usage": USAGE, "usage_missing_responses": 2}
    process = SimpleNamespace(cancelled=False, timed_out=False, returncode=0,
                              read_stdout=lambda: json.dumps(envelope))
    parsed = _public_result(process, claim, [])
    assert parsed["usage"] == USAGE and parsed["usage_missing_responses"] == 2
    envelope.update(status="succeeded", result={"text": "synthetic-protected-marker"}, error=None)
    rejected = _public_result(process, claim, ["synthetic-protected-marker"])
    assert rejected["error_code"] == "public_output_forbidden"
    assert rejected["usage"] == USAGE and rejected["usage_missing_responses"] == 2
    envelope.pop("usage_missing_responses")
    assert _public_result(process, claim, ["synthetic-protected-marker"])["usage_missing_responses"] is None
    for invalid in (-1, True, "2", 2.0):
        envelope["usage_missing_responses"] = invalid
        with pytest.raises(ValueError, match="chat_result_usage_missing_responses"):
            _public_result(process, claim, [])
        with pytest.raises(ValidationError):
            Finish(task_run_id="tr1", status="failed", usage_missing_responses=invalid)

    # Server 拒绝正文的既有失败回执分支只发送元数据，不重放或执行业务。
    original = {"status": "succeeded", "text": "synthetic-answer", "error_code": None,
                "usage": USAGE, "usage_missing_responses": 2}
    receipt = _PendingAgentFinish(conversation_id="c1", message_id="1", task_run_id="tr1", result=original)
    loop = AgentChatMixin()
    loop._save_agent_finish = Mock()
    loop.client = SimpleNamespace(agent_turn_action=Mock(side_effect=[
        ApiResult(422, {"detail": "agent_public_content_blocked"}), ApiResult(0),
    ]))
    loop._send_agent_finish(receipt)
    assert receipt.result == original
    loop._send_agent_finish(receipt)
    body = Finish.model_validate(loop.client.agent_turn_action.call_args.args[-1])
    assert body.status == "failed" and body.text == ""
    assert body.usage == USAGE and body.usage_missing_responses == 2
    assert Finish(task_run_id="tr1", status="failed").usage_missing_responses is None
    assert Finish(task_run_id="tr1", status="failed", usage_missing_responses=0).usage_missing_responses == 0


def test_chat_usage_finish_persists_once_compares_metadata_and_projects_unknown(monkeypatch):
    context = {}
    body = Finish(task_run_id="tr1", status="failed", text="", error_code="provider_error",
                  usage=USAGE, usage_missing_responses=2)
    row = {"id": 1, "conversation_id": "c1", "client_message_id": "m1", "project_id": None,
           "task_run_id": "tr1", "run_status": "running", "text": "", "created_at": "2026-11-01T00:00:00Z",
           "answer": None, "error_code": None, "heartbeat_at": None, "revision": 1,
           **USAGE, "usage_missing_responses": None}
    monkeypatch.setattr(service, "_owned_turn", Mock(return_value=(row, {"id": "tr1"}, context)))
    monkeypatch.setattr(service, "_artifacts", lambda *_: [])
    monkeypatch.setattr(service.attachments, "list_attachments", lambda *_, **__: [])
    conn = Mock()
    conn.execute.return_value.fetchall.return_value = []
    conn.execute.return_value.fetchone.side_effect = lambda: {
        **row, "run_status": body.status, "answer": body.text, "error_code": body.error_code,
        "usage_missing_responses": context.get("usage_missing_responses"),
    }
    first = service.finish_turn(conn, "c1", 1, body, object())
    update = conn.execute.call_args_list[0].args[1]
    saved = json.loads(update[3])
    assert update[-2:] == ("c1", "tr1")
    assert saved["usage"] == USAGE and saved["usage_missing_responses"] == 2
    assert first["usage"] == USAGE and first["usage_missing_responses"] == 2
    row.update(run_status=body.status, answer=body.text, error_code=body.error_code, usage_missing_responses=2)
    writes = lambda: sum(call.args[0].startswith("UPDATE ") for call in conn.execute.call_args_list)
    assert writes() == 3
    assert service.finish_turn(conn, "c1", 1, body, object()) == first
    assert writes() == 3
    with pytest.raises(HTTPException) as conflict:
        service.finish_turn(conn, "c1", 1, body.model_copy(update={"usage_missing_responses": 3}), object())
    assert conflict.value.status_code == 409 and writes() == 3

    context.pop("usage_missing_responses")
    row["usage_missing_responses"] = None
    historical = body.model_copy(update={"usage_missing_responses": None})
    assert service.finish_turn(conn, "c1", 1, historical, object())["usage_missing_responses"] is None
    with pytest.raises(HTTPException) as conflict:
        service.finish_turn(conn, "c1", 1, historical.model_copy(update={"usage_missing_responses": 0}), object())
    assert conflict.value.status_code == 409 and writes() == 3
