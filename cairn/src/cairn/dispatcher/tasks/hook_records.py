"""将 pi stderr 结构化事件流保存为原始 Artifact 并投影工具审计。"""

from __future__ import annotations

from contextlib import contextmanager
from io import BytesIO
from typing import BinaryIO, Iterator

from .common_base import *


@contextmanager
def _open_pi_stderr(result: ProcessResult) -> Iterator[BinaryIO]:
    # 文件型输出保持流式；inline 只用于小型结果和注入式测试替身。
    if result._stderr_path is not None:
        with open(result._stderr_path, "rb") as stream:
            yield stream
    else:
        with BytesIO(result.stderr.encode("utf-8")) as stream:
            yield stream


def _pi_tool_events(
    stream: BinaryIO,
    *,
    project_id: str,
    task_run_id: str,
    phase: str,
    session: str | None,
    artifact_id: str,
) -> Iterator[dict[str, object]]:
    while line := stream.readline(256 * 1024 + 1):
        if len(line) > 256 * 1024:
            # 非协议长行仍保存在原始 Artifact，不能把其尾部误当下一条事件。
            while line and not line.endswith(b"\n"):
                line = stream.readline(256 * 1024 + 1)
            continue
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(event, dict) or event.get("protocol") != "cairn-agent-audit-v1":
            continue
        if any(event.get(key) != value for key, value in (
            ("project_id", project_id), ("task_run_id", task_run_id),
            ("phase", phase), ("session", session),
        )):
            continue
        name = event.get("event")
        if name not in {"tool_execution_start", "tool_execution_end"}:
            continue
        tool = event.get("tool_name")
        operation_id = event.get("tool_call_id")
        if not isinstance(tool, str) or not tool or not isinstance(operation_id, str) or not operation_id:
            continue
        status = "started" if name == "tool_execution_start" else (
            "failed" if event.get("is_error") is True else "succeeded"
        )
        payload = {
            "hook_source": "pi",
            "hook_event_name": name,
            "session_id": session,
            "tool_name": tool,
            "operation_id": operation_id,
            "status": status,
            "recorded_at": event.get("timestamp"),
            "phase": phase,
            "artifact_id": artifact_id,
            "summary": f"{phase} / {name} / {tool}",
        }
        # SDK 不向审计流复制工具参数和结果正文，投影也不反向填充敏感内容。
        for key in ("input_bytes", "output_bytes", "input_sha256", "output_sha256"):
            if key in event:
                payload[key] = event[key]
        yield payload


def record_pi_tool_events(
    client: CairnClient | None,
    project_id: str | None,
    task_run_id: str | None,
    *,
    phase: str,
    session: str | None,
    result: ProcessResult,
) -> None:
    if client is None or project_id is None or task_run_id is None or result.stderr_blank:
        return
    try:
        with _open_pi_stderr(result) as stream:
            response = client.create_artifact(
                project_id,
                task_run_id=task_run_id,
                kind="hook_log",
                content=stream,
                filename=f"{phase}-pi-events.jsonl",
                metadata={"phase": phase, "hook_source": "pi", "stream": "stderr"},
            )
        if not response.ok:
            LOG.warning("pi audit artifact failed project=%s task_run=%s phase=%s", project_id, task_run_id, phase)
            return
        artifact_id = _artifact_id_from_response(response.data)
        if artifact_id is None:
            return
        with _open_pi_stderr(result) as stream:
            for payload in _pi_tool_events(
                stream, project_id=project_id, task_run_id=task_run_id,
                phase=phase, session=session, artifact_id=artifact_id,
            ):
                response = client.create_agent_event(
                    project_id, task_run_id=task_run_id, kind="hook_operation", payload=payload,
                )
                if not response.ok:
                    LOG.warning("pi audit projection failed project=%s task_run=%s phase=%s", project_id, task_run_id, phase)
                    return
    except Exception as exc:
        # 采集失败不能吞掉执行结果；任务层仍独立保存 stdout/stderr 原始输出。
        LOG.warning("pi audit collection failed project=%s task_run=%s phase=%s error=%s", project_id, task_run_id, phase, type(exc).__name__)
