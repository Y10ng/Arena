"""仅使用合成项目与临时数据库验证历史阶段、Attempt 计数和不可变报告输入。"""
from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3

import pytest
import yaml
from fastapi import HTTPException

from cairn.dispatcher.tasks.explore_context import selected_experiment_context
from cairn.server.routers.export import _export_yaml, _fact_summary

from cairn.server import db, migrations
from cairn.server.auth import RequestPrincipal
from cairn.server.models import (
    CompleteRequest, ConcludeRequest, CreateTaskRunRequest, DepthReviewRequest,
    HeartbeatRequest,
)
from cairn.server.routers import intents as intents_router
from cairn.server.services import project_reason
from cairn.server.services.assessment_convergence import (
    advance_assessment_phase, assessment_execution_view, assessment_review_digest,
    require_assessment_completion, stop_assessment,
)
from cairn.server.services.assessment_reports import (
    _require_report_request_eligibility, build_assessment_report_packet,
)
from cairn.server.services.explore_attempts import start_explore_attempt
from cairn.server.services.project_completion import complete_project_record_with_conn
from cairn.server.services.projects import abandon_intent_record
from cairn.server.services.time_utils import utcnow


TEST_DISPATCHER = RequestPrincipal(kind="machine", subject_id="synthetic-dispatcher")


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    db.run_migrations(connection, "sqlite")
    connection.commit()
    yield connection
    connection.close()


def seed_project(conn, *, budget=10, historical_reserve=2, phase="baseline"):
    # 保留旧项目快照中明确记录的额度与预留值，不再计算现行执行策略。
    features = dict(
        execution_contract_version=1, execution_engine="pi", assessment_contract_version=2,
        outcome_contract_version=2, blocker_cooldown_version=1, surface_manifest_version=1,
        minimum_coverage_version=1, quality_gate_version=1, verifier_contract_version=1,
        verification_reserve=historical_reserve,
    )
    now = utcnow()
    conn.execute(
        "INSERT INTO projects (id,title,status,created_at,scenario_key,assessment_protocol,scenario_snapshot_json,assessment_progress_json,explore_budget,explore_deadline) VALUES ('p','synthetic','active',?,'default','default-copy-v1',?,?,?,'2099-01-01T00:00:00Z')",
        (now, json.dumps({"features": features}), json.dumps({"version": 1, "phase": phase, "phase_started_at": now}), budget),
    )
    for fid in ("origin", "goal", "baseline"):
        conn.execute("INSERT INTO facts (id,project_id,description,creator,created_at) VALUES (?,'p',?,'system',?)", (fid, fid, now))
    return conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()


def seed_cell(conn, *, status="explored", result="tested_no_finding", family="business_logic"):
    now = utcnow()
    conn.execute("INSERT INTO surface_manifest_entries (entry_id,project_id,surface_id,surface_kind,surface_key,traits_json,evidence_json,observation_fingerprint,created_at) VALUES ('m','p','s','endpoint','POST https://example.test/order','{}','[]','fingerprint',?)", (now,))
    conn.execute("INSERT INTO coverage_slots (project_id,slot_key,kind,description,status,surface_id,surface_key,surface_kind,test_family,coverage_requirement,result,resulting_fact_id,applicability_basis,completion_criteria,created_at,updated_at) VALUES ('p','cell','endpoint','synthetic',?,'s','POST https://example.test/order','endpoint',?,'required',?,'baseline','authorized state transition','compare baseline and controlled variant',?,?)", (status, family, result, now, now))


def seed_intent(conn, iid, *, role="discovery", depth=None):
    project = conn.execute(
        "SELECT assessment_protocol FROM projects WHERE id='p'"
    ).fetchone()
    structured = bool(
        project is not None and project["assessment_protocol"] == "default-copy-v1"
    )
    slot_key = None
    strategy_key = ""
    if structured and role == "discovery":
        if depth is not None:
            slot_key = "cell"
            strategy_key = "state-replay"
            record = {
                "version": 1,
                "kind": "depth_review",
                "input_digest": "a" * 64,
                "description": "Synthetic depth selection",
                "evidence_refs": [{"artifact_id": "synthetic"}],
                "selected": [
                    {
                        "slot_key": slot_key,
                        "strategy_key": strategy_key,
                    }
                ],
            }
            conn.execute(
                "UPDATE facts SET assessment_record_json=? WHERE project_id='p' AND id=?",
                (json.dumps(record), depth),
            )
        else:
            unassigned = conn.execute(
                """
                SELECT slot_key FROM coverage_slots
                WHERE project_id='p' AND status IN ('pending', 'exploring')
                  AND assigned_intent_id IS NULL
                ORDER BY created_at, slot_key LIMIT 1
                """
            ).fetchone()
            slot_key = unassigned["slot_key"] if unassigned is not None else f"cell:{iid}"
            strategy_key = "synthetic.baseline-v1"
            if unassigned is None:
                now = utcnow()
                conn.execute(
                    """
                    INSERT INTO coverage_slots (
                        project_id, slot_key, kind, description, status, tier,
                        surface_key, surface_kind, test_family,
                        applicability_basis, completion_criteria,
                        created_at, updated_at
                    ) VALUES ('p', ?, 'endpoint', 'synthetic', 'pending', 'core', ?,
                              'endpoint', 'authorization', 'authorized boundary',
                              'compare controlled identities', ?, ?)
                    """,
                    (slot_key, f"GET https://example.test/{iid}", now, now),
                )
    conn.execute(
        """
        INSERT INTO intents (
            id, project_id, description, creator, created_at, assessment_role,
            depth_review_fact_id, slot_key, strategy_key
        ) VALUES (?, 'p', 'synthetic', 'system', ?, ?, ?, ?, ?)
        """,
        (iid, utcnow(), role, depth, slot_key, strategy_key),
    )
    if structured and role == "discovery" and depth is None:
        conn.execute(
            "UPDATE coverage_slots SET assigned_intent_id=? WHERE project_id='p' AND slot_key=?",
            (iid, slot_key),
        )


