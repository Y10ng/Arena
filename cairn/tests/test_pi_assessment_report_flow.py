"""合成 Server＋程序渲染同库集成，不是真实应用漏洞发现或 SDK/模型端到端测试。

只隔离连接与 Artifact 目录；结论、复核、处置、packet、渲染和 ready 使用真实服务。
修复对照是两个独立项目的合成证据，不自动认定修复，也不把 not_reproduced 当 remediated。
"""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import json
import sqlite3

from fastapi import HTTPException
import pytest

from cairn.assessment_contracts import (
    GOAL_DRIVEN_PROTOCOL,
    goal_driven_decision_input_digest,
)
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.worker_protocol.submission import build_submit_result_schema
from cairn.server.schemas.candidates import canonical_value_sha256
from cairn.dispatcher.tasks import report
from cairn.server import db
from cairn.server.auth import RequestPrincipal
from cairn.server.models import (
    AssessmentReportFinishRequest,
    AssessmentReportTaskRunResult,
    CompleteRequest,
    ConcludeRequest,
    CreateArtifactRequest,
    CreateIntentRequest,
    CreateTaskRunRequest,
    ReasonClaimRequest,
    UpdateProjectStatusRequest,
    UpdateTaskRunRequest,
)
from cairn.server.routers import intents as intents_router
from cairn.server.routers.export import _export_yaml
from cairn.server.services import (
    assessment_reports, audit_artifacts, audit_task_runs, project_authorization,
    project_completion, project_reason, project_records,
)
from cairn.server.services.assessment_convergence import (
    advance_goal_driven_convergence,
)
from cairn.server.services.explore_attempts import start_explore_attempt
from cairn.server.services.project_completion import _goal_driven_completion_detail
from cairn.server.services.time_utils import utcnow


TEST_DISPATCHER = RequestPrincipal(kind="machine", subject_id="synthetic-dispatcher")


@pytest.fixture
def conn(monkeypatch, tmp_path):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    db.run_migrations(connection, "sqlite")
    connection.commit()

    @contextmanager
    def get_conn():
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    for module in (
        intents_router, assessment_reports, audit_artifacts, audit_task_runs,
        project_completion, project_reason, project_records,
    ):
        monkeypatch.setattr(module, "get_conn", get_conn)
    monkeypatch.setattr(audit_artifacts, "artifact_root", lambda: tmp_path / "artifacts")
    monkeypatch.setattr(assessment_reports, "artifact_root", lambda: tmp_path / "artifacts")
    monkeypatch.setattr(db, "artifact_root", lambda: tmp_path / "artifacts")
    yield connection
    connection.close()


def _seed(conn, project_id):
    features = {
        "execution_contract_version": 1,
        "execution_engine": "pi",
        "assessment_contract_version": 2,
        "assessment_protocol": GOAL_DRIVEN_PROTOCOL,
        "outcome_contract_version": 2,
        "verifier_contract_version": 1,
        "verification_reserve": 2,
        "report_policy_version": "1",
        "report_prompt_group": GOAL_DRIVEN_PROTOCOL,
    }
    snapshot = {
        "key": "default-copy-v2",
        "prompt_group": GOAL_DRIVEN_PROTOCOL,
        "profile_id": "default-copy-v2",
        "features": features,
    }
    now = utcnow()
    conn.execute(
        """INSERT INTO projects
            (id, title, status, created_at, scenario_key, assessment_protocol,
             scenario_snapshot_json, explore_budget, explore_deadline,
             vulnerability_fingerprint_version)
           VALUES (?, 'Synthetic orchestration only', 'active', ?, 'default-copy-v2',
                   ?, ?, 10, '2099-01-01T00:00:00Z', 1)""",
        (project_id, now, GOAL_DRIVEN_PROTOCOL, json.dumps(snapshot)),
    )
    for fact_id in ("origin", "goal"):
        conn.execute(
            "INSERT INTO facts (id, project_id, description, creator, created_at) VALUES (?, ?, ?, 'system', ?)",
            (fact_id, project_id, "Synthetic " + fact_id, now),
        )
    project_authorization.initialize_project_authorization(
        conn,
        project_id,
        actor_id="synthetic-creator",
        authorized_at=now,
    )
    conn.commit()
    return intents_router.create_intent(
        project_id,
        CreateIntentRequest(
            **{
                "from": ["origin"],
                "creator": "planner",
                "description": "Synthetic identity comparison",
                "priority": 2,
            }
        ),
    )


