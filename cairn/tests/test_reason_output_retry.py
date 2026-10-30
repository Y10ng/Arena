"""临时 PostgreSQL、真实调度/Reason/SDK；仅替换容器运输、provider 和时钟。"""
from concurrent.futures import Future
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sys
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.tasks import reason, delivery
from cairn.dispatcher.tasks.candidate_capture_outbox import CandidateCaptureOutbox
from cairn.dispatcher.workers.adapters import pi
from cairn.server import db
from cairn.server.auth import RequestPrincipal, require_dispatcher, require_tenant_project
from cairn.server.routers import audit, candidates, projects, intents
from cairn.server.services import assessment_attempts, project_authorization, project_runtime_diagnostics
from test_report_templates import defaults_v60_conn, private_postgres
from test_goal_driven_authorization_v42 import _seed_goal_project
from test_goal_driven_reason import FakeContainerManager
from test_pi_execution import worker
from test_pi_submission_receipts import _run_reason_submission_fixture
from test_delivery_retry import _decision_loop


class InlineExecutor:
    def submit(self, function, *args, **kwargs):
        result = Future()
        try:
            result.set_result(function(*args, **kwargs))
        except BaseException as exc:
            result.set_exception(exc)
        return result


@pytest.fixture
def recovery(defaults_v60_conn, monkeypatch, tmp_path):
    conn = defaults_v60_conn
    db.run_migrations(conn, "postgresql")
    _seed_goal_project(conn, project_id="p", status="active")
    conn.execute("UPDATE projects SET explore_deadline='2099-01-01T00:00:00Z' WHERE id='p'")
    snapshot = json.loads(conn.execute("SELECT scenario_snapshot_json FROM projects WHERE id='p'").fetchone()["scenario_snapshot_json"])
    snapshot["features"]["http_traffic_execution_version"] = 1
    conn.execute("UPDATE projects SET scenario_snapshot_json=? WHERE id='p'", (json.dumps(snapshot),))
    for identity in ("origin", "goal", "baseline"):
        conn.execute("INSERT INTO facts (id,project_id,description,creator,created_at) VALUES (?,'p',?,'system','2026-10-26T00:00:00Z')", (identity, identity))
    conn.commit()

    @contextmanager
    def connection():
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    # 不读取运行实例配置，也不接受外部业务 DSN。
    for module in tuple(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("cairn.server."):
            if hasattr(module, "get_conn"):
                monkeypatch.setattr(module, "get_conn", connection)
            if hasattr(module, "artifact_root"):
                monkeypatch.setattr(module, "artifact_root", lambda: tmp_path / "artifacts")
    monkeypatch.setattr(project_authorization, "schedule_transaction", connection)
    app = FastAPI()
    for router in (audit.router, candidates.router, projects.router, intents.router):
        app.include_router(router)
    principal = RequestPrincipal(kind="machine", subject_id="synthetic-dispatcher")
    app.dependency_overrides[require_tenant_project] = lambda: principal
    app.dependency_overrides[require_dispatcher] = lambda: principal
    api = CairnClient("http://testserver", token="synthetic-only")
    manager = FakeContainerManager()
    manager.container_name = lambda project_id: "synthetic-reason-container"
    manager.ensure_running = lambda *args: "synthetic-reason-container"
    manager.runtime_metadata = lambda *args: {"container_id": "synthetic-reason-container", "container_state": "running"}
    manager.stop_task_execution = lambda *args: None

    def prepare(project, run_id, cancellation, options):
        scope = uuid4().hex
        return {"protocol": "cairn-task-execution-v1", "scope_id": scope,
                "task_run_id": run_id, "socket_path": f"/run/cairn-exec/{scope}/execute.sock"}

    manager.prepare_task_execution = prepare
    state = SimpleNamespace(api=api, worker=worker(), conn=conn, calls=[], answers=[], legacy=True)
    original_schema = delivery.build_submit_result_schema

    def schema(*args, **kwargs):
        value = deepcopy(original_schema(*args, **kwargs))
        if state.legacy:
            # 模拟历史生产者宽松合同；冻结摘要、请求、spool 必须使用同一份 Schema。
            complete = value["$defs"]["ReasonCompleteResult"]
            complete["required"].remove("from")
            complete["properties"]["from"].pop("minItems", None)
        return value

    monkeypatch.setattr(delivery, "build_submit_result_schema", schema)
    monkeypatch.setattr(pi, "build_submit_result_schema", schema)
    from cairn.dispatcher.worker_protocol import submission
    monkeypatch.setattr(submission, "build_submit_result_schema", schema)

    def transport(manager, name, bound_worker, invocation, **kwargs):
        request = json.loads(invocation.input_files[0].content)
        row = conn.execute("SELECT * FROM task_runs WHERE id=?", (kwargs["task_run_id"],)).fetchone()
        context = json.loads(row["runtime_context_json"])
        process = SimpleNamespace(execution_started=True, execution_completed=False,
                                  execution_identity=(name, uuid4().hex))
        cancellation = kwargs["cancellation"]
        cancellation.register_execution(process)
        cancellation.observe_execution_start(process)
        fixture_state = SimpleNamespace(invocation=invocation, frozen=context["execution_delivery"], run=SimpleNamespace(id=row["id"]))
        captured = _run_reason_submission_fixture(fixture_state, [state.answers.pop(0)])
        # helper 已等待真实 Node 子进程退出；不将 mock 的启动当终止证明。
        process.execution_completed = True
        cancellation.finish_execution(process)
        state.calls.append((request, captured))
        return ProcessResult(0, captured["raw"], "")

    monkeypatch.setattr(reason, "run_worker_process", transport)
    with TestClient(app) as http:
        monkeypatch.setattr(api, "_session", lambda: http)
        state.http = http
        def new_loop():
            loop = _decision_loop(state)
            loop.container_manager = manager
            loop.config.tasks.reason.timeout = 120
            loop.executor = loop.cleanup_executor = InlineExecutor()
            loop.candidate_capture_outbox = CandidateCaptureOutbox(tmp_path / "outbox", owner_id="synthetic-dispatcher")
            loop._finish_runtime_context = lambda *args: {}
            return loop

        loop = new_loop()

        def restart():
            nonlocal loop
            assert not loop.futures
            loop = new_loop()

        state.restart = restart

        def dispatch():
            result = loop._dispatch_reason(api.get_project("p"), "facts: [origin, goal, baseline]\n", "graph_changed")
            loop._reap_futures()
            loop._flush_pending_task_run_finishes()
            loop._flush_pending_task_run_finishes()
            assert not loop.futures
            return result

        state.dispatch = dispatch
        state.runs = lambda: conn.execute("SELECT * FROM task_runs ORDER BY LENGTH(id), id").fetchall()
        state.advance = lambda: monkeypatch.setattr(assessment_attempts, "utcnow", lambda: (datetime.now(timezone.utc) + timedelta(seconds=20)).isoformat())
        yield state


def output(valid=False):
    complete = {"description": "合成事实支持结束，不代表真实目标验收。"}
    if valid:
        complete["from"] = ["baseline"]
    return {"accepted": True, "data": {"rationale": "使用既有合成材料。", "complete": complete, "intents": []}}


@pytest.mark.parametrize("intents_shape", ["missing", "null", "empty"])
def test_dispatcher_rejection_generates_again_and_accepts_once(recovery, intents_shape):
    s = recovery
    valid = output(True)
    if intents_shape == "missing":
        valid["data"].pop("intents")
    elif intents_shape == "null":
        valid["data"]["intents"] = None
    s.answers = [output(), valid]
    assert s.dispatch()
    first = s.runs()[0]
    assert first["status"] == "failed", first
    context = json.loads(first["runtime_context_json"])
    assert context["worker_failure"]["code"] == "reason_completion_sources_invalid"
    assert s.calls[0][1]["outcomes"][0]["code"] == "locally_validated"
    assert len(s.calls[0][1]["provider_schemas"]) == 1
    assert s.conn.execute("SELECT COUNT(*) AS n FROM intents").fetchone()["n"] == 0
    assert not s.dispatch()  # 原 Server 退避生效。
    project = s.conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()
    eligibility = assessment_attempts.execution_retry_eligibility(s.conn, project, "reason")
    runtime = project_runtime_diagnostics.build_project_runtime(s.conn, project, schema_version=2)
    assert runtime.last_error.code == "business_acceptance"
    assert runtime.last_error.task_run_id == first["id"]
    assert runtime.last_error.message == "执行结果未通过业务验收，请查看失败原因和当前调度状态"
    assert runtime.scheduling.code == eligibility["reason"] == "execution_retry_cooldown"
    assert runtime.scheduling.retry_at == eligibility["retry_at"]
    assert assessment_attempts.execution_retry_eligibility(s.conn, project, "reason") == eligibility
    s.advance()
    s.legacy = False
    assert s.dispatch()
    runs = s.runs()
    assert [run["status"] for run in runs] == ["failed", "succeeded"]
    assert runs[0]["session_id"] != runs[1]["session_id"]
    request = s.calls[1][0]
    assert request["resume"] is False
    assert "reason_completion_sources_invalid" in request["prompt"]
    assert "complete.from must contain" in request["prompt"]
    second = json.loads(runs[1]["runtime_context_json"])
    assert second["execution_delivery"]["deadline_at"] == context["execution_delivery"]["deadline_at"]
    assert second["reason_output_retry"]["source_task_run_id"] == first["id"]
    assert s.conn.execute("SELECT COUNT(*) AS n FROM intents WHERE to_fact_id='goal'").fetchone()["n"] == 1
    assert not s.dispatch()
    assert len(s.calls) == 2
    original = s.conn.execute("SELECT raw_bytes FROM task_run_candidates WHERE task_run_id=?", (first["id"],)).fetchone()
    assert "from" not in json.loads(bytes(original["raw_bytes"]))["data"]["complete"]


def test_repeated_post_validation_failure_exhausts_same_chain(recovery):
    s = recovery
    s.answers = [output()] * 3
    assert s.dispatch()
    s.advance()
    s.restart()
    assert s.dispatch()
    s.restart()
    assert s.dispatch()
    s.restart()
    assert not s.dispatch()
    assert len(s.calls) == 3
    assert [run["status"] for run in s.runs()] == ["failed"] * 3
    project = s.conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()
    assert assessment_attempts.execution_retry_eligibility(s.conn, project, "reason")["reason"] == "execution_retry_exhausted"


@pytest.mark.parametrize("boundary,expected", [
    ("accepted", "execution_retry_blocked"),
    ("fact", "execution_retry_blocked"),
    ("pending_candidate", "execution_retry_blocked"),
    ("termination_unknown", "assessment_execution_not_quiescent"),
    ("pause_requested", "assessment_execution_not_quiescent"),
    ("old_code", "execution_retry_blocked"),
    ("wrong_origin", "execution_retry_blocked"),
    ("deadline", "execution_delivery_unavailable"),
    ("authorization", "authorization_expired"),
])
def test_route_claim_and_admission_do_not_bypass_boundaries(recovery, boundary, expected):
    s = recovery
    s.answers = [output()]
    assert s.dispatch()
    s.advance()
    assert s.api.claim_reason("p", s.worker.name, "graph_changed").ok
    run = s.runs()[0]
    context = json.loads(run["runtime_context_json"])
    # 故障注入仅改本 fixture 的合成记录，模拟 ACK 丢失或历史诊断；不更改业务库。
    if boundary == "accepted":
        context["accepted_completion_digest"] = "a" * 64
    elif boundary == "fact":
        s.conn.execute("INSERT INTO facts (id,project_id,description,creator,created_at,task_run_id) VALUES ('accepted','p','synthetic','system','now',?)", (run["id"],))
    elif boundary == "pending_candidate":
        candidate = dict(s.conn.execute("SELECT * FROM task_run_candidates WHERE task_run_id=?", (run["id"],)).fetchone())
        candidate.update(id="synthetic-pending", producer_candidate_key="synthetic-pending")
        s.conn.execute(
            f"INSERT INTO task_run_candidates ({','.join(candidate)}) VALUES ({','.join('?' for _ in candidate)})",
            tuple(candidate.values()),
        )
    elif boundary == "termination_unknown":
        context["execution_lifecycle"]["termination_confirmed"] = False
    elif boundary == "pause_requested":
        s.conn.execute("UPDATE task_runs SET pause_requested=1 WHERE id=?", (run["id"],))
    elif boundary == "old_code":
        context["worker_failure"]["code"] = "phase_contract_invalid"
    elif boundary == "wrong_origin":
        context["worker_failure"]["origin"] = "server_acceptance"
    elif boundary == "deadline":
        context["execution_delivery"]["deadline_at"] = "2000-01-01T00:00:00Z"
    elif boundary == "authorization":
        s.conn.execute("UPDATE projects SET authorization_expires_at='2000-01-01T00:00:00Z' WHERE id='p'")
    s.conn.execute("UPDATE task_runs SET runtime_context_json=? WHERE id=?", (json.dumps(context), run["id"]))
    s.conn.commit()
    routes = s.http.get("/projects/p/task-runs").json()
    route = routes[-1]["delivery_retry_route"]
    assert route["allowed"] is False
    if boundary != "authorization":
        assert route["reason"] == expected
    assert not s.dispatch()
    claim = s.api.claim_reason("p", s.worker.name, "graph_changed")
    assert claim.status_code in (403, 409), claim
    admission = s.http.post("/projects/p/task-runs", json={
        "task_type": "reason", "worker_name": s.worker.name, "worker_type": "pi",
        "expected_execution_generation": 3,
        "runtime_context": {"dispatcher_id": "synthetic-dispatcher"},
    })
    assert admission.status_code in (403, 409), admission.text
    if boundary != "authorization":
        assert claim.data["detail"] == admission.json()["detail"] == expected
    assert len(s.runs()) == len(s.calls) == 1


def test_fresh_admission_cannot_forge_context_or_extend_original_deadline(recovery):
    s = recovery
    s.answers = [output()]
    assert s.dispatch()
    s.advance()
    assert s.api.claim_reason("p", s.worker.name, "graph_changed").ok
    body = {"task_type": "reason", "worker_name": s.worker.name, "worker_type": "pi", "status": "running", "expected_execution_generation": 3}
    forged = s.http.post("/projects/p/task-runs", json={**body, "runtime_context": {"reason_output_retry": {}}})
    assert forged.status_code == 422
    reused = s.http.post("/projects/p/task-runs", json={**body, "session_id": s.runs()[0]["session_id"]})
    assert reused.status_code == 409
    assert reused.json()["detail"] == "execution_delivery_session_already_bound"
    response = s.http.post("/projects/p/task-runs", json=body)
    assert response.status_code == 201, response.text
    admitted = response.json()
    old = json.loads(s.runs()[0]["runtime_context_json"])["execution_delivery"]
    snapshot = {**old, "session_id": "pi:" + str(uuid4()), "deadline_at": "2098-01-01T00:00:00Z"}
    rejected = s.http.patch(f'/projects/p/task-runs/{admitted["id"]}', json={
        "session_id": snapshot["session_id"], "runtime_context": {"execution_delivery": snapshot},
    })
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["detail"] == "execution_delivery_invalid"
    assert admitted["runtime_context"]["reason_output_retry"]["deadline_at"] == old["deadline_at"]
    assert len(s.calls) == 1


@pytest.mark.parametrize("data", [{"rationale": "no decision"}, {"intents": []}])
def test_empty_decision_is_recoverable_only_when_queue_is_empty(data):
    from cairn.dispatcher.contracts import validate_reason_payload
    from cairn.dispatcher.worker_result_gateway import validate_worker_payload

    payload = {"protocol": "cairn-assessment-v2", "accepted": True, "kind": "reason_result", "data": data}

    def validate(empty):
        return lambda value: (validate_reason_payload(value, open_intents_empty=empty, max_intents=3)[:2])

    rejected = validate_worker_payload(payload, phase="reason_execute", validator=validate(True))
    assert rejected.failure.code == "reason_decision_empty"
    assert rejected.failure.origin == "dispatcher_validation"
    assert validate_worker_payload(payload, phase="reason_execute", validator=validate(False)).failure is None
