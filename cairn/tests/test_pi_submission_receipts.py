"""合成 Server 事务与 Dispatcher 写回联合验证，不运行模型或目标工具。"""
from __future__ import annotations

from base64 import b64decode, b64encode
from concurrent.futures import Future
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.dispatcher.scheduler.task_lifecycle import TaskLifecycleMixin
from cairn.dispatcher.tasks import bootstrap_results, conclude_writer
from cairn.dispatcher.worker_result_gateway import WorkerResultFailure
from cairn.server.models import CompleteRequest, ConcludeRequest, CreateTaskRunRequest, UpdateTaskRunRequest
from cairn.server.auth import RequestPrincipal
from cairn.server.routers import intents as intents_router
from cairn.server.services import audit_task_runs, project_reason
from cairn.server.services.assessment_attempts import execution_retry_eligibility
from cairn.server.services.audit_task_runs import insert_task_run_record
from cairn.server.services.project_completion import complete_project_record_with_conn
from test_goal_driven_authorization_v42 import _seed_goal_project
from test_web_assessment_phases import TEST_DISPATCHER, conn, patch_connection, seed_intent, seed_project, start


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


@pytest.mark.parametrize("error_type", [
    "candidate_integrity", "readiness_timeout", "readiness_transport", "readiness_oom",
    "readiness_invalid", "readiness_process", "container_runtime_mismatch",
])
def test_task_run_finish_preserves_classification_and_settles_non_retryable_failure(conn, monkeypatch, error_type):
    _seed_goal_project(conn, project_id="p", status="active")
    conn.execute("UPDATE projects SET execution_generation=0, explore_deadline='2099-01-01T00:00:00Z' WHERE id='p'")
    conn.executemany(
        "INSERT INTO facts (id,project_id,description,creator,created_at) VALUES (?,'p',?,'system','2026-09-21T00:00:00Z')",
        [(fact_id, fact_id) for fact_id in ("origin", "goal", "baseline")],
    )
    seed_intent(conn, "i")
    run = start(conn, "i")
    conn.commit()
    patch_connection(monkeypatch, conn)
    monkeypatch.setattr(audit_task_runs, "get_conn", project_reason.get_conn)
    budget_before = tuple(conn.execute(
        "SELECT explore_budget, explore_attempts_started, verification_attempts_started FROM projects WHERE id='p'",
    ).fetchone())
    charge_before = tuple(conn.execute(
        "SELECT charged, execution_count, max_runtime_retries FROM assessment_attempts WHERE id=?",
        (run.assessment_attempt_id,),
    ).fetchone())
    assert budget_before == (10, 1, 0)
    assert charge_before[0:2] == (1, 1)

    failure = WorkerResultFailure(
        failure_class="candidate_integrity", code="candidate_spool_limit",
        message="候选暂存容量已达上限，完整性校验失败", fingerprint="",
        origin="dispatcher_candidate_capture", phase="explore_execute", exit_code=0,
    ) if error_type == "candidate_integrity" else None
    sent, acknowledged = [], []

    class Client(CairnClient):
        def _request_json(self, method, path, **kwargs):
            assert (method, path) == ("PATCH", f"/projects/p/task-runs/{run.id}")
            # 只替换 HTTP 边界；请求由真实 CairnClient 生成，再穿过 JSON 与 Server 校验和事务。
            wire_json = json.dumps(kwargs["json"], ensure_ascii=False)
            sent.append(json.loads(wire_json))
            value = audit_task_runs.update_task_run_record(
                "p", run.id, UpdateTaskRunRequest.model_validate_json(wire_json), principal=TEST_DISPATCHER,
            )
            acknowledged.append(json.loads(value.model_dump_json(by_alias=True)))
            return ApiResult(status_code=200, data=acknowledged[-1])

    class Scheduler(TaskLifecycleMixin):
        def _finish_runtime_context(self, *args):
            # 不执行真实容器或工具版本探针，终态发送与失败映射保持生产实现。
            return {}

        def _clear_project_log_state(self, *args):
            pass

        def _worker_cooldown_error_type(self, error_type):
            return None

    scheduler = Scheduler()
    scheduler.client = Client("http://synthetic.invalid", token="synthetic-test-token")
    future = Future()
    future.set_result(failure if failure is not None else error_type)
    scheduler.futures = {future: SimpleNamespace(
        project_id="p", task_run_id=run.id, task_type="explore", worker_name="worker",
        worker_type="pi", container_runtime=None, binding_circuit_key=None,
    )}
    scheduler._reap_futures()
    pending = scheduler._pending_task_run_finishes()
    assert list(pending) == [("p", run.id)]
    assert scheduler._send_task_run_finish(pending[("p", run.id)]) == "acknowledged"
    assert len(sent) == len(acknowledged) == 1
    assert sent[0]["status"] == acknowledged[0]["status"] == "failed"
    assert sent[0]["error_type"] == acknowledged[0]["error_type"] == error_type
    assert acknowledged[0]["runtime_context"]["error_type"] == error_type

    stored_run = conn.execute("SELECT * FROM task_runs WHERE id=?", (run.id,)).fetchone()
    stored_context = json.loads(stored_run["runtime_context_json"])
    assert (stored_run["status"], stored_run["error_type"]) == ("failed", error_type)
    if failure is not None:
        assert sent[0]["error_message"] == acknowledged[0]["error_message"] == stored_run["error_message"] == failure.message
        assert sent[0]["exit_code"] == acknowledged[0]["exit_code"] == stored_run["exit_code"] == 0
        for context in (sent[0]["runtime_context"], acknowledged[0]["runtime_context"], stored_context):
            assert context["worker_failure"] == failure.details()
            assert context["worker_failure"]["error_type"] == "candidate_integrity"
            assert context["worker_failure"]["code"] == "candidate_spool_limit"
            assert context["worker_failure"]["origin"] == failure.origin
            assert context["worker_failure"]["phase"] == failure.phase
            assert context["worker_failure"]["message"] == failure.message

    attempt = conn.execute("SELECT * FROM assessment_attempts WHERE id=?", (run.assessment_attempt_id,)).fetchone()
    assert (attempt["status"], attempt["outcome"]) == ("failed", error_type)
    assert attempt["ended_at"] is not None
    intent = conn.execute("SELECT * FROM intents WHERE id='i'").fetchone()
    assert (intent["conclusion"], intent["conclusion_reason"]) == ("failed", error_type)
    assert intent["concluded_at"] is not None
    assert intent["worker"] is None
    assert intent["to_fact_id"] is None
    project = conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()
    assert execution_retry_eligibility(conn, project, "explore", intent_id="i", ignore_cooldown=True) == {
        "allowed": False, "reason": "execution_retry_blocked", "retry_at": None,
    }
    with pytest.raises(HTTPException):
        start(conn, "i")
    assert conn.execute("SELECT count(*) FROM task_runs WHERE project_id='p'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM assessment_attempts WHERE project_id='p'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM facts WHERE project_id='p'").fetchone()[0] == 3
    assert tuple(conn.execute(
        "SELECT explore_budget, explore_attempts_started, verification_attempts_started FROM projects WHERE id='p'",
    ).fetchone()) == budget_before
    assert tuple(conn.execute(
        "SELECT charged, execution_count, max_runtime_retries FROM assessment_attempts WHERE id=?",
        (run.assessment_attempt_id,),
    ).fetchone()) == charge_before


