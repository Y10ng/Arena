"""到期原因沿原控制、取消及 Run 写回路径传播；Docker 使用本地假对象。"""

from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from cairn.dispatcher.models import RunningTask
from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.scheduler.execution_control import ExecutionControlObserver
from test_dispatcher_multi_project_capacity import _loop, _summary
from test_dispatcher_pause_confirmation import flush_finishes
from test_pi_container_ownership import runtime, owned_container


def test_timeout_client_uses_authenticated_bodyless_post_and_surfaces_errors(monkeypatch):
    client = CairnClient("https://server.example.test", token="synthetic-token")
    response, session = Mock(), Mock()
    response.json.return_value = {"finished": 2}
    session.post.return_value = response
    monkeypatch.setattr(client, "_session", lambda: session)
    assert client.expire_paused_projects() == 2
    session.post.assert_called_once_with("https://server.example.test/projects/pause-timeouts/expire", timeout=10.0)
    for invalid in (None, [], {}, {"finished": True}, {"finished": -1}, {"finished": "2"}):
        response.json.return_value = invalid
        with pytest.raises(ValueError):
            client.expire_paused_projects()
    response.raise_for_status.side_effect = requests.HTTPError("synthetic failure")
    with pytest.raises(requests.HTTPError):
        client.expire_paused_projects()


def test_main_loop_cancellation_preserves_reason_in_run_and_excludes_report_chat():
    loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
    loop._finish_runtime_context = lambda *_args: {}
    loop.client.update_task_run = Mock(return_value=ApiResult(status_code=200))
    for kind in ("explore", "report", "agent_chat"):
        future = Future()
        loop.futures[future] = RunningTask("p1", kind, "w", "pi", TaskCancellation(generation=7), None, task_run_id=kind)
    stopped = _summary("p1").model_copy(update={"status": "stopped", "coverage_stop_reason": "pause_timeout"})
    loop._cancel_inactive_tasks([stopped])
    for future, task in list(loop.futures.items()):
        if task.task_type == "explore":
            assert task.cancellation.reason == "pause_timeout"
            future.set_result("cancelled")
        else:
            assert task.cancellation.reason is None
    loop._reap_futures()
    flush_finishes(loop)
    update = loop.client.update_task_run.call_args.kwargs
    assert update["status"] == update["error_type"] == "cancelled"
    assert update["error_message"] == "pause_timeout"
    assert {task.task_type for task in loop.futures.values()} == {"report", "agent_chat"}


@pytest.mark.parametrize("local_future", [False, True])
@pytest.mark.parametrize("write_ok", [False, True])
def test_owner_stop_retains_container_and_settles_only_orphan_research(runtime, local_future, write_ok):
    manager, docker = runtime
    container = owned_container(manager, docker, project="p1", state="paused")
    control = {"request_id": "timeout-finish", "action": "finish", "state": "requested"}
    project = SimpleNamespace(execution_control=control, authorization_expires_at=None,
                              coverage_stop_reason="pause_timeout", execution_generation=8)
    runs = [{"id": "research", "task_type": "explore", "status": "running", "execution_generation": 7,
             "runtime_context": {"dispatcher_id": "owner-a", "execution_container_ids": [container.id]}}]
    for kind in ("report", "agent_chat"):
        runs.append({"id": kind, "task_type": kind, "status": "running", "runtime_context": {"dispatcher_id": "owner-a"}})
    runs.append({"id": "other", "task_type": "explore", "status": "running", "runtime_context": {"dispatcher_id": "owner-b"}})
    client = SimpleNamespace(
        get_project=lambda _pid: SimpleNamespace(project=project),
        list_task_runs=lambda _pid: ApiResult(status_code=200, data=runs),
        get_container_activation=lambda _pid: ApiResult(status_code=200, data={"activation": None}),
        update_task_run=Mock(return_value=ApiResult(status_code=200 if write_ok else 503)),
        confirm_execution_control=Mock(return_value=ApiResult(status_code=200)),
    )
    token = TaskCancellation(generation=7)
    futures = {Future(): RunningTask("p1", "explore", "w", "pi", token, None, task_run_id="research")} if local_future else {}
    observer = ExecutionControlObserver(SimpleNamespace(client=client, container_manager=manager, futures=futures, dispatcher_id="owner-a"))
    try:
        observer.apply("p1", control)
        assert container.status == "exited" and container.actions == ["kill"]
        assert container in docker.objects and docker.runs == []
        receipt = client.confirm_execution_control.call_args.kwargs
        assert receipt["state"] == ("stopped" if local_future or write_ok else "failed")
        if local_future:
            assert token.reason == "pause_timeout"
            client.update_task_run.assert_not_called()
        else:
            client.update_task_run.assert_called_once_with("p1", "research", status="cancelled", error_type="cancelled", error_message="pause_timeout")
        if not local_future and not write_ok:
            assert "p1" not in observer._acknowledged
            client.update_task_run.return_value = ApiResult(status_code=200)
            observer.apply("p1", control)
            assert client.confirm_execution_control.call_args.kwargs["state"] == "stopped"
            assert container.actions == ["kill"]
    finally:
        observer._pool.shutdown(wait=True)


def test_restart_stale_research_keeps_timeout_reason_without_fabricating_proof():
    loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
    loop.dispatcher_id = "owner-a"
    items = [{"id": kind, "task_type": kind, "status": "running", "pause_requested": True}
             for kind in ("explore", "report", "agent_chat")]
    loop.client.list_task_runs = Mock(return_value=ApiResult(status_code=200, data=items))
    loop.client.update_task_run = Mock(return_value=ApiResult(status_code=200))
    stopped = _summary("p1").model_copy(update={"status": "stopped", "coverage_stop_reason": "pause_timeout"})
    loop._reconcile_stale_running_task_runs([stopped])
    updates = {call.args[1]: call.kwargs for call in loop.client.update_task_run.call_args_list}
    assert "agent_chat" not in updates
    assert updates["explore"]["status"] == "cancelled" and updates["explore"]["error_message"] == "pause_timeout"
    assert "execution_lifecycle" not in updates["explore"]["runtime_context"]
    assert updates["report"]["error_type"] == "heartbeat_lost"
