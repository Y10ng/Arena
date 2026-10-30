"""只在自建 PostgreSQL 中验证历史单条收尾与项目总报告；不运行模型或访问目标。"""

from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.scheduler.cleanup import CleanupMixin
from cairn.server.auth import RequestPrincipal
from cairn.server.services import assessment_reports as reports, report_templates
from cairn.server.services.vulnerability_reports import read_accepted_vulnerability_report
from test_report_frozen_rendering import TEMPLATE, _data
from test_report_templates import defaults_v60_conn, private_postgres, _migrate_defaults_v60, _seed_project


PRINCIPAL = RequestPrincipal(kind="machine", subject_id="synthetic-dispatcher")


@pytest.fixture
def scoped_conn(defaults_v60_conn, monkeypatch, tmp_path):
    conn = defaults_v60_conn
    _migrate_defaults_v60(conn, max_version=63)
    _seed_project(conn)
    conn.execute("UPDATE projects SET status = 'active' WHERE id = 'p1'")
    for key in ("v1", "v2"):
        conn.execute(
            """INSERT INTO vulnerabilities
            (id, project_id, title, severity, created_at, updated_at)
            VALUES (?, 'p1', ?, 'high', 'before', 'before')""", (key, key),
        )
    for key in ("o1", "o2"):
        conn.execute(
            """INSERT INTO vulnerability_observations
            (id, project_id, vulnerability_id, severity, confidence, observed_at)
            VALUES (?, 'p1', 'v1', 'high', 0.8, ?)""", (key, key),
        )

    @contextmanager
    def connection():
        yield conn

    monkeypatch.setattr(reports, "get_conn", connection)
    monkeypatch.setattr(reports, "artifact_root", lambda: tmp_path)
    return conn, tmp_path


@pytest.fixture
def historical_scope_conn(scoped_conn):
    conn, _ = scoped_conn
    # 仅布置切换前已持久化的请求；测试不得借用已删除的新建单条入口。
    policy = reports.report_policy_from_project_row(reports.get_project_or_404(conn, "p1"))
    plan, prompt = reports.freeze_report_execution_plan(policy)
    template = report_templates.get_report_template(conn, "vulnerability-report")
    now = reports.utcnow()
    for vulnerability_id, observation_id in (("v1", "o1"), ("v1", "o2"), ("v2", None)):
        packet = reports._build_scoped_report_packet(conn, "p1", vulnerability_id, observation_id)
        values = {
            "project_id": "p1", "scope_key": packet.scope_key,
            "vulnerability_id": vulnerability_id, "observation_id": observation_id,
            "frozen_packet_json": packet.model_dump_json(),
            "status": "pending", "generation_started_at": now, "generation_token": uuid4().hex,
            "requested_at": now, "requested_by": "synthetic-user",
            "template_key": template.key, "template_digest_sha256": template.digest_sha256,
            "template_markdown": template.markdown_content,
            "template_profiles_json": json.dumps(template.compatibility_profiles),
            "report_policy_version": policy.version, "report_execution_plan_json": plan.canonical_json(),
            "report_execution_plan_sha256": plan.digest_sha256(),
            "report_prompt_digest_sha256": plan.report_prompt_digest_sha256, "report_prompt_markdown": prompt,
            "created_at": now, "updated_at": now,
        }
        conn.execute(
            f"INSERT INTO assessment_report_finalizations ({', '.join(values)}) "
            f"VALUES ({', '.join('?' for _ in values)})", tuple(values.values()),
        )
    return scoped_conn


def historical_state(conn, vulnerability_id="v1", observation_id="o1"):
    return reports.get_assessment_report_finalization(
        conn, "p1", vulnerability_id=vulnerability_id, observation_id=observation_id,
    )


def claim(state):
    return reports.claim_assessment_report_finalization(
        "p1", "worker", generation_token=state.generation_token, principal=PRINCIPAL,
    )


def finish_body(claimed, **kwargs):
    return reports.AssessmentReportFinishRequest(
        protocol="cairn-assessment-report-finish-v1", worker="worker",
        generation_token=claimed.generation_token, claim_token=claimed.claim_token,
        task_run_id=claimed.task_run_id, **kwargs,
    )


