"""领取拒绝必须保留 Server 的重试裁决，且不能进入付费执行。"""
from contextlib import contextmanager
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cairn.dispatcher.protocol.client import ApiResult
from test_dispatcher_multi_project_capacity import _loop


@pytest.mark.parametrize("status, detail, expected", [
    (409, "execution_retry_blocked", "execution_retry_blocked"),
    (409, "execution_retry_exhausted", "execution_retry_exhausted"),
    (409, "execution_retry_cooldown", "execution_retry_cooldown"),
    (409, "reason_execution_pending", "cleanup_pending"),
    (409, "private-error-canary", "claim_conflict"),
    (409, {"message": "private-error-canary"}, "claim_conflict"),
    (403, "execution_retry_blocked", "claim_denied"),
    (503, "execution_retry_blocked", "claim_failed"),
])
@pytest.mark.parametrize("task_type", ["reason", "bootstrap"])
def test_claim_retry_decision_precedes_task_creation(status, detail, expected, task_type):
    loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
    worker = SimpleNamespace(id="worker", name="worker", type="pi")
    selection = SimpleNamespace(worker=worker)
    scenario = SimpleNamespace(key="default")
    loop._scenario_for_project = lambda project: scenario
    loop._container_runtime_options = lambda *args: {}
    loop._select_worker = lambda *args: selection
    loop._materialize_selected_worker = lambda *args: (worker, {}, None)
    loop.client.list_task_runs = lambda project_id: ApiResult(status_code=200, data=[])
    rejection = ApiResult(status_code=status, data={"detail": detail})
    loop.client.claim_reason = Mock(return_value=rejection)
    loop.client.heartbeat = Mock(return_value=rejection)
    loop._create_task_run = Mock(side_effect=AssertionError("rejected claim created a task"))
    loop.executor = SimpleNamespace(submit=Mock(side_effect=AssertionError("rejected claim executed work")))
    loop._project_generations = {"p": 7}
    project = SimpleNamespace(project=SimpleNamespace(id="p", tenant_workers=[]))

    for _ in range(3):
        if task_type == "reason":
            assert loop._dispatch_reason(project, "", "finalizing") is False
        else:
            assert loop._dispatch_bootstrap(project, SimpleNamespace(id="i")) is False
        decision = loop._project_decisions["p"]
        assert decision["code"] == expected
        assert decision["task_type"] == task_type
        assert decision["execution_generation"] == 7
        assert "private-error-canary" not in str(decision)

    loop._create_task_run.assert_not_called()
    loop.executor.submit.assert_not_called()
    assert loop.futures == {}


@pytest.fixture
def persisted_retry_failure(monkeypatch):
    from cairn.server import db
    from cairn.server.services import assessment_attempts, audit_task_runs

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.run_migrations(conn, "sqlite")
    conn.execute("INSERT INTO projects (id, title, created_at) VALUES ('p', 'synthetic', '2026-01-01T00:00:00Z')")
    conn.execute(
        """INSERT INTO task_runs (id, project_id, intent_id, task_type, worker_name,
               worker_type, status, started_at, ended_at, error_type, runtime_context_json)
           VALUES ('t1', 'p', 'i', 'bootstrap', 'worker', 'pi', 'failed',
                   '2026-01-01T00:00:00Z', '2026-01-01T00:00:01Z',
                   'business_acceptance', ?)""",
        (json.dumps({"dispatcher_id": "original-dispatcher"}),),
    )
    conn.commit()

    @contextmanager
    def get_conn():
        yield conn

    monkeypatch.setattr(audit_task_runs, "get_conn", get_conn)
    monkeypatch.setattr(assessment_attempts, "utcnow", lambda: "2026-01-01T00:01:00Z")
    yield conn
    conn.close()