def _goal_reason_delivery(conn, monkeypatch, *, open_work=False):
    from cairn.dispatcher.prompting import load_prompt, render_prompt
    from cairn.dispatcher.tasks import reason_writes
    from cairn.dispatcher.tasks.delivery import register_delivery
    from cairn.dispatcher.tasks.reason_context import build_reason_prompt_context
    from cairn.dispatcher.workers.adapters.pi import PiDriver
    from cairn.server.models import CreateIntentRequest, ReasonClaimRequest
    from cairn.server.services import candidate_store, project_records
    from test_goal_driven_reason import FakeContainerManager
    from test_pi_execution import worker

    _seed_goal_project(conn, project_id="p", status="active")
    conn.execute("UPDATE projects SET execution_generation=0, explore_deadline='2099-01-01T00:00:00Z' WHERE id='p'")
    conn.executemany(
        "INSERT INTO facts (id,project_id,description,creator,created_at) VALUES (?,'p',?,'system','2026-09-21T00:00:00Z')",
        [(fact_id, fact_id) for fact_id in ("origin", "goal", "baseline")],
    )
    if open_work:
        seed_intent(conn, "open-work")
        conn.execute("INSERT INTO intent_sources (intent_id,project_id,fact_id,source_order) VALUES ('open-work','p','baseline',0)")
    conn.commit()
    patch_connection(monkeypatch, conn)
    for module in (audit_task_runs, project_records, candidate_store):
        monkeypatch.setattr(module, "get_conn", project_reason.get_conn)
    for name in ("record_protocol_write", "record_decision_result"):
        monkeypatch.setattr(reason_writes, name, lambda *a, **k: None)
    bound_worker = worker()
    project_reason.claim_project_reason_record("p", ReasonClaimRequest(worker=bound_worker.name, trigger="facts_changed"))
    run = insert_task_run_record(conn, "p", CreateTaskRunRequest(
        task_type="reason", worker_name=bound_worker.name, worker_type="pi", status="running",
    ), principal=TEST_DISPATCHER)
    conn.commit()
    sent, accepted = [], []

    class Client(CairnClient):
        def _request_json(self, method, path, **kwargs):
            wire = json.dumps(kwargs["json"], ensure_ascii=False)
            if (method, path) == ("PATCH", f"/projects/p/task-runs/{run.id}"):
                value = audit_task_runs.update_task_run_record(
                    "p", run.id, UpdateTaskRunRequest.model_validate_json(wire), principal=TEST_DISPATCHER,
                )
                return ApiResult(status_code=200, data=value.model_dump(mode="json", by_alias=True))
            assert (method, path) == ("POST", "/projects/p/intents")
            sent.append(json.loads(wire))
            value = intents_router.create_intent("p", CreateIntentRequest.model_validate_json(wire), principal=TEST_DISPATCHER)
            accepted.append(value.model_dump(mode="json", by_alias=True))
            return ApiResult(status_code=201, data=accepted[-1])

    client = Client("http://synthetic.invalid", token="synthetic-test-token")
    project = project_records.get_project_detail("p")
    manager = FakeContainerManager()
    manager.runtime_metadata = lambda name: {"container_id": "synthetic-reason-container", "container_state": "running"}
    manager.container_name = lambda project_id: "synthetic-reason-container"
    context = build_reason_prompt_context(
        SimpleNamespace(tasks=SimpleNamespace(reason=SimpleNamespace(max_intents=3))),
        manager, "synthetic-reason-container", project, "facts: [origin, goal, baseline]", client=client,
    )
    session = PiDriver.prepare_session()
    registered = register_delivery(
        client, manager, "synthetic-reason-container", project, run.id, bound_worker,
        phase="reason_execute", session=session, timeout_seconds=120,
        binding=context.binding, inputs={"open_intent_ids": [item["id"] for item in context.open_intents]},
    )
    frozen = json.loads(conn.execute("SELECT runtime_context_json FROM task_runs WHERE id=?", (run.id,)).fetchone()[0])["execution_delivery"]
    assert frozen == registered
    invocation = PiDriver().build_execute(
        bound_worker, render_prompt(load_prompt("goal-driven-v1", "reason.md"), context.replacements), session,
        phase="reason_execute", project_id="p", task_run_id=run.id, timeout_seconds=60,
        binding=frozen["binding"], inputs=frozen["inputs"],
    )
    return SimpleNamespace(conn=conn, client=client, run=run, worker=bound_worker, manager=manager,
                           frozen=frozen, invocation=invocation, sent=sent, accepted=accepted)


