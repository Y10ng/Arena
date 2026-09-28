from __future__ import annotations

from contextlib import contextmanager
from json import dumps
import sqlite3

import pytest

from cairn.server import db
from cairn.server.routers import monitoring
from cairn.server.services.infra import dispatcher_effective_status
from cairn.server.schemas.events import TaskRunHookEvent
from cairn.server.services.event_payloads import hook_event_to_model
from cairn.server.services.payload_values import hook_event_name, hook_tool_name


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(db.SCHEMA)
    db._ensure_schema_migrations(conn)
    return conn


def insert_project(conn: sqlite3.Connection, project_id: str, title: str = "Project") -> None:
    conn.execute(
        "INSERT INTO projects (id, title, status, created_at) VALUES (?, ?, ?, ?)",
        (project_id, title, "active", "2026-06-08T00:00:00Z"),
    )


def insert_task_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    project_id: str,
    status: str,
    error_type: str | None = None,
    started_at: str = "2026-06-08T00:00:00Z",
    ended_at: str | None = "2026-06-08T00:01:00Z",
) -> None:
    conn.execute(
        """
        INSERT INTO task_runs
            (id, project_id, task_type, worker_name, worker_type, status, started_at, ended_at, error_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, project_id, "explore", "worker-a", "mock", status, started_at, ended_at, error_type),
    )


def test_dispatcher_summary_exposes_contract_fields_and_hard_windows() -> None:
    conn = make_conn()
    try:
        insert_project(conn, "p1")
        conn.execute(
            """
            INSERT INTO dispatcher_status
                (id, status, started_at, config_hash, last_loop_started_at, last_loop_finished_at,
                 last_loop_duration_ms, worker_cooldowns_json, worker_rejections_json,
                 worker_pool_json, error_message, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "dispatcher-test",
                "running",
                "2026-06-07T23:57:00Z",
                "abcdef1234567890",
                "2026-06-08T00:00:00Z",
                "2026-06-08T00:00:02Z",
                2000,
                dumps(
                    [
                        {
                            "worker_name": "worker-a",
                            "worker_type": "mock",
                            "error_type": "worker_timeout",
                            "retry_after_seconds": 300,
                            "retry_until_at": "2026-06-08T00:05:00Z",
                        }
                    ]
                ),
                dumps(
                    [
                        {
                            "project_id": "p1",
                            "task_type": "explore",
                            "worker_name": "worker-a",
                            "worker_type": "mock",
                            "error_type": "policy_denied",
                            "retry_after_seconds": 0,
                        }
                    ]
                ),
                dumps(
                    [
                        {
                            "worker_name": "worker-a",
                            "worker_type": "mock",
                            "task_types": ["explore"],
                            "priority": 0,
                            "max_running": 1,
                            "running_count": 0,
                        }
                    ]
                ),
                "server unreachable",
                "2026-06-07T23:58:00Z",
            ),
        )

        summary = monitoring._dispatcher_summary(conn, "2026-06-08T00:00:00Z")

        assert summary.config_hash == "abcdef1234567890"
        assert summary.started_at == "2026-06-07T23:57:00Z"
        assert summary.last_error_message == "server unreachable"
        assert summary.updated_at == "2026-06-07T23:58:00Z"
        assert summary.heartbeat_stale is True
        assert summary.effective_status == "stale"
        assert summary.worker_cooldowns[0].retry_after_seconds == 300
        assert summary.worker_rejections[0].error_type == "policy_denied"
    finally:
        conn.close()