def start(conn, iid, worker="worker"):
    return start_explore_attempt(conn, "p", iid, CreateTaskRunRequest(intent_id=iid, task_type="explore", worker_name=worker, worker_type="pi", status="running"), principal=TEST_DISPATCHER)


def finish(conn, run):
    conn.execute("UPDATE task_runs SET status='succeeded',ended_at=? WHERE project_id='p' AND id=?", (utcnow(), run.id))
    conn.execute("UPDATE intents SET concluded_at=?,conclusion='completed',worker=NULL WHERE project_id='p' AND id=?", (utcnow(), run.intent_id))
    conn.execute("UPDATE assessment_attempts SET status='completed',ended_at=? WHERE project_id='p' AND id=?", (utcnow(), run.assessment_attempt_id))


def seed_artifact(conn, aid="a", run=None):
    conn.execute("INSERT INTO artifacts (id,project_id,task_run_id,kind,uri,sha256,size,created_at) VALUES (?,'p',?,'file',?,'synthetic',1,?)", (aid, run, f"/synthetic/{aid}", utcnow()))


def patch_connection(monkeypatch, conn):
    @contextmanager
    def get_conn():
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    monkeypatch.setattr(project_reason, "get_conn", get_conn)
    monkeypatch.setattr(intents_router, "get_conn", get_conn)


def review_body(conn, *, selected=True):
    now = utcnow()
    conn.execute("UPDATE projects SET reason_worker='reasoner',reason_trigger='assessment',reason_started_at=?,reason_last_heartbeat_at=? WHERE id='p'", (now, now))
    conn.execute("INSERT INTO task_runs (id,project_id,task_type,worker_name,worker_type,status,started_at) VALUES ('review-run','p','reason','reasoner','pi','running',?)", (now,))
    seed_artifact(conn)
    conn.execute("INSERT INTO fact_evidence (project_id,fact_id,artifact_id,pointer,created_at) VALUES ('p','baseline','a','',?)", (now,))
    return DepthReviewRequest(
        worker="reasoner", task_run_id="review-run", input_digest=assessment_review_digest(conn, "p"),
        description="Evidence supports one discriminating state experiment" if selected else "Baseline evidence exposes no supported additional hypothesis",
        evidence_refs=[{"artifact_id": "a"}],
        selected=[{
            "from": ["baseline"], "description": "Compare replay against normal state transition",
            "slot_key": "cell", "strategy_key": "state-replay", "hypothesis": "Replay may bypass state transition",
            "preconditions": ["An authorized owner and an already completed order"],
            "baseline": "Normal owner completes one order", "variants": ["Replay the same operation"],
            "controls": ["A fresh owner order completes once"], "evidence_refs": [{"artifact_id": "a"}],
        }] if selected else [],
    )


def test_attempt_counts_verifier_atomically_and_does_not_charge_retry(conn):
    seed_project(conn, budget=3, historical_reserve=1)
    for iid in ("d1", "d2", "d3"):
        seed_intent(conn, iid)
    first = start(conn, "d1")
    conn.execute("UPDATE task_runs SET status='failed',error_type='worker_timeout' WHERE id=? AND project_id='p'", (first.id,))
    conn.execute("UPDATE intents SET worker=NULL WHERE project_id='p' AND id='d1'")
    retry = start(conn, "d1")
    assert retry.assessment_attempt_id == first.assessment_attempt_id
    finish(conn, retry)
    finish(conn, start(conn, "d2"))
    seed_intent(conn, "v", role="verifier")
    start(conn, "v")
    row = conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()
    assert row["explore_attempts_started"] == 3
    assert row["verification_attempts_started"] == 1
    assert conn.execute("SELECT concluded_at FROM intents WHERE id='d3' AND project_id='p'").fetchone()[0] is None


