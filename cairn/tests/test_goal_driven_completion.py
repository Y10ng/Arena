"""Goal-driven 完成建议必须由 Server 原子复核最新输入和真实静止状态。"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi import HTTPException

from cairn.server import db
from cairn.server.models import CompleteRequest
from cairn.server.services.project_completion import (
    _goal_driven_completion_detail,
    complete_project_record_with_conn,
)
from cairn.assessment_contracts import goal_driven_decision_input_digest
from cairn.server.services.time_utils import utcnow


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    db.run_migrations(connection, "sqlite")
    connection.commit()
    yield connection
    connection.close()


def seed_goal_driven_reason(conn) -> None:
    now = utcnow()
    features = {
        "execution_contract_version": 1,
        "execution_engine": "pi",
        "assessment_protocol": "goal-driven-v1",
        "assessment_contract_version": 2,
        "outcome_contract_version": 3,
        "verifier_contract_version": 1,
    }
    conn.execute(
        """
        INSERT INTO projects (
            id, title, status, created_at, scenario_key,
            assessment_protocol, scenario_snapshot_json,
            explore_budget, explore_attempts_started,
            verification_attempts_started, reason_worker, reason_trigger,
            reason_started_at, reason_last_heartbeat_at
        ) VALUES (
            'p', 'goal-driven', 'active', ?, 'default',
            'goal-driven-v1', ?, 8, 2, 1, 'reasoner', 'graph_changed', ?, ?
        )
        """,
        (now, json.dumps({"features": features}), now, now),
    )
    for fact_id, description in (
        ("origin", "authorized target"),
        ("goal", "verify access boundaries"),
        ("f1", "bootstrap evidence accepted"),
    ):
        conn.execute(
            """
            INSERT INTO facts (
                id, project_id, description, creator, created_at
            ) VALUES (?, 'p', ?, 'system', ?)
            """,
            (fact_id, description, now),
        )
    conn.execute(
        """
        INSERT INTO task_runs (
            id, project_id, task_type, worker_name, worker_type, status,
            execution_generation, runtime_context_json, started_at
        ) VALUES (
            'reason-run', 'p', 'reason', 'reasoner', 'pi', 'running',
            0, '{}', ?
        )
        """,
        (now,),
    )
    refresh_reason_digest(conn)


def refresh_reason_digest(conn) -> str:
    project = conn.execute("SELECT * FROM projects WHERE id = 'p'").fetchone()
    digest = goal_driven_decision_input_digest(
        _goal_driven_completion_detail(conn, project)
    )
    runtime_context = {
        "execution_delivery": {
            "phase": "reason_execute",
            "binding": {
                "allowed_fact_ids": ["origin", "f1"],
                "max_intents": 4,
                "decision_input_digest": digest,
            },
        },
    }
    conn.execute(
        """
        UPDATE task_runs
        SET runtime_context_json = ?
        WHERE project_id = 'p' AND id = 'reason-run'
        """,
        (json.dumps(runtime_context, sort_keys=True),),
    )
    return digest


def complete_body() -> CompleteRequest:
    return CompleteRequest(
        from_=["f1"],
        description="No unresolved worthwhile work remains",
        worker="reasoner",
        task_run_id="reason-run",
    )


def test_goal_driven_completion_accepts_fresh_quiescent_reason_beyond_legacy_budget(conn):
    seed_goal_driven_reason(conn)
    snapshot = json.loads(conn.execute(
        "SELECT scenario_snapshot_json FROM projects WHERE id='p'"
    ).fetchone()["scenario_snapshot_json"])
    snapshot["features"]["verification_reserve"] = 8
    conn.execute(
        "UPDATE projects SET explore_budget=40, explore_attempts_started=47, "
        "verification_attempts_started=9, scenario_snapshot_json=? WHERE id='p'",
        (json.dumps(snapshot),),
    )
    refresh_reason_digest(conn)

    completed = complete_project_record_with_conn(conn, "p", complete_body())

    assert completed.to == "goal"
    project = conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()
    assert project["status"] == "completed"
    assert project["coverage_stop_reason"] is None
    assert project["explore_budget"] == 40
    assert project["explore_attempts_started"] == 47
    assert project["verification_attempts_started"] == 9


def test_goal_driven_completion_rejects_stale_reason_input(conn):
    seed_goal_driven_reason(conn)
    conn.execute(
        """
        INSERT INTO hints (id, project_id, content, creator, created_at)
        VALUES ('h1', 'p', 'new operator direction', 'operator', ?)
        """,
        (utcnow(),),
    )

    with pytest.raises(HTTPException) as exc:
        complete_project_record_with_conn(conn, "p", complete_body())

    assert exc.value.status_code == 409
    assert exc.value.detail == "goal_driven_completion_input_stale"
    assert conn.execute(
        "SELECT status FROM projects WHERE id = 'p'"
    ).fetchone()["status"] == "active"


def test_goal_driven_completion_rejects_open_intent_after_fresh_snapshot(conn):
    seed_goal_driven_reason(conn)
    conn.execute(
        """
        INSERT INTO intents (
            id, project_id, description, creator, created_at
        ) VALUES ('i1', 'p', 'still unresolved', 'reasoner', ?)
        """,
        (utcnow(),),
    )
    conn.execute(
        """
        INSERT INTO intent_sources (
            intent_id, project_id, fact_id, source_order
        ) VALUES ('i1', 'p', 'f1', 0)
        """
    )
    refresh_reason_digest(conn)

    with pytest.raises(HTTPException) as exc:
        complete_project_record_with_conn(conn, "p", complete_body())

    assert exc.value.detail == "goal_driven_completion_open_intents"


@pytest.mark.parametrize(
    ("pending", "detail"),
    [
        ("task_run", "goal_driven_completion_execution_pending"),
        ("attempt", "goal_driven_completion_attempt_active"),
    ],
)
def test_goal_driven_completion_rejects_pending_execution_or_attempt(conn, pending, detail):
    seed_goal_driven_reason(conn)
    now = utcnow()
    if pending == "task_run":
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, task_type, worker_name, worker_type, status,
                execution_generation, runtime_context_json, started_at
            ) VALUES (
                'explore-run', 'p', 'explore', 'worker-a', 'pi', 'running',
                0, '{}', ?
            )
            """,
            (now,),
        )
    else:
        # 已有业务结果不代表逻辑 Attempt 也已关闭；完成仍须独立检查它。
        conn.execute(
            "INSERT INTO intents (id, project_id, description, creator, created_at, to_fact_id) "
            "VALUES ('i1', 'p', 'accepted observation', 'worker-a', ?, 'f1')",
            (now,),
        )
        conn.execute(
            "INSERT INTO assessment_attempts "
            "(id, project_id, intent_id, assessment_role, status, charged, execution_count, started_at) "
            "VALUES ('attempt-1', 'p', 'i1', 'discovery', 'active', 1, 1, ?)",
            (now,),
        )
        refresh_reason_digest(conn)

    with pytest.raises(HTTPException) as exc:
        complete_project_record_with_conn(conn, "p", complete_body())

    assert exc.value.detail == detail
