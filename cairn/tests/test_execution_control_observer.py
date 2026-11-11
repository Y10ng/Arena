"""真实授权截止与热暂停执行预算不能共用冻结时钟。"""

from concurrent.futures import Future, ThreadPoolExecutor
import threading
import time

import pytest
from types import SimpleNamespace
from unittest.mock import Mock

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.pause_clock import ExecutionSceneLost
from cairn.dispatcher.scheduler.execution_control import ExecutionControlObserver


def test_expired_authorization_cancels_research_even_while_clock_is_paused():
    cancellation = TaskCancellation()
    cancellation.pause_clock.pause()
    task = SimpleNamespace(project_id="p1", task_type="explore", cancellation=cancellation)
    loop = SimpleNamespace(futures={Future(): task}, dispatcher_id="dispatcher-1")
    observer = ExecutionControlObserver(loop)
    try:
        observer.observe([SimpleNamespace(id="p1", authorization_expires_at="2000-01-01T00:00:00Z", execution_control={})])
        assert cancellation.reason == "project_authorization_expired"
        assert cancellation.pause_clock.paused
    finally:
        observer._pool.shutdown(wait=True)


def test_resume_never_unfreezes_after_absolute_authorization_deadline():
    control = {"request_id": "request-1", "action": "resume", "resources": []}
    client = SimpleNamespace(
        get_project=lambda _id: SimpleNamespace(project=SimpleNamespace(execution_control=control,
            authorization_expires_at="2000-01-01T00:00:00Z")),
        confirm_execution_control=Mock(return_value=ApiResult(status_code=200)),
    )
    manager = SimpleNamespace(hot_control=Mock())
    loop = SimpleNamespace(client=client, container_manager=manager, futures={}, dispatcher_id="dispatcher-1")
    observer = ExecutionControlObserver(loop)
    try:
        observer.apply("p1", control)
        manager.hot_control.assert_not_called()
        assert client.confirm_execution_control.call_args.kwargs["state"] == "failed"
    finally:
        observer._pool.shutdown(wait=True)


from test_pi_container_ownership import runtime, owned_container  # noqa: E402, F401


def retained_scene_observer(manager, container):
    control = {"request_id": "resume-2", "action": "resume", "state": "requested", "resources": []}
    run = {"id": "tr001", "task_type": "bootstrap", "status": "succeeded", "execution_generation": 7,
           "runtime_context": {"dispatcher_id": "dispatcher-1", "container_id": container.id,
                               "execution_container_ids": [container.id],
                               "execution_lifecycle": {"generation": 7, "termination_confirmed": True,
                                                       "cleanup_confirmed": False}}}
    activation = {"dispatcher_id": "dispatcher-1", "activation_generation": 7, "state": "running",
                  "container_id": container.id, "docker_daemon_id": manager.docker_daemon_id()}
    client = SimpleNamespace(
        get_project=lambda _id: SimpleNamespace(project=SimpleNamespace(
            execution_control=control, authorization_expires_at=None, execution_generation=7)),
        list_task_runs=lambda _id: ApiResult(status_code=200, data=[run]),
        get_container_activation=lambda _id: ApiResult(status_code=200, data={"activation": activation}),
        confirm_execution_control=Mock(return_value=ApiResult(status_code=200)),
    )
    loop = SimpleNamespace(client=client, container_manager=manager, futures={}, dispatcher_id="dispatcher-1")
    return ExecutionControlObserver(loop), control, run, activation


def test_resume_terminated_run_retains_original_environment_without_future(runtime):
    manager, docker = runtime
    container = owned_container(manager, docker, project="p1", state="paused")
    observer, control, run, activation = retained_scene_observer(manager, container)
    try:
        observer.apply("p1", control)
        receipt = observer.loop.client.confirm_execution_control.call_args.kwargs
        assert receipt["state"] == "running" and receipt["resources"][0]["container_id"] == container.id
        assert container.actions == ["unpause"] and docker.runs == []
        assert run["status"] == "succeeded" and run["execution_generation"] == activation["activation_generation"] == 7
        assert run["runtime_context"]["execution_lifecycle"]["cleanup_confirmed"] is False
    finally:
        observer._pool.shutdown(wait=True)


