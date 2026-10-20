from __future__ import annotations

import asyncio
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path

import pytest
from fastapi import HTTPException, Response

from cairn.dispatcher.tasks.delivery import result_schema_digest
from cairn.server import db
from cairn.server.app import app
from route_helpers import iter_app_routes
from cairn.server.auth import (
    DEFAULT_PROVISIONED_PASSWORD,
    RequestPrincipal,
    create_machine_principal,
    create_user_with_personal_tenant,
    require_dispatcher,
    require_super_admin,
    verify_password,
)
from cairn.server.models import AdminUserPasswordResetRequest
from cairn.server.routers import admin, monitoring, vulnerabilities
from cairn.server.routers.auth import LoginRequest, login_route


@pytest.fixture(autouse=True)
def configured_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db._reset_for_tests()
    database_path = tmp_path / "admin-monitoring.db"
    monkeypatch.setenv("CAIRN_DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("CAIRN_TRUSTED_ORIGINS", "http://testserver")
    db.configure(database_path)
    yield
    db._reset_for_tests()


def _session_credentials(login: str, password: str) -> tuple[str, str]:
    response = Response()
    view = login_route(LoginRequest(login=login, password=password), response)
    session_cookie = response.headers["set-cookie"].split("cairn_session=", 1)[1].split(";", 1)[0]
    return session_cookie, view.csrf_token


def _admin_request(
    method: str,
    path: str,
    *,
    session_cookie: str | None = None,
    csrf_token: str | None = None,
    authorization: str | None = None,
    payload: dict[str, object] | None = None,
) -> tuple[int, dict[str, object], str]:
    body = json.dumps(payload).encode() if payload is not None else b""
    headers = [
        (b"host", b"testserver"),
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"origin", b"http://testserver"),
    ]
    if csrf_token is not None:
        headers.append((b"x-cairn-csrf-token", csrf_token.encode()))
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    if session_cookie is not None:
        headers.append((b"cookie", f"cairn_session={session_cookie}".encode()))

    messages: list[dict[str, object]] = []
    received = False

    async def receive() -> dict[str, object]:
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))
    start = next(message for message in messages if message["type"] == "http.response.start")
    raw = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    ).decode()
    return int(start["status"]), json.loads(raw), raw


def _identity_project():
    user_id, tenant_id = create_user_with_personal_tenant("identity-owner", "safe-password")
    cookie, csrf = _session_credentials("identity-owner", "safe-password")
    machine_id, token = create_machine_principal("identity-machine", "machine-secret")
    snapshot = {"key": "default", "prompt_group": "default", "features": {
        "execution_contract_version": 1, "execution_engine": "pi",
    }}
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, tenant_id, scenario_snapshot_json) "
            "VALUES ('identity-project', 'Identity fixture', 'active', ?, ?, ?)",
            ("2026-09-01T00:00:00Z", tenant_id, json.dumps(snapshot)),
        )
    return cookie, csrf, machine_id, token


def test_authenticated_admission_binds_owner_and_rejects_config_identity():
    cookie, csrf, machine_id, token = _identity_project()
    configured_id = "configured-dispatcher-not-machine"
    assert machine_id != configured_id
    status, heartbeat, _ = _admin_request("POST", "/dispatcher/heartbeat", authorization=f"Bearer {token}", payload={
        "dispatcher_id": configured_id, "status": "starting",
    })
    assert status == 200
    assert heartbeat["dispatcher_id"] == machine_id
    path = "/projects/identity-project/task-runs"
    body = {"task_type": "bootstrap", "worker_name": "fixture", "worker_type": "pi"}
    status, error, _ = _admin_request("POST", path, authorization=f"Bearer {token}", payload={
        **body, "runtime_context": {"dispatcher_id": configured_id},
    })
    assert (status, error.get("detail")) == (403, "execution_owner_mismatch")
    status, error, _ = _admin_request("POST", path, session_cookie=cookie, csrf_token=csrf, payload=body)
    assert (status, error.get("detail")) == (403, "execution_owner_machine_required")
    status, run, _ = _admin_request("POST", path, authorization=f"Bearer {token}", payload=body)
    assert status == 201, run
    assert run["runtime_context"]["dispatcher_id"] == machine_id


def test_authenticated_proof_requires_immutable_exact_owner_and_monotonic_generation():
    cookie, csrf, machine_id, token = _identity_project()
    other_id, other_token = create_machine_principal("other-machine", "other-secret")
    path = "/projects/identity-project/task-runs"
    status, run, _ = _admin_request("POST", path, authorization=f"Bearer {token}", payload={
        "task_type": "bootstrap", "worker_name": "fixture", "worker_type": "pi",
        "runtime_context": {"dispatcher_id": machine_id},
    })
    assert status == 201, run
    run_path = f"{path}/{run['id']}"
    proof = {"generation": 0, "process_started": True, "termination_confirmed": True, "cleanup_confirmed": True}
    for authorization, cookie_value, csrf_value, context, expected in [
        (f"Bearer {other_token}", None, None, {"execution_lifecycle": proof}, "execution_owner_mismatch"),
        (None, cookie, csrf, {"execution_lifecycle": proof}, "execution_owner_machine_required"),
        (f"Bearer {token}", None, None, {"dispatcher_id": other_id}, "execution_owner_immutable"),
    ]:
        status, error, _ = _admin_request("PATCH", run_path, authorization=authorization,
            session_cookie=cookie_value, csrf_token=csrf_value, payload={"runtime_context": context})
        assert (status, error.get("detail")) == (403, expected)
    status, error, _ = _admin_request("PATCH", run_path, authorization=f"Bearer {token}", payload={
        "runtime_context": {"execution_lifecycle": {**proof, "generation": 1}},
    })
    assert (status, error.get("detail")) == (409, "execution_generation_mismatch")
    status, updated, _ = _admin_request("PATCH", run_path, authorization=f"Bearer {token}", payload={
        "status": "cancelled", "runtime_context": {"execution_lifecycle": proof},
    })
    assert status == 200, updated
    status, error, _ = _admin_request("PATCH", run_path, authorization=f"Bearer {token}", payload={
        "runtime_context": {"execution_lifecycle": {**proof, "cleanup_confirmed": False}},
    })
    assert (status, error.get("detail")) == (409, "execution_lifecycle_confirmation_conflict")
    status, runs, _ = _admin_request("GET", path, session_cookie=cookie)
    assert status == 200
    assert runs[0]["runtime_context"]["dispatcher_id"] == machine_id
    assert runs[0]["runtime_context"]["execution_lifecycle"] == proof


@pytest.mark.parametrize("admission", ["explore", "report"])
def test_authenticated_alternate_admissions_bind_machine_owner(admission):
    cookie, csrf, machine_id, token = _identity_project()
    if admission == "explore":
        project_id = "identity-project"
        with db.get_conn() as conn:
            conn.execute("INSERT INTO intents (id, project_id, description, creator, created_at) "
                         "VALUES ('intent-1', ?, 'Fixture', 'fixture', '2026-09-01T00:00:00Z')", (project_id,))
        path = f"/projects/{project_id}/intents/intent-1/explore-attempts"
        body = {"task_type": "explore", "intent_id": "intent-1", "worker_name": "fixture", "worker_type": "pi"}
        status, error, _ = _admin_request("POST", path, authorization=f"Bearer {token}", payload={
            **body, "runtime_context": {"dispatcher_id": "configured-not-owner"},
        })
        assert (status, error.get("detail")) == (403, "execution_owner_mismatch")
    else:
        from test_assessment_reports import seed_project
        project_id = "p1"
        with db.get_conn() as conn:
            seed_project(conn)
            conn.execute("UPDATE projects SET tenant_id = (SELECT tenant_id FROM projects WHERE id = 'identity-project') WHERE id = 'p1'")
        status, requested, _ = _admin_request("POST", f"/projects/{project_id}/assessment-report-finalization/request",
            session_cookie=cookie, csrf_token=csrf, payload={"template_key": "standard-pentest"})
        assert status == 202, requested
        path = f"/projects/{project_id}/assessment-report-finalization/claim"
        body = {"worker": "fixture"}
    status, error, _ = _admin_request("POST", path, session_cookie=cookie, csrf_token=csrf, payload=body)
    assert (status, error.get("detail")) == (403, "execution_owner_machine_required")
    status, result, _ = _admin_request("POST", path, authorization=f"Bearer {token}", payload=body)
    assert status == (201 if admission == "explore" else 200), result
    status, runs, _ = _admin_request("GET", f"/projects/{project_id}/task-runs", session_cookie=cookie)
    assert status == 200
    assert len(runs) == 1
    assert runs[0]["runtime_context"]["dispatcher_id"] == machine_id


