"""合成 Server 事务与 Dispatcher 写回联合验证，不运行模型或目标工具。"""
from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.dispatcher.tasks import bootstrap_results, conclude_writer
from cairn.server.models import CompleteRequest, ConcludeRequest, CreateTaskRunRequest
from cairn.server.auth import RequestPrincipal
from cairn.server.routers import intents as intents_router
from cairn.server.services.audit_task_runs import insert_task_run_record
from cairn.server.services.project_completion import complete_project_record_with_conn
from test_web_assessment_phases import conn, patch_connection, seed_intent, seed_project


@pytest.mark.parametrize("lost_operations", [{"conclude"}, {"complete"}, {"conclude", "complete"}])
def test_bootstrap_response_loss_replays_server_receipts_without_repeating_work(conn, monkeypatch, lost_operations):
    seed_project(conn)
    snapshot = {"features": {"execution_contract_version": 1, "execution_engine": "pi"}}
    conn.execute(
        "UPDATE projects SET scenario_key='custom',assessment_protocol='',assessment_progress_json=NULL,scenario_snapshot_json=? WHERE id='p'",
        (json.dumps(snapshot),),
    )
    seed_intent(conn, "bootstrap")
    conn.execute("UPDATE intents SET worker='worker' WHERE id='bootstrap'")
    run = insert_task_run_record(conn, "p", CreateTaskRunRequest(
        task_type="bootstrap", intent_id="bootstrap", worker_name="worker", worker_type="pi", status="running",
    ), principal=RequestPrincipal(kind="machine", subject_id="submission-fixture"))
    conn.commit()
    patch_connection(monkeypatch, conn)
    for module in (bootstrap_results, conclude_writer):
        monkeypatch.setattr(module, "record_protocol_write", lambda *a, **k: None)
    monkeypatch.setattr(bootstrap_results, "record_decision_result", lambda *a, **k: None)
    monkeypatch.setattr(conclude_writer.time, "sleep", lambda seconds: None)
    calls = {"conclude": 0, "complete": 0}

    def response(operation, value):
        calls[operation] += 1
        if operation in lost_operations and calls[operation] == 1:
            # 数据已提交，但 Dispatcher 没有收到响应；重试必须返回同一收据。
            return ApiResult(status_code=0, text="synthetic response loss")
        return ApiResult(status_code=200, data=value.model_dump(by_alias=True))

    class Client(CairnClient):
        def _request_json(self, method, path, *, json):
            assert method == "POST"
            if path == "/projects/p/intents/bootstrap/conclude":
                value = intents_router.conclude("p", "bootstrap", ConcludeRequest.model_validate(json))
                return response("conclude", value)
            assert path == "/projects/p/complete"
            value = complete_project_record_with_conn(conn, "p", CompleteRequest.model_validate(json))
            conn.commit()
            return response("complete", value)

    result = bootstrap_results.write_bootstrap_complete_result(
        Client("http://synthetic.invalid", token="synthetic-test-token"), "p", "bootstrap", "worker", "Synthetic observation", "Synthetic goal met",
        source="bootstrap", phase_ms=1, task_run_id=run.id,
    )
    assert result == "success"
    assert calls == {operation: 2 if operation in lost_operations else 1 for operation in calls}
    assert conn.execute("SELECT count(*) FROM facts").fetchone()[0] == 4
    assert conn.execute("SELECT count(*) FROM intents").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM projects WHERE id='p'").fetchone()[0] == "completed"


def test_bootstrap_completion_write_failure_is_not_success(monkeypatch):
    for module in (bootstrap_results, conclude_writer):
        monkeypatch.setattr(module, "record_protocol_write", lambda *a, **k: None)
    monkeypatch.setattr(bootstrap_results, "record_decision_result", lambda *a, **k: None)
    monkeypatch.setattr(conclude_writer.time, "sleep", lambda seconds: None)
    client = Mock()
    client.conclude_with_evidence.return_value = ApiResult(status_code=200, data={"fact": {"id": "f1"}})
    client.complete_with_evidence.return_value = ApiResult(status_code=503)
    result = bootstrap_results.write_bootstrap_complete_result(
        client, "p", "bootstrap", "worker", "Observed", "Goal met",
        source="bootstrap", phase_ms=1, task_run_id="run",
    )
    assert result == "server_write_failed"
    assert client.conclude_with_evidence.call_count == 1
    assert client.complete_with_evidence.call_count == 3
    client.release.assert_not_called()


@pytest.mark.parametrize("status,expected", [(400, "business_acceptance"), (401, "policy_denied"), (403, "policy_denied"), (409, "business_acceptance"), (422, "business_acceptance"), (503, "server_write_failed")])
def test_conclude_rejection_cannot_relaunch_model_or_leak_server_body(monkeypatch, caplog, status, expected):
    monkeypatch.setattr(conclude_writer.time, "sleep", lambda seconds: None)
    client = Mock()
    client.conclude_with_evidence.return_value = ApiResult(status_code=status, text="PRIVATE_SERVER_CANARY")
    client.create_agent_event.return_value = ApiResult(status_code=200)
    client.release.return_value = ApiResult(status_code=200)
    result = conclude_writer.write_conclude_result_with_fact_id(
        client, "p", "intent", "worker", "observation", source="bootstrap", phase_ms=1, task_run_id="run",
    )
    assert result.status == expected
    assert result.fact_id is None
    assert client.conclude_with_evidence.call_count == (3 if status == 503 else 1)
    assert "PRIVATE_SERVER_CANARY" not in caplog.text
    assert "PRIVATE_SERVER_CANARY" not in repr(client.create_agent_event.call_args_list)


@pytest.mark.parametrize("status,expected_calls", [(0, 3), (408, 3), (429, 3), (503, 3), (403, 1), (409, 1), (422, 1)])
def test_result_write_retry_is_bounded_and_never_retries_rejection(monkeypatch, status, expected_calls):
    waits = []
    monkeypatch.setattr(conclude_writer.time, "sleep", waits.append)
    calls = []

    def operation():
        calls.append(status)
        return ApiResult(status_code=status, text="synthetic failure")

    result = conclude_writer.retry_graph_write(operation)
    assert result.status_code == status
    assert len(calls) == expected_calls
    assert waits == ([0.5, 1.0] if expected_calls == 3 else [])