@pytest.mark.parametrize("fault,reason,task_run_id", [
    ("missing_future", "run_connection_missing", "tr001"),
    ("unrelated_future", "run_connection_missing", "tr001"),
    ("wrong_future_generation", "run_connection_missing", "tr001"),
    ("missing_container", "physical_scene_unverified", "-"),
    ("container_owner", None, "-"),
    ("activation_owner", "activation_owner_mismatch", "-"),
    ("activation_generation", "activation_generation_mismatch", "-"),
    ("activation_stopped", "activation_stopped", "-"),
    ("activation_missing_identity", "activation_identity_mismatch", "-"),
    ("daemon", "docker_daemon_mismatch", "-"),
    ("generation", "run_connection_missing", "tr001"),
    ("run_generation", "run_generation_mismatch", "tr001"),
    ("missing_identity", "activation_missing", "-"),
    ("missing_identity_and_container", "activation_missing", "-"),
    ("container_name", "physical_scene_unverified", "-"),
])
def test_resume_retained_scene_rejects_missing_connection_or_identity(runtime, caplog, fault, reason, task_run_id):
    manager, docker = runtime
    container = owned_container(manager, docker, project="p1", state="paused")
    observer, control, run, activation = retained_scene_observer(manager, container)
    if fault in {"missing_future", "unrelated_future", "wrong_future_generation"}:
        run["status"] = "running"
        if fault != "missing_future":
            observer.loop.futures[Future()] = SimpleNamespace(
                project_id="p1", task_type="explore", cancellation=TaskCancellation(generation=7),
                task_run_id="other" if fault == "unrelated_future" else "tr001",
                execution_generation=8 if fault == "wrong_future_generation" else 7)
    elif fault == "missing_container":
        docker.objects.remove(container)
    elif fault == "container_owner":
        container.attrs["Config"]["Labels"]["cairn.pi.instance_id"] = "other-owner"
    elif fault == "activation_owner":
        activation["dispatcher_id"] = "other-owner"
    elif fault == "activation_generation":
        activation["activation_generation"] = 6
    elif fault == "activation_stopped":
        activation["state"] = "stopped"
    elif fault == "activation_missing_identity":
        activation["container_id"] = None
    elif fault == "daemon":
        activation["docker_daemon_id"] = "other-daemon"
    elif fault == "generation":
        run["runtime_context"]["execution_lifecycle"]["generation"] = 6
    elif fault == "run_generation":
        run["execution_generation"] = 6
        run["runtime_context"]["execution_lifecycle"]["generation"] = 6
    elif fault in {"missing_identity", "missing_identity_and_container"}:
        activation.clear()
        run["runtime_context"].update(container_id=None, execution_container_ids=[])
        if fault == "missing_identity_and_container":
            docker.objects.remove(container)
    elif fault == "container_name":
        activation["container_id"] = container.name
        run["runtime_context"].update(container_id=container.name, execution_container_ids=[container.name])
    try:
        observer.apply("p1", control)
        receipt = observer.loop.client.confirm_execution_control.call_args.kwargs
        assert receipt["state"] == ("lost" if reason else "failed")
        assert receipt["error"] == ("execution_scene_lost" if reason else "execution_control_failed")
        assert receipt["resources"] == []
        assert receipt["scene_loss_reason"] == reason
        assert receipt["task_run_id"] == (None if task_run_id == "-" else task_run_id)
        assert container.actions == [] and docker.runs == []
        if reason:
            assert (
                f"project=p1 request=resume-2 action=resume reason={reason} task_run={task_run_id}"
            ) in caplog.text
    finally:
        observer._pool.shutdown(wait=True)


