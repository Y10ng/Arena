"""R2a 报告材料的 PostgreSQL/HTTP 验证；不调用模型或目标。"""
from contextlib import contextmanager
import json
import sys
from time import perf_counter
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import psycopg
from psycopg.rows import dict_row

from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.scheduler.dispatch_cycle import DispatchCycleMixin
from cairn.server import db
from cairn.server.auth import RequestPrincipal, require_dispatcher, require_tenant_project
from cairn.server.routers import audit, projects
from cairn.server.services import assessment_reports
from test_report_templates import defaults_v60_conn, private_postgres, _seed_project


def test_chat_does_not_block_report_and_packet_uses_unlocked_snapshot(defaults_v60_conn, private_postgres, monkeypatch, tmp_path):
    conn = defaults_v60_conn
    db.run_migrations(conn, "postgresql")
    _seed_project(conn)
    conn.execute("UPDATE projects SET status = 'stopped' WHERE id = 'p1'")
    conn.execute("INSERT INTO task_runs (id, project_id, task_type, worker_name, worker_type, status, started_at) VALUES ('chat', 'p1', 'agent_chat', 'chat', 'pi', 'running', 'before')")
    conn.execute("INSERT INTO facts (id, project_id, description, creator, created_at) VALUES ('f1', 'p1', '冻结前的研究事实', 'worker', 'before')")
    schema = conn.execute("SELECT current_schema() AS name").fetchone()["name"]
    conn.commit()

    source_queries = []
    class Connection(db.PostgresConnection):
        def execute(self, query, params=None):
            if query.startswith("SELECT DISTINCT json_build_object("):
                source_queries.append(params)
            return super().execute(query, params)

    @contextmanager
    def connection():
        with psycopg.connect(**private_postgres, options=f"-c search_path={schema}", row_factory=dict_row) as raw:
            yield Connection(raw)

    for module in tuple(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("cairn.server."):
            if hasattr(module, "get_conn"):
                monkeypatch.setattr(module, "get_conn", connection)
            if hasattr(module, "artifact_root"):
                monkeypatch.setattr(module, "artifact_root", lambda: tmp_path / "artifacts")
    app = FastAPI()
    app.include_router(audit.router)
    app.include_router(projects.router)
    principal = RequestPrincipal(kind="machine", subject_id="synthetic-dispatcher")
    app.dependency_overrides[require_tenant_project] = lambda: principal
    app.dependency_overrides[require_dispatcher] = lambda: principal
    client = CairnClient("http://testserver", token="synthetic-only")
    with TestClient(app) as transport:
        monkeypatch.setattr(client, "_session", lambda: transport)
        evidence = client.create_artifact("p1", task_run_id=None, kind="raw_output", content=b"HTTP/1.1 200 OK\r\n\r\ncontrolled evidence", filename="response.http", metadata={"source": "synthetic_capture"})
        assert evidence.ok, evidence.text
        unused = client.create_artifact("p1", task_run_id="chat", kind="raw_output", content=b"only a chat export", filename="export.md", metadata={"source": "synthetic_export"})
        assert unused.ok, unused.text
        evidence_id = evidence.data["id"]
        with connection() as write:
            write.execute("INSERT INTO vulnerabilities (id, project_id, title, severity, created_at, updated_at) VALUES ('v1', 'p1', '受控发现', 'high', 'before', 'before')")
            write.execute("""INSERT INTO vulnerability_observations
                (id, project_id, vulnerability_id, severity, confidence, observed_at, evidence_items_json)
                VALUES ('o1', 'p1', 'v1', 'high', 0.8, 'before', ?)""", (json.dumps([{"id": "e1", "kind": "http", "source": "capture", "title": "响应", "response": {"artifact_id": evidence_id}}]),))
            requested = assessment_reports.request_assessment_report_generation(write, "p1", template_key="vulnerability-report", requested_by="synthetic-user")
        claimed = assessment_reports.claim_assessment_report_finalization("p1", "report-worker", generation_token=requested.generation_token, principal=principal)
        assert claimed.generation_token == requested.generation_token

        # 正常编辑项目/研究事实可与材料读取重叠：不得等待长写锁，也不能混入这次快照。
        original = assessment_reports._report_discovery_rows
        writes = []
        def update_during_read(read, project_id, **kwargs):
            assert read.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"] == "repeatable read"
            assert read.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "on"
            if not writes:
                with connection() as write:
                    write.execute("SET LOCAL lock_timeout = '500ms'")
                    write.execute("UPDATE projects SET title = '材料冻结后改名' WHERE id = 'p1'")
                    write.execute("UPDATE facts SET description = '材料冻结后的研究事实' WHERE project_id = 'p1' AND id = 'f1'")
                writes.append(True)
            return original(read, project_id, **kwargs)
        monkeypatch.setattr(assessment_reports, "_report_discovery_rows", update_during_read)
        source_queries.clear()
        started = perf_counter()
        result = client.get_assessment_report_packet("p1", generation_token=requested.generation_token)
        elapsed = perf_counter() - started
        assert result.ok, result.text
        assert elapsed < client._timeout
        packet = result.data
        assert packet["project"]["title"] == "受控报告"
        assert packet["facts"][0]["description"] == "冻结前的研究事实"
        assert packet["task_runs"] == []
        assert packet["assessment_summary"]["execution"]["current_tasks"] == []
        assert packet["assessment_summary"]["calls_by_task_type"] == []
        assert {artifact["id"] for artifact in packet["artifacts"]} == {evidence_id}
        assert packet["artifact_contents"][evidence_id]["text"].endswith("controlled evidence")
        assert unused.data["id"] not in json.dumps(packet)
        assert writes == [True]
        assert len(source_queries) == 1
        print(f"R2a controlled Packet HTTP: {elapsed:.3f}s")

        # 真实研究执行仍然阻断；仅排除控制聊天，不取消研究静止要求。
        with connection() as write:
            write.execute("UPDATE task_runs SET task_type = 'explore' WHERE id = 'chat' AND project_id = 'p1'")
        blocked = client.get_assessment_report_packet("p1", generation_token=requested.generation_token)
        assert blocked.status_code == 409
        assert blocked.data["detail"] == "assessment_report_tasks_not_quiescent"

    dispatched = []
    scheduler = SimpleNamespace(
        _ordered_projects=lambda summaries: summaries,
        _ordinary_task_count=lambda: 1,
        config=SimpleNamespace(runtime=SimpleNamespace(max_workers=4, max_project_workers=2)),
        futures={"chat": SimpleNamespace(project_id="p1", task_type="agent_chat")},
        client=SimpleNamespace(list_assessment_report_finalizations=lambda project_id: SimpleNamespace(ok=True, data=[{
            "requested_at": "before", "status": "pending", "scope_key": "", "generation_token": requested.generation_token,
        }])),
        _try_dispatch_completed_report=lambda project_id, report_state: dispatched.append(project_id) or True,
    )
    DispatchCycleMixin._dispatch_completed_reports(scheduler, [SimpleNamespace(id="p1", status="stopped", verification_request_id=None)])
    assert dispatched == ["p1"]