@pytest.mark.parametrize("task_type", ["bootstrap", "reason", "explore", "report"])
def test_persisted_business_failure_blocks_before_any_claim(persisted_retry_failure, task_type):
    from cairn.server.services import assessment_reports, audit_task_runs

    conn = persisted_retry_failure
    intent_id = "i" if task_type in {"bootstrap", "explore"} else None
    conn.execute("UPDATE task_runs SET task_type = ?, intent_id = ?", (task_type, intent_id))
    if task_type == "report":
        conn.execute(
            """INSERT INTO assessment_report_finalizations
               (project_id, status, generation_token, generation_started_at, created_at, updated_at)
               VALUES ('p', 'pending', 'report-generation', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"""
        )
        conn.execute("UPDATE task_runs SET runtime_context_json = ?", (json.dumps({
            "dispatcher_id": "original-dispatcher", "report_claim": {"generation_token": "report-generation"},
        }),))
    conn.commit()

    for _ in range(3):
        # 每轮重建 Dispatcher，并消费真实 Server 投影；不是靠内存冷却或反复收到 409。
        loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
        loop.dispatcher_id = "another-dispatcher"
        loop._project_generations = {"p": 0}
        loop._scenario_for_project = lambda project: SimpleNamespace(key="default")
        loop._container_runtime_options = lambda *args: {}
        loop._select_worker = Mock(side_effect=AssertionError("blocked retry selected worker"))
        loop.client.list_task_runs = lambda project_id: ApiResult(
            status_code=200,
            data=[run.model_dump(mode="json") for run in audit_task_runs.list_task_run_records(project_id)],
        )
        loop.client.claim_reason = Mock()
        loop.client.heartbeat = Mock()
        loop._create_task_run = Mock()
        project = SimpleNamespace(project=SimpleNamespace(id="p", tenant_workers=[]))
        if task_type == "reason":
            assert loop._dispatch_reason(project, "", "idle") is False
        elif task_type == "bootstrap":
            assert loop._dispatch_bootstrap(project, SimpleNamespace(id="i")) is False
        elif task_type == "explore":
            assert loop._delivery_route_workers(project, task_type, intent_id) is None
        else:
            state = assessment_reports.get_assessment_report_finalization(conn, "p").model_dump(mode="json")
            assert loop._delivery_route_workers(project, task_type, report_state=state) is None
        assert loop._project_decisions["p"]["code"] == "execution_retry_blocked"
        loop._select_worker.assert_not_called()
        loop.client.claim_reason.assert_not_called()
        loop.client.heartbeat.assert_not_called()
        loop._create_task_run.assert_not_called()
    assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 1


@pytest.mark.parametrize("error_type, ended_at, expected", [
    ("container_failed", "2026-01-01T00:00:01Z", None),
    ("container_failed", "2026-01-01T00:00:59Z", "execution_retry_cooldown"),
    ("submission_invalid", "2026-01-01T00:00:01Z", "execution_delivery_unavailable"),
])
def test_retry_route_preserves_authoritative_recovery_rules(persisted_retry_failure, error_type, ended_at, expected):
    from cairn.server.services import audit_task_runs

    conn = persisted_retry_failure
    conn.execute("UPDATE task_runs SET error_type = ?, ended_at = ?", (error_type, ended_at))
    conn.commit()
    route = audit_task_runs.list_task_run_records("p")[0].delivery_retry_route
    if expected is None:
        assert route is None
    else:
        assert route["allowed"] is False
        assert route["reason"] == expected
        assert route["container_id"] is None
    # 历史代次不是当前工作的重试链，读取不修改原失败或推进 generation。
    conn.execute("UPDATE projects SET execution_generation = 1")
    assert audit_task_runs.list_task_run_records("p")[0].delivery_retry_route is None
    assert conn.execute("SELECT execution_generation FROM task_runs").fetchone()[0] == 0


def _append_retry_failure(conn, index, error_type, *, generation=0, context=None):
    conn.execute(
        """INSERT INTO task_runs
           (id, project_id, intent_id, task_type, worker_name, worker_type,
            status, started_at, ended_at, error_type, runtime_context_json,
            execution_generation)
           VALUES (?, 'p', NULL, 'reason', 'worker', 'pi', 'failed',
                   '2026-01-01T00:00:00Z', '2026-01-01T00:00:01Z', ?, ?, ?)""",
        (f"reason-{index}", error_type, json.dumps(context or {}), generation),
    )


def test_provider_reason_retry_limit_cooldown_and_generation_are_persisted(persisted_retry_failure):
    from cairn.server.services.assessment_attempts import execution_retry_eligibility
    from cairn.server.services.audit_task_runs import list_task_run_records

    conn = persisted_retry_failure
    conn.execute("DELETE FROM task_runs")
    before = dict(conn.execute(
        "SELECT execution_generation, authorization_epoch, explore_budget, "
        "explore_attempts_started FROM projects WHERE id='p'"
    ).fetchone())
    for index in range(1, 12):
        _append_retry_failure(
            conn, index, ("transport_error" if index == 1 else "provider_error"),
        )
        project = conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()
        decision = execution_retry_eligibility(conn, project, "reason", now="2026-01-01T01:00:00Z")
        if index <= 10:
            assert decision == {"allowed": True, "reason": None, "retry_at": None}
        else:
            assert decision["reason"] == "execution_retry_exhausted"
        if index == 3:
            assert list_task_run_records("p")[-1].delivery_retry_route is None
        if index == 7:
            cooldown = execution_retry_eligibility(
                conn, project, "reason", now="2026-01-01T00:00:02Z",
            )
            assert cooldown["retry_at"] == "2026-01-01T00:05:01Z"
    assert len(conn.execute("SELECT id FROM task_runs WHERE project_id='p'").fetchall()) == 11
    assert dict(conn.execute(
        "SELECT execution_generation, authorization_epoch, explore_budget, "
        "explore_attempts_started FROM projects WHERE id='p'"
    ).fetchone()) == before
    assert list_task_run_records("p")[-1].delivery_retry_route["reason"] == "execution_retry_exhausted"
    conn.execute("UPDATE projects SET execution_generation=1 WHERE id='p'")
    assert execution_retry_eligibility(conn, conn.execute(
        "SELECT * FROM projects WHERE id='p'"
    ).fetchone(), "reason")["allowed"] is True
    assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE execution_generation=0").fetchone()[0] == 11