def artifact(conn, root, claimed, key, suffix, value, metadata):
    content = value.encode("utf-8") if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode()
    path = root / "projects" / "p1" / "runs" / claimed.task_run_id / f"{key}-{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    digest = sha256(content).hexdigest()
    conn.execute(
        """INSERT INTO artifacts
        (id, project_id, task_run_id, kind, category, uri, sha256, size, created_at, metadata_json)
        VALUES (?, 'p1', ?, 'report', 'deliverable', ?, ?, ?, 'now', ?)""",
        (key, claimed.task_run_id,
         f"artifact://projects/p1/runs/{claimed.task_run_id}/{key}-{suffix}",
         digest, len(content), json.dumps(metadata)),
    )
    return digest


def accept(conn, root, claimed, *, project_data=None):
    row = reports._finalization_row(conn, "p1", generation_token=claimed.generation_token)
    packet = reports.build_assessment_report_packet(conn, "p1", generation_token=claimed.generation_token).model_dump(mode="json")
    snapshot = {
        "version": 1, "graph_yaml": "facts: []\n", "report_packet": packet,
        "report_template": {
            "key": claimed.template_key, "markdown_content": claimed.template_markdown,
            "digest_sha256": claimed.template_digest_sha256,
            "compatibility_profiles": claimed.template_compatibility_profiles,
        },
    }
    snapshot_id = "snapshot-" + claimed.generation_token
    output_id = "output-" + claimed.generation_token
    snapshot_sha = sha256(json.dumps(snapshot, ensure_ascii=False).encode()).hexdigest()
    metadata = reports._expected_report_metadata(row, snapshot_artifact_id=snapshot_id, snapshot_sha256=snapshot_sha)
    snapshot_metadata = {key: value for key, value in metadata.items() if key not in {
        "input_snapshot_artifact_id", "input_snapshot_sha256", "completeness_rule_version",
    }}
    snapshot_metadata.update(format="json", snapshot_sha256=snapshot_sha, runtime_context={})
    artifact(conn, root, claimed, snapshot_id, "report-input-snapshot.json", snapshot, snapshot_metadata)
    reports.bind_assessment_report_input_snapshot(
        "p1", "worker", claim_token=claimed.claim_token, artifact_id=snapshot_id,
        digest_sha256=snapshot_sha, principal=PRINCIPAL,
    )
    data = {
        "schema_version": 4, "executive_summary": "仅整理已有材料。",
        "overall_remediation": "需要复测。", "limitations": "无新增测试。",
        "findings": [{
            "vulnerability_id": packet["vulnerability_id"], "observation_id": packet["observation_id"],
            "description": "整理成功的说明。", "steps": [], "remediation": "建议复核权限。", "limitations": "证据不足。",
        }],
    }
    if project_data is None:
        digest = artifact(conn, root, claimed, output_id, "vulnerability-readable-report.json", data, {**metadata, "runtime_context": {}})
    else:
        from cairn.dispatcher.tasks import report

        def create_output(project_id, **kwargs):
            assert project_id == "p1" and kwargs["task_run_id"] == claimed.task_run_id
            digest = artifact(conn, root, claimed, output_id, kwargs["filename"], kwargs["content"], {
                **kwargs["metadata"], "runtime_context": {},
            })
            return ApiResult(status_code=201, data={"id": output_id, "sha256": digest})

        plan = json.loads(claimed.report_execution_plan_json)
        identity = report._write_report_results(
            SimpleNamespace(create_artifact=create_output), "p1", claimed.task_run_id,
            data=project_data, report_packet_json=json.dumps(packet, ensure_ascii=False),
            report_template_markdown=claimed.template_markdown,
            report_generation_token=claimed.generation_token, report_template_key=claimed.template_key,
            report_template_digest_sha256=claimed.template_digest_sha256,
            report_policy_version=plan["report_policy_version"],
            report_execution_plan_sha256=claimed.report_execution_plan_sha256,
            report_prompt_digest_sha256=claimed.report_prompt_digest_sha256,
            completeness_rule_version=plan["completeness_rule_version"],
            input_snapshot_artifact_id=snapshot_id, input_snapshot_sha256=snapshot_sha,
        )
        assert isinstance(identity, report.ReportArtifactIdentity)
        assert identity.artifact_id == output_id
        digest = identity.sha256
    body = finish_body(
        claimed, status="ready", artifact_id=output_id, artifact_sha256=digest,
        input_snapshot_artifact_id=snapshot_id, input_snapshot_sha256=snapshot_sha,
        task_run_result={"status": "succeeded", "exit_code": 0},
    )
    receipt = reports.finish_assessment_report_finalization("p1", body, principal=PRINCIPAL)
    assert reports.finish_assessment_report_finalization("p1", body, principal=PRINCIPAL) == receipt
    replay = reports.AssessmentReportFinishReplayRequest(
        **body.model_dump(exclude={"claim_token"}),
        claim_sha256=sha256(claimed.claim_token.encode()).hexdigest(), request_sha256=receipt.request_sha256,
    )
    assert reports.replay_assessment_report_finalization("p1", replay, principal=PRINCIPAL) == receipt
    return output_id