def test_dispatcher_effective_status_ignores_empty_static_worker_pool() -> None:
    conn = make_conn()
    try:
        conn.execute(
            """
            INSERT INTO dispatcher_status
                (id, status, worker_pool_json, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            ("dispatcher-empty-pool", "idle", "[]", "2026-06-08T00:00:00Z"),
        )

        assert dispatcher_effective_status(
            conn,
            now="2026-06-08T00:00:30Z",
        ) == "idle"
        assert dispatcher_effective_status(
            conn,
            now="2026-06-08T00:01:01Z",
        ) == "stale"
    finally:
        conn.close()


def test_error_counts_exclude_cancelled_runs() -> None:
    conn = make_conn()
    try:
        insert_project(conn, "p1")
        insert_project(conn, "p2")
        insert_task_run(conn, run_id="tr001", project_id="p1", status="cancelled", error_type=None)
        insert_task_run(conn, run_id="tr001", project_id="p2", status="failed", error_type="worker_timeout")
        insert_task_run(conn, run_id="tr002", project_id="p2", status="timed_out", error_type="worker_timeout")

        counts = monitoring._error_counts(conn, "2026-06-08T00:10:00Z")

        assert [(item.error_type, item.count) for item in counts] == [("worker_timeout", 2)]
        assert counts[0].latest_task_run is not None
        assert counts[0].latest_task_run.status in {"failed", "timed_out"}
    finally:
        conn.close()


def test_dispatcher_profile_skips_expensive_summary_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    def fail_if_called(*args, **kwargs):
        raise AssertionError("expensive summary should not be called for dispatcher profile")

    try:
        monkeypatch.setattr(monitoring, "get_conn", fake_get_conn)
        monkeypatch.setattr(monitoring, "expire_workers", lambda conn: None)
        monkeypatch.setattr(monitoring, "expire_reason_leases", lambda conn: None)
        monkeypatch.setattr(monitoring, "_worker_summaries", fail_if_called)
        monkeypatch.setattr(monitoring, "_artifact_summary", fail_if_called)
        monkeypatch.setattr(monitoring, "_result_audit_summary", fail_if_called)
        monkeypatch.setattr(monitoring, "_prompt_effect_summary", fail_if_called)
        monkeypatch.setattr(monitoring, "_trend_summary", fail_if_called)
        monkeypatch.setattr(monitoring, "_agent_event_summary", fail_if_called)
        monkeypatch.setattr(monitoring, "_error_counts", fail_if_called)

        summary = monitoring.get_dispatcher_monitoring_summary(_principal=object())

        assert summary.dispatcher.project_count == 0
        assert summary.workers == []
        assert summary.artifacts.total_count == 0
    finally:
        conn.close()


@pytest.mark.parametrize("tool", [
    "read", "bash", "edit", "write", "grep", "find", "ls",
    "read_evidence", "register_artifact", "submit_result",
])
@pytest.mark.parametrize(("event", "status"), [
    ("tool_execution_start", "started"),
    ("tool_execution_end", "succeeded"),
    ("tool_execution_end", "failed"),
])
def test_pi_tool_audit_preserves_native_names_through_schema_and_projection(
    tool: str, event: str, status: str,
) -> None:
    identity = {"id": "event-1", "task_run_id": "run-1", "project_id": "project-1", "ts": "test-ts"}
    payload = {
        "hook_source": "pi", "hook_event_name": event, "tool_name": tool,
        "operation_id": "tool-call-1", "status": status, "phase": "explore_execute",
        "artifact_id": "audit-artifact", "session_id": "session-1", "input_bytes": 123,
    }
    schema = TaskRunHookEvent(**identity, **payload)
    projected = hook_event_to_model(identity | {"payload_json": dumps(payload)})
    assert hook_event_name(event) == event
    assert hook_tool_name(tool) == tool
    for model in (schema, projected):
        assert model.hook_event_name == event
        assert model.tool_name == tool
        assert model.status == status
        assert model.operation_id == "tool-call-1"
        assert model.artifact_id == "audit-artifact"
        assert model.session_id == "session-1"
        # pi 不提供参数/结果正文，Server 不能伪造历史 Claude 工具输入。
        assert model.tool_input == {}
        assert model.tool_response == {}
        assert model.command is None


@pytest.mark.parametrize(("event", "tool", "status"), [
    ("SessionStart", "Task", "started"),
    ("UserPromptSubmit", "WebSearch", "submitted"),
    ("PreToolUse", "Bash", "planned"),
    ("PostToolUse", "Read", "succeeded"),
    ("PostToolUseFailure", "Edit", "failed"),
    ("Stop", "MultiEdit", "stopped"),
    ("SessionEnd", "Write", "ended"),
])
def test_historical_hook_audit_stays_readable_without_pi_relabeling(
    event: str, tool: str, status: str,
) -> None:
    payload = {
        "hook_event_name": event, "tool_name": tool, "status": status,
        "tool_input": {"file_path": "/historical/test.txt"},
        "tool_response": {"historical": True},
    }
    projected = hook_event_to_model({
        "id": "old-event", "task_run_id": "old-run", "project_id": "old-project",
        "ts": "old-ts", "payload_json": dumps(payload),
    })
    assert projected.hook_event_name == event
    assert projected.tool_name == tool
    assert projected.status == status
    assert projected.tool_input == payload["tool_input"]
    assert projected.tool_response == payload["tool_response"]


@pytest.mark.parametrize("value", ["not_registered", "BASH", "TOOL_EXECUTION_START", "<script>", ""])
def test_tool_audit_unknown_names_are_not_reinterpreted(value: str) -> None:
    assert hook_event_name(value) == "unknown"
    assert hook_tool_name(value) == "unknown"
    model = TaskRunHookEvent(
        id="event", task_run_id="run", project_id="project", ts="ts",
        hook_event_name=value, tool_name=value,
    )
    assert model.hook_event_name == "unknown"
    assert model.tool_name == "unknown"


@pytest.mark.parametrize("value", [None, 12, [], {}])
def test_tool_audit_helpers_handle_non_text_payloads(value: object) -> None:
    assert hook_event_name(value) == "unknown"
    assert hook_tool_name(value) == "unknown"