def test_old_execution_is_readable_but_not_claimable(conn):
    seed_project(conn)
    seed_intent(conn, "old")
    conn.execute("UPDATE projects SET scenario_snapshot_json='{}',assessment_progress_json=NULL WHERE id='p'")
    view = assessment_execution_view(conn, conn.execute("SELECT * FROM projects WHERE id='p'").fetchone())
    assert not view["executable"]
    assert view["rejection_reason"] == "execution_contract_unsupported"
    with pytest.raises(HTTPException, match="execution_contract_unsupported"):
        start(conn, "old")


def test_coverage_terminal_requires_atomic_depth_review_not_lease_release(conn, monkeypatch):
    seed_project(conn)
    seed_cell(conn)
    advance_assessment_phase(conn, "p")
    body = review_body(conn)
    conn.commit()
    patch_connection(monkeypatch, conn)
    project_reason.release_project_reason_record("p", HeartbeatRequest(worker="reasoner", task_run_id="review-run"))
    row = conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()
    assert row["status"] == "active"
    assert json.loads(row["assessment_progress_json"])["phase"] == "depth_review"
    with pytest.raises(HTTPException, match="depth_review"):
        require_assessment_completion(conn, "p")
    with pytest.raises(HTTPException, match="lease_mismatch"):
        project_reason.accept_depth_review("p", body)


def test_review_and_deep_experiment_preserve_baseline_and_are_idempotent(conn, monkeypatch):
    seed_project(conn)
    seed_cell(conn)
    advance_assessment_phase(conn, "p")
    body = review_body(conn)
    conn.commit()
    patch_connection(monkeypatch, conn)
    accepted = project_reason.accept_depth_review("p", body)
    again = project_reason.accept_depth_review("p", body)
    assert again.fact.id == accepted.fact.id
    intent = accepted.intents[0]
    assert intent.depth_review_fact_id == accepted.fact.id
    context = selected_experiment_context(intent, [accepted.fact])
    assert body.selected[0].hypothesis in context
    assert body.selected[0].controls[0] in context
    exported_review = next(fact for fact in yaml.safe_load(_export_yaml(conn, "p"))["facts"] if fact["id"] == accepted.fact.id)
    assert exported_review["assessment_record"] == accepted.fact.assessment_record
    with pytest.raises(HTTPException, match="explicit_disposition"):
        abandon_intent_record(conn, "p", intent.id, "reasoner")
    conn.execute("UPDATE task_runs SET status='succeeded' WHERE id='review-run' AND project_id='p'")
    run = start(conn, intent.id)
    seed_artifact(conn, "deep-evidence", run.id)
    conn.commit()
    experiment = dict(
        version=1, kind="experiment", hypothesis=body.selected[0].hypothesis,
        preconditions=["Authorized owner, completed order"], baseline="Normal owner completes one order",
        variants=["Replay the same operation"], controls=["Fresh order succeeds once"],
        observations=["Replay denied; original state preserved"], impact="No bypass under these conditions",
        limitations=[], evidence_refs=[{"artifact_id": "deep-evidence", "pointer": ""}],
    )
    conclude = ConcludeRequest(
        worker="worker", task_run_id=run.id, description="State replay comparison",
        evidence=[{"artifact_id": "deep-evidence"}], assessment_record=experiment,
        coverage_result=dict(slot_key="cell", surface_key="POST https://example.test/order", test_family="business_logic", outcome="tested_no_finding", baseline=experiment["baseline"], variants=experiment["variants"], evidence_refs=experiment["evidence_refs"]),
    )
    result = intents_router.conclude("p", intent.id, conclude)
    repeated = intents_router.conclude("p", intent.id, conclude)
    assert result.fact.id == repeated.fact.id
    assert result.fact.assessment_record["observations"] == experiment["observations"]
    exported = yaml.safe_load(_export_yaml(conn, "p"))
    exported_fact = next(fact for fact in exported["facts"] if fact["id"] == result.fact.id)
    assert exported_fact["assessment_record"] == experiment
    assert "assessment_record" not in next(fact for fact in exported["facts"] if fact["id"] == "origin")
    fact_row = conn.execute("SELECT * FROM facts WHERE project_id='p' AND id=?", (result.fact.id,)).fetchone()
    assert _fact_summary(fact_row)["assessment_record"] == experiment
    cell = conn.execute("SELECT * FROM coverage_slots WHERE project_id='p'").fetchone()
    assert cell["resulting_fact_id"] == "baseline"
    assert conn.execute("SELECT count(*) FROM coverage_slots WHERE project_id='p'").fetchone()[0] == 1
    with pytest.raises(HTTPException, match="quiescent"):
        build_assessment_report_packet(conn, "p")
    conn.execute("UPDATE task_runs SET status='succeeded' WHERE id=? AND project_id='p'", (run.id,))
    advance_assessment_phase(conn, "p")
    require_assessment_completion(conn, "p")
    complete_project_record_with_conn(conn, "p", CompleteRequest(**{"from": [result.fact.id]}, description="All obligations disposed", worker="reasoner"))
    packet = build_assessment_report_packet(conn, "p")
    assert packet.version == 2
    assert len(packet.assessment_summary["selected_deep"]) == 1
    assert len(packet.assessment_summary["coverage"]["tested"]) == 1
    assert packet.assessment_summary["coverage"]["untested"] == []
    # 接受后的响应丢失不应因项目已完成而变成失败，更不能再写一份结果。
    counts = tuple(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("facts", "intents", "assessment_attempts"))
    assert intents_router.conclude("p", intent.id, conclude).fact.id == result.fact.id
    assert project_reason.accept_depth_review("p", body).fact.id == accepted.fact.id
    with pytest.raises(HTTPException, match="assessment_result_conflict"):
        intents_router.conclude("p", intent.id, conclude.model_copy(update={"description": "conflicting late result"}))
    with pytest.raises(HTTPException, match="depth_review_already_accepted"):
        project_reason.accept_depth_review("p", body.model_copy(update={"worker": "other-worker"}))
    assert counts == tuple(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("facts", "intents", "assessment_attempts"))


