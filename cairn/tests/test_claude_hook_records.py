"""pi 工具审计失败不吞掉执行结果，原始流由任务生命周期统一清理。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks import hook_records


class _HookClient:
    def __init__(self, failure: str) -> None:
        self.failure = failure
        self.uploaded = b""
        self.events: list[dict[str, object]] = []

    def create_artifact(self, project_id: str, **kwargs) -> ApiResult:
        self.uploaded = kwargs["content"].read()
        assert project_id == "project-1"
        assert kwargs["metadata"] == {
            "phase": "explore_execute", "hook_source": "pi", "stream": "stderr",
        }
        if self.failure == "artifact_exception":
            raise RuntimeError("fixture upload failure")
        if self.failure == "artifact":
            return ApiResult(status_code=500, text="failed")
        if self.failure == "missing_artifact_id":
            return ApiResult(status_code=201, data={})
        return ApiResult(status_code=201, data={"id": "audit-artifact"})

    def create_agent_event(self, project_id: str, **kwargs) -> ApiResult:
        self.events.append(kwargs["payload"])
        if self.failure == "event_exception":
            raise RuntimeError("fixture event failure")
        return ApiResult(status_code=500, text="failed")


@pytest.mark.parametrize("failure", [
    "artifact", "artifact_exception", "missing_artifact_id", "event", "event_exception",
])
def test_pi_audit_failure_preserves_raw_stream_and_stops_partial_projection(
    tmp_path: Path, failure: str,
) -> None:
    content = b"".join((json.dumps({
        "protocol": "cairn-agent-audit-v1", "project_id": "project-1",
        "task_run_id": "run-1", "phase": "explore_execute", "session": "pi:session",
        "event": "tool_execution_start", "tool_name": "read",
        "tool_call_id": f"call-{index}", "input_bytes": 17,
    }) + "\n").encode() for index in range(2))
    path = tmp_path / "stderr.jsonl"
    path.write_bytes(content)
    result = ProcessResult(0, "unchanged-result", "preview", _stderr_path=str(path))
    client = _HookClient(failure)
    try:
        hook_records.record_pi_tool_events(
            client, "project-1", "run-1", phase="explore_execute", session="pi:session", result=result,
        )
        assert client.uploaded == content
        assert path.read_bytes() == content
        assert result.returncode == 0
        assert result.stdout == "unchanged-result"
        expected_events = 1 if failure.startswith("event") else 0
        assert len(client.events) == expected_events
        if client.events:
            assert client.events[0]["artifact_id"] == "audit-artifact"
            assert client.events[0]["tool_name"] == "read"
            assert client.events[0]["hook_source"] == "pi"
    finally:
        result.cleanup()
    assert not path.exists()
