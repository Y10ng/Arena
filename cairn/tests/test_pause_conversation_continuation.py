"""隔离项目与容器的暂停续作准入；不连接模型或真实 Docker。"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from test_goal_driven_lifecycle import (
    _DISPATCHER,
    _SESSION,
    _seed_running_goal_driven_explore,
    lifecycle_db,
)
from cairn.dispatcher.worker_protocol.submission import build_submit_result_schema
from cairn.server.models import CreateTaskRunRequest, UpdateTaskRunRequest
from cairn.server.schemas.candidates import canonical_value_sha256
from cairn.server.services import audit_task_runs, project_container_pause, project_records
from cairn.server.services.assessment_attempts import delivery_retry_routes, derive_delivery_retry_context
from cairn.server.services.explore_attempts import start_explore_attempt
from cairn.server.services.time_utils import utcnow


def _paused_source(conn, monkeypatch, *, confirmed=True, container_id="container-p", has_session=True):
    _seed_running_goal_driven_explore(conn)
    if not has_session:
        conn.execute("UPDATE task_runs SET session_id = NULL WHERE id = 'run-1'")
    if container_id != "container-p":
        row = conn.execute("SELECT runtime_context_json FROM task_runs WHERE id = 'run-1'").fetchone()
        context = json.loads(row["runtime_context_json"])
        context["execution_delivery"]["container_id"] = container_id
        conn.execute("UPDATE task_runs SET runtime_context_json = ? WHERE id = 'run-1'", (json.dumps(context),))

    @contextmanager
    def same_connection():
        yield conn

    monkeypatch.setattr(project_container_pause, "get_conn", same_connection)
    activation = project_container_pause.register_container_activation("p", "dispatcher", 0, "daemon-a")
    project_container_pause.finish_container_activation("p", "dispatcher", activation["activation_token"], "container-p")
    conn.execute(
        "INSERT INTO dispatcher_status (id, status, runtime_json, updated_at) VALUES (?, 'running', ?, ?)",
        ("dispatcher", json.dumps({"runtime_capabilities": ["project_pause_v1"]}), utcnow()),
    )
    conn.commit()
    assert project_records.project_lifecycle_action("p", "pause").execution_generation == 1
    stop = project_container_pause.claim_container_pause("p", "dispatcher", 1, "daemon-a")
    project_container_pause.confirm_container_pause("p", "dispatcher", 1, stop["stop_token"], "container-p", "daemon-a")
    if confirmed:
        audit_task_runs.update_task_run_record(
            "p", "run-1", UpdateTaskRunRequest(
                status="cancelled", error_type="cancelled",
                runtime_context={"execution_lifecycle": {
                    "generation": 0, "process_started": True,
                    "termination_confirmed": True, "cleanup_confirmed": True,
                }},
            ), principal=_DISPATCHER,
        )
    else:
        audit_task_runs.update_task_run_record(
            "p", "run-1", UpdateTaskRunRequest(status="cancelled", error_type="cancelled"),
            principal=_DISPATCHER,
        )
    conn.commit()


def _admit(conn, *, worker="worker", session_id=None):
    return start_explore_attempt(
        conn, "p", "i1", CreateTaskRunRequest(
            intent_id="i1", task_type="explore", worker_name=worker,
            worker_type="pi", session_id=session_id,
            expected_execution_generation=1,
            runtime_context={"dispatcher_id": "dispatcher"},
        ), principal=_DISPATCHER,
    )


def test_confirmed_pause_reuses_original_session_without_new_logical_attempt(lifecycle_db, monkeypatch):
    conn = lifecycle_db
    _paused_source(conn, monkeypatch)
    assert project_records.project_lifecycle_action("p", "resume").status == "active"
    project = conn.execute("SELECT * FROM projects WHERE id = 'p'").fetchone()
    routes = delivery_retry_routes(conn, project)
    assert routes["run-1"]["continuation"] is True
    assert routes["run-1"]["worker_name"] == "worker"

    next_run = _admit(conn)
    assert next_run.id != "run-1"
    assert next_run.execution_generation == 1
    assert next_run.session_id == _SESSION
    assert next_run.assessment_attempt_id == "attempt-1"
    assert next_run.runtime_context["continuation"] == {
        "source_task_run_id": "run-1", "execution_generation": 1,
        "container_id": "container-p",
    }
    assert "delivery_retry" not in next_run.runtime_context
    assert "execution_delivery" not in next_run.runtime_context
    assert conn.execute("SELECT explore_attempts_started FROM projects WHERE id = 'p'").fetchone()[0] == 1
    assert conn.execute("SELECT execution_count FROM assessment_attempts WHERE id = 'attempt-1'").fetchone()[0] == 2
    assert conn.execute("SELECT session_id FROM task_runs WHERE id = 'run-1'").fetchone()[0] == _SESSION

    binding = {"assessment_protocol": "goal-driven-v1"}
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()
    snapshot = {
        "session_id": _SESSION, "phase": "explore_execute", "deadline_at": deadline,
        "container_id": "container-p", "model_binding_sha256": "a" * 64,
        "result_schema_sha256": canonical_value_sha256(build_submit_result_schema("explore_execute", binding=binding)),
        "binding": binding, "inputs": {},
    }
    # 新执行只在原容器本代激活得到确认后才能登记自己的正向 deadline。
    with pytest.raises(HTTPException) as not_active:
        audit_task_runs.update_task_run_record(
            "p", next_run.id, UpdateTaskRunRequest(session_id=_SESSION, runtime_context={"execution_delivery": snapshot}),
            principal=_DISPATCHER,
        )
    assert not_active.value.detail == "execution_continuation_workspace_unavailable"
    activation = project_container_pause.register_container_activation("p", "dispatcher", 1, "daemon-a")
    project_container_pause.finish_container_activation("p", "dispatcher", activation["activation_token"], "container-p")
    recorded = audit_task_runs.update_task_run_record(
        "p", next_run.id, UpdateTaskRunRequest(session_id=_SESSION, runtime_context={"execution_delivery": snapshot}),
        principal=_DISPATCHER,
    )
    assert recorded.runtime_context["execution_delivery"]["deadline_at"] == deadline
    assert recorded.runtime_context["execution_delivery"]["binding"] == binding


@pytest.mark.parametrize("case,detail", [
    ("unconfirmed", "execution_continuation_not_confirmed"),
    ("wrong_container", "execution_continuation_workspace_unavailable"),
    ("wrong_worker", "execution_continuation_owner_mismatch"),
    ("wrong_session", "execution_continuation_is_server_owned"),
    ("no_transcript", "execution_continuation_session_missing"),
])
def test_pause_continuation_never_falls_back_to_fresh_session(lifecycle_db, monkeypatch, case, detail):
    conn = lifecycle_db
    _paused_source(conn, monkeypatch, confirmed=case != "unconfirmed",
                   container_id="wrong-container" if case == "wrong_container" else "container-p",
                   has_session=case != "no_transcript")
    if case == "unconfirmed":
        with pytest.raises(HTTPException) as draining:
            project_records.project_lifecycle_action("p", "resume")
        assert draining.value.detail == "project_pause_draining"
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE project_id='p'").fetchone()[0] == 1
        return
    else:
        project_records.project_lifecycle_action("p", "resume")
    with pytest.raises(HTTPException) as denied:
        _admit(conn, worker="different" if case == "wrong_worker" else "worker",
               session_id="pi:22222222-2222-4222-8222-222222222222" if case == "wrong_session" else None)
    assert denied.value.detail == detail
    assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE project_id='p'").fetchone()[0] == 1


def test_later_reason_phase_does_not_resurrect_old_paused_reason(lifecycle_db, monkeypatch):
    conn = lifecycle_db
    _paused_source(conn, monkeypatch)
    project_records.project_lifecycle_action("p", "resume")
    original = json.loads(conn.execute("SELECT runtime_context_json FROM task_runs WHERE id='run-1'").fetchone()[0])
    original["assessment_phase"] = "baseline"
    conn.execute(
        "UPDATE task_runs SET task_type='reason', intent_id=NULL, assessment_attempt_id=NULL, runtime_context_json=? WHERE id='run-1'",
        (json.dumps(original),),
    )
    conn.execute(
        "INSERT INTO task_runs (id, project_id, task_type, worker_name, worker_type, status, execution_generation, runtime_context_json) "
        "VALUES ('run-2', 'p', 'reason', 'worker', 'pi', 'succeeded', 1, ?)",
        (json.dumps({"assessment_phase": "baseline", "dispatcher_id": "dispatcher"}),),
    )
    conn.execute("UPDATE projects SET assessment_progress_json=? WHERE id='p'", (json.dumps({"phase": "depth_review"}),))
    project = conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()
    assert "run-1" not in delivery_retry_routes(conn, project)
    assert derive_delivery_retry_context(
        conn, project, CreateTaskRunRequest(
            task_type="reason", worker_name="worker", worker_type="pi",
            runtime_context={"dispatcher_id": "dispatcher"},
        ),
    ) == {}


def test_never_started_pause_run_allows_fresh_worker(lifecycle_db, monkeypatch):
    conn = lifecycle_db
    _paused_source(conn, monkeypatch)
    context = json.loads(conn.execute("SELECT runtime_context_json FROM task_runs WHERE id='run-1'").fetchone()[0])
    context["execution_lifecycle"]["process_started"] = False
    conn.execute("UPDATE task_runs SET runtime_context_json=?, session_id=NULL WHERE id='run-1'", (json.dumps(context),))
    project_records.project_lifecycle_action("p", "resume")
    next_run = _admit(conn, worker="different")
    assert next_run.session_id is None
    assert "continuation" not in next_run.runtime_context
    assert next_run.assessment_attempt_id == "attempt-1"


def test_prestart_failure_retries_original_session_with_existing_limit(lifecycle_db, monkeypatch):
    conn = lifecycle_db
    _paused_source(conn, monkeypatch)
    project_records.project_lifecycle_action("p", "resume")
    first = _admit(conn)
    audit_task_runs.update_task_run_record(
        "p", first.id, UpdateTaskRunRequest(
            status="failed", error_type="container_failed",
            runtime_context={"execution_lifecycle": {
                "generation": 1, "process_started": False,
                "termination_confirmed": True, "cleanup_confirmed": False,
            }},
        ), principal=_DISPATCHER,
    )
    from cairn.server.models import HeartbeatRequest
    from cairn.server.routers import intents as intents_router

    intents_router.release("p", "i1", HeartbeatRequest(worker="worker", task_run_id=first.id))
    conn.execute("UPDATE task_runs SET ended_at = '2020-01-01T00:00:00Z' WHERE id = ?", (first.id,))
    project = conn.execute("SELECT * FROM projects WHERE id = 'p'").fetchone()
    route = delivery_retry_routes(conn, project)[first.id]
    assert route["allowed"] is True and route["continuation"] is True
    assert route["source_task_run_id"] == "run-1"
    next_run = _admit(conn)
    assert next_run.session_id == _SESSION
    assert next_run.runtime_context["continuation"] == first.runtime_context["continuation"]
    assert next_run.assessment_attempt_id == "attempt-1"
    assert conn.execute("SELECT explore_attempts_started FROM projects WHERE id = 'p'").fetchone()[0] == 1
    assert conn.execute("SELECT execution_count FROM assessment_attempts WHERE id = 'attempt-1'").fetchone()[0] == 3
    for run in (next_run,):
        audit_task_runs.update_task_run_record(
            "p", run.id, UpdateTaskRunRequest(
                status="failed", error_type="container_failed",
                runtime_context={"execution_lifecycle": {
                    "generation": 1, "process_started": False,
                    "termination_confirmed": True, "cleanup_confirmed": False,
                }},
            ), principal=_DISPATCHER,
        )
        intents_router.release("p", "i1", HeartbeatRequest(worker="worker", task_run_id=run.id))
        conn.execute("UPDATE task_runs SET ended_at = '2020-01-01T00:00:00Z' WHERE id = ?", (run.id,))
    last = _admit(conn)
    audit_task_runs.update_task_run_record(
        "p", last.id, UpdateTaskRunRequest(
            status="failed", error_type="container_failed",
            runtime_context={"execution_lifecycle": {
                "generation": 1, "process_started": False,
                "termination_confirmed": True, "cleanup_confirmed": False,
            }},
        ), principal=_DISPATCHER,
    )
    assert delivery_retry_routes(conn, conn.execute("SELECT * FROM projects WHERE id='p'").fetchone())[last.id]["reason"] == "execution_retry_exhausted"
    assert conn.execute("SELECT explore_attempts_started FROM projects WHERE id='p'").fetchone()[0] == 1


@pytest.mark.parametrize("invalid", ["process_started", "delivery_registered", "source_changed"])
def test_continuation_failure_without_prestart_proof_remains_blocked(lifecycle_db, monkeypatch, invalid):
    conn = lifecycle_db
    _paused_source(conn, monkeypatch)
    project_records.project_lifecycle_action("p", "resume")
    first = _admit(conn)
    context = dict(first.runtime_context)
    lifecycle = dict(context["execution_lifecycle"])
    lifecycle.update(process_started=invalid == "process_started", termination_confirmed=True)
    context["execution_lifecycle"] = lifecycle
    if invalid == "delivery_registered":
        context["execution_delivery"] = json.loads(conn.execute(
            "SELECT runtime_context_json FROM task_runs WHERE id = 'run-1'",
        ).fetchone()[0])["execution_delivery"]
    if invalid == "source_changed":
        context["continuation"] = {**context["continuation"], "source_task_run_id": "absent"}
    conn.execute(
        "UPDATE task_runs SET status='failed', error_type='container_failed', ended_at=?, runtime_context_json=? WHERE id=?",
        ("2020-01-01T00:00:00Z", json.dumps(context), first.id),
    )
    project = conn.execute("SELECT * FROM projects WHERE id = 'p'").fetchone()
    assert delivery_retry_routes(conn, project)[first.id]["allowed"] is False
    with pytest.raises(HTTPException):
        _admit(conn)
    assert conn.execute("SELECT explore_attempts_started FROM projects WHERE id='p'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE project_id='p'").fetchone()[0] == 2
    assert conn.execute("SELECT execution_count FROM assessment_attempts WHERE id='attempt-1'").fetchone()[0] == 2


def test_second_pause_before_continuation_started_keeps_original_conversation(lifecycle_db, monkeypatch):
    conn = lifecycle_db
    _paused_source(conn, monkeypatch)
    project_records.project_lifecycle_action("p", "resume")
    first_continuation = _admit(conn)
    activation = project_container_pause.register_container_activation("p", "dispatcher", 1, "daemon-a")
    project_container_pause.finish_container_activation("p", "dispatcher", activation["activation_token"], "container-p")
    assert project_records.project_lifecycle_action("p", "pause").execution_generation == 2
    stop = project_container_pause.claim_container_pause("p", "dispatcher", 2, "daemon-a")
    project_container_pause.confirm_container_pause("p", "dispatcher", 2, stop["stop_token"], "container-p", "daemon-a")
    audit_task_runs.update_task_run_record(
        "p", first_continuation.id, UpdateTaskRunRequest(
            status="cancelled", error_type="cancelled",
            runtime_context={"execution_lifecycle": {
                "generation": 1, "process_started": False,
                "termination_confirmed": True, "cleanup_confirmed": True,
            }},
        ), principal=_DISPATCHER,
    )
    assert project_records.project_lifecycle_action("p", "resume").status == "active"
    project = conn.execute("SELECT * FROM projects WHERE id = 'p'").fetchone()
    routes = delivery_retry_routes(conn, project)
    assert routes["run-1"]["continuation"] is True
    assert first_continuation.id not in routes
    next_run = start_explore_attempt(
        conn, "p", "i1", CreateTaskRunRequest(
            intent_id="i1", task_type="explore", worker_name="worker", worker_type="pi",
            expected_execution_generation=2, runtime_context={"dispatcher_id": "dispatcher"},
        ), principal=_DISPATCHER,
    )
    assert next_run.session_id == _SESSION
    assert next_run.runtime_context["continuation"]["source_task_run_id"] == "run-1"
    assert next_run.assessment_attempt_id == "attempt-1"
    assert conn.execute("SELECT explore_attempts_started FROM projects WHERE id = 'p'").fetchone()[0] == 1


def test_two_paused_sessions_preflight_activation_and_explore_execution(lifecycle_db, monkeypatch, tmp_path):
    from cairn.dispatcher.config import ContainerConfig
    from cairn.dispatcher.protocol.client import ApiResult
    from cairn.dispatcher.runtime import readiness
    from cairn.dispatcher.runtime.cancellation import TaskCancellation
    from cairn.dispatcher.runtime.containers import ContainerManager
    from cairn.dispatcher.runtime.process import ProcessResult
    from cairn.dispatcher.scheduler.common import DeliveryWorkerRoute
    from cairn.dispatcher.scheduler.task_lifecycle import TaskLifecycleMixin
    from cairn.dispatcher.tasks import explore
    from cairn.server.services.assessment_attempts import delivery_retry_routes
    from test_pi_container_ownership import FakeContainer, FakeDocker, INSTANCE_ID

    conn = lifecycle_db
    _paused_source(conn, monkeypatch)
    other_session = "pi:33333333-3333-4333-8333-333333333333"
    original = json.loads(conn.execute("SELECT runtime_context_json FROM task_runs WHERE id='run-1'").fetchone()[0])
    original["execution_delivery"]["session_id"] = other_session
    now = utcnow()
    conn.execute(
        "INSERT INTO intents (id, project_id, description, creator, worker, last_heartbeat_at, created_at, assessment_role, priority) "
        "VALUES ('i2', 'p', 'second unfinished question', 'reasoner', NULL, ?, ?, 'discovery', 1)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO assessment_attempts (id, project_id, intent_id, assessment_role, status, charged, execution_count, "
        "max_runtime_retries, strategy_key, started_at) VALUES ('attempt-2', 'p', 'i2', 'discovery', 'active', 1, 1, 2, '', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO task_runs (id, project_id, intent_id, task_type, worker_name, worker_type, status, session_id, "
        "assessment_attempt_id, runtime_context_json, execution_generation, started_at, ended_at, pause_requested, pause_interrupted) "
        "VALUES ('run-other', 'p', 'i2', 'explore', 'worker', 'pi', 'cancelled', ?, 'attempt-2', ?, 0, ?, ?, 1, 1)",
        (other_session, json.dumps(original), now, now),
    )
    conn.execute("UPDATE projects SET explore_attempts_started=2 WHERE id='p'")
    assert project_records.project_lifecycle_action("p", "resume").status == "active"

    docker = FakeDocker()
    docker.daemon_id = "daemon-a"
    monkeypatch.setattr("cairn.dispatcher.runtime.containers.docker.from_env", lambda *, timeout: docker)
    monkeypatch.setenv("CAIRN_RUNTIME_TEMP_DIR", str(tmp_path / "locks"))
    manager = ContainerManager(ContainerConfig(image="worker:test", network_mode="none", completed_action="stop"), instance_id=INSTANCE_ID)
    retained = FakeContainer(docker, manager.container_name("p"), manager._container_labels("p", manager.default_runtime_options()), state="exited")
    retained.id = "container-p"
    retained.image = SimpleNamespace(attrs={"RepoDigests": []})
    retained.workspace_marker = "original workspace survives"
    runner = tmp_path / "cli.js"
    runner.write_text('"session-continuation-v1"')

    @contextmanager
    def runner_file(name, path):
        assert name == manager.container_name("p") and path == "/opt/cairn/pi-agent/dist/cli.js"
        yield SimpleNamespace(path=runner, size=runner.stat().st_size)

    monkeypatch.setattr(manager, "materialize_file", runner_file)
    monkeypatch.setattr(readiness, "run_readiness_check", lambda *_args, **_kwargs: SimpleNamespace(
        ok=True, capabilities=frozenset({"session-continuation-v1"}),
    ))

    class Client:
        def get_container_activation(self, project_id):
            return ApiResult(200, data={"activation": project_container_pause.container_activation(project_id, "dispatcher")})

        def register_container_activation(self, project_id, generation, daemon_id):
            return ApiResult(200, data=project_container_pause.register_container_activation(project_id, "dispatcher", generation, daemon_id))

        def finish_container_activation(self, project_id, token, container_id):
            return ApiResult(200, data=project_container_pause.finish_container_activation(project_id, "dispatcher", token, container_id))

        def update_task_run(self, project_id, task_run_id, **kwargs):
            audit_task_runs.update_task_run_record(
                project_id, task_run_id, UpdateTaskRunRequest(**kwargs), principal=_DISPATCHER,
            )
            return ApiResult(200)

    client = Client()
    loop = SimpleNamespace(container_manager=manager, client=client, _project_generations={"p": 1})
    worker = SimpleNamespace(name="worker", id="worker", type="pi", model_binding=SimpleNamespace(
        model_dump=lambda **_kwargs: {"model": "synthetic"},
    ))
    config = SimpleNamespace(runtime=SimpleNamespace(prompt_group="goal-driven-v1", interval=1),
                             tasks=SimpleNamespace(explore=SimpleNamespace(timeout=90, conclude_timeout=10)))
    project = SimpleNamespace(project=SimpleNamespace(
        id="p", assessment_protocol="goal-driven-v1", assessment_execution={"version": 2, "executable": True, "enabled": True},
    ), facts=[])
    sessions = {_SESSION: ["first earlier assistant reply"], other_session: ["second earlier assistant reply"]}
    executed = []

    class Driver:
        def build_execute(self, _worker, _prompt, session, **kwargs):
            assert kwargs["resume"] is True and kwargs["conclude_only"] is False
            assert sessions[session]
            executed.append((session, kwargs["task_run_id"], tuple(sessions[session])))
            return SimpleNamespace(session=session)

    monkeypatch.setattr(explore, "get_driver", lambda _type: Driver())
    monkeypatch.setattr(explore.HeartbeatLease, "for_intent", lambda *_args: SimpleNamespace(
        start=lambda: None, stop=lambda: None, failure=None,
    ))
    monkeypatch.setattr(explore, "explore_prompt_replacements", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(explore, "render_prompt", lambda *_args: "synthetic continuation")
    monkeypatch.setattr(explore, "load_prompt", lambda *_args: "synthetic")
    monkeypatch.setattr(explore, "record_prompt_rendered", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(explore, "record_process_artifacts", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(explore, "conclude_or_release_on_failure", lambda *_args, **_kwargs: None)

    def controlled_process(_manager, name, _worker, _invocation, *, task_run_id, **_kwargs):
        assert name == retained.name and retained.workspace_marker == "original workspace survives"
        assert task_run_id == executed[-1][1]
        sessions[executed[-1][0]].append(f"continued by {task_run_id}")
        return ProcessResult(1, "", "", transport_error=True)

    monkeypatch.setattr(explore, "run_worker_process", controlled_process)
    for intent_id, source_id, session in (("i1", "run-1", _SESSION), ("i2", "run-other", other_session)):
        project_row = conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()
        route = delivery_retry_routes(conn, project_row)[source_id]
        assert route["allowed"] and route["continuation"] and route["container_id"] == retained.id
        assert TaskLifecycleMixin._preflight_retained_runner(
            loop, "p", "explore", worker,
            DeliveryWorkerRoute(workers=[worker], mode="continuation", container_id=route["container_id"]),
        )
        admitted = start_explore_attempt(
            conn, "p", intent_id, CreateTaskRunRequest(
                intent_id=intent_id, task_type="explore", worker_name="worker", worker_type="pi",
                expected_execution_generation=1, runtime_context={"dispatcher_id": "dispatcher"},
            ), principal=_DISPATCHER,
        )
        assert admitted.session_id == session
        result = explore.run_explore_task(
            config, client, manager, project, "", SimpleNamespace(id=intent_id, assessment_role="discovery"),
            worker, TaskCancellation(generation=1), admitted.id, container_runtime=manager.default_runtime_options(),
            continuation_context={**admitted.runtime_context["continuation"], "session_id": admitted.session_id},
        )
        assert result.failure_class == "transport"
        assert conn.execute("SELECT session_id FROM task_runs WHERE id=?", (admitted.id,)).fetchone()[0] == session
    assert executed == [
        (_SESSION, executed[0][1], ("first earlier assistant reply",)),
        (other_session, executed[1][1], ("second earlier assistant reply",)),
    ]
    assert sessions[_SESSION][-1] == f"continued by {executed[0][1]}"
    assert sessions[other_session][-1] == f"continued by {executed[1][1]}"
    assert retained.actions == ["start"] and docker.runs == []
    from cairn.dispatcher.tasks.delivery import retained_runner_available, DeliveryUnavailable, resume_continuation

    assert not retained_runner_available(manager, "p", "another-container", client=client, generation=1)
    assert not retained_runner_available(manager, "p", retained.id, client=client, generation=0)
    with pytest.raises(DeliveryUnavailable):
        resume_continuation(
            client, manager, "p", 0, manager.default_runtime_options(),
            {"container_id": retained.id, "session_id": _SESSION, "execution_generation": 0},
        )
    assert retained.actions == ["start"]
    assert retained.workspace_marker == "original workspace survives"
    assert conn.execute("SELECT description FROM facts WHERE id='f1'").fetchone()[0] == "bootstrap accepted"
    assert conn.execute("SELECT explore_attempts_started FROM projects WHERE id='p'").fetchone()[0] == 2
    assert [row[0] for row in conn.execute("SELECT execution_count FROM assessment_attempts ORDER BY id")] == [2, 2]


@pytest.mark.parametrize("task_type", ["reason", "bootstrap", "explore"])
def test_unsupported_retained_runner_blocks_before_claim(monkeypatch, task_type):
    from cairn.dispatcher.scheduler.common import DeliveryWorkerRoute
    from cairn.dispatcher.scheduler import task_lifecycle
    from test_dispatcher_multi_project_capacity import _loop

    loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
    worker = SimpleNamespace(id="worker", name="worker", type="pi")
    route = DeliveryWorkerRoute(workers=[worker], mode="continuation", container_id="container-p")
    loop._delivery_route_workers = lambda *args, **kwargs: route
    loop._scenario_for_project = lambda project: SimpleNamespace(key="fixture")
    loop._container_runtime_options = lambda *args: {}
    loop._select_worker = lambda *args, **kwargs: SimpleNamespace(worker=worker)
    loop._materialize_selected_worker = lambda *args: (worker, {}, None)
    loop._project_generations = {"p": 1}
    loop.client.claim_reason = Mock(side_effect=AssertionError("claimed reason"))
    loop.client.heartbeat = Mock(side_effect=AssertionError("claimed bootstrap"))
    loop.client.start_explore_attempt = Mock(side_effect=AssertionError("admitted explore"))
    loop._create_task_run = Mock(side_effect=AssertionError("created task run"))
    loop.executor = SimpleNamespace(submit=Mock(side_effect=AssertionError("executed task")))
    inspected = []

    def unavailable(manager, project_id, container_id, **kwargs):
        inspected.append((project_id, container_id))
        return False

    monkeypatch.setattr(task_lifecycle, "retained_runner_available", unavailable)
    project = SimpleNamespace(project=SimpleNamespace(id="p", tenant_workers=[], execution_generation=1))
    intent = SimpleNamespace(id="i", worker_history=[], assessment_role="explorer")
    if task_type == "reason":
        dispatched = loop._dispatch_reason(project, "", "idle")
    elif task_type == "bootstrap":
        dispatched = loop._dispatch_bootstrap(project, intent)
    else:
        dispatched = loop._dispatch_explore(project, "", intent)
    assert dispatched is False
    assert inspected == [("p", "container-p")]
    assert loop._project_decisions["p"]["code"] == "execution_continuation_unavailable"
    loop.client.claim_reason.assert_not_called()
    loop.client.heartbeat.assert_not_called()
    loop.client.start_explore_attempt.assert_not_called()
    loop._create_task_run.assert_not_called()
    loop.executor.submit.assert_not_called()


@pytest.mark.parametrize("marker,state,expected", [
    (True, "exited", True), (False, "exited", False), (True, "running", False),
    (True, "replaced", False),
])
def test_retained_runner_offline_inspection_requires_same_container(tmp_path, marker, state, expected):
    from cairn.dispatcher.tasks.delivery import retained_runner_available

    runner = tmp_path / "cli.js"
    runner.write_text('"session-continuation-v1"' if marker else '"candidate-spool-v1"')

    class Manager:
        checks = 0

        def container_name(self, project_id):
            assert project_id == "p"
            return "cairn-p"

        def runtime_metadata(self, name):
            self.checks += 1
            assert name == "cairn-p"
            return {"container_id": "container-p" if state != "replaced" or self.checks == 1 else "replacement",
                    "container_state": "exited" if state == "replaced" else state}

        @contextmanager
        def materialize_file(self, name, path):
            assert name == "cairn-p"
            assert path == "/opt/cairn/pi-agent/dist/cli.js"
            yield SimpleNamespace(path=runner, size=runner.stat().st_size)

    manager = Manager()
    assert retained_runner_available(manager, "p", "container-p") is expected
    assert manager.checks == (1 if state == "running" else 2)
    assert retained_runner_available(manager, "p", "wrong-container") is False


def test_unsupported_original_runner_has_explicit_blocked_diagnostic():
    from cairn.server.services.project_runtime_diagnostics import DECISIONS

    state, message = DECISIONS["execution_continuation_unavailable"]
    assert state == "blocked"
    assert "原项目容器" in message and "空会话" in message


def test_pi_adapter_continuation_executes_normal_phase_with_new_run_and_checkpoints():
    from cairn.dispatcher.config import PiModelConfig, WorkerConfig
    from cairn.dispatcher.workers.adapters.pi import PiDriver

    worker = WorkerConfig(
        id="worker", engine="pi", task_types=["explore"], concurrency=1, priority=0,
        model_binding=PiModelConfig(
            protocol="anthropic-messages", model_name="fixture-model",
            inference_endpoint="https://fixture.invalid/v1/messages",
            base_url="https://fixture.invalid", auth_header="x-api-key", api_key="fixture-secret",
        ),
    )
    request = PiDriver().build_execute(
        worker, "Continue the unfinished work", _SESSION,
        phase="explore_execute", project_id="p", task_run_id="run-2",
        timeout_seconds=74, conclude_timeout_seconds=14,
        binding={"assessment_protocol": "goal-driven-v1"},
        resume=True, conclude_only=False, enable_checkpoints=True,
        continuation={"source_task_run_id": "run-1", "execution_generation": 1,
                      "container_id": "container-p", "session_id": _SESSION},
    )
    payload = json.loads(request.input_files[0].content)
    assert payload["session"] == _SESSION
    assert payload["task_run_id"] == "run-2"
    assert payload["resume"] is True and payload["conclude_only"] is False
    assert payload["continuation"] == {"source_task_run_id": "run-1", "execution_generation": 1}
    assert payload["timeout_seconds"] == 74 and payload["conclude_timeout_seconds"] == 14
    assert "execution_checkpoints" in payload["capabilities"]
    assert request.checkpoint_spool is not None


@pytest.mark.parametrize("capable", [True, False])
def test_retained_container_requires_exact_activation_and_runner_capability(monkeypatch, capable):
    import sys
    from types import SimpleNamespace
    from cairn.dispatcher.tasks.delivery import DeliveryUnavailable, resume_continuation

    activations = []

    class Manager:
        state = "exited"

        def container_name(self, project_id):
            assert project_id == "p"
            return "cairn-p"

        def runtime_metadata(self, name):
            assert name == "cairn-p"
            return {"container_id": "container-p", "container_state": self.state}

        def activate_existing_for_continuation(self, project_id, options, expected_id, *, register, finish, reuse):
            assert expected_id == "container-p"
            token = register("daemon-a")
            self.state = "running"
            finish(token, expected_id)
            return self.container_name(project_id)

    class Client:
        def register_container_activation(self, project_id, generation, daemon_id):
            activations.append(("register", generation, daemon_id))
            return SimpleNamespace(ok=True, data={"activation_token": "token-a"})

        def finish_container_activation(self, project_id, token, container_id):
            activations.append(("finish", token, container_id))
            return SimpleNamespace(ok=True)

    check = lambda *args, **kwargs: SimpleNamespace(
        ok=True, capabilities=frozenset({"session-continuation-v1"} if capable else {"candidate-spool-v1"}),
    )
    monkeypatch.setitem(sys.modules, "cairn.dispatcher.runtime.readiness", SimpleNamespace(run_readiness_check=check))
    manager = Manager()
    context = {"container_id": "container-p", "session_id": _SESSION,
               "source_task_run_id": "run-1", "execution_generation": 1}
    if capable:
        assert resume_continuation(Client(), manager, "p", 1, None, context) == ("cairn-p", _SESSION)
    else:
        with pytest.raises(DeliveryUnavailable, match="retained_container_continuation_unsupported"):
            resume_continuation(Client(), manager, "p", 1, None, context)
    assert activations == [("register", 1, "daemon-a"), ("finish", "token-a", "container-p")]
    manager.state = "exited"
    with pytest.raises(DeliveryUnavailable):
        resume_continuation(Client(), manager, "p", 2, None, context)
    assert manager.state == "exited"
