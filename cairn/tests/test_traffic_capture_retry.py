"""采集基础设施取消的诊断与持久重试预算；数据库仅使用私有 PostgreSQL。"""

from __future__ import annotations

from contextlib import contextmanager
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.scheduler.task_run_finish import TaskRunFinishMixin
from cairn.dispatcher.tasks.process_runner import worker_process_failure
from cairn.dispatcher.worker_result_gateway import worker_failure_error_type
from cairn.server import db
from cairn.server.services import assessment_attempts, audit_task_runs
from test_dispatcher_multi_project_capacity import _loop
from test_report_templates import defaults_v60_conn, private_postgres


@pytest.mark.parametrize("cancel_reason, expected_code", [
    ("traffic_capture_stalled", "traffic_capture_stalled"),
    (None, "cancelled"),
])
def test_process_cancellation_preserves_specific_diagnostic(cancel_reason, expected_code):
    result = ProcessResult(
        1, "", "", cancelled=True, cancel_reason=cancel_reason,
    )
    failure = worker_process_failure(
        result, phase="explore_execute", project_id="synthetic", task_run_id="run",
        session=None,
    )

    assert failure is not None
    assert failure.failure_class == "cancelled"
    assert failure.code == expected_code
    assert worker_failure_error_type(failure) == "cancelled"
    assert failure.details()["origin"] == "dispatcher_process"
    assert failure.details()["code"] == expected_code
    assert TaskRunFinishMixin._task_run_status_for_outcome("cancelled") == "cancelled"


@pytest.fixture
def retry_conn(defaults_v60_conn, monkeypatch):
    conn = defaults_v60_conn
    db.run_migrations(conn, "postgresql")
    conn.execute(
        "INSERT INTO projects (id, title, status, created_at) "
        "VALUES ('synthetic-retry', '合成重试项目', 'active', '2026-01-01T00:00:00Z')"
    )
    conn.commit()

    @contextmanager
    def get_conn():
        yield conn

    monkeypatch.setattr(audit_task_runs, "get_conn", get_conn)
    return conn


def _append_cancellation(
    conn, index, *, task_type="explore", code="traffic_capture_stalled",
    origin="dispatcher_process", pause_requested=False, pause_interrupted=False,
):
    context = {
        "dispatcher_id": "original-dispatcher",
        "worker_failure": {
            "origin": origin,
            "code": code,
            "phase": f"{task_type}_execute",
            "error_type": "cancelled",
            "message": "合成取消诊断",
        },
        "execution_lifecycle": {"generation": 0, "termination_confirmed": True},
    }
    ended_at = f"2026-01-01T00:00:0{index}Z"
    conn.execute(
        """INSERT INTO task_runs
        (id, project_id, intent_id, task_type, worker_name, worker_type,
         status, started_at, ended_at, error_type, runtime_context_json,
         execution_generation, pause_requested, pause_interrupted)
        VALUES (?, 'synthetic-retry', 'intent', ?, 'worker', 'pi',
                'cancelled', '2026-01-01T00:00:00Z', ?, 'cancelled', ?, 0, ?, ?)""",
        (
            f"run-{index}", task_type, ended_at, json.dumps(context),
            int(pause_requested), int(pause_interrupted),
        ),
    )
    conn.commit()


def _project(conn):
    return conn.execute(
        "SELECT * FROM projects WHERE id = 'synthetic-retry'"
    ).fetchone()


@pytest.mark.parametrize("task_type", ["bootstrap", "explore"])
@pytest.mark.parametrize("code", [
    "traffic_executor_stopped",
    "traffic_collector_stopped",
    "traffic_egress_stopped",
    "traffic_capture_stalled",
    "traffic_capture_unavailable",
    "traffic_health_unavailable",
])
def test_capture_cancellation_uses_persistent_bounded_retry_budget(retry_conn, task_type, code):
    conn = retry_conn
    _append_cancellation(conn, 1, task_type=task_type, code=code)
    decision = assessment_attempts.execution_retry_eligibility(
        conn, _project(conn), task_type, intent_id="intent",
        now="2026-01-01T00:00:02Z",
    )
    assert decision == {
        "allowed": False, "reason": "execution_retry_cooldown",
        "retry_at": "2026-01-01T00:00:06Z",
    }

    _append_cancellation(conn, 2, task_type=task_type, code=code)
    decision = assessment_attempts.execution_retry_eligibility(
        conn, _project(conn), task_type, intent_id="intent",
        now="2026-01-01T00:00:03Z",
    )
    assert decision == {
        "allowed": False, "reason": "execution_retry_cooldown",
        "retry_at": "2026-01-01T00:00:12Z",
    }
    assert assessment_attempts.execution_retry_eligibility(
        conn, _project(conn), task_type, intent_id="intent",
        ignore_cooldown=True,
    )["allowed"] is True

    _append_cancellation(conn, 3, task_type=task_type, code=code)
    assert assessment_attempts.execution_retry_eligibility(
        conn, _project(conn), task_type, intent_id="intent", ignore_cooldown=True,
    )["reason"] == "execution_retry_exhausted"

    # 每次都重建 Dispatcher 并读取 Server 持久投影；内存状态不能重置失败预算。
    for _ in range(2):
        loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
        loop.dispatcher_id = "replacement-dispatcher"
        loop._project_generations = {"synthetic-retry": 0}
        loop.client.list_task_runs = lambda project_id: ApiResult(
            status_code=200,
            data=[run.model_dump(mode="json") for run in audit_task_runs.list_task_run_records(project_id)],
        )
        loop._create_task_run = Mock(
            side_effect=AssertionError("exhausted retry created a task run")
        )
        project = SimpleNamespace(project=SimpleNamespace(
            id="synthetic-retry", tenant_workers=[],
        ))
        assert loop._delivery_route_workers(project, task_type, "intent") is None
        assert loop._project_decisions["synthetic-retry"]["code"] == "execution_retry_exhausted"
        loop._create_task_run.assert_not_called()
    assert conn.execute(
        "SELECT COUNT(*) AS count FROM task_runs WHERE project_id = 'synthetic-retry'"
    ).fetchone()["count"] == 3
    terminal_states = conn.execute(
        "SELECT DISTINCT status, error_type FROM task_runs WHERE project_id = 'synthetic-retry'"
    ).fetchall()
    assert [dict(row) for row in terminal_states] == [
        {"status": "cancelled", "error_type": "cancelled"}
    ]


@pytest.mark.parametrize("code, origin, pause_requested, pause_interrupted", [
    ("cancelled", "dispatcher_process", False, False),
    ("pause_requested", "dispatcher_process", False, False),
    ("traffic_capture_stalled", "dispatcher_process", True, False),
    ("traffic_capture_stalled", "dispatcher_process", False, True),
    ("traffic_capture_stalled", "pi_request", False, False),
    ("traffic_executor_stopped", "pi_request", False, False),
    ("traffic_scope_closed", "dispatcher_process", False, False),
    ("traffic_termination_unconfirmed", "dispatcher_process", False, False),
])
def test_other_cancellations_never_consume_retry_budget(
    retry_conn, code, origin, pause_requested, pause_interrupted,
):
    for index in range(1, 4):
        _append_cancellation(
            retry_conn, index, code=code, origin=origin,
            pause_requested=pause_requested, pause_interrupted=pause_interrupted,
        )
    decision = assessment_attempts.execution_retry_eligibility(
        retry_conn, _project(retry_conn), "explore", intent_id="intent",
        now="2026-01-01T00:00:04Z",
    )
    assert decision == {"allowed": True, "reason": None, "retry_at": None}
