"""暂停必须由实际执行与精确资源清理确认，不把 future/DB 终态当证据。"""
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace
import threading
from unittest.mock import Mock

import pytest

from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.process import ManagedProcess
from cairn.dispatcher.tasks.process_runner import run_worker_process
from test_browser_process_runner import ProcessManager, DockerAPI, docker_container


def flush_finishes(loop):
    with ThreadPoolExecutor(max_workers=1) as executor:
        loop.cleanup_executor = executor
        loop._flush_pending_task_run_finishes()
    loop._flush_pending_task_run_finishes()


def test_exec_start_confirmation_is_not_reader_thread_start():
    entered, release = threading.Event(), threading.Event()

    class SlowAPI(DockerAPI):
        def exec_start(self, exec_id, **kwargs):
            entered.set()
            assert release.wait(5)
            return super().exec_start(exec_id, **kwargs)

    process = ManagedProcess(docker_container(SlowAPI()), ["fixture"], {})
    started = Mock()
    process.on_execution_started = started
    process.start()
    try:
        assert entered.wait(5)
        assert process.execution_started is False
        started.assert_not_called()
    finally:
        release.set()
        process.communicate(timeout=5).cleanup()
    assert process.execution_started is True
    started.assert_called_once_with()


def test_all_phases_must_confirm_termination_and_late_completion_is_not_reinterpreted():
    cancellation = TaskCancellation(generation=4)
    manager = ProcessManager()
    worker = SimpleNamespace(name="fixture", type="pi", env={})
    with run_worker_process(manager, "worker", worker, ["fixture"], phase="execute",
                            timeout_seconds=1, cancellation=cancellation):
        pass
    assert cancellation.execution_lifecycle(task_finished=False) == {
        "generation": 4, "process_started": True,
        "termination_confirmed": False, "cleanup_confirmed": False,
    }
    assert cancellation.execution_lifecycle(task_finished=True)["termination_confirmed"] is True

    uncertain = SimpleNamespace(execution_started=True, execution_completed=False)
    cancellation.register_execution(uncertain)
    cancellation.finish_execution(uncertain)
    uncertain.execution_completed = True
    assert cancellation.execution_lifecycle(task_finished=True)["termination_confirmed"] is False


def test_execution_first_finish_freezes_identity_start_and_proof() -> None:
    cancellation = TaskCancellation(generation=5)
    process = SimpleNamespace(
        execution_identity=("first-container", "exec-1"),
        execution_started=False,
        execution_completed=False,
    )
    cancellation.register_execution(process)
    cancellation.finish_execution(process)

    process.execution_identity = ("late-container", "exec-2")
    process.execution_started = True
    process.execution_completed = True
    cancellation.observe_execution_start(process)
    cancellation.finish_execution(process)
    cancellation.register_execution(process)

    assert cancellation.execution_container_ids() == ["first-container"]
    assert cancellation.execution_lifecycle(task_finished=True) == {
        "generation": 5,
        "process_started": False,
        "termination_confirmed": False,
        "cleanup_confirmed": False,
    }


def test_execution_property_error_at_first_finish_cannot_be_upgraded() -> None:
    class FaultyProcess:
        execution_identity = ("exact-container", "exec-1")
        execution_started = True
        _completed = False
        _raise_completed = True

        @property
        def execution_completed(self):
            if self._raise_completed:
                raise RuntimeError("late reader state unavailable")
            return self._completed

        @execution_completed.setter
        def execution_completed(self, value):
            self._raise_completed = False
            self._completed = value

    cancellation = TaskCancellation(generation=6)
    process = FaultyProcess()
    cancellation.register_execution(process)
    cancellation.finish_execution(process)
    process.execution_completed = True
    cancellation.finish_execution(process)

    assert cancellation.execution_container_ids() == ["exact-container"]
    assert cancellation.execution_lifecycle(task_finished=True)["termination_confirmed"] is False