def _start(conn, project_id, intent_id, worker):
    with conn:
        return start_explore_attempt(conn, project_id, intent_id, CreateTaskRunRequest(
            intent_id=intent_id, task_type="explore", worker_name=worker,
            worker_type="pi", session_id=f"synthetic-{project_id}-{worker}", status="running",
        ), principal=TEST_DISPATCHER)


def _artifact(
    project_id,
    run_id,
    content,
    *,
    kind="raw_output",
    filename="comparison.txt",
    metadata=None,
    category=None,
):
    request = {
        "task_run_id": run_id,
        "kind": kind,
        "filename": filename,
        "content": content,
        "metadata": metadata or {"synthetic": True},
    }
    if category is not None:
        request["category"] = category
    return audit_artifacts.create_artifact_record(
        project_id,
        CreateArtifactRequest(**request),
    )


def _terminal(project_id, run, *, status="succeeded", error_type=None):
    return audit_task_runs.update_task_run_record(
        project_id,
        run.id,
        UpdateTaskRunRequest(
            status=status,
            exit_code=0 if status == "succeeded" else None,
            error_type=error_type,
            runtime_context={
                "execution_lifecycle": {
                    "generation": run.execution_generation,
                    "process_started": True,
                    "termination_confirmed": True,
                    "cleanup_confirmed": True,
                }
            },
        ),
        principal=TEST_DISPATCHER,
    )


def _conclude(conn, project_id, intent, run, outcome):
    observed = {
        "finding": "Owner control: 200 owner; other identity: 200 owner data (synthetic).",
        "normal": "Owner control: 200 owner; other identity: 403 denied (synthetic).",
        "blocked": "Owner control: 200 owner; second approved identity unavailable (synthetic).",
    }[outcome]
    record_outcome = {
        "normal": "tested_no_finding",
        "blocked": "temporarily_blocked",
    }.get(outcome, outcome)
    artifact = _artifact(project_id, run.id, observed)
    evidence = {"artifact_id": artifact.id, "pointer": ""}
    baseline = "Owner control returns 200 and its own profile"
    variants = [] if outcome == "blocked" else [
        "Other approved identity returns "
        + ("200 owner data" if outcome == "finding" else "403 denied")
    ]
    limitations = (
        ["Only owner identity available; cross-user access was not tested"]
        if outcome == "blocked"
        else []
    )
    experiment = {
        "version": 1,
        "kind": "experiment",
        "hypothesis": "Another identity may read owner data",
        "outcome": record_outcome,
        "preconditions": [
            "Owner identity available"
            if outcome == "blocked"
            else "Two distinct approved identities available"
        ],
        "baseline": baseline,
        "variants": variants,
        "controls": ["Repeat the owner control"],
        "observations": [observed],
        "limitations": limitations,
        "evidence_refs": [evidence],
    }
    if outcome == "blocked":
        experiment.update(
            blocker_code="missing_second_principal",
            limitation_reason="Second approved identity unavailable",
            limitation_details=limitations[0],
            attempted_strategies=["Request another approved test account"],
        )
    vulnerability = None
    if outcome == "finding":
        vulnerability = {
            "title": "Synthetic object authorization finding",
            "severity": "high",
            "vuln_type": "IDOR",
            "target": "GET https://flow.invalid/profile",
            "repro_request": "GET /profile HTTP/1.1\nX-Synthetic-Identity: other",
            "repro_response": "HTTP/1.1 200 OK\n\nowner data (synthetic)",
            "packet_artifact_id": artifact.id,
            "identity": {
                "vulnerability_class": "IDOR",
                "asset": {"protocol": "https", "host": "flow.invalid"},
                "entrypoint": {"method": "GET", "route": "/profile"},
                "vector": {"parameter": "profile_id"},
            },
        }
    response = intents_router.conclude(
        project_id,
        intent.id,
        ConcludeRequest(
            worker="discoverer",
            task_run_id=run.id,
            description=observed,
            evidence=[evidence],
            assessment_record=experiment,
            vulnerability=vulnerability,
        ),
    )
    assert response.fact.assessment_record["outcome"] == record_outcome
    assert response.fact.assessment_record["observations"] == [observed]
    attempt = conn.execute(
        "SELECT status, outcome FROM assessment_attempts WHERE project_id=? AND id=?",
        (project_id, run.assessment_attempt_id),
    ).fetchone()
    assert tuple(attempt) == ("completed", record_outcome)
    assert conn.execute(
        "SELECT 1 FROM fact_evidence WHERE project_id=? AND fact_id=? AND artifact_id=?",
        (project_id, response.fact.id, artifact.id),
    ).fetchone() is not None
    _terminal(project_id, run)
    return response.fact, artifact


