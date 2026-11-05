"""研究上下文的受控只读验证，不连接模型或目标。"""

import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from cairn.dispatcher.runtime.project_tools import ProjectToolBinding
from cairn.dispatcher.runtime.research_sessions import read_session_page
from cairn.server import db
from cairn.server.services import agent_research_context as context
from cairn.server.services.agent_conversations import _tool_params
from test_postgres_bootstrap import postgres_instance, empty_postgres_url


def _session(tmp_path):
    session_id = "pi:" + str(uuid4())
    path = tmp_path / "project" / "control" / "pi" / session_id[3:] / "session.jsonl"
    path.parent.mkdir(parents=True)
    lines = [
        {"type": "session", "cairn": {"project_id": "project", "session": session_id}},
        {"type": "message", "message": {"role": "assistant", "content": "原始研究观察"}},
        {"type": "message", "message": {"role": "toolResult", "content": "Authorization: Bearer synthetic-protected-value"}},
        {"type": "message", "message": {"role": "assistant", "content": "后续已保存事实"}},
    ]
    raw = "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines)
    path.write_text(raw + '{"type":', encoding="utf-8")
    manager = SimpleNamespace(traffic_execution=SimpleNamespace(root=tmp_path))
    reference = {"project_id": "project", "task_run_id": "r1", "session_id": session_id,
                 "storage": "persistent_control", "offset": 0, "limit": 2}
    return path, manager, reference


def test_running_session_pages_preserve_raw_and_incomplete_tail(tmp_path):
    path, manager, reference = _session(tmp_path)
    before = path.read_bytes()
    first = read_session_page(manager, reference)
    assert len(first["entries"]) == 2 and first["next_offset"] == 2
    assert first["entries"][1]["raw"].endswith("\n")
    second = read_session_page(manager, {**reference, "offset": 2, "limit": 10})
    assert [entry["line"] for entry in second["entries"]] == [2, 3]
    assert second["incomplete_tail"] is True and second["next_offset"] == 4
    # 活动执行继续落盘后，原 offset 读取新完整行，不接管写者。
    with path.open("ab") as stream:
        stream.write(b'"message"}\n')
    third = read_session_page(manager, {**reference, "offset": 4, "limit": 10})
    assert third["entries"] == [{"line": 4, "raw": '{"type":"message"}\n'}]
    assert path.read_bytes() == before + b'"message"}\n'
    assert read_session_page(manager, {**reference, "project_id": "other"})["availability"] == "unavailable"


def test_session_output_boundary_and_bridge_private_method(tmp_path, monkeypatch):
    path, manager, reference = _session(tmp_path)
    raw = path.read_bytes()
    page = read_session_page(manager, {**reference, "limit": 10})
    _tool_params(context.SESSION_PAGE_METHOD, {"task_run_id": "r1", "page": page})
    run = {"id": "r1", "session_id": reference["session_id"]}
    monkeypatch.setattr(context, "_research_run", lambda *args: run)
    monkeypatch.setattr(context, "_source_secrets", lambda *args: [b"synthetic-platform-key"])
    conn = SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchall=lambda: [{"id": "r1"}]))
    result = context.admit_research_session_page(conn, "project", {"task_run_id": "r1", "page": page})
    assert result["availability"] == "partial"
    assert result["missing"] == [{"line": 2, "reason": "protected_authentication_material"}]
    assert "synthetic-protected-value" not in json.dumps(result)
    assert result["entries"][1]["raw"] == page["entries"][1]["raw"]
    assert path.read_bytes() == raw
    calls = []
    def action(*args):
        calls.append(args[-1])
        return SimpleNamespace(ok=True, data=reference if len(calls) == 1 else result)
    binding = ProjectToolBinding(SimpleNamespace(agent_turn_action=action), "project", "conversation", "message", "chat-run", [], manager=manager)
    assert binding.dispatch({"method": "read_research_session", "params": {"task_run_id": "r1"}, "tool_call_id": "call"}) == result
    assert len(calls) == 2 and calls[1]["method"] == context.SESSION_PAGE_METHOD
    with pytest.raises(ValueError, match="project_tool_request_invalid"):
        binding.dispatch({"method": context.SESSION_PAGE_METHOD, "params": {}, "tool_call_id": "call"})


def test_research_parameter_validation():
    assert context._protected(json.dumps({"content": 'value-"secret"-inside'}), [b'value-"secret"-inside'])
    _tool_params("read_research_context", {"source": "facts", "project_id": "other"})
    for params in ({"source": "unknown"}, {"source": []}, {"source": "facts", "project_id": "../other"},
                   {"source": "facts", "offset": -1}, {"source": "facts", "chunk_size": 20000}):
        with pytest.raises(HTTPException) as error:
            _tool_params("read_research_context", params)
        assert error.value.status_code == 422
    with pytest.raises(HTTPException):
        _tool_params("read_research_session", {})