def test_historical_scope_freeze_claim_finish_and_accepted_survive(historical_scope_conn):
    conn, root = historical_scope_conn
    whole = reports.ensure_assessment_report_finalization(conn, "p1")
    state = historical_state(conn)
    other = historical_state(conn, observation_id="o2")
    manual = historical_state(conn, "v2", None)
    assert len({state.scope_key, other.scope_key, manual.scope_key, whole.scope_key}) == 4
    assert len(reports.list_assessment_report_finalizations(conn, "p1")) == 3
    packet = reports.build_assessment_report_packet(conn, "p1", generation_token=state.generation_token)
    assert [row["id"] for row in packet.vulnerabilities] == ["v1"]
    assert [row["id"] for row in packet.observations] == ["o1"]
    assert packet.task_runs == [] and packet.intents == []
    conn.execute("UPDATE vulnerabilities SET title = 'changed' WHERE id = 'v1'")
    assert reports.build_assessment_report_packet(conn, "p1", generation_token=state.generation_token) == packet
    # 不同 scope 的 claim 不得封口或覆盖彼此的 TaskRun。
    other_claimed = claim(other)
    # 模拟项目暂停代次变化；单条收尾仍只受自己的 generation fencing。
    claimed = claim(state)
    conn.execute("UPDATE projects SET status = 'paused', execution_generation = execution_generation + 1 WHERE id = 'p1'")
    accepted_id = accept(conn, root, claimed)
    first = read_accepted_vulnerability_report(conn, "p1", "v1", "o1")
    assert first["artifact_id"] == accepted_id and first["stale"] is True
    assert "整理成功的说明" in first["readable"]["description_markdown"]
    assert "data" not in first and "report_packet" not in first
    assert reports.get_assessment_report_finalization(conn, "p1").generation_token == whole.generation_token
    assert reports.get_assessment_report_finalization(conn, "p1", generation_token=other.generation_token).status == "running"
    assert conn.execute("SELECT status FROM task_runs WHERE id = ?", (other_claimed.task_run_id,)).fetchone()["status"] == "running"
    failure = finish_body(other_claimed, status="failed", task_run_result={
        "status": "failed", "error_type": "artifact_error", "error_message": "invalid_report",
    })
    reports.finish_assessment_report_finalization("p1", failure, principal=PRINCIPAL)
    assert read_accepted_vulnerability_report(conn, "p1", "v1", "o1") == first
    cancelled = reports.cancel_assessment_report_generation(conn, "p1", vulnerability_id="v2")
    assert cancelled.status == "cancelled"
    with pytest.raises(HTTPException, match="not_claimable:cancelled"):
        claim(manual)
    assert read_accepted_vulnerability_report(conn, "p1", "v1", "o1") == first
    assert conn.execute("SELECT status FROM projects WHERE id='p1'").fetchone()["status"] == "paused"


