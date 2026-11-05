from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest

from cairn.dispatcher.config import PiModelConfig, WorkerConfig
from cairn.dispatcher.models import RunningTask
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.container_options import ContainerRuntimeOptions
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.runtime.project_tools import ProjectToolBinding
from cairn.dispatcher.scheduler.agent_chat import AgentChatMixin
from cairn.dispatcher.scheduler.cleanup import CleanupMixin
from cairn.dispatcher.scheduler.worker_runtime import WorkerRuntimeMixin
from cairn.dispatcher.tasks.agent_chat import run_agent_turn


def worker():
    return WorkerConfig(id="w1", engine="pi", task_types=["reason"], concurrency=2, priority=0,
                        model_binding=PiModelConfig(protocol="openai-chat-completions", model_name="fixture-model",
                            inference_endpoint="http://provider.invalid/v1/chat/completions", base_url="http://provider.invalid/v1",
                            auth_header="authorization-bearer", api_key="synthetic-model-secret"))


def claim():
    return {"project_id": None, "conversation_id": "c1", "message_id": 9, "task_run_id": "tr1",
            "session_id": f"pi:{uuid.uuid4()}", "resume": False, "prompt": "查询项目进度", "worker_id": "w1", "status": "running", "session_machine_id": "d1",
            "allowed_tools": ["query_project_progress"]}


def runtime():
    return ContainerRuntimeOptions.build(image="worker:fixture", network_mode="bridge", dns=[], cap_add=[],
                                         features={"conversation_chat": 1}, scenario_key="conversation-chat")


def finish_storage(loop, root):
    loop._candidate_outbox_parent = root
    loop._candidate_server_namespace = "synthetic-server"
    loop.dispatcher_id = "d1"
    loop._log_state = {}
    loop._log_changed = CleanupMixin._log_changed.__get__(loop)
    loop._clear_log_state = CleanupMixin._clear_log_state.__get__(loop)
    return loop


class Client:
    def __init__(self):
        self.calls = []

    def agent_turn_action(self, conversation, message, action, body):
        self.calls.append((None, conversation, message, action, body))
        if action == "tools":
            assert body == {"task_run_id": "tr1", "method": "query_project_progress", "params": {}, "tool_call_id": "sdk-call-1"}
            return ApiResult(200, {"status": "completed"})
        return ApiResult(200, {"cancelled": False})

    def create_agent_event(self, *args, **kwargs):
        self.calls.append((None, None, None, "unexpected_public_event", kwargs))
        return ApiResult(200)


class Manager:
    def __init__(self, root, *, answer="项目已完成。"):
        self.root = root
        (root / "prompts").mkdir()
        self.files = {}
        self.starts = 0
        self.answer = answer

    def ensure_chat_running(self, project, options):
        assert project == "c1" and options.features == {"conversation_chat": 1}
        return "owned-p1-chat"

    def chat_directory(self, project):
        return self.root

    def write_text_file(self, container, path, content, **kwargs):
        self.files[path] = content

    def remove_file(self, container, path, **kwargs):
        self.files.pop(path)

    def prepare_browser_session(self, *args, **kwargs):
        raise AssertionError("聊天不能准备浏览器或测试scope")

    def build_exec_process(self, container, env, argv, **kwargs):
        request = json.loads(self.files[argv[-1]])
        self.request = request
        assert request["phase"] == "chat_turn"
        assert set(request["capabilities"]) == {"project_tools"}
        assert request["max_result_repairs"] == 0
        manager = self

        class Process:
            execution_completed = True
            execution_started = True
            execution_identity = ("container1", "exec1")

            def start(self):
                manager.starts += 1

            def communicate(self, timeout):
                path = manager.root / "prompts" / Path(request["capabilities"]["project_tools"]["socket_path"]).name
                with socket.socket(socket.AF_UNIX) as connection:
                    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        connection.connect(f"/proc/self/fd/{directory}/{path.name}")
                    finally:
                        os.close(directory)
                    connection.sendall(b'{"method":"query_project_progress","params":{},"tool_call_id":"sdk-call-1"}\n')
                    response = json.loads(connection.makefile("rb").readline())
                    assert response == {"ok": True, "data": {"status": "completed"}}
                return ProcessResult(0, json.dumps({
                    "protocol": "cairn-agent-result-v1", "engine": "pi", "phase": "chat_turn",
                    "project_id": None, "conversation_id": "c1", "task_run_id": "tr1", "session": request["session"],
                    "status": "succeeded", "result": {"text": manager.answer}, "submission_id": None,
                    "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": None, "cache_write_tokens": None},
                    "error": None,
                }), "private-thinking-marker")

        return Process()