def _authenticated_scheduler(monkeypatch, tmp_path, token):
    from cairn.dispatcher.protocol.client import CairnClient
    from cairn.dispatcher.scheduler.loop import DispatcherBootstrap, DispatcherLoop
    from test_dispatcher_startup_healthcheck import make_config
    from test_pi_container_ownership import FakeDocker

    events = []
    docker = FakeDocker()

    def docker_client(*, timeout):
        events.append("container_manager")
        return docker

    monkeypatch.setattr("cairn.dispatcher.runtime.containers.docker.from_env", docker_client)
    monkeypatch.setenv("CAIRN_RUNTIME_TEMP_DIR", str(tmp_path / "runtime"))
    config = make_config([]).model_copy(update={"server": "http://testserver", "dispatcher_id": "configured-not-machine"})
    client = CairnClient(config.server, token=token)

    def send(_adapter, request, **kwargs):
        from urllib.parse import urlsplit
        from requests import Response as HttpResponse

        assert urlsplit(request.url).netloc == "testserver"
        path = urlsplit(request.url).path
        status, data, text = _admin_request(request.method, path,
            authorization=request.headers.get("Authorization"),
            payload=json.loads(request.body) if request.body else None)
        if path == "/dispatcher/heartbeat":
            events.append(("heartbeat", status, data.get("dispatcher_id")))
        response = HttpResponse()
        response.status_code = status
        response._content = text.encode()
        response.headers["Content-Type"] = "application/json"
        response.request = request
        response.url = request.url
        return response

    # 只替换 requests 的网络 Adapter，真实 Client 组装 Bearer 后仍走完整 ASGI 认证。
    monkeypatch.setattr("requests.adapters.HTTPAdapter.send", send)
    loop = DispatcherLoop(DispatcherBootstrap(config_path=tmp_path / "dispatch.yaml", hard_cap=4), client=client, config=config)
    return loop, docker, events


def test_scheduler_freezes_real_heartbeat_subject_before_container_or_selfcheck(monkeypatch, tmp_path):
    _, _, machine_id, token = _identity_project()
    loop, docker, events = _authenticated_scheduler(monkeypatch, tmp_path, token)
    try:
        assert events == []
        assert loop.container_manager is None
        loop.run_startup_healthchecks()
        assert events[0] == ("heartbeat", 200, machine_id)
        assert events[1] == "container_manager"
        assert loop.dispatcher_id == machine_id != loop.config.dispatcher_id
        from cairn.dispatcher.runtime.containers import container_instance_id
        assert loop.container_manager.instance_id == container_instance_id(machine_id, loop.config.server)
        loop._apply_config(loop.config.model_copy(update={"dispatcher_id": "changed-config-name"}), force=False)
        assert loop.dispatcher_id == machine_id
        assert loop.container_manager.instance_id == container_instance_id(machine_id, loop.config.server)
        assert docker.runs == []
    finally:
        loop.close()


def test_scheduler_failed_first_real_auth_never_builds_manager_or_dispatches(monkeypatch, tmp_path):
    _identity_project()
    loop, docker, events = _authenticated_scheduler(monkeypatch, tmp_path, "invalid-machine-token")
    with pytest.raises(RuntimeError, match="dispatcher_identity_handshake_failed"):
        loop.run(once=True)
    assert loop.container_manager is None
    assert loop.dispatcher_id is None
    assert events == [("heartbeat", 401, None)]
    assert docker.runs == []


def test_scheduler_changed_real_principal_stops_before_dispatch_and_keeps_namespace(monkeypatch, tmp_path):
    cookie, csrf, machine_id, token = _identity_project()
    other_id, other_token = create_machine_principal("replacement-machine", "replacement-secret")
    loop, docker, events = _authenticated_scheduler(monkeypatch, tmp_path, token)
    loop.run_startup_healthchecks()
    status, paused, _ = _admin_request("POST", "/projects/identity-project/pause", session_cookie=cookie, csrf_token=csrf)
    assert status == 200, paused
    manager = loop.container_manager
    loop.client._session().headers["Authorization"] = f"Bearer {other_token}"
    with pytest.raises(RuntimeError, match="dispatcher_identity_changed"):
        loop.run(once=True)
    assert loop.dispatcher_id == machine_id
    assert loop.container_manager is manager
    assert events[-1] == ("heartbeat", 200, other_id)
    assert events.count("container_manager") == 1
    assert docker.runs == []


def test_real_auth_scheduler_context_pause_exact_cleanup_proof_and_resume(monkeypatch, tmp_path):
    from concurrent.futures import Future, wait
    from types import SimpleNamespace
    from cairn.dispatcher.models import RunningTask
    from cairn.dispatcher.runtime.cancellation import TaskCancellation
    from cairn.server.models import Scenario
    from test_dispatcher_startup_healthcheck import pi_worker
    from test_pi_container_ownership import owned_container, FakeContainer

    cookie, csrf, machine_id, token = _identity_project()
    loop, docker, _ = _authenticated_scheduler(monkeypatch, tmp_path, token)
    try:
        loop.run_startup_healthchecks()
        project_id = "identity-project"
        project = loop.client.get_project(project_id)
        runtime = loop.container_manager.default_runtime_options()
        worker = pi_worker("fixture", enabled=True)
        context = loop._task_run_runtime_context(project, "bootstrap", worker, None,
            scenario=Scenario(key="default", label="Fixture"), container_runtime=runtime)
        assert context["dispatcher_id"] == machine_id != loop.config.dispatcher_id
        owned = owned_container(loop.container_manager, docker, project=project_id)
        unrelated = FakeContainer(docker, "legacy-unrelated", {})
        response = loop.client.create_task_run(project_id, intent_id=None, task_type="bootstrap",
            worker_name=worker.name, worker_type="pi", prompt_group="default", runtime_context=context)
        assert response.status_code == 201, response.text
        run = response.data
        cancellation = TaskCancellation(generation=run["execution_generation"])
        process = SimpleNamespace(execution_started=True, execution_completed=False,
            execution_identity=(owned.id, "fixture-exec"))
        cancellation.register_execution(process)
        cancellation.observe_execution_start(process)
        future = Future()
        loop.futures[future] = RunningTask(project_id, "bootstrap", worker.name, "pi", cancellation,
            runtime, task_run_id=run["id"])
        status, paused, _ = _admin_request("POST", f"/projects/{project_id}/pause", session_cookie=cookie, csrf_token=csrf)
        assert status == 200, paused
        assert paused["execution_generation"] == 1
        summaries = loop.client.list_projects()
        loop._cancel_inactive_tasks(summaries)
        assert cancellation.is_cancelled
        loop._cleanup_paused_containers(summaries)
        assert owned.actions == []
        status, blocked, _ = _admin_request("POST", f"/projects/{project_id}/resume", session_cookie=cookie, csrf_token=csrf)
        assert (status, blocked.get("detail")) == (409, "project_pause_draining")
        process.execution_completed = True
        cancellation.finish_execution(process)
        future.set_result("cancelled")
        loop._reap_futures()
        loop._flush_pending_task_run_finishes()
        assert wait([item[0] for item in loop._task_run_finish_futures.values()], timeout=5).not_done == set()
        loop._flush_pending_task_run_finishes()
        persisted = loop.client.list_task_runs(project_id).data[0]
        assert persisted["status"] == "cancelled"
        assert persisted["runtime_context"]["execution_lifecycle"] == {
            "generation": 0, "process_started": True, "termination_confirmed": True, "cleanup_confirmed": False,
        }
        loop._cleanup_paused_containers(summaries)
        assert wait(list(loop.cleanup_futures), timeout=5).not_done == set()
        loop._reap_cleanup_futures()
        assert owned.actions[-1] == "remove"
        assert unrelated.actions == []
        persisted = loop.client.list_task_runs(project_id).data[0]
        assert persisted["runtime_context"]["execution_lifecycle"]["cleanup_confirmed"] is True
        status, diagnostic, _ = _admin_request("GET", f"/projects/{project_id}/runtime", session_cookie=cookie)
        assert status == 200
        assert diagnostic["state"] == "paused"
        assert diagnostic["actions"]["resume"] == {"allowed": True, "reason": None}
        status, resumed, _ = _admin_request("POST", f"/projects/{project_id}/resume", session_cookie=cookie, csrf_token=csrf)
        assert status == 200, resumed
        assert resumed["execution_generation"] == 1
        assert resumed["status"] == "active"
    finally:
        loop.close()