@pytest.mark.parametrize("previous_status", ["running", "failed"])
def test_running_cancel_waits_for_finish_and_physical_proof_on_recovery(historical_scope_conn, previous_status):
    conn, _ = historical_scope_conn
    state = historical_state(conn)
    claimed = claim(state)
    cancelled = reports.cancel_assessment_report_generation(conn, "p1", vulnerability_id="v1", observation_id="o1")
    assert cancelled.status == "running" and cancelled.cancel_requested_at
    conn.execute("UPDATE assessment_report_finalizations SET lease_expires_at = '2000-01-01T00:00:00Z' WHERE generation_token = ?", (state.generation_token,))
    with pytest.raises(HTTPException, match="execution_unresolved"):
        claim(state)
    run = conn.execute("SELECT * FROM task_runs WHERE id = ?", (claimed.task_run_id,)).fetchone()
    context = json.loads(run["runtime_context_json"])
    context["execution_lifecycle"].update(termination_confirmed=True, cleanup_confirmed=True)
    conn.execute("UPDATE task_runs SET runtime_context_json = ? WHERE id = ?", (json.dumps(context), claimed.task_run_id))
    if previous_status == "failed":
        conn.execute("UPDATE task_runs SET status = 'failed', error_type = 'container_failed', error_message = '' WHERE id = ?", (claimed.task_run_id,))
    recovered = claim(state)
    assert recovered.finish_disposition == "recovered" and recovered.status == "failed"
    final = reports.get_assessment_report_finalization(conn, "p1", generation_token=state.generation_token)
    assert final.status == "cancelled" and final.attempt_count == 1
    assert conn.execute("SELECT count(*) AS n FROM task_runs").fetchone()["n"] == 1
    running = claim(historical_state(conn, observation_id="o2"))
    reports.cancel_assessment_report_generation(conn, "p1", vulnerability_id="v1", observation_id="o2")
    receipt = reports.finish_assessment_report_finalization("p1", finish_body(
        running, status="failed", task_run_result={
            "status": "cancelled", "error_type": "cancelled", "error_message": "report_cancelled",
        },
    ), principal=PRINCIPAL)
    assert receipt.status == "failed"
    assert reports.get_assessment_report_finalization(conn, "p1", generation_token=running.generation_token).status == "cancelled"