def _verify(conn, project_id):
    verifier = conn.execute(
        "SELECT * FROM intents WHERE project_id=? AND assessment_role='verifier'", (project_id,),
    ).fetchall()
    assert len(verifier) == 1
    run = _start(conn, project_id, verifier[0]["id"], "independent-verifier")
    artifact = _artifact(project_id, run.id, "Synthetic independent session: owner 200; other identity reads owner data 200")
    response = intents_router.conclude(project_id, verifier[0]["id"], ConcludeRequest(
        worker="independent-verifier", task_run_id=run.id, description="Synthetic independently confirmed",
        verification_result={
            "observation_id": verifier[0]["target_observation_id"], "outcome": "confirmed",
            "evidence_refs": [{"artifact_id": artifact.id}], "notes": "Distinct synthetic worker and session",
        },
    ))
    assert response.fact.task_run_id == run.id
    _terminal(project_id, run)
    stored = conn.execute("SELECT * FROM vulnerability_verifications WHERE project_id=?", (project_id,)).fetchone()
    assert stored["outcome"] == "confirmed" and stored["independence_level"] == "different_worker"
    return artifact


def _complete(conn, project_id, source_fact_id):
    project_reason.claim_project_reason_record(
        project_id,
        ReasonClaimRequest(worker="reviewer", trigger="assessment"),
    )
    run = audit_task_runs.create_task_run_record(
        project_id,
        CreateTaskRunRequest(
            task_type="reason",
            worker_name="reviewer",
            worker_type="pi",
            status="running",
        ),
        principal=TEST_DISPATCHER,
    )
    project = conn.execute(
        "SELECT * FROM projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    digest = goal_driven_decision_input_digest(
        _goal_driven_completion_detail(conn, project)
    )
    context = dict(run.runtime_context)
    context["execution_delivery"] = {
        "phase": "reason_execute",
        "binding": {
            "allowed_fact_ids": [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM facts WHERE project_id = ? AND id != 'goal' ORDER BY created_at, id",
                    (project_id,),
                ).fetchall()
            ],
            "max_intents": 4,
            "decision_input_digest": digest,
        },
    }
    conn.execute(
        "UPDATE task_runs SET runtime_context_json = ? WHERE project_id = ? AND id = ?",
        (json.dumps(context, sort_keys=True), project_id, run.id),
    )
    conn.commit()
    completed = project_completion.complete_project_record(
        project_id,
        CompleteRequest(
            **{
                "from": [source_fact_id],
                "description": "Synthetic obligations disposed",
                "worker": "reviewer",
                "task_run_id": run.id,
            }
        ),
    )
    assert completed.to == "goal"
    _terminal(project_id, run)


class _ArtifactServiceClient:
    """仅适配返回类型；Artifact 注册和文件持久化不替换为 mock。"""
    def create_artifact(self, project_id, **kwargs):
        artifact = audit_artifacts.create_artifact_record(
            project_id,
            CreateArtifactRequest(**kwargs),
        )
        return ApiResult(
            status_code=201,
            data={
                "id": artifact.id,
                "sha256": sha256(kwargs["content"].encode("utf-8")).hexdigest(),
            },
        )


def _business_snapshot(conn, project_id):
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} WHERE project_id=? ORDER BY rowid", (project_id,))]
        for table in ("facts", "intents", "coverage_slots", "assessment_attempts", "vulnerabilities",
                      "vulnerability_observations", "vulnerability_verifications", "fact_evidence")
    }