@pytest.mark.parametrize("task_type", ["bootstrap", "reason", "explore"])
@pytest.mark.parametrize("routing", ["original", "other_owner", "unavailable", "cooldown"])
def test_real_http_delivery_route_selects_original_worker_before_claim(monkeypatch, tmp_path, routing, task_type):
    from concurrent.futures import Future
    from types import SimpleNamespace
    from unittest.mock import Mock

    _, _, machine_id, token = _identity_project()
    with db.get_conn() as conn:
        conn.execute("INSERT INTO intents (id, project_id, description, creator, created_at) VALUES ('delivery-intent', 'identity-project', 'fixture', 'system', '2026-01-01T00:00:00Z')")
    loop, _, _ = _authenticated_scheduler(monkeypatch, tmp_path, token)
    try:
        loop.run_startup_healthchecks()
        if task_type == "explore":
            created = loop.client.start_explore_attempt("identity-project", "delivery-intent", "original-worker", worker_type="pi", prompt_group="default")
        else:
            created = loop.client.create_task_run("identity-project", intent_id=None if task_type == "reason" else "delivery-intent",
                task_type=task_type, worker_name="original-worker", worker_type="pi", prompt_group="default")
        assert created.status_code == 201, created.text
        run_id = created.data["id"]
        session = "pi:11111111-1111-4111-8111-111111111111"
        phase = f"{task_type}_execute"
        binding = None
        inputs = {}
        if task_type == "reason":
            binding = {"allowed_fact_ids": [], "max_intents": 3}
            inputs = {"open_intent_ids": ["delivery-intent"]}
        snapshot = {"session_id": session, "phase": phase, "deadline_at": "2098-01-01T00:00:00Z",
                    "container_id": "original-container", "model_binding_sha256": "a" * 64,
                    "result_schema_sha256": result_schema_digest(phase, binding),
                    "binding": binding, "inputs": inputs}
        registered = loop.client.update_task_run("identity-project", run_id, status="running", session_id=session,
            runtime_context={"execution_delivery": snapshot})
        assert registered.ok, registered.text
        # 仅控制服务端时钟；来源与终止证明仍由真实 owner 通过 HTTP 持久化。
        with monkeypatch.context() as clock:
            if routing != "cooldown":
                clock.setattr("cairn.server.services.audit_task_runs.utcnow", lambda: "2020-01-01T00:00:00Z")
            finished = loop.client.update_task_run("identity-project", run_id, status="failed", error_type="submission_invalid",
                runtime_context={"execution_lifecycle": {"generation": 0, "process_started": True,
                    "termination_confirmed": True, "cleanup_confirmed": False}})
        assert finished.ok, finished.text
        if task_type == "explore":
            assert loop.client.release("identity-project", "delivery-intent", "original-worker", task_run_id=run_id).ok
        route = loop.client.list_task_runs("identity-project").data[0]["delivery_retry_route"]
        assert route["dispatcher_id"] == machine_id
        assert route["allowed"] is (routing != "cooldown")
        if routing == "other_owner":
            loop.close()
            _, other_token = create_machine_principal("delivery-other", "other-secret")
            loop, _, _ = _authenticated_scheduler(monkeypatch, tmp_path, other_token)
            loop.run_startup_healthchecks()
        project = loop.client.get_project("identity-project")
        project.project.tenant_workers = [SimpleNamespace(id=name, engine="pi", task_types=[task_type],
            max_running=1, priority=priority, enabled=enabled, currently_available=True, unavailable_reason=None)
            for name, priority, enabled in [("other-worker", 1, True), ("original-worker", 100, routing != "unavailable")]]
        # 只替换凭据物化与执行器边界；候选排序、HTTP claim/admission 均执行生产实现。
        monkeypatch.setattr(loop, "_materialize_selected_worker", lambda _pid, _task, selection, _bindings: (selection.worker, {}, None))
        readiness = Mock(side_effect=AssertionError("delivery retry must not run readiness"))
        monkeypatch.setattr(loop, "_probe_worker_readiness", readiness)
        submitted = Mock(return_value=Future())
        monkeypatch.setattr(loop.executor, "submit", submitted)
        intent = next(item for item in project.intents if item.id == "delivery-intent")
        if task_type == "reason":
            dispatched = loop._dispatch_reason(project, "", "fixture")
        elif task_type == "explore":
            dispatched = loop._dispatch_explore(project, "", intent)
        else:
            dispatched = loop._dispatch_bootstrap(project, intent)
        assert dispatched is (routing == "original")
        runs = loop.client.list_task_runs("identity-project").data
        if routing == "original":
            assert len(runs) == 2
            readiness.assert_not_called()
            newest = next(item for item in runs if item["id"] != run_id)
            assert newest["worker_name"] == "original-worker"
            assert newest["runtime_context"]["dispatcher_id"] == machine_id
            assert newest["runtime_context"]["delivery_retry"] == {"source_task_run_id": run_id}
            assert submitted.call_args.kwargs["delivery_context"]["execution_delivery"] == snapshot
        else:
            assert len(runs) == 1
            submitted.assert_not_called()
            detail = loop.client.get_project("identity-project")
            assert next(item for item in detail.intents if item.id == "delivery-intent").worker is None
            assert loop._project_decisions["identity-project"]["code"] == (
                "execution_retry_cooldown" if routing == "cooldown" else "execution_delivery_unavailable")
    finally:
        loop.futures.clear()
        loop.close()


@pytest.mark.parametrize("task_type", ["bootstrap", "reason", "explore"])
def test_real_http_readiness_failure_precedes_claim_task_run_and_charge(
    monkeypatch,
    tmp_path,
    task_type,
):
    from types import SimpleNamespace
    from unittest.mock import Mock

    _, _, _, token = _identity_project()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO intents (id, project_id, description, creator, created_at) "
            "VALUES ('readiness-intent', 'identity-project', 'fixture', 'system', "
            "'2026-01-01T00:00:00Z')"
        )

    loop, _, _ = _authenticated_scheduler(monkeypatch, tmp_path, token)
    try:
        loop.run_startup_healthchecks()
        project = loop.client.get_project("identity-project")
        project.project.tenant_workers = [
            SimpleNamespace(
                id="ready-worker",
                engine="pi",
                task_types=[task_type],
                max_running=1,
                priority=0,
                enabled=True,
                currently_available=True,
                unavailable_reason=None,
            )
        ]
        scenario = SimpleNamespace(
            key="default",
            prompt_group="default",
            knowledge=None,
        )
        monkeypatch.setattr(loop, "_scenario_for_project", lambda _project: scenario)
        monkeypatch.setattr(loop, "_container_runtime_options", lambda *_args: None)
        monkeypatch.setattr(
            loop,
            "_materialize_selected_worker",
            lambda _pid, _task, selection, _bindings: (selection.worker, {}, None),
        )
        readiness = Mock(return_value=False)
        monkeypatch.setattr(loop, "_probe_worker_readiness", readiness)
        submitted = Mock(
            side_effect=AssertionError("readiness failure submitted work")
        )
        monkeypatch.setattr(loop.executor, "submit", submitted)
        intent = next(
            item for item in project.intents if item.id == "readiness-intent"
        )

        if task_type == "reason":
            dispatched = loop._dispatch_reason(project, "", "fixture")
        elif task_type == "explore":
            dispatched = loop._dispatch_explore(project, "", intent)
        else:
            dispatched = loop._dispatch_bootstrap(project, intent)

        assert dispatched is False
        readiness.assert_called_once()
        submitted.assert_not_called()
        assert loop.client.list_task_runs("identity-project").data == []
        with db.get_conn() as conn:
            project_row = conn.execute(
                "SELECT reason_worker, reason_last_heartbeat_at, "
                "explore_attempts_started FROM projects WHERE id='identity-project'"
            ).fetchone()
            intent_row = conn.execute(
                "SELECT worker, last_heartbeat_at FROM intents "
                "WHERE project_id='identity-project' AND id='readiness-intent'"
            ).fetchone()
            attempt_count = conn.execute(
                "SELECT COUNT(*) AS count FROM assessment_attempts "
                "WHERE project_id='identity-project'"
            ).fetchone()["count"]
        assert dict(project_row) == {
            "reason_worker": None,
            "reason_last_heartbeat_at": None,
            "explore_attempts_started": 0,
        }
        assert dict(intent_row) == {
            "worker": None,
            "last_heartbeat_at": None,
        }
        assert attempt_count == 0
    finally:
        loop.close()