def test_report_candidate_admission_keeps_256_identities_and_length_outcome(scoped_conn, monkeypatch):
    import psycopg

    from cairn.report_segments import REPORT_MAX_MANIFEST_BYTES, REPORT_MAX_SEGMENTS
    from cairn.server import db, migrations
    from cairn.server.schemas.candidates import (
        CANDIDATE_MAX_MANIFEST_BYTES, CandidateFrameManifest, CandidateOutcomeManifest,
        canonical_json_bytes,
    )
    from cairn.server.services import candidate_store
    from test_candidate_intake import (
        _BINDING_SHA256, _CAPTURE_ID, _SCHEMA_SHA256, _SESSION, _candidate_manifest,
        _outcome_manifest, _report_artifacts,
    )

    conn, _ = scoped_conn
    conn.execute("UPDATE projects SET status = 'stopped' WHERE id = 'p1'")
    requested = reports.request_assessment_report_generation(
        conn, "p1", template_key="vulnerability-report", requested_by="synthetic-user",
    )
    claimed = claim(requested)
    run = conn.execute("SELECT * FROM task_runs WHERE id = ?", (claimed.task_run_id,)).fetchone()
    context = json.loads(run["runtime_context_json"])
    context["execution_delivery"] = {
        "session_id": _SESSION, "phase": "report_execute", "deadline_at": "2099-01-01T00:00:00Z",
        "container_id": "synthetic-container", "model_binding_sha256": _BINDING_SHA256,
        "result_schema_sha256": _SCHEMA_SHA256, "binding": None, "inputs": {},
    }
    conn.execute("UPDATE task_runs SET session_id = ?, runtime_context_json = ? WHERE id = ?",
                 (_SESSION, json.dumps(context), claimed.task_run_id))
    monkeypatch.setattr(candidate_store, "get_conn", reports.get_conn)
    raw = b'{"accepted":true}'
    previous = CandidateFrameManifest.model_validate(_candidate_manifest(
        raw, phase="report_execute", project_id="p1", task_run_id=claimed.task_run_id,
    ))
    previous_receipt = candidate_store.capture_candidate(
        "p1", claimed.task_run_id, previous, canonical_json_bytes(previous.model_dump()), raw,
        principal=PRINCIPAL,
    )
    before = dict(conn.execute("SELECT * FROM task_run_candidates WHERE id = ?",
                               (previous_receipt.candidate_id,)).fetchone())
    previous_checksums = conn.execute("SELECT version, checksum FROM schema_migrations ORDER BY version").fetchall()
    assert previous_checksums[-1]["version"] == 63
    assert migrations.POSTGRES_MIGRATIONS[-1].version == 64
    for _ in range(2):
        db.run_migrations(conn, "postgresql")
    assert dict(conn.execute("SELECT * FROM task_run_candidates WHERE id = ?",
                             (previous_receipt.candidate_id,)).fetchone()) == before
    assert conn.execute("SELECT version, checksum FROM schema_migrations WHERE version < 64 ORDER BY version").fetchall() == previous_checksums
    manifest = _candidate_manifest(
        raw, phase="report_execute", project_id="p1", task_run_id=claimed.task_run_id,
        assistant_turn=2, producer_candidate_key=f"{_CAPTURE_ID}:2:0",
        parent_producer_candidate_key=previous.producer_candidate_key,
        artifacts=_report_artifacts(REPORT_MAX_SEGMENTS),
    )
    manifest_bytes = canonical_json_bytes(manifest)
    assert len(manifest_bytes) > CANDIDATE_MAX_MANIFEST_BYTES
    parsed = CandidateFrameManifest.model_validate(manifest)
    receipt = candidate_store.capture_candidate(
        "p1", claimed.task_run_id, parsed, manifest_bytes, raw, principal=PRINCIPAL,
    )
    assert receipt.disposition == "received"
    assert candidate_store.capture_candidate(
        "p1", claimed.task_run_id, parsed, manifest_bytes, raw, principal=PRINCIPAL,
    ).disposition == "replayed"
    stored = conn.execute("SELECT * FROM task_run_candidates WHERE id = ?", (receipt.candidate_id,)).fetchone()
    assert stored["artifact_count"] == REPORT_MAX_SEGMENTS
    assert stored["manifest_json"].encode() == manifest_bytes and bytes(stored["raw_bytes"]) == raw
    outcome = CandidateOutcomeManifest.model_validate(_outcome_manifest(
        producer_candidate_key=parsed.producer_candidate_key,
        code="model_output_limit", path=None, keyword=None, count=None,
    ))
    outcome_receipt = candidate_store.capture_candidate_outcome(
        "p1", claimed.task_run_id, outcome, canonical_json_bytes(outcome.model_dump()), b"",
        principal=PRINCIPAL,
    )
    assert outcome_receipt.record_kind == "outcome" and outcome_receipt.disposition == "received"
    assert conn.execute("SELECT code FROM task_run_candidate_outcomes WHERE candidate_id = ?",
                        (receipt.candidate_id,)).fetchone()["code"] == "model_output_limit"
    for phase, count, size, constraint in (
        ("explore_execute", 33, 2, "artifact_count"),
        ("report_execute", 257, 2, "artifact_count"),
        ("explore_execute", 32, CANDIDATE_MAX_MANIFEST_BYTES + 1, "manifest_json"),
        ("report_execute", 256, REPORT_MAX_MANIFEST_BYTES + 1, "manifest_json"),
    ):
        values = dict(stored)
        values.update(id=uuid4().hex, producer_candidate_key=uuid4().hex,
                      phase=phase, artifact_count=count, manifest_json=" " * size)
        conn.execute("SAVEPOINT candidate_capacity_bound")
        with pytest.raises(psycopg.errors.CheckViolation, match=constraint):
            conn.execute(
                f"INSERT INTO task_run_candidates ({', '.join(values)}) "
                f"VALUES ({', '.join('?' for _ in values)})", tuple(values.values()),
            )
        conn.execute("ROLLBACK TO SAVEPOINT candidate_capacity_bound")
        conn.execute("RELEASE SAVEPOINT candidate_capacity_bound")