@pytest.mark.parametrize("completed", [False, True])
def test_future_terminal_only_forwards_actual_execution_proof(completed):
    from cairn.dispatcher.models import RunningTask
    from cairn.dispatcher.protocol.client import ApiResult
    from test_dispatcher_multi_project_capacity import _loop

    loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
    loop._finish_runtime_context = lambda *_args: {}
    loop.client.update_task_run = Mock(return_value=ApiResult(status_code=200))
    cancellation = TaskCancellation(generation=9)
    process = SimpleNamespace(execution_started=True, execution_completed=completed, execution_identity=("actual-container", "exec-1"))
    cancellation.register_execution(process)
    cancellation.finish_execution(process)
    cancellation.cancel("paused")
    future = Future()
    future.set_result("cancelled")
    loop.futures[future] = RunningTask("p1", "explore", "w", "pi", cancellation, None, task_run_id="r1")

    loop._reap_futures()
    flush_finishes(loop)

    update = loop.client.update_task_run.call_args.kwargs
    assert update["status"] == "cancelled"
    assert update["runtime_context"]["execution_container_ids"] == ["actual-container"]
    assert update["runtime_context"]["execution_lifecycle"] == {
        "generation": 9, "process_started": True,
        "termination_confirmed": completed, "cleanup_confirmed": False,
    }


from test_pi_container_ownership import runtime, owned_container, FakeContainer


def test_pause_stops_exact_container_and_reuses_preserved_workspace(runtime):
    manager, docker = runtime
    owned = owned_container(manager, docker, project="p1")
    owned.workspace_marker = "unfinished-material"
    other = owned_container(manager, docker, project="p2")
    legacy = FakeContainer(docker, "cairn-dispatch-p1", {})

    assert manager.cleanup_paused("p1", container_ids=[owned.id]) is True
    assert manager.cleanup_paused("p1", container_ids=[owned.id]) is True
    assert owned.actions == ["stop"]
    assert owned.status == "exited"
    assert other.actions == legacy.actions == []
    assert owned.id in docker.lookups
    assert manager.ensure_running("p1") == owned.name
    assert owned.actions == ["stop", "start"]
    assert owned.workspace_marker == "unfinished-material"
    assert docker.runs == []


def test_pause_cleanup_never_confirms_a_failed_stop(runtime, monkeypatch):
    manager, docker = runtime
    owned = owned_container(manager, docker, project="p1")
    monkeypatch.setattr(owned, "stop", lambda **_kw: None)
    assert manager.cleanup_paused("p1", container_ids=[owned.id]) is False
    assert owned.status == "running"