@pytest.mark.parametrize("routing", ["original", "other_owner", "unavailable", "cooldown"])
def test_real_http_report_delivery_route_constrains_executor_worker(monkeypatch, tmp_path, routing):
    from concurrent.futures import Future
    from types import SimpleNamespace
    from unittest.mock import Mock
    from test_assessment_reports import seed_project

    cookie, csrf, machine_id, token = _identity_project()
    with db.get_conn() as conn:
        seed_project(conn, status="completed")
        conn.execute("UPDATE projects SET tenant_id = (SELECT tenant_id FROM projects WHERE id = 'identity-project') WHERE id = 'p1'")
    status, requested, _ = _admin_request("POST", "/projects/p1/assessment-report-finalization/request",
        session_cookie=cookie, csrf_token=csrf, payload={"template_key": "standard-pentest"})
    assert status == 202, requested
    loop, _, _ = _authenticated_scheduler(monkeypatch, tmp_path, token)
    try:
        loop.run_startup_healthchecks()
        claim = loop.client.claim_assessment_report_finalization("p1", "original-worker")
        assert claim.ok, claim.text
        run_id = claim.data["task_run_id"]
        plan = json.loads(claim.data["report_execution_plan_json"])
        snapshot_content = json.dumps({
            "version": 1,
            "report_packet": {"version": plan["packet_version"], "project": {"id": "p1"}},
            "graph_yaml": "project: p1\n",
        }, ensure_ascii=False)
        snapshot_sha256 = sha256(snapshot_content.encode("utf-8")).hexdigest()
        snapshot_artifact = loop.client.create_artifact(
            "p1",
            task_run_id=run_id,
            kind="report",
            content=snapshot_content,
            filename="report-input-snapshot.json",
            category="deliverable",
            metadata={
                "source": "report_task",
                "format": "json",
                "report_generation_token": claim.data["generation_token"],
                "report_template_key": claim.data["template_key"],
                "report_template_version": claim.data["template_version"],
                "report_template_digest_sha256": claim.data["template_digest_sha256"],
                "report_execution_plan_sha256": claim.data["report_execution_plan_sha256"],
                "report_prompt_digest_sha256": claim.data["report_prompt_digest_sha256"],
                "report_policy_version": plan["report_policy_version"],
                "packet_version": plan["packet_version"],
                "snapshot_sha256": snapshot_sha256,
            },
        )
        assert snapshot_artifact.ok, snapshot_artifact.text
        snapshot_artifact_id = snapshot_artifact.data["id"]
        assert snapshot_artifact.data["sha256"] == snapshot_sha256
        bound = loop.client.bind_assessment_report_input_snapshot("p1", "original-worker", claim_token=claim.data["claim_token"],
            artifact_id=snapshot_artifact_id, digest_sha256=snapshot_sha256)
        assert bound.ok, bound.text
        session = "pi:11111111-1111-4111-8111-111111111111"
        snapshot = {"session_id": session, "phase": "report_execute", "deadline_at": "2098-01-01T00:00:00Z",
            "container_id": "original-container", "model_binding_sha256": "a" * 64,
            "result_schema_sha256": result_schema_digest("report_execute", None), "binding": None,
            "inputs": {"snapshot_path": f"/tmp/cairn-prompts/report-delivery-{run_id}.json", "input_snapshot_artifact_id": snapshot_artifact_id,
                "input_snapshot_sha256": snapshot_sha256, "generation_token": claim.data["generation_token"]}}
        registered = loop.client.update_task_run("p1", run_id, status="running", session_id=session,
            runtime_context={"execution_delivery": snapshot})
        assert registered.ok, registered.text
        lifecycle = loop.client.update_task_run(
            "p1",
            run_id,
            runtime_context={"execution_lifecycle": {"generation": 0, "process_started": True,
                "termination_confirmed": True, "cleanup_confirmed": False}},
        )
        assert lifecycle.ok, lifecycle.text
        with monkeypatch.context() as clock:
            if routing != "cooldown":
                clock.setattr("cairn.server.services.assessment_reports.utcnow", lambda: "2020-01-01T00:00:00Z")
            finalized = loop.client.finish_assessment_report_finalization(
                "p1",
                "original-worker",
                claim_token=claim.data["claim_token"],
                generation_token=claim.data["generation_token"],
                task_run_id=run_id,
                status="failed",
                artifact_id=None,
                artifact_sha256=None,
                input_snapshot_artifact_id=snapshot_artifact_id,
                input_snapshot_sha256=snapshot_sha256,
                task_run_result={
                    "status": "failed",
                    "exit_code": None,
                    "error_type": "submission_invalid",
                    "error_message": "submission_invalid",
                    "worker_failure": None,
                },
            )
        assert finalized.ok, finalized.text
        state = loop.client.get_assessment_report_finalization("p1")
        assert state.data["delivery_retry_route"]["allowed"] is (routing != "cooldown")
        if routing == "other_owner":
            loop.close()
            _, other_token = create_machine_principal("report-other", "other-secret")
            loop, _, _ = _authenticated_scheduler(monkeypatch, tmp_path, other_token)
            loop.run_startup_healthchecks()
        project = loop.client.get_project("p1")
        project.project.tenant_workers = [SimpleNamespace(id=name, engine="pi", task_types=["reason", "bootstrap"],
            max_running=1, priority=priority, enabled=enabled, currently_available=True, unavailable_reason=None)
            for name, priority, enabled in [("other-worker", 1, True), ("original-worker", 100, routing != "unavailable")]]
        monkeypatch.setattr(loop.client, "get_project", lambda _pid: project)
        monkeypatch.setattr(
            loop.client,
            "export_project",
            lambda _pid: "project: p1\n",
        )
        monkeypatch.setattr(loop, "_materialize_selected_worker", lambda _pid, _task, selection, _bindings: (selection.worker, {}, None))
        submitted = Mock(return_value=Future())
        monkeypatch.setattr(loop.executor, "submit", submitted)
        assert loop._try_dispatch_completed_report("p1") is (routing == "original")
        if routing == "original":
            assert submitted.call_args.args[6].name == "original-worker"
        else:
            submitted.assert_not_called()
            assert loop._project_decisions["p1"]["code"] == (
                "execution_retry_cooldown" if routing == "cooldown" else "execution_delivery_unavailable")
        assert len(loop.client.list_task_runs("p1").data) == 1
    finally:
        loop.futures.clear()
        loop.close()


@pytest.mark.parametrize("historical_owner", ["historical-configured-id", None])
def test_confirmed_historical_snapshot_retires_owner_with_current_authenticated_ready(historical_owner):
    cookie, csrf, machine_id, token = _identity_project()
    # 独立历史快照回归，不把直接写入的双确认冒充 scheduler 清理整链。
    context = {"dispatcher_id": historical_owner, "execution_lifecycle": {
        "generation": 0, "process_started": True, "termination_confirmed": True, "cleanup_confirmed": True,
    }}
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET status = 'paused', execution_generation = 1 WHERE id = 'identity-project'")
        conn.execute("INSERT INTO task_runs (id, project_id, task_type, worker_name, worker_type, status, "
                     "execution_generation, pause_requested, runtime_context_json) "
                     "VALUES ('historical-run', 'identity-project', 'bootstrap', 'fixture', 'pi', 'failed', 0, 1, ?)",
                     (json.dumps(context),))
    status, heartbeat, _ = _admin_request("POST", "/dispatcher/heartbeat", authorization=f"Bearer {token}", payload={
        "dispatcher_id": "current-configured-id", "status": "idle",
        "runtime": {"runtime_capabilities": ["project_pause_v1", "project_runtime_v1"]},
    })
    assert status == 200
    assert heartbeat["dispatcher_id"] == machine_id != historical_owner
    status, diagnostic, _ = _admin_request("GET", "/projects/identity-project/runtime", session_cookie=cookie)
    assert status == 200
    assert diagnostic["actions"]["resume"] == {"allowed": True, "reason": None}
    assert diagnostic["dispatcher"]["status"] == "idle"
    assert diagnostic["dispatcher"]["freshness"] == "fresh"
    status, resumed, _ = _admin_request("POST", "/projects/identity-project/resume", session_cookie=cookie, csrf_token=csrf)
    assert status == 200, resumed
    status, runs, _ = _admin_request("GET", "/projects/identity-project/task-runs", session_cookie=cookie)
    assert status == 200
    assert runs[0]["runtime_context"]["dispatcher_id"] == historical_owner
    assert runs[0]["runtime_context"]["execution_lifecycle"] == context["execution_lifecycle"]