def test_claim_runs_through_driver_process_and_private_socket_without_public_diagnostics(tmp_path):
    client, manager = Client(), Manager(tmp_path)
    config = SimpleNamespace(runtime=SimpleNamespace(interval=30), tasks=SimpleNamespace(reason=SimpleNamespace(timeout=10)))
    result = run_agent_turn(config, client, manager, worker(), TaskCancellation(), claim(), runtime())
    assert result == {"status": "succeeded", "text": "项目已完成。", "error_code": None,
                      "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": None, "cache_write_tokens": None},
                      "usage_missing_responses": None, "session_initialized": None}
    assert manager.starts == 1
    assert manager.request["capabilities"]["project_tools"]["allowed_tools"] == claim()["allowed_tools"]
    assert not manager.files
    assert list((tmp_path / "prompts").iterdir()) == []
    assert [call[3] for call in client.calls] == ["heartbeat", "tools"]
    assert "private-thinking-marker" not in json.dumps(result)


def test_project_tool_bridge_preserves_sdk_identity_and_only_exposes_public_business_errors():
    detail = {"code": "assessment_report_project_active", "message": "项目仍在研究中，不能生成综合报告。"}
    client = SimpleNamespace(agent_turn_action=Mock(return_value=ApiResult(409, {"detail": detail})))
    binding = ProjectToolBinding(client, "p1", "c1", "m1", "tr1", ["synthetic-secret"])
    request = {"method": "request_project_report", "params": {}, "tool_call_id": "sdk-call-1"}
    assert binding.dispatch(request) == {"error": detail}
    client.agent_turn_action.assert_called_once_with("c1", "m1", "tools", {"task_run_id": "tr1", **request})
    for data in ({"detail": "private-stack"}, {"detail": {**detail, "trace": "private-stack"}}, None):
        client.agent_turn_action.return_value = ApiResult(500, data, text="private-response")
        with pytest.raises(ValueError, match="^project_tool_failed$"):
            binding.dispatch(request)
    client.agent_turn_action.return_value = ApiResult(409, {"detail": {**detail, "message": "synthetic-secret"}})
    with pytest.raises(ValueError, match="^project_tool_protected_material$"):
        binding.dispatch(request)


@pytest.mark.parametrize("allowed", [None, "all", ["_validate_workspace_claim"], ["query_agent_work", "query_agent_work"]])
def test_chat_driver_rejects_missing_or_malformed_capability(tmp_path, allowed):
    from cairn.dispatcher.workers.adapters.pi import PiDriver
    from cairn.dispatcher.worker_result_gateway import WorkerRequestError

    with pytest.raises(WorkerRequestError):
        PiDriver().build_execute(
            worker(), "只读查询", claim()["session_id"], phase="chat_turn", project_id=None, conversation_id="c1", task_run_id="tr1",
            timeout_seconds=10, session_root="/home/kali/.cairn/pi",
            project_tools_socket_path="/tmp/cairn-prompts/pi-project-" + "a" * 32 + ".sock",
            project_tools_workspace_path="/home/kali/workspace/c1", project_tools_allowed_tools=allowed,
        )


