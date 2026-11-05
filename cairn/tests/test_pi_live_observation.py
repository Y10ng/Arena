"""离线贯通真实进程排空、增量投影与 PostgreSQL 正式审计读取，不启动模型或目标访问。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import threading
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.process import ManagedProcess
from cairn.dispatcher.tasks.hook_records import PiToolEventRecorder
from cairn.dispatcher.tasks.process_runner import run_worker_process
from cairn.dispatcher.workers.base import DriverResult
from cairn.server import db
from cairn.server.models import CreateAgentEventRequest
from cairn.server.services import audit_events
from test_assessment_migration_v34 import private_postgres


@pytest.fixture
def audit_database(private_postgres, monkeypatch):
    name = f"live_audit_{uuid4().hex}"
    with psycopg.connect(**private_postgres, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
        @contextmanager
        def connect():
            with psycopg.connect(**{**private_postgres, "dbname": name}, row_factory=dict_row) as raw:
                yield db.PostgresConnection(raw)
        try:
            with connect() as conn:
                db.run_migrations(conn, "postgresql")
                conn.execute("INSERT INTO projects(id,title,status,created_at,tenant_id) VALUES ('p','synthetic','active','2026-01-01','tenant_legacy')")
                conn.execute("INSERT INTO task_runs(id,project_id,task_type,worker_name,worker_type,status,started_at) VALUES ('r','p','explore','synthetic','pi','running','2026-01-01')")
            monkeypatch.setattr(audit_events, "get_conn", connect)
            yield connect
        finally:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def audit_line(event, call="call-1", **extra):
    return (json.dumps({
        "protocol": "cairn-agent-audit-v1", "project_id": "p", "task_run_id": "r",
        "phase": "explore_execute", "session": "pi:synthetic", "event": event,
        "tool_call_id": call, "tool_name": "read", "timestamp": "2026-01-01T00:00:01Z",
        **extra,
    }) + "\n").encode()


def test_observation_thread_start_failure_does_not_break_finish(monkeypatch):
    def unavailable_thread(self):
        raise RuntimeError("synthetic thread unavailable")

    monkeypatch.setattr(threading.Thread, "start", unavailable_thread)
    recorder = PiToolEventRecorder(SimpleNamespace(), "p", "r", phase="explore_execute", session="pi:synthetic")
    recorder.start("unused-stderr-path")
    recorder.finish()
    recorder.finish()
    assert recorder.incomplete


@pytest.mark.parametrize("failure", [None, "exception", "slow", "artifact"])
def test_live_observation_is_readable_before_exit_and_never_blocks_drain(audit_database, failure):
    started = audit_line("tool_execution_start")
    ended = audit_line("tool_execution_end", is_error=False)
    # 包含半行、重复 start 和超长普通诊断；原件必须逐字保留。
    raw = started + started + b"x" * (270 * 1024) + b"\n" + ended
    upload_entered = threading.Event()
    release_upload = threading.Event()
    events_saved = threading.Event()
    drained = threading.Event()
    exit_process = threading.Event()
    uploaded = []
    hook_attempts = []

    class API:
        def exec_create(self, container_id, command, **kwargs):
            return {"Id": "control" if kwargs.get("stdout") is False else "exec"}

        def exec_start(self, exec_id, **kwargs):
            if exec_id == "control":
                return iter([])
            def stream():
                yield (None, started[:35])
                yield (b"unchanged-output", raw[35:])
                drained.set()
                assert exit_process.wait(10)
            return stream()

        def exec_inspect(self, exec_id):
            return {"Running": False, "ExitCode": 0}

    class Client:
        def create_agent_event(self, project_id, **kwargs):
            payload = kwargs["payload"]
            event = payload.get("hook_event_name")
            if event in {"tool_execution_start", "tool_execution_end"}:
                assert kwargs["timeout"] == 1.0
                hook_attempts.append(event)
                upload_entered.set()
                if failure == "slow":
                    assert release_upload.wait(10)
                    return ApiResult(status_code=503)
                if failure == "exception":
                    raise TimeoutError("synthetic observation timeout")
            result = audit_events.create_agent_event_record(project_id, CreateAgentEventRequest(
                task_run_id=kwargs["task_run_id"], kind=kwargs["kind"], payload=payload,
            ))
            if event == "tool_execution_end":
                events_saved.set()
            return ApiResult(status_code=201, data={"id": result.id})

        def create_artifact(self, project_id, **kwargs):
            uploaded.append((kwargs["content"].read(), kwargs["metadata"]))
            return ApiResult(status_code=503) if failure == "artifact" else ApiResult(status_code=201, data={"id": "raw-audit"})

    container = SimpleNamespace(id="synthetic", client=SimpleNamespace(api=API()))
    manager = SimpleNamespace(build_exec_process=lambda name, env, argv, **kwargs: ManagedProcess(container, argv, env))
    worker = SimpleNamespace(name="synthetic", type="pi", env={})
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            run_worker_process, manager, "synthetic", worker,
            DriverResult(argv=["synthetic"], session="pi:synthetic"),
            phase="explore_execute", timeout_seconds=30,
            project_id="p", task_run_id="r", client=Client(),
        )
        try:
            assert upload_entered.wait(5)
            assert drained.wait(5), "网络观测不得阻塞 stderr/stdout 排空"
            assert not future.done()
            if failure in (None, "artifact"):
                assert events_saved.wait(5)
                events = audit_events.list_task_run_hook_event_records("p", ["r"])[0].events
                assert [event.hook_event_name for event in events] == ["tool_execution_start", "tool_execution_end"]
                assert {event.operation_id for event in events} == {"call-1"}
                assert all(event.artifact_id is None for event in events)
                with audit_database() as conn:
                    assert conn.execute("SELECT status FROM task_runs WHERE id='r'").fetchone()["status"] == "running"
        finally:
            exit_process.set()
            # slow 分支故意不释放上传：验证 finish 的有限预算，而不等待端点恢复。
            if failure != "slow":
                release_upload.set()
        try:
            result = future.result(timeout=5)
            try:
                assert result.returncode == 0
                assert not result.cancelled
                assert result.read_stdout() == "unchanged-output"
                assert result.read_stderr().encode() == raw
                assert uploaded[0][0] == raw
                projected = failure in (None, "artifact")
                assert uploaded[0][1]["projection_complete"] is projected
                events = audit_events.list_task_run_hook_event_records("p", ["r"])[0].events
                expected = ["tool_execution_start", "tool_execution_end"] if projected else []
                expected.append("observation_saved" if failure is None else "observation_incomplete")
                assert [event.hook_event_name for event in events] == expected
                assert events[-1].artifact_id == (None if failure == "artifact" else "raw-audit")
                assert hook_attempts == (["tool_execution_start", "tool_execution_end"] if projected else ["tool_execution_start"])
            finally:
                result.cleanup()
        finally:
            release_upload.set()
