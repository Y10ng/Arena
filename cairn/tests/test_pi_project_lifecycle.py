"""通过真实 HTTP 路由验证历史计划、Web 终态和报告冻结不能被恢复操作绕过。

只替换认证主体和数据库连接；SQLite、迁移、状态门禁及报告资格校验均执行真实逻辑。
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from cairn.server import db
from cairn.server.auth import RequestPrincipal
from cairn.server.routers import audit as audit_router, intents as intents_router, projects as projects_router
from cairn.server.services import (
    assessment_reports,
    audit_events,
    audit_task_runs,
    project_authorization,
    project_completion,
    project_records,
    project_reason,
)
from cairn.server.services.time_utils import utcnow


CONTRACTS = {
    "historical-no-execution": ({}, None, "execution_contract_unsupported"),
    "historical-assessment-v1": (
        {
            "execution_contract_version": 1,
            "execution_engine": "pi",
            "assessment_contract_version": 1,
        },
        None,
        "assessment_contract_unsupported",
    ),
    "v2-missing-progress": (
        {
            "execution_contract_version": 1,
            "execution_engine": "pi",
            "assessment_contract_version": 2,
        },
        None,
        "assessment_progress_unsupported",
    ),
    "web-v2-finalizing": (
        {
            "execution_contract_version": 1,
            "execution_engine": "pi",
            "assessment_contract_version": 2,
            "outcome_contract_version": 2,
            "minimum_coverage_version": 1,
        },
        {"version": 1, "phase": "finalizing"},
        "assessment_new_project_required",
    ),
}


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    db.run_migrations(connection, "sqlite")
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def client(conn, monkeypatch):
    @contextmanager
    def get_conn():
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    monkeypatch.setattr(project_records, "get_conn", get_conn)
    monkeypatch.setattr(project_authorization, "get_conn", get_conn)
    monkeypatch.setattr(assessment_reports, "get_conn", get_conn)
    monkeypatch.setattr(project_completion, "get_conn", get_conn)
    monkeypatch.setattr(projects_router, "get_conn", get_conn)
    monkeypatch.setattr(audit_task_runs, "get_conn", get_conn)
    monkeypatch.setattr(audit_events, "get_conn", get_conn)
    monkeypatch.setattr(project_reason, "get_conn", get_conn)
    monkeypatch.setattr(intents_router, "get_conn", get_conn)
    app = FastAPI()
    app.include_router(projects_router.router)
    app.include_router(audit_router.router)
    app.include_router(intents_router.router)
    app.dependency_overrides[audit_router.require_tenant_project] = lambda: RequestPrincipal(kind="machine", subject_id="synthetic-dispatcher")
    # 此处验证已授权用户的生命周期语义，不替代认证/租户隔离专项测试。
    app.dependency_overrides[projects_router.require_user_tenant_project] = lambda: (
        RequestPrincipal(kind="user", subject_id="synthetic-authorized-user")
    )
    with TestClient(app) as test_client:
        yield test_client


def _seed(conn, *, contract="web-v2-finalizing", status="active", report=None):
    features, progress, _ = CONTRACTS[contract]
    now = utcnow()
    snapshot = {
        "key": "default",
        "prompt_group": "default",
        "profile_id": "default",
        "features": features,
    }
    conn.execute(
        """INSERT INTO projects
            (id, title, status, created_at, scenario_key, assessment_protocol,
             scenario_snapshot_json, assessment_progress_json, explore_budget,
             explore_deadline)
           VALUES ('p', 'synthetic', ?, ?, 'default', 'default-copy-v1', ?, ?,
                   10, '2099-01-01T00:00:00Z')""",
        (status, now, json.dumps(snapshot), json.dumps(progress) if progress else None),
    )
    for fact_id in ("origin", "goal"):
        conn.execute(
            """INSERT INTO facts (id, project_id, description, creator, created_at)
               VALUES (?, 'p', ?, 'system', ?)""",
            (fact_id, fact_id, now),
        )
    if status == "completed":
        # 保留有效 completion 来源链，确保拒绝不是缺少 goal intent 的偶然结果。
        conn.execute(
            """INSERT INTO intents
                (id, project_id, to_fact_id, description, creator, created_at, concluded_at)
               VALUES ('i001', 'p', 'goal', 'synthetic completion', 'system', ?, ?)""",
            (now, now),
        )
        conn.execute(
            """INSERT INTO intent_sources (intent_id, project_id, fact_id, source_order)
               VALUES ('i001', 'p', 'origin', 0)"""
        )
    if report is not None:
        # 无需文件或实际报告运行；保留冻结代次以检测恢复操作是否重置报告行。
        conn.execute(
            """INSERT INTO assessment_report_finalizations
                (project_id, status, generation_started_at, generation_token,
                 created_at, updated_at, next_attempt_at)
               VALUES ('p', ?, ?, 'immutable-generation', ?, ?, ?)""",
            (
                report, now, now, now,
                "2099-01-01T00:00:00Z" if report == "failed" else None,
            ),
        )
    project_authorization.initialize_project_authorization(
        conn,
        "p",
        actor_id="synthetic-authorized-user",
        authorized_at=now,
    )
    conn.commit()


def _snapshot(conn):
    """拒绝或无操作必须保留完整数据库，包括 completion、报告和冻结计划。"""
    return "\n".join(conn.iterdump())


def _request(client, action):
    if action == "activate":
        return client.put("/projects/p/status", json={"status": "active"})
    return client.post(
        "/projects/p/reopen", json={"description": "Synthetic follow-up feedback"}
    )


@pytest.mark.parametrize("contract", CONTRACTS)
@pytest.mark.parametrize(
    "status,action,expected_status,expected_detail",
    [
        ("active", "activate", 200, None),
        ("active", "reopen", 403, "Project is active"),
        ("stopped", "activate", 409, "contract_rejection"),
        ("stopped", "reopen", 403, "Project is stopped"),
        ("completed", "activate", 409, "Completed projects cannot change status"),
        ("completed", "reopen", 409, "contract_rejection"),
    ],
)
def test_lifecycle_preserves_frozen_contract_and_terminal_state(
    conn, client, contract, status, action, expected_status, expected_detail,
):
    _seed(conn, contract=contract, status=status)
    before = _snapshot(conn)
    response = _request(client, action)
    assert response.status_code == expected_status, response.text
    if expected_detail == "contract_rejection":
        expected_detail = CONTRACTS[contract][2]
    if expected_detail is not None:
        assert response.json()["detail"] == expected_detail
    else:
        assert response.json()["status"] == "active"
    assert _snapshot(conn) == before


@pytest.mark.parametrize("status", ["stopped", "completed"])
@pytest.mark.parametrize("report", ["pending", "running", "ready", "failed"])
@pytest.mark.parametrize("action", ["activate", "reopen"])
def test_web_report_generation_and_graph_cannot_be_reset_by_lifecycle(
    conn, client, status, report, action,
):
    _seed(conn, status=status, report=report)
    before = _snapshot(conn)
    response = _request(client, action)
    if status == "stopped" and action == "reopen":
        assert response.status_code == 403
        assert response.json()["detail"] == "Project is stopped"
    else:
        assert response.status_code == 409
        expected_detail = (
            "Completed projects cannot change status"
            if status == "completed" and action == "activate"
            else "assessment_new_project_required"
        )
        assert response.json()["detail"] == expected_detail
    assert _snapshot(conn) == before


def test_active_finalizing_cannot_request_report(conn, client):
    _seed(conn)
    before = _snapshot(conn)
    response = client.post(
        "/projects/p/assessment-report-finalization/request",
        json={"template_key": "standard-pentest", "template_version": 1},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "assessment_report_project_active"
    assert _snapshot(conn) == before


@pytest.mark.parametrize("resume", [False, True], ids=["stop", "stop-then-resume"])
def test_explicit_stop_keeps_finalizing_and_cannot_resume(conn, client, resume):
    _seed(conn)
    response = client.put("/projects/p/status", json={"status": "stopped"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "stopped"
    row = conn.execute("SELECT * FROM projects WHERE id = 'p'").fetchone()
    assert row["coverage_stop_reason"] == "user_stopped"
    assert json.loads(row["assessment_progress_json"])["phase"] == "finalizing"
    assert conn.execute("SELECT 1 FROM intents WHERE to_fact_id = 'goal'").fetchone() is None
    if resume:
        before = _snapshot(conn)
        response = _request(client, "activate")
        assert response.status_code == 409
        assert response.json()["detail"] == "assessment_new_project_required"
        assert _snapshot(conn) == before


@pytest.mark.parametrize("phase", ["mapping", "baseline", "depth_review", "finalizing"])
def test_explicit_pause_resume_preserves_phase_obligations_and_budget(conn, client, phase):
    _seed(conn)
    progress = {"version": 1, "phase": phase, "phase_started_at": "2026-01-01T00:00:00Z"}
    conn.execute("UPDATE projects SET assessment_progress_json = ? WHERE id = 'p'", (json.dumps(progress),))
    conn.execute("INSERT INTO intents (id, project_id, description, creator, created_at) VALUES ('pending', 'p', 'unfinished', 'system', ?)", (utcnow(),))
    conn.commit()
    response = client.post("/projects/p/pause")
    assert response.status_code == 200, response.text
    paused = response.json()
    assert paused["status"] == "paused"
    assert paused["assessment_progress"] == progress
    assert paused["execution_generation"] == 1
    assert paused["explore_budget"] == 10
    assert paused["explore_deadline"] == "2099-01-01T00:00:00Z"
    assert paused["coverage_stop_reason"] is None
    assert client.post("/projects/p/pause").json()["execution_generation"] == 1
    report = client.post("/projects/p/assessment-report-finalization/request", json={"template_key": "standard-pentest", "template_version": 1})
    assert report.status_code == 409
    assert report.json()["detail"] == "assessment_report_project_paused"
    # 本例只回归业务状态保留；真实认证就绪门禁由 monitoring HTTP 测试覆盖。
    conn.execute("INSERT INTO dispatcher_status (id, status, runtime_json, updated_at) VALUES ('ready-fixture', 'idle', ?, ?)", (json.dumps({"runtime_capabilities": ["project_pause_v1"]}), utcnow()))
    conn.commit()
    response = client.post("/projects/p/resume")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "active"
    assert response.json()["assessment_progress"] == progress
    assert response.json()["execution_generation"] == 1
    intent = conn.execute("SELECT * FROM intents WHERE id = 'pending' AND project_id = 'p'").fetchone()
    assert intent["concluded_at"] is None
    assert client.post("/projects/p/resume").status_code == 200


def _seed_executing_project(conn, client):
    _seed(conn)
    now = utcnow()
    conn.execute(
        "UPDATE projects SET assessment_progress_json = ? WHERE id = 'p'",
        (json.dumps({"version": 1, "phase": "baseline"}),),
    )
    conn.execute(
        """
        INSERT INTO coverage_slots (
            project_id, slot_key, kind, description, status, tier,
            surface_key, surface_kind, test_family, coverage_requirement,
            applicability_basis, completion_criteria, assigned_intent_id,
            created_at, updated_at
        ) VALUES ('p', 'cell-pending', 'endpoint', 'synthetic', 'exploring',
                  'core', 'GET https://example.test/resource/1', 'endpoint',
                  'authorization', 'required', 'authorized identity boundary',
                  'compare owner and non-owner', 'pending', ?, ?)
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO intents (
            id, project_id, description, creator, created_at, slot_key,
            strategy_key
        ) VALUES ('pending', 'p', 'unfinished', 'system', ?, 'cell-pending',
                  'auth.owner-vs-other-v1')
        """,
        (now,),
    )
    conn.execute(
        """
        INSERT INTO intent_sources (
            intent_id, project_id, fact_id, source_order
        ) VALUES ('pending', 'p', 'origin', 0)
        """
    )
    conn.execute(
        """
        INSERT INTO dispatcher_status (
            id, status, runtime_json, updated_at
        ) VALUES ('synthetic-dispatcher', 'running', ?, ?)
        """,
        (
            json.dumps({
                "runtime_capabilities": [
                    "project_pause_v1",
                    "project_runtime_v1",
                ],
            }),
            now,
        ),
    )
    conn.commit()
    response = client.post(
        "/projects/p/intents/pending/explore-attempts",
        json={
            "intent_id": "pending",
            "task_type": "explore",
            "worker_name": "pi-worker",
            "worker_type": "pi",
            "runtime_context": {"dispatcher_id": "synthetic-dispatcher"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _confirm_run(client, run, **updates):
    lifecycle = {"generation": run["execution_generation"], "process_started": True, "termination_confirmed": True, "cleanup_confirmed": True, **updates}
    return client.patch(f"/projects/p/task-runs/{run['id']}", json={"runtime_context": {"execution_lifecycle": lifecycle}})


def test_pause_requires_trusted_process_and_cleanup_confirmation_without_spending_retry(conn, client):
    run = _seed_executing_project(conn, client)
    assert run["execution_generation"] == 0
    assert run["pause_requested"] is False
    assert client.post("/projects/p/pause").status_code == 200
    rows = client.get("/projects/p/task-runs").json()
    assert rows[0]["pause_requested"] is True
    for path in ("/projects/p/resume", "/projects/p/status"):
        response = client.post(path) if path.endswith("resume") else client.put(path, json={"status": "active"})
        assert response.status_code == 409
        assert response.json()["detail"] == "project_pause_draining"
    response = client.patch(f"/projects/p/task-runs/{run['id']}", json={"status": "cancelled", "error_type": "cancelled"})
    assert response.status_code == 200, response.text
    assert client.post("/projects/p/resume").json()["detail"] == "project_pause_draining"
    assert _confirm_run(client, run, cleanup_confirmed=False).status_code == 200
    assert client.post("/projects/p/resume").json()["detail"] == "project_pause_draining"
    assert _confirm_run(client, run).status_code == 200
    response = client.post("/projects/p/resume")
    assert response.status_code == 200, response.text
    assert response.json()["assessment_progress"]["phase"] == "baseline"
    retry = client.post("/projects/p/intents/pending/explore-attempts", json={"intent_id": "pending", "task_type": "explore", "worker_name": "pi-worker", "worker_type": "pi", "expected_execution_generation": 1})
    assert retry.status_code == 201, retry.text
    assert retry.json()["execution_generation"] == 1
    assert retry.json()["assessment_attempt_id"] == run["assessment_attempt_id"]
    attempt = conn.execute("SELECT * FROM assessment_attempts WHERE id = ? AND project_id = 'p'", (run["assessment_attempt_id"],)).fetchone()
    assert attempt["status"] == "active"
    assert attempt["execution_count"] == 2
    assert response.json()["explore_attempts_started"] == 1
    for endpoint in ("release", "heartbeat", "conclude"):
        payload = {"worker": "pi-worker", "task_run_id": run["id"]}
        if endpoint == "conclude":
            payload["description"] = "Late old result must not be accepted"
        late = client.post(f"/projects/p/intents/pending/{endpoint}", json=payload)
        assert late.status_code == 409, late.text
        assert late.json()["detail"] == "execution_generation_mismatch"
    assert client.post("/projects/p/intents/pending/release", json={"worker": "pi-worker"}).status_code == 409
    assert conn.execute("SELECT worker FROM intents WHERE project_id = 'p' AND id = 'pending'").fetchone()["worker"] == "pi-worker"


@pytest.mark.parametrize("kind,prior_execution", [("bootstrap", False), ("reason", False), ("reason", True)])
def test_pause_releases_claim_that_never_reached_execution_admission(conn, client, kind, prior_execution):
    _seed(conn)
    conn.execute("UPDATE projects SET assessment_progress_json = ? WHERE id = 'p'", (json.dumps({"version": 1, "phase": "mapping"}),))
    conn.execute("INSERT INTO intents (id, project_id, description, creator, created_at) VALUES ('pending', 'p', 'unfinished', 'system', ?)", (utcnow(),))
    conn.execute("INSERT INTO dispatcher_status (id, status, runtime_json, updated_at) VALUES ('synthetic-dispatcher', 'running', ?, ?)", (json.dumps({"runtime_capabilities": ["project_pause_v1"]}), utcnow()))
    conn.commit()
    path = "/projects/p/reason/claim" if kind == "reason" else "/projects/p/intents/pending/heartbeat"
    body = {"worker": "pi-worker", "trigger": "bootstrap_pending"} if kind == "reason" else {"worker": "pi-worker"}
    claimed = client.post(path, json=body)
    assert claimed.status_code == 200, claimed.text
    if prior_execution:
        response = client.post("/projects/p/task-runs", json={"task_type": "reason", "worker_name": "pi-worker", "worker_type": "pi", "runtime_context": {"dispatcher_id": "synthetic-dispatcher"}})
        assert response.status_code == 201, response.text
        prior = response.json()
        assert client.patch(f"/projects/p/task-runs/{prior['id']}", json={"status": "cancelled"}).status_code == 200
        assert _confirm_run(client, prior).status_code == 200
        assert client.post("/projects/p/reason/release", json={"worker": "pi-worker", "task_run_id": prior["id"]}).status_code == 200
        assert client.post(path, json=body).status_code == 200
    assert client.post("/projects/p/pause").status_code == 200
    admission = {"task_type": kind, "worker_name": "pi-worker", "worker_type": "pi"}
    if kind == "bootstrap":
        admission["intent_id"] = "pending"
    assert client.post("/projects/p/task-runs", json=admission).status_code == 403
    resumed = client.post("/projects/p/resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["assessment_progress"]["phase"] == "mapping"
    assert len(client.get("/projects/p/task-runs").json()) == int(prior_execution)
    assert client.get("/projects/p").json()["intents"][0]["concluded_at"] is None
    assert client.post(path, json=body).status_code == 200
    before = _snapshot(conn)
    stale = client.post("/projects/p/task-runs", json={**admission, "expected_execution_generation": 0})
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"] == "execution_generation_mismatch"
    assert client.post("/projects/p/task-runs", json=admission).status_code == 409
    assert client.post("/projects/p/task-runs", json={**admission, "expected_execution_generation": 1, "worker_name": "not-the-claim-owner"}).status_code == 409
    assert _snapshot(conn) == before
    current = client.post("/projects/p/task-runs", json={**admission, "expected_execution_generation": 1})
    assert current.status_code == 201, current.text
    assert current.json()["execution_generation"] == 1


@pytest.mark.parametrize("capabilities,timestamp,reason", [
    ([], None, "project_pause_unsupported"),
    (["project_runtime_v1"], None, "project_pause_unsupported"),
    (["project_pause_v1"], "2000-01-01T00:00:00Z", "project_dispatcher_unavailable"),
    (["project_pause_v1"], "2099-01-01T00:00:00Z", "project_dispatcher_unavailable"),
])
def test_pause_accepts_unsupported_stale_or_future_dispatcher(conn, client, capabilities, timestamp, reason):
    run = _seed_executing_project(conn, client)
    conn.execute("UPDATE dispatcher_status SET runtime_json = ?, updated_at = ?", (json.dumps({"runtime_capabilities": capabilities}), timestamp or utcnow()))
    conn.commit()
    response = client.post("/projects/p/pause")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "paused"
    assert response.json()["execution_generation"] == 1
    assert client.post("/projects/p/pause").json()["execution_generation"] == 1
    assert client.get("/projects/p/task-runs").json()[0]["pause_requested"] is True
    assert client.post("/projects/p/resume").json()["detail"] == "project_pause_draining"


@pytest.mark.parametrize("contract", CONTRACTS)
def test_pause_accepts_offline_and_invalid_execution_contract(conn, client, contract):
    _seed(conn, contract=contract)
    response = client.post("/projects/p/pause")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "paused"
    assert response.json()["execution_generation"] == 1
    assert client.post("/projects/p/pause").json()["execution_generation"] == 1


@pytest.mark.parametrize("status", ["completed", "stopped"])
def test_pause_never_reopens_terminal_project(conn, client, status):
    _seed(conn, status=status)
    before = _snapshot(conn)
    assert client.post("/projects/p/pause").status_code == 409
    assert _snapshot(conn) == before


@pytest.mark.parametrize("owner_state", ["stale", "unsupported", "unknown"])
@pytest.mark.parametrize("confirmed", [False, True])
def test_resume_separates_unconfirmed_owner_from_confirmed_history(conn, client, owner_state, confirmed):
    run = _seed_executing_project(conn, client)
    assert client.post("/projects/p/pause").status_code == 200
    assert client.patch(f"/projects/p/task-runs/{run['id']}", json={"status": "cancelled"}).status_code == 200
    if confirmed:
        # 证明必须先由真实 fixture owner 提交，不能删除 owner 后绕过精确授权。
        assert _confirm_run(client, run).status_code == 200
    if owner_state == "unknown":
        context = json.loads(conn.execute("SELECT runtime_context_json FROM task_runs WHERE project_id = 'p' AND id = ?", (run["id"],)).fetchone()["runtime_context_json"])
        context.pop("dispatcher_id")
        conn.execute("UPDATE task_runs SET runtime_context_json = ? WHERE project_id = 'p' AND id = ?", (json.dumps(context), run["id"]))
    elif owner_state == "stale":
        conn.execute("UPDATE dispatcher_status SET updated_at = '2000-01-01T00:00:00Z' WHERE id = 'synthetic-dispatcher'")
    else:
        conn.execute("UPDATE dispatcher_status SET runtime_json = '{}' WHERE id = 'synthetic-dispatcher'")
    conn.execute("INSERT INTO dispatcher_status (id, status, runtime_json, updated_at) VALUES ('unrelated', 'running', ?, ?)", (json.dumps({"runtime_capabilities": ["project_pause_v1"]}), utcnow()))
    conn.commit()
    response = client.post("/projects/p/resume")
    if confirmed:
        assert response.status_code == 200, response.text
    else:
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "project_pause_draining"


def test_resume_does_not_extend_expired_absolute_deadline(conn, client):
    run = _seed_executing_project(conn, client)
    assert client.post("/projects/p/pause").status_code == 200
    assert client.patch(f"/projects/p/task-runs/{run['id']}", json={"status": "cancelled"}).status_code == 200
    assert _confirm_run(client, run).status_code == 200
    conn.execute("UPDATE projects SET explore_deadline = '2000-01-01T00:00:00Z' WHERE id = 'p'")
    conn.commit()
    before = _snapshot(conn)
    response = client.post("/projects/p/resume")
    assert response.status_code == 409
    assert response.json()["detail"] == "project_reauthorization_required"
    assert _snapshot(conn) == before


def test_expired_project_reauthorizes_same_project_before_separate_resume(conn, client):
    run = _seed_executing_project(conn, client)
    assert client.post("/projects/p/pause").status_code == 200
    assert client.patch(
        f"/projects/p/task-runs/{run['id']}",
        json={"status": "cancelled", "error_type": "cancelled"},
    ).status_code == 200
    assert _confirm_run(client, run).status_code == 200
    conn.execute(
        "UPDATE projects SET explore_deadline = '2000-01-01T00:00:00Z' WHERE id = 'p'"
    )
    conn.commit()

    current = client.get("/projects/p").json()["project"]
    response = client.post(
        "/projects/p/reauthorize",
        json={
            "confirmed": True,
            "expected_authorization_epoch": current["authorization_epoch"],
            "expected_scope_digest": current["authorization_scope_digest"],
        },
    )
    assert response.status_code == 200, response.text
    reauthorized = response.json()
    assert reauthorized["status"] == "paused"
    assert reauthorized["authorization_epoch"] == current["authorization_epoch"] + 1
    assert reauthorized["execution_generation"] == 2
    assert reauthorized["explore_attempts_started"] == 1
    assert reauthorized["explore_deadline"] > "2026-09-19T00:00:00Z"
    assert client.post("/projects/p/resume").status_code == 200


def test_reauthorization_requires_literal_confirmation_and_current_scope(conn, client):
    run = _seed_executing_project(conn, client)
    assert client.post("/projects/p/pause").status_code == 200
    assert client.patch(
        f"/projects/p/task-runs/{run['id']}",
        json={"status": "cancelled", "error_type": "cancelled"},
    ).status_code == 200
    assert _confirm_run(client, run).status_code == 200
    conn.execute(
        "UPDATE projects SET explore_deadline = '2000-01-01T00:00:00Z' WHERE id = 'p'"
    )
    conn.commit()
    current = client.get("/projects/p").json()["project"]
    before = _snapshot(conn)

    unconfirmed = client.post(
        "/projects/p/reauthorize",
        json={
            "confirmed": False,
            "expected_authorization_epoch": current["authorization_epoch"],
            "expected_scope_digest": current["authorization_scope_digest"],
        },
    )
    assert unconfirmed.status_code == 422
    assert _snapshot(conn) == before

    stale_scope = client.post(
        "/projects/p/reauthorize",
        json={
            "confirmed": True,
            "expected_authorization_epoch": current["authorization_epoch"],
            "expected_scope_digest": f"authorization-scope:{'0' * 64}",
        },
    )
    assert stale_scope.status_code == 409
    assert stale_scope.json()["detail"] == "authorization_scope_mismatch"
    assert _snapshot(conn) == before


def test_runtime_reopen_action_matches_the_endpoint_authority(conn, client):
    _seed(conn, status="completed")
    phased = conn.execute("SELECT * FROM projects WHERE id = 'p'").fetchone()
    assert project_records.project_lifecycle_actions(conn, phased)["reopen"] == {
        "allowed": False,
        "reason": "assessment_new_project_required",
    }
    rejected = client.post(
        "/projects/p/reopen", json={"description": "follow-up"}
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == "assessment_new_project_required"

    conn.execute(
        "UPDATE projects SET assessment_protocol = '', assessment_progress_json = NULL "
        "WHERE id = 'p'"
    )
    conn.commit()
    legacy = conn.execute("SELECT * FROM projects WHERE id = 'p'").fetchone()
    assert project_records.project_lifecycle_actions(conn, legacy)["reopen"] == {
        "allowed": True,
        "reason": None,
    }
    reopened = client.post(
        "/projects/p/reopen", json={"description": "follow-up"}
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["project"]["status"] == "active"


def test_finalizing_resume_preserves_frozen_report_and_ignores_exploration_deadline(conn, client):
    _seed(conn, report="pending")
    conn.execute("INSERT INTO dispatcher_status (id, status, runtime_json, updated_at) VALUES ('ready-fixture', 'idle', ?, ?)", (json.dumps({"runtime_capabilities": ["project_pause_v1"]}), utcnow()))
    conn.execute("UPDATE projects SET explore_deadline = '2000-01-01T00:00:00Z' WHERE id = 'p'")
    conn.commit()
    frozen = dict(conn.execute("SELECT * FROM assessment_report_finalizations WHERE project_id = 'p'").fetchone())
    assert client.post("/projects/p/pause").status_code == 200
    response = client.post("/projects/p/resume")
    assert response.status_code == 200, response.text
    assert response.json()["assessment_progress"]["phase"] == "finalizing"
    assert response.json()["explore_deadline"] == "2000-01-01T00:00:00Z"
    assert dict(conn.execute("SELECT * FROM assessment_report_finalizations WHERE project_id = 'p'").fetchone()) == frozen
    claim = client.post("/projects/p/reason/claim", json={"worker": "pi-worker", "trigger": "finalizing"})
    assert claim.status_code == 200, claim.text
    assert claim.json()["assessment_progress"]["phase"] == "finalizing"


def test_repeated_pause_continuations_keep_real_execution_count_and_logical_budget(conn, client):
    run = _seed_executing_project(conn, client)
    attempt_id = run["assessment_attempt_id"]
    for generation in range(1, 6):
        assert client.post("/projects/p/pause").status_code == 200
        assert client.patch(f"/projects/p/task-runs/{run['id']}", json={"status": "cancelled", "error_type": "cancelled"}).status_code == 200
        assert _confirm_run(client, run).status_code == 200
        resumed = client.post("/projects/p/resume")
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["explore_attempts_started"] == 1
        response = client.post("/projects/p/intents/pending/explore-attempts", json={"intent_id": "pending", "task_type": "explore", "worker_name": "pi-worker", "worker_type": "pi", "expected_execution_generation": generation, "runtime_context": {"dispatcher_id": "synthetic-dispatcher"}})
        assert response.status_code == 201, response.text
        run = response.json()
        assert run["execution_generation"] == generation
        assert run["assessment_attempt_id"] == attempt_id
        assert run["runtime_context"]["assessment_execution_number"] == generation + 1


def test_terminal_timeout_does_not_discard_later_trusted_termination_proof(conn, client):
    run = _seed_executing_project(conn, client)
    conn.execute("UPDATE intents SET last_heartbeat_at = '2000-01-01T00:00:00Z' WHERE project_id = 'p'")
    conn.commit()
    assert client.get("/projects/p").status_code == 200
    expired = client.get("/projects/p/task-runs").json()[0]
    assert expired["status"] == "failed"
    assert client.post("/projects/p/pause").status_code == 200
    lifecycle = {"generation": 0, "process_started": True, "termination_confirmed": True, "cleanup_confirmed": False}
    payload = {"status": "cancelled", "error_type": "cancelled", "runtime_context": {"execution_lifecycle": lifecycle}}
    client.app.dependency_overrides[audit_router.require_tenant_project] = lambda: RequestPrincipal(kind="user", subject_id="synthetic-authorized-user")
    assert client.patch(f"/projects/p/task-runs/{run['id']}", json=payload).status_code == 403
    client.app.dependency_overrides[audit_router.require_tenant_project] = lambda: RequestPrincipal(kind="machine", subject_id="synthetic-dispatcher")
    lifecycle["generation"] = 1
    assert client.patch(f"/projects/p/task-runs/{run['id']}", json=payload).status_code == 409
    lifecycle["generation"] = 0
    response = client.patch(f"/projects/p/task-runs/{run['id']}", json=payload)
    assert response.status_code == 200, response.text
    confirmed = response.json()
    assert confirmed["status"] == "failed"
    assert confirmed["error_type"] == expired["error_type"]
    assert confirmed["ended_at"] == expired["ended_at"]
    assert confirmed["runtime_context"]["execution_lifecycle"]["termination_confirmed"] is True
    assert client.post("/projects/p/resume").json()["detail"] == "project_pause_draining"
    assert _confirm_run(client, run).status_code == 200
    assert client.post("/projects/p/resume").status_code == 200


def test_pause_confirmation_cannot_be_forged_by_user_or_wrong_generation(conn, client):
    run = _seed_executing_project(conn, client)
    assert client.post("/projects/p/pause").status_code == 200
    assert client.patch(f"/projects/p/task-runs/{run['id']}", json={"status": "cancelled"}).status_code == 200
    client.app.dependency_overrides[audit_router.require_tenant_project] = lambda: RequestPrincipal(kind="user", subject_id="synthetic-authorized-user")
    assert _confirm_run(client, run).status_code == 403
    client.app.dependency_overrides[audit_router.require_tenant_project] = lambda: RequestPrincipal(kind="machine", subject_id="synthetic-dispatcher")
    assert _confirm_run(client, run, generation=1).status_code == 409
    assert _confirm_run(client, run, termination_confirmed="true").status_code == 422
    assert client.patch(f"/projects/p/task-runs/{run['id']}", json={"runtime_context": {"pause_requested": False}}).status_code == 422
    assert client.post("/projects/p/resume").json()["detail"] == "project_pause_draining"


def test_user_cannot_forge_execution_lifecycle_audit_event(conn, client):
    run = _seed_executing_project(conn, client)
    client.app.dependency_overrides[audit_router.require_tenant_project] = lambda: RequestPrincipal(kind="user", subject_id="synthetic-authorized-user")
    forged = {"task_run_id": run["id"], "kind": "runtime_context_updated", "payload": {"runtime_context": {"execution_lifecycle": {"generation": 0, "process_started": True, "termination_confirmed": True, "cleanup_confirmed": True}}}}
    before = _snapshot(conn)
    response = client.post("/projects/p/agent-events", json=forged)
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "execution_lifecycle_machine_required"
    assert _snapshot(conn) == before
    ordinary = client.post("/projects/p/agent-events", json={"task_run_id": run["id"], "kind": "runtime_context_updated", "payload": {"runtime_context": {"synthetic_note": "ordinary audit metadata"}}})
    assert ordinary.status_code == 201, ordinary.text


def test_paused_project_read_does_not_expire_claim_or_infer_process_termination(conn, client):
    run = _seed_executing_project(conn, client)
    assert client.post("/projects/p/pause").status_code == 200
    conn.execute("UPDATE intents SET last_heartbeat_at = '2000-01-01T00:00:00Z' WHERE project_id = 'p'")
    conn.execute("UPDATE projects SET reason_worker = 'old-reason', reason_trigger = 'test', reason_started_at = '2000-01-01T00:00:00Z', reason_last_heartbeat_at = '2000-01-01T00:00:00Z' WHERE id = 'p'")
    conn.commit()
    before = _snapshot(conn)
    response = client.get("/projects/p")
    assert response.status_code == 200
    assert response.json()["intents"][0]["worker"] == "pi-worker"
    assert _snapshot(conn) == before
    assert client.get("/projects/p/task-runs").json()[0]["status"] == "running"
    assert client.post("/projects/p/resume").json()["detail"] == "project_pause_draining"


def test_finish_after_pause_cannot_freeze_report_until_cleanup_confirmed(conn, client):
    run = _seed_executing_project(conn, client)
    assert client.post("/projects/p/pause").status_code == 200
    assert client.patch(f"/projects/p/task-runs/{run['id']}", json={"status": "cancelled"}).status_code == 200
    finished = client.post("/projects/p/finish")
    assert finished.status_code == 200, finished.text
    assert finished.json()["status"] == "stopped"
    assert finished.json()["assessment_progress"]["phase"] == "finalizing"
    before = _snapshot(conn)
    response = client.post("/projects/p/assessment-report-finalization/request", json={"template_key": "standard-pentest", "template_version": 1})
    assert response.status_code == 409
    assert response.json()["detail"] == "project_pause_draining"
    assert _snapshot(conn) == before
    assert client.post("/projects/p/resume").json()["detail"] == "assessment_new_project_required"
    assert _confirm_run(client, run).status_code == 200


def _request_report(client):
    response = client.post("/projects/p/assessment-report-finalization/request", json={"template_key": "full-technical", "template_version": 1})
    assert response.status_code == 202, response.text
    return response.json()


def _failed_report_finish_body(claim, *, error_type="request_contract"):
    return {
        "protocol": "cairn-assessment-report-finish-v1",
        "worker": "pi-worker",
        "claim_token": claim["claim_token"],
        "generation_token": claim["generation_token"],
        "task_run_id": claim["task_run_id"],
        "status": "failed",
        "artifact_id": None,
        "artifact_sha256": None,
        "input_snapshot_artifact_id": None,
        "input_snapshot_sha256": None,
        "task_run_result": {
            "status": "failed",
            "exit_code": None,
            "error_type": error_type,
            "error_message": error_type,
            "worker_failure": None,
        },
    }


def test_report_retry_block_is_durable_but_explicit_report_generation_is_independent(conn, client, monkeypatch):
    _seed(conn, status="stopped")
    original = _request_report(client)
    claim = client.post("/projects/p/assessment-report-finalization/claim", json={"worker": "pi-worker"})
    assert claim.status_code == 200, claim.text
    claim = claim.json()
    finished = client.post(
        "/projects/p/assessment-report-finalization/finish",
        json=_failed_report_finish_body(claim),
    )
    assert finished.status_code == 200, finished.text
    monkeypatch.setattr(assessment_reports, "utcnow", lambda: "2099-01-01T00:00:00Z")
    for _ in range(2):
        denied = client.post("/projects/p/assessment-report-finalization/claim", json={"worker": "pi-worker"})
        assert denied.status_code == 409, denied.text
        assert denied.json()["detail"] == "execution_retry_blocked"
    replacement = _request_report(client)
    assert replacement["generation_token"] != original["generation_token"]
    assert client.post("/projects/p/assessment-report-finalization/claim", json={"worker": "pi-worker"}).status_code == 200
    assert len(client.get("/projects/p/task-runs").json()) == 2


def test_non_phased_reopen_cannot_cross_outstanding_report_generation(conn, client):
    _seed(conn, status="completed")
    row = conn.execute(
        "SELECT scenario_snapshot_json FROM projects WHERE id = 'p'"
    ).fetchone()
    snapshot = json.loads(row["scenario_snapshot_json"])
    snapshot["features"].pop("assessment_contract_version", None)
    conn.execute(
        """UPDATE projects
           SET scenario_snapshot_json = ?, assessment_protocol = ''
           WHERE id = 'p'""",
        (json.dumps(snapshot),),
    )
    conn.commit()

    _request_report(client)
    claim = client.post(
        "/projects/p/assessment-report-finalization/claim",
        json={"worker": "pi-worker"},
    )
    assert claim.status_code == 200, claim.text
    before = _snapshot(conn)

    reopened = client.post(
        "/projects/p/reopen",
        json={"description": "Synthetic follow-up feedback"},
    )
    assert reopened.status_code == 409, reopened.text
    assert reopened.json()["detail"] == "assessment_report_finalization_outstanding"
    assert _snapshot(conn) == before


def test_report_expired_leases_consume_persistent_retry_allowance(conn, client, monkeypatch):
    _seed(conn, status="stopped")
    _request_report(client)
    clock = ["2026-09-17T00:00:00Z"]
    monkeypatch.setattr(assessment_reports, "utcnow", lambda: clock[0])
    for minute in (0, 10, 20):
        clock[0] = f"2026-09-17T00:{minute:02d}:00Z"
        response = client.post("/projects/p/assessment-report-finalization/claim", json={"worker": "pi-worker", "lease_seconds": 60})
        assert response.status_code == 200, response.text
    clock[0] = "2026-09-17T00:30:00Z"
    for _ in range(2):
        denied = client.post("/projects/p/assessment-report-finalization/claim", json={"worker": "pi-worker"})
        assert denied.status_code == 409, denied.text
        assert denied.json()["detail"] == "execution_retry_exhausted"
    runs = client.get("/projects/p/task-runs").json()
    assert len(runs) == 3
    assert all(run["status"] == "timed_out" for run in runs)


@pytest.mark.parametrize("operation", ["snapshot", "finish"])
def test_pause_then_finish_never_accepts_old_report_generation_after_cleanup(conn, client, operation):
    _seed(conn, status="stopped")
    _request_report(client)
    claim = client.post("/projects/p/assessment-report-finalization/claim", json={"worker": "pi-worker"}).json()
    # 合成历史终态但尚无 finish receipt 的 report run；普通 PATCH 不得伪造此状态。
    run_row = conn.execute(
        "SELECT * FROM task_runs WHERE project_id = 'p' AND id = ?",
        (claim["task_run_id"],),
    ).fetchone()
    runtime_context = json.loads(run_row["runtime_context_json"])
    ended_at = utcnow()
    runtime_context.update(
        status="cancelled",
        ended_at=ended_at,
        error_type="cancelled",
        error_message="synthetic historical cancellation",
    )
    conn.execute(
        """UPDATE task_runs
           SET status = 'cancelled', ended_at = ?, error_type = 'cancelled',
               error_message = 'synthetic historical cancellation',
               runtime_context_json = ?
           WHERE project_id = 'p' AND id = ?""",
        (ended_at, json.dumps(runtime_context), claim["task_run_id"]),
    )
    # 合成存量 active/report 时间线；公开 API 仍不允许重新打开 stopped 项目。
    conn.execute("UPDATE projects SET status = 'active' WHERE id = 'p'")
    conn.commit()
    assert client.post("/projects/p/pause").status_code == 200
    assert client.post("/projects/p/finish").status_code == 200
    run = client.get("/projects/p/task-runs").json()[0]
    body = (
        _failed_report_finish_body(claim, error_type="contract_error")
        if operation == "finish"
        else {
            "worker": "pi-worker",
            "claim_token": claim["claim_token"],
            "artifact_id": "old-snapshot",
            "digest_sha256": "a" * 64,
        }
    )
    before = client.get("/projects/p/assessment-report-finalization").json()
    draining = client.post(
        f"/projects/p/assessment-report-finalization/{operation}",
        json=body,
    )
    assert draining.status_code == 409, draining.text
    assert draining.json()["detail"] == "project_pause_draining"
    assert client.get("/projects/p/assessment-report-finalization").json() == before

    assert _confirm_run(client, run).status_code == 200
    response = client.post(f"/projects/p/assessment-report-finalization/{operation}", json=body)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "execution_generation_mismatch"
    assert client.get("/projects/p/assessment-report-finalization").json() == before


def _start_reason(client, *, generation=0):
    claim = client.post("/projects/p/reason/claim", json={"worker": "pi-worker", "trigger": "synthetic"})
    assert claim.status_code == 200, claim.text
    response = client.post("/projects/p/task-runs", json={
        "task_type": "reason", "worker_name": "pi-worker", "worker_type": "pi",
        "expected_execution_generation": generation, "runtime_context": {"dispatcher_id": "synthetic-dispatcher"},
    })
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("phase", ["baseline", "depth_review", "finalizing"])
@pytest.mark.parametrize("error_type", ["contract_error", "request_contract", "result_schema", "business_acceptance", "protocol_error", "repair_failed"])
def test_reason_deterministic_failure_blocks_same_generation_across_repeated_claims(conn, client, phase, error_type):
    _seed(conn)
    conn.execute("UPDATE projects SET assessment_progress_json = ? WHERE id = 'p'", (json.dumps({"version": 1, "phase": phase}),))
    conn.commit()
    run = _start_reason(client)
    assert client.patch(f"/projects/p/task-runs/{run['id']}", json={"status": "failed", "error_type": error_type}).status_code == 200
    assert client.post("/projects/p/reason/release", json={"worker": "pi-worker", "task_run_id": run["id"]}).status_code == 200
    for _ in range(3):
        response = client.post("/projects/p/reason/claim", json={"worker": "pi-worker", "trigger": "synthetic"})
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "execution_retry_blocked"
    assert len(client.get("/projects/p/task-runs").json()) == 1


@pytest.mark.parametrize("phase", ["mapping", "depth_review", "finalizing"])
def test_retry_recovery_requires_new_generation_and_preserves_failed_run(conn, client, phase):
    _seed(conn)
    conn.execute("UPDATE projects SET assessment_progress_json = ? WHERE id = 'p'", (json.dumps({"version": 1, "phase": phase}),))
    conn.execute("INSERT INTO dispatcher_status (id, status, runtime_json, updated_at) VALUES ('synthetic-dispatcher', 'running', ?, ?)", (json.dumps({"runtime_capabilities": ["project_pause_v1"]}), utcnow()))
    conn.commit()
    run = _start_reason(client)
    assert client.patch(f"/projects/p/task-runs/{run['id']}", json={"status": "failed", "error_type": "request_contract"}).status_code == 200
    assert client.post("/projects/p/pause").status_code == 200
    assert client.post("/projects/p/resume").json()["detail"] == "project_pause_draining"
    assert _confirm_run(client, run).status_code == 200
    assert client.post("/projects/p/resume").status_code == 200
    retry = _start_reason(client, generation=1)
    assert retry["execution_generation"] == 1
    assert retry["runtime_context"]["assessment_phase"] == phase
    old = client.get("/projects/p/task-runs").json()[0]
    assert old["status"] == "failed"
    assert old["error_type"] == "request_contract"
    assert old["execution_generation"] == 0


@pytest.mark.parametrize("phase", ["mapping", "depth_review", "finalizing"])
def test_transient_reason_retry_has_persistent_cooldown_and_failure_allowance(conn, client, monkeypatch, phase):
    from cairn.server.services import assessment_attempts
    _seed(conn)
    conn.execute("UPDATE projects SET assessment_progress_json = ? WHERE id = 'p'", (json.dumps({"version": 1, "phase": phase}),))
    conn.commit()
    clock = ["2026-09-17T00:00:00Z"]
    monkeypatch.setattr(audit_task_runs, "utcnow", lambda: clock[0])
    monkeypatch.setattr(assessment_attempts, "utcnow", lambda: clock[0])
    for index in range(3):
        run = _start_reason(client)
        assert client.patch(f"/projects/p/task-runs/{run['id']}", json={"status": "failed", "error_type": "transport_error"}).status_code == 200
        assert client.post("/projects/p/reason/release", json={"worker": "pi-worker", "task_run_id": run["id"]}).status_code == 200
        denied = client.post("/projects/p/reason/claim", json={"worker": "pi-worker", "trigger": "synthetic"})
        assert denied.status_code == 409, denied.text
        assert denied.json()["detail"] == ("execution_retry_cooldown" if index < 2 else "execution_retry_exhausted")
        clock[0] = f"2026-09-17T00:0{index + 1}:00Z"
    assert client.post("/projects/p/reason/claim", json={"worker": "other-worker", "trigger": "synthetic"}).json()["detail"] == "execution_retry_exhausted"
    assert len(client.get("/projects/p/task-runs").json()) == 3


def test_cancelled_reason_does_not_spend_failure_allowance(conn, client):
    _seed(conn)
    for _ in range(4):
        run = _start_reason(client)
        assert client.patch(f"/projects/p/task-runs/{run['id']}", json={"status": "cancelled", "error_type": "cancelled"}).status_code == 200
        assert client.post("/projects/p/reason/release", json={"worker": "pi-worker", "task_run_id": run["id"]}).status_code == 200
    assert _start_reason(client)["status"] == "running"


def test_assessment_phase_is_server_owned_at_all_audit_write_boundaries(conn, client):
    _seed(conn)
    run = _start_reason(client)
    assert run["runtime_context"]["assessment_phase"] == "finalizing"
    forged = {"assessment_phase": "mapping"}
    assert client.patch(f"/projects/p/task-runs/{run['id']}", json={"runtime_context": forged}).status_code == 422
    assert client.post("/projects/p/agent-events", json={"task_run_id": run["id"], "kind": "runtime_context_updated", "payload": {"runtime_context": forged}}).status_code == 422
    assert client.post("/projects/p/task-runs", json={"task_type": "reason", "worker_name": "pi-worker", "worker_type": "pi", "runtime_context": forged}).status_code == 422
    assert client.get("/projects/p/task-runs").json()[0]["runtime_context"]["assessment_phase"] == "finalizing"


@pytest.mark.parametrize("status", ["completed", "baseline", "mapping"])
def test_status_api_cannot_complete_project_or_reset_phase(conn, client, status):
    _seed(conn, status="stopped")
    before = _snapshot(conn)
    response = client.put("/projects/p/status", json={"status": status})
    assert response.status_code == 422
    assert _snapshot(conn) == before