def test_report_candidate_capacity_fresh_postgres_upgrade_runner(defaults_v60_conn):
    from cairn.server import db

    conn = defaults_v60_conn
    db.run_migrations(conn, "postgresql")
    versions = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    assert versions[-1]["version"] == 64
    constraints = {
        row["conname"]: row["definition"]
        for row in conn.execute(
            "SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint "
            "WHERE conrelid = 'task_run_candidates'::regclass AND contype = 'c'",
        ).fetchall()
    }
    assert "report_execute" in constraints["task_run_candidates_artifact_count_check"]
    assert "256" in constraints["task_run_candidates_artifact_count_check"]
    assert "32" in constraints["task_run_candidates_artifact_count_check"]
    assert "131072" in constraints["task_run_candidates_manifest_json_check"]
    assert "16384" in constraints["task_run_candidates_manifest_json_check"]
    db.run_migrations(conn, "postgresql")
    assert conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == versions


@pytest.mark.parametrize("error_type,retryable", [
    ("model_output_limit", False), ("artifact_error", False), ("provider_error", True),
])
def test_report_finish_retry_plan_and_error_code_follow_real_task_result(scoped_conn, error_type, retryable):
    conn, _ = scoped_conn
    conn.execute("UPDATE projects SET status = 'stopped' WHERE id = 'p1'")
    requested = reports.request_assessment_report_generation(
        conn, "p1", template_key="vulnerability-report", requested_by="synthetic-user",
    )
    claimed = claim(requested)
    body = finish_body(claimed, status="failed", task_run_result={
        "status": "failed", "error_type": error_type, "error_message": "provider_error",
    })
    receipt = reports.finish_assessment_report_finalization("p1", body, principal=PRINCIPAL)
    final = reports.get_assessment_report_finalization(conn, "p1")
    assert receipt.last_error_code == final.last_error_code == error_type
    assert final.status == "failed" and final.last_error == "provider_error"
    assert bool(final.next_attempt_at) is retryable
    if retryable:
        assert final.next_attempt_at > final.updated_at
        with pytest.raises(HTTPException, match="execution_retry_cooldown"):
            claim(final)
        assert reports.get_assessment_report_finalization(conn, "p1").next_attempt_at == final.next_attempt_at
    else:
        assert final.next_attempt_at is None
        # 历史误写的计划在真实准入拒绝后清除，但失败正文与代码不能被替换。
        conn.execute("UPDATE assessment_report_finalizations SET next_attempt_at = '2099-01-01T00:00:00Z' WHERE project_id = 'p1'")
        with pytest.raises(HTTPException, match="execution_retry_blocked"):
            claim(final)
        stopped = reports.get_assessment_report_finalization(conn, "p1")
        assert stopped.next_attempt_at is None and stopped.last_error_code == error_type
        assert stopped.last_error == "provider_error"
    assert reports.finish_assessment_report_finalization("p1", body, principal=PRINCIPAL).last_error_code == error_type
    assert conn.execute("SELECT count(*) AS n FROM task_runs").fetchone()["n"] == 1


def test_report_retry_exhaustion_clears_plan_and_legacy_text_never_becomes_code(scoped_conn):
    from cairn.server.services.assessment_attempts import DEFAULT_MAX_RUNTIME_RETRIES

    conn, _ = scoped_conn
    conn.execute("UPDATE projects SET status = 'stopped' WHERE id = 'p1'")
    requested = reports.request_assessment_report_generation(
        conn, "p1", template_key="vulnerability-report", requested_by="synthetic-user",
    )
    conn.execute("UPDATE assessment_report_finalizations SET status = 'failed', last_error = 'model_output_limit' WHERE project_id = 'p1'")
    legacy = reports.get_assessment_report_finalization(conn, "p1")
    assert legacy.last_error == "model_output_limit" and legacy.last_error_code is None
    for attempt in range(DEFAULT_MAX_RUNTIME_RETRIES + 1):
        claimed = claim(requested)
        reports.finish_assessment_report_finalization("p1", finish_body(
            claimed, status="failed", task_run_result={
                "status": "failed", "error_type": "provider_error", "error_message": "temporary",
            },
        ), principal=PRINCIPAL)
        state = reports.get_assessment_report_finalization(conn, "p1")
        if attempt < DEFAULT_MAX_RUNTIME_RETRIES:
            assert state.next_attempt_at is not None
            conn.execute("UPDATE assessment_report_finalizations SET next_attempt_at = '2000-01-01T00:00:00Z' WHERE project_id = 'p1'")
    assert state.next_attempt_at is None and state.last_error_code == "provider_error"
    with pytest.raises(HTTPException, match="execution_retry_exhausted"):
        claim(state)