def test_scene_loss_log_excludes_private_exception_and_runtime_context(runtime, caplog, monkeypatch):
    manager, docker = runtime
    container = owned_container(manager, docker, project="p1", state="paused")
    observer, control, run, activation = retained_scene_observer(manager, container)
    private_value = "private-control-token-do-not-log"
    activation["activation_token"] = private_value
    run["runtime_context"]["private_path"] = f"/private/{private_value}"
    monkeypatch.setattr(manager, "hot_control", Mock(side_effect=ExecutionSceneLost(f"/private/{private_value}")))
    try:
        observer.apply("p1", control)
        receipt = observer.loop.client.confirm_execution_control.call_args.kwargs
        assert receipt == {
            "request_id": "resume-2", "state": "lost", "resources": [], "error": "execution_scene_lost",
            "scene_loss_reason": "physical_scene_unverified", "task_run_id": None,
        }
        assert "reason=physical_scene_unverified task_run=-" in caplog.text
        assert private_value not in caplog.text and private_value not in repr(receipt)
        assert all(record.exc_info is None for record in caplog.records)
        assert container.actions == [] and docker.runs == []
    finally:
        observer._pool.shutdown(wait=True)


def test_failed_resume_waits_for_user_request_instead_of_automatic_retry(runtime):
    manager, docker = runtime
    container = owned_container(manager, docker, project="p1", state="paused")
    observer, control, _, _ = retained_scene_observer(manager, container)
    control.update(state="failed", dispatcher_ids=["dispatcher-1"])
    try:
        observer.observe([SimpleNamespace(id="p1", authorization_expires_at=None, execution_control=control)])
        assert observer._pending == {} and container.actions == []
        observer.loop.client.confirm_execution_control.assert_not_called()
    finally:
        observer._pool.shutdown(wait=True)


def preparing_observer(manager, identities):
    control = {"request_id": "pause-1", "action": "pause"}
    client = SimpleNamespace(
        get_project=lambda _id: SimpleNamespace(project=SimpleNamespace(
            execution_control=control, authorization_expires_at=None, coverage_stop_reason=None)),
        list_task_runs=lambda _id: ApiResult(status_code=200, data=[]),
        get_container_activation=lambda _id: ApiResult(status_code=200, data={"activation": None}),
        confirm_execution_control=Mock(return_value=ApiResult(status_code=409)),
    )
    cancellation = TaskCancellation()
    cancellation.pause_clock = manager.research_clock("p1")
    cancellation.execution_container_ids = lambda: list(identities)
    task = SimpleNamespace(project_id="p1", task_type="explore", cancellation=cancellation)
    loop = SimpleNamespace(client=client, container_manager=manager,
                           futures={Future(): task}, dispatcher_id="dispatcher-1")
    return ExecutionControlObserver(loop), control


@pytest.mark.parametrize("action", ["pause", "finish"])
def test_control_freezes_existing_producer_without_waiting_for_readiness_http(runtime, action):
    manager, docker = runtime
    producer = owned_container(manager, docker, project="p1")
    entered, release = threading.Event(), threading.Event()
    finished = []
    observer, control = preparing_observer(manager, [producer.id])
    control["action"] = action

    def register(_daemon):
        entered.set()
        assert release.wait(5)
        return "original-token"

    with ThreadPoolExecutor(max_workers=2) as pool:
        preparation = pool.submit(manager.activate_for_readiness, "p1", manager.default_runtime_options(),
            register=register, finish=lambda *binding: finished.append(binding),
            abort=lambda *_args: pytest.fail("原容器不能结算未创建"))
        try:
            assert entered.wait(2)
            started = time.monotonic()
            pool.submit(observer.apply, "p1", control).result(timeout=1)
            assert time.monotonic() - started < 1 and not preparation.done()
            assert producer.status == ("paused" if action == "pause" else "exited")
            release.set()
            if action == "finish":
                with pytest.raises(RuntimeError, match="research_execution_stopped"):
                    preparation.result(timeout=2)
            else:
                manager.hot_control("p1", "resume", container_ids=[producer.id], in_flight=True)
                assert preparation.result(timeout=2) == producer.name
                assert producer.status == "running"
            assert finished == [("original-token", producer.id)]
            assert "start" not in producer.actions and docker.runs == []
        finally:
            release.set()
            observer._pool.shutdown(wait=True)