def _report_ready(conn, project_id):
    with conn:
        requested = assessment_reports.request_assessment_report_generation(
            conn, project_id, template_key="standard-pentest", template_version=1, requested_by="synthetic-user",
        )
    claim = assessment_reports.claim_assessment_report_finalization(project_id, "reporter", principal=TEST_DISPATCHER)
    assert claim.generation_token == requested.generation_token
    packet = assessment_reports.build_assessment_report_packet(conn, project_id)
    packet_json = packet.model_dump_json()
    frozen = json.dumps({"version": 1, "report_packet": json.loads(packet_json), "graph_yaml": _export_yaml(conn, project_id)}, ensure_ascii=False)
    digest = sha256(frozen.encode()).hexdigest()
    plan = json.loads(claim.report_execution_plan_json)
    snapshot = _artifact(
        project_id,
        claim.task_run_id,
        frozen,
        kind="report",
        filename="report-input-snapshot.json",
        category="deliverable",
        metadata={
            "source": "report_task",
            "format": "json",
            "report_generation_token": claim.generation_token,
            "report_template_key": claim.template_key,
            "report_template_version": claim.template_version,
            "report_template_digest_sha256": claim.template_digest_sha256,
            "report_execution_plan_sha256": claim.report_execution_plan_sha256,
            "report_prompt_digest_sha256": claim.report_prompt_digest_sha256,
            "report_policy_version": plan["report_policy_version"],
            "packet_version": plan["packet_version"],
            "snapshot_sha256": digest,
        },
    )
    assert snapshot.sha256 == digest
    assessment_reports.bind_assessment_report_input_snapshot(
        project_id,
        "reporter",
        claim_token=claim.claim_token,
        artifact_id=snapshot.id,
        digest_sha256=digest,
        principal=TEST_DISPATCHER,
    )
    before = _business_snapshot(conn, project_id)
    data = {
        "executive_summary": "Synthetic orchestration evidence only; no real target or repair was executed.",
        "findings": [{
            "vulnerability_id": vuln["id"], "impact": "Limited to synthetic comparison",
            "root_cause": "Synthetic authorization condition, not a source-code diagnosis",
            "repro_steps": "Repeat the recorded synthetic identities and control",
            "remediation": "Proposed authorization check; not implemented",
            "regression_steps": "Compare a separate synthetic negative with its normal owner control",
            "limitations": "No actual target or remediation execution",
        } for vuln in packet.vulnerabilities],
        "limitations": "No automatic repaired/remediated determination", "artifacts": [],
    }
    report_identity = report._write_report_results(
        _ArtifactServiceClient(), project_id, claim.task_run_id, data=data, report_packet_json=packet_json,
        report_generation_token=claim.generation_token, input_snapshot_artifact_id=snapshot.id,
        input_snapshot_sha256=digest, report_template_key=claim.template_key,
        report_template_version=claim.template_version, report_template_digest_sha256=claim.template_digest_sha256,
        report_policy_version=plan["report_policy_version"],
        report_execution_plan_sha256=claim.report_execution_plan_sha256,
        report_prompt_digest_sha256=claim.report_prompt_digest_sha256,
        completeness_rule_version=plan["completeness_rule_version"],
    )
    assert isinstance(report_identity, report.ReportArtifactIdentity)
    finalization_row = conn.execute(
        "SELECT * FROM assessment_report_finalizations WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    report_row = conn.execute(
        "SELECT * FROM artifacts WHERE project_id = ? AND id = ?",
        (project_id, report_identity.artifact_id),
    ).fetchone()
    expected_metadata = assessment_reports._expected_report_metadata(
        finalization_row,
        snapshot_artifact_id=snapshot.id,
        snapshot_sha256=digest,
    )
    actual_metadata = json.loads(report_row["metadata_json"])
    assert actual_metadata == {**expected_metadata, "runtime_context": actual_metadata["runtime_context"]}
    assert report_row["sha256"] == report_identity.sha256
    finished = assessment_reports.finish_assessment_report_finalization(
        project_id,
        AssessmentReportFinishRequest(
            protocol="cairn-assessment-report-finish-v1",
            worker="reporter",
            claim_token=claim.claim_token,
            generation_token=claim.generation_token,
            task_run_id=claim.task_run_id,
            status="ready",
            artifact_id=report_identity.artifact_id,
            artifact_sha256=report_identity.sha256,
            input_snapshot_artifact_id=snapshot.id,
            input_snapshot_sha256=digest,
            task_run_result=AssessmentReportTaskRunResult(
                status="succeeded",
                exit_code=0,
            ),
        ),
        principal=TEST_DISPATCHER,
    )
    assert finished.status == "ready" and finished.input_snapshot_artifact_id == snapshot.id
    assert _business_snapshot(conn, project_id) == before
    artifact = next(item for item in audit_artifacts.list_artifact_records(project_id) if item.id == finished.artifact_id)
    assert artifact.task_run_id == claim.task_run_id
    assert artifact.metadata["report_generation_token"] == claim.generation_token
    assert artifact.metadata["input_snapshot_sha256"] == digest
    preview = audit_artifacts.read_artifact_preview_record(project_id, artifact.id, tail=False)
    assert not preview.truncated and sha256(preview.content.encode()).hexdigest() == artifact.sha256
    run = conn.execute("SELECT * FROM task_runs WHERE project_id=? AND id=?", (project_id, claim.task_run_id)).fetchone()
    assert run["worker_name"] == "reporter" and run["status"] == "succeeded"
    assert run["ended_at"] is not None
    return packet, preview.content, artifact