def test_project_report_selected_template_render_and_finish_without_single_reports(scoped_conn):
    conn, root = scoped_conn
    conn.execute("UPDATE projects SET status = 'stopped' WHERE id = 'p1'")
    selected_markdown = "# 自选项目总报告\n\n" + TEMPLATE
    selected = report_templates.create_report_template(
        conn, "selected-project-report", "受控总报告模板", "", selected_markdown,
    )
    before = {
        table: conn.execute(f"SELECT * FROM {table} WHERE project_id = 'p1' ORDER BY id").fetchall()
        for table in ("vulnerabilities", "vulnerability_observations", "facts")
    }
    requested = reports.request_assessment_report_generation(
        conn, "p1", template_key=selected.key, requested_by="synthetic-user",
    )
    claimed = claim(requested)
    assert claimed.template_key == selected.key and claimed.template_markdown == selected_markdown
    assert claimed.scope_key == "" and claimed.generation_token == requested.generation_token
    packet = reports.build_assessment_report_packet(conn, "p1", generation_token=claimed.generation_token)
    assert {item["id"] for item in packet.vulnerabilities} == {"v1", "v2"}
    assert not packet.accepted_readable_findings
    assert conn.execute("SELECT count(*) AS n FROM assessment_report_finalizations WHERE scope_key <> ''").fetchone()["n"] == 0

    output_id = accept(conn, root, claimed, project_data=_data("v1", "v2"))
    ready = reports.get_assessment_report_finalization(conn, "p1")
    assert ready.status == "ready" and ready.artifact_id == output_id
    output = conn.execute("SELECT * FROM artifacts WHERE id = ?", (output_id,)).fetchone()
    rendered = reports._artifact_content_bytes(output, error_code="synthetic_output_invalid").decode("utf-8")
    assert rendered.startswith("# 自选项目总报告\n\n")
    assert "冻结材料的整体解释。" in rendered
    assert "### v1" in rendered and "### v2" in rendered
    assert "修复建议一" in rendered and "修复建议二" in rendered
    metadata = json.loads(output["metadata_json"])
    assert metadata["format"] == "markdown" and metadata["report_template_key"] == selected.key
    assert metadata["report_template_digest_sha256"] == selected.digest_sha256
    task = conn.execute("SELECT * FROM task_runs WHERE id = ?", (claimed.task_run_id,)).fetchone()
    assert task["task_type"] == "report" and task["status"] == "succeeded"
    assert conn.execute("SELECT count(*) AS n FROM task_runs").fetchone()["n"] == 1
    for table, rows in before.items():
        assert conn.execute(f"SELECT * FROM {table} WHERE project_id = 'p1' ORDER BY id").fetchall() == rows


def test_stale_ignores_runtime_template_and_row_timestamps():
    packet = {"vulnerability_id": "v1", "observation_id": None,
              "vulnerabilities": [{"id": "v1", "title": "title", "updated_at": "before"}],
              "artifacts": [{"id": "a1", "project_id": "p1", "sha256": "a" * 64, "size": 2, "metadata": {"run": "before"}}]}
    changed = deepcopy(packet)
    changed.update(project={"status": "paused"}, report_policy={"new": "template"})
    changed["vulnerabilities"][0]["updated_at"] = "after"
    changed["artifacts"][0]["metadata"] = {"run": "after"}
    assert reports._scope_input_sha256(changed) == reports._scope_input_sha256(packet)
    changed["vulnerabilities"][0]["title"] = "changed"
    assert reports._scope_input_sha256(changed) != reports._scope_input_sha256(packet)