@pytest.mark.parametrize("dispatcher_status,capabilities,timestamp,reason", [
    (None, ["project_pause_v1"], None, "project_dispatcher_unavailable"),
    ("starting", ["project_pause_v1"], None, "project_dispatcher_unavailable"),
    ("error", ["project_pause_v1"], None, "project_dispatcher_unavailable"),
    ("stopping", ["project_pause_v1"], None, "project_dispatcher_unavailable"),
    ("idle", [], None, "project_pause_unsupported"),
    ("idle", ["project_pause_v1"], "2000-01-01T00:00:00Z", "project_dispatcher_unavailable"),
    ("idle", ["project_pause_v1"], "2099-01-01T00:00:00Z", "project_dispatcher_unavailable"),
    ("idle", ["project_pause_v1"], "", "project_dispatcher_unavailable"),
])
def test_resume_separately_requires_current_ready_authenticated_dispatcher(monkeypatch, dispatcher_status, capabilities, timestamp, reason):
    cookie, csrf, _, token = _identity_project()
    status, paused, _ = _admin_request("POST", "/projects/identity-project/pause", session_cookie=cookie, csrf_token=csrf)
    assert status == 200, paused
    if dispatcher_status is not None:
        if timestamp is not None:
            monkeypatch.setattr("cairn.server.services.monitoring.dispatcher.utcnow", lambda: timestamp)
        status, heartbeat, _ = _admin_request("POST", "/dispatcher/heartbeat", authorization=f"Bearer {token}", payload={
            "dispatcher_id": "configured-id", "status": dispatcher_status,
            "runtime": {"runtime_capabilities": capabilities},
        })
        assert status == 200, heartbeat
    status, diagnostic, _ = _admin_request("GET", "/projects/identity-project/runtime", session_cookie=cookie)
    assert status == 200
    assert diagnostic["state"] == "paused"
    assert diagnostic["actions"]["resume"] == {"allowed": False, "reason": reason}
    status, denied, _ = _admin_request("POST", "/projects/identity-project/resume", session_cookie=cookie, csrf_token=csrf)
    assert (status, denied.get("detail")) == (409, reason)


def test_runtime_and_resume_choose_ready_not_latest_starting_and_capacity_is_waiting():
    from cairn.server.services.time_utils import utcnow

    cookie, csrf, _, token = _identity_project()
    _, starting_token = create_machine_principal("starting-machine", "starting-secret")
    status, paused, _ = _admin_request("POST", "/projects/identity-project/pause", session_cookie=cookie, csrf_token=csrf)
    assert status == 200, paused
    status, ready, _ = _admin_request("POST", "/dispatcher/heartbeat", authorization=f"Bearer {token}", payload={
        "dispatcher_id": "ready-config", "status": "running", "running_task_count": 1,
        "runtime": {"max_workers": 1, "runtime_capabilities": ["project_pause_v1", "project_runtime_v1"],
                    "project_decisions": [{"project_id": "identity-project", "execution_generation": 1,
                                           "code": "global_capacity", "observed_at": utcnow()}]},
    })
    assert status == 200, ready
    status, starting, _ = _admin_request("POST", "/dispatcher/heartbeat", authorization=f"Bearer {starting_token}", payload={
        "dispatcher_id": "starting-config", "status": "starting",
        "runtime": {"runtime_capabilities": ["project_pause_v1", "project_runtime_v1"]},
    })
    assert status == 200, starting
    status, diagnostic, _ = _admin_request("GET", "/projects/identity-project/runtime", session_cookie=cookie)
    assert status == 200
    assert diagnostic["actions"]["resume"] == {"allowed": True, "reason": None}
    assert diagnostic["dispatcher"]["status"] == "running"
    status, resumed, _ = _admin_request("POST", "/projects/identity-project/resume", session_cookie=cookie, csrf_token=csrf)
    assert status == 200, resumed
    status, diagnostic, _ = _admin_request("GET", "/projects/identity-project/runtime", session_cookie=cookie)
    assert status == 200
    assert (diagnostic["state"], diagnostic["code"]) == ("waiting", "global_capacity")


@pytest.mark.parametrize("status,generation,termination,cleanup,pause_requested,known_owner", [
    ("failed", 0, False, False, True, True),
    ("failed", 0, True, False, True, True),
    ("failed", 1, True, True, True, True),
    ("running", 0, True, True, True, True),
    ("failed", 0, True, False, True, False),
    ("failed", 0, True, False, False, True),
    ("failed", False, True, True, True, True),
])
def test_history_without_terminal_same_generation_dual_proof_still_drains(status, generation, termination, cleanup, pause_requested, known_owner):
    cookie, csrf, machine_id, token = _identity_project()
    context = {"dispatcher_id": machine_id if known_owner else None, "execution_lifecycle": {
        "generation": generation, "process_started": True,
        "termination_confirmed": termination, "cleanup_confirmed": cleanup,
    }}
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET status = 'paused', execution_generation = 1 WHERE id = 'identity-project'")
        conn.execute("INSERT INTO task_runs (id, project_id, task_type, worker_name, worker_type, status, "
                     "execution_generation, pause_requested, runtime_context_json) "
                     "VALUES ('historical-run', 'identity-project', 'bootstrap', 'fixture', 'pi', ?, 0, ?, ?)",
                     (status, int(pause_requested), json.dumps(context)))
    heartbeat_status, heartbeat, _ = _admin_request("POST", "/dispatcher/heartbeat", authorization=f"Bearer {token}", payload={
        "dispatcher_id": "different-config-id", "status": "idle",
        "runtime": {"runtime_capabilities": ["project_pause_v1", "project_runtime_v1"]},
    })
    assert heartbeat_status == 200, heartbeat
    code, diagnostic, _ = _admin_request("GET", "/projects/identity-project/runtime", session_cookie=cookie)
    assert code == 200
    assert diagnostic["actions"]["resume"] == {"allowed": False, "reason": "project_pause_draining"}
    code, denied, _ = _admin_request("POST", "/projects/identity-project/resume", session_cookie=cookie, csrf_token=csrf)
    assert (code, denied.get("detail")) == (409, "project_pause_draining")


def _post_user(
    *,
    session_cookie: str | None = None,
    csrf_token: str | None = None,
    authorization: str | None = None,
    payload: dict[str, object] | None = None,
) -> tuple[int, dict[str, object], str]:
    return _admin_request(
        "POST",
        "/admin/users",
        session_cookie=session_cookie,
        csrf_token=csrf_token,
        authorization=authorization,
        payload=payload or {"login": "new-user"},
    )


