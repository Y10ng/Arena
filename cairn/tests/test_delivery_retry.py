"""隔离 HTTP admission 验证持久交付重试，而非进程内重试标记。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_pi_project_lifecycle import conn, client, _seed
from report_test_support import ephemeral_report_finish_outbox
from test_assessment_reports import (
    TEST_DISPATCHER,
    _ready_finish_request,
    _write_report_artifact,
    _write_report_input_snapshot,
    report_claim_db,
)


OWNER = "synthetic-dispatcher"
SESSION = "pi:11111111-1111-4111-8111-111111111111"
_DEFAULT_BINDING = object()


def _report_delivery_failure_request(claim, error_type: str):
    from cairn.server.models import (
        AssessmentReportFinishRequest,
        AssessmentReportTaskRunResult,
    )

    return AssessmentReportFinishRequest(
        protocol="cairn-assessment-report-finish-v1",
        worker="worker-a",
        claim_token=claim.claim_token,
        generation_token=claim.generation_token,
        task_run_id=claim.task_run_id,
        status="failed",
        task_run_result=AssessmentReportTaskRunResult(
            status="failed",
            exit_code=1,
            error_type=error_type,
            error_message=error_type,
        ),
    )


def source_execution(conn, *, error="submission_invalid", lifecycle=True, delivery=None):
    from test_goal_driven_authorization_v42 import _seed_goal_project

    _seed_goal_project(conn, project_id="p", status="active")
    conn.execute("UPDATE projects SET execution_generation=0, explore_deadline='2099-01-01T00:00:00Z' WHERE id='p'")
    for fact_id in ("origin", "goal"):
        conn.execute(
            "INSERT INTO facts (id, project_id, description, creator, created_at) VALUES (?, 'p', ?, 'system', '2020-01-01T00:00:00Z')",
            (fact_id, fact_id),
        )
    snapshot = delivery or {
        "session_id": SESSION,
        "phase": "reason_execute",
        "deadline_at": "2098-01-01T00:00:00Z",
        "container_id": "original-container",
        "model_binding_sha256": "a" * 64,
        "binding": {"allowed_fact_ids": ["origin"], "max_intents": 3},
        "inputs": {"open_intent_ids": []},
    }
    context = {
        "dispatcher_id": OWNER,
        "execution_delivery": snapshot,
        "execution_lifecycle": {"generation": 0, "process_started": True,
                                "termination_confirmed": lifecycle, "cleanup_confirmed": False},
    }
    conn.execute(
        """INSERT INTO task_runs (id, project_id, task_type, worker_name, worker_type,
               status, session_id, started_at, ended_at, error_type, runtime_context_json,
               execution_generation)
           VALUES ('t001', 'p', 'reason', 'pi-worker', 'pi', 'failed', ?,
                   '2020-01-01T00:00:00Z', '2020-01-01T00:00:01Z', ?, ?, 0)""",
        (SESSION, error, json.dumps(context)),
    )
    conn.commit()
    return snapshot


def admit_reason(client, **updates):
    claim = client.post("/projects/p/reason/claim", json={"worker": "pi-worker", "trigger": "finalizing"})
    if claim.status_code != 200:
        return claim
    body = {"task_type": "reason", "worker_name": "pi-worker", "worker_type": "pi",
            "expected_execution_generation": 0, "runtime_context": {"dispatcher_id": OWNER}}
    body.update(updates)
    return client.post("/projects/p/task-runs", json=body)


def test_http_admission_derives_same_session_delivery_from_persisted_failure(conn, client):
    snapshot = source_execution(conn)
    before = client.get("/projects/p/task-runs").json()
    route = before[0]["delivery_retry_route"]
    assert route["dispatcher_id"] == OWNER and route["worker_name"] == "pi-worker"
    assert route["source_task_run_id"] == "t001" and route["container_id"] == "original-container"
    assert route["allowed"] is True
    response = admit_reason(client)
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["session_id"] == SESSION
    assert run["runtime_context"]["delivery_retry"] == {"source_task_run_id": "t001"}
    assert run["runtime_context"]["execution_delivery"] == snapshot
    assert len(client.get("/projects/p/task-runs").json()) == 2


def test_http_prior_successful_reason_does_not_close_new_delivery_obligation(conn, client):
    snapshot = source_execution(conn)
    conn.execute("INSERT INTO task_runs (id, project_id, task_type, worker_name, worker_type, status, started_at, ended_at, runtime_context_json, execution_generation) VALUES ('t000', 'p', 'reason', 'pi-worker', 'pi', 'succeeded', '2019-01-01T00:00:00Z', '2019-01-01T00:00:01Z', ?, 0)",
        (json.dumps({"dispatcher_id": OWNER, "accepted_result_digest": "earlier-business-result"}),))
    conn.commit()
    route = client.get("/projects/p/task-runs").json()[-1]["delivery_retry_route"]
    assert route["allowed"] is True
    admitted = admit_reason(client)
    assert admitted.status_code == 201, admitted.text
    assert admitted.json()["runtime_context"]["execution_delivery"] == snapshot


def test_http_successful_delivery_reason_releases_next_same_generation_reason(conn, client, monkeypatch):
    from types import MethodType
    from unittest.mock import Mock
    from cairn.dispatcher.models import ReasonCheckpoint
    from cairn.dispatcher.protocol.client import CairnClient
    from cairn.dispatcher.scheduler.dispatch_cycle import DispatchCycleMixin
    from test_dispatcher_multi_project_capacity import _loop, _summary
    from test_pi_execution import worker

    source_execution(conn, error="submission_missing")
    conn.execute("INSERT INTO facts (id, project_id, description, creator, created_at) VALUES ('f1', 'p', 'prior evidence', 'system', '2020-01-01T00:00:00Z')")
    conn.commit()
    delivered = admit_reason(client)
    assert delivered.status_code == 201, delivered.text
    run_id = delivered.json()["id"]
    assert client.patch(f"/projects/p/task-runs/{run_id}", json={"status": "succeeded"}).status_code == 200
    assert client.post("/projects/p/reason/release", json={"worker": "pi-worker", "task_run_id": run_id}).status_code == 200

    runs = client.get("/projects/p/task-runs").json()
    assert all(run["delivery_retry_route"] is None for run in runs)
    # 合成 Reason 产出的整批 Intent；Explore 结算经真实 TaskRun API，业务接受由种子模拟。
    for index in range(3):
        intent_id = f"i{index}"
        explore_id = f"e{index}"
        conn.execute("INSERT INTO intents (id, project_id, description, creator, created_at) VALUES (?, 'p', 'synthetic work', 'reason', '2020-01-01T00:00:00Z')", (intent_id,))
        conn.execute("INSERT INTO task_runs (id, project_id, intent_id, task_type, worker_name, worker_type, status, started_at, execution_generation, runtime_context_json) VALUES (?, 'p', ?, 'explore', 'pi-worker', 'pi', 'running', '2020-01-01T00:00:00Z', 0, ?)", (explore_id, intent_id, json.dumps({"dispatcher_id": OWNER})))
        conn.commit()
        assert client.patch(f"/projects/p/task-runs/{explore_id}", json={"status": "succeeded"}).status_code == 200
        conn.execute("UPDATE intents SET concluded_at='2020-01-01T00:00:01Z', conclusion='completed' WHERE id=?", (intent_id,))
        conn.commit()

    api = CairnClient("http://testserver", token="synthetic-test-only")
    monkeypatch.setattr(api, "_session", lambda: client)
    monkeypatch.setattr(api, "export_project", lambda project_id: "accumulated synthetic results")
    loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
    loop._try_dispatch_project = MethodType(DispatchCycleMixin._try_dispatch_project, loop)
    loop.client = api
    loop.dispatcher_id = OWNER
    loop._candidate_project_blocked = lambda project_id: False
    loop.reason_checkpoints = {"p": ReasonCheckpoint(fact_count=3, hint_count=0, open_intent_count=0, settled_intent_count=0, execution_generation=0)}
    bound_worker = worker()
    loop._scenario_for_project = lambda project: SimpleNamespace(key="default", prompt_group="goal-driven-v1", knowledge=None)
    loop._container_runtime_options = lambda *a: None
    loop._select_worker = lambda *a, **kw: SimpleNamespace(worker=bound_worker)
    loop._materialize_selected_worker = lambda *a: (bound_worker, {}, None)
    loop._task_run_runtime_context = lambda *a, **kw: {"dispatcher_id": OWNER}
    loop.executor = SimpleNamespace(submit=Mock(return_value=object()))
    assert loop._try_dispatch_project(_summary("p")) is True
    submitted = loop.executor.submit.call_args
    assert submitted.kwargs["delivery_context"] is None
    assert conn.execute("SELECT reason_trigger FROM projects WHERE id='p'").fetchone()[0] == "settled_intents:0->3"
    next_reason = client.get("/projects/p/task-runs").json()[-1]
    assert next_reason["task_type"] == "reason"
    assert next_reason["session_id"] is None
    assert "delivery_retry" not in next_reason["runtime_context"]
    assert "execution_delivery" not in next_reason["runtime_context"]
    assert next_reason["execution_generation"] == 0
    assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE error_type='submission_missing'").fetchone()[0] == 1
    api.close()


@pytest.mark.parametrize("error", ["provider_error", "provider_rate_limit", "transport_error"])
def test_http_delivery_transient_failure_keeps_original_chain(conn, client, monkeypatch, error):
    from cairn.server.services import assessment_attempts, audit_task_runs
    from cairn.dispatcher.runtime.cancellation import TaskCancellation
    from cairn.dispatcher.tasks import delivery, reason
    from test_pi_execution import worker
    from test_reason_depth_review import execution_view, project_with_execution

    bound_worker = worker()
    binding = {"allowed_fact_ids": ["origin"], "max_intents": 3}
    snapshot = source_execution(conn, error="submission_missing", delivery={
        "session_id": SESSION, "phase": "reason_execute",
        "deadline_at": "2050-01-01T00:01:00Z", "container_id": "original-container",
        "model_binding_sha256": delivery.model_binding_digest(bound_worker),
        "result_schema_sha256": delivery.result_schema_digest("reason_execute", binding),
        "binding": binding, "inputs": {"open_intent_ids": []},
    })
    clock = ["2050-01-01T00:00:00Z"]
    monkeypatch.setattr(assessment_attempts, "utcnow", lambda: clock[0])
    monkeypatch.setattr(audit_task_runs, "utcnow", lambda: clock[0])
    first_retry = admit_reason(client)
    assert first_retry.status_code == 201, first_retry.text
    run_id = first_retry.json()["id"]
    failed = client.patch(f"/projects/p/task-runs/{run_id}", json={
        "status": "failed", "error_type": error,
        "runtime_context": {"execution_lifecycle": {
            "generation": 0, "process_started": True,
            "termination_confirmed": True, "cleanup_confirmed": True,
        }},
    })
    assert failed.status_code == 200, failed.text
    assert client.post("/projects/p/reason/release", json={"worker": "pi-worker", "task_run_id": run_id}).status_code == 200
    route = client.get("/projects/p/task-runs").json()[-1]["delivery_retry_route"]
    assert route["reason"] == "execution_retry_cooldown"
    assert route["retry_at"] == "2050-01-01T00:00:10Z"
    clock[0] = "2050-01-01T00:00:11Z"
    second_retry = admit_reason(client)
    assert second_retry.status_code == 201, second_retry.text
    run = second_retry.json()
    assert run["session_id"] == SESSION
    assert run["worker_name"] == "pi-worker"
    assert run["runtime_context"]["dispatcher_id"] == OWNER
    assert run["runtime_context"]["delivery_retry"] == {"source_task_run_id": run_id}
    assert run["runtime_context"]["execution_delivery"] == snapshot

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(clock[0].replace("Z", "+00:00"))

    monkeypatch.setattr(delivery, "datetime", FrozenDatetime)
    lease = SimpleNamespace(start=lambda: None, stop=lambda: None, failure=None)
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", lambda *a: lease)
    monkeypatch.setattr(reason, "best_effort_release_reason", lambda *a, **kw: None)

    def forbidden(*a, **kw):
        pytest.fail("original delivery attempted fresh exploration or deadline registration")

    monkeypatch.setattr(reason, "build_reason_prompt_context", forbidden)
    monkeypatch.setattr(reason, "record_prompt_rendered", forbidden)
    monkeypatch.setattr(reason, "register_delivery", forbidden)
    manager = SimpleNamespace(ensure_running=forbidden, container_name=lambda p: "original-name",
        runtime_metadata=lambda n: {"container_id": "original-container", "container_state": "running"})
    captured = []

    class ExecutionReached(BaseException):
        pass

    def capture(manager, name, worker, command, **kwargs):
        assert name == snapshot["container_id"]
        captured.append(json.loads(command.input_files[0].content))
        raise ExecutionReached

    monkeypatch.setattr(reason, "run_worker_process", capture)
    config = SimpleNamespace(runtime=SimpleNamespace(interval=1, prompt_group="goal-driven-v1"), tasks=SimpleNamespace(reason=SimpleNamespace(timeout=500, max_intents=3)))
    project = project_with_execution(execution_view(phase="baseline"))
    with pytest.raises(ExecutionReached):
        reason.run_reason_task(config, SimpleNamespace(), manager, project, "changed graph", bound_worker, TaskCancellation(), run["id"], delivery_context=run["runtime_context"])
    request = captured[0]
    assert request["phase"] == snapshot["phase"]
    assert request["session"] == SESSION
    assert request["binding"] == binding
    assert request["resume"] is True and request["conclude_only"] is True
    assert request["timeout_seconds"] == 49
    assert snapshot["deadline_at"] == "2050-01-01T00:01:00Z"


@pytest.mark.parametrize("boundary,reason", [
    ("expired", "execution_delivery_unavailable"),
    ("termination_unconfirmed", "execution_delivery_unavailable"),
    ("quota", "execution_retry_exhausted"),
    ("artifact_changed", "execution_retry_blocked"),
    ("source_missing", "execution_retry_blocked"),
    ("snapshot_changed", "execution_retry_blocked"),
    ("in_flight", "reason_execution_pending"),
])
def test_http_transient_delivery_preserves_blocking_boundaries(conn, client, monkeypatch, boundary, reason):
    from cairn.server.services import assessment_attempts

    source_execution(conn, error="submission_missing")
    monkeypatch.setattr(assessment_attempts, "utcnow", lambda: "2050-01-01T00:00:00Z")
    context = json.loads(conn.execute("SELECT runtime_context_json FROM task_runs WHERE id='t001'").fetchone()[0])
    if boundary == "expired":
        context["execution_delivery"]["deadline_at"] = "2049-01-01T00:00:00Z"
        conn.execute("UPDATE task_runs SET runtime_context_json=? WHERE id='t001'", (json.dumps(context),))
    context["delivery_retry"] = {"source_task_run_id": "t001"}
    if boundary == "termination_unconfirmed":
        context["execution_lifecycle"]["termination_confirmed"] = False
    elif boundary == "source_missing":
        context["delivery_retry"]["source_task_run_id"] = "missing"
    elif boundary == "snapshot_changed":
        context["execution_delivery"]["container_id"] = "replacement"
    conn.execute("INSERT INTO task_runs (id, project_id, task_type, worker_name, worker_type, status, session_id, started_at, ended_at, error_type, runtime_context_json, execution_generation) VALUES ('t002', 'p', 'reason', 'pi-worker', 'pi', ?, ?, '2020-01-01T00:00:02Z', '2020-01-01T00:00:03Z', ?, ?, 0)", ("running" if boundary == "in_flight" else "failed", SESSION, "artifact_error" if boundary == "artifact_changed" else "provider_error", json.dumps(context)))
    if boundary == "quota":
        context["delivery_retry"] = {"source_task_run_id": "t002"}
        conn.execute("INSERT INTO task_runs (id, project_id, task_type, worker_name, worker_type, status, session_id, started_at, ended_at, error_type, runtime_context_json, execution_generation) VALUES ('t003', 'p', 'reason', 'pi-worker', 'pi', 'failed', ?, '2020-01-01T00:00:04Z', '2020-01-01T00:00:05Z', 'provider_error', ?, 0)", (SESSION, json.dumps(context)))
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]
    denied = admit_reason(client)
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"] == reason
    route = client.get("/projects/p/task-runs").json()[-1]["delivery_retry_route"]
    assert route["allowed"] is False
    assert route["reason"] == ("assessment_execution_not_quiescent" if boundary == "in_flight" else reason)
    assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == before


@pytest.mark.parametrize("partial", [False, True])
def test_http_success_boundary_diagnostics_preserve_new_failure_and_partial_receipt(conn, client, partial):
    from cairn.server.services import project_runtime_diagnostics

    source_execution(conn, error="submission_missing")
    delivered = admit_reason(client)
    run_id = delivered.json()["id"]
    assert client.patch(f"/projects/p/task-runs/{run_id}", json={"status": "succeeded"}).status_code == 200
    assert client.post("/projects/p/reason/release", json={"worker": "pi-worker", "task_run_id": run_id}).status_code == 200
    project = conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()
    before = list(conn.iterdump())
    assert project_runtime_diagnostics._retry_decision(conn, project, "2050-01-01T00:00:00Z") is None
    assert project_runtime_diagnostics._last_error(conn, "p").task_run_id == "t001"
    assert list(conn.iterdump()) == before
    new_reason = admit_reason(client)
    assert new_reason.status_code == 201, new_reason.text
    new_id = new_reason.json()["id"]
    context = new_reason.json()["runtime_context"]
    if partial:
        context["accepted_intent_submissions"] = {"0": {"intent_id": "accepted-prefix", "digest": "a" * 64}}
        conn.execute("INSERT INTO intents (id, project_id, task_run_id, description, creator, created_at) VALUES ('accepted-prefix', 'p', ?, 'accepted first item', 'reason', '2020-01-01T00:00:00Z')", (new_id,))
    conn.execute("UPDATE task_runs SET status='failed', error_type=?, ended_at='2020-01-01T00:00:01Z', runtime_context_json=? WHERE id=?", ("provider_error" if partial else "business_acceptance", json.dumps(context), new_id))
    conn.execute("UPDATE projects SET reason_worker=NULL WHERE id='p'")
    conn.commit()
    project = conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()
    before = list(conn.iterdump())
    decision = project_runtime_diagnostics._retry_decision(conn, project, "2050-01-01T00:00:00Z")
    assert decision.code == "execution_retry_blocked"
    assert project_runtime_diagnostics._last_error(conn, "p").task_run_id == new_id
    if partial:
        run = conn.execute("SELECT * FROM task_runs WHERE id=?", (new_id,)).fetchone()
        assert project_runtime_diagnostics._business_receipt_projection(conn, project, run, context)[0] == "partially_accepted"
    assert list(conn.iterdump()) == before
    denied = admit_reason(client)
    assert denied.status_code == 409 and denied.json()["detail"] == "execution_retry_blocked"


@pytest.mark.parametrize("mutation", ["generation_bool", "generation_wrong", "termination_missing", "snapshot_missing", "accepted", "expired", "owner", "session_missing", "legacy_schema", "delivery_session_error"])
def test_http_delivery_blocks_unproven_or_incompatible_persisted_source(conn, client, mutation):
    source_execution(conn)
    context = json.loads(conn.execute("SELECT runtime_context_json FROM task_runs WHERE id='t001'").fetchone()[0])
    if mutation == "generation_bool":
        context["execution_lifecycle"]["generation"] = False
    elif mutation == "generation_wrong":
        context["execution_lifecycle"]["generation"] = 7
    elif mutation == "termination_missing":
        context["execution_lifecycle"]["termination_confirmed"] = False
    elif mutation == "snapshot_missing":
        context.pop("execution_delivery")
    elif mutation == "accepted":
        context["accepted_result_digest"] = "already-accepted"
    elif mutation == "expired":
        context["execution_delivery"]["deadline_at"] = "2000-01-01T00:00:00Z"
    elif mutation == "owner":
        context["dispatcher_id"] = "another-owner"
    elif mutation == "session_missing":
        conn.execute("UPDATE task_runs SET session_id=NULL WHERE id='t001'")
    elif mutation == "legacy_schema":
        conn.execute("UPDATE task_runs SET error_type='result_schema' WHERE id='t001'")
    elif mutation == "delivery_session_error":
        context["delivery_retry"] = {"source_task_run_id": "original"}
        conn.execute("UPDATE task_runs SET error_type='session_error' WHERE id='t001'")
    conn.execute("UPDATE task_runs SET runtime_context_json=? WHERE id='t001'", (json.dumps(context),))
    conn.commit()
    response = admit_reason(client)
    assert response.status_code == 409, response.text
    assert len(client.get("/projects/p/task-runs").json()) == 1


def test_http_delivery_two_retries_keep_original_deadline_and_persistent_cooldown(conn, client, monkeypatch):
    from cairn.server.services import assessment_attempts, audit_task_runs

    snapshot = source_execution(conn)
    clock = ["2050-01-01T00:00:00Z"]
    monkeypatch.setattr(assessment_attempts, "utcnow", lambda: clock[0])
    monkeypatch.setattr(audit_task_runs, "utcnow", lambda: clock[0])
    for retry in range(2):
        admitted = admit_reason(client)
        assert admitted.status_code == 201, admitted.text
        run = admitted.json()
        assert run["runtime_context"]["execution_delivery"] == snapshot
        response = client.patch(f"/projects/p/task-runs/{run['id']}", json={"status": "failed", "error_type": "submission_missing", "runtime_context": {
            "execution_lifecycle": {"generation": 0, "process_started": True, "termination_confirmed": True, "cleanup_confirmed": False}}})
        assert response.status_code == 200, response.text
        assert client.post("/projects/p/reason/release", json={"worker": "pi-worker", "task_run_id": run["id"]}).status_code == 200
        denied = admit_reason(client)
        assert denied.status_code == 409, denied.text
        assert denied.json()["detail"] == ("execution_retry_cooldown" if retry == 0 else "execution_retry_exhausted")
        clock[0] = "2050-01-01T00:01:00Z"
    for _ in range(2):
        assert admit_reason(client).json()["detail"] == "execution_retry_exhausted"
    assert len(client.get("/projects/p/task-runs").json()) == 3


def test_http_concurrent_delivery_admission_reserves_only_one_execution(conn, client, tmp_path, monkeypatch):
    import sqlite3
    from contextlib import contextmanager
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from cairn.server.services import audit_task_runs

    source_execution(conn)
    assert client.post("/projects/p/reason/claim", json={"worker": "pi-worker", "trigger": "finalizing"}).status_code == 200
    database = tmp_path / "isolated-delivery.sqlite"
    with sqlite3.connect(database) as target:
        conn.backup(target)
    @contextmanager
    def independent_connection():
        connection = sqlite3.connect(database, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()
    monkeypatch.setattr(audit_task_runs, "get_conn", independent_connection)
    barrier = Barrier(2)
    def admit(_):
        barrier.wait(timeout=5)
        return client.post("/projects/p/task-runs", json={"task_type": "reason", "worker_name": "pi-worker", "worker_type": "pi", "expected_execution_generation": 0})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(admit, range(2)))
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert len(client.get("/projects/p/task-runs").json()) == 2


def test_explore_delivery_reuses_original_logical_attempt_and_cost(conn, client):
    admitted = admit_structured_explore(conn, client)
    snapshot = explore_delivery_snapshot(admitted)
    context = admitted["runtime_context"]
    context.update(
        {
            "dispatcher_id": OWNER,
            "execution_delivery": snapshot,
            "execution_lifecycle": {
                "generation": 0,
                "process_started": True,
                "termination_confirmed": True,
                "cleanup_confirmed": False,
            },
        }
    )
    conn.execute(
        """
        UPDATE task_runs
        SET status='failed', session_id=?, ended_at='2020-01-01T00:00:01Z',
            error_type='submission_invalid', runtime_context_json=?
        WHERE project_id='p' AND id=?
        """,
        (SESSION, json.dumps(context), admitted["id"]),
    )
    conn.execute(
        "UPDATE intents SET worker=NULL, last_heartbeat_at=NULL WHERE project_id='p' AND id='i1'"
    )
    conn.commit()

    response = client.post("/projects/p/intents/i1/explore-attempts", json={
        "intent_id": "i1", "task_type": "explore", "worker_name": "pi-worker", "worker_type": "pi",
        "expected_execution_generation": 0, "runtime_context": {"dispatcher_id": OWNER},
    })

    assert response.status_code == 201, response.text
    result = response.json()
    assert result["assessment_attempt_id"] == admitted["assessment_attempt_id"]
    assert result["runtime_context"]["assessment_execution_number"] == 2
    assert result["runtime_context"]["execution_delivery"] == snapshot
    project = client.get("/projects/p").json()
    assert project["project"]["explore_attempts_started"] == 1


def test_http_scheduler_passes_server_delivery_context_to_task(conn, client, monkeypatch):
    from unittest.mock import Mock
    from cairn.dispatcher.protocol.client import CairnClient
    from test_dispatcher_multi_project_capacity import _loop
    from test_pi_execution import worker

    snapshot = source_execution(conn)
    api = CairnClient("http://testserver", token="synthetic-test-only")
    monkeypatch.setattr(api, "_session", lambda: client)
    loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
    loop.client = api
    loop.dispatcher_id = OWNER
    bound_worker = worker()
    selection = SimpleNamespace(worker=bound_worker)
    loop._scenario_for_project = lambda project: SimpleNamespace(key="default", prompt_group="default", knowledge=None)
    loop._container_runtime_options = lambda *a: None
    loop._select_worker = lambda *a, **kw: selection
    loop._materialize_selected_worker = lambda *a: (bound_worker, {}, None)
    loop._task_run_runtime_context = lambda *a, **kw: {"dispatcher_id": OWNER}
    loop._project_open_intent_count = lambda project: 0
    loop.executor = SimpleNamespace(submit=Mock(return_value=object()))
    project = SimpleNamespace(project=SimpleNamespace(id="p", execution_generation=0, tenant_workers=[SimpleNamespace(id="pi-worker", engine="pi", enabled=True, currently_available=True)],
                              coverage_checkpoint_cell_count=0, coverage_stop_reason=None), facts=[], hints=[], intents=[])
    assert loop._dispatch_reason(project, "", "finalizing") is True
    context = loop.executor.submit.call_args.kwargs["delivery_context"]
    assert context["delivery_retry"] == {"source_task_run_id": "t001"}
    assert context["execution_delivery"] == snapshot
    api.close()


def test_http_scheduler_passes_fresh_reason_admission_binding_separately(
    conn,
    client,
    monkeypatch,
):
    from unittest.mock import Mock
    from cairn.dispatcher.protocol.client import CairnClient
    from test_dispatcher_multi_project_capacity import _loop
    from test_pi_execution import worker

    _seed(conn)
    now = "2026-09-19T08:00:00Z"
    conn.execute(
        "UPDATE projects SET assessment_progress_json=? WHERE id='p'",
        (json.dumps({"version": 1, "phase": "baseline"}),),
    )
    conn.execute(
        """
        INSERT INTO coverage_slots (
            project_id, slot_key, kind, description, status, tier,
            surface_key, surface_kind, test_family, coverage_requirement,
            applicability_basis, completion_criteria, created_at, updated_at
        ) VALUES ('p', 'cell-1', 'endpoint', 'synthetic', 'pending', 'core',
                  'GET https://example.test/resource/1', 'endpoint',
                  'authorization', 'required', 'authorized identity boundary',
                  'compare owner and non-owner', ?, ?)
        """,
        (now, now),
    )
    conn.commit()
    api = CairnClient("http://testserver", token="synthetic-test-only")
    monkeypatch.setattr(api, "_session", lambda: client)
    loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
    loop.client = api
    loop.dispatcher_id = OWNER
    bound_worker = worker()
    selection = SimpleNamespace(worker=bound_worker)
    loop._scenario_for_project = lambda project: SimpleNamespace(
        key="default", prompt_group="default", knowledge=None
    )
    loop._container_runtime_options = lambda *a: None
    loop._select_worker = lambda *a, **kw: selection
    loop._materialize_selected_worker = lambda *a: (bound_worker, {}, None)
    loop._task_run_runtime_context = lambda *a, **kw: {"dispatcher_id": OWNER}
    loop._project_open_intent_count = lambda project: 0
    loop.executor = SimpleNamespace(submit=Mock(return_value=object()))
    project = SimpleNamespace(
        project=SimpleNamespace(
            id="p",
            execution_generation=0,
            tenant_workers=[
                SimpleNamespace(
                    id="pi-worker",
                    engine="pi",
                    enabled=True,
                    currently_available=True,
                )
            ],
            coverage_checkpoint_cell_count=0,
            coverage_stop_reason=None,
        ),
        facts=[],
        hints=[],
        intents=[],
    )

    assert loop._dispatch_reason(project, "", "baseline") is True

    kwargs = loop.executor.submit.call_args.kwargs
    assert kwargs["delivery_context"] is None
    binding = kwargs["admission_context"]["structured_intent_binding"]
    assert [cell["slot_key"] for cell in binding["eligible_cells"]] == [
        "cell-1"
    ]
    assert not getattr(loop, "_task_admission_contexts", {})
    api.close()


def admit_structured_reason(conn, client):
    _seed(conn)
    now = "2026-09-19T08:00:00Z"
    conn.execute(
        "UPDATE projects SET assessment_progress_json=? WHERE id='p'",
        (json.dumps({"version": 1, "phase": "baseline"}),),
    )
    conn.execute(
        """
        INSERT INTO coverage_slots (
            project_id, slot_key, kind, description, status, tier,
            surface_key, surface_kind, test_family, coverage_requirement,
            applicability_basis, completion_criteria, created_at, updated_at
        ) VALUES ('p', 'cell-1', 'endpoint', 'synthetic', 'pending', 'core',
                  'GET https://example.test/resource/1', 'endpoint',
                  'authorization', 'required', 'authorized identity boundary',
                  'compare owner and non-owner', ?, ?)
        """,
        (now, now),
    )
    conn.commit()
    response = admit_reason(client)
    assert response.status_code == 201, response.text
    return response.json()


def test_reason_delivery_registers_exact_server_admission_binding(
    conn,
    client,
):
    run = admit_structured_reason(conn, client)
    structured = run["runtime_context"]["structured_intent_binding"]
    snapshot = {
        "session_id": SESSION,
        "phase": "reason_execute",
        "deadline_at": "2098-01-01T00:00:00Z",
        "container_id": "original-container",
        "model_binding_sha256": "a" * 64,
        "binding": {
            "allowed_fact_ids": ["origin"],
            "max_intents": 3,
            "structured_intent_binding": structured,
        },
        "inputs": {"open_intent_ids": []},
    }
    response = client.patch(
        f"/projects/p/task-runs/{run['id']}",
        json={
            "session_id": SESSION,
            "runtime_context": {"execution_delivery": snapshot},
        },
    )
    assert response.status_code == 200, response.text
    persisted = client.get("/projects/p/task-runs").json()[0]
    assert persisted["runtime_context"]["execution_delivery"] == snapshot


@pytest.mark.parametrize("mutation", ["missing", "digest", "expanded"])
def test_reason_delivery_cannot_replace_server_admission_binding(
    conn,
    client,
    mutation,
):
    run = admit_structured_reason(conn, client)
    structured = json.loads(
        json.dumps(
            run["runtime_context"]["structured_intent_binding"]
        )
    )
    binding = {
        "allowed_fact_ids": ["origin"],
        "max_intents": 3,
        "structured_intent_binding": structured,
    }
    if mutation == "missing":
        binding.pop("structured_intent_binding")
    elif mutation == "digest":
        structured["digest"] = "f" * 64
    else:
        structured["eligible_cells"].append(
            {**structured["eligible_cells"][0], "slot_key": "forged-cell"}
        )
    snapshot = {
        "session_id": SESSION,
        "phase": "reason_execute",
        "deadline_at": "2098-01-01T00:00:00Z",
        "container_id": "original-container",
        "model_binding_sha256": "a" * 64,
        "binding": binding,
        "inputs": {"open_intent_ids": []},
    }
    response = client.patch(
        f"/projects/p/task-runs/{run['id']}",
        json={
            "session_id": SESSION,
            "runtime_context": {"execution_delivery": snapshot},
        },
    )
    assert response.status_code == 422, response.text
    persisted = client.get("/projects/p/task-runs").json()[0]
    assert "execution_delivery" not in persisted["runtime_context"]


def admit_structured_explore(conn, client):
    _seed(conn)
    now = "2026-09-19T08:00:00Z"
    project = conn.execute(
        "SELECT scenario_snapshot_json FROM projects WHERE id='p'"
    ).fetchone()
    snapshot = json.loads(project["scenario_snapshot_json"])
    snapshot["features"].update(
        {
            "execution_contract_version": 1,
            "execution_engine": "pi",
            "assessment_contract_version": 2,
            "outcome_contract_version": 2,
        }
    )
    conn.execute(
        "UPDATE projects SET scenario_snapshot_json=?, assessment_progress_json=? WHERE id='p'",
        (json.dumps(snapshot), json.dumps({"version": 1, "phase": "baseline"})),
    )
    conn.execute(
        """
        INSERT INTO coverage_slots (
            project_id, slot_key, kind, description, status, tier,
            surface_key, surface_kind, test_family, coverage_requirement,
            applicability_basis, completion_criteria, assigned_intent_id,
            created_at, updated_at
        ) VALUES ('p', 'cell-1', 'endpoint', 'synthetic', 'exploring', 'core',
                  'GET https://example.test/resource/1', 'endpoint',
                  'authorization', 'required', 'authorized identity boundary',
                  'compare owner and non-owner', 'i1', ?, ?)
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO intents (
            id, project_id, description, creator, created_at,
            slot_key, strategy_key
        ) VALUES ('i1', 'p', 'test authorization', 'reason', ?,
                  'cell-1', 'auth.owner-vs-other-v1')
        """,
        (now,),
    )
    conn.execute(
        """
        INSERT INTO intent_sources (intent_id, project_id, fact_id, source_order)
        VALUES ('i1', 'p', 'origin', 0)
        """
    )
    conn.commit()
    response = client.post(
        "/projects/p/intents/i1/explore-attempts",
        json={
            "intent_id": "i1",
            "task_type": "explore",
            "worker_name": "pi-worker",
            "worker_type": "pi",
            "expected_execution_generation": 0,
            "runtime_context": {"dispatcher_id": OWNER},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def explore_delivery_snapshot(run, *, binding=_DEFAULT_BINDING):
    admitted = run["runtime_context"]["structured_explore_binding"]
    cell = admitted["cell"]
    return {
        "session_id": SESSION,
        "phase": "explore_execute",
        "deadline_at": "2098-01-01T00:00:00Z",
        "container_id": "original-container",
        "model_binding_sha256": "a" * 64,
        "binding": binding
        if binding is not _DEFAULT_BINDING
        else {
            "slot_key": cell["slot_key"],
            "surface_key": cell["surface_key"],
            "test_family": cell["test_family"],
        },
        "inputs": {},
    }


def test_explore_delivery_registers_exact_server_admission_binding(conn, client):
    run = admit_structured_explore(conn, client)
    snapshot = explore_delivery_snapshot(run)

    response = client.patch(
        f"/projects/p/task-runs/{run['id']}",
        json={
            "session_id": SESSION,
            "runtime_context": {"execution_delivery": snapshot},
        },
    )

    assert response.status_code == 200, response.text
    persisted = client.get("/projects/p/task-runs").json()[0]
    assert persisted["runtime_context"]["execution_delivery"] == snapshot


@pytest.mark.parametrize("mutation", ["missing", "replaced", "expanded"])
def test_explore_delivery_cannot_replace_server_admission_binding(
    conn,
    client,
    mutation,
):
    run = admit_structured_explore(conn, client)
    binding = explore_delivery_snapshot(run)["binding"]
    if mutation == "missing":
        binding = None
    elif mutation == "replaced":
        binding = {**binding, "slot_key": "cell-other"}
    else:
        binding = {**binding, "verification_observation_id": "obs-forged"}
    snapshot = explore_delivery_snapshot(run, binding=binding)

    response = client.patch(
        f"/projects/p/task-runs/{run['id']}",
        json={
            "session_id": SESSION,
            "runtime_context": {"execution_delivery": snapshot},
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "execution_delivery_invalid"
    persisted = client.get("/projects/p/task-runs").json()[0]
    assert "execution_delivery" not in persisted["runtime_context"]


@pytest.mark.parametrize("task_type", ["reason", "explore"])
def test_delivery_registration_normalizes_malformed_server_binding(
    conn,
    client,
    task_type,
):
    if task_type == "reason":
        run = admit_structured_reason(conn, client)
        binding = {
            "allowed_fact_ids": ["origin"],
            "max_intents": 3,
            "structured_intent_binding": run["runtime_context"][
                "structured_intent_binding"
            ],
        }
        snapshot = {
            "session_id": SESSION,
            "phase": "reason_execute",
            "deadline_at": "2098-01-01T00:00:00Z",
            "container_id": "original-container",
            "model_binding_sha256": "a" * 64,
            "binding": binding,
            "inputs": {"open_intent_ids": []},
        }
        context_key = "structured_intent_binding"
    else:
        run = admit_structured_explore(conn, client)
        snapshot = explore_delivery_snapshot(run)
        context_key = "structured_explore_binding"
    context = run["runtime_context"]
    context[context_key]["digest"] = "f" * 64
    if task_type == "reason":
        snapshot["binding"]["structured_intent_binding"] = context[context_key]
    conn.execute(
        "UPDATE task_runs SET runtime_context_json=? WHERE project_id='p' AND id=?",
        (json.dumps(context), run["id"]),
    )
    conn.commit()

    response = client.patch(
        f"/projects/p/task-runs/{run['id']}",
        json={
            "session_id": SESSION,
            "runtime_context": {"execution_delivery": snapshot},
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "execution_delivery_invalid"


def test_original_owner_freezes_delivery_before_execution_and_can_repeat_ack(conn, client):
    _seed(conn)
    admitted = admit_reason(client)
    assert admitted.status_code == 201, admitted.text
    run_id = admitted.json()["id"]
    snapshot = {"session_id": SESSION, "phase": "reason_execute", "deadline_at": "2098-01-01T00:00:00Z",
                "container_id": "original-container", "model_binding_sha256": "a" * 64,
                "binding": {"allowed_fact_ids": ["origin"], "max_intents": 3}, "inputs": {"open_intent_ids": []}}
    fields = {"status": "running", "session_id": SESSION, "runtime_context": {"execution_delivery": snapshot}}
    for _ in range(2):
        response = client.patch(f"/projects/p/task-runs/{run_id}", json=fields)
        assert response.status_code == 200, response.text
    assert client.patch(f"/projects/p/task-runs/{run_id}", json={"status": "failed", "error_type": "request_contract"}).status_code == 200
    assert client.patch(f"/projects/p/task-runs/{run_id}", json={"runtime_context": {"execution_delivery": {**snapshot, "container_id": "replacement"}}}).status_code == 422


@pytest.mark.parametrize("mutation", ["fact_object", "unknown_fact", "boolean_budget", "intent_object", "extra_input"])
def test_http_initial_delivery_registration_rejects_unbounded_or_foreign_binding(conn, client, mutation):
    _seed(conn)
    run = admit_reason(client).json()
    snapshot = {"session_id": SESSION, "phase": "reason_execute", "deadline_at": "2098-01-01T00:00:00Z",
                "container_id": "original-container", "model_binding_sha256": "a" * 64,
                "binding": {"allowed_fact_ids": ["origin"], "max_intents": 3}, "inputs": {"open_intent_ids": []}}
    if mutation == "fact_object":
        snapshot["binding"]["allowed_fact_ids"] = [{"raw_context": "not-an-id"}]
    elif mutation == "unknown_fact":
        snapshot["binding"]["allowed_fact_ids"] = ["foreign-fact"]
    elif mutation == "boolean_budget":
        snapshot["binding"]["max_intents"] = True
    elif mutation == "intent_object":
        snapshot["inputs"]["open_intent_ids"] = {"raw_context": "not-ids"}
    else:
        snapshot["inputs"]["prompt"] = "must-not-persist"
    response = client.patch(f"/projects/p/task-runs/{run['id']}", json={"session_id": SESSION, "runtime_context": {"execution_delivery": snapshot}})
    assert response.status_code == 422, response.text
    assert "execution_delivery" not in client.get("/projects/p/task-runs").json()[0]["runtime_context"]


@pytest.mark.parametrize("mutation", ["source", "session", "snapshot"])
def test_admitted_delivery_source_and_session_cannot_be_mutated(conn, client, mutation):
    snapshot = source_execution(conn)
    admitted = admit_reason(client)
    assert admitted.status_code == 201, admitted.text
    run_id = admitted.json()["id"]
    changes = {
        "source": {"runtime_context": {"delivery_retry": {"source_task_run_id": "other"}}},
        "session": {"session_id": "pi:22222222-2222-4222-8222-222222222222"},
        "snapshot": {"runtime_context": {"execution_delivery": {**snapshot, "deadline_at": "2099-01-01T00:00:00Z"}}},
    }
    response = client.patch(f"/projects/p/task-runs/{run_id}", json=changes[mutation])
    assert response.status_code == 422, response.text
    persisted = client.get("/projects/p/task-runs").json()[-1]
    assert persisted["runtime_context"]["execution_delivery"] == snapshot
    assert persisted["session_id"] == SESSION


@pytest.mark.parametrize("metadata", [None, {}, {"container_id": "unknown"}, {"container_id": "original-container", "container_state": "exited"}])
def test_original_registration_requires_exact_live_container_before_model(conn, client, metadata, monkeypatch):
    from cairn.dispatcher.protocol.client import CairnClient
    from cairn.dispatcher.tasks.delivery import DeliveryUnavailable, register_delivery
    from test_pi_execution import worker

    _seed(conn)
    admitted = admit_reason(client).json()
    api = CairnClient("http://testserver", token="synthetic-test-only")
    monkeypatch.setattr(api, "_session", lambda: client)
    manager = SimpleNamespace(runtime_metadata=lambda name: metadata)
    with pytest.raises(DeliveryUnavailable):
        register_delivery(api, manager, "original-name", SimpleNamespace(project=SimpleNamespace(id="p", explore_deadline=None)), admitted["id"], worker(),
                          phase="reason_execute", session=SESSION, timeout_seconds=20,
                          binding={"allowed_fact_ids": ["origin"], "max_intents": 3}, inputs={"open_intent_ids": []})
    assert "execution_delivery" not in client.get("/projects/p/task-runs").json()[0]["runtime_context"]
    api.close()


def assert_cleanup_submissions(state, status, count, owner, *, container_id="original-container", api_status=200):
    from unittest.mock import Mock
    from cairn.dispatcher.protocol.client import ApiResult
    from cairn.dispatcher.scheduler.cleanup import CleanupMixin

    loop = CleanupMixin()
    loop._candidate_container_blocked = lambda _container_name: False
    loop.dispatcher_id = owner
    loop.client = SimpleNamespace(get_assessment_report_finalization=lambda p: ApiResult(status_code=api_status, data=state),
                                  list_task_runs=lambda p: ApiResult(status_code=200, data=[]))
    loop.container_manager = SimpleNamespace(container_name=lambda p: "original-name",
        runtime_metadata=lambda n: {"container_id": container_id, "container_state": "running"},
        needs_completed_cleanup=lambda p: True, needs_stopped_cleanup=lambda p: True,
        cleanup_completed=lambda p: True, cleanup_stopped=lambda p: True, cleanup_paused=lambda *a, **kw: True,
        managed_container_names=lambda: [])
    loop._inactive_cleanup_done, loop.futures, loop.cleanup_futures, loop._cleanup_pending = {}, {}, {}, set()
    loop.cleanup_executor = SimpleNamespace(submit=Mock(return_value=object()))
    loop._queue_container_cleanups([SimpleNamespace(id="p1", status=status, execution_generation=0)])
    assert loop.cleanup_executor.submit.call_count == count


@pytest.mark.parametrize("api_status,state", [(503, None), (200, None), (200, {})])
def test_unknown_report_eligibility_defers_destructive_cleanup_but_not_pause(api_status, state):
    for status in ("completed", "stopped"):
        assert_cleanup_submissions(state, status, 0, OWNER, api_status=api_status)
    assert_cleanup_submissions(state, "paused", 1, OWNER, api_status=api_status)


@pytest.mark.parametrize("second_error", ["submission_invalid", "provider_error"])
def test_report_claim_retains_generation_and_persisted_backoff(
    report_claim_db,
    monkeypatch,
    tmp_path: Path,
    second_error,
):
    from fastapi import HTTPException
    from cairn.server.models import UpdateTaskRunRequest
    from cairn.server.services import (
        assessment_attempts,
        assessment_reports,
        audit_task_runs,
    )

    # 报告属于探索截止后的正常 finalization，但仍受首次执行绝对 deadline 约束。
    report_claim_db.execute(
        "UPDATE projects SET explore_deadline='2049-01-01T00:00:00Z' "
        "WHERE id='p1'"
    )
    report_claim_db.commit()
    clock = ["2050-01-01T00:00:00Z"]
    monkeypatch.setattr(assessment_reports, "utcnow", lambda: clock[0])
    monkeypatch.setattr(assessment_attempts, "utcnow", lambda: clock[0])
    first = assessment_reports.claim_assessment_report_finalization(
        "p1",
        "worker-a",
        principal=TEST_DISPATCHER,
    )
    snapshot_id, snapshot_digest = _write_report_input_snapshot(
        report_claim_db,
        tmp_path / "artifacts",
        first,
        artifact_id="a1",
    )
    from cairn.dispatcher.tasks.delivery import result_schema_digest

    snapshot = {
        "session_id": SESSION,
        "phase": "report_execute",
        "deadline_at": "2098-01-01T00:00:00Z",
        "container_id": "original-container",
        "model_binding_sha256": "a" * 64,
        "result_schema_sha256": result_schema_digest("report_execute", None),
        "binding": None,
        "inputs": {
            "snapshot_path": (
                f"/tmp/cairn-prompts/report-delivery-{first.task_run_id}.json"
            ),
            "input_snapshot_artifact_id": snapshot_id,
            "input_snapshot_sha256": snapshot_digest,
            "generation_token": first.generation_token,
        },
    }
    with pytest.raises(HTTPException) as unbound:
        audit_task_runs.update_task_run_record(
            "p1",
            first.task_run_id,
            UpdateTaskRunRequest(
                session_id=SESSION,
                runtime_context={"execution_delivery": snapshot},
            ),
            principal=TEST_DISPATCHER,
        )
    assert unbound.value.status_code == 422
    assessment_reports.bind_assessment_report_input_snapshot(
        "p1",
        "worker-a",
        claim_token=first.claim_token,
        artifact_id=snapshot_id,
        digest_sha256=snapshot_digest,
        principal=TEST_DISPATCHER,
    )
    for inputs in (
        {**snapshot["inputs"], "snapshot_path": "/etc/passwd"},
        {**snapshot["inputs"], "generation_token": "other"},
    ):
        with pytest.raises(HTTPException) as invalid:
            audit_task_runs.update_task_run_record(
                "p1",
                first.task_run_id,
                UpdateTaskRunRequest(
                    session_id=SESSION,
                    runtime_context={
                        "execution_delivery": {**snapshot, "inputs": inputs}
                    },
                ),
                principal=TEST_DISPATCHER,
            )
        assert invalid.value.status_code == 422
    audit_task_runs.update_task_run_record(
        "p1",
        first.task_run_id,
        UpdateTaskRunRequest(
            session_id=SESSION,
            runtime_context={"execution_delivery": snapshot},
        ),
        principal=TEST_DISPATCHER,
    )
    lifecycle = {
        "generation": 0,
        "process_started": True,
        "termination_confirmed": True,
        "cleanup_confirmed": False,
    }
    audit_task_runs.update_task_run_record(
        "p1",
        first.task_run_id,
        UpdateTaskRunRequest(
            runtime_context={"execution_lifecycle": lifecycle},
        ),
        principal=TEST_DISPATCHER,
    )
    ended = assessment_reports.finish_assessment_report_finalization(
        "p1",
        _report_delivery_failure_request(first, "submission_missing"),
        principal=TEST_DISPATCHER,
    )
    assert ended.next_attempt_at == "2050-01-01T00:00:30Z"
    state = assessment_reports.get_assessment_report_finalization(
        report_claim_db,
        "p1",
    )
    route = state.delivery_retry_route
    assert route["reason"] == "execution_retry_cooldown"
    assert route["dispatcher_id"] == TEST_DISPATCHER.subject_id
    assert route["container_id"] == "original-container"
    for status, count in (("completed", 0), ("stopped", 0), ("paused", 1)):
        assert_cleanup_submissions(
            state.model_dump(),
            status,
            count,
            TEST_DISPATCHER.subject_id,
        )
    assert_cleanup_submissions(
        state.model_dump(),
        "completed",
        1,
        "another-dispatcher",
    )
    assert_cleanup_submissions(
        state.model_dump(),
        "completed",
        1,
        TEST_DISPATCHER.subject_id,
        container_id="replacement",
    )
    clock[0] = "2099-01-01T00:00:00Z"
    expired = assessment_reports.get_assessment_report_finalization(
        report_claim_db,
        "p1",
    )
    assert expired.delivery_retry_route["reason"] == "execution_delivery_unavailable"
    assert_cleanup_submissions(
        expired.model_dump(),
        "completed",
        1,
        TEST_DISPATCHER.subject_id,
    )
    clock[0] = "2050-01-01T00:00:00Z"
    with pytest.raises(HTTPException) as cooldown:
        assessment_reports.claim_assessment_report_finalization(
            "p1",
            "worker-a",
            principal=TEST_DISPATCHER,
        )
    assert cooldown.value.detail == "execution_retry_cooldown"
    clock[0] = "2050-01-01T00:00:31Z"
    second = assessment_reports.claim_assessment_report_finalization(
        "p1",
        "worker-a",
        principal=TEST_DISPATCHER,
    )
    assert second.generation_token == first.generation_token
    assert second.claim_token != first.claim_token
    assert second.attempt_count == 2
    assert second.input_snapshot_artifact_id == snapshot_id
    assert second.input_snapshot_sha256 == snapshot_digest
    assert second.task_run_context["execution_delivery"] == snapshot
    assert second.task_run_context["delivery_retry"] == {
        "source_task_run_id": first.task_run_id
    }
    audit_task_runs.update_task_run_record(
        "p1",
        second.task_run_id,
        UpdateTaskRunRequest(
            runtime_context={"execution_lifecycle": lifecycle},
        ),
        principal=TEST_DISPATCHER,
    )
    assessment_reports.finish_assessment_report_finalization(
        "p1",
        _report_delivery_failure_request(second, second_error),
        principal=TEST_DISPATCHER,
    )
    clock[0] = "2050-01-01T00:02:00Z"
    third = assessment_reports.claim_assessment_report_finalization(
        "p1",
        "worker-a",
        principal=TEST_DISPATCHER,
    )
    assert third.attempt_count == 3
    assert third.input_snapshot_artifact_id == snapshot_id
    assert third.input_snapshot_sha256 == snapshot_digest
    assert third.task_run_context["execution_delivery"] == snapshot
    assert third.generation_token == first.generation_token
    assert third.task_run_context["delivery_retry"] == {"source_task_run_id": second.task_run_id}
    assert report_claim_db.execute("SELECT task_run_id FROM artifacts WHERE id=? AND project_id='p1'", (snapshot_id,)).fetchone()[0] == first.task_run_id
    assert report_claim_db.execute("SELECT COUNT(*) FROM artifacts WHERE project_id='p1'").fetchone()[0] == 1
    audit_task_runs.update_task_run_record(
        "p1",
        third.task_run_id,
        UpdateTaskRunRequest(
            runtime_context={"execution_lifecycle": lifecycle},
        ),
        principal=TEST_DISPATCHER,
    )
    assessment_reports.finish_assessment_report_finalization(
        "p1",
        _report_delivery_failure_request(third, "submission_invalid"),
        principal=TEST_DISPATCHER,
    )
    exhausted = assessment_reports.get_assessment_report_finalization(
        report_claim_db,
        "p1",
    )
    assert exhausted.delivery_retry_route["reason"] == "execution_retry_exhausted"
    assert_cleanup_submissions(
        exhausted.model_dump(),
        "completed",
        1,
        TEST_DISPATCHER.subject_id,
    )
    with pytest.raises(HTTPException) as blocked:
        assessment_reports.claim_assessment_report_finalization(
            "p1",
            "worker-a",
            principal=TEST_DISPATCHER,
        )
    assert blocked.value.detail == "execution_retry_exhausted"


def test_report_delivery_retry_finishes_with_original_snapshot(
    report_claim_db,
    monkeypatch,
    tmp_path: Path,
):
    from cairn.server.models import UpdateTaskRunRequest
    from cairn.server.services import (
        assessment_attempts,
        assessment_reports,
        audit_task_runs,
    )

    clock = ["2050-01-01T00:00:00Z"]
    monkeypatch.setattr(assessment_reports, "utcnow", lambda: clock[0])
    monkeypatch.setattr(assessment_attempts, "utcnow", lambda: clock[0])
    first = assessment_reports.claim_assessment_report_finalization(
        "p1",
        "worker-a",
        principal=TEST_DISPATCHER,
    )
    snapshot_id, snapshot_digest = _write_report_input_snapshot(
        report_claim_db,
        tmp_path / "artifacts",
        first,
        artifact_id="delivery-snapshot",
    )
    assessment_reports.bind_assessment_report_input_snapshot(
        "p1",
        "worker-a",
        claim_token=first.claim_token,
        artifact_id=snapshot_id,
        digest_sha256=snapshot_digest,
        principal=TEST_DISPATCHER,
    )
    delivery = {
        "session_id": SESSION,
        "phase": "report_execute",
        "deadline_at": "2098-01-01T00:00:00Z",
        "container_id": "original-container",
        "model_binding_sha256": "a" * 64,
        "binding": None,
        "inputs": {
            "snapshot_path": (
                f"/tmp/cairn-prompts/report-delivery-{first.task_run_id}.json"
            ),
            "input_snapshot_artifact_id": snapshot_id,
            "input_snapshot_sha256": snapshot_digest,
            "generation_token": first.generation_token,
        },
    }
    audit_task_runs.update_task_run_record(
        "p1",
        first.task_run_id,
        UpdateTaskRunRequest(
            session_id=SESSION,
            runtime_context={
                "execution_delivery": delivery,
                "execution_lifecycle": {
                    "generation": 0,
                    "process_started": True,
                    "termination_confirmed": True,
                    "cleanup_confirmed": False,
                },
            },
        ),
        principal=TEST_DISPATCHER,
    )
    assessment_reports.finish_assessment_report_finalization(
        "p1",
        _report_delivery_failure_request(first, "submission_missing"),
        principal=TEST_DISPATCHER,
    )

    clock[0] = "2050-01-01T00:00:31Z"
    second = assessment_reports.claim_assessment_report_finalization(
        "p1",
        "worker-a",
        principal=TEST_DISPATCHER,
    )
    assert second.input_snapshot_artifact_id == snapshot_id
    assert second.input_snapshot_sha256 == snapshot_digest
    assert second.task_run_context["delivery_retry"] == {
        "source_task_run_id": first.task_run_id
    }
    assert second.task_run_context["execution_delivery"] == delivery
    frozen_inputs = second.task_run_context["execution_delivery"]["inputs"]
    assert frozen_inputs["input_snapshot_artifact_id"] == (
        second.input_snapshot_artifact_id
    )
    assert frozen_inputs["input_snapshot_sha256"] == second.input_snapshot_sha256
    assert frozen_inputs["generation_token"] == second.generation_token

    report_id, report_digest = _write_report_artifact(
        report_claim_db,
        tmp_path / "artifacts",
        second,
        snapshot_artifact_id=snapshot_id,
        snapshot_sha256=snapshot_digest,
        artifact_id="delivery-report",
    )
    ready = assessment_reports.finish_assessment_report_finalization(
        "p1",
        _ready_finish_request(
            second,
            artifact_id=report_id,
            artifact_sha256=report_digest,
            snapshot_artifact_id=snapshot_id,
            snapshot_sha256=snapshot_digest,
        ),
        principal=TEST_DISPATCHER,
    )

    assert ready.status == "ready"
    assert ready.artifact_id == report_id
    assert ready.input_snapshot_artifact_id == snapshot_id
    assert ready.input_snapshot_sha256 == snapshot_digest
    persisted_finalization = report_claim_db.execute(
        """SELECT input_snapshot_artifact_id, input_snapshot_sha256
           FROM assessment_report_finalizations WHERE project_id = 'p1'"""
    ).fetchone()
    assert persisted_finalization["input_snapshot_artifact_id"] == snapshot_id
    assert persisted_finalization["input_snapshot_sha256"] == snapshot_digest
    assert report_claim_db.execute(
        "SELECT status FROM task_runs WHERE id = ?",
        (first.task_run_id,),
    ).fetchone()["status"] == "failed"
    assert report_claim_db.execute(
        "SELECT status FROM task_runs WHERE id = ?",
        (second.task_run_id,),
    ).fetchone()["status"] == "succeeded"
    assert report_claim_db.execute(
        "SELECT COUNT(*) FROM assessment_report_finish_receipts"
    ).fetchone()[0] == 2


def test_report_finish_response_loss_replays_receipt_without_rerunning_worker(
    report_claim_db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from cairn.dispatcher.protocol.client import ApiResult
    from cairn.dispatcher.tasks import report
    from cairn.server.schemas.projects import AssessmentReportFinishReplayRequest
    from cairn.server.services import assessment_reports

    worker_calls: list[str] = []
    artifact_creations: list[str] = []
    terminal_notifications: list[dict[str, object]] = []
    finish_requests: list[dict[str, object]] = []
    finish_responses: list[dict[str, object]] = []

    class ReceiptReplayClient:
        claim = None

        def get_project(self, project_id: str):
            assert project_id == "p1"
            return SimpleNamespace(project=SimpleNamespace(status="completed"))

        def claim_assessment_report_finalization(
            self,
            project_id: str,
            worker: str,
            *,
            lease_seconds: int,
        ) -> ApiResult:
            assert lease_seconds == 900
            self.claim = assessment_reports.claim_assessment_report_finalization(
                project_id,
                worker,
                lease_seconds=lease_seconds,
                principal=TEST_DISPATCHER,
            )
            return ApiResult(
                status_code=200,
                data=self.claim.model_dump(mode="json"),
            )

        def finish_assessment_report_finalization(self, *args, **kwargs) -> ApiResult:
            pytest.fail("durable finish must use the token-free replay endpoint")

        def replay_assessment_report_finalization(
            self,
            project_id: str,
            worker: str,
            **kwargs,
        ) -> ApiResult:
            request = json.loads(json.dumps(kwargs, ensure_ascii=False))
            finish_requests.append(request)
            receipt = assessment_reports.replay_assessment_report_finalization(
                project_id,
                AssessmentReportFinishReplayRequest(
                    protocol="cairn-assessment-report-finish-v1",
                    worker=worker,
                    **kwargs,
                ),
                principal=TEST_DISPATCHER,
            )
            payload = receipt.model_dump(mode="json")
            finish_responses.append(payload)
            if len(finish_requests) == 1:
                # Server 已提交，但 transport ACK 丢失；Dispatcher 只能重放同一 write。
                return ApiResult(status_code=0, text="response lost")
            return ApiResult(status_code=200, data=payload)

    client = ReceiptReplayClient()

    def run_once(*args, **kwargs):
        worker_calls.append("run")
        claim = client.claim
        assert claim is not None
        snapshot_id, snapshot_digest = _write_report_input_snapshot(
            report_claim_db,
            tmp_path / "artifacts",
            claim,
            artifact_id="ack-loss-snapshot",
        )
        artifact_creations.append(snapshot_id)
        assessment_reports.bind_assessment_report_input_snapshot(
            "p1",
            "worker-a",
            claim_token=claim.claim_token,
            artifact_id=snapshot_id,
            digest_sha256=snapshot_digest,
            principal=TEST_DISPATCHER,
        )
        report_id, report_digest = _write_report_artifact(
            report_claim_db,
            tmp_path / "artifacts",
            claim,
            snapshot_artifact_id=snapshot_id,
            snapshot_sha256=snapshot_digest,
            artifact_id="ack-loss-report",
        )
        artifact_creations.append(report_id)
        finish_state = kwargs["finish_state"]
        finish_state.input_snapshot = report.ReportArtifactIdentity(
            snapshot_id,
            snapshot_digest,
        )
        finish_state.artifact = report.ReportArtifactIdentity(
            report_id,
            report_digest,
        )
        finish_state.task_run_result = report._report_task_run_result("success")
        return "success"

    monkeypatch.setattr(report, "run_report_task", run_once)
    monkeypatch.setattr(
        "cairn.dispatcher.tasks.conclude_writer.time.sleep",
        lambda _: None,
    )
    monkeypatch.setattr(
        report,
        "notify_browser_task",
        lambda *args, **kwargs: terminal_notifications.append(kwargs),
    )

    outcome = report.run_post_completion_report(
        SimpleNamespace(
            dispatcher_id=TEST_DISPATCHER.subject_id,
            tasks=SimpleNamespace(report=SimpleNamespace(timeout=600)),
        ),
        client,
        SimpleNamespace(),
        "p1",
        "stale graph is not used",
        SimpleNamespace(name="worker-a"),
        prompt_group="default-copy-v2",
        durable_request=True,
        finish_outbox=ephemeral_report_finish_outbox(),
    )

    assert outcome == "success"
    assert worker_calls == ["run"]
    assert artifact_creations == ["ack-loss-snapshot", "ack-loss-report"]
    assert len(finish_requests) == 2
    assert finish_requests[0] == finish_requests[1]
    assert finish_responses[0] == finish_responses[1]
    assert terminal_notifications == [{"terminal_confirmed": True}]
    assert report_claim_db.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_type = 'report'"
    ).fetchone()[0] == 1
    receipt = report_claim_db.execute(
        "SELECT request_sha256 FROM assessment_report_finish_receipts"
    ).fetchone()
    assert receipt is not None
    assert "claim_token" not in finish_requests[0]
    assert finish_requests[0]["claim_sha256"] == sha256(
        client.claim.claim_token.encode("utf-8")
    ).hexdigest()
    assert receipt["request_sha256"] == finish_requests[0]["request_sha256"]
    assert report_claim_db.execute(
        "SELECT COUNT(*) FROM assessment_report_finish_receipts"
    ).fetchone()[0] == 1
    finalization = report_claim_db.execute(
        "SELECT status, artifact_id FROM assessment_report_finalizations "
        "WHERE project_id = 'p1'"
    ).fetchone()
    assert tuple(finalization) == ("ready", "ack-loss-report")


@pytest.mark.parametrize("server_commits_finish", [True, False])
def test_exhausted_finish_ack_loss_short_circuits_next_scheduler_cycle(
    report_claim_db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    server_commits_finish: bool,
) -> None:
    from fastapi import HTTPException

    from cairn.dispatcher.protocol.client import ApiResult
    from cairn.dispatcher.scheduler.report_finish_outbox import ReportFinishOutboxMixin
    from cairn.dispatcher.tasks import report
    from cairn.dispatcher.tasks.report_finish_outbox import ReportFinishOutbox
    from cairn.server.schemas.projects import AssessmentReportFinishReplayRequest
    from cairn.server.services import assessment_reports

    clock = {"now": "2050-01-01T00:00:00Z"}
    monkeypatch.setattr(assessment_reports, "utcnow", lambda: clock["now"])
    worker_calls: list[str] = []
    finish_requests: list[dict[str, object]] = []
    terminal_notifications: list[dict[str, object]] = []

    class ExhaustedAckClient:
        claim = None
        deliver_ack = False

        def get_project(self, project_id: str):
            assert project_id == "p1"
            return SimpleNamespace(project=SimpleNamespace(status="completed"))

        def claim_assessment_report_finalization(
            self,
            project_id: str,
            worker: str,
            *,
            lease_seconds: int,
        ) -> ApiResult:
            assert lease_seconds == 900
            try:
                self.claim = assessment_reports.claim_assessment_report_finalization(
                    project_id,
                    worker,
                    lease_seconds=lease_seconds,
                    principal=TEST_DISPATCHER,
                )
            except HTTPException as exc:
                return ApiResult(
                    status_code=exc.status_code,
                    data={"detail": exc.detail},
                )
            return ApiResult(
                status_code=200,
                data=self.claim.model_dump(mode="json"),
            )

        def finish_assessment_report_finalization(self, *args, **kwargs) -> ApiResult:
            pytest.fail("durable finish must use the token-free replay endpoint")

        def replay_assessment_report_finalization(
            self,
            project_id: str,
            worker: str,
            **kwargs,
        ) -> ApiResult:
            finish_requests.append(json.loads(json.dumps(kwargs, ensure_ascii=False)))
            receipt = None
            if server_commits_finish or self.deliver_ack:
                receipt = assessment_reports.replay_assessment_report_finalization(
                    project_id,
                    AssessmentReportFinishReplayRequest(
                        protocol="cairn-assessment-report-finish-v1",
                        worker=worker,
                        **kwargs,
                    ),
                    principal=TEST_DISPATCHER,
                )
            if not self.deliver_ack:
                # 三次 transport ACK 都丢失；Dispatcher 不得重新运行 Worker。
                return ApiResult(status_code=0, text="response lost")
            assert receipt is not None
            return ApiResult(status_code=200, data=receipt.model_dump(mode="json"))

    client = ExhaustedAckClient()
    suffix = "committed" if server_commits_finish else "durable-artifact"

    def run_once(*args, **kwargs):
        worker_calls.append("run")
        claim = client.claim
        assert claim is not None
        snapshot_id, snapshot_digest = _write_report_input_snapshot(
            report_claim_db,
            tmp_path / "artifacts",
            claim,
            artifact_id=f"{suffix}-snapshot",
        )
        assessment_reports.bind_assessment_report_input_snapshot(
            "p1",
            "worker-a",
            claim_token=claim.claim_token,
            artifact_id=snapshot_id,
            digest_sha256=snapshot_digest,
            principal=TEST_DISPATCHER,
        )
        report_id, report_digest = _write_report_artifact(
            report_claim_db,
            tmp_path / "artifacts",
            claim,
            snapshot_artifact_id=snapshot_id,
            snapshot_sha256=snapshot_digest,
            artifact_id=f"{suffix}-report",
        )
        finish_state = kwargs["finish_state"]
        finish_state.input_snapshot = report.ReportArtifactIdentity(
            snapshot_id,
            snapshot_digest,
        )
        finish_state.artifact = report.ReportArtifactIdentity(
            report_id,
            report_digest,
        )
        finish_state.task_run_result = report._report_task_run_result("success")
        return "success"

    monkeypatch.setattr(report, "run_report_task", run_once)
    monkeypatch.setattr(
        "cairn.dispatcher.tasks.conclude_writer.time.sleep",
        lambda _: None,
    )
    monkeypatch.setattr(
        "cairn.dispatcher.scheduler.report_finish_outbox.notify_browser_task",
        lambda *args, **kwargs: terminal_notifications.append(kwargs),
    )
    config = SimpleNamespace(
        dispatcher_id=TEST_DISPATCHER.subject_id,
        tasks=SimpleNamespace(report=SimpleNamespace(timeout=600)),
    )

    outbox = ReportFinishOutbox(
        tmp_path / "dispatcher-data" / "report-finish-outbox"
    )
    first_outcome = report.run_post_completion_report(
        config,
        client,
        SimpleNamespace(),
        "p1",
        "stale graph is not used",
        SimpleNamespace(name="worker-a"),
        prompt_group="default-copy-v2",
        durable_request=True,
        finish_outbox=outbox,
    )

    assert first_outcome == "server_write_failed"
    assert len(finish_requests) == 3
    assert finish_requests[0] == finish_requests[1] == finish_requests[2]
    assert worker_calls == ["run"]
    assert terminal_notifications == []
    pending = outbox.entries()
    assert len(pending) == 1
    assert pending[0].status == "ready"

    clock["now"] = "2050-01-01T00:15:01Z"
    second_outcome = report.run_post_completion_report(
        config,
        client,
        SimpleNamespace(),
        "p1",
        "stale graph is not used",
        SimpleNamespace(name="worker-a"),
        prompt_group="default-copy-v2",
        durable_request=True,
        finish_outbox=outbox,
    )

    assert second_outcome == "server_write_failed"
    assert worker_calls == ["run"]
    assert len(finish_requests) == 3
    assert terminal_notifications == []

    client.deliver_ack = True
    scheduler = SimpleNamespace(
        report_finish_outbox=outbox,
        client=client,
        container_manager=SimpleNamespace(),
        dispatcher_id=TEST_DISPATCHER.subject_id,
    )
    ReportFinishOutboxMixin._flush_pending_report_finishes(scheduler)

    assert outbox.entries() == []
    assert len(finish_requests) == 4
    assert finish_requests[3] == finish_requests[0]
    assert terminal_notifications == [{"terminal_confirmed": True}]
    assert report_claim_db.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_type = 'report'"
    ).fetchone()[0] == 1
    receipt = report_claim_db.execute(
        "SELECT response_json FROM assessment_report_finish_receipts"
    ).fetchone()
    assert receipt is not None
    response = json.loads(receipt["response_json"])
    assert response["finish_disposition"] == "accepted"
    assert response["artifact_id"] == f"{suffix}-report"


def test_exhausted_failed_finish_blocks_readmission_after_expiry(
    report_claim_db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from fastapi import HTTPException

    from cairn.dispatcher.protocol.client import ApiResult
    from cairn.dispatcher.tasks import report
    from cairn.dispatcher.tasks.report_finish_outbox import ReportFinishOutbox
    from cairn.dispatcher.worker_result_gateway import WorkerResultFailure
    from cairn.server.services import assessment_reports

    clock = {"now": "2050-01-01T00:00:00Z"}
    monkeypatch.setattr(assessment_reports, "utcnow", lambda: clock["now"])
    worker_task_runs: list[str] = []
    finish_requests: list[dict[str, object]] = []
    failure = WorkerResultFailure(
        failure_class="timeout",
        code="deadline_exceeded",
        message="worker deadline exceeded",
        fingerprint="timeout",
        phase="report_execute",
        exit_code=124,
    )

    class FailedFinishAckClient:
        claim = None

        def get_project(self, project_id: str):
            assert project_id == "p1"
            return SimpleNamespace(project=SimpleNamespace(status="completed"))

        def claim_assessment_report_finalization(
            self,
            project_id: str,
            worker: str,
            *,
            lease_seconds: int,
        ) -> ApiResult:
            assert lease_seconds == 900
            try:
                self.claim = assessment_reports.claim_assessment_report_finalization(
                    project_id,
                    worker,
                    lease_seconds=lease_seconds,
                    principal=TEST_DISPATCHER,
                )
            except HTTPException as exc:
                return ApiResult(
                    status_code=exc.status_code,
                    data={"detail": exc.detail},
                )
            return ApiResult(
                status_code=200,
                data=self.claim.model_dump(mode="json"),
            )

        def finish_assessment_report_finalization(self, *args, **kwargs) -> ApiResult:
            pytest.fail("durable finish must use the token-free replay endpoint")

        def replay_assessment_report_finalization(
            self,
            project_id: str,
            worker: str,
            **kwargs,
        ) -> ApiResult:
            finish_requests.append(json.loads(json.dumps(kwargs, ensure_ascii=False)))
            # 请求从未到达 Server；本地 outbox 仍保留精确失败事实。
            return ApiResult(status_code=0, text="request not delivered")

    client = FailedFinishAckClient()

    def fail_once(*args, **kwargs):
        claim = client.claim
        assert claim is not None
        worker_task_runs.append(claim.task_run_id)
        snapshot_id, snapshot_digest = _write_report_input_snapshot(
            report_claim_db,
            tmp_path / "artifacts",
            claim,
            artifact_id=f"failed-snapshot-{len(worker_task_runs)}",
        )
        assessment_reports.bind_assessment_report_input_snapshot(
            "p1",
            "worker-a",
            claim_token=claim.claim_token,
            artifact_id=snapshot_id,
            digest_sha256=snapshot_digest,
            principal=TEST_DISPATCHER,
        )
        finish_state = kwargs["finish_state"]
        finish_state.input_snapshot = report.ReportArtifactIdentity(
            snapshot_id,
            snapshot_digest,
        )
        finish_state.task_run_result = report._report_task_run_result(failure)
        return failure

    monkeypatch.setattr(report, "run_report_task", fail_once)
    monkeypatch.setattr(
        "cairn.dispatcher.tasks.conclude_writer.time.sleep",
        lambda _: None,
    )
    config = SimpleNamespace(
        dispatcher_id=TEST_DISPATCHER.subject_id,
        tasks=SimpleNamespace(report=SimpleNamespace(timeout=600)),
    )

    outbox = ReportFinishOutbox(
        tmp_path / "dispatcher-data" / "report-finish-outbox"
    )
    first_outcome = report.run_post_completion_report(
        config,
        client,
        SimpleNamespace(),
        "p1",
        "stale graph is not used",
        SimpleNamespace(name="worker-a"),
        prompt_group="default-copy-v2",
        durable_request=True,
        finish_outbox=outbox,
    )
    assert first_outcome == "server_write_failed"
    assert len(finish_requests) == 3
    assert finish_requests[0] == finish_requests[1] == finish_requests[2]
    pending = outbox.entries()
    assert len(pending) == 1
    assert pending[0].status == "failed"
    assert pending[0].task_run_result.error_type == "worker_timeout"

    clock["now"] = "2050-01-01T00:15:01Z"
    second_outcome = report.run_post_completion_report(
        config,
        client,
        SimpleNamespace(),
        "p1",
        "stale graph is not used",
        SimpleNamespace(name="worker-a"),
        prompt_group="default-copy-v2",
        durable_request=True,
        finish_outbox=outbox,
    )

    assert second_outcome == "server_write_failed"
    assert len(worker_task_runs) == 1
    assert len(finish_requests) == 3
    assert outbox.entries() == pending
    assert report_claim_db.execute(
        "SELECT COUNT(*) FROM assessment_report_finish_receipts"
    ).fetchone()[0] == 0
    runs = report_claim_db.execute(
        "SELECT id, status, error_type FROM task_runs WHERE task_type = 'report' "
        "ORDER BY id"
    ).fetchall()
    assert [(row["status"], row["error_type"]) for row in runs] == [
        ("running", None),
    ]


def test_failed_finish_outbox_replays_after_dispatcher_restart_without_model_rerun(
    report_claim_db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from fastapi import HTTPException

    from cairn.dispatcher.protocol.client import ApiResult
    from cairn.dispatcher.scheduler.report_finish_outbox import ReportFinishOutboxMixin
    from cairn.dispatcher.tasks import report
    from cairn.dispatcher.tasks.report_finish_outbox import ReportFinishOutbox
    from cairn.dispatcher.worker_result_gateway import WorkerResultFailure
    from cairn.server.schemas.projects import AssessmentReportFinishReplayRequest
    from cairn.server.services import assessment_reports

    clock = {"now": "2050-01-01T00:00:00Z"}
    monkeypatch.setattr(assessment_reports, "utcnow", lambda: clock["now"])
    worker_task_runs: list[str] = []
    finish_requests: list[dict[str, object]] = []
    replay_requests: list[dict[str, object]] = []
    terminal_notifications: list[dict[str, object]] = []
    failure = WorkerResultFailure(
        failure_class="timeout",
        code="deadline_exceeded",
        message="worker deadline exceeded",
        fingerprint="timeout",
        phase="report_execute",
        exit_code=124,
    )

    class RestartClient:
        claim = None
        replay_enabled = False

        def get_project(self, project_id: str):
            assert project_id == "p1"
            return SimpleNamespace(project=SimpleNamespace(status="completed"))

        def claim_assessment_report_finalization(
            self,
            project_id: str,
            worker: str,
            *,
            lease_seconds: int,
        ) -> ApiResult:
            try:
                self.claim = assessment_reports.claim_assessment_report_finalization(
                    project_id,
                    worker,
                    lease_seconds=lease_seconds,
                    principal=TEST_DISPATCHER,
                )
            except HTTPException as exc:
                return ApiResult(
                    status_code=exc.status_code,
                    data={"detail": exc.detail},
                )
            return ApiResult(
                status_code=200,
                data=self.claim.model_dump(mode="json"),
            )

        def finish_assessment_report_finalization(
            self,
            project_id: str,
            worker: str,
            **kwargs,
        ) -> ApiResult:
            pytest.fail("durable finish must use the token-free replay endpoint")

        def replay_assessment_report_finalization(
            self,
            project_id: str,
            worker: str,
            **kwargs,
        ) -> ApiResult:
            request = json.loads(json.dumps(kwargs, ensure_ascii=False))
            if not self.replay_enabled:
                finish_requests.append(request)
                # 三次首发请求均未到达 Server，只有本地 outbox 保存原始失败事实。
                return ApiResult(status_code=0, text="request not delivered")
            replay_requests.append(request)
            try:
                receipt = assessment_reports.replay_assessment_report_finalization(
                    project_id,
                    AssessmentReportFinishReplayRequest(
                        protocol="cairn-assessment-report-finish-v1",
                        worker=worker,
                        **kwargs,
                    ),
                    principal=TEST_DISPATCHER,
                )
            except HTTPException as exc:
                return ApiResult(
                    status_code=exc.status_code,
                    data={"detail": exc.detail},
                )
            return ApiResult(status_code=200, data=receipt.model_dump(mode="json"))

    client = RestartClient()

    def fail_once(*args, **kwargs):
        claim = client.claim
        assert claim is not None
        worker_task_runs.append(claim.task_run_id)
        snapshot_id, snapshot_digest = _write_report_input_snapshot(
            report_claim_db,
            tmp_path / "artifacts",
            claim,
            artifact_id="restart-failed-snapshot",
        )
        assessment_reports.bind_assessment_report_input_snapshot(
            "p1",
            "worker-a",
            claim_token=claim.claim_token,
            artifact_id=snapshot_id,
            digest_sha256=snapshot_digest,
            principal=TEST_DISPATCHER,
        )
        finish_state = kwargs["finish_state"]
        finish_state.input_snapshot = report.ReportArtifactIdentity(
            snapshot_id,
            snapshot_digest,
        )
        finish_state.task_run_result = report._report_task_run_result(failure)
        return failure

    monkeypatch.setattr(report, "run_report_task", fail_once)
    monkeypatch.setattr(
        "cairn.dispatcher.tasks.conclude_writer.time.sleep",
        lambda _: None,
    )
    monkeypatch.setattr(
        "cairn.dispatcher.scheduler.report_finish_outbox.notify_browser_task",
        lambda *args, **kwargs: terminal_notifications.append(kwargs),
    )
    config = SimpleNamespace(
        dispatcher_id=TEST_DISPATCHER.subject_id,
        tasks=SimpleNamespace(report=SimpleNamespace(timeout=600)),
    )
    outbox_root = tmp_path / "dispatcher-data" / "report-finish-outbox"
    first_process_outbox = ReportFinishOutbox(outbox_root)

    first_outcome = report.run_post_completion_report(
        config,
        client,
        SimpleNamespace(),
        "p1",
        "stale graph is not used",
        SimpleNamespace(name="worker-a"),
        prompt_group="default-copy-v2",
        durable_request=True,
        finish_outbox=first_process_outbox,
    )

    assert first_outcome == "server_write_failed"
    assert worker_task_runs == [client.claim.task_run_id]
    assert len(finish_requests) == 3
    assert finish_requests[0] == finish_requests[1] == finish_requests[2]
    pending = first_process_outbox.entries()
    assert len(pending) == 1
    assert pending[0].status == "failed"
    assert pending[0].task_run_result.error_type == "worker_timeout"
    stored = next(outbox_root.glob("*.json")).read_text(encoding="utf-8")
    assert client.claim.claim_token not in stored
    assert report_claim_db.execute(
        "SELECT COUNT(*) FROM assessment_report_finish_receipts"
    ).fetchone()[0] == 0

    # 模拟进程退出、lease 过期和新 Dispatcher 从同一 owner-only 目录启动。
    clock["now"] = "2050-01-01T00:15:01Z"
    client.replay_enabled = True
    restarted_outbox = ReportFinishOutbox(outbox_root)
    restarted = SimpleNamespace(
        report_finish_outbox=restarted_outbox,
        client=client,
        container_manager=SimpleNamespace(),
        dispatcher_id=TEST_DISPATCHER.subject_id,
    )
    ReportFinishOutboxMixin._flush_pending_report_finishes(restarted)

    assert restarted_outbox.entries() == []
    assert len(replay_requests) == 1
    assert "claim_token" not in replay_requests[0]
    assert terminal_notifications == [{"terminal_confirmed": True}]
    assert report_claim_db.execute(
        "SELECT COUNT(*) FROM assessment_report_finish_receipts"
    ).fetchone()[0] == 1
    run = report_claim_db.execute(
        "SELECT status, error_type, error_message FROM task_runs WHERE id = ?",
        (worker_task_runs[0],),
    ).fetchone()
    assert tuple(run) == (
        "timed_out",
        "worker_timeout",
        "worker deadline exceeded",
    )

    second_outcome = report.run_post_completion_report(
        config,
        client,
        SimpleNamespace(),
        "p1",
        "stale graph is not used",
        SimpleNamespace(name="worker-a"),
        prompt_group="default-copy-v2",
        durable_request=True,
        finish_outbox=restarted_outbox,
    )
    assert second_outcome == "deferred"
    assert len(worker_task_runs) == 1
    assert report_claim_db.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_type = 'report'"
    ).fetchone()[0] == 1


def test_corrupt_snapshot_ancestry_cannot_recover_after_failed_finish_ack_loss(
    report_claim_db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from fastapi import HTTPException

    from cairn.dispatcher.protocol.client import ApiResult
    from cairn.dispatcher.tasks import report
    from cairn.dispatcher.tasks.report_finish_outbox import ReportFinishOutbox
    from cairn.server.schemas.projects import AssessmentReportFinishReplayRequest
    from cairn.server.services import assessment_reports
    from test_assessment_reports import _insert_report_chain_run

    clock = {"now": "2050-01-01T00:00:00Z"}
    monkeypatch.setattr(assessment_reports, "utcnow", lambda: clock["now"])
    finish_requests: list[dict[str, object]] = []
    terminal_notifications: list[dict[str, object]] = []

    class CorruptAncestryClient:
        claim = None
        snapshot_id = "corrupt-ancestry-snapshot"
        snapshot_digest = ""

        def get_project(self, project_id: str):
            assert project_id == "p1"
            return SimpleNamespace(project=SimpleNamespace(status="completed"))

        def claim_assessment_report_finalization(
            self,
            project_id: str,
            worker: str,
            *,
            lease_seconds: int,
        ) -> ApiResult:
            assert lease_seconds == 900
            self.claim = assessment_reports.claim_assessment_report_finalization(
                project_id,
                worker,
                lease_seconds=lease_seconds,
                principal=TEST_DISPATCHER,
            )
            origin_id = "corrupt-ancestry-origin"
            _insert_report_chain_run(
                report_claim_db,
                self.claim,
                origin_id,
                runtime_context={
                    "report_claim": {
                        "generation_token": self.claim.generation_token,
                    }
                },
            )
            row = report_claim_db.execute(
                "SELECT runtime_context_json FROM task_runs WHERE id = ?",
                (self.claim.task_run_id,),
            ).fetchone()
            context = json.loads(row["runtime_context_json"])
            context["delivery_retry"] = {"source_task_run_id": origin_id}
            report_claim_db.execute(
                "UPDATE task_runs SET runtime_context_json = ? WHERE id = ?",
                (json.dumps(context), self.claim.task_run_id),
            )
            report_claim_db.commit()
            origin_claim = self.claim.model_copy(update={"task_run_id": origin_id})
            self.snapshot_id, self.snapshot_digest = _write_report_input_snapshot(
                report_claim_db,
                tmp_path / "artifacts",
                origin_claim,
                artifact_id=self.snapshot_id,
            )
            assessment_reports.bind_assessment_report_input_snapshot(
                project_id,
                worker,
                claim_token=self.claim.claim_token,
                artifact_id=self.snapshot_id,
                digest_sha256=self.snapshot_digest,
                principal=TEST_DISPATCHER,
            )
            return ApiResult(
                status_code=200,
                data=self.claim.model_copy(
                    update={
                        "input_snapshot_artifact_id": self.snapshot_id,
                        "input_snapshot_sha256": self.snapshot_digest,
                    }
                ).model_dump(mode="json"),
            )

        def finish_assessment_report_finalization(self, *args, **kwargs) -> ApiResult:
            pytest.fail("durable finish must use the token-free replay endpoint")

        def replay_assessment_report_finalization(
            self,
            project_id: str,
            worker: str,
            **kwargs,
        ) -> ApiResult:
            request = json.loads(json.dumps(kwargs, ensure_ascii=False))
            finish_requests.append(request)
            if request["status"] == "failed":
                # failed fallback 未到达 Server；durable journal 保留精确事实。
                return ApiResult(status_code=0, text="response lost")
            try:
                assessment_reports.replay_assessment_report_finalization(
                    project_id,
                    AssessmentReportFinishReplayRequest(
                        protocol="cairn-assessment-report-finish-v1",
                        worker=worker,
                        **kwargs,
                    ),
                    principal=TEST_DISPATCHER,
                )
            except HTTPException as exc:
                return ApiResult(
                    status_code=exc.status_code,
                    data={"detail": exc.detail},
                )
            pytest.fail("corrupt snapshot ancestry was accepted")

    client = CorruptAncestryClient()

    def run_once(*args, **kwargs):
        claim = client.claim
        assert claim is not None
        report_id, report_digest = _write_report_artifact(
            report_claim_db,
            tmp_path / "artifacts",
            claim,
            snapshot_artifact_id=client.snapshot_id,
            snapshot_sha256=client.snapshot_digest,
            artifact_id="corrupt-ancestry-report",
        )
        row = report_claim_db.execute(
            "SELECT runtime_context_json FROM task_runs WHERE id = ?",
            (claim.task_run_id,),
        ).fetchone()
        context = json.loads(row["runtime_context_json"])
        context["delivery_retry"]["unexpected"] = True
        report_claim_db.execute(
            "UPDATE task_runs SET runtime_context_json = ? WHERE id = ?",
            (json.dumps(context), claim.task_run_id),
        )
        report_claim_db.commit()
        finish_state = kwargs["finish_state"]
        finish_state.input_snapshot = report.ReportArtifactIdentity(
            client.snapshot_id,
            client.snapshot_digest,
        )
        finish_state.artifact = report.ReportArtifactIdentity(
            report_id,
            report_digest,
        )
        finish_state.task_run_result = report._report_task_run_result("success")
        return "success"

    monkeypatch.setattr(report, "run_report_task", run_once)
    monkeypatch.setattr(
        "cairn.dispatcher.tasks.conclude_writer.time.sleep",
        lambda _: None,
    )
    monkeypatch.setattr(
        report,
        "notify_browser_task",
        lambda *args, **kwargs: terminal_notifications.append(kwargs),
    )

    config = SimpleNamespace(
        dispatcher_id=TEST_DISPATCHER.subject_id,
        tasks=SimpleNamespace(report=SimpleNamespace(timeout=600)),
    )
    outbox = ReportFinishOutbox(
        tmp_path / "corrupt-ancestry" / "report-finish-outbox"
    )
    outcome = report.run_post_completion_report(
        config,
        client,
        SimpleNamespace(),
        "p1",
        "stale graph is not used",
        SimpleNamespace(name="worker-a"),
        prompt_group="default-copy-v2",
        durable_request=True,
        finish_outbox=outbox,
    )

    assert outcome == "server_write_failed"
    assert [request["status"] for request in finish_requests] == [
        "ready",
        "failed",
        "failed",
        "failed",
    ]
    assert finish_requests[1] == finish_requests[2] == finish_requests[3]
    assert finish_requests[1]["input_snapshot_artifact_id"] is None
    assert finish_requests[1]["input_snapshot_sha256"] is None
    pending = outbox.entries()
    assert len(pending) == 1
    assert pending[0].status == "failed"
    assert pending[0].input_snapshot_artifact_id is None
    assert terminal_notifications == []
    task_run = report_claim_db.execute(
        "SELECT status FROM task_runs WHERE id = ?",
        (client.claim.task_run_id,),
    ).fetchone()
    assert task_run["status"] == "running"
    assert report_claim_db.execute(
        "SELECT COUNT(*) FROM assessment_report_finish_receipts WHERE task_run_id = ?",
        (client.claim.task_run_id,),
    ).fetchone()[0] == 0

    second_outcome = report.run_post_completion_report(
        config,
        client,
        SimpleNamespace(),
        "p1",
        "stale graph is not used",
        SimpleNamespace(name="worker-a"),
        prompt_group="default-copy-v2",
        durable_request=True,
        finish_outbox=outbox,
    )
    assert second_outcome == "server_write_failed"
    assert len(finish_requests) == 4
    assert outbox.entries() == pending

    clock["now"] = "2050-01-01T00:15:01Z"
    with pytest.raises(HTTPException) as blocked:
        assessment_reports.claim_assessment_report_finalization(
            "p1",
            "worker-a",
            principal=TEST_DISPATCHER,
        )
    assert blocked.value.detail == "execution_retry_blocked"
    expired_run = report_claim_db.execute(
        "SELECT status, error_type, error_message FROM task_runs WHERE id = ?",
        (client.claim.task_run_id,),
    ).fetchone()
    assert tuple(expired_run) == (
        "timed_out",
        "worker_timeout",
        "report_claim_expired",
    )
    assert report_claim_db.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_type = 'report' AND status = 'running'"
    ).fetchone()[0] == 0


@pytest.mark.parametrize("preparation_delay", [0, 30])
def test_report_delivery_uses_original_snapshot_and_execute_schema(
    monkeypatch,
    preparation_delay,
):
    from cairn.dispatcher.protocol.client import ApiResult
    from cairn.dispatcher.tasks import delivery, report
    from test_pi_execution import worker
    from test_reason_depth_review import execution_view, project_with_execution
    from test_report_frozen_rendering import _packet

    clock = [datetime.now(timezone.utc)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(delivery, "datetime", Clock)
    bound_worker = worker()
    original = json.dumps(
        {
            "version": 1,
            "report_packet": _packet(empty=True),
            "graph_yaml": "original graph",
        }
    )
    digest = sha256(original.encode()).hexdigest()
    snapshot = {
        "session_id": SESSION,
        "phase": "report_execute",
        "deadline_at": (
            datetime.now(timezone.utc) + timedelta(seconds=21)
        ).isoformat(),
        "container_id": "original-container",
        "model_binding_sha256": sha256(
            json.dumps(
                bound_worker.model_binding.model_dump(mode="json"),
                sort_keys=True,
            ).encode()
        ).hexdigest(),
        "result_schema_sha256": delivery.result_schema_digest("report_execute", None),
        "binding": None,
        "inputs": {
            "snapshot_path": "/tmp/cairn-prompts/report-delivery-t001.json",
            "input_snapshot_artifact_id": "a1",
            "input_snapshot_sha256": digest,
            "generation_token": "frozen-generation",
        },
    }

    def forbidden(*args, **kwargs):
        pytest.fail("report delivery refreshed frozen input or created a container")

    captured = []
    captured_files = []
    states = []

    class ExecutionReached(BaseException):
        pass

    def capture(manager, name, worker, command, **kwargs):
        captured.append(json.loads(command.input_files[0].content))
        captured_files.append(command.input_files)
        raise ExecutionReached

    def read_snapshot(name, path):
        clock[0] += timedelta(seconds=preparation_delay)
        return original.encode()

    api = SimpleNamespace(
        get_assessment_report_packet=forbidden,
        create_artifact=forbidden,
        bind_assessment_report_input_snapshot=forbidden,
        update_task_run=lambda *args, **kwargs: ApiResult(status_code=200),
    )
    manager = SimpleNamespace(
        ensure_running=forbidden,
        container_name=lambda project_id: "original-name",
        runtime_metadata=lambda name: {
            "container_id": "original-container",
            "container_state": "running",
        },
        read_file=read_snapshot,
    )
    monkeypatch.setattr(report, "run_worker_process", capture)
    monkeypatch.setattr(report, "record_prompt_rendered", forbidden)
    project = project_with_execution(execution_view())

    def run():
        state = report.ReportFinishState()
        states.append(state)
        outcome = report.run_report_task(
            SimpleNamespace(
                dispatcher_id="synthetic-dispatcher",
                tasks=SimpleNamespace(report=SimpleNamespace(timeout=500)),
            ),
            api,
            manager,
            project,
            "changed graph",
            bound_worker,
            prompt_group="default",
            task_run_id="t002",
            report_generation_token="frozen-generation",
            report_claim_token="new-claim",
            durable_request=True,
            report_execution_plan={},
            frozen_report_prompt="frozen prompt",
            delivery_context={
                "delivery_retry": {"source_task_run_id": "t001"},
                "execution_delivery": snapshot,
            },
            finish_state=state,
            finish_outbox=ephemeral_report_finish_outbox(),
        )
        return outcome

    if preparation_delay:
        assert run() == "delivery_unavailable"
        assert captured == []
        assert states[0].input_snapshot == report.ReportArtifactIdentity(
            "a1",
            digest,
        )
        return

    with pytest.raises(ExecutionReached):
        run()
    assert states[0].input_snapshot == report.ReportArtifactIdentity("a1", digest)
    assert captured[0]["phase"] == "report_execute"
    assert captured[0]["resume"] is True
    assert captured[0]["conclude_only"] is True
    assert captured[0]["session"] == SESSION
    assert 0 < captured[0]["timeout_seconds"] <= 21
    request_file, candidate_file, packet_file, graph_file = captured_files[0]
    assert request_file.path.startswith("/tmp/cairn-prompts/")
    assert candidate_file.path.startswith("/tmp/cairn-prompts/pi-candidates-")
    assert candidate_file.content == ""
    assert packet_file.owner == graph_file.owner == "root"
    assert packet_file.mode == graph_file.mode == 0o444
    assert packet_file.readonly_directory == graph_file.readonly_directory
    assert packet_file.path.endswith("/report-packet.json")
    assert graph_file.path.endswith("/graph.yaml")
    assert json.loads(packet_file.content) == _packet(empty=True)
    assert graph_file.content == "original graph"
    assert packet_file.path in captured[0]["prompt"]
    assert graph_file.path in captured[0]["prompt"]

    # 相同 delivery snapshot 的再次交付必须重新物化随机路径，而不是依赖已清理文件。
    with pytest.raises(ExecutionReached):
        run()
    assert states[1].input_snapshot == report.ReportArtifactIdentity("a1", digest)
    assert captured_files[1][2].content == packet_file.content
    assert captured_files[1][3].content == graph_file.content
    assert captured_files[1][2].readonly_directory != packet_file.readonly_directory


@pytest.mark.parametrize("phase", ["bootstrap", "reason", "explore"])
def test_initial_business_execution_waits_for_persisted_delivery_registration(monkeypatch, phase):
    from cairn.dispatcher.runtime.cancellation import TaskCancellation
    from cairn.dispatcher.tasks import bootstrap, explore, reason
    from test_pi_execution import reason_admission_context, worker
    from test_reason_depth_review import execution_view, project_with_execution

    module = {"bootstrap": bootstrap, "reason": reason, "explore": explore}[phase]
    lease = SimpleNamespace(start=lambda: None, stop=lambda: None, failure=None)
    monkeypatch.setattr(module.HeartbeatLease, "for_reason" if phase == "reason" else "for_intent", lambda *a: lease)
    monkeypatch.setattr(module, "best_effort_release_reason" if phase == "reason" else "best_effort_release", lambda *a, **kw: None)
    def forbidden(*a, **kw):
        pytest.fail("unregistered execution reached model")
    monkeypatch.setattr(module, "run_worker_process", forbidden)
    config = SimpleNamespace(runtime=SimpleNamespace(healthcheck_timeout=3, interval=1, prompt_group="default"), tasks=SimpleNamespace(**{phase: SimpleNamespace(timeout=500, max_intents=3)}))
    project = project_with_execution(execution_view(phase="baseline"))
    project.project.assessment_execution["enabled"] = False
    manager = SimpleNamespace(ensure_running=lambda *a: "original-name", runtime_metadata=lambda name: {}, write_text_file=lambda *a, **kw: None)
    from cairn.dispatcher.protocol.client import ApiResult
    api = SimpleNamespace(list_coverage_slots=lambda p: ApiResult(status_code=200, data=[]))
    args = [config, api, manager, project]
    if phase in {"reason", "explore"}:
        args.append("facts: []")
    if phase != "reason":
        args.append(SimpleNamespace(id="i1", assessment_role="discovery"))
    args.extend([worker(), TaskCancellation(), "t1"])
    kwargs = {}
    if phase == "reason":
        kwargs = {
            "prompt_group": "default-v2",
            "admission_context": reason_admission_context("baseline"),
        }
    assert getattr(module, "run_" + phase + "_task")(*args, **kwargs) == "delivery_unavailable"


@pytest.mark.parametrize("phase", ["bootstrap", "reason", "explore", "verifier"])
@pytest.mark.parametrize("resource", ["valid", "missing", "replaced", "stopped", "unreachable", "expired", "model_changed"])
def test_delivery_task_resumes_original_execute_without_readiness_or_new_container(monkeypatch, phase, resource):
    from cairn.dispatcher.runtime.cancellation import TaskCancellation
    from cairn.dispatcher.tasks import bootstrap, explore, reason
    from cairn.dispatcher.tasks.delivery import result_schema_digest
    from test_pi_execution import worker
    from test_reason_depth_review import execution_view, project_with_execution

    task_name = "explore" if phase == "verifier" else phase
    execute_phase = phase + "_execute"
    module = {"bootstrap": bootstrap, "reason": reason, "explore": explore, "verifier": explore}[phase]
    bound_worker = worker()
    if phase == "reason":
        binding = {"allowed_fact_ids": ["f1"], "max_intents": 3}
    elif phase == "verifier":
        binding = {"verification_observation_id": "observation-1"}
    else:
        binding = None
    snapshot = {
        "session_id": SESSION, "phase": execute_phase,
        "deadline_at": (datetime.now(timezone.utc) + timedelta(seconds=21)).isoformat(),
        "container_id": "original-container",
        "model_binding_sha256": sha256(json.dumps(bound_worker.model_binding.model_dump(mode="json"), sort_keys=True).encode()).hexdigest(),
        "result_schema_sha256": result_schema_digest(execute_phase, binding),
        "binding": binding, "inputs": {"open_intent_ids": []},
    }
    original_deadline = snapshot["deadline_at"]
    admission = {"delivery_retry": {"source_task_run_id": "t001"}, "execution_delivery": snapshot}
    lease = SimpleNamespace(start=lambda: None, stop=lambda: None, failure=None)
    monkeypatch.setattr(module.HeartbeatLease, "for_intent" if phase != "reason" else "for_reason", lambda *a: lease)
    released = []
    monkeypatch.setattr(module, "best_effort_release_reason" if phase == "reason" else "best_effort_release", lambda *a, **kw: released.append(True))

    def forbidden(*a, **kw):
        pytest.fail("delivery invoked readiness, new container or new exploration context")

    if phase == "reason":
        monkeypatch.setattr(module, "build_reason_prompt_context", forbidden)
    monkeypatch.setattr(module, "record_prompt_rendered", forbidden)
    def metadata(name):
        if resource == "unreachable":
            raise OSError("synthetic Docker unavailable")
        if resource == "missing":
            return {}
        return {"container_id": "replacement" if resource == "replaced" else "original-container", "container_state": "exited" if resource == "stopped" else "running"}
    if resource == "expired":
        snapshot["deadline_at"] = "2000-01-01T00:00:00Z"
    elif resource == "model_changed":
        snapshot["model_binding_sha256"] = "0" * 64
    manager = SimpleNamespace(
        ensure_running=forbidden, container_name=lambda p: "original-name",
        runtime_metadata=metadata,
    )
    captured = []

    class ExecutionReached(BaseException):
        pass

    def capture(manager, name, worker, command, **kwargs):
        captured.append(json.loads(command.input_files[0].content))
        raise ExecutionReached

    monkeypatch.setattr(module, "run_worker_process", capture)
    config = SimpleNamespace(runtime=SimpleNamespace(healthcheck_timeout=3, interval=1, prompt_group="default"),
                             tasks=SimpleNamespace(**{task_name: SimpleNamespace(timeout=500, max_intents=3)}))
    project = project_with_execution(execution_view(phase="baseline"))
    # 即使实时项目开关和 Intent 角色已漂移，delivery 仍须使用冻结 phase/binding。
    project.project.assessment_execution["enabled"] = False
    intent = SimpleNamespace(
        id="i1",
        assessment_role="discovery",
        target_observation_id=None,
    )
    client = SimpleNamespace(get_observation_verifier_target=forbidden)
    args = [config, client, manager, project]
    if task_name in {"reason", "explore"}:
        args.append("changed graph must not replace frozen inputs")
    if task_name != "reason":
        args.append(intent)
    args += [bound_worker, TaskCancellation(), "t002"]
    run = getattr(module, "run_" + task_name + "_task")
    if resource != "valid":
        assert run(*args, delivery_context=admission) == "delivery_unavailable"
        assert captured == []
        assert released == [True]
        return
    with pytest.raises(ExecutionReached):
        run(*args, delivery_context=admission)
    request = captured[0]
    assert request["phase"] == execute_phase
    assert request["session"] == SESSION
    assert request["resume"] is True
    assert request["conclude_only"] is True
    assert request["binding"] == binding
    assert request["model"] == bound_worker.model_binding.model_dump(mode="json")
    assert snapshot["phase"] == execute_phase
    assert snapshot["deadline_at"] == original_deadline
    assert 0 < request["timeout_seconds"] <= 21
    assert request["max_result_repairs"] == 2


def test_verifier_delivery_validates_result_against_frozen_target(monkeypatch):
    from cairn.dispatcher.runtime.cancellation import TaskCancellation
    from cairn.dispatcher.tasks import explore
    from cairn.dispatcher.tasks.delivery import (
        model_binding_digest,
        result_schema_digest,
    )
    from test_pi_execution import worker
    from test_reason_depth_review import execution_view, project_with_execution

    bound_worker = worker()
    binding = {"verification_observation_id": "observation-frozen"}
    snapshot = {
        "session_id": SESSION,
        "phase": "verifier_execute",
        "deadline_at": (
            datetime.now(timezone.utc) + timedelta(seconds=21)
        ).isoformat(),
        "container_id": "original-container",
        "model_binding_sha256": model_binding_digest(bound_worker),
        "result_schema_sha256": result_schema_digest(
            "verifier_execute",
            binding,
        ),
        "binding": binding,
        "inputs": {},
    }
    delivery_context = {
        "delivery_retry": {"source_task_run_id": "t001"},
        "execution_delivery": snapshot,
    }
    lease = SimpleNamespace(start=lambda: None, stop=lambda: None, failure=None)
    monkeypatch.setattr(
        explore.HeartbeatLease,
        "for_intent",
        lambda *args: lease,
    )

    def forbidden(*args, **kwargs):
        pytest.fail("delivery used live verifier context")

    manager = SimpleNamespace(
        ensure_running=forbidden,
        container_name=lambda project_id: "original-name",
        runtime_metadata=lambda name: {
            "container_id": "original-container",
            "container_state": "running",
        },
    )
    captured = []

    def execute(manager, name, worker, command, **kwargs):
        captured.append(json.loads(command.input_files[0].content))
        return SimpleNamespace(
            read_stdout=lambda: b"",
            transport_error=None,
            returncode=0,
            cleanup=lambda: None,
        )

    monkeypatch.setattr(explore, "run_worker_process", execute)
    monkeypatch.setattr(
        explore,
        "record_process_artifacts",
        lambda *args, **kwargs: SimpleNamespace(refs=[]),
    )
    monkeypatch.setattr(explore, "cancel_reason", lambda *args: None)
    monkeypatch.setattr(explore, "did_timeout", lambda result: False)
    monkeypatch.setattr(explore, "did_oom", lambda result: False)

    class ValidationReached(BaseException):
        pass

    def validate(payload, *, target_observation_id, expected_kind):
        assert target_observation_id == "observation-frozen"
        assert expected_kind == "verifier_result"
        raise ValidationReached

    monkeypatch.setattr(explore, "validate_verifier_payload", validate)

    def decode(stdout, *, validator, **kwargs):
        validator({})
        pytest.fail("verifier validation did not stop the fixture")

    monkeypatch.setattr(explore, "decode_pi_result", decode)
    project = project_with_execution(execution_view(phase="baseline"))
    project.project.assessment_execution["enabled"] = False
    intent = SimpleNamespace(
        id="i1",
        assessment_role="discovery",
        target_observation_id=None,
    )
    config = SimpleNamespace(
        runtime=SimpleNamespace(interval=1, prompt_group="default"),
        tasks=SimpleNamespace(explore=SimpleNamespace(timeout=500)),
    )
    client = SimpleNamespace(get_observation_verifier_target=forbidden)

    with pytest.raises(ValidationReached):
        explore.run_explore_task(
            config,
            client,
            manager,
            project,
            "changed graph must not replace frozen inputs",
            intent,
            bound_worker,
            TaskCancellation(),
            "t002",
            delivery_context=delivery_context,
        )
    assert captured[0]["phase"] == "verifier_execute"
    assert captured[0]["binding"] == binding


def test_report_output_ack_loss_recovers_after_restart_without_model_rerun(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from cairn.dispatcher.protocol.client import ApiResult
    from cairn.dispatcher.runtime.process import ProcessResult
    from cairn.dispatcher.scheduler.report_finish_outbox import ReportFinishOutboxMixin
    from cairn.dispatcher.tasks import report
    from cairn.dispatcher.tasks.report_finish_outbox import (
        PendingReportExecution,
        PendingReportFinish,
        PendingReportOutput,
        ReportFinishOutbox,
    )
    from test_pi_execution import healthcheck_envelope, worker
    from test_report_frozen_rendering import _data, _packet

    packet = _packet()
    model_calls: list[dict[str, object]] = []
    first_artifact_calls: list[dict[str, object]] = []
    recovery_artifact_calls: list[dict[str, object]] = []
    finish_replays: list[dict[str, object]] = []
    claim_token = "claim-token-never-on-disk"
    generation_token = "generation-1"
    task_run_id = "report-run-1"

    class FirstProcessClient:
        def get_assessment_report_packet(self, project_id: str) -> ApiResult:
            assert project_id == "p1"
            return ApiResult(status_code=200, data=packet)

        def create_artifact(self, project_id: str, **kwargs) -> ApiResult:
            call = {"project_id": project_id, **kwargs}
            first_artifact_calls.append(call)
            digest = sha256(kwargs["content"].encode("utf-8")).hexdigest()
            if kwargs["filename"] == "report-input-snapshot.json":
                return ApiResult(
                    status_code=201,
                    data={"id": "snapshot-1", "sha256": digest},
                )
            assert kwargs["filename"] == "vulnerability-report.md"
            # exact output 已到 transport 边界，但 ACK 丢失；本进程不得重新执行模型。
            return ApiResult(status_code=0, text="response lost")

        def bind_assessment_report_input_snapshot(self, *args, **kwargs) -> ApiResult:
            assert kwargs["claim_token"] == claim_token
            return ApiResult(status_code=200, data={})

        def update_task_run(self, project_id: str, run_id: str, **kwargs) -> ApiResult:
            assert (project_id, run_id) == ("p1", task_run_id)
            return ApiResult(status_code=200, data={"id": run_id})

        def claim_assessment_report_finalization(self, *args, **kwargs):
            pytest.fail("run_report_task must not create another claim")

    first_client = FirstProcessClient()

    def execute_once(manager, container, bound_worker, command, **kwargs):
        fenced = first_outbox.entries()
        assert len(fenced) == 1
        assert isinstance(fenced[0], PendingReportExecution)
        request = json.loads(command.input_files[0].content)
        model_calls.append(request)
        raw = json.loads(
            healthcheck_envelope(
                command.session,
                phase="report_execute",
                project_id="p1",
                task_run_id=task_run_id,
                result={
                    "protocol": "cairn-assessment-v2",
                    "accepted": True,
                    "kind": "report_result",
                    "data": _data("v1", "v2"),
                },
            )
        )
        return ProcessResult(0, json.dumps(raw, ensure_ascii=False), "")

    monkeypatch.setattr(report, "run_worker_process", execute_once)
    monkeypatch.setattr(report, "record_process_artifacts", lambda *a, **k: None)
    monkeypatch.setattr(report, "record_prompt_rendered", lambda *a, **k: None)
    monkeypatch.setattr(report, "record_validation_result", lambda *a, **k: None)
    monkeypatch.setattr(report, "notify_browser_task", lambda *a, **k: None)
    manager = SimpleNamespace(
        ensure_running=lambda *a, **k: "fixture",
        write_text_file=lambda *a, **k: None,
        runtime_metadata=lambda name: {
            "container_id": "fixture-report-container-id",
            "container_state": "running",
        },
    )
    project = SimpleNamespace(
        project=SimpleNamespace(
            id="p1",
            execution_generation=0,
            model_dump=lambda: packet["project"],
        )
    )
    config = SimpleNamespace(
        dispatcher_id="dispatcher-1",
        tasks=SimpleNamespace(report=SimpleNamespace(timeout=5)),
    )
    outbox_root = tmp_path / "dispatcher-data" / "report-journal"
    first_outbox = ReportFinishOutbox(outbox_root)
    finish_state = report.ReportFinishState()

    first_outcome = report.run_report_task(
        config,
        first_client,
        manager,
        project,
        "frozen graph\n",
        worker(),
        prompt_group="default",
        task_run_id=task_run_id,
        report_generation_token=generation_token,
        report_claim_token=claim_token,
        durable_request=True,
        report_execution_plan={
            "packet_version": 2,
            "output_contract_version": 2,
            "completeness_rule_version": 2,
            "report_policy_version": 1,
        },
        report_execution_plan_sha256="d" * 64,
        report_prompt_digest_sha256="e" * 64,
        frozen_report_prompt=(
            "Use {{report_packet_json}} and {{graph_yaml}} to submit_result."
        ),
        finish_state=finish_state,
        finish_outbox=first_outbox,
        authenticated_dispatcher_id="dispatcher-1",
    )

    assert first_outcome == "server_write_failed"
    assert len(model_calls) == 1
    assert finish_state.delivery_pending is True
    assert len(first_artifact_calls) == 2
    first_report_write = first_artifact_calls[1]
    pending = first_outbox.entries()
    assert len(pending) == 1
    assert isinstance(pending[0], PendingReportOutput)
    assert pending[0].content == first_report_write["content"]
    assert pending[0].metadata.model_dump(mode="json") == first_report_write["metadata"]
    stored = next(outbox_root.glob("*.json")).read_text(encoding="utf-8")
    assert claim_token not in stored
    assert sha256(claim_token.encode("utf-8")).hexdigest() in stored

    class RestartClient:
        finish_attempt = 0

        def list_artifacts(
            self,
            project_id: str,
            *,
            task_run_id: str | None = None,
        ) -> ApiResult:
            assert (project_id, task_run_id) == ("p1", "report-run-1")
            return ApiResult(status_code=200, data=[])

        def create_artifact(self, project_id: str, **kwargs) -> ApiResult:
            call = {"project_id": project_id, **kwargs}
            recovery_artifact_calls.append(call)
            digest = sha256(kwargs["content"].encode("utf-8")).hexdigest()
            return ApiResult(
                status_code=201,
                data={"id": "recovered-report", "sha256": digest},
            )

        def replay_assessment_report_finalization(
            self,
            project_id: str,
            worker_name: str,
            **kwargs,
        ) -> ApiResult:
            request = {
                "project_id": project_id,
                "worker": worker_name,
                **kwargs,
            }
            finish_replays.append(json.loads(json.dumps(request, ensure_ascii=False)))
            assert "claim_token" not in request
            self.finish_attempt += 1
            if self.finish_attempt == 1:
                return ApiResult(status_code=0, text="finish response lost")
            return ApiResult(
                status_code=200,
                data={
                    "protocol": "cairn-assessment-report-finish-v1",
                    "project_id": project_id,
                    "task_run_id": kwargs["task_run_id"],
                    "generation_token": kwargs["generation_token"],
                    "status": kwargs["status"],
                    "artifact_id": kwargs["artifact_id"],
                    "input_snapshot_artifact_id": kwargs[
                        "input_snapshot_artifact_id"
                    ],
                    "input_snapshot_sha256": kwargs["input_snapshot_sha256"],
                    "request_sha256": kwargs["request_sha256"],
                    "finish_disposition": "accepted",
                },
            )

        def claim_assessment_report_finalization(self, *args, **kwargs):
            pytest.fail("restart recovery must not create another claim")

        def resume_session(self, *args, **kwargs):
            pytest.fail("restart recovery must not resume a model session")

    class RestartHarness(ReportFinishOutboxMixin):
        def __init__(self, outbox, recovery_client):
            self.report_finish_outbox = outbox
            self.client = recovery_client
            self.container_manager = SimpleNamespace()
            self.dispatcher_id = "dispatcher-1"

    restart_client = RestartClient()
    restarted_outbox = ReportFinishOutbox(outbox_root)
    restarted = RestartHarness(restarted_outbox, restart_client)
    monkeypatch.setattr(
        report,
        "run_worker_process",
        lambda *args, **kwargs: pytest.fail("restart recovery reran the model"),
    )

    ReportFinishOutboxMixin._flush_pending_report_finishes(restarted)

    assert recovery_artifact_calls == [first_report_write]
    finish_entries = restarted_outbox.entries()
    assert len(finish_entries) == 1
    assert isinstance(finish_entries[0], PendingReportFinish)
    assert finish_entries[0].artifact_id == "recovered-report"
    assert finish_entries[0].artifact_sha256 == pending[0].artifact_sha256
    assert finish_replays == []
    assert len(model_calls) == 1

    ReportFinishOutboxMixin._flush_pending_report_finishes(restarted)

    assert restarted_outbox.entries() == finish_entries
    assert len(finish_replays) == 1
    assert len(model_calls) == 1

    ReportFinishOutboxMixin._flush_pending_report_finishes(restarted)

    assert restarted_outbox.entries() == []
    assert len(finish_replays) == 2
    assert finish_replays[0] == finish_replays[1]
    assert len(model_calls) == 1


def test_report_crash_after_execution_fence_blocks_claim_and_model_after_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from cairn.dispatcher.protocol.client import ApiResult
    from cairn.dispatcher.scheduler.report_finish_outbox import ReportFinishOutboxMixin
    from cairn.dispatcher.tasks import report
    from cairn.dispatcher.tasks.report_finish_outbox import (
        PendingReportExecution,
        ReportFinishOutbox,
    )
    from test_pi_execution import worker
    from test_report_frozen_rendering import _packet

    packet = _packet(empty=True)
    claim_token = "crash-claim-token-never-on-disk"
    task_run_id = "crash-report-run"
    model_calls: list[str] = []

    class FirstProcessClient:
        def get_assessment_report_packet(self, project_id: str) -> ApiResult:
            return ApiResult(status_code=200, data=packet)

        def create_artifact(self, project_id: str, **kwargs) -> ApiResult:
            assert kwargs["filename"] == "report-input-snapshot.json"
            digest = sha256(kwargs["content"].encode("utf-8")).hexdigest()
            return ApiResult(
                status_code=201,
                data={"id": "crash-snapshot", "sha256": digest},
            )

        def bind_assessment_report_input_snapshot(self, *args, **kwargs) -> ApiResult:
            assert kwargs["claim_token"] == claim_token
            return ApiResult(status_code=200, data={})

        def update_task_run(self, project_id: str, run_id: str, **kwargs) -> ApiResult:
            return ApiResult(status_code=200, data={"id": run_id})

    class SimulatedDispatcherCrash(BaseException):
        pass

    outbox_root = tmp_path / "dispatcher-data" / "report-journal"
    first_outbox = ReportFinishOutbox(outbox_root)

    def crash_after_fence(*args, **kwargs):
        model_calls.append("entered")
        entries = first_outbox.entries()
        assert len(entries) == 1
        assert isinstance(entries[0], PendingReportExecution)
        raise SimulatedDispatcherCrash

    monkeypatch.setattr(report, "run_worker_process", crash_after_fence)
    monkeypatch.setattr(report, "record_prompt_rendered", lambda *a, **k: None)
    monkeypatch.setattr(report, "notify_browser_task", lambda *a, **k: None)
    manager = SimpleNamespace(
        ensure_running=lambda *a, **k: "fixture",
        write_text_file=lambda *a, **k: None,
        runtime_metadata=lambda name: {
            "container_id": "fixture-report-container-id",
            "container_state": "running",
        },
    )
    project = SimpleNamespace(
        project=SimpleNamespace(
            id="p1",
            execution_generation=0,
            model_dump=lambda: packet["project"],
        )
    )
    config = SimpleNamespace(
        dispatcher_id="dispatcher-1",
        tasks=SimpleNamespace(report=SimpleNamespace(timeout=5)),
    )

    with pytest.raises(SimulatedDispatcherCrash):
        report.run_report_task(
            config,
            FirstProcessClient(),
            manager,
            project,
            "frozen graph",
            worker(),
            prompt_group="default",
            task_run_id=task_run_id,
            report_generation_token="generation-1",
            report_claim_token=claim_token,
            durable_request=True,
            report_execution_plan={
                "packet_version": 2,
                "output_contract_version": 2,
                "completeness_rule_version": 2,
                "report_policy_version": 1,
            },
            frozen_report_prompt=(
                "Use {{report_packet_json}} and {{graph_yaml}} to submit_result."
            ),
            finish_outbox=first_outbox,
            authenticated_dispatcher_id="dispatcher-1",
        )

    assert model_calls == ["entered"]
    restarted_outbox = ReportFinishOutbox(outbox_root)
    pending = restarted_outbox.entries()
    assert len(pending) == 1
    assert isinstance(pending[0], PendingReportExecution)
    stored = next(outbox_root.glob("*.json")).read_text(encoding="utf-8")
    assert claim_token not in stored

    class GuardClient:
        def __init__(self) -> None:
            self.claim_calls = 0

        def get_project(self, project_id: str):
            return SimpleNamespace(project=SimpleNamespace(status="completed"))

        def claim_assessment_report_finalization(self, *args, **kwargs):
            self.claim_calls += 1
            pytest.fail("execution fence must block a new claim")

        def finish_assessment_report_finalization(self, *args, **kwargs):
            pytest.fail("execution fence has no finish request")

        def list_artifacts(self, *args, **kwargs):
            pytest.fail("execution fence has no exact output to reconcile")

        def create_artifact(self, *args, **kwargs):
            pytest.fail("execution fence has no exact output to upload")

        def replay_assessment_report_finalization(self, *args, **kwargs):
            pytest.fail("execution fence has no finish request to replay")

    guard_client = GuardClient()
    monkeypatch.setattr(
        report,
        "run_report_task",
        lambda *args, **kwargs: pytest.fail("restart reran the report model"),
    )

    guarded_outcome = report.run_post_completion_report(
        config,
        guard_client,
        SimpleNamespace(),
        "p1",
        "changed graph",
        worker(),
        prompt_group="default",
        durable_request=True,
        finish_outbox=restarted_outbox,
    )

    assert guarded_outcome == "server_write_failed"
    assert guard_client.claim_calls == 0

    class RestartHarness(ReportFinishOutboxMixin):
        def __init__(self):
            self.report_finish_outbox = restarted_outbox
            self.client = guard_client
            self.container_manager = SimpleNamespace()
            self.dispatcher_id = "dispatcher-1"

    RestartHarness()._flush_pending_report_finishes()

    assert restarted_outbox.entries() == pending
    assert model_calls == ["entered"]


def _decision_lifecycle(*, cleanup=True):
    return {"generation": 0, "process_started": True,
            "termination_confirmed": True, "cleanup_confirmed": cleanup}


def _decision_patch(client, run_id, **updates):
    response = client.patch(f"/projects/p/task-runs/{run_id}", json=updates)
    assert response.status_code == 200, response.text
    return response.json()


def _decision_cost_state(conn):
    # 结算可改变 attempt 状态，但不得重复执行、收费、换代或重建授权。
    queries = (
        "SELECT explore_budget, explore_attempts_started, verification_attempts_started, execution_generation, explore_deadline, authorization_expires_at FROM projects WHERE id='p'",
        "SELECT id, intent_id, charged, execution_count FROM assessment_attempts WHERE project_id='p' ORDER BY id",
        "SELECT id, assessment_attempt_id, session_id FROM task_runs WHERE project_id='p' AND task_type='explore' ORDER BY id",
        "SELECT * FROM project_authorization_grants_v2 WHERE project_id='p'",
    )
    return [[tuple(row) for row in conn.execute(query)] for query in queries]


@pytest.fixture
def rejected_reason_with_verifier(conn, client, monkeypatch):
    from contextlib import contextmanager
    from cairn.dispatcher.protocol.client import CairnClient
    from cairn.dispatcher.tasks.delivery import register_delivery
    from cairn.dispatcher.tasks.reason_context import build_reason_prompt_context
    from cairn.server.auth import RequestPrincipal
    from cairn.server.schemas.candidates import (
        CandidateFrameManifest, CandidateOutcomeManifest, canonical_json_bytes, canonical_value_sha256,
    )
    from cairn.server.services import candidate_store
    from cairn.server.services.vulnerability_verifications import schedule_observation_verifier
    from test_goal_driven_authorization_v42 import _seed_goal_project
    from test_goal_driven_reason import FakeContainerManager
    from test_independent_vulnerability_verification import seed_observation, seed_verifier_run
    from test_pi_execution import worker

    _seed_goal_project(conn, project_id="p", status="active")
    conn.execute("UPDATE projects SET execution_generation=0, explore_deadline='2099-01-01T00:00:00Z' WHERE id='p'")
    for fact_id in ("origin", "goal"):
        conn.execute("INSERT INTO facts (id, project_id, description, creator, created_at) VALUES (?, 'p', ?, 'system', '2020-01-01T00:00:00Z')", (fact_id, fact_id))
    seed_observation(conn, project_id="p")
    conn.execute("UPDATE task_runs SET worker_type='pi', runtime_context_json=? WHERE id='tr-discovery'",
        (json.dumps({"dispatcher_id": OWNER}),))
    verifier_intent = schedule_observation_verifier(conn, "p", "vo1")
    # 只复用证据种子；verifier 的 claim、attempt 和收费执行真实 HTTP admission。
    seed_verifier_run(conn, verifier_intent, project_id="p", run_id="seed-verifier")
    conn.execute("UPDATE artifacts SET task_run_id='tr-discovery' WHERE id='a-verifier'")
    conn.execute("DELETE FROM task_runs WHERE id='seed-verifier'")
    conn.commit()
    _decision_patch(client, "tr-discovery", runtime_context={"execution_lifecycle": _decision_lifecycle()})
    started = client.post(f"/projects/p/intents/{verifier_intent}/explore-attempts", json={
        "task_type": "explore", "intent_id": verifier_intent, "worker_name": "worker-b",
        "worker_type": "pi", "status": "running", "expected_execution_generation": 0,
        "session_id": "pi:22222222-2222-4222-8222-222222222222", "runtime_context": {"dispatcher_id": OWNER},
    })
    assert started.status_code == 201, started.text
    verifier_run = started.json()["id"]
    conn.execute("UPDATE artifacts SET task_run_id=? WHERE id='a-verifier'", (verifier_run,))
    conn.commit()
    api = CairnClient("http://testserver", token="synthetic-test-only")
    monkeypatch.setattr(api, "_session", lambda: client)
    manager = FakeContainerManager()
    manager.runtime_metadata = lambda name: {"container_id": "reason-container", "container_state": "running"}
    config = SimpleNamespace(tasks=SimpleNamespace(reason=SimpleNamespace(max_intents=3)))
    bound_worker = worker()
    admitted = admit_reason(client)
    assert admitted.status_code == 201, admitted.text
    reason_run = admitted.json()["id"]
    project = api.get_project("p")
    context = build_reason_prompt_context(config, manager, "reason-container", project, "original evidence", client=api)
    snapshot = register_delivery(api, manager, "reason-container", project, reason_run, bound_worker,
        phase="reason_execute", session=SESSION, timeout_seconds=120,
        binding=context.binding, inputs={"open_intent_ids": [verifier_intent]})
    complete = {"from": ["f-discovery"], "description": "原始完成建议，不应改写或丢失。",
                "worker": "pi-worker", "task_run_id": reason_run}
    rejected = client.post("/projects/p/complete", json=complete)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"] == "goal_driven_completion_open_intents"
    persisted = next(run for run in client.get("/projects/p/task-runs").json() if run["id"] == reason_run)
    assert persisted["runtime_context"]["completion_rejection"] == {
        "code": "goal_driven_completion_open_intents", "decision_input_digest": context.binding["decision_input_digest"],
    }

    @contextmanager
    def get_conn():
        with conn:
            yield conn

    monkeypatch.setattr(candidate_store, "get_conn", get_conn)
    from cairn.server.routers import export as export_router
    monkeypatch.setattr(export_router, "get_conn", get_conn)
    client.app.include_router(export_router.router)
    principal = RequestPrincipal(kind="machine", subject_id=OWNER)
    raw = json.dumps({"action": "complete", "data": complete}, ensure_ascii=False, indent=2).encode()
    capture_id = "33333333-3333-4333-8333-333333333333"
    manifest = CandidateFrameManifest.model_validate({
        "protocol": "cairn-candidate-frame-v1", "record_kind": "candidate", "capture_id": capture_id,
        "producer_candidate_key": f"{capture_id}:1:0", "parent_producer_candidate_key": None,
        "assistant_turn": 1, "content_index": 0, "project_id": "p", "task_run_id": reason_run,
        "phase": "reason_execute", "session": SESSION, "binding_sha256": canonical_value_sha256(context.binding),
        "result_schema_sha256": snapshot["result_schema_sha256"], "raw_sha256": sha256(raw).hexdigest(),
        "raw_size_bytes": len(raw), "artifacts": [],
    })
    receipt = candidate_store.capture_candidate("p", reason_run, manifest,
        canonical_json_bytes(manifest.model_dump(mode="json")), raw, principal=principal)

    def settle_candidate():
        outcome = CandidateOutcomeManifest.model_validate({
            "protocol": "cairn-candidate-frame-v1", "record_kind": "outcome", "capture_id": capture_id,
            "producer_candidate_key": manifest.producer_candidate_key, "outcome_sequence": 1,
            "code": "locally_validated", "path": None, "keyword": None, "count": 1,
            "artifact_status": "validated", "binding_status": "injected",
        })
        return candidate_store.capture_candidate_outcome("p", reason_run, outcome,
            canonical_json_bytes(outcome.model_dump(mode="json")), b"", principal=principal)

    state = SimpleNamespace(api=api, manager=manager, config=config, worker=bound_worker,
        reason_run=reason_run, verifier_run=verifier_run, verifier_intent=verifier_intent,
        snapshot=snapshot, context=context, raw=raw, candidate_id=receipt.candidate_id,
        settle_candidate=settle_candidate, costs=_decision_cost_state(conn))
    yield state
    api.close()


def _decision_conclude_verifier(client, state):
    concluded = client.post(f"/projects/p/intents/{state.verifier_intent}/conclude", json={
        "worker": "worker-b", "description": "独立复核确认跨身份读取；证据保留。", "task_run_id": state.verifier_run,
        "verification_result": {"observation_id": "vo1", "outcome": "confirmed",
            "evidence_refs": [{"artifact_id": "a-verifier"}], "notes": "independent reproduction"},
    })
    assert concluded.status_code == 200, concluded.text
    state.fact_id = concluded.json()["fact"]["id"]


def _decision_loop(state):
    from types import MethodType
    from unittest.mock import Mock
    from cairn.dispatcher.scheduler.dispatch_cycle import DispatchCycleMixin
    from test_dispatcher_multi_project_capacity import _loop

    loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
    loop._try_dispatch_project = MethodType(DispatchCycleMixin._try_dispatch_project, loop)
    loop.client = state.api
    loop.dispatcher_id = OWNER
    loop.reason_checkpoints = {}
    loop._candidate_project_blocked = lambda project_id: False
    loop._scenario_for_project = lambda project: SimpleNamespace(key="default", prompt_group="goal-driven-v1", knowledge=None)
    loop._container_runtime_options = lambda *a: None
    loop._select_worker = lambda *a, **kw: SimpleNamespace(worker=state.worker)
    loop._materialize_selected_worker = lambda *a: (state.worker, {}, None)
    loop._task_run_runtime_context = lambda *a, **kw: {"dispatcher_id": OWNER}
    loop.executor = SimpleNamespace(submit=Mock(return_value=object()))
    return loop


def _decision_assert_blocked(conn, client, state):
    from test_dispatcher_multi_project_capacity import _summary

    before = conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]
    route = next(run for run in client.get("/projects/p/task-runs").json() if run["id"] == state.reason_run)["delivery_retry_route"]
    assert route is not None and route["allowed"] is False
    claim = client.post("/projects/p/reason/claim", json={"worker": "pi-worker", "trigger": "graph_changed"})
    assert claim.status_code == 409, claim.text
    admission = client.post("/projects/p/task-runs", json={
        "task_type": "reason", "worker_name": "pi-worker", "worker_type": "pi", "runtime_context": {"dispatcher_id": OWNER},
    })
    assert admission.status_code == 409, admission.text
    for _ in range(2):
        # 重启丢失 checkpoint 仍不能绕过 Server，不能靠 Dispatcher 缓存避免重复。
        loop = _decision_loop(state)
        assert loop._try_dispatch_project(_summary("p")) is False
        assert loop._try_dispatch_project(_summary("p")) is False
        loop.executor.submit.assert_not_called()
    assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == before
    assert _decision_cost_state(conn) == state.costs


def test_http_verifier_fact_releases_fresh_reason_after_controlled_completion_rejection(
    conn, client, rejected_reason_with_verifier,
):
    from cairn.dispatcher.prompting import load_prompt, render_prompt
    from cairn.dispatcher.tasks.delivery import register_delivery, remaining_delivery_seconds
    from cairn.dispatcher.tasks.reason_context import build_reason_prompt_context
    from cairn.dispatcher.workers.adapters.pi import PiDriver
    from test_dispatcher_multi_project_capacity import _summary

    state = rejected_reason_with_verifier
    state.settle_candidate()
    _decision_patch(client, state.reason_run, status="failed", error_type="business_acceptance",
        runtime_context={"execution_lifecycle": _decision_lifecycle(cleanup=False)})
    assert client.post("/projects/p/reason/release", json={"worker": "pi-worker", "task_run_id": state.reason_run}).status_code == 200
    _decision_assert_blocked(conn, client, state)
    _decision_conclude_verifier(client, state)
    assert conn.execute("SELECT status FROM vulnerabilities WHERE id='v1'").fetchone()[0] == "confirmed"
    # Fact 已被真实接受，但生产者仍运行时不允许切断失败决策链。
    _decision_assert_blocked(conn, client, state)
    _decision_patch(client, state.verifier_run, status="succeeded",
        runtime_context={"execution_lifecycle": _decision_lifecycle(cleanup=False)})
    _decision_assert_blocked(conn, client, state)
    _decision_patch(client, state.verifier_run, runtime_context={"execution_lifecycle": _decision_lifecycle()})
    _decision_assert_blocked(conn, client, state)
    _decision_patch(client, state.reason_run, runtime_context={"execution_lifecycle": _decision_lifecycle()})
    assert all(run["delivery_retry_route"] is None for run in client.get("/projects/p/task-runs").json())
    for field in ("completion_rejection", "reason_decision_boundary"):
        forged = {field: {"source_task_run_id": state.reason_run, "decision_input_digest": "f" * 64}}
        response = client.patch(f"/projects/p/task-runs/{state.reason_run}", json={"runtime_context": forged})
        assert response.status_code == 422, response.text
        response = client.post("/projects/p/task-runs", json={
            "task_type": "reason", "worker_name": "pi-worker", "worker_type": "pi", "runtime_context": {"dispatcher_id": OWNER, **forged},
        })
        assert response.status_code == 422, response.text
    claimed = client.post("/projects/p/reason/claim", json={"worker": "pi-worker", "trigger": "facts_changed"})
    assert claimed.status_code == 200, claimed.text
    old_session = client.post("/projects/p/task-runs", json={
        "task_type": "reason", "worker_name": "pi-worker", "worker_type": "pi", "expected_execution_generation": 0,
        "session_id": SESSION, "runtime_context": {"dispatcher_id": OWNER},
    })
    assert old_session.status_code == 409, old_session.text
    assert old_session.json()["detail"] == "execution_delivery_session_already_bound"
    assert client.post("/projects/p/reason/release", json={"worker": "pi-worker"}).status_code == 200
    loop = _decision_loop(state)
    assert loop._try_dispatch_project(_summary("p")) is True
    kwargs = loop.executor.submit.call_args.kwargs
    assert kwargs["delivery_context"] is None and kwargs["continuation_context"] is None
    new_run = client.get("/projects/p/task-runs").json()[-1]
    assert new_run["id"] != state.reason_run and new_run["session_id"] is None
    boundary = new_run["runtime_context"]["reason_decision_boundary"]
    assert boundary["source_task_run_id"] == state.reason_run
    assert boundary["decision_input_digest"] != state.context.binding["decision_input_digest"]
    assert not {"delivery_retry", "execution_delivery", "continuation", "completion_rejection"} & new_run["runtime_context"].keys()
    duplicate = admit_reason(client)
    assert duplicate.status_code == 409, duplicate.text
    assert len([run for run in client.get("/projects/p/task-runs").json() if run["task_type"] == "reason"]) == 2
    project = state.api.get_project("p")
    export_yaml = state.api.export_project("p")
    context = build_reason_prompt_context(state.config, state.manager, "reason-container", project,
        export_yaml, client=state.api, admission_context=kwargs["admission_context"])
    assert context.binding["decision_input_digest"] == boundary["decision_input_digest"]
    assert state.fact_id in context.binding["allowed_fact_ids"]
    assert any(state.fact_id in text and "独立复核确认跨身份读取" in text
               for text in state.manager.files.values())
    driver = PiDriver()
    session = driver.prepare_session()
    snapshot = register_delivery(state.api, state.manager, "reason-container", project, new_run["id"], state.worker,
        phase="reason_execute", session=session, timeout_seconds=300, binding=context.binding, inputs={"open_intent_ids": []})
    assert session != SESSION and snapshot["deadline_at"] != state.snapshot["deadline_at"]
    command = driver.build_execute(state.worker, render_prompt(load_prompt("goal-driven-v1", "reason.md"), context.replacements),
        session, phase="reason_execute", project_id="p", task_run_id=new_run["id"],
        timeout_seconds=remaining_delivery_seconds(snapshot), binding=context.binding, inputs=snapshot["inputs"])
    request = json.loads(command.input_files[0].content)
    assert request["session"] == session
    assert request["resume"] is False and request["conclude_only"] is False
    assert request["binding"]["decision_input_digest"] == boundary["decision_input_digest"]
    assert state.fact_id in request["binding"]["allowed_fact_ids"]
    complete = {"from": [state.fact_id], "description": "最新独立复核事实已结算，无未决工作。",
                "worker": "pi-worker", "task_run_id": new_run["id"]}
    accepted = client.post("/projects/p/complete", json=complete)
    assert accepted.status_code == 200, accepted.text
    tables = ("task_runs", "facts", "intents", "assessment_attempts", "task_run_candidates", "task_run_candidate_outcomes")
    counts = [conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables]
    replay = client.post("/projects/p/complete", json=complete)
    assert replay.status_code == 200, replay.text
    for key in ("id", "from", "to", "description", "task_run_id"):
        assert replay.json()[key] == accepted.json()[key]
    assert counts == [conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables]
    assert conn.execute("SELECT status FROM projects WHERE id='p'").fetchone()[0] == "completed"
    _decision_patch(client, new_run["id"], status="succeeded", runtime_context={"execution_lifecycle": _decision_lifecycle()})
    assert _decision_cost_state(conn) == state.costs
    old_run = next(run for run in client.get("/projects/p/task-runs").json() if run["id"] == state.reason_run)
    assert old_run["status"] == "failed" and old_run["error_type"] == "business_acceptance"
    stored = conn.execute("SELECT raw_bytes FROM task_run_candidates WHERE id=?", (state.candidate_id,)).fetchone()
    assert bytes(stored[0]) == state.raw
    assert conn.execute("SELECT code FROM task_run_candidate_outcomes WHERE candidate_id=?", (state.candidate_id,)).fetchone()[0] == "locally_validated"


@pytest.mark.parametrize("boundary", ["ordinary_error", "provider_delivery", "accepted_result", "pending_candidate"])
def test_http_changed_verifier_fact_does_not_reset_unqualified_reason_chain(
    conn, client, rejected_reason_with_verifier, boundary, monkeypatch,
):
    state = rejected_reason_with_verifier
    if boundary != "pending_candidate":
        state.settle_candidate()
    error = {"ordinary_error": "provider_auth", "provider_delivery": "submission_missing"}.get(boundary, "business_acceptance")
    _decision_patch(client, state.reason_run, status="failed", error_type=error,
        runtime_context={"execution_lifecycle": _decision_lifecycle()})
    assert client.post("/projects/p/reason/release", json={"worker": "pi-worker", "task_run_id": state.reason_run}).status_code == 200
    if boundary == "provider_delivery":
        from cairn.server.services import assessment_attempts, audit_task_runs
        retry_time = (datetime.now(timezone.utc) + timedelta(seconds=20)).isoformat()
        monkeypatch.setattr(assessment_attempts, "utcnow", lambda: retry_time)
        monkeypatch.setattr(audit_task_runs, "utcnow", lambda: retry_time)
        retry = admit_reason(client)
        assert retry.status_code == 201, retry.text
        retry_id = retry.json()["id"]
        assert retry.json()["runtime_context"]["delivery_retry"] == {"source_task_run_id": state.reason_run}
        _decision_patch(client, retry_id, status="failed", error_type="provider_error",
            runtime_context={"execution_lifecycle": _decision_lifecycle()})
        assert client.post("/projects/p/reason/release", json={"worker": "pi-worker", "task_run_id": retry_id}).status_code == 200
        state.reason_run = retry_id
    elif boundary == "accepted_result":
        # 历史接受回执种子不替代 Server marker、Fact 生产、route 或 admission 规则。
        row = conn.execute("SELECT runtime_context_json FROM task_runs WHERE id=?", (state.reason_run,)).fetchone()
        context = json.loads(row[0])
        context["accepted_result_digest"] = "a" * 64
        conn.execute("UPDATE task_runs SET runtime_context_json=? WHERE id=?", (json.dumps(context), state.reason_run))
        conn.commit()
    _decision_conclude_verifier(client, state)
    _decision_patch(client, state.verifier_run, status="succeeded",
        runtime_context={"execution_lifecycle": _decision_lifecycle()})
    if boundary == "provider_delivery":
        route = next(run for run in client.get("/projects/p/task-runs").json() if run["id"] == state.reason_run)["delivery_retry_route"]
        assert route["reason"] == "execution_retry_cooldown"
    _decision_assert_blocked(conn, client, state)


def test_completion_with_shared_connection_does_not_commit_callers_pending_writes(
    conn, client, rejected_reason_with_verifier,
):
    from fastapi import HTTPException
    from cairn.server.models import CompleteRequest
    from cairn.server.services.project_completion import complete_project_record_with_conn

    state = rejected_reason_with_verifier
    before = conn.execute("SELECT runtime_context_json FROM task_runs WHERE id=?", (state.reason_run,)).fetchone()[0]
    assert conn.in_transaction is False
    conn.execute("INSERT INTO hints (id, project_id, content, creator, created_at) VALUES ('pending-hint', 'p', '尚未提交的业务方向', 'operator', '2026-10-10T00:00:00Z')")
    with pytest.raises(HTTPException) as exc:
        complete_project_record_with_conn(conn, "p", CompleteRequest(
            from_=["f-discovery"], description="旧输入完成建议", worker="pi-worker", task_run_id=state.reason_run,
        ))
    assert exc.value.status_code == 409
    assert exc.value.detail == "goal_driven_completion_input_stale"
    current = json.loads(conn.execute("SELECT runtime_context_json FROM task_runs WHERE id=?", (state.reason_run,)).fetchone()[0])
    assert current["completion_rejection"]["code"] == "goal_driven_completion_input_stale"
    assert conn.in_transaction is True
    # with_conn 不能因为动态拒绝提交调用者事务；HTTP 自有事务另由主线验证。
    conn.rollback()
    assert conn.execute("SELECT 1 FROM hints WHERE id='pending-hint'").fetchone() is None
    assert conn.execute("SELECT runtime_context_json FROM task_runs WHERE id=?", (state.reason_run,)).fetchone()[0] == before


def test_http_stale_snapshot_on_fresh_boundary_cannot_repeat_same_reason_input(
    conn, client, rejected_reason_with_verifier,
):
    from cairn.dispatcher.tasks.delivery import register_delivery
    from cairn.dispatcher.workers.adapters.pi import PiDriver

    state = rejected_reason_with_verifier
    state.settle_candidate()
    source_run_id = state.reason_run
    _decision_patch(client, source_run_id, status="failed", error_type="business_acceptance",
        runtime_context={"execution_lifecycle": _decision_lifecycle()})
    assert client.post("/projects/p/reason/release", json={"worker": "pi-worker", "task_run_id": source_run_id}).status_code == 200
    _decision_conclude_verifier(client, state)
    _decision_patch(client, state.verifier_run, status="succeeded",
        runtime_context={"execution_lifecycle": _decision_lifecycle()})
    admitted = admit_reason(client)
    assert admitted.status_code == 201, admitted.text
    fresh = admitted.json()
    boundary = fresh["runtime_context"]["reason_decision_boundary"]
    assert boundary["source_task_run_id"] == source_run_id
    assert boundary["decision_input_digest"] != state.context.binding["decision_input_digest"]
    # 新会话错误消费旧业务快照，不应因 stale 拒绝再次消费同一份权威输入。
    snapshot = register_delivery(state.api, state.manager, "reason-container", state.api.get_project("p"),
        fresh["id"], state.worker, phase="reason_execute", session=PiDriver.prepare_session(),
        timeout_seconds=300, binding=state.context.binding,
        inputs=state.snapshot["inputs"])
    assert snapshot["session_id"] != SESSION
    rejected = client.post("/projects/p/complete", json={
        "from": ["f-discovery"], "description": "新Run不慎沿用旧完成快照。",
        "worker": "pi-worker", "task_run_id": fresh["id"],
    })
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"] == "goal_driven_completion_input_stale"
    failed = _decision_patch(client, fresh["id"], status="failed", error_type="stale_business_state",
        runtime_context={"execution_lifecycle": _decision_lifecycle()})
    assert failed["runtime_context"]["reason_decision_boundary"] == boundary
    assert failed["runtime_context"]["completion_rejection"] == {
        "code": "goal_driven_completion_input_stale",
        "decision_input_digest": state.context.binding["decision_input_digest"],
    }
    assert client.post("/projects/p/reason/release", json={"worker": "pi-worker", "task_run_id": fresh["id"]}).status_code == 200
    state.reason_run = fresh["id"]
    _decision_assert_blocked(conn, client, state)
    reasons = [run for run in client.get("/projects/p/task-runs").json() if run["task_type"] == "reason"]
    assert len(reasons) == 2 and all(run["status"] == "failed" for run in reasons)
    assert next(run for run in reasons if run["id"] == source_run_id)["runtime_context"]["completion_rejection"]["code"] == "goal_driven_completion_open_intents"