def test_pause_during_continuation_reuse_preserves_original_scene(runtime):
    manager, docker = runtime
    producer = owned_container(manager, docker, project="p1")
    entered, release = threading.Event(), threading.Event()
    observer, control = preparing_observer(manager, [producer.id])

    def reuse(_daemon, identity):
        assert identity == producer.id
        entered.set()
        assert release.wait(5)
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        preparation = pool.submit(manager.activate_existing_for_continuation,
            "p1", manager.default_runtime_options(), producer.id,
            register=lambda *_: pytest.fail("运行中原现场不能重复激活"),
            finish=lambda *_: pytest.fail("复用不能更换原 token"), reuse=reuse)
        try:
            assert entered.wait(2)
            pool.submit(observer.apply, "p1", control).result(timeout=1)
            assert producer.status == "paused" and not preparation.done()
            release.set()
            manager.hot_control("p1", "resume", container_ids=[producer.id], in_flight=True)
            assert preparation.result(timeout=2) == producer.name
            assert producer.actions == ["pause", "unpause"] and docker.runs == []
        finally:
            release.set()
            manager.research_clock("p1").resume()
            observer._pool.shutdown(wait=True)


@pytest.mark.parametrize("path", ["ensure", "readiness", "continuation"])
@pytest.mark.parametrize("action", ["pause", "finish"])
def test_late_preparation_keeps_exact_resource_without_leaking_a_start(runtime, monkeypatch, path, action):
    manager, docker = runtime
    entered, release, published, created = (threading.Event() for _ in range(4))
    bindings = []
    original = owned_container(manager, docker, project="p1", state="exited") if path == "continuation" else None
    create = docker.create

    def blocked_create(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        resource = create(*args, **kwargs)
        created.set()
        return resource

    def register(_daemon):
        if path == "continuation":
            entered.set()
            assert release.wait(5)
        return "original-token"

    def finish(token, identity):
        bindings.append((token, identity))
        published.set()

    monkeypatch.setattr(docker, "create", blocked_create)
    observer, control = preparing_observer(manager, [original.id] if original else [])
    control["action"] = action
    with ThreadPoolExecutor(max_workers=2) as pool:
        if path == "ensure":
            preparation = pool.submit(manager.ensure_running, "p1")
        elif path == "readiness":
            preparation = pool.submit(manager.activate_for_readiness, "p1", manager.default_runtime_options(),
                register=register, finish=finish, abort=lambda *_args: pytest.fail("晚到容器仍有归属"))
        else:
            preparation = pool.submit(manager.activate_existing_for_continuation, "p1", manager.default_runtime_options(),
                original.id, register=register, finish=finish, reuse=lambda *_args: False)
        try:
            assert entered.wait(2)
            pool.submit(observer.apply, "p1", control).result(timeout=1)
            assert not preparation.done()
            release.set()
            if action == "finish":
                with pytest.raises(RuntimeError, match="research_execution_stopped"):
                    preparation.result(timeout=2)
            else:
                if path != "ensure":
                    assert published.wait(2)
                else:
                    assert created.wait(2)
                assert not preparation.done()
                observer.apply("p1", control)
                receipt = observer.loop.client.confirm_execution_control.call_args.kwargs
                assert receipt["resources"][0]["state"] == ("exited" if original else "created")
                manager.hot_control("p1", "resume", container_ids=[docker.objects[0].id], in_flight=True)
                assert preparation.result(timeout=2) == docker.objects[0].name
            resource = docker.objects[0]
            assert resource.id == (original.id if original else "container-0")
            assert resource.actions == (["start"] if action == "pause" else [])
            assert len(docker.runs) == (0 if original else 1)
            if path != "ensure":
                assert bindings == [("original-token", resource.id)]
        finally:
            release.set()
            manager.research_clock("p1").resume()
            observer._pool.shutdown(wait=True)