def test_two_manager_instances_serialize_activation_and_pause(runtime):
    from test_pi_container_ownership import ContainerManager

    manager, docker = runtime
    another = ContainerManager(manager._config, instance_id=manager.instance_id)
    registered, release = threading.Event(), threading.Event()
    claims = []
    pause_started, claim_started = threading.Event(), threading.Event()

    def register(_daemon):
        registered.set()
        assert release.wait(5)
        return "activation-token"

    def claim(current_id, _daemon):
        claim_started.set()
        claims.append(current_id)
        return "stop-token", current_id

    def pause():
        pause_started.set()
        return another.pause_with_confirmation(
            "p1", container_ids=[], claim=claim,
            confirm=lambda _token, _id, _daemon: True,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        activation = executor.submit(
            manager.activate_for_readiness, "p1", manager.default_runtime_options(),
            register=register, finish=lambda _token, _id: None,
        )
        assert registered.wait(5)
        paused = executor.submit(pause)
        try:
            assert pause_started.wait(5)
            assert not claim_started.wait(0.1)
        finally:
            release.set()
        activation.result(timeout=5)
        assert paused.result(timeout=5) is True
    assert claims == [docker.objects[0].id]
    assert docker.objects[0].actions == ["stop"]


def test_daemon_change_during_stop_never_confirms(runtime, monkeypatch):
    manager, docker = runtime
    owned = owned_container(manager, docker, project="p1")
    confirmations = Mock()
    original_stop = owned.stop

    def stop_and_switch(**kwargs):
        original_stop(**kwargs)
        docker.daemon_id = "different-daemon"

    monkeypatch.setattr(owned, "stop", stop_and_switch)
    assert manager.pause_with_confirmation(
        "p1", container_ids=[owned.id],
        claim=lambda _current, _daemon: ("stop-token", owned.id),
        confirm=confirmations,
    ) is False
    assert owned.status == "exited"
    confirmations.assert_not_called()


def test_no_run_pause_stops_original_container_before_server_confirmation(runtime):
    from cairn.dispatcher.protocol.client import ApiResult
    from test_dispatcher_multi_project_capacity import _loop, _summary

    manager, docker = runtime
    owned = owned_container(manager, docker, project="p1")
    owned.workspace_marker = "preserved"
    loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
    loop.container_manager = manager
    loop.dispatcher_id = "test-dispatcher"
    loop.cleanup_futures = {}
    paused = _summary("p1").model_copy(update={"status": "paused", "execution_generation": 1})
    loop.client.list_task_runs = Mock(return_value=ApiResult(status_code=200, data=[]))
    loop.client.get_project = Mock(return_value=SimpleNamespace(project=paused))
    loop.client.get_container_activation = Mock(return_value=ApiResult(status_code=200, data={
        "activation": {"state": "running", "container_id": owned.id, "docker_daemon_id": docker.daemon_id},
    }))
    loop.client.claim_container_pause = Mock(return_value=ApiResult(status_code=200, data={
        "state": "stopping", "stop_token": "stop-token", "container_id": owned.id,
    }))
    confirmations = []

    def confirm(_project, _generation, _token, _id, _daemon):
        confirmations.append(owned.status)
        return ApiResult(status_code=200, data={"state": "stopped"})

    loop.client.confirm_container_pause = Mock(side_effect=confirm)
    loop.cleanup_executor = SimpleNamespace(submit=lambda fn, *args: _finished_future(fn, *args))
    loop._cleanup_paused_containers([paused])
    loop._reap_cleanup_futures()

    assert confirmations == ["exited"]
    assert owned.actions == ["stop"]
    assert owned.workspace_marker == "preserved"
    assert docker.runs == []
    assert loop._inactive_cleanup_done["p1"] == ("paused", 1)
    loop.client.get_project.return_value = SimpleNamespace(project=paused.model_copy(update={"status": "active"}))
    owned.start()
    assert loop._run_paused_container_cleanup("p1", 1, []) is False
    assert owned.actions == ["stop", "start"]


def test_paused_candidate_fence_allows_exact_stop_but_defers_run_ack(runtime):
    from cairn.dispatcher.protocol.client import ApiResult
    from test_dispatcher_multi_project_capacity import _loop, _summary

    manager, docker = runtime
    owned = owned_container(manager, docker, project="p1")
    owned.workspace_marker = "unfinished-material"
    loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
    loop.container_manager = manager
    loop.dispatcher_id = "test-dispatcher"
    loop.cleanup_futures = {}
    paused = _summary("p1").model_copy(update={"status": "paused", "execution_generation": 1})
    run = {
        "id": "old-run", "status": "cancelled", "execution_generation": 0,
        "runtime_context": {
            "dispatcher_id": "test-dispatcher", "container_name": owned.name,
            "execution_container_ids": [owned.id],
            "execution_lifecycle": {
                "generation": 0, "process_started": True,
                "termination_confirmed": False, "cleanup_confirmed": False,
            },
        },
    }
    loop.client.list_task_runs = Mock(return_value=ApiResult(status_code=200, data=[run]))
    loop.client.get_project = Mock(return_value=SimpleNamespace(project=paused))
    loop.client.get_container_activation = Mock(return_value=ApiResult(status_code=200, data={
        "activation": {"state": "running", "container_id": owned.id, "docker_daemon_id": docker.daemon_id},
    }))
    loop.client.claim_container_pause = Mock(side_effect=[
        ApiResult(status_code=200, data={"state": "stopping", "stop_token": "stop-token", "container_id": owned.id}),
        ApiResult(status_code=200, data={"state": "stopped", "container_id": owned.id}),
    ])
    loop.client.confirm_container_pause = Mock(return_value=ApiResult(status_code=200, data={"state": "stopped"}))
    loop.client.update_task_run = Mock(side_effect=[
        ApiResult(status_code=503), ApiResult(status_code=200),
    ])
    loop.cleanup_executor = SimpleNamespace(submit=lambda fn, *args: _finished_future(fn, *args))
    loop._candidate_fenced_containers = {owned.name}
    loop._candidate_fenced_task_runs = {("p1", "old-run")}

    loop._cleanup_paused_containers([paused])
    loop._reap_cleanup_futures()
    assert owned.actions == ["stop"]
    assert owned.workspace_marker == "unfinished-material"
    assert docker.runs == []
    loop.client.confirm_container_pause.assert_called_once_with(
        "p1", 1, "stop-token", owned.id, docker.daemon_id,
    )
    loop.client.update_task_run.assert_not_called()
    assert loop._inactive_cleanup_done["p1"] == ("paused", 1)
    docker.api.exec_inspect = lambda _exec_id: pytest.fail("stopped container needs no verifier exec")
    check = manager.candidate_execution_status(
        docker_daemon_id=docker.daemon_id,
        container_id=owned.id,
        container_name=owned.name,
        project_id="p1",
        exec_id="retained-exec",
        control_path="/tmp/cairn-exec-0123456789abcdef0123456789abcdef.json",
    )
    assert check.status == "stopped"
    assert manager.read_file(owned.id, "/tmp/fixture") == b"fixture"
    assert loop.client.update_task_run.call_count == 0

    loop._candidate_fenced_containers.clear()
    loop._candidate_fenced_task_runs.clear()
    loop._cleanup_paused_containers([paused])
    loop._reap_cleanup_futures()
    assert loop.client.update_task_run.call_count == 1
    assert loop.client.update_task_run.call_args.kwargs["runtime_context"]["execution_lifecycle"] == {
        "generation": 0, "process_started": True,
        "termination_confirmed": True, "cleanup_confirmed": True,
    }
    assert loop._pause_cleanup_confirmations
    loop._reap_cleanup_futures()
    assert loop.client.update_task_run.call_count == 2
    assert loop._pause_cleanup_confirmations == {}
    assert owned.actions == ["stop", ("read", "/tmp/fixture")]


def _finished_future(fn, *args):
    future = Future()
    future.set_result(fn(*args))
    return future


@pytest.mark.parametrize("candidate_fence", [False, True])
def test_failed_pause_stop_never_reports_project_or_task_cleanup(runtime, monkeypatch, candidate_fence):
    from cairn.dispatcher.protocol.client import ApiResult
    from test_dispatcher_multi_project_capacity import _loop, _summary

    manager, docker = runtime
    owned = owned_container(manager, docker, project="p1")
    monkeypatch.setattr(owned, "stop", lambda **_kwargs: None)
    loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
    loop.container_manager = manager
    loop.dispatcher_id = "test-dispatcher"
    loop.cleanup_futures = {}
    paused = _summary("p1").model_copy(update={"status": "paused", "execution_generation": 1})
    loop.client.list_task_runs = Mock(return_value=ApiResult(status_code=200, data=[]))
    loop.client.get_project = Mock(return_value=SimpleNamespace(project=paused))
    loop.client.get_container_activation = Mock(return_value=ApiResult(status_code=200, data={
        "activation": {"state": "running", "container_id": owned.id, "docker_daemon_id": docker.daemon_id},
    }))
    loop.client.claim_container_pause = Mock(return_value=ApiResult(status_code=200, data={
        "state": "stopping", "stop_token": "stop-token", "container_id": owned.id,
    }))
    loop.client.confirm_container_pause = Mock()
    loop.cleanup_executor = SimpleNamespace(submit=lambda fn, *args: _finished_future(fn, *args))
    if candidate_fence:
        loop._candidate_fenced_containers = {owned.name}
    loop._cleanup_paused_containers([paused])
    loop._reap_cleanup_futures()
    loop.client.confirm_container_pause.assert_not_called()
    assert "p1" not in loop._inactive_cleanup_done
    assert owned.status == "running"


def test_paused_cleanup_waits_for_local_future_and_confirms_only_after_stop():
    from cairn.dispatcher.protocol.client import ApiResult
    from test_dispatcher_multi_project_capacity import _loop, _summary

    loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
    loop.dispatcher_id = "test-dispatcher"
    loop.cleanup_futures = {}
    loop.browser_cleanup_futures = {}
    loop.container_manager.cleanup_paused = Mock(return_value=True)
    loop.client.update_task_run = Mock(return_value=ApiResult(status_code=200))
    lifecycle = {"generation": 1, "process_started": True,
                 "termination_confirmed": True, "cleanup_confirmed": False}
    run = {"id": "r1", "status": "cancelled", "execution_generation": 1,
           "runtime_context": {"dispatcher_id": "test-dispatcher", "container_name": "container-p1",
                               "container_id": "exact-id", "execution_lifecycle": lifecycle}}
    unknown = {**run, "id": "unknown", "runtime_context": {**run["runtime_context"],
               "execution_lifecycle": {**lifecycle, "termination_confirmed": False}}}
    loop.client.list_task_runs = Mock(return_value=ApiResult(status_code=200, data=[run, unknown]))
    cleanup_future = Future()
    loop.cleanup_executor = SimpleNamespace(submit=Mock(return_value=cleanup_future))
    task = SimpleNamespace(project_id="p1", task_type="explore")
    loop.futures[object()] = task
    paused = _summary("p1").model_copy(update={"status": "paused"})
    loop._cleanup_paused_containers([paused])
    loop.cleanup_executor.submit.assert_not_called()
    loop.futures.clear()
    loop._cleanup_paused_containers([paused])
    assert "container-p1" in loop._cleanup_pending
    loop.client.update_task_run.assert_not_called()
    cleanup_future.set_result(True)
    loop._reap_cleanup_futures()
    assert loop.client.update_task_run.call_count == 2
    updates = {call.args[1]: call.kwargs for call in loop.client.update_task_run.call_args_list}
    assert set(updates) == {"r1", "unknown"}
    for update in updates.values():
        assert update["runtime_context"]["execution_lifecycle"]["termination_confirmed"] is True
        assert update["runtime_context"]["execution_lifecycle"]["cleanup_confirmed"] is True
        assert update["runtime_context"]["execution_lifecycle"]["generation"] == 1


def test_user_stop_drain_cancels_local_work_and_survives_dispatcher_restart():
    from cairn.dispatcher.models import RunningTask
    from cairn.dispatcher.protocol.client import ApiResult
    from test_dispatcher_multi_project_capacity import _loop, _summary

    loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
    loop.dispatcher_id = "test-dispatcher"
    loop.cleanup_futures = {}
    loop.browser_cleanup_futures = {}
    loop.runtime_project_ids = {"p1"}
    loop.container_manager.cleanup_finishing = Mock(return_value=True)
    loop.client.update_task_run = Mock(return_value=ApiResult(status_code=200))
    lifecycle = {
        "generation": 1,
        "process_started": True,
        "termination_confirmed": True,
        "cleanup_confirmed": False,
    }
    run = {
        "id": "r1",
        "status": "cancelled",
        "execution_generation": 1,
        "pause_requested": True,
        "runtime_context": {
            "dispatcher_id": "test-dispatcher",
            "container_name": "container-p1",
            "execution_container_ids": ["exact-id"],
            "execution_lifecycle": lifecycle,
        },
    }
    loop.client.list_task_runs = Mock(
        return_value=ApiResult(status_code=200, data=[run])
    )
    def submit(fn, *args, **kwargs):
        future = Future()
        future.set_result(fn(*args, **kwargs))
        return future

    loop.cleanup_executor = SimpleNamespace(submit=submit)
    stopping = _summary("p1").model_copy(
        update={"status": "active", "coverage_stop_reason": "user_stopped"}
    )

    cancellation = TaskCancellation(generation=1)
    loop.futures[object()] = RunningTask(
        "p1",
        "explore",
        "w",
        "pi",
        cancellation,
        None,
        task_run_id="r1",
    )
    loop._cancel_inactive_tasks([stopping])
    assert cancellation.reason == "user_stopped"
    loop.futures.clear()

    loop._refresh_runtime_projects([stopping])
    assert "p1" not in loop.runtime_project_ids
    loop._cleanup_paused_containers([stopping])
    loop._reap_cleanup_futures()

    loop.container_manager.cleanup_finishing.assert_called_once_with(
        "p1",
        container_ids=["exact-id"],
    )
    assert loop._inactive_cleanup_done["p1"] == "user_stopped"
    update = loop.client.update_task_run.call_args.kwargs
    assert update["status"] == "cancelled"
    assert update["runtime_context"]["execution_lifecycle"] == {
        **lifecycle,
        "cleanup_confirmed": True,
    }


@pytest.mark.parametrize("removed", [False, True])
def test_finish_after_pause_keeps_exact_cleanup_confirmation_chain(removed):
    from cairn.dispatcher.protocol.client import ApiResult
    from test_dispatcher_multi_project_capacity import _loop, _summary

    loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
    loop.dispatcher_id = "test-dispatcher"
    loop.cleanup_futures = {}
    loop.browser_cleanup_futures = {}
    lifecycle = {"generation": 1, "process_started": True,
                 "termination_confirmed": True, "cleanup_confirmed": False}
    run = {"id": "r1", "status": "cancelled", "execution_generation": 1, "pause_requested": True,
           "runtime_context": {"dispatcher_id": "test-dispatcher", "container_name": "container-p1",
                               "execution_container_ids": ["exact-id"], "execution_lifecycle": lifecycle}}
    loop.client.list_task_runs = Mock(return_value=ApiResult(status_code=200, data=[run]))
    loop.client.update_task_run = Mock(return_value=ApiResult(status_code=200))
    loop.container_manager.needs_stopped_cleanup = lambda _project: True
    loop.container_manager.cleanup_stopped = Mock(return_value=True)
    loop.container_manager.cleanup_finishing = Mock(return_value=removed)

    def submit(fn, *args, **kwargs):
        future = Future()
        future.set_result(fn(*args, **kwargs))
        return future

    loop.cleanup_executor = SimpleNamespace(submit=submit)
    stopped = _summary("p1").model_copy(update={"status": "stopped"})
    loop._cleanup_stopped_containers([stopped])
    loop._cleanup_paused_containers([stopped])
    loop._reap_cleanup_futures()
    # 普通 stop 成功不等于历史资源已删除；下一轮仍需精确清理。
    loop.client.update_task_run.assert_not_called()
    loop._cleanup_stopped_containers([stopped])
    loop._cleanup_paused_containers([stopped])
    loop._reap_cleanup_futures()
    loop.container_manager.cleanup_finishing.assert_called_once_with("p1", container_ids=["exact-id"])
    if removed:
        update = loop.client.update_task_run.call_args.kwargs
        assert update["status"] == "cancelled"
        assert update["runtime_context"]["execution_lifecycle"] == {**lifecycle, "cleanup_confirmed": True}
    else:
        loop.client.update_task_run.assert_not_called()


@pytest.mark.parametrize("method,args", [
    ("reason_heartbeat", ("p1", "worker")),
    ("release_reason", ("p1", "worker")),
    ("release", ("p1", "intent", "worker")),
])
def test_lease_updates_include_task_run_identity(method, args):
    from cairn.dispatcher.protocol.client import CairnClient, ApiResult
    client = object.__new__(CairnClient)
    client._request_json = Mock(return_value=ApiResult(status_code=200))
    getattr(client, method)(*args, task_run_id="run-generation-2")
    assert client._request_json.call_args.kwargs["json"]["task_run_id"] == "run-generation-2"


@pytest.mark.parametrize("task_type", ["bootstrap", "reason", "explore"])
def test_admission_transmits_frozen_generation_and_never_submits_rejected_old_work(task_type):
    from cairn.dispatcher.protocol.client import CairnClient, ApiResult
    from test_dispatcher_multi_project_capacity import _loop

    loop = _loop(max_workers=1, max_running_projects=1, worker_count=1)
    worker = loop.config.workers[0]
    scenario = SimpleNamespace(key="fixture", prompt_group="default")
    project = SimpleNamespace(project=SimpleNamespace(id="p1", execution_generation=7, tenant_workers=[]))
    intent = SimpleNamespace(id="i1")
    loop._project_generations = {"p1": 8}
    loop._scenario_for_project = lambda _project: scenario
    loop._container_runtime_options = lambda *_args: None
    loop._select_worker = lambda *_args, **_kwargs: SimpleNamespace(worker=worker)
    loop._materialize_selected_worker = lambda *_args: (worker, {}, None)
    loop._task_run_runtime_context = lambda *_args, **_kwargs: {}
    loop._explore_tried_workers = lambda *_args: set()
    loop.executor = SimpleNamespace(submit=Mock())
    loop.client = object.__new__(CairnClient)
    loop.client.list_task_runs = lambda project_id: ApiResult(status_code=200, data=[])
    bodies = []

    def request(method, path, **kwargs):
        if path.endswith(("/task-runs", "/explore-attempts")):
            bodies.append(kwargs["json"])
            return ApiResult(status_code=409, data={"detail": "stale_execution_generation"})
        return ApiResult(status_code=200)

    loop.client._request_json = request
    if task_type == "bootstrap":
        assert loop._dispatch_bootstrap(project, intent) is False
    elif task_type == "reason":
        assert loop._dispatch_reason(project, "graph", "fixture") is False
    else:
        assert loop._dispatch_explore(project, "graph", intent) is False
    assert len(bodies) == 1
    assert bodies[0]["expected_execution_generation"] == 7
    assert "execution_generation" not in bodies[0]
    loop.executor.submit.assert_not_called()


@pytest.mark.parametrize("error_type", [None, "provider_auth", "protocol_error"])
def test_future_exception_keeps_specific_safe_error_instead_of_container_failed(error_type):
    from cairn.dispatcher.models import RunningTask
    from cairn.dispatcher.protocol.client import ApiResult
    from test_dispatcher_multi_project_capacity import _loop
    loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
    loop._finish_runtime_context = lambda *_args: {}
    loop.client.update_task_run = Mock(return_value=ApiResult(status_code=200))
    error = RuntimeError("secret-canary")
    if error_type:
        error.error_type = error_type
    future = Future()
    future.set_exception(error)
    loop.futures[future] = RunningTask("p1", "explore", "w", "pi", TaskCancellation(), None, task_run_id="r1")
    loop._reap_futures()
    flush_finishes(loop)
    payload = loop.client.update_task_run.call_args.kwargs
    assert payload["error_type"] == (error_type or "unknown")
    assert "secret-canary" not in str(payload)


def test_pause_cleanup_rejects_replacement_without_stopping_it(runtime, monkeypatch):
    manager, docker = runtime
    owned = owned_container(manager, docker, project="p1")
    original_stop = owned.stop
    replacement = []

    def replace(**kwargs):
        original_stop(**kwargs)
        docker.objects.remove(owned)
        item = owned_container(manager, docker, project="p1")
        item.id = "replacement-id"
        replacement.append(item)

    monkeypatch.setattr(owned, "stop", replace)
    assert manager.cleanup_paused("p1", container_ids=[owned.id]) is False
    assert replacement[0].actions == []