@pytest.mark.parametrize("error_type", [
    "provider_auth", "request_contract", "contract_error", "result_schema",
    "session_error", "candidate_spool_unavailable", "receipt_conflict",
])
def test_nonrecoverable_reason_failure_does_not_gain_extended_allowance(
    persisted_retry_failure, error_type,
):
    from cairn.server.services.assessment_attempts import execution_retry_eligibility

    conn = persisted_retry_failure
    conn.execute("DELETE FROM task_runs")
    _append_retry_failure(conn, 1, error_type, context={"error_message": "provider temporarily unavailable"})
    project = conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()
    assert execution_retry_eligibility(conn, project, "reason")["reason"] == "execution_retry_blocked"


def test_bootstrap_three_provider_failures_still_allow_fresh_retry(persisted_retry_failure):
    from cairn.server.services.assessment_attempts import execution_retry_eligibility

    conn = persisted_retry_failure
    conn.execute("DELETE FROM task_runs")
    for index, error in enumerate(("transport_error", "provider_error", "provider_rate_limit"), 1):
        _append_retry_failure(conn, index, error)
    conn.execute("UPDATE task_runs SET task_type='bootstrap', intent_id='i'")
    project = conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()
    assert execution_retry_eligibility(
        conn, project, "bootstrap", intent_id="i", now="2026-01-01T01:00:00Z",
    )["allowed"] is True


def test_failed_reason_with_server_acceptance_receipt_never_restarts(persisted_retry_failure):
    from cairn.server.services.assessment_attempts import execution_retry_eligibility

    conn = persisted_retry_failure
    conn.execute("DELETE FROM task_runs")
    _append_retry_failure(conn, 1, "provider_error", context={"accepted_result_digest": "accepted"})
    project = conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()
    assert execution_retry_eligibility(conn, project, "reason")["reason"] == "execution_retry_blocked"


def test_mixed_legacy_failure_remains_capped_at_two(persisted_retry_failure):
    from cairn.server.services.assessment_attempts import execution_retry_eligibility

    conn = persisted_retry_failure
    conn.execute("DELETE FROM task_runs")
    for index, error in enumerate(("provider_error", "worker_timeout", "provider_rate_limit"), 1):
        _append_retry_failure(conn, index, error)
    project = conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()
    assert execution_retry_eligibility(conn, project, "reason")["reason"] == "execution_retry_exhausted"


def test_delivery_chain_keeps_two_retries_even_with_extended_provider_cap(persisted_retry_failure):
    from cairn.server.services.assessment_attempts import execution_retry_eligibility

    conn = persisted_retry_failure
    conn.execute("DELETE FROM task_runs")
    session = "pi:11111111-1111-4111-8111-111111111111"
    context = {
        "dispatcher_id": "original-dispatcher",
        "execution_delivery": {"session_id": session, "deadline_at": "2099-01-01T00:00:00Z"},
        "execution_lifecycle": {"generation": 0, "termination_confirmed": True},
    }
    for index in range(1, 4):
        _append_retry_failure(conn, index, "submission_missing", context=context)
    conn.execute("UPDATE task_runs SET session_id=? WHERE project_id='p'", (session,))
    project = conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()
    assert execution_retry_eligibility(conn, project, "reason", now="2026-01-01T01:00:00Z")["reason"] == "execution_retry_exhausted"


def test_fresh_provider_retry_cannot_reuse_a_previous_session(persisted_retry_failure):
    from fastapi import HTTPException
    from cairn.server.models import CreateTaskRunRequest
    from cairn.server.services.assessment_attempts import derive_delivery_retry_context

    conn = persisted_retry_failure
    conn.execute("DELETE FROM task_runs")
    _append_retry_failure(conn, 1, "provider_error")
    old_session = "pi:11111111-1111-4111-8111-111111111111"
    conn.execute("UPDATE task_runs SET session_id=? WHERE project_id='p'", (old_session,))
    project = conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()
    request = CreateTaskRunRequest(
        task_type="reason", worker_name="worker", worker_type="pi", status="running",
        session_id=old_session, runtime_context={"dispatcher_id": "another-dispatcher"},
    )
    with pytest.raises(HTTPException) as refused:
        derive_delivery_retry_context(conn, project, request)
    assert refused.value.detail == "execution_delivery_session_already_bound"
    assert derive_delivery_retry_context(
        conn, project, request.model_copy(update={"session_id": None}),
    ) == {}
    assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE project_id='p'").fetchone()[0] == 1


def test_delivery_chain_cannot_fall_back_to_fresh_provider_retry(persisted_retry_failure):
    from cairn.server.services.assessment_attempts import execution_retry_eligibility

    conn = persisted_retry_failure
    conn.execute("DELETE FROM task_runs")
    _append_retry_failure(conn, 1, "provider_error", context={"delivery_retry": {"source_task_run_id": "old"}})
    project = conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()
    assert execution_retry_eligibility(conn, project, "reason")["reason"] == "execution_retry_blocked"
