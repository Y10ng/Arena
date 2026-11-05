from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from docker.errors import APIError, NotFound

from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.chat_workspace import ChatWorkspace
from cairn.dispatcher.runtime.project_tools import ProjectToolBinding


def workspace(tmp_path):
    container = Mock(id="a" * 64)
    container.wait.return_value = {"StatusCode": 0}
    container.logs.return_value = [b"local output\n"]
    manager = SimpleNamespace(
        chat_directory=lambda _: tmp_path,
        chat_container_name=lambda _: "owned-chat",
        _require_container=Mock(return_value=SimpleNamespace(attrs={"Config": {"Image": "worker:local"}})),
        _client=SimpleNamespace(containers=SimpleNamespace(
            create=Mock(return_value=container), get=Mock(side_effect=NotFound("removed")),
        )),
    )
    return ChatWorkspace(manager, "c1"), manager, container


def test_attachment_bridge_and_markdown_use_real_workspace_and_sdk_identity(tmp_path):
    local, _, _ = workspace(tmp_path)
    calls = []
    reference = {"method": "_read_conversation_attachment_bytes", "params": {"attachment_id": "a1"}, "expected_version": "a" * 64}

    def action(conversation, message, operation, body):
        calls.append(body)
        assert (conversation, message, operation) == ("c1", "m1", "tools")
        assert body["task_run_id"] == "tr1"
        if body["method"] == "_read_conversation_attachment_bytes":
            assert body["params"] == {"attachment_id": "a1"}
            return ApiResult(200, {"attachment": {"id": "a1", "size": len("# 原始资料\n".encode())}, "content_base64": base64.b64encode("# 原始资料\n".encode()).decode(), "source_reference": reference, "source_scope": "完整附件字节"})
        assert body["method"] == "publish_custom_markdown"
        assert body["params"] == {"title": "用户报告", "content": "# 原始资料\n", "source_references": [reference]}
        return ApiResult(200, {"action_id": "action-1", "artifact_id": "artifact-1"})

    binding = ProjectToolBinding(SimpleNamespace(agent_turn_action=action), None, "c1", "m1", "tr1", [])
    binding.workspace = local
    attachment = binding.dispatch({"method": "read_conversation_attachment", "params": {"attachment_id": "a1", "workspace_path": "inputs/data.md"}, "tool_call_id": "real-read-call"})
    assert attachment["workspace_path"] == "/home/kali/workspace/c1/inputs/data.md"
    assert "content_base64" not in attachment
    assert attachment["source_reference"] == reference and attachment["source_scope"] == "完整附件字节"
    result = binding.dispatch({"method": "publish_custom_markdown", "params": {"title": "用户报告", "workspace_path": attachment["workspace_path"], "source_references": [reference]}, "tool_call_id": "real-publish-call"})
    assert result["artifact_id"] == "artifact-1"
    assert [call["tool_call_id"] for call in calls] == ["real-read-call", "real-publish-call"]
    assert local.read("inputs/data.md") == "# 原始资料\n"
    with pytest.raises(ValueError, match="project_tool_request_invalid"):
        binding.dispatch({"method": "_read_conversation_attachment_bytes", "params": {"attachment_id": "a1"}, "tool_call_id": "bypass"})


def test_workspace_bridge_validates_claim_without_reading_project_details():
    client = SimpleNamespace(agent_turn_action=Mock(return_value=ApiResult(200, {"active": True})))
    binding = ProjectToolBinding(client, None, "c1", "m1", "tr1", [])
    binding.workspace = SimpleNamespace(execute=Mock(return_value={"output": "local", "exit_code": 0}))
    params = {"command": "printf local", "cwd": "/home/kali/workspace/c1", "timeout_seconds": 1}
    assert binding.dispatch({"method": "_workspace_bash", "params": params, "tool_call_id": "local"})["output"] == "local"
    client.agent_turn_action.assert_called_once_with("c1", "m1", "tools", {
        "task_run_id": "tr1", "method": "_validate_workspace_claim", "params": {}, "tool_call_id": "local",
    })
    binding.workspace.execute.assert_called_once_with(params)
    binding.workspace.execute.reset_mock()
    client.agent_turn_action.return_value = ApiResult(403, {"detail": {"code": "execution_owner_mismatch", "message": "执行归属不符"}})
    assert "error" in binding.dispatch({"method": "_workspace_bash", "params": params, "tool_call_id": "denied"})
    binding.workspace.execute.assert_not_called()
    for method in ("_validate_workspace_claim", "_admit_research_session_page"):
        with pytest.raises(ValueError, match="project_tool_request_invalid"):
            binding.dispatch({"method": method, "params": {}, "tool_call_id": "bypass"})