@pytest.mark.parametrize("final_status", ["active", "completed", "stopped"])
def test_depth_review_receipt_survives_status_change_without_new_writes(conn, monkeypatch, final_status):
    seed_project(conn, phase="depth_review")
    seed_cell(conn)
    body = review_body(conn, selected=False)
    conn.commit()
    patch_connection(monkeypatch, conn)
    accepted = project_reason.accept_depth_review("p", body)
    conn.execute("UPDATE projects SET status=? WHERE id='p'", (final_status,))
    conn.execute("UPDATE task_runs SET status='succeeded' WHERE id='review-run'")
    conn.commit()
    before = conn.total_changes
    repeated = project_reason.accept_depth_review("p", body)
    assert repeated.fact.id == accepted.fact.id
    assert repeated.project.status == final_status
    # 唯一 SQL 写操作是获取项目行锁；不创建第二个选题或重开任务。
    assert conn.total_changes == before + 1
    assert conn.execute("SELECT count(*) FROM facts WHERE assessment_record_json IS NOT NULL").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM intents").fetchone()[0] == 0


def test_completion_response_loss_returns_same_receipt_and_rejects_conflicts(conn, monkeypatch):
    seed_project(conn, phase="depth_review")
    seed_cell(conn)
    review = review_body(conn, selected=False)
    conn.commit()
    patch_connection(monkeypatch, conn)
    project_reason.accept_depth_review("p", review)
    body = CompleteRequest(**{"from": ["baseline"]}, description="All obligations disposed", worker="reasoner", task_run_id="review-run")
    accepted = complete_project_record_with_conn(conn, "p", body)
    conn.commit()
    conn.execute("UPDATE task_runs SET status='succeeded' WHERE id='review-run'")
    conn.commit()
    repeated = complete_project_record_with_conn(conn, "p", body)
    assert repeated.id == accepted.id
    assert conn.execute("SELECT count(*) FROM intents WHERE to_fact_id='goal'").fetchone()[0] == 1
    for changes in ({"description": "changed explanation"}, {"worker": "another-worker"}):
        with pytest.raises(HTTPException, match="assessment_completion_conflict"):
            complete_project_record_with_conn(conn, "p", body.model_copy(update=changes))
    assert conn.execute("SELECT status FROM projects WHERE id='p'").fetchone()[0] == "completed"