def _seed_two_tenants() -> None:
    with db.get_conn() as conn:
        conn.executemany(
            "INSERT INTO tenants (id, kind, name, created_at) VALUES (?, 'personal', ?, ?)",
            [
                ("tenant_a", "Tenant A", "2026-08-13T00:00:00Z"),
                ("tenant_b", "Tenant B", "2026-08-13T00:00:01Z"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO projects (id, title, status, created_at, tenant_id)
            VALUES (?, ?, 'active', ?, ?)
            """,
            [
                ("project_a", "Project A", "2026-08-13T00:01:00Z", "tenant_a"),
                ("project_b", "Project B", "2026-08-13T00:02:00Z", "tenant_b"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO task_runs
                (id, project_id, task_type, worker_name, worker_type, status, started_at,
                 error_type, error_message, runtime_context_json)
            VALUES (?, ?, 'explore', ?, 'mock', 'running', ?, NULL, ?, ?)
            """,
            [
                (
                    "run_a",
                    "project_a",
                    "tenant-a-worker",
                    "2026-08-13T00:03:00Z",
                    "tenant-a diagnostic",
                    '{"artifact_root":"/secret/tenant-a"}',
                ),
                (
                    "run_b",
                    "project_b",
                    "tenant-b-worker",
                    "2026-08-13T00:04:00Z",
                    "tenant-b diagnostic",
                    '{"artifact_root":"/secret/tenant-b"}',
                ),
            ],
        )
        conn.executemany(
            """
            INSERT INTO vulnerabilities
                (id, project_id, title, severity, confidence, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 0.5, 'new', ?, ?)
            """,
            [
                ("vuln_a", "project_a", "Vulnerability A", "high", "2026-08-13T00:05:00Z", "2026-08-13T00:05:00Z"),
                ("vuln_b", "project_b", "Vulnerability B", "low", "2026-08-13T00:06:00Z", "2026-08-13T00:06:00Z"),
            ],
        )
        conn.execute(
            """
            INSERT INTO dispatcher_status
                (id, status, host, config_path, runtime_json, error_message, updated_at)
            VALUES ('dispatcher-1', 'error', 'internal-host', '/secret/config.yaml',
                    '{"credential":"secret"}', 'global diagnostic', '2026-08-13T00:07:00Z')
            """
        )


def test_post_admin_users_denies_anonymous_regular_user_and_machine() -> None:
    create_user_with_personal_tenant("regular", "regular-password")
    regular_cookie, regular_csrf = _session_credentials("regular", "regular-password")
    _, machine_token = create_machine_principal("admin-denied-machine", "machine-secret")

    with db.get_conn() as conn:
        audit_before = conn.execute(
            "SELECT COUNT(*) AS count FROM security_audit_events WHERE action = 'admin.access.denied'"
        ).fetchone()["count"]

    anonymous_status, _, _ = _post_user()
    regular_status, _, _ = _post_user(
        session_cookie=regular_cookie,
        csrf_token=regular_csrf,
        payload={"login": "must-not-exist"},
    )
    machine_status, _, _ = _post_user(authorization=f"Bearer {machine_token}")

    assert anonymous_status == 401
    assert regular_status == 403
    assert machine_status == 401
    with db.get_conn() as conn:
        denied = conn.execute(
            """
            SELECT actor_type, actor_id, tenant_id, action, resource_type, resource_id,
                   outcome, metadata_json
            FROM security_audit_events
            WHERE action = 'admin.access.denied'
            ORDER BY created_at DESC, id DESC
            """
        ).fetchall()
        target = conn.execute("SELECT 1 FROM users WHERE login = 'must-not-exist'").fetchone()
    assert len(denied) == audit_before + 1
    event = dict(denied[0])
    assert event["actor_type"] == "user"
    assert event["action"] == "admin.access.denied"
    assert event["resource_type"] == "route"
    assert event["resource_id"] == "/admin/users"
    assert event["outcome"] == "denied"
    assert json.loads(event["metadata_json"]) == {"method": "POST"}
    assert "known-password" not in event["metadata_json"]
    assert target is None


def test_post_admin_users_creates_hashed_user_personal_membership_and_safe_audit() -> None:
    admin_id, _ = create_user_with_personal_tenant(
        "root-admin",
        "admin-password",
        role="super_admin",
    )
    session_cookie, csrf_token = _session_credentials("root-admin", "admin-password")
    status_code, payload, response_text = _post_user(
        session_cookie=session_cookie,
        csrf_token=csrf_token,
        payload={"login": "  New.User  "},
    )

    assert status_code == 201
    assert set(payload) == {
        "id", "login", "role", "disabled", "must_change_password", "tenant_id",
        "active_session_count", "created_at", "updated_at"
    }
    assert payload["login"] == "new.user"
    assert payload["role"] == "user"
    assert payload["must_change_password"] is True
    assert "password_hash" not in response_text
    assert DEFAULT_PROVISIONED_PASSWORD not in response_text

    with db.get_conn() as conn:
        user = conn.execute(
            """
            SELECT id, login, password_hash, role, disabled, must_change_password
            FROM users WHERE id = ?
            """,
            (payload["id"],),
        ).fetchone()
        memberships = conn.execute(
            "SELECT tenant_id, role FROM tenant_memberships WHERE user_id = ?",
            (payload["id"],),
        ).fetchall()
        tenant = conn.execute(
            "SELECT id, kind, name FROM tenants WHERE id = ?",
            (payload["tenant_id"],),
        ).fetchone()
        audit_rows = conn.execute(
            """
            SELECT actor_type, actor_id, tenant_id, action, resource_type, resource_id,
                   outcome, metadata_json
            FROM security_audit_events
            WHERE action = 'admin.users.create' AND resource_id = ?
            """,
            (payload["id"],),
        ).fetchall()
        persisted = "\n".join(
            str(dict(row))
            for table in ("users", "tenants", "tenant_memberships", "security_audit_events")
            for row in conn.execute(f"SELECT * FROM {table}").fetchall()
        )

    assert user is not None
    assert user["password_hash"].startswith("$argon2")
    assert user["password_hash"] != DEFAULT_PROVISIONED_PASSWORD
    assert verify_password(user["password_hash"], DEFAULT_PROVISIONED_PASSWORD)
    assert user["must_change_password"] == 1
    assert len(memberships) == 1
    assert dict(memberships[0]) == {"tenant_id": payload["tenant_id"], "role": "owner"}
    assert dict(tenant) == {"id": payload["tenant_id"], "kind": "personal", "name": "new.user"}
    assert len(audit_rows) == 1
    assert dict(audit_rows[0]) == {
        "actor_type": "user",
        "actor_id": admin_id,
        "tenant_id": payload["tenant_id"],
        "action": "admin.users.create",
        "resource_type": "user",
        "resource_id": payload["id"],
        "outcome": "success",
        "metadata_json": '{"login": "new.user"}',
    }
    assert DEFAULT_PROVISIONED_PASSWORD not in persisted


def test_post_admin_users_duplicate_and_invalid_input_are_safe_and_audited() -> None:
    admin_id, _ = create_user_with_personal_tenant(
        "root-admin",
        "admin-password",
        role="super_admin",
    )
    create_user_with_personal_tenant("duplicate", "existing-secret")
    session_cookie, csrf_token = _session_credentials("root-admin", "admin-password")

    duplicate_status, duplicate_payload, duplicate_text = _post_user(
        session_cookie=session_cookie,
        csrf_token=csrf_token,
        payload={"login": " DUPLICATE "},
    )
    blank_status, blank_payload, blank_text = _post_user(
        session_cookie=session_cookie,
        csrf_token=csrf_token,
        payload={"login": "   "},
    )

    assert duplicate_status == 409
    assert duplicate_payload == {"detail": "User already exists"}
    assert blank_status == 422
    assert blank_payload == {"detail": "Invalid user input"}
    response_text = duplicate_text + blank_text
    assert DEFAULT_PROVISIONED_PASSWORD not in response_text

    with db.get_conn() as conn:
        failures = conn.execute(
            """
            SELECT actor_type, actor_id, action, resource_id, outcome, metadata_json
            FROM security_audit_events
            WHERE action = 'admin.users.create' AND outcome = 'failure'
            ORDER BY created_at, id
            """
        ).fetchall()
        persisted = "\n".join(
            str(dict(row))
            for row in conn.execute("SELECT * FROM security_audit_events").fetchall()
        )

    assert len(failures) == 2
    assert all(row["actor_type"] == "user" and row["actor_id"] == admin_id for row in failures)
    assert all(row["resource_id"] is None for row in failures)
    assert sorted(row["metadata_json"] for row in failures) == [
        '{"login": "duplicate", "reason": "duplicate"}',
        '{"reason": "invalid_input"}',
    ]
    assert DEFAULT_PROVISIONED_PASSWORD not in persisted


def test_tenant_monitoring_isolates_two_tenants_and_hides_diagnostics() -> None:
    _seed_two_tenants()

    summary = monitoring.get_monitoring_summary(
        long_running_seconds=0,
        principal=RequestPrincipal("user", "user_a", "tenant_a", "user"),
    )
    payload = summary.model_dump()

    assert summary.project_count == 1
    assert [item.project_id for item in summary.running_tasks] == ["project_a"]
    assert [item.worker_name for item in summary.running_tasks] == ["tenant-a-worker"]
    assert summary.vulnerabilities.total == 1
    assert summary.vulnerabilities.per_project == [
        {"project_id": "project_a", "title": "Project A", "count": 1}
    ]
    assert set(payload["dispatcher"]) == {"status", "stale", "updated_at"}
    assert "internal-host" not in str(payload)
    assert "/secret/config.yaml" not in str(payload)
    assert "global diagnostic" not in str(payload)
    assert "tenant-a diagnostic" not in str(payload)
    assert "/secret/tenant-a" not in str(payload)
    assert "tenant-b-worker" not in str(payload)
    assert "project_b" not in str(payload)


def test_global_vulnerability_route_expands_scope_only_for_super_admin() -> None:
    _seed_two_tenants()

    tenant_results = vulnerabilities.get_all_vulnerabilities_route(
        severity=None,
        status=None,
        principal=RequestPrincipal("user", "user_a", "tenant_a", "user"),
    )
    admin_results = vulnerabilities.get_all_vulnerabilities_route(
        severity=None,
        status=None,
        principal=RequestPrincipal("user", "admin", "tenant_a", "super_admin"),
    )
    filtered_admin_results = vulnerabilities.get_all_vulnerabilities_route(
        severity="low",
        status="new",
        principal=RequestPrincipal("user", "admin", "tenant_a", "super_admin"),
    )

    assert [(item.project_id, item.id) for item in tenant_results] == [
        ("project_a", "vuln_a")
    ]
    assert {(item.project_id, item.id) for item in admin_results} == {
        ("project_a", "vuln_a"),
        ("project_b", "vuln_b"),
    }
    assert [(item.project_id, item.id) for item in filtered_admin_results] == [
        ("project_b", "vuln_b")
    ]
    assert filtered_admin_results[0].project_title == "Project B"
    with db.get_conn() as conn:
        audit_events = conn.execute(
            """
            SELECT actor_id, tenant_id, resource_type, outcome
            FROM security_audit_events
            WHERE action = 'admin.vulnerabilities.read'
            ORDER BY created_at, id
            """
        ).fetchall()
    assert [dict(event) for event in audit_events] == [
        {
            "actor_id": "admin",
            "tenant_id": "tenant_a",
            "resource_type": "admin_read_model",
            "outcome": "success",
        },
        {
            "actor_id": "admin",
            "tenant_id": "tenant_a",
            "resource_type": "admin_read_model",
            "outcome": "success",
        },
    ]


def test_project_monitoring_resolves_target_project_tenant() -> None:
    _seed_two_tenants()

    summary = monitoring.get_project_monitoring_summary(
        "project_b",
        long_running_seconds=0,
    )

    assert summary.project_count == 1
    assert [item.project_id for item in summary.running_tasks] == ["project_b"]
    assert [item.worker_name for item in summary.running_tasks] == ["tenant-b-worker"]
    assert "project_a" not in summary.model_dump_json()


def test_admin_monitoring_keeps_global_dispatcher_diagnostics() -> None:
    _seed_two_tenants()

    summary = admin.get_admin_monitoring(
        long_running_seconds=0,
        profile="dispatcher",
        principal=RequestPrincipal("user", "admin", "tenant_a", "super_admin"),
    )
    payload = summary.model_dump()

    assert payload["dispatcher"]["host"] == "internal-host"
    assert payload["dispatcher"]["config_path"] == "/secret/config.yaml"
    assert payload["dispatcher"]["last_error_message"] == "global diagnostic"
    assert "tenant_a" not in payload["dispatcher"]


def test_admin_overview_monitoring_aggregates_all_tenants() -> None:
    _seed_two_tenants()

    summary = admin.get_admin_monitoring(
        long_running_seconds=0,
        profile="overview",
        principal=RequestPrincipal("user", "admin", "tenant_a", "super_admin"),
    )

    assert summary.vulnerabilities is not None
    assert summary.vulnerabilities.total == 2
    assert summary.vulnerabilities.by_severity == {"high": 1, "low": 1}
    assert {item["project_id"] for item in summary.vulnerabilities.per_project} == {
        "project_a",
        "project_b",
    }
    assert {item.project_id for item in summary.running_tasks} == {
        "project_a",
        "project_b",
    }


def test_admin_read_models_are_global_fixed_and_audit_is_resanitized() -> None:
    _seed_two_tenants()
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO security_audit_events
                (id, actor_type, actor_id, tenant_id, action, resource_type, resource_id,
                 outcome, metadata_json, created_at)
            VALUES ('audit-legacy', 'system', NULL, NULL, 'legacy.import', NULL, NULL,
                    'success', '{"Authorization":"Bearer leaked-secret","safe":"ok"}',
                    '2026-08-13T00:08:00Z')
            """
        )

    principal = RequestPrincipal("user", "admin", "tenant_a", "super_admin")
    projects = admin.list_admin_projects(principal)
    vulnerabilities = admin.list_admin_vulnerabilities(principal)
    audit_events = admin.list_admin_security_audit(principal=principal, limit=100)

    assert {item.id for item in projects} >= {"project_a", "project_b"}
    assert {item.id for item in vulnerabilities} == {"vuln_a", "vuln_b"}
    assert all("preferred_workers_json" not in item.model_dump() for item in projects)
    event = next(item for item in audit_events if item.id == "audit-legacy")
    assert event.metadata == {"Authorization": "[REDACTED]", "safe": "ok"}
    assert "leaked-secret" not in event.model_dump_json()


def test_admin_lists_use_stable_bounded_pagination_and_fixed_fields() -> None:
    principal = RequestPrincipal("user", "admin", "tenant_a", "super_admin")
    with db.get_conn() as conn:
        conn.executemany(
            "INSERT INTO tenants (id, kind, name, created_at) VALUES (?, 'personal', ?, ?)",
            [
                ("tenant_c", "Tenant C", "2026-08-13T00:00:00Z"),
                ("tenant_b", "Tenant B", "2026-08-13T00:00:00Z"),
                ("tenant_a", "Tenant A", "2026-08-13T00:00:00Z"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO users (id, login, password_hash, role, created_at, updated_at)
            VALUES (?, ?, ?, 'user', ?, ?)
            """,
            [
                ("user_c", "c", "hash-c", "2026-08-13T00:01:00Z", "2026-08-13T00:01:00Z"),
                ("user_b", "b", "hash-b", "2026-08-13T00:01:00Z", "2026-08-13T00:01:00Z"),
                ("user_a", "a", "hash-a", "2026-08-13T00:01:00Z", "2026-08-13T00:01:00Z"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO tenant_memberships (tenant_id, user_id, role, created_at)
            VALUES (?, ?, 'owner', '2026-08-13T00:01:00Z')
            """,
            [("tenant_c", "user_c"), ("tenant_b", "user_b"), ("tenant_a", "user_a")],
        )
        conn.executemany(
            """
            INSERT INTO projects (id, title, status, created_at, tenant_id)
            VALUES (?, ?, 'active', '2026-08-13T00:02:00Z', ?)
            """,
            [
                ("project_c", "Project C", "tenant_c"),
                ("project_b", "Project B", "tenant_b"),
                ("project_a", "Project A", "tenant_a"),
            ],
        )

    users = admin.list_admin_users(principal, limit=1, offset=1)
    tenants = admin.list_admin_tenants(principal, limit=1, offset=1)
    projects = admin.list_admin_projects(principal, limit=1, offset=1)

    assert [item.id for item in users] == ["user_b"]
    assert [item.id for item in tenants] == ["tenant_b"]
    assert [item.id for item in projects] == ["project_b"]
    assert "password_hash" not in users[0].model_dump()
    assert set(users[0].model_dump()) == {
        "id", "login", "role", "disabled", "must_change_password", "tenant_id",
        "active_session_count", "created_at", "updated_at"
    }


def test_admin_route_query_bounds_are_fixed() -> None:
    route_queries = {
        route.path: {
            parameter.name: (parameter.field_info.metadata, parameter.default)
            for parameter in route.dependant.query_params
        }
        for route in iter_app_routes(app)
        if hasattr(route, "dependant") and route.path.startswith("/admin/")
    }

    for path in ("/admin/users", "/admin/tenants", "/admin/projects", "/admin/vulnerabilities", "/admin/audit"):
        assert set(route_queries[path]) >= {"limit", "offset"}
        limit_metadata, limit_default = route_queries[path]["limit"]
        offset_metadata, offset_default = route_queries[path]["offset"]
        assert limit_default == 100
        assert offset_default == 0
        assert any(getattr(item, "ge", None) == 1 for item in limit_metadata)
        assert any(getattr(item, "le", None) == 500 for item in limit_metadata)
        assert any(getattr(item, "ge", None) == 0 for item in offset_metadata)
        assert any(getattr(item, "le", None) == 1_000_000 for item in offset_metadata)


def test_route_policy_separates_tenant_admin_and_dispatcher_monitoring() -> None:
    route_dependencies = {
        (route.path, method): {dependency.call for dependency in route.dependant.dependencies}
        for route in iter_app_routes(app)
        if hasattr(route, "dependant") and getattr(route, "methods", None)
        for method in route.methods
    }
    from cairn.server.auth import protect_cookie_request

    assert protect_cookie_request in route_dependencies[("/monitoring/summary", "GET")]
    assert require_super_admin in route_dependencies[("/admin/users", "GET")]
    assert require_super_admin in route_dependencies[("/admin/users", "POST")]
    assert require_super_admin in route_dependencies[("/admin/tenants", "GET")]
    assert require_super_admin in route_dependencies[("/admin/projects", "GET")]
    assert require_super_admin in route_dependencies[("/admin/vulnerabilities", "GET")]
    assert require_super_admin in route_dependencies[("/admin/monitoring", "GET")]
    assert require_super_admin in route_dependencies[("/admin/audit", "GET")]
    assert require_dispatcher in route_dependencies[("/dispatcher/monitoring/summary", "GET")]


def test_admin_user_lifecycle_disables_reenables_and_revokes_sessions() -> None:
    admin_id, _ = create_user_with_personal_tenant(
        "root-admin", "admin-password", role="super_admin"
    )
    target_id, _ = create_user_with_personal_tenant("target", "target-password")
    admin_cookie, admin_csrf = _session_credentials("root-admin", "admin-password")
    target_cookie, _ = _session_credentials("target", "target-password")

    users = admin.list_admin_users(
        RequestPrincipal("user", admin_id, role="super_admin")
    )
    target = next(item for item in users if item.id == target_id)
    assert target.active_session_count == 1

    status, disabled, _ = _admin_request(
        "PATCH",
        f"/admin/users/{target_id}/status",
        session_cookie=admin_cookie,
        csrf_token=admin_csrf,
        payload={"disabled": True, "expected_updated_at": target.updated_at},
    )
    assert status == 200
    assert disabled["disabled"] is True
    assert disabled["active_session_count"] == 0
    from cairn.server.auth import resolve_session
    assert resolve_session(target_cookie) is None

    status, enabled, _ = _admin_request(
        "PATCH",
        f"/admin/users/{target_id}/status",
        session_cookie=admin_cookie,
        csrf_token=admin_csrf,
        payload={"disabled": False, "expected_updated_at": disabled["updated_at"]},
    )
    assert status == 200
    assert enabled["disabled"] is False
    _session_credentials("target", "target-password")

    stale_status, stale_payload, _ = _admin_request(
        "PATCH",
        f"/admin/users/{target_id}/status",
        session_cookie=admin_cookie,
        csrf_token=admin_csrf,
        payload={"disabled": True, "expected_updated_at": target.updated_at},
    )
    assert stale_status == 409
    assert stale_payload == {"detail": "User update conflict"}


def test_admin_password_reset_and_explicit_revoke_are_write_only_and_immediate() -> None:
    admin_id, _ = create_user_with_personal_tenant(
        "root-admin", "admin-password", role="super_admin"
    )
    target_id, _ = create_user_with_personal_tenant(
        "target", "old-password", must_change_password=True
    )
    admin_cookie, admin_csrf = _session_credentials("root-admin", "admin-password")
    old_cookie, _ = _session_credentials("target", "old-password")
    principal = RequestPrincipal("user", admin_id, role="super_admin")
    target = next(item for item in admin.list_admin_users(principal) if item.id == target_id)
    new_password = "replacement-secret"

    status, reset, raw = _admin_request(
        "POST",
        f"/admin/users/{target_id}/password-reset",
        session_cookie=admin_cookie,
        csrf_token=admin_csrf,
        payload={"password": new_password, "expected_updated_at": target.updated_at},
    )
    assert status == 200
    assert set(reset) == {
        "id", "login", "role", "disabled", "must_change_password", "tenant_id",
        "active_session_count", "created_at", "updated_at",
    }
    assert reset["must_change_password"] is False
    assert new_password not in raw
    from cairn.server.auth import resolve_session
    assert resolve_session(old_cookie) is None
    with pytest.raises(HTTPException):
        _session_credentials("target", "old-password")
    new_cookie, _ = _session_credentials("target", new_password)

    status, revoked, _ = _admin_request(
        "POST",
        f"/admin/users/{target_id}/sessions/revoke",
        session_cookie=admin_cookie,
        csrf_token=admin_csrf,
        payload={},
    )
    assert status == 200
    assert revoked["active_session_count"] == 0
    assert resolve_session(new_cookie) is None

    with db.get_conn() as conn:
        audit = "\n".join(
            row["metadata_json"]
            for row in conn.execute(
                "SELECT metadata_json FROM security_audit_events WHERE action LIKE 'admin.users.%'"
            ).fetchall()
        )
        password_hash = conn.execute(
            "SELECT password_hash FROM users WHERE id = ?", (target_id,)
        ).fetchone()["password_hash"]
    assert verify_password(password_hash, new_password)
    assert new_password not in audit


def test_admin_self_password_reset_deletes_cookie_and_revokes_current_session() -> None:
    admin_id, _ = create_user_with_personal_tenant(
        "root-admin", "admin-password", role="super_admin"
    )
    admin_cookie, _ = _session_credentials("root-admin", "admin-password")
    principal = RequestPrincipal("user", admin_id, role="super_admin")
    current = next(item for item in admin.list_admin_users(principal) if item.id == admin_id)
    response = Response()

    result = admin.reset_admin_user_password(
        admin_id,
        AdminUserPasswordResetRequest(
            password="new-admin-secret",
            expected_updated_at=current.updated_at,
        ),
        response,
        principal,
    )

    assert result.must_change_password is False
    assert "cairn_session=\"\"" in response.headers["set-cookie"]
    from cairn.server.auth import resolve_session
    assert resolve_session(admin_cookie) is None


def test_admin_user_lifecycle_denies_self_last_admin_missing_and_invalid_requests() -> None:
    admin_id, _ = create_user_with_personal_tenant(
        "root-admin", "admin-password", role="super_admin"
    )
    admin_cookie, admin_csrf = _session_credentials("root-admin", "admin-password")
    principal = RequestPrincipal("user", admin_id, role="super_admin")
    current = next(item for item in admin.list_admin_users(principal) if item.id == admin_id)

    operations = [
        ("PATCH", f"/admin/users/{admin_id}/status", {"disabled": True, "expected_updated_at": current.updated_at}),
        ("POST", f"/admin/users/{admin_id}/sessions/revoke", {}),
    ]
    for method, path, payload in operations:
        status, response, _ = _admin_request(
            method, path, session_cookie=admin_cookie, csrf_token=admin_csrf, payload=payload
        )
        assert status == 409
        assert response == {"detail": "Self lifecycle operation is not allowed"}

    missing_status, _, _ = _admin_request(
        "POST",
        "/admin/users/missing/sessions/revoke",
        session_cookie=admin_cookie,
        csrf_token=admin_csrf,
        payload={},
    )
    invalid_status, invalid_response, _ = _admin_request(
        "POST",
        "/admin/users/missing/password-reset",
        session_cookie=admin_cookie,
        csrf_token=admin_csrf,
        payload={"password": "   ", "expected_updated_at": ""},
    )
    assert missing_status == 404
    assert invalid_status == 422
    assert invalid_response["detail"]

    reset_status, reset_payload, _ = _admin_request(
        "POST",
        f"/admin/users/{admin_id}/password-reset",
        session_cookie=admin_cookie,
        csrf_token=admin_csrf,
        payload={"password": "new-admin-secret", "expected_updated_at": current.updated_at},
    )
    assert reset_status == 200
    assert reset_payload["must_change_password"] is False
    from cairn.server.auth import resolve_session
    assert resolve_session(admin_cookie) is None

    with db.get_conn() as conn:
        row = conn.execute("SELECT disabled FROM users WHERE id = ?", (admin_id,)).fetchone()
        failures = conn.execute(
            "SELECT outcome, metadata_json FROM security_audit_events WHERE action LIKE 'admin.users.%' AND outcome = 'failure'"
        ).fetchall()
    assert row["disabled"] == 0
    assert len(failures) >= 4
    assert all("password" not in item["metadata_json"].casefold() for item in failures)


def test_tenant_monitoring_rejects_session_without_tenant() -> None:
    with pytest.raises(HTTPException) as exc:
        monitoring.get_monitoring_summary(
            long_running_seconds=1800,
            principal=RequestPrincipal("user", "orphan", None, "user"),
        )
    assert exc.value.status_code == 403