def test_cancel_poll_reaches_attached_process_without_pausing_other_scope():
    reasons = []
    token = TaskCancellation(generation=1)
    token.attach_process(SimpleNamespace(cancel=reasons.append))
    second = TaskCancellation(generation=1)
    requested = SimpleNamespace(project_id="p1", task_type="report", report_generation_token="g1", report_scope_key="s1", cancellation=token)
    unrelated = SimpleNamespace(project_id="p1", task_type="report", report_generation_token="g2", report_scope_key="s2", cancellation=second)
    def get_state(project_id, *, generation_token):
        assert project_id == "p1"
        return ApiResult(status_code=200, data={"scope_key": generation_token, "cancel_requested_at": "now" if generation_token == "g1" else None})
    scheduler = SimpleNamespace(futures={1: requested, 2: unrelated}, client=SimpleNamespace(get_assessment_report_finalization=get_state))
    CleanupMixin._cancel_inactive_tasks(scheduler, [SimpleNamespace(id="p1", status="paused")])
    assert reasons == ["report_cancelled"] and token.is_cancelled
    assert not second.is_cancelled
    scheduler.client.get_assessment_report_finalization = lambda *args, **kwargs: ApiResult(status_code=0, data=None)
    CleanupMixin._cancel_inactive_tasks(scheduler, [SimpleNamespace(id="p1", status="paused")])
    assert not second.is_cancelled


def test_json_output_journal_replays_same_generation_and_cancel_fallback(tmp_path):
    from cairn.dispatcher.tasks.report_finish_outbox import (
        PendingReportOutput, ReportFinishOutbox, failed_finish_fallback,
        pending_report_output, ready_finish_for_output,
    )
    from test_report_finish_outbox import _execution, _output

    execution = _execution()
    metadata = _output(execution).metadata.model_dump(mode="json")
    metadata.update(format="json", packet_version=2, output_contract_version=4, report_data_schema_version=4)
    metadata.pop("report_template_version")
    content = '{"schema_version":4,"findings":[]}'
    output = pending_report_output(execution, content=content, metadata=metadata)
    assert output.filename == "vulnerability-readable-report.json"
    outbox = ReportFinishOutbox(tmp_path / "journal")
    outbox.persist(output)
    restarted = ReportFinishOutbox(outbox.root)
    assert restarted.has_generation(execution.project_id, execution.generation_token)
    assert not restarted.has_generation(execution.project_id, "another-scope-generation")
    loaded = restarted.entries()[0]
    calls = []
    loaded.replay(SimpleNamespace(create_artifact=lambda project_id, **kwargs: calls.append((project_id, kwargs))))
    assert calls[0][1]["filename"] == output.filename and calls[0][1]["content"] == content
    assert calls[0][1]["metadata"] == metadata
    altered = output.model_dump(mode="json")
    altered["filename"] = "vulnerability-report.md"
    with pytest.raises(ValueError, match="filename does not match"):
        PendingReportOutput.model_validate(altered)
    ready = ready_finish_for_output(output, artifact_id="accepted-artifact")
    fallback = failed_finish_fallback(ready, safe_code="assessment_report_cancellation_pending")
    assert fallback.generation_token == execution.generation_token
    assert fallback.status == "failed" and fallback.task_run_result.status == "cancelled"


def test_report_dispatch_waits_for_same_container_cleanup():
    from cairn.dispatcher.scheduler.dispatch_cycle import DispatchCycleMixin

    observed = []
    scheduler = SimpleNamespace(
        container_manager=SimpleNamespace(container_name=lambda _: "exact-container"),
        _cleanup_pending={"exact-container"},
        _observe_decision=lambda *args, **kwargs: observed.append((args, kwargs)),
    )
    assert DispatchCycleMixin._try_dispatch_completed_report(scheduler, "p1", report_state={"scope_key": "s1"}) is False
    assert observed == [(("p1", "cleanup_pending"), {"task_type": "report"})]