def _flow(conn, project_id, outcome):
    intent = _seed(conn, project_id)
    run = _start(conn, project_id, intent.id, "discoverer")
    if outcome == "interrupted":
        _artifact(
            project_id,
            run.id,
            "Synthetic owner control captured; variant interrupted before a conclusion",
        )
        project_records.update_project_status_record(
            project_id,
            UpdateProjectStatusRequest(status="stopped"),
        )
        with pytest.raises(HTTPException, match="quiescent"):
            assessment_reports.build_assessment_report_packet(conn, project_id)
        _terminal(project_id, run, status="cancelled", error_type="cancelled")
        attempt = conn.execute(
            "SELECT status, outcome FROM assessment_attempts WHERE project_id=?",
            (project_id,),
        ).fetchone()
        assert tuple(attempt) == ("failed", "user_stopped")
        assert conn.execute(
            "SELECT count(*) FROM facts WHERE project_id=?",
            (project_id,),
        ).fetchone()[0] == 2
    else:
        fact, _ = _conclude(conn, project_id, intent, run, outcome)
        if outcome == "finding":
            _verify(conn, project_id)
        if outcome == "blocked":
            project_records.update_project_status_record(
                project_id,
                UpdateProjectStatusRequest(status="stopped"),
            )
        else:
            _complete(conn, project_id, fact.id)
    packet, markdown, artifact = _report_ready(conn, project_id)
    coverage = packet.assessment_summary["coverage"]
    if outcome == "finding":
        assert len(packet.vulnerabilities) == len(packet.observations) == len(packet.verifications) == 1
        assert packet.vulnerabilities[0]["status"] == "confirmed"
        assert packet.observation_statuses[0]["verification_status"] == "confirmed"
        assert "confirmed" in markdown and "Canonical 漏洞记录数：1；Observation 数：1" in markdown
        assert packet.verifications[0]["evidence"][0]["artifact_id"] in markdown
        assert len(coverage["tested"]) == 1
        assert coverage["tested"][0]["intent_id"] == intent.id
        assert coverage["tested"][0]["result"] == "finding"
    else:
        assert packet.vulnerabilities == packet.observations == packet.verifications == []
        if outcome == "normal":
            assert packet.project["status"] == "completed"
            assert len(coverage["tested"]) == 1
            assert coverage["tested"][0]["intent_id"] == intent.id
            assert coverage["tested"][0]["result"] == "tested_no_finding"
            assert coverage["blocked"] == coverage["untested"] == []
            assert "tested_no_finding" in markdown
        else:
            assert packet.project["status"] == "stopped" and packet.assessment_summary["stop_reason"] == "user_stopped"
            assert "# 安全评估部分报告" in markdown and "user_stopped" in markdown
            assert coverage["tested"] == []
            if outcome == "blocked":
                assert len(coverage["blocked"]) == 1
                assert coverage["blocked"][0]["intent_id"] == intent.id
                assert coverage["blocked"][0]["result"] == "temporarily_blocked"
                assert "Second approved identity unavailable" in markdown
            else:
                assert len(coverage["untested"]) == 1 and coverage["blocked"] == []
                assert coverage["untested"][0]["intent_id"] == intent.id
                assert coverage["untested"][0]["result"] == "abandoned"
                assert coverage["untested"][0]["reason"] == "user_stopped"
                assert "tested_no_finding" not in markdown
    return packet, markdown, artifact


