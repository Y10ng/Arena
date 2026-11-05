"""纯服务单元验证：研究代际不替代报告自身的 claim／owner fence。"""
from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from cairn.dispatcher.tasks.delivery import result_schema_digest
from cairn.server.services import assessment_attempts as attempts


SESSION = "pi:11111111-1111-4111-8111-111111111111"
NOW = "2050-01-01T00:00:00Z"


class _Rows(list):
    def fetchone(self):
        return self[0] if self else None

    def fetchall(self):
        return list(self)


def _fixture():
    project = {"id": "p", "execution_generation": 2, "explore_deadline": "2049-01-01T00:00:00Z",
               "assessment_progress_json": "{}"}
    claim = {"scope_key": "", "generation_token": "report-generation", "attempt_count": 1,
             "claim_sha256": "a" * 64}
    context = {"report_claim": claim, "dispatcher_id": "dispatcher", "execution_lifecycle": {
        "generation": 1, "process_started": True, "termination_confirmed": True, "cleanup_confirmed": False,
    }}
    run = {"id": "r1", "project_id": "p", "task_type": "report", "intent_id": None,
           "assessment_attempt_id": None, "worker_name": "report-worker", "execution_generation": 1,
           "status": "running", "pause_requested": 0, "pause_interrupted": 0, "session_id": SESSION,
           "runtime_context_json": json.dumps(context), "error_type": None, "ended_at": NOW}
    finalization = {"project_id": "p", "generation_token": "report-generation", "scope_key": "",
                    "status": "running", "claimed_by": "report-worker", "claim_sha256": "a" * 64,
                    "attempt_count": 1, "cancel_requested_at": None, "next_attempt_at": None,
                    "input_snapshot_artifact_id": "a1", "input_snapshot_sha256": "b" * 64,
                    "template_digest_sha256": "c" * 64}
    inputs = {"snapshot_path": "/tmp/cairn-prompts/report-delivery-r1.json", "input_snapshot_artifact_id": "a1",
              "input_snapshot_sha256": "b" * 64, "generation_token": "report-generation"}
    binding = {"report_segments": {"project_id": "p", "task_run_id": "r1", "generation_token": "report-generation",
                                    "input_snapshot_sha256": "b" * 64, "report_template_digest_sha256": "c" * 64}}
    snapshot = {"session_id": SESSION, "phase": "report_execute", "deadline_at": "2050-01-01T00:05:00Z",
                "container_id": "report-container", "model_binding_sha256": "d" * 64,
                "result_schema_sha256": result_schema_digest("report_execute", binding, inputs),
                "binding": binding, "inputs": inputs}
    context["execution_delivery"] = snapshot
    run["runtime_context_json"] = json.dumps(context)

    def execute(query, params):
        if "FROM task_runs WHERE session_id" in query:
            return _Rows()
        if "FROM task_runs WHERE project_id" in query:
            return _Rows([run])
        if "FROM assessment_report_finalizations" in query:
            return _Rows([finalization])
        raise AssertionError(f"unexpected query: {query}")

    return project, run, finalization, snapshot, SimpleNamespace(execute=execute)


@pytest.mark.parametrize("generation", [0, 2, 3])
def test_whole_report_registration_uses_report_claim_not_research_generation(monkeypatch, generation):
    monkeypatch.setattr(attempts, "utcnow", lambda: NOW)
    project, run, finalization, snapshot, conn = _fixture()
    project["execution_generation"] = generation
    attempts.validate_delivery_registration(conn, project, run, SimpleNamespace(status=None, session_id=SESSION), snapshot)


@pytest.mark.parametrize("changed", ["missing_generation", "pause_requested", "worker", "claim", "attempt", "cancelled"])
def test_report_registration_preserves_its_own_fences(monkeypatch, changed):
    monkeypatch.setattr(attempts, "utcnow", lambda: NOW)
    project, run, finalization, snapshot, conn = _fixture()
    if changed == "missing_generation":
        context = json.loads(run["runtime_context_json"])
        context["report_claim"].pop("generation_token")
        run["runtime_context_json"] = json.dumps(context)
    elif changed == "pause_requested":
        run["pause_requested"] = 1
    elif changed == "worker":
        finalization["claimed_by"] = "other-worker"
    elif changed == "claim":
        finalization["claim_sha256"] = "e" * 64
    elif changed == "attempt":
        finalization["attempt_count"] = 2
    elif changed == "cancelled":
        finalization["cancel_requested_at"] = NOW
    with pytest.raises(HTTPException) as denied:
        attempts.validate_delivery_registration(conn, project, run, SimpleNamespace(status=None, session_id=SESSION), snapshot)
    assert denied.value.detail in {"execution_delivery_registration_closed", "execution_delivery_invalid"}


def test_report_retry_chain_survives_research_generation_but_not_foreign_report():
    project, original, _, _, _ = _fixture()
    original.update(status="failed", error_type="submission_missing")
    retried = deepcopy(original)
    retried.update(id="r2", execution_generation=2)
    context = json.loads(retried["runtime_context_json"])
    context["delivery_retry"] = {"source_task_run_id": "r1"}
    retried["runtime_context_json"] = json.dumps(context)
    assert attempts._delivery_predecessor_matches([original, retried], retried)
    original["execution_generation"] = retried["execution_generation"]
    context["report_claim"]["generation_token"] = "foreign-report"
    retried["runtime_context_json"] = json.dumps(context)
    assert not attempts._delivery_predecessor_matches([original, retried], retried)


def test_report_retry_counts_old_report_attempts_instead_of_restarting_fresh():
    project, run, _, _, conn = _fixture()
    run.update(status="failed", error_type="submission_missing")
    assert attempts._logical_executions(conn, project, "report", None, "report-generation") == [run]
    assert attempts._logical_executions(conn, project, "report", None, "foreign-report") == []
    assert attempts.execution_retry_eligibility(conn, project, "report", report_generation_token="report-generation", now=NOW) == {
        "allowed": True, "reason": None, "retry_at": None,
    }
    run["pause_requested"] = 1
    assert attempts.execution_retry_eligibility(conn, project, "report", report_generation_token="report-generation", now=NOW)["reason"] == "execution_delivery_unavailable"


@pytest.mark.parametrize("research_generation", [0, 2])
def test_report_routes_use_active_report_generation_not_research_generation(research_generation):
    project, run, finalization, _, conn = _fixture()
    project["execution_generation"] = research_generation
    run.update(status="failed", error_type="submission_missing")
    routes = attempts.delivery_retry_routes(conn, project, executions=[run])
    assert routes["r1"]["container_id"] == "report-container"
    finalization["generation_token"] = "new-report-generation"
    assert attempts.delivery_retry_routes(conn, project, executions=[run]) == {}


def test_research_delivery_still_requires_current_research_generation():
    project, run, _, _, conn = _fixture()
    run["task_type"] = "bootstrap"
    assert attempts._logical_executions(conn, project, "bootstrap", None) == []
    run.update(status="failed", error_type="submission_missing")
    assert not attempts._delivery_source_usable(project, run, NOW)
