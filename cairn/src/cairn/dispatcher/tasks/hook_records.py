"""将 pi stderr 结构化事件流保存为原始 Artifact 并投影工具审计。"""

from __future__ import annotations

from contextlib import contextmanager
from io import BytesIO
import re
import threading
import time
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
    artifact_id: str | None,
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
        if name == "instructions_loaded":
            digest, size = event.get("prompt_hash"), event.get("prompt_bytes")
            if (event.get("prompt_name") == "AGENTS.md" and isinstance(digest, str)
                    and re.fullmatch(r"[0-9a-f]{64}", digest) and type(size) is int and size > 0):
                yield {
                    "hook_source": "pi", "hook_event_name": name, "session_id": session,
                    "phase": phase, "artifact_id": artifact_id, "prompt_group": "runner",
                    "prompt_name": "AGENTS.md", "prompt_hash": digest, "prompt_bytes": size,
                }
            continue
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


class PiToolEventRecorder:
    """独立尾读现有 stderr 文件；上传慢或失败均不反压 Docker 输出。"""

    def __init__(self, client: CairnClient, project_id: str, task_run_id: str, *, phase: str, session: str | None):
        self.client = client
        self.project_id = project_id
        self.task_run_id = task_run_id
        self.phase = phase
        self.session = session
        self.incomplete = False
        self._finished = threading.Event()
        self._deadline: float | None = None
        self._thread: threading.Thread | None = None

    def start(self, path: str) -> None:
        try:
            self._thread = threading.Thread(target=self._observe, args=(path,), daemon=True)
            self._thread.start()
        except Exception as exc:
            # 线程资源不足同样属于观测失败，不能让收尾 join 未启动线程影响任务结果。
            self._thread = None
            self.incomplete = True
            LOG.warning("pi live audit start failed project=%s task_run=%s phase=%s error=%s", self.project_id, self.task_run_id, self.phase, type(exc).__name__)

    def finish(self) -> None:
        # 收尾只排空同一游标，不再从头投影；含网络 ACK 未知的事件也不盲目重发。
        if self._finished.is_set():
            return
        self._deadline = time.monotonic() + 2.0
        self._finished.set()
        if self._thread is not None:
            self._thread.join(timeout=2.1)
        if self._thread is None or self._thread.is_alive():
            self.incomplete = True
        if self.incomplete:
            LOG.warning("pi live audit incomplete project=%s task_run=%s phase=%s", self.project_id, self.task_run_id, self.phase)

    def _observe(self, path: str) -> None:
        pending = bytearray()
        skipping = False
        seen: set[tuple[object, ...]] = set()
        try:
            with open(path, "rb") as stream:
                while self._deadline is None or time.monotonic() < self._deadline:
                    part = stream.readline(256 * 1024 + 1)
                    if part:
                        if not skipping:
                            pending.extend(part)
                            if len(pending) > 256 * 1024:
                                pending.clear()
                                skipping = True
                        if not part.endswith(b"\n"):
                            continue
                    elif not self._finished.is_set():
                        self._finished.wait(0.1)
                        continue
                    # 半行只在真实 EOF 收尾后解析；超长非协议行原样留在 stderr。
                    if not skipping and pending:
                        for payload in _pi_tool_events(
                            BytesIO(pending), project_id=self.project_id, task_run_id=self.task_run_id,
                            phase=self.phase, session=self.session, artifact_id=None,
                        ):
                            key = (payload["hook_event_name"], payload.get("operation_id"), payload.get("prompt_hash"))
                            if key in seen:
                                continue
                            seen.add(key)
                            kind = "prompt_rendered" if payload["hook_event_name"] == "instructions_loaded" else "hook_operation"
                            response = self.client.create_agent_event(
                                self.project_id, task_run_id=self.task_run_id, kind=kind, payload=payload, timeout=1.0,
                            )
                            if not response.ok:
                                self.incomplete = True
                                # 不反复请求故障端点；完整已保存原文仍走原有 Artifact 链。
                                return
                    pending.clear()
                    skipping = False
                    if not part:
                        return
                self.incomplete = True
        except Exception as exc:
            self.incomplete = True
            LOG.warning("pi live audit failed project=%s task_run=%s phase=%s error=%s", self.project_id, self.task_run_id, self.phase, type(exc).__name__)


def record_pi_tool_events(
    client: CairnClient | None,
    project_id: str | None,
    task_run_id: str | None,
    *,
    phase: str,
    session: str | None,
    result: ProcessResult,
    live_recorder: PiToolEventRecorder | None = None,
) -> None:
    if client is None or project_id is None or task_run_id is None or result.stderr_blank:
        return
    artifact_id = None
    try:
        with _open_pi_stderr(result) as stream:
            response = client.create_artifact(
                project_id,
                task_run_id=task_run_id,
                kind="hook_log",
                content=stream,
                filename=f"{phase}-pi-events.jsonl",
                metadata={
                    "phase": phase, "hook_source": "pi", "stream": "stderr",
                    "session_id": session,
                    "projection_complete": not live_recorder.incomplete if live_recorder is not None else None,
                },
            )
        if not response.ok:
            LOG.warning("pi audit artifact failed project=%s task_run=%s phase=%s", project_id, task_run_id, phase)
            return
        artifact_id = _artifact_id_from_response(response.data)
        if artifact_id is None:
            return
        if live_recorder is not None:
            # 增量观测复用同一游标，收尾不重新投影开始／结束。
            return
        with _open_pi_stderr(result) as stream:
            for payload in _pi_tool_events(
                stream, project_id=project_id, task_run_id=task_run_id,
                phase=phase, session=session, artifact_id=artifact_id,
            ):
                kind = "prompt_rendered" if payload["hook_event_name"] == "instructions_loaded" else "hook_operation"
                response = client.create_agent_event(
                    project_id, task_run_id=task_run_id, kind=kind, payload=payload,
                )
                if not response.ok:
                    LOG.warning("pi audit projection failed project=%s task_run=%s phase=%s", project_id, task_run_id, phase)
                    return
    except Exception as exc:
        # 采集失败不能吞掉执行结果；任务层仍独立保存 stdout/stderr 原始输出。
        LOG.warning("pi audit collection failed project=%s task_run=%s phase=%s error=%s", project_id, task_run_id, phase, type(exc).__name__)
    finally:
        if live_recorder is not None:
            # 原件上传失败也尝试明确观测缺口；标记自身失败只记诊断，不覆盖任务结果。
            incomplete = live_recorder.incomplete or artifact_id is None
            try:
                response = client.create_agent_event(
                    project_id, task_run_id=task_run_id, kind="hook_operation",
                    payload={
                        "hook_source": "pi", "phase": phase, "session_id": session,
                        "hook_event_name": "observation_incomplete" if incomplete else "observation_saved",
                        "status": "unknown" if incomplete else "succeeded",
                        "artifact_id": artifact_id,
                    }, timeout=1.0,
                )
                if not response.ok:
                    LOG.warning("pi audit source link failed project=%s task_run=%s phase=%s", project_id, task_run_id, phase)
            except Exception as exc:
                LOG.warning("pi audit source link failed project=%s task_run=%s phase=%s error=%s", project_id, task_run_id, phase, type(exc).__name__)