@pytest.mark.parametrize("mutation", ["valid", "missing", "foreign", "duplicate", "object"])
def test_goal_driven_reason_delivery_registration_binds_project_intent_ids(conn, mutation):
    project_id = "synthetic-reason-binding"
    intent = _seed(conn, project_id)
    project_reason.claim_project_reason_record(
        project_id, ReasonClaimRequest(worker="reviewer", trigger="graph_changed")
    )
    run = audit_task_runs.create_task_run_record(
        project_id,
        CreateTaskRunRequest(task_type="reason", worker_name="reviewer", worker_type="pi", status="running"),
        principal=TEST_DISPATCHER,
    )
    binding = {
        "allowed_fact_ids": ["origin"], "allowed_intent_ids": [intent.id],
        "max_intents": 3, "decision_input_digest": "a" * 64,
    }
    schema_digest = canonical_value_sha256(build_submit_result_schema(
        "reason_execute", binding, inputs={"open_intent_ids": [intent.id]},
    ))
    if mutation == "missing":
        binding.pop("allowed_intent_ids")
    elif mutation == "foreign":
        binding["allowed_intent_ids"] = ["i-foreign"]
    elif mutation == "duplicate":
        binding["allowed_intent_ids"] *= 2
    elif mutation == "object":
        binding["allowed_intent_ids"] = [{"description": "synthetic invalid"}]
    snapshot = {
        "session_id": "pi:11111111-1111-4111-8111-111111111111",
        "phase": "reason_execute", "deadline_at": "2098-01-01T00:00:00Z",
        "container_id": "original-container", "model_binding_sha256": "a" * 64,
        "result_schema_sha256": schema_digest,
        "binding": binding, "inputs": {"open_intent_ids": [intent.id]},
    }
    request = UpdateTaskRunRequest(
        session_id=snapshot["session_id"], runtime_context={"execution_delivery": snapshot},
    )
    if mutation == "valid":
        result = audit_task_runs.update_task_run_record(
            project_id, run.id, request, principal=TEST_DISPATCHER,
        )
        assert result.runtime_context["execution_delivery"] == snapshot
    else:
        with pytest.raises(HTTPException) as exc:
            audit_task_runs.update_task_run_record(
                project_id, run.id, request, principal=TEST_DISPATCHER,
            )
        assert exc.value.status_code == 422
        stored = conn.execute(
            "SELECT runtime_context_json FROM task_runs WHERE project_id=? AND id=?",
            (project_id, run.id),
        ).fetchone()
        assert "execution_delivery" not in json.loads(stored[0] or "{}")


@pytest.mark.parametrize("outcome", ["finding", "tested_no_finding"])
def test_goal_driven_conclude_accepts_evidence_without_universal_comparison(conn, outcome):
    project_id = "synthetic-experiment"
    intent = _seed(conn, project_id)
    run = _start(conn, project_id, intent.id, "discoverer")
    artifact = _artifact(project_id, run.id, "Synthetic observation")
    evidence = {"artifact_id": artifact.id}
    record = {
        "hypothesis": "Synthetic authorization boundary",
        "outcome": outcome,
        "observations": ["Synthetic observation"],
        "evidence_refs": [evidence],
    }
    vulnerability = None
    if outcome == "finding":
        vulnerability = {
            "title": "Synthetic boundary finding", "severity": "high",
            "vuln_type": "IDOR", "target": "GET https://flow.invalid/item",
            "repro_request": "GET /item HTTP/1.1",
            "repro_response": "HTTP/1.1 200 OK",
            "identity": {
                "vulnerability_class": "IDOR",
                "asset": {"protocol": "https", "host": "flow.invalid"},
                "entrypoint": {"method": "GET", "route": "/item"},
                "vector": {"parameter": "id"},
            },
        }
    result = intents_router.conclude(project_id, intent.id, ConcludeRequest(
        worker="discoverer", task_run_id=run.id, description="Synthetic observation",
        evidence=[evidence], assessment_record=record, vulnerability=vulnerability,
    ))
    assert result.fact.assessment_record["baseline"] == ""
    assert result.fact.assessment_record["variants"] == []
    assert result.fact.assessment_record["controls"] == []
    if outcome == "finding":
        assert conn.execute("SELECT status FROM vulnerabilities WHERE project_id=?", (project_id,)).fetchone()[0] == "new"