def test_non_web_pi_bootstrap_receipts_do_not_repeat_fact_or_completion(conn, monkeypatch):
    from cairn.server.services.audit_task_runs import insert_task_run_record

    seed_project(conn)
    snapshot = {"features": {"execution_contract_version": 1, "execution_engine": "pi"}}
    conn.execute("UPDATE projects SET scenario_key='custom',assessment_protocol='',assessment_progress_json=NULL,scenario_snapshot_json=? WHERE id='p'", (json.dumps(snapshot),))
    seed_intent(conn, "bootstrap")
    conn.execute("UPDATE intents SET worker='worker' WHERE id='bootstrap'")
    run = insert_task_run_record(conn, "p", CreateTaskRunRequest(
        task_type="bootstrap", intent_id="bootstrap", worker_name="worker", worker_type="pi", status="running",
    ), principal=TEST_DISPATCHER)
    conn.commit()
    patch_connection(monkeypatch, conn)
    body = ConcludeRequest(worker="worker", task_run_id=run.id, description="Synthetic bootstrap observation")
    accepted = intents_router.conclude("p", "bootstrap", body)
    assert intents_router.conclude("p", "bootstrap", body).fact.id == accepted.fact.id
    complete = CompleteRequest(**{"from": [accepted.fact.id]}, worker="worker", task_run_id=run.id, description="Synthetic non-Web goal met")
    completed = complete_project_record_with_conn(conn, "p", complete)
    conn.commit()
    assert intents_router.conclude("p", "bootstrap", body).fact.id == accepted.fact.id
    assert complete_project_record_with_conn(conn, "p", complete).id == completed.id
    assert conn.execute("SELECT count(*) FROM facts").fetchone()[0] == 4
    assert conn.execute("SELECT count(*) FROM intents").fetchone()[0] == 2


@pytest.mark.parametrize("key", [
    "accepted_result_digest", "accepted_fact_id", "accepted_completion_digest",
    "accepted_completion_intent_id", "accepted_intent_submissions",
    "structured_intent_binding", "structured_explore_binding",
])
def test_task_run_receipts_cannot_be_supplied_by_client(conn, monkeypatch, key):
    from cairn.server.models import UpdateTaskRunRequest
    from cairn.server.services import audit_task_runs

    seed_project(conn)
    with pytest.raises(HTTPException, match="metadata_is_server_owned"):
        audit_task_runs.insert_task_run_record(conn, "p", CreateTaskRunRequest(
            task_type="reason", worker_name="reasoner", worker_type="pi", status="running",
            runtime_context={key: "forged"},
        ), principal=TEST_DISPATCHER)
    assert conn.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 0
    run = audit_task_runs.insert_task_run_record(conn, "p", CreateTaskRunRequest(
        task_type="reason", worker_name="reasoner", worker_type="pi", status="running",
    ), principal=TEST_DISPATCHER)
    conn.commit()
    patch_connection(monkeypatch, conn)
    monkeypatch.setattr(audit_task_runs, "get_conn", project_reason.get_conn)
    with pytest.raises(HTTPException, match="metadata_is_server_owned"):
        audit_task_runs.update_task_run_record("p", run.id, UpdateTaskRunRequest(runtime_context={key: "forged"}), principal=TEST_DISPATCHER)
    context = json.loads(conn.execute("SELECT runtime_context_json FROM task_runs WHERE id=?", (run.id,)).fetchone()[0])
    if key == "structured_intent_binding":
        assert isinstance(context[key], dict)
        assert context[key] != "forged"
    else:
        assert key not in context


def test_stale_manifest_traits_reject_review_without_partial_fact_or_intents(conn, monkeypatch):
    seed_project(conn)
    seed_cell(conn)
    advance_assessment_phase(conn, "p")
    body = review_body(conn)
    conn.execute("UPDATE surface_manifest_entries SET traits_json='{\"state\":\"changed\"}' WHERE project_id='p'")
    conn.commit()
    patch_connection(monkeypatch, conn)
    with pytest.raises(HTTPException, match="input_stale"):
        project_reason.accept_depth_review("p", body)
    assert conn.execute("SELECT count(*) FROM facts WHERE assessment_record_json IS NOT NULL").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM intents WHERE depth_review_fact_id IS NOT NULL").fetchone()[0] == 0


def test_zero_selection_with_evidence_is_accepted_but_running_reason_blocks_report(conn, monkeypatch):
    seed_project(conn)
    seed_cell(conn)
    advance_assessment_phase(conn, "p")
    body = review_body(conn, selected=False)
    conn.commit()
    patch_connection(monkeypatch, conn)
    accepted = project_reason.accept_depth_review("p", body)
    assert accepted.intents == []
    assert accepted.project.assessment_progress["phase"] == "finalizing"
    with pytest.raises(HTTPException, match="quiescent"):
        require_assessment_completion(conn, "p")


def test_stopped_zero_findings_report_retains_untested_and_stop_reason(conn):
    seed_project(conn)
    seed_cell(conn, status="pending", result="")
    seed_intent(conn, "queued-deep", depth="baseline")
    seed_intent(conn, "queued-verifier", role="verifier")
    stop_assessment(conn, "p", "user_stopped")
    row = conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()
    _require_report_request_eligibility(conn, row)
    packet = build_assessment_report_packet(conn, "p")
    assert packet.vulnerabilities == []
    assert packet.assessment_summary["stop_reason"] == "user_stopped"
    assert len(packet.assessment_summary["coverage"]["untested"]) == 1
    assert {row["conclusion_reason"] for row in conn.execute("SELECT * FROM intents WHERE project_id='p'")} == {"user_stopped"}


