from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.chat_workspace import ChatWorkspace
from cairn.dispatcher.runtime.project_tools import ProjectToolBinding


def workspace(tmp_path):
    container = Mock()
    container.wait.return_value = {"StatusCode": 0}
    container.logs.return_value = [b"local output\n"]
    manager = SimpleNamespace(
        chat_directory=lambda _: tmp_path,
        chat_container_name=lambda _: "owned-chat",
        _require_container=Mock(return_value=SimpleNamespace(attrs={"Config": {"Image": "worker:local"}})),
        _client=SimpleNamespace(containers=SimpleNamespace(create=Mock(return_value=container))),
    )
    return ChatWorkspace(manager, "p1", "c1"), manager, container


def test_attachment_bridge_and_markdown_use_real_workspace_and_sdk_identity(tmp_path):
    local, _, _ = workspace(tmp_path)
    calls = []

    def action(project, conversation, message, operation, body):
        calls.append(body)
        assert (project, conversation, message, operation) == ("p1", "c1", "m1", "tools")
        assert body["task_run_id"] == "tr1"
        if body["method"] == "_read_conversation_attachment_bytes":
            assert body["params"] == {"attachment_id": "a1"}
            return ApiResult(200, {"attachment": {"id": "a1", "size": len("# 原始资料\n".encode())}, "content_base64": base64.b64encode("# 原始资料\n".encode()).decode()})
        assert body["method"] == "publish_custom_markdown"
        assert body["params"] == {"title": "用户报告", "content": "# 原始资料\n", "sources": ["a1"]}
        return ApiResult(200, {"action_id": "action-1", "artifact_id": "artifact-1"})

    binding = ProjectToolBinding(SimpleNamespace(agent_turn_action=action), "p1", "c1", "m1", "tr1", [])
    binding.workspace = local
    attachment = binding.dispatch({"method": "read_conversation_attachment", "params": {"attachment_id": "a1", "workspace_path": "inputs/data.md"}, "tool_call_id": "real-read-call"})
    assert attachment["workspace_path"] == "/home/kali/workspace/c1/inputs/data.md"
    assert "content_base64" not in attachment
    result = binding.dispatch({"method": "publish_custom_markdown", "params": {"title": "用户报告", "workspace_path": attachment["workspace_path"], "sources": ["a1"]}, "tool_call_id": "real-publish-call"})
    assert result["artifact_id"] == "artifact-1"
    assert [call["tool_call_id"] for call in calls] == ["real-read-call", "real-publish-call"]
    assert local.read("inputs/data.md") == "# 原始资料\n"
    with pytest.raises(ValueError, match="project_tool_request_invalid"):
        binding.dispatch({"method": "_read_conversation_attachment_bytes", "params": {"attachment_id": "a1"}, "tool_call_id": "bypass"})


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
    container.remove.assert_called_once_with(force=True)
    local.close()
    with pytest.raises(ValueError, match="closed"):
        local.execute({"command": "pwd", "cwd": local.cwd, "timeout_seconds": 1})