@pytest.mark.parametrize("invalid", [
    "observations", "evidence", "foreign_artifact", "identity", "reproduction",
])
def test_goal_driven_conclude_rejects_incomplete_or_foreign_proof(conn, invalid):
    project_id = "synthetic-experiment"
    intent = _seed(conn, project_id)
    run = _start(conn, project_id, intent.id, "discoverer")
    artifact = _artifact(project_id, run.id, "Synthetic observation")
    evidence = {"artifact_id": artifact.id}
    if invalid == "foreign_artifact":
        other_intent = _seed(conn, "synthetic-other")
        other_run = _start(conn, "synthetic-other", other_intent.id, "other")
        _artifact("synthetic-other", other_run.id, "Foreign synthetic first artifact")
        evidence = {"artifact_id": _artifact("synthetic-other", other_run.id, "Foreign synthetic data").id}
    record = {
        "hypothesis": "Synthetic authorization boundary", "outcome": "finding",
        "observations": ["Synthetic observation"], "evidence_refs": [evidence],
    }
    vulnerability = {
        "title": "Synthetic boundary finding", "severity": "high",
        "vuln_type": "IDOR", "target": "GET https://flow.invalid/item",
        "repro_request": "GET /item HTTP/1.1", "repro_response": "HTTP/1.1 200 OK",
        "identity": {
            "vulnerability_class": "IDOR",
            "asset": {"protocol": "https", "host": "flow.invalid"},
            "entrypoint": {"method": "GET", "route": "/item"},
            "vector": {"parameter": "id"},
        },
    }
    if invalid == "observations":
        record.pop("observations")
    elif invalid == "evidence":
        record["evidence_refs"] = []
    elif invalid == "identity":
        vulnerability.pop("identity")
    elif invalid == "reproduction":
        vulnerability.pop("repro_request")
        vulnerability.pop("repro_response")
    with pytest.raises(HTTPException) as exc:
        intents_router.conclude(project_id, intent.id, ConcludeRequest(
            worker="discoverer", task_run_id=run.id, description="Synthetic observation",
            evidence=[evidence], assessment_record=record, vulnerability=vulnerability,
        ))
    assert exc.value.status_code == 422
    assert conn.execute("SELECT count(*) FROM facts WHERE project_id=?", (project_id,)).fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM vulnerabilities WHERE project_id=?", (project_id,)).fetchone()[0] == 0


@pytest.mark.parametrize("outcome", ["finding", "normal", "blocked", "interrupted"])
def test_synthetic_server_and_render_flow_reaches_owned_ready_report(conn, outcome):
    _flow(conn, "synthetic-flow", outcome)


@pytest.mark.parametrize("stop_reason", ["budget_exhausted", "deadline_exceeded"])
def test_hard_stop_keeps_accepted_blocker_separate_from_execution_stop(conn, stop_reason):
    project_id = "synthetic-blocked-stop"
    intent = _seed(conn, project_id)
    run = _start(conn, project_id, intent.id, "discoverer")
    fact, _ = _conclude(conn, project_id, intent, run, "blocked")
    with conn:
        advance_goal_driven_convergence(conn, project_id, reason=stop_reason)
    packet, markdown, _ = _report_ready(conn, project_id)
    coverage = packet.assessment_summary["coverage"]
    assert packet.assessment_summary["stop_reason"] == stop_reason
    assert coverage["tested"] == coverage["untested"] == []
    assert len(coverage["blocked"]) == 1
    blocked = coverage["blocked"][0]
    assert blocked["intent_id"] == intent.id
    assert blocked["result"] == "temporarily_blocked"
    assert blocked["fact_id"] == fact.id
    assert "Second approved identity unavailable" in blocked["reason"]
    assert blocked["attempted_strategies"] == [
        "Request another approved test account"
    ]
    assert stop_reason in markdown and "Second approved identity unavailable" in markdown


def test_separate_synthetic_repair_control_does_not_relabel_original_finding(conn):
    finding, original_markdown, original_artifact = _flow(conn, "synthetic-before", "finding")
    original = _business_snapshot(conn, "synthetic-before")
    negative, negative_markdown, negative_artifact = _flow(conn, "synthetic-after-control", "normal")
    assert _business_snapshot(conn, "synthetic-before") == original
    assert finding.vulnerabilities[0]["status"] == "confirmed"
    assert negative.vulnerabilities == []
    assert "tested_no_finding" in negative_markdown and "修复后回归方法（未执行）" in original_markdown
    assert original_artifact.project_id != negative_artifact.project_id
    assert original_artifact.metadata["report_generation_token"] != negative_artifact.metadata["report_generation_token"]
    assert audit_artifacts.read_artifact_preview_record("synthetic-before", original_artifact.id, tail=False).content == original_markdown
    assert conn.execute("SELECT count(*) FROM vulnerabilities WHERE status='remediated'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM vulnerability_verifications WHERE outcome='not_reproduced'").fetchone()[0] == 0