def test_unlimited_budget_still_stops_at_deadline_and_waits_for_reconciliation(conn):
    seed_project(conn, budget=None, historical_reserve=0)
    seed_intent(conn, "running")
    run = start(conn, "running")
    conn.execute("UPDATE projects SET explore_deadline='2000-01-01T00:00:00Z' WHERE id='p'")
    assert advance_assessment_phase(conn, "p") == "deadline_exceeded"
    assert conn.execute("SELECT status FROM task_runs WHERE id=? AND project_id='p'", (run.id,)).fetchone()[0] == "running"
    with pytest.raises(HTTPException, match="quiescent"):
        build_assessment_report_packet(conn, "p")
    conn.execute("UPDATE task_runs SET status='cancelled' WHERE id=? AND project_id='p'", (run.id,))
    packet = build_assessment_report_packet(conn, "p")
    assert packet.assessment_summary["stop_reason"] == "deadline_exceeded"


def test_forward_migration_does_not_invent_historical_progress_or_experiments(conn):
    seed_project(conn)
    conn.execute("UPDATE projects SET assessment_progress_json=NULL WHERE id='p'")
    before = [dict(row) for row in conn.execute("SELECT * FROM schema_migrations ORDER BY version")]
    conn.commit()
    db.run_migrations(conn, "sqlite")
    assert before == [dict(row) for row in conn.execute("SELECT * FROM schema_migrations ORDER BY version")]
    assert conn.execute("SELECT assessment_progress_json FROM projects WHERE id='p'").fetchone()[0] is None
    assert conn.execute("SELECT count(*) FROM facts WHERE assessment_record_json IS NOT NULL").fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert migrations.SQLITE_MIGRATIONS[-1].version == 41


@pytest.mark.parametrize("features", [[], "invalid", {"execution_contract_version": 1, "execution_engine": "pi", "assessment_contract_version": "broken"}])
def test_invalid_historical_policy_is_readable_not_executable(conn, features):
    seed_project(conn)
    conn.execute("UPDATE projects SET scenario_snapshot_json=? WHERE id='p'", (json.dumps({"features": features}),))
    assert not assessment_execution_view(conn, conn.execute("SELECT * FROM projects WHERE id='p'").fetchone())["executable"]


def test_budget_does_not_discard_paid_retry_or_turn_last_success_into_stop(conn):
    seed_project(conn, budget=1, historical_reserve=0, phase="deep")
    seed_cell(conn)
    conn.execute("UPDATE projects SET assessment_progress_json=? WHERE id='p'", (json.dumps({"version": 1, "phase": "deep", "depth_review_fact_id": "baseline"}),))
    seed_intent(conn, "deep", depth="baseline")
    run = start(conn, "deep")
    conn.execute("UPDATE task_runs SET status='failed',error_type='worker_timeout' WHERE project_id='p' AND id=?", (run.id,))
    conn.execute("UPDATE intents SET worker=NULL WHERE project_id='p' AND id='deep'")
    advance_assessment_phase(conn, "p")
    assert conn.execute("SELECT status FROM projects WHERE id='p'").fetchone()[0] == "active"
    retry = start(conn, "deep")
    assert retry.assessment_attempt_id == run.assessment_attempt_id
    finish(conn, retry)
    advance_assessment_phase(conn, "p")
    row = conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()
    assert row["status"] == "active"
    assert json.loads(row["assessment_progress_json"])["phase"] == "finalizing"
    assert row["explore_attempts_started"] == 1


def test_unowned_running_execution_requires_reconciliation_before_retry(conn):
    seed_project(conn)
    seed_intent(conn, "i")
    run = start(conn, "i")
    conn.execute("UPDATE intents SET worker=NULL WHERE id='i' AND project_id='p'")
    with pytest.raises(HTTPException, match="not_quiescent"):
        start(conn, "i")
    assert conn.execute("SELECT status FROM task_runs WHERE id=? AND project_id='p'", (run.id,)).fetchone()[0] == "running"


def test_unexecuted_selected_deep_remains_open_and_verifier_is_preserved(conn):
    seed_project(conn, budget=3, historical_reserve=1, phase="deep")
    seed_cell(conn)
    conn.execute("UPDATE projects SET explore_attempts_started=2,assessment_progress_json=? WHERE id='p'", (json.dumps({"version": 1, "phase": "deep", "depth_review_fact_id": "baseline"}),))
    seed_intent(conn, "deep", depth="baseline")
    seed_intent(conn, "verify", role="verifier")
    advance_assessment_phase(conn, "p")
    deep = conn.execute("SELECT * FROM intents WHERE id='deep' AND project_id='p'").fetchone()
    assert deep["concluded_at"] is None
    assert conn.execute("SELECT concluded_at FROM intents WHERE id='verify' AND project_id='p'").fetchone()[0] is None
    finish(conn, start(conn, "verify"))
    advance_assessment_phase(conn, "p")
    assert conn.execute("SELECT concluded_at FROM intents WHERE id='deep' AND project_id='p'").fetchone()[0] is None
    assert json.loads(conn.execute("SELECT assessment_progress_json FROM projects WHERE id='p'").fetchone()[0])["phase"] == "deep"