def _run_reason_submission_fixture(state, attempts):
    from cairn.dispatcher.tasks.candidate_intake import decode_candidate_spool
    from cairn.dispatcher.tasks.delivery import result_schema_digest
    from cairn.dispatcher.worker_protocol.candidate_authority import candidate_authority_sha256
    from cairn.server.schemas.candidates import CandidateFrameManifest, CandidateOutcomeManifest, canonical_value_sha256
    from cairn.server.services import candidate_store

    request = json.loads(state.invocation.input_files[0].content)
    # fixture 仅接管 loopback provider 和临时工作区；Schema、会话及 candidate authority 均取生产请求。
    script = r"""
import { readFile, rm } from 'node:fs/promises';
import { fixture } from './tests/fixtures.mjs';
import { parseRequest } from './dist/protocol.js';
import { runRequest } from './dist/runner.js';
let input = '';
for await (const chunk of process.stdin) input += chunk;
const { request: production, attempts } = JSON.parse(input);
const cleanups = [];
try {
  const f = await fixture({ after: (cleanup) => cleanups.push(cleanup) }, 'anthropic-messages',
    attempts.map((args) => ({ tools: [{ name: 'submit_result', args }] })));
  cleanups.push(() => rm(production.capabilities.candidate_capture.spool_path, { force: true }));
  const request = parseRequest({ ...production, model: f.request.model, cwd: f.cwd, session_root: f.request.session_root });
  const audit = [];
  const envelope = await runRequest(request, { audit: (event) => audit.push(event) });
  const rejections = f.calls.flatMap((call) => call.body.messages.flatMap((message) =>
    Array.isArray(message.content) ? message.content.filter((block) => block.type === 'tool_result' && block.is_error)
      .map((block) => JSON.parse(typeof block.content === 'string' ? block.content : block.content.map((part) => part.text).join(''))) : []));
  process.stdout.write(JSON.stringify({ raw: JSON.stringify(envelope), audit, rejections,
    provider_schemas: f.calls.map((call) => call.body.tools.find((tool) => tool.name === 'submit_result').input_schema),
    tool_scopes: f.calls.map((call) => call.body.tools.map((tool) => tool.name).sort()),
    spool_base64: (await readFile(request.capabilities.candidate_capture.spool_path)).toString('base64'), cwd: f.cwd }));
} finally {
  for (const cleanup of cleanups.reverse()) await cleanup();
}
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=Path(__file__).resolve().parents[2] / "container" / "pi-agent",
        input=json.dumps({"request": request, "attempts": attempts}, ensure_ascii=False),
        text=True, capture_output=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    captured = json.loads(completed.stdout)
    assert captured["provider_schemas"] == [request["result_schema"]] * len(attempts)
    expected_hash = canonical_value_sha256(request["result_schema"])
    assert expected_hash == candidate_authority_sha256(request["result_schema"]) == state.frozen["result_schema_sha256"]
    assert expected_hash == state.invocation.candidate_spool.result_schema_sha256
    assert expected_hash == result_schema_digest("reason_execute", state.frozen["binding"], state.frozen["inputs"])
    entries = decode_candidate_spool(
        b64decode(captured["spool_base64"], validate=True), state.invocation.candidate_spool,
        project_id="p", task_run_id=state.run.id, phase="reason_execute", session=state.frozen["session_id"],
    )
    candidates, outcomes = [], []
    for entry in entries:
        manifest = json.loads(entry.manifest_bytes)
        if entry.record_kind == "candidate":
            candidates.append(manifest)
            assert manifest["result_schema_sha256"] == expected_hash
            assert manifest["binding_sha256"] == candidate_authority_sha256(state.frozen["binding"])
            receipt = candidate_store.capture_candidate(
                "p", state.run.id, CandidateFrameManifest.model_validate(manifest),
                entry.manifest_bytes, entry.raw, principal=TEST_DISPATCHER,
            )
        else:
            outcomes.append(manifest)
            receipt = candidate_store.capture_candidate_outcome(
                "p", state.run.id, CandidateOutcomeManifest.model_validate(manifest),
                entry.manifest_bytes, entry.raw, principal=TEST_DISPATCHER,
            )
        assert receipt.disposition == "received"
    assert len(candidates) == len(attempts)
    assert {event["session"] for event in captured["audit"]} == {state.frozen["session_id"]}
    assert {event["task_run_id"] for event in captured["audit"]} == {state.run.id}
    assert not Path(captured["cwd"]).exists()
    assert not Path(state.invocation.candidate_spool.path).exists()
    captured["candidates"], captured["outcomes"] = candidates, outcomes
    return captured


def _decode_reason_submission(state, raw):
    from cairn.dispatcher.contracts import validate_reason_payload
    from cairn.dispatcher.worker_result_gateway import decode_pi_result

    def validate(payload):
        kind, data, abandon = validate_reason_payload(
            payload, open_intents_empty=not state.frozen["inputs"]["open_intent_ids"],
            max_intents=state.frozen["binding"]["max_intents"], goal_driven=True,
            allowed_intent_ids=state.frozen["binding"]["allowed_intent_ids"],
        )
        return kind, (data, abandon)

    return decode_pi_result(raw, phase="reason_execute", project_id="p", task_run_id=state.run.id,
                            session=state.frozen["session_id"], validator=validate)


def _write_decoded_reason(state, decoded):
    from cairn.dispatcher.tasks.common import ProcessArtifactRecord
    from cairn.dispatcher.tasks.reason_writes import write_reason_decision

    data, abandon = decoded.validated
    return write_reason_decision(
        state.client, "p", state.worker, state.run.id, kind=decoded.kind, data=data,
        abandon=abandon, payload=decoded.payload, evidence=ProcessArtifactRecord(refs=[]),
        declared_artifacts=ProcessArtifactRecord(refs=[]), execute_ms=1, total_ms=1,
    )


def test_goal_reason_empty_work_repairs_in_session_and_server_accepts_intent_receipt(conn, monkeypatch):
    from cairn.dispatcher.tasks.delivery import DeliveryUnavailable, resume_delivery
    from cairn.dispatcher.worker_protocol.submission import build_submit_result_schema

    state = _goal_reason_delivery(conn, monkeypatch)
    assert state.frozen["inputs"] == {"open_intent_ids": []}
    for inputs in (None, {}):
        with pytest.raises(ValueError):
            build_submit_result_schema("reason_execute", state.frozen["binding"], inputs=inputs)
    retry_context = {"delivery_retry": {"source_task_run_id": state.run.id}, "execution_delivery": state.frozen}
    container_id, restored, remaining = resume_delivery(
        state.manager, "p", state.worker, retry_context, "reason_execute",
    )
    assert container_id == state.frozen["container_id"] and remaining > 0
    assert restored is state.frozen
    changed_context = json.loads(json.dumps(retry_context))
    changed_context["execution_delivery"]["inputs"]["open_intent_ids"] = ["open-work"]
    with pytest.raises(DeliveryUnavailable):
        resume_delivery(state.manager, "p", state.worker, changed_context, "reason_execute")
    assert state.sent == []
    persisted = json.loads(conn.execute("SELECT runtime_context_json FROM task_runs WHERE id=?", (state.run.id,)).fetchone()[0])
    assert persisted["execution_delivery"] == state.frozen
    rationale = "合成事实支持继续研究，不把未测试写成安全结论。"
    decision = {"from": ["baseline", "origin"], "description": "只安排后续合成证据核对，不访问目标。", "priority": 2}
    captured = _run_reason_submission_fixture(state, [
        {"accepted": True, "data": {"rationale": rationale}},
        {"accepted": True, "data": {"rationale": rationale, "intents": [decision]}},
    ])
    assert json.loads(captured["raw"])["status"] == "succeeded", captured["raw"]
    assert [item["code"] for item in captured["rejections"]] == ["submission_invalid"]
    assert [item["code"] for item in captured["outcomes"]] == ["submission_invalid", "locally_validated"]
    assert captured["candidates"][1]["parent_producer_candidate_key"] == captured["candidates"][0]["producer_candidate_key"]
    assert [event["code"] for event in captured["audit"] if event["event"] == "result_repair"] == ["submission_invalid"]
    assert captured["tool_scopes"][1] == ["read_evidence", "register_artifact", "submit_result"]
    assert conn.execute("SELECT count(*) FROM intents").fetchone()[0] == 0
    decoded = _decode_reason_submission(state, captured["raw"])
    assert decoded.succeeded and decoded.kind == "intents", decoded.failure
    assert _write_decoded_reason(state, decoded) == "success"
    assert len(state.accepted) == 1
    intent_id = state.accepted[0]["id"]
    intent = conn.execute("SELECT * FROM intents WHERE id=?", (intent_id,)).fetchone()
    assert (intent["creator"], intent["task_run_id"], intent["priority"]) == (state.worker.name, state.run.id, 2)
    assert intent["description"] == decision["description"]
    assert [tuple(row) for row in conn.execute(
        "SELECT fact_id,source_order FROM intent_sources WHERE intent_id=? ORDER BY source_order", (intent_id,),
    )] == [("baseline", 0), ("origin", 1)]
    context = json.loads(conn.execute("SELECT runtime_context_json FROM task_runs WHERE id=?", (state.run.id,)).fetchone()[0])
    assert context["accepted_intent_submissions"]["0"]["intent_id"] == intent_id
    assert context["execution_delivery"] == state.frozen
    assert _write_decoded_reason(state, decoded) == "success"
    assert state.accepted[0] == state.accepted[1]
    assert conn.execute("SELECT count(*) FROM intents").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM intent_sources").fetchone()[0] == 2
    state.client.close()


@pytest.mark.parametrize("no_actions", [{}, {"intents": [], "abandon": [], "complete": None},
                                         {"intents": None, "abandon": [], "complete": None}],
                         ids=["missing", "empty-arrays", "null-intents"])
def test_goal_reason_empty_work_rejects_no_actions_but_allows_explicit_rejection(conn, monkeypatch, no_actions):
    state = _goal_reason_delivery(conn, monkeypatch)
    captured = _run_reason_submission_fixture(state, [
        {"accepted": True, "data": {"rationale": "尚无动作，必须在本地拒绝。", **no_actions}},
        {"accepted": False, "reason": "合成材料不足，拒绝补造动作。"},
    ])
    assert json.loads(captured["raw"])["status"] == "succeeded", captured["raw"]
    assert [item["code"] for item in captured["rejections"]] == ["submission_invalid"]
    assert [item["code"] for item in captured["outcomes"]] == ["submission_invalid", "locally_validated"]
    decoded = _decode_reason_submission(state, captured["raw"])
    assert decoded.succeeded and decoded.kind == "rejected", decoded.failure
    assert state.sent == []
    assert conn.execute("SELECT count(*) FROM intents").fetchone()[0] == 0
    state.client.close()


def test_goal_reason_open_work_rationale_only_is_runner_noop_without_graph_write(conn, monkeypatch):
    state = _goal_reason_delivery(conn, monkeypatch, open_work=True)
    assert state.frozen["inputs"] == {"open_intent_ids": ["open-work"]}
    before = [tuple(row) for row in conn.execute("SELECT * FROM intents")]
    sources = [tuple(row) for row in conn.execute("SELECT * FROM intent_sources")]
    captured = _run_reason_submission_fixture(state, [
        {"accepted": True, "data": {"rationale": "等待已有开放工作，不重复安排或宣称完成。"}},
    ])
    assert json.loads(captured["raw"])["status"] == "succeeded", captured["raw"]
    assert captured["rejections"] == []
    assert [item["code"] for item in captured["outcomes"]] == ["locally_validated"]
    assert not any(event["event"] == "result_repair" for event in captured["audit"])
    decoded = _decode_reason_submission(state, captured["raw"])
    assert decoded.succeeded and decoded.kind == "noop", decoded.failure
    assert _write_decoded_reason(state, decoded) == "success"
    assert state.sent == []
    assert [tuple(row) for row in conn.execute("SELECT * FROM intents")] == before
    assert [tuple(row) for row in conn.execute("SELECT * FROM intent_sources")] == sources
    context = json.loads(conn.execute("SELECT runtime_context_json FROM task_runs WHERE id=?", (state.run.id,)).fetchone()[0])
    assert "accepted_intent_submissions" not in context
    state.client.close()


def test_explore_thinking_length_continues_to_synthetic_evidence_and_server_conclude(conn, monkeypatch, tmp_path):
    from cairn.dispatcher import contracts
    from cairn.dispatcher.tasks.artifact_records import record_declared_artifacts
    from cairn.dispatcher.tasks.conclude_writer import build_assessment_record_for_conclude
    from cairn.dispatcher.worker_result_gateway import decode_pi_result
    from cairn.dispatcher.workers.adapters.pi import PiDriver
    from cairn.server.models import CreateArtifactRequest
    from cairn.server.schemas.candidates import canonical_value_sha256
    from cairn.server.services import audit_artifacts
    from test_goal_driven_lifecycle import _seed_running_goal_driven_explore, _SESSION
    from test_pi_execution import worker

    _seed_running_goal_driven_explore(conn)
    patch_connection(monkeypatch, conn)
    monkeypatch.setattr(audit_artifacts, "get_conn", project_reason.get_conn)
    monkeypatch.setattr(audit_artifacts, "_artifact_disk_path", lambda project_id, run_id, filename: tmp_path / filename)
    binding = {"assessment_protocol": "goal-driven-v1"}
    invocation = PiDriver().build_execute(
        worker(), "只用本地合成材料验证交付，不访问任何目标。", _SESSION,
        phase="explore_execute", project_id="p", task_run_id="run-1", timeout_seconds=20, binding=binding,
    )
    request = json.loads(invocation.input_files[0].content)
    context = json.loads(conn.execute("SELECT runtime_context_json FROM task_runs WHERE id='run-1'").fetchone()[0])
    context["execution_delivery"].update(binding=binding, result_schema_sha256=canonical_value_sha256(request["result_schema"]))
    conn.execute("UPDATE task_runs SET runtime_context_json=? WHERE id='run-1'", (json.dumps(context),))
    conn.commit()
    script = r"""