def test_workspace_rejects_traversal_symlinks_and_conflicting_overwrite(tmp_path):
    local, _, _ = workspace(tmp_path)
    local.write("input.txt", b"original")
    assert local.write("input.txt", b"original").endswith("/input.txt")
    with pytest.raises(ValueError, match="file_exists"):
        local.write("input.txt", b"replacement")
    (local.directory / "escape").symlink_to(tmp_path / "missing")
    for path in ["../control/secret", "/etc/passwd", "escape/secret"]:
        with pytest.raises(ValueError, match="path_invalid"):
            local.write(path, b"no")
    assert not (tmp_path / "missing").exists()


def test_command_container_only_mounts_current_conversation_and_is_removed(tmp_path):
    local, manager, container = workspace(tmp_path)
    result = local.execute({"command": "printf local", "cwd": local.cwd, "timeout_seconds": 3})
    assert result == {"exit_code": 0, "output": "local output\n"}
    args, kwargs = manager._client.containers.create.call_args
    assert args[0] == "worker:local"
    assert kwargs["network_mode"] == "none"
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges:true"]
    assert kwargs["volumes"] == {str(local.directory): {"bind": local.cwd, "mode": "rw"}}
    assert set(kwargs["environment"]) == {"HOME", "TMPDIR"}
    assert kwargs["labels"] == {"cairn.chat.workspace": "c1", "cairn.conversation_id": "c1"}
    manager._require_container.assert_called_once_with("owned-chat", conversation_id="c1")
    container.remove.assert_called_once_with(force=True)
    local.close()
    with pytest.raises(ValueError, match="closed"):
        local.execute({"command": "pwd", "cwd": local.cwd, "timeout_seconds": 1})
    manager._client.containers.get.assert_called_once_with(container.id)
    assert local.execution_completed


def test_workspace_unconfirmed_removal_blocks_terminal_and_still_stops_research(tmp_path):
    local, manager, container = workspace(tmp_path)
    cancellation = TaskCancellation()
    cancellation.register_resource(local)
    local._container = container
    container.remove.side_effect = APIError("temporary failure")
    research = SimpleNamespace(execution_completed=False, stop=Mock())
    local.research_execution = research
    local.close()
    research.stop.assert_called_once_with("chat_turn_finished")
    assert not cancellation.execution_lifecycle(task_finished=True)["termination_confirmed"]
    container.remove.side_effect = None
    manager._client.containers.get.side_effect = None
    manager._client.containers.get.return_value = container
    local.close()
    assert not local.execution_completed
    manager._client.containers.get.side_effect = NotFound("removed")
    local.close()
    assert not local.execution_completed
    research.execution_completed = True
    assert cancellation.execution_lifecycle(task_finished=True)["termination_confirmed"]


def test_late_workspace_registration_observes_existing_cancellation(tmp_path):
    import threading

    local, _, container = workspace(tmp_path)
    local._container = container
    stopped = threading.Event()
    container.remove.side_effect = lambda **_: stopped.set()
    cancellation = TaskCancellation()
    cancellation.cancel("chat_cancelled")
    cancellation.register_resource(local)
    assert stopped.wait(2)
    local.close()
    assert cancellation.execution_lifecycle(task_finished=True)["termination_confirmed"]
    with pytest.raises(ValueError, match="closed"):
        local.execute({"command": "pwd", "cwd": local.cwd, "timeout_seconds": 1})