def test_terminal_audit_closes_missing_result_and_cannot_forge_acceptance(conn, monkeypatch):
    from cairn.server.models import UpdateTaskRunRequest
    from cairn.server.services import audit_task_runs
    seed_project(conn)
    seed_intent(conn, "i")
    run = start(conn, "i")
    conn.commit()
    patch_connection(monkeypatch, conn)
    monkeypatch.setattr(audit_task_runs, "get_conn", project_reason.get_conn)
    with pytest.raises(HTTPException, match="server_owned"):
        audit_task_runs.update_task_run_record("p", run.id, UpdateTaskRunRequest(runtime_context={"accepted_result_digest": "forged"}), principal=TEST_DISPATCHER)
    audit_task_runs.update_task_run_record("p", run.id, UpdateTaskRunRequest(status="succeeded"), principal=TEST_DISPATCHER)
    row = conn.execute("SELECT * FROM intents WHERE project_id='p' AND id='i'").fetchone()
    assert row["conclusion"] == "failed"
    assert row["conclusion_reason"] == "assessment_result_missing"
    assert row["to_fact_id"] is None


@pytest.mark.parametrize("error_type", [
    "provider_auth", "provider_rate_limit", "provider_error", "protocol_error",
    "contract_error", "session_error", "transport_error", "artifact_error",
])
@pytest.mark.parametrize("role", ["discovery", "verifier"])
def test_non_retryable_failure_is_durable_and_never_becomes_new_target_attempt(conn, monkeypatch, error_type, role):
    from cairn.server.models import UpdateTaskRunRequest
    from cairn.server.services import audit_task_runs
    seed_project(conn)
    seed_intent(conn, "i", role=role)
    run = start(conn, "i")
    conn.commit()
    patch_connection(monkeypatch, conn)
    monkeypatch.setattr(audit_task_runs, "get_conn", project_reason.get_conn)
    audit_task_runs.update_task_run_record("p", run.id, UpdateTaskRunRequest(status="failed", error_type=error_type), principal=TEST_DISPATCHER)
    intent_row = conn.execute("SELECT * FROM intents WHERE project_id='p' AND id='i'").fetchone()
    assert intent_row["conclusion"] == "failed"
    assert intent_row["conclusion_reason"] == error_type
    assert intent_row["worker"] is None
    assert intent_row["to_fact_id"] is None
    attempt = conn.execute("SELECT * FROM assessment_attempts WHERE project_id='p'").fetchone()
    assert attempt["status"] == "failed"
    assert attempt["outcome"] == error_type
    with pytest.raises(HTTPException):
        start(conn, "i")
    assert conn.execute("SELECT count(*) FROM task_runs WHERE project_id='p'").fetchone()[0] == 1
    assert conn.execute("SELECT explore_attempts_started FROM projects WHERE id='p'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM facts WHERE project_id='p'").fetchone()[0] == 3


@pytest.mark.parametrize("still_running", [False, True])
def test_deadline_closes_only_quiescent_attempts_and_late_timeout_cannot_retry(conn, monkeypatch, still_running):
    from cairn.server.models import UpdateTaskRunRequest
    from cairn.server.services import audit_task_runs
    seed_project(conn)
    seed_cell(conn, status="pending", result=None)
    seed_intent(conn, "i")
    run = start(conn, "i")
    if not still_running:
        conn.execute("UPDATE task_runs SET status='timed_out',error_type='worker_timeout' WHERE project_id='p' AND id=?", (run.id,))
        conn.execute("UPDATE intents SET worker=NULL WHERE project_id='p' AND id='i'")
    conn.execute("UPDATE projects SET explore_deadline='2000-01-01T00:00:00Z' WHERE id='p'")
    conn.commit()
    with pytest.raises(HTTPException, match="explore_deadline_exceeded"):
        start(conn, "i")
    attempt = conn.execute("SELECT * FROM assessment_attempts WHERE project_id='p'").fetchone()
    assert attempt["status"] == ("active" if still_running else "failed")
    if still_running:
        assert conn.execute("SELECT status FROM task_runs WHERE project_id='p' AND id=?", (run.id,)).fetchone()[0] == "running"
        with pytest.raises(HTTPException, match="quiescent"):
            build_assessment_report_packet(conn, "p")
        patch_connection(monkeypatch, conn)
        monkeypatch.setattr(audit_task_runs, "get_conn", project_reason.get_conn)
        audit_task_runs.update_task_run_record("p", run.id, UpdateTaskRunRequest(status="timed_out", error_type="worker_timeout"), principal=TEST_DISPATCHER)
    attempt = conn.execute("SELECT * FROM assessment_attempts WHERE project_id='p'").fetchone()
    assert attempt["status"] == "failed"
    assert attempt["outcome"] == "deadline_exceeded"
    assert attempt["ended_at"] is not None
    project = conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()
    assert project["status"] == "stopped"
    assert project["explore_attempts_started"] == 1
    assert conn.execute("SELECT count(*) FROM task_runs WHERE project_id='p'").fetchone()[0] == 1
    assert build_assessment_report_packet(conn, "p").version == 2