@pytest.mark.skipif(os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1", reason="仅使用显式启用的一次性 PostgreSQL")
def test_research_queries_are_scoped_paginated_and_readonly(empty_postgres_url, tmp_path, monkeypatch):
    db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")
    monkeypatch.setattr(context, "_source_secrets", lambda *args: [b"synthetic-platform-key"])
    with db.get_conn() as conn:
        for project_id in ("project", "other"):
            conn.execute("INSERT INTO projects(id, title, status, created_at) VALUES (?, ?, 'active', '2026-10-29T00:00:00Z')", (project_id, project_id))
        for run_id, task_type in (("r1", "explore"), ("r2", "reason"), ("r3", "agent_chat")):
            conn.execute("INSERT INTO task_runs(id, project_id, task_type, worker_name, worker_type, status, session_id, started_at) "
                         "VALUES (?, 'project', ?, 'worker', 'pi', 'running', ?, '2026-10-29T00:00:00Z')",
                         (run_id, task_type, "pi:" + str(uuid4())))
        conn.execute("INSERT INTO agent_events(id, task_run_id, project_id, ts, kind, payload_json) "
                     "VALUES ('e1', 'r1', 'project', '2026-10-29T00:01:00Z', 'observation', ?)", (json.dumps({"text": "完整事件"}),))
        conn.execute("INSERT INTO facts(id, project_id, description, task_run_id) VALUES ('f1', 'project', ?, 'r1')", ("原始事实" * 30,))
        conn.execute("INSERT INTO facts(id, project_id, description) VALUES ('f1', 'other', '其他项目材料')")
        conn.execute("INSERT INTO intents(id, project_id, description, creator, created_at, task_run_id) VALUES ('i1', 'project', '待验证方向', 'worker', '2026-10-29T00:00:00Z', 'r2')")
        conn.execute("INSERT INTO intent_sources(intent_id, project_id, fact_id) VALUES ('i1', 'project', 'f1')")
        conn.execute("INSERT INTO http_capture_scopes(id, project_id, task_run_id, generation, container_id, dispatcher_id, created_at) "
                     "VALUES ('scope1', 'project', 'r1', 0, 'controlled', 'controlled', '2026-10-29T00:00:00Z')")
        conn.execute("INSERT INTO http_flows(id, project_id, capture_scope_id, revision, record_json, record_sha256, state, method, host, created_cursor, updated_cursor, received_at, updated_at) "
                     "VALUES ('flow1', 'project', 'scope1', 1, '{\"state\":\"receiving\"}', ?, 'receiving', 'GET', 'controlled.invalid', 1, 1, '2026-10-29T00:00:00Z', '2026-10-29T00:01:00Z')", ("0" * 64,))
    with db.get_conn() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        before = dict(conn.execute("SELECT * FROM projects WHERE id = 'project'").fetchone())
        first = context.list_research_sessions(conn, "project", {"limit": 1})
        assert first["items"][0]["id"] == "r1" and first["items"][0]["updated_at"] == "2026-10-29T00:01:00Z"
        second = context.list_research_sessions(conn, "project", {"offset": first["next_offset"]})
        assert [item["id"] for item in second["items"]] == ["r2"]
        assert context.read_research_context(conn, "project", {"source": "events"})["items"][0]["id"] == "e1"
        flows = context.read_research_context(conn, "project", {"source": "http_flows"})
        assert flows["items"][0]["id"] == "flow1" and flows["items"][0]["task_run_id"] == "r1"
        pending = context.read_research_context(conn, "project", {"source": "http_flows", "item_id": "flow1"})
        assert pending["availability"] == "unavailable" and pending["reason"] == "traffic_not_sealed_or_material_missing"
        chunks, offset = [], 0
        while offset is not None:
            result = context.read_research_context(conn, "project", {"source": "facts", "item_id": "f1", "offset": offset, "chunk_size": 32})
            chunks.append(result["content"])
            offset = result["next_offset"]
        assert json.loads("".join(chunks))["description"] == "原始事实" * 30
        intent = context.read_research_context(conn, "project", {"source": "intents", "item_id": "i1"})
        assert json.loads(intent["content"])["source_fact_ids"] == ["f1"]
        with pytest.raises(HTTPException) as error:
            context.research_session_reference(conn, "other", {"task_run_id": "r1"})
        assert error.value.status_code == 404
        assert dict(conn.execute("SELECT * FROM projects WHERE id = 'project'").fetchone()) == before
        assert conn.execute("SELECT status FROM task_runs WHERE project_id='project' AND id='r1'").fetchone()["status"] == "running"