import { readFile, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { fixture } from './tests/fixtures.mjs';
import { parseRequest } from './dist/protocol.js';
import { runRequest } from './dist/runner.js';
let input = '';
for await (const chunk of process.stdin) input += chunk;
const production = JSON.parse(input);
const cleanups = [];
try {
  const f = await fixture({ after: (cleanup) => cleanups.push(cleanup) }, 'anthropic-messages', [
    { thinking: 'synthetic unfinished reasoning', stopReason: 'max_tokens' },
    { tools: [{ name: 'bash', args: { command: "printf synthetic-evidence > evidence.txt; printf executed >> actions.txt" } }] },
    { tools: [{ name: 'register_artifact', args: { path: 'evidence.txt', kind: 'raw_output' } }] },
    (call) => {
      const results = call.body.messages.flatMap((message) => Array.isArray(message.content)
        ? message.content.filter((block) => block.type === 'tool_result') : []);
      const block = results.at(-1);
      const artifact = JSON.parse(typeof block.content === 'string' ? block.content : block.content.map((part) => part.text).join(''));
      return { tools: [{ name: 'submit_result', args: { accepted: true, data: {
        fact: { description: '本地合成证据已生成，不代表真实目标已测试。' },
        assessment_record: { version: 1, kind: 'experiment', hypothesis: 'thinking length 后仍可继续生成并交付合成材料？',
          outcome: 'tested_no_finding', observations: ['synthetic-evidence'], limitations: ['仅验证合成交付'],
          evidence_refs: [{ artifact_handle: artifact.handle }] },
      } } }] };
    },
  ]);
  cleanups.push(() => rm(production.capabilities.candidate_capture.spool_path, { force: true }));
  const request = parseRequest({ ...production, model: f.request.model, cwd: f.cwd, session_root: f.request.session_root });
  const audit = [];
  const envelope = await runRequest(request, { audit: (event) => audit.push(event) });
  const materials = {};
  for (const item of envelope.result?.data.artifacts ?? []) materials[item.path] = (await readFile(item.path)).toString('base64');
  process.stdout.write(JSON.stringify({ raw: JSON.stringify(envelope), audit, materials,
    actions: await readFile(join(f.cwd, 'actions.txt'), 'utf8'), cwd: f.cwd,
    provider_schemas: f.calls.map((call) => call.body.tools.find((tool) => tool.name === 'submit_result').input_schema),
    tool_scopes: f.calls.map((call) => call.body.tools.map((tool) => tool.name).sort()) }));
} finally {
  for (const cleanup of cleanups.reverse()) await cleanup();
}
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=Path(__file__).resolve().parents[2] / "container" / "pi-agent",
        input=json.dumps(request, ensure_ascii=False), text=True, capture_output=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    captured = json.loads(completed.stdout)
    assert json.loads(captured["raw"])["status"] == "succeeded", captured["raw"]
    assert captured["provider_schemas"] == [request["result_schema"]] * 4
    assert invocation.candidate_spool.result_schema_sha256 == context["execution_delivery"]["result_schema_sha256"]
    assert captured["actions"] == "executed"
    assert "bash" in captured["tool_scopes"][1]
    assert [event["event"] for event in captured["audit"] if event["event"] in {"output_continuation", "result_repair"}] == ["output_continuation"]
    assert len([event for event in captured["audit"] if event["event"] == "tool_execution_start" and event["tool_name"] == "bash"]) == 1
    assert {event["session"] for event in captured["audit"]} == {_SESSION}
    assert {event["task_run_id"] for event in captured["audit"]} == {"run-1"}
    assert not Path(captured["cwd"]).exists()
    assert not Path(invocation.candidate_spool.path).exists()
    decoded = decode_pi_result(
        captured["raw"], phase="explore_execute", project_id="p", task_run_id="run-1", session=_SESSION,
        assessment_protocol="goal-driven-v1",
        validator=lambda payload: contracts.validate_explore_payload(
            payload, goal_driven=True, strict_contract=True, expected_kind="explore_result", outcome_contract_version=2,
        ),
    )
    assert decoded.succeeded, decoded.failure
    materials = {path: b64decode(raw, validate=True) for path, raw in captured["materials"].items()}

    class MaterialIO:
        def read_file(self, container_name, path):
            assert container_name == "synthetic-container"
            return materials[path]

    class ArtifactIO:
        def create_artifact(self, project_id, *, task_run_id, kind, content, filename, metadata):
            artifact = audit_artifacts.create_artifact_record(project_id, CreateArtifactRequest(
                task_run_id=task_run_id, kind=kind, content_base64=b64encode(content.read()).decode("ascii"),
                filename=filename, metadata=metadata,
            ))
            return ApiResult(status_code=201, data=artifact.model_dump(mode="json"))

    declared = record_declared_artifacts(
        ArtifactIO(), MaterialIO(), "synthetic-container", "p", "run-1", phase="explore_execute",
        artifacts=contracts.extract_declared_artifacts(decoded.payload),
    )
    assert not declared.collect_failed and not declared.write_failed
    assert len(declared.artifact_ids_by_handle) == 1
    wire, evidence_refs = build_assessment_record_for_conclude(decoded.payload, declared)
    body = ConcludeRequest(worker="worker", task_run_id="run-1", description=decoded.description,
                           assessment_record=wire, evidence=declared.refs + evidence_refs)
    accepted = intents_router.conclude("p", "i1", body)
    assert accepted.fact.task_run_id == "run-1"
    assert accepted.fact.description == decoded.description
    assert accepted.fact.assessment_record["observations"] == ["synthetic-evidence"]
    assert accepted.fact.assessment_record["outcome"] == "tested_no_finding"
    stored_artifact = conn.execute("SELECT * FROM artifacts WHERE task_run_id='run-1'").fetchone()
    assert stored_artifact["id"] in declared.artifact_ids_by_handle.values()
    assert (tmp_path / stored_artifact["uri"].rsplit("/", 1)[1]).read_bytes() == b"synthetic-evidence"
    assert conn.execute("SELECT to_fact_id FROM intents WHERE id='i1'").fetchone()[0] == accepted.fact.id