def test_phase_updates_public_coverage_checkpoint_without_completing_project(conn):
    seed_project(conn)
    seed_cell(conn)
    advance_assessment_phase(conn, "p")
    row = conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()
    assert row["coverage_checkpoint_surface_count"] == 1
    assert row["coverage_checkpoint_cell_count"] == 1
    assert row["coverage_checkpoint_terminal_count"] == 1
    assert row["coverage_checkpoint_required_count"] == 1
    assert row["coverage_checkpoint_required_terminal_count"] == 1
    assert row["status"] == "active"
    assert json.loads(row["assessment_progress_json"])["phase"] == "depth_review"


def test_generic_run_creation_cannot_bypass_atomic_admission(conn, monkeypatch):
    from cairn.server.services import audit_task_runs
    seed_project(conn)
    seed_intent(conn, "i")
    conn.commit()
    patch_connection(monkeypatch, conn)
    monkeypatch.setattr(audit_task_runs, "get_conn", project_reason.get_conn)
    with pytest.raises(HTTPException, match="admission_required"):
        audit_task_runs.create_task_run_record("p", CreateTaskRunRequest(intent_id="i", task_type="explore", worker_name="worker", worker_type="pi"), principal=TEST_DISPATCHER)
    assert conn.execute("SELECT count(*) FROM task_runs WHERE project_id='p'").fetchone()[0] == 0


@pytest.mark.parametrize("fault,error", [("worker", "lease_mismatch"), ("run", "run_mismatch"), ("evidence", "not_linked"), ("artifact", "not found"), ("deadline", "deadline_exceeded")])
def test_review_rejection_is_atomic(conn, monkeypatch, fault, error):
    seed_project(conn)
    seed_cell(conn)
    advance_assessment_phase(conn, "p")
    body = review_body(conn)
    if fault == "worker":
        body.worker = "intruder"
    elif fault == "run":
        conn.execute("UPDATE task_runs SET worker_type='claudecode' WHERE id='review-run' AND project_id='p'")
    elif fault == "evidence":
        seed_artifact(conn, "unrelated")
        body.selected[0].evidence_refs[0].artifact_id = "unrelated"
    elif fault == "artifact":
        body.evidence_refs[0].artifact_id = "unknown"
    else:
        conn.execute("UPDATE projects SET explore_deadline='2000-01-01T00:00:00Z' WHERE id='p'")
    conn.commit()
    patch_connection(monkeypatch, conn)
    with pytest.raises(HTTPException, match=error):
        project_reason.accept_depth_review("p", body)
    assert conn.execute("SELECT count(*) FROM facts WHERE assessment_record_json IS NOT NULL").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM intents WHERE depth_review_fact_id IS NOT NULL").fetchone()[0] == 0


def test_contract_protects_selection_limit_marker_and_immutable_fact(conn, monkeypatch):
    from pydantic import ValidationError
    from cairn.server.models import CreateIntentRequest
    seed_project(conn)
    seed_cell(conn)
    advance_assessment_phase(conn, "p")
    body = review_body(conn)
    with pytest.raises(ValidationError):
        DepthReviewRequest.model_validate({**body.model_dump(by_alias=True), "selected": body.model_dump(by_alias=True)["selected"] * 4})
    with pytest.raises(ValidationError):
        CreateIntentRequest.model_validate({"from": ["baseline"], "description": "forged", "worker": "w", "depth_review_fact_id": "baseline"})
    conn.commit()
    patch_connection(monkeypatch, conn)
    accepted = project_reason.accept_depth_review("p", body)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE facts SET assessment_record_json='{}' WHERE id=? AND project_id='p'", (accepted.fact.id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE intents SET depth_review_fact_id=NULL WHERE id=? AND project_id='p'", (accepted.intents[0].id,))


def test_old_frozen_report_plan_is_not_executable():
    from cairn.server.services.report_policy import parse_report_execution_plan
    with pytest.raises(ValueError, match="report_execution_contract_unsupported"):
        parse_report_execution_plan(json.dumps({"version": 1, "packet_version": 1}))