def test_chat_consumer_rejects_known_secret_without_reexecution(tmp_path):
    client, manager = Client(), Manager(tmp_path, answer="synthetic-model-secret")
    config = SimpleNamespace(runtime=SimpleNamespace(interval=30), tasks=SimpleNamespace(reason=SimpleNamespace(timeout=10)))
    result = run_agent_turn(config, client, manager, worker(), TaskCancellation(), claim(), runtime())
    assert result["status"] == "failed" and result["error_code"] == "public_output_forbidden"
    assert result["text"] == "" and manager.starts == 1
    assert result["usage"] == {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": None, "cache_write_tokens": None}


def test_pending_chat_cursor_advances_after_rejected_full_page(tmp_path):
    loop = finish_storage(AgentChatMixin(), tmp_path)
    loop.futures = {}
    loop.config = SimpleNamespace(container=SimpleNamespace(image="worker:fixture", network_mode="bridge", dns=[]))
    loop.container_manager = SimpleNamespace(chat_container_name=lambda project: "owned-" + project + "-chat")
    page = [{"project_id": "p1", "conversation_id": f"c{index:03d}", "execution_generation": 0} for index in range(101)]
    loop.client = SimpleNamespace(
        pending_agent_turns=Mock(side_effect=[ApiResult(200, page[:100]), ApiResult(503), ApiResult(200, page[100:]), ApiResult(200, [])]),
        claim_agent_turn=Mock(return_value=ApiResult(403)),
    )
    loop._dispatch_agent_turns()
    assert loop.client.claim_agent_turn.call_count == 100
    assert loop._agent_pending_after == "c099"
    loop._dispatch_agent_turns()
    assert loop._agent_pending_after == "c099"
    loop._dispatch_agent_turns()
    assert loop.client.claim_agent_turn.call_count == 101
    assert loop.client.claim_agent_turn.call_args.args[0] == "c100"
    assert loop._agent_pending_after == ""
    loop._dispatch_agent_turns()
    assert [call.kwargs["after"] for call in loop.client.pending_agent_turns.call_args_list] == ["", "c099", "c099", ""]


def test_agent_has_real_executor_capacity_when_research_is_full_and_retries_only_finish(monkeypatch, tmp_path):
    from cairn.dispatcher.scheduler.dispatch_cycle import DispatchCycleMixin

    class Loop(AgentChatMixin, WorkerRuntimeMixin, DispatchCycleMixin):
        pass
    loop = finish_storage(Loop(), tmp_path)
    loop.config = SimpleNamespace(runtime=SimpleNamespace(max_workers=1, max_project_workers=1, max_running_projects=1),
                                  container=SimpleNamespace(image="worker:fixture", network_mode="bridge", dns=[]))
    loop.bootstrap = SimpleNamespace(hard_cap=1)
    loop.futures = {}
    loop.runtime_project_ids = {"p1"}
    loop.dispatcher_id = "d1"
    loop.container_manager = SimpleNamespace(chat_container_name=lambda project: "owned-" + project + "-chat")
    current = claim()
    current["execution_binding"] = {
        "model_id": "model1", "model_revision": 1,
        "credential_version_id": "cred1", "credential_version": 1, "credential_fingerprint": "fixture",
        "protocol": "openai-chat-completions",
        "model_name": "fixture", "base_url": "http://provider.invalid/v1", "inference_endpoint": "http://provider.invalid/v1/chat/completions",
        "auth_header": "authorization-bearer", "api_key": "synthetic-model-secret",
    }
    loop.client = SimpleNamespace(
        pending_agent_turns=Mock(return_value=ApiResult(200, [
            {"project_id": "p1", "conversation_id": "c1", "execution_generation": 0},
            {"project_id": "p2", "conversation_id": "c2", "execution_generation": 0},
        ])),
        get_project=Mock(side_effect=AssertionError("聊天不能解析或清理研究 Worker")),
        claim_agent_turn=Mock(side_effect=lambda cid, rid: ApiResult(200, {**current, "request_id": rid})),
        agent_turn_action=Mock(side_effect=[ApiResult(0), ApiResult(200, {})]),
    )
    execute = Mock(return_value={"status": "succeeded", "text": "你好", "error_code": None, "usage": None})
    monkeypatch.setattr("cairn.dispatcher.scheduler.agent_chat.run_agent_turn", execute)
    research_started, release_research = threading.Event(), threading.Event()

    def research():
        research_started.set()
        assert release_research.wait(5)

    with ThreadPoolExecutor(max_workers=1) as loop.executor, ThreadPoolExecutor(max_workers=1) as loop.agent_executor:
        research_future = loop.executor.submit(research)
        loop.futures[research_future] = RunningTask("p1", "explore", "w1", "pi", TaskCancellation(), runtime())
        try:
            assert research_started.wait(1)
            loop._dispatch_agent_turns()
            future, task = next((future, task) for future, task in loop.futures.items() if task.task_type == "agent_chat")
            future.result(timeout=1)
            assert not research_future.done()
            assert task.worker_name == "project-agent"
            assert task.agent_message_id == 9 and type(task.agent_message_id) is int
            assert loop._worker_counts() == {"w1": 1}
            assert loop._ordinary_task_count() == 1
            assert loop._project_running_task_count("p1") == 1
            loop._dispatch_agent_turns()
            assert loop.client.claim_agent_turn.call_count == 1
            loop._reap_agent_turn(future, task)
            assert future in loop.futures
            loop._reap_agent_turn(future, task)
            assert list(loop.futures) == [research_future] and execute.call_count == 1
            assert loop.client.agent_turn_action.call_args_list[0] == loop.client.agent_turn_action.call_args_list[1]
            assert loop.client.agent_turn_action.call_args.args[1] == "9"
            assert not loop.futures[research_future].cancellation.is_cancelled
            loop.client.get_project.assert_not_called()
        finally:
            release_research.set()


def test_stopped_project_does_not_cancel_readonly_chat():
    cleanup = CleanupMixin()
    task = RunningTask("p1", "agent_chat", "w1", "pi", TaskCancellation(), runtime())
    cleanup.futures = {object(): task}
    cleanup._cancel_inactive_tasks([SimpleNamespace(id="p1", status="stopped")])
    assert not task.cancellation.is_cancelled
    cleanup._cancel_inactive_tasks([])
    assert not task.cancellation.is_cancelled


def test_chat_container_uses_persistent_private_mounts_without_research_topology(tmp_path):
    manager = ContainerManager.__new__(ContainerManager)
    manager._project_prefix = "owned-"
    manager._ensure_running_locks = {}
    manager._ensure_running_locks_guard = threading.Lock()
    manager.traffic_execution = SimpleNamespace(root=tmp_path / "traffic")
    manager._get_container = Mock(return_value=None)
    manager._require_created_running_container = Mock()
    manager.instance_id = "fixture"
    manager._client = SimpleNamespace(containers=SimpleNamespace(run=Mock()))
    name = manager.ensure_chat_running("p1", runtime())
    assert name == "owned-conversation-p1"
    volumes = manager._client.containers.run.call_args.kwargs["volumes"]
    assert {item["bind"] for item in volumes.values()} == {"/home/kali/workspace", "/home/kali/.cairn", "/tmp/cairn-prompts"}
    saved = manager.chat_directory("p1") / "control" / "session-fixture"
    saved.write_text("synthetic transcript")
    manager.ensure_chat_running("p1", runtime())
    assert saved.read_text() == "synthetic transcript"
    assert manager._client.containers.run.call_count == 2
    labels = manager._client.containers.run.call_args.kwargs["labels"]
    assert labels["cairn.conversation_id"] == "p1" and "cairn.project_id" not in labels
    assert labels["cairn.scenario_key"] == "conversation-chat"
    manager._startup_prefix = "startup-"
    container = SimpleNamespace(name=name, attrs={"Config": {"Labels": labels}, "State": {"Status": "running"}},
                                reload=Mock(), remove=Mock(), stop=Mock())
    manager._get_container.side_effect = lambda requested: container if requested == name else None
    manager._client.containers.list = Mock(return_value=[container])
    assert manager._owns_container(container)
    assert manager.project_id_from_container_name(name) is None
    assert manager.managed_container_names() == []
    assert manager.cleanup_orphan(name) is False
    assert name not in manager._project_container_names("p1")
    container.remove.assert_not_called()
    container.stop.assert_not_called()


@pytest.mark.parametrize("restart", [False, True])
def test_three_finish_failures_recover_exact_result_without_rerunning_model(tmp_path, monkeypatch, restart):
    from concurrent.futures import Future

    clock = [1000.0]
    monkeypatch.setattr("cairn.dispatcher.scheduler.agent_chat.time", SimpleNamespace(time=lambda: clock[0]))
    loop = finish_storage(AgentChatMixin(), tmp_path / "receipts")
    client, manager = Client(), Manager(tmp_path)
    config = SimpleNamespace(runtime=SimpleNamespace(interval=30), tasks=SimpleNamespace(reason=SimpleNamespace(timeout=10)))
    cancellation = TaskCancellation()
    result = run_agent_turn(config, client, manager, worker(), cancellation, claim(), runtime())
    assert result["status"] == "succeeded" and result["text"] == manager.answer
    future = Future()
    future.set_result(result)
    task = RunningTask("p1", "agent_chat", "w1", "pi", cancellation, runtime(),
                       task_run_id="tr1", agent_conversation_id="c1", agent_message_id=9)
    loop.futures = {future: task}
    finish = Mock(side_effect=[ApiResult(0), ApiResult(503), ApiResult(503), ApiResult(200)])
    loop.client = SimpleNamespace(
        agent_turn_action=finish,
        pending_agent_turns=Mock(return_value=ApiResult(200, [{"project_id": "p1", "conversation_id": "c1"}])),
        claim_agent_turn=Mock(side_effect=AssertionError("未确认回执不能重新认领会话")),
    )
    for _ in range(3):
        loop._reap_agent_turn(future, task)
    assert not loop.futures
    saved = next(iter(loop._pending_agent_finishes().values()))
    assert saved.result == result and saved.attempts == 3 and saved.state == "pending"
    receipt_path = loop._agent_finish_directory() / f"{saved.key}.json"
    assert json.loads(receipt_path.read_text())["message_id"] == "9"
    assert finish.call_args.args[1] == "9"
    loop._dispatch_agent_turns()
    assert finish.call_count == 3
    loop.client.claim_agent_turn.assert_not_called()
    if restart:
        resumed = finish_storage(AgentChatMixin(), tmp_path / "receipts")
        resumed.futures, resumed.client = {}, loop.client
        loop = resumed
        assert next(iter(loop._pending_agent_finishes().values())).attempts == 3
    clock[0] += 5
    loop._flush_agent_finishes()
    assert finish.call_count == 4
    assert all(call == finish.call_args_list[0] for call in finish.call_args_list)
    assert not loop._pending_agent_finishes() and not list(loop._agent_finish_directory().glob("*.json"))
    assert manager.starts == 1 and future.result() == result


def test_finish_protected_answer_retains_original_and_recovers_only_failure_receipt(tmp_path, monkeypatch):
    from concurrent.futures import Future

    clock = [1000.0]
    monkeypatch.setattr("cairn.dispatcher.scheduler.agent_chat.time", SimpleNamespace(time=lambda: clock[0]))
    loop = finish_storage(AgentChatMixin(), tmp_path)
    task = RunningTask("p1", "agent_chat", "w1", "pi", TaskCancellation(), runtime(),
                       task_run_id="tr1", agent_conversation_id="c1", agent_message_id=9)
    future = Future()
    original = {"status": "succeeded", "text": "synthetic-protected-answer", "error_code": None, "usage": None}
    future.set_result(original)
    loop.futures = {future: task}
    loop.client = SimpleNamespace(agent_turn_action=Mock(side_effect=[
        ApiResult(422, {"detail": "agent_public_content_blocked"}), ApiResult(0), ApiResult(503), ApiResult(503), ApiResult(200),
    ]))
    for _ in range(4):
        loop._reap_agent_turn(future, task)
    assert future.result() is original and not loop.futures
    restarted = finish_storage(AgentChatMixin(), tmp_path)
    restarted.futures, restarted.client = {}, loop.client
    receipt = next(iter(restarted._pending_agent_finishes().values()))
    assert receipt.result == original and receipt.attempts == 3
    clock[0] += 5
    restarted._flush_agent_finishes()
    assert not restarted._pending_agent_finishes()
    calls = loop.client.agent_turn_action.call_args_list
    assert len(calls) == 5 and calls[0].args[-1]["text"] == original["text"]
    for call in calls[1:]:
        assert call.args[-1] == {"task_run_id": "tr1", "status": "failed", "text": "",
                                "error_code": "public_output_forbidden", "usage": None,
                                "usage_missing_responses": None, "session_initialized": None}


@pytest.mark.parametrize("status", [0, 401, 403, 404, 409])
def test_finish_budget_and_rejections_persist_without_claiming_acceptance(tmp_path, monkeypatch, caplog, status):
    from concurrent.futures import Future

    clock = [1000.0]
    monkeypatch.setattr("cairn.dispatcher.scheduler.agent_chat.time", SimpleNamespace(time=lambda: clock[0]))
    loop = finish_storage(AgentChatMixin(), tmp_path)
    task = RunningTask("p1", "agent_chat", "w1", "pi", TaskCancellation(), runtime(),
                       task_run_id="tr1", agent_conversation_id="c1", agent_message_id=9)
    original = {"status": "succeeded", "text": "exact original", "error_code": None, "usage": None}
    future = Future()
    future.set_result(original)
    loop.futures = {future: task}
    loop.client = SimpleNamespace(agent_turn_action=Mock(return_value=ApiResult(status)))
    loop._reap_agent_turn(future, task)
    for _ in range(10):
        clock[0] += 60
        if future in loop.futures:
            loop._reap_agent_turn(future, task)
        else:
            loop._flush_agent_finishes()
    count = 6 if status == 0 else 1
    state = "exhausted" if status == 0 else "rejected"
    assert not loop.futures and loop.client.agent_turn_action.call_count == count
    restarted = finish_storage(AgentChatMixin(), tmp_path)
    restarted.futures, restarted.client = {}, loop.client
    restarted._flush_agent_finishes()
    receipt = next(iter(restarted._pending_agent_finishes().values()))
    assert receipt.state == state and receipt.result == original and receipt.response_status == status
    assert loop.client.agent_turn_action.call_count == count
    assert f"code=chat_finish_{state}" in caplog.text


@pytest.mark.parametrize("failure", ["schema", "io"])
def test_finish_invalid_receipt_and_io_errors_preserve_future_without_log_spam(tmp_path, monkeypatch, caplog, failure):
    from concurrent.futures import Future

    loop = finish_storage(AgentChatMixin(), tmp_path)
    task = RunningTask("p1", "agent_chat", "w1", "pi", TaskCancellation(), runtime(),
                       task_run_id="tr1", agent_conversation_id="c1", agent_message_id=9)
    original = {"status": "succeeded", "text": "synthetic-private-answer", "error_code": None, "usage": None}
    future = Future()
    future.set_result(original)
    loop.futures = {future: task}
    loop.client = SimpleNamespace(agent_turn_action=Mock(return_value=ApiResult(200)))
    save = loop._save_agent_finish
    if failure == "schema":
        task.agent_conversation_id = 42
    else:
        monkeypatch.setattr(loop, "_save_agent_finish", Mock(side_effect=OSError("synthetic-private-error")))
    for _ in range(3):
        loop._reap_agent_turn(future, task)
    code = "chat_finish_receipt_invalid" if failure == "schema" else "chat_finish_storage_unavailable"
    assert caplog.text.count(f"code={code}") == 1
    assert "synthetic-private" not in caplog.text
    assert future in loop.futures and future.result() is original
    assert not list(loop._agent_finish_directory().glob("*.json"))
    loop.client.agent_turn_action.assert_not_called()
    task.agent_conversation_id = "c1"
    monkeypatch.setattr(loop, "_save_agent_finish", save)
    loop._reap_agent_turn(future, task)
    assert not loop.futures and not loop._log_state
    assert loop.client.agent_turn_action.call_args.args[-1] == {"task_run_id": "tr1", **original}


@pytest.mark.parametrize("failure", ["schema", "io"])
def test_finish_load_errors_block_claim_and_log_only_changes(tmp_path, monkeypatch, caplog, failure):
    loop = finish_storage(AgentChatMixin(), tmp_path)
    loop.futures = {}
    loop.client = SimpleNamespace(pending_agent_turns=Mock(return_value=ApiResult(200, [])))
    directory = loop._agent_finish_directory()
    directory.mkdir(parents=True)
    path = directory / "invalid.json"
    load = loop._pending_agent_finishes
    if failure == "schema":
        path.write_text(json.dumps({"message_id": 9, "result": {"text": "synthetic-private-answer"}}))
    else:
        monkeypatch.setattr(loop, "_pending_agent_finishes", Mock(side_effect=OSError("synthetic-private-error")))
    for _ in range(3):
        loop._dispatch_agent_turns()
    code = "chat_finish_receipt_invalid" if failure == "schema" else "chat_finish_storage_unavailable"
    assert caplog.text.count(f"code={code}") == 1
    assert "synthetic-private" not in caplog.text
    loop.client.pending_agent_turns.assert_not_called()
    if failure == "schema":
        assert path.exists()
        path.unlink()
    monkeypatch.setattr(loop, "_pending_agent_finishes", load)
    loop._dispatch_agent_turns()
    loop.client.pending_agent_turns.assert_called_once()
    assert not loop._log_state


@pytest.mark.parametrize("failure", ["return", "start", "communicate"])
def test_unconfirmed_execution_keeps_running_slot_without_terminal_finish(tmp_path, monkeypatch, caplog, failure):
    from concurrent.futures import Future
    client, manager = Client(), Manager(tmp_path)
    build = manager.build_exec_process

    def unconfirmed(*args, **kwargs):
        process = build(*args, **kwargs)
        process.execution_completed = False
        if failure != "return":
            setattr(process, failure, Mock(side_effect=RuntimeError("synthetic-private-error")))
        return process

    monkeypatch.setattr(manager, "build_exec_process", unconfirmed)
    config = SimpleNamespace(runtime=SimpleNamespace(interval=30), tasks=SimpleNamespace(reason=SimpleNamespace(timeout=10)))
    cancellation = TaskCancellation()
    result = run_agent_turn(config, client, manager, worker(), cancellation, claim(), runtime())
    assert result["status"] == ("cancelled" if failure == "return" else "failed")
    assert result["text"] == ""
    assert result["error_code"] == ("execution_cancelled" if failure == "return" else "chat_execution_failed")
    assert cancellation.execution_lifecycle(task_finished=True)["termination_confirmed"] is False
    task = RunningTask("p1", "agent_chat", "w1", "pi", cancellation, runtime(),
                       task_run_id="tr1", agent_conversation_id="c1", agent_message_id=9)
    future = Future()
    future.set_result(result)
    loop = AgentChatMixin()
    loop.futures, loop.client = {future: task}, client
    loop._reap_agent_turn(future, task)
    loop._reap_agent_turn(future, task)
    assert future in loop.futures and task.agent_execution_unconfirmed
    assert not any(call[3] == "finish" for call in client.calls)
    assert caplog.text.count("code=execution_unconfirmed") == 1
    assert "synthetic-private-error" not in caplog.text


@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.parametrize("execution_error", [False, True])
def test_research_close_result_survives_finally_and_real_scheduler_reap(tmp_path, monkeypatch, confirmed, execution_error):
    from docker.errors import DockerException

    from cairn.dispatcher.runtime.chat_workspace import ChatWorkspace
    from cairn.dispatcher.runtime.traffic_execution import TaskTrafficExecution
    from cairn.dispatcher.scheduler.loop import DispatcherLoop

    client, manager = Client(), Manager(tmp_path)
    cancellation = TaskCancellation()
    stop_scope = Mock(side_effect=[DockerException("synthetic-stop-unavailable"), {"termination_confirmed": confirmed}])
    research = TaskTrafficExecution(SimpleNamespace(stop_scope=stop_scope), "scope1", cancellation)

    def workspace(*args):
        workspace = ChatWorkspace(*args)
        workspace.research_execution = research
        return workspace

    monkeypatch.setattr("cairn.dispatcher.tasks.agent_chat.ChatWorkspace", workspace)
    if execution_error:
        monkeypatch.setattr(manager, "build_exec_process", Mock(side_effect=RuntimeError("synthetic-private-error")))
    config = SimpleNamespace(runtime=SimpleNamespace(interval=30), tasks=SimpleNamespace(reason=SimpleNamespace(timeout=10)))
    loop = finish_storage(DispatcherLoop.__new__(DispatcherLoop), tmp_path / "receipts")
    loop.client = client
    client.pending_agent_turns = Mock(return_value=ApiResult(200, []))
    action = client.agent_turn_action
    saved_results = []

    def verify_finish(conversation, message, operation, body):
        if operation == "finish":
            saved = json.loads(next(loop._agent_finish_directory().glob("*.json")).read_bytes())
            assert saved["conversation_id"] == conversation == "c1"
            assert saved["message_id"] == message == "9"
            assert saved["task_run_id"] == body["task_run_id"] == "tr1"
            assert body == {"task_run_id": "tr1", **saved["result"]}
            saved_results.append(saved["result"])
        return action(conversation, message, operation, body)

    client.agent_turn_action = verify_finish
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run_agent_turn, config, client, manager, worker(), cancellation, claim(), runtime())
        task = RunningTask(None, "agent_chat", "w1", "pi", cancellation, runtime(),
                           task_run_id="tr1", agent_conversation_id="c1", agent_message_id=9)
        loop.futures = {future: task}
        result = future.result(timeout=5)
        assert result["status"] == ("failed" if execution_error else "succeeded")
        assert result["text"] == ("" if execution_error else manager.answer)
        assert result["error_code"] == ("chat_execution_failed" if execution_error else None)
        assert stop_scope.call_count == 2
        assert cancellation.execution_lifecycle(task_finished=True)["termination_confirmed"] is confirmed
        loop._reap_futures()
        loop._reap_futures()
        loop._dispatch_agent_turns()

    if confirmed:
        assert saved_results == [result]
        assert not loop.futures and not loop._pending_agent_finishes()
        assert not list(loop._agent_finish_directory().glob("*.json"))
        client.pending_agent_turns.assert_called_once()
    else:
        assert not saved_results and future in loop.futures and task.agent_execution_unconfirmed
        assert not any(call[3] == "finish" for call in client.calls)
        assert not list(loop._agent_finish_directory().glob("*.json"))
        client.pending_agent_turns.assert_not_called()
    assert stop_scope.call_count == 2
