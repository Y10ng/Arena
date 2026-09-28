from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from threading import Barrier

import pytest
from fastapi import HTTPException

from cairn.server import db
from cairn.server.auth import RequestPrincipal
from cairn.server.models import (
    AssessmentReportFinishRequest,
    AssessmentReportTaskRunResult,
)
from cairn.server.schemas.projects import (
    AssessmentReportFinishReplayRequest,
    UpdateProjectStatusRequest,
)
from cairn.server.services import assessment_reports
from cairn.server.services.assessment_reports import (
    bind_assessment_report_input_snapshot,
    build_assessment_report_packet,
    claim_assessment_report_finalization,
    finish_assessment_report_finalization,
    get_assessment_report_finalization,
    replay_assessment_report_finalization,
    request_assessment_report_generation,
)
from cairn.server.services.project_records import update_project_status_record


TEST_DISPATCHER = RequestPrincipal(kind="machine", subject_id="synthetic-dispatcher")


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    db.run_migrations(conn, "sqlite")
    return conn


def seed_project(conn: sqlite3.Connection, *, status: str = "completed") -> None:
    snapshot = {
        "key": "default-copy-v2",
        "prompt_group": "goal-driven-v1",
        "profile_id": "default-copy-v2",
        "features": {
            "execution_contract_version": 1,
            "execution_engine": "pi",
            "assessment_contract_version": 2,
            "assessment_protocol": "goal-driven-v1",
            "outcome_contract_version": 2,
            "verifier_contract_version": 1,
            "report_policy_version": "1",
            "report_prompt_group": "goal-driven-v1",
        },
    }
    conn.execute(
        """
        INSERT INTO projects (
            id, title, status, created_at, scenario_key, assessment_protocol,
            scenario_snapshot_json, coverage_stop_reason
        ) VALUES ('p1', 'Report', ?, ?, 'default-copy-v2',
                  'goal-driven-v1', ?, 'saturated')
        """,
        (status, "2026-08-17T00:00:00Z", json.dumps(snapshot)),
    )


def seed_vulnerability(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO vulnerabilities (
            id, project_id, title, severity, confidence, status,
            identity_json, created_at, updated_at
        ) VALUES ('v1', 'p1', 'Paused finding', 'high', 0.9, 'confirmed',
                  '{}', '2026-08-17T00:01:00Z', '2026-08-17T00:01:00Z')
        """
    )


def _configure_report_artifact_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    root = tmp_path / "artifacts"
    monkeypatch.setattr(
        "cairn.server.services.assessment_reports.artifact_root",
        lambda: root,
    )
    return root


def _finalization_row(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM assessment_report_finalizations WHERE project_id = 'p1'"
    ).fetchone()
    assert row is not None
    return row


def _confirm_report_execution_resolved(
    conn: sqlite3.Connection,
    task_run_id: str,
) -> None:
    row = conn.execute(
        "SELECT execution_generation, runtime_context_json FROM task_runs WHERE id = ?",
        (task_run_id,),
    ).fetchone()
    assert row is not None
    context = json.loads(row["runtime_context_json"] or "{}")
    context["execution_lifecycle"] = {
        "generation": row["execution_generation"],
        "termination_confirmed": True,
        "cleanup_confirmed": True,
    }
    conn.execute(
        "UPDATE task_runs SET runtime_context_json = ? WHERE id = ?",
        (json.dumps(context, sort_keys=True), task_run_id),
    )
    conn.commit()


def _write_report_input_snapshot(
    conn: sqlite3.Connection,
    artifact_root: Path,
    claim,
    *,
    artifact_id: str = "snapshot-1",
) -> tuple[str, str]:
    finalization = _finalization_row(conn)
    plan = json.loads(finalization["report_execution_plan_json"])
    content = json.dumps(
        {
            "version": 1,
            "graph_yaml": "project: p1\n",
            "report_packet": {
                "version": plan["packet_version"],
                "project": {"id": "p1"},
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = sha256(content).hexdigest()
    filename = f"{artifact_id}-report-input-snapshot.json"
    path = artifact_root / "projects" / "p1" / "runs" / claim.task_run_id / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    metadata = {
        "source": "report_task",
        "format": "json",
        "report_generation_token": finalization["generation_token"],
        "report_template_key": finalization["template_key"],
        "report_template_version": finalization["template_version"],
        "report_template_digest_sha256": finalization["template_digest_sha256"],
        "report_execution_plan_sha256": finalization[
            "report_execution_plan_sha256"
        ],
        "report_prompt_digest_sha256": finalization["report_prompt_digest_sha256"],
        "report_policy_version": finalization["report_policy_version"],
        "packet_version": plan["packet_version"],
        "snapshot_sha256": digest,
        "runtime_context": {},
    }
    conn.execute(
        """
        INSERT INTO artifacts (
            id, project_id, task_run_id, kind, category, uri,
            sha256, size, created_at, metadata_json
        ) VALUES (?, 'p1', ?, 'report', 'deliverable', ?, ?, ?, ?, ?)
        """,
        (
            artifact_id,
            claim.task_run_id,
            f"artifact://projects/p1/runs/{claim.task_run_id}/{filename}",
            digest,
            len(content),
            claim.updated_at,
            json.dumps(metadata),
        ),
    )
    conn.commit()
    return artifact_id, digest


def _write_report_artifact(
    conn: sqlite3.Connection,
    artifact_root: Path,
    claim,
    *,
    snapshot_artifact_id: str,
    snapshot_sha256: str,
    artifact_id: str = "report-1",
    generation_token: str | None = None,
) -> tuple[str, str]:
    finalization = _finalization_row(conn)
    plan = json.loads(finalization["report_execution_plan_json"])
    content = b"# Vulnerability report\n\nVerified report content.\n"
    digest = sha256(content).hexdigest()
    filename = f"{artifact_id}-vulnerability-report.md"
    path = artifact_root / "projects" / "p1" / "runs" / claim.task_run_id / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    metadata = {
        "source": "report_task",
        "format": "markdown",
        "report_generation_token": generation_token or finalization["generation_token"],
        "report_template_key": finalization["template_key"],
        "report_template_version": finalization["template_version"],
        "report_template_digest_sha256": finalization["template_digest_sha256"],
        "report_policy_version": finalization["report_policy_version"],
        "report_execution_plan_sha256": finalization[
            "report_execution_plan_sha256"
        ],
        "report_prompt_digest_sha256": finalization["report_prompt_digest_sha256"],
        "input_snapshot_artifact_id": snapshot_artifact_id,
        "input_snapshot_sha256": snapshot_sha256,
        "completeness_rule_version": plan["completeness_rule_version"],
        "runtime_context": {},
    }
    conn.execute(
        """
        INSERT INTO artifacts (
            id, project_id, task_run_id, kind, category, uri,
            sha256, size, created_at, metadata_json
        ) VALUES (?, 'p1', ?, 'report', 'deliverable', ?, ?, ?, ?, ?)
        """,
        (
            artifact_id,
            claim.task_run_id,
            f"artifact://projects/p1/runs/{claim.task_run_id}/{filename}",
            digest,
            len(content),
            claim.updated_at,
            json.dumps(metadata),
        ),
    )
    conn.commit()
    return artifact_id, digest


def _insert_report_chain_run(
    conn: sqlite3.Connection,
    claim,
    run_id: str,
    *,
    runtime_context: dict,
    project_id: str = "p1",
    task_type: str = "report",
    worker_name: str = "worker-a",
    execution_generation: int | None = None,
) -> None:
    if execution_generation is None:
        current = conn.execute(
            "SELECT execution_generation FROM task_runs WHERE project_id = 'p1' AND id = ?",
            (claim.task_run_id,),
        ).fetchone()
        execution_generation = current["execution_generation"]
    conn.execute(
        """INSERT INTO task_runs (
               id, project_id, task_type, worker_name, worker_type, status,
               prompt_group, started_at, ended_at, error_type,
               runtime_context_json, execution_generation
           ) VALUES (?, ?, ?, ?, 'pi', 'failed', 'default', ?, ?,
                     'submission_missing', ?, ?)""",
        (
            run_id,
            project_id,
            task_type,
            worker_name,
            claim.updated_at,
            claim.updated_at,
            json.dumps(runtime_context),
            execution_generation,
        ),
    )
    conn.commit()


def _failed_finish_request(
    claim,
    *,
    worker: str = "worker-a",
    claim_token: str | None = None,
    error_message: str = "temporary failure",
) -> AssessmentReportFinishRequest:
    return AssessmentReportFinishRequest(
        protocol="cairn-assessment-report-finish-v1",
        worker=worker,
        claim_token=claim_token or claim.claim_token,
        generation_token=claim.generation_token,
        task_run_id=claim.task_run_id,
        status="failed",
        task_run_result=AssessmentReportTaskRunResult(
            status="failed",
            exit_code=1,
            error_type="container_failed",
            error_message=error_message,
        ),
    )


def _ready_finish_request(
    claim,
    *,
    artifact_id: str,
    artifact_sha256: str,
    snapshot_artifact_id: str,
    snapshot_sha256: str,
    worker: str = "worker-a",
    claim_token: str | None = None,
) -> AssessmentReportFinishRequest:
    return AssessmentReportFinishRequest(
        protocol="cairn-assessment-report-finish-v1",
        worker=worker,
        claim_token=claim_token or claim.claim_token,
        generation_token=claim.generation_token,
        task_run_id=claim.task_run_id,
        status="ready",
        artifact_id=artifact_id,
        artifact_sha256=artifact_sha256,
        input_snapshot_artifact_id=snapshot_artifact_id,
        input_snapshot_sha256=snapshot_sha256,
        task_run_result=AssessmentReportTaskRunResult(
            status="succeeded",
            exit_code=0,
        ),
    )


def _replay_finish_request(
    request: AssessmentReportFinishRequest,
    *,
    dispatcher_id: str = TEST_DISPATCHER.subject_id,
    claim_sha256: str | None = None,
    **updates,
) -> AssessmentReportFinishReplayRequest:
    """按生产 canonical identity 构造不含明文 token 的 replay 请求。"""
    values = request.model_dump(mode="json", exclude={"claim_token"})
    values.update(updates)
    claim_digest = claim_sha256 or sha256(request.claim_token.encode("utf-8")).hexdigest()
    provisional = AssessmentReportFinishReplayRequest(
        **values,
        claim_sha256=claim_digest,
        request_sha256="0" * 64,
    )
    material = assessment_reports._finish_request_material(
        "p1",
        dispatcher_id,
        provisional,
        claim_sha256=claim_digest,
    )
    digest = sha256(
        assessment_reports._canonical_json(material).encode("utf-8")
    ).hexdigest()
    return AssessmentReportFinishReplayRequest(
        **values,
        claim_sha256=claim_digest,
        request_sha256=digest,
    )


class _owned_connection_context:
    def __init__(self, path: Path):
        self.path = path
        self.conn: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        self.conn = sqlite3.connect(self.path, timeout=20)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=10000")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self.conn is not None
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()


@pytest.fixture
def concurrent_report_finish_db(monkeypatch, tmp_path: Path):
    setup = make_persisted_conn()
    monkeypatch.setattr(
        assessment_reports,
        "get_conn",
        lambda: _connection_context(setup),
    )
    claim = claim_assessment_report_finalization(
        "p1",
        "worker-a",
        principal=TEST_DISPATCHER,
    )
    setup.commit()

    database_path = tmp_path / "report-finish-race.db"
    persisted = sqlite3.connect(database_path)
    setup.backup(persisted)
    persisted.execute("PRAGMA journal_mode=WAL")
    persisted.close()
    setup.close()
    monkeypatch.setattr(
        assessment_reports,
        "get_conn",
        lambda: _owned_connection_context(database_path),
    )
    return database_path, claim


@pytest.fixture
def report_claim_db(monkeypatch, tmp_path: Path):
    conn = make_persisted_conn()
    conn.commit()
    _configure_report_artifact_root(monkeypatch, tmp_path)
    for module in ("assessment_reports", "audit_task_runs"):
        monkeypatch.setattr(
            f"cairn.server.services.{module}.get_conn",
            lambda: _connection_context(conn),
        )
    yield conn
    conn.close()


def test_claim_atomically_creates_one_pi_task_run_and_rejects_generic_create(report_claim_db):
    from cairn.server.models import CreateTaskRunRequest, UpdateTaskRunRequest
    from cairn.server.services.audit_task_runs import create_task_run_record, update_task_run_record

    conn = report_claim_db
    claim = claim_assessment_report_finalization("p1", "worker-a", principal=TEST_DISPATCHER)
    run = conn.execute("SELECT * FROM task_runs WHERE id = ?", (claim.task_run_id,)).fetchone()
    assert (run["task_type"], run["worker_type"], run["worker_name"], run["status"]) == (
        "report", "pi", "worker-a", "running",
    )
    context = json.loads(run["runtime_context_json"])
    claim_digest = sha256(claim.claim_token.encode("utf-8")).hexdigest()
    assert context["report_claim"]["generation_token"] == claim.generation_token
    assert context["report_claim"]["claim_sha256"] == claim_digest
    assert claim.claim_token not in run["runtime_context_json"]
    stored_claim = conn.execute(
        "SELECT claim_token, claim_sha256 FROM assessment_report_finalizations "
        "WHERE project_id = 'p1'"
    ).fetchone()
    assert stored_claim["claim_token"] is None
    assert stored_claim["claim_sha256"] == claim_digest
    with pytest.raises(HTTPException) as duplicate:
        claim_assessment_report_finalization("p1", "worker-a", principal=TEST_DISPATCHER)
    assert duplicate.value.status_code == 409
    for supplied in ({}, {"report_generation": {
        "claim_token": claim.claim_token, "generation_token": claim.generation_token,
    }}):
        with pytest.raises(HTTPException) as generic:
            create_task_run_record("p1", CreateTaskRunRequest(
                task_type="report", worker_name="worker-a", worker_type="pi", runtime_context=supplied,
            ), principal=TEST_DISPATCHER)
        assert generic.value.detail == "report_claim_required"
    with pytest.raises(HTTPException) as forged:
        update_task_run_record("p1", claim.task_run_id, UpdateTaskRunRequest(runtime_context={"report_claim": {}}), principal=TEST_DISPATCHER)
    assert forged.value.detail == "report_claim_metadata_is_server_owned"
    assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 1


def test_report_task_run_terminal_update_is_receipt_owned(
    report_claim_db,
    monkeypatch,
):
    from cairn.server.models import UpdateTaskRunRequest
    from cairn.server.services.audit_task_runs import update_task_run_record

    claim = claim_assessment_report_finalization(
        "p1",
        "worker-a",
        principal=TEST_DISPATCHER,
    )

    def unexpected_policy(*args):
        pytest.fail("报告收尾不能重新推进 assessment")

    monkeypatch.setattr(
        "cairn.server.services.audit_task_runs.policy_from_project_row",
        unexpected_policy,
    )
    with pytest.raises(HTTPException) as rejected:
        update_task_run_record(
            "p1",
            claim.task_run_id,
            UpdateTaskRunRequest(status="failed"),
            principal=TEST_DISPATCHER,
        )
    assert rejected.value.status_code == 422
    assert (
        rejected.value.detail
        == "assessment_report_terminal_fields_are_receipt_owned"
    )
    assert report_claim_db.execute(
        "SELECT status FROM task_runs WHERE id = ?",
        (claim.task_run_id,),
    ).fetchone()["status"] == "running"

    receipt = finish_assessment_report_finalization(
        "p1",
        _failed_finish_request(claim),
        principal=TEST_DISPATCHER,
    )
    assert receipt.status == "failed"


def test_snapshot_and_finish_require_authenticated_claim_owner(report_claim_db):
    conn = report_claim_db
    claim = claim_assessment_report_finalization(
        "p1",
        "worker-a",
        principal=TEST_DISPATCHER,
    )
    other_dispatcher = RequestPrincipal(
        kind="machine",
        subject_id="other-dispatcher",
    )

    with pytest.raises(HTTPException) as snapshot_owner:
        bind_assessment_report_input_snapshot(
            "p1",
            "worker-a",
            claim_token=claim.claim_token,
            artifact_id="untrusted-snapshot",
            digest_sha256="a" * 64,
            principal=other_dispatcher,
        )
    assert snapshot_owner.value.status_code == 403
    assert snapshot_owner.value.detail == "execution_owner_mismatch"

    with pytest.raises(HTTPException) as finish_owner:
        finish_assessment_report_finalization(
            "p1",
            _failed_finish_request(claim),
            principal=other_dispatcher,
        )
    assert finish_owner.value.status_code == 403
    assert finish_owner.value.detail == "execution_owner_mismatch"
    assert conn.execute(
        "SELECT status FROM task_runs WHERE id = ?",
        (claim.task_run_id,),
    ).fetchone()["status"] == "running"
    assert conn.execute(
        "SELECT COUNT(*) FROM assessment_report_finish_receipts"
    ).fetchone()[0] == 0


def test_claim_rolls_back_if_task_run_insert_fails(report_claim_db, monkeypatch):
    def fail_insert(*args, **kwargs):
        raise RuntimeError("fixture insert failure")

    monkeypatch.setattr("cairn.server.services.audit_task_runs.insert_task_run_record", fail_insert)
    with pytest.raises(RuntimeError, match="fixture insert failure"):
        claim_assessment_report_finalization("p1", "worker-a", principal=TEST_DISPATCHER)
    finalization = get_assessment_report_finalization(report_claim_db, "p1")
    assert finalization.status == "pending" and finalization.attempt_count == 0
    assert report_claim_db.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0


def test_finish_rolls_back_task_run_finalization_and_receipt_together(
    report_claim_db,
    monkeypatch,
):
    conn = report_claim_db
    claim = claim_assessment_report_finalization(
        "p1",
        "worker-a",
        principal=TEST_DISPATCHER,
    )
    original_terminalize = assessment_reports._terminalize_report_task_run

    def fail_after_terminalize(*args, **kwargs):
        original_terminalize(*args, **kwargs)
        raise RuntimeError("fixture receipt insert failure")

    monkeypatch.setattr(
        assessment_reports,
        "_terminalize_report_task_run",
        fail_after_terminalize,
    )
    with pytest.raises(RuntimeError, match="fixture receipt insert failure"):
        finish_assessment_report_finalization(
            "p1",
            _failed_finish_request(claim),
            principal=TEST_DISPATCHER,
        )

    run = conn.execute(
        "SELECT status, ended_at, error_type FROM task_runs WHERE id = ?",
        (claim.task_run_id,),
    ).fetchone()
    assert tuple(run) == ("running", None, None)
    finalization = get_assessment_report_finalization(conn, "p1")
    assert finalization.status == "running"
    assert finalization.claim_token is None
    stored_claim = conn.execute(
        "SELECT claim_token, claim_sha256 FROM assessment_report_finalizations "
        "WHERE project_id = 'p1'"
    ).fetchone()
    assert stored_claim["claim_token"] is None
    assert stored_claim["claim_sha256"] == sha256(
        claim.claim_token.encode("utf-8")
    ).hexdigest()
    assert conn.execute(
        "SELECT COUNT(*) FROM assessment_report_finish_receipts"
    ).fetchone()[0] == 0


def test_finish_receipt_insert_failure_rolls_back_all_terminal_updates(
    report_claim_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = report_claim_db
    claim = claim_assessment_report_finalization(
        "p1",
        "worker-a",
        principal=TEST_DISPATCHER,
    )

    class ReceiptInsertFailureConnection:
        def execute(self, sql: str, parameters=()):
            normalized = " ".join(sql.split()).upper()
            if normalized.startswith(
                "INSERT INTO ASSESSMENT_REPORT_FINISH_RECEIPTS"
            ):
                raise sqlite3.IntegrityError("fixture receipt insert failure")
            return conn.execute(sql, parameters)

        def __getattr__(self, name: str):
            return getattr(conn, name)

    @contextmanager
    def failing_connection():
        try:
            yield ReceiptInsertFailureConnection()
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    monkeypatch.setattr(assessment_reports, "get_conn", failing_connection)

    with pytest.raises(sqlite3.IntegrityError, match="receipt insert failure"):
        finish_assessment_report_finalization(
            "p1",
            _failed_finish_request(claim),
            principal=TEST_DISPATCHER,
        )

    run = conn.execute(
        "SELECT status, ended_at, error_type, error_message "
        "FROM task_runs WHERE id = ?",
        (claim.task_run_id,),
    ).fetchone()
    assert tuple(run) == ("running", None, None, None)
    finalization = conn.execute(
        "SELECT status, claimed_by, claim_token, claim_sha256, lease_expires_at, "
        "next_attempt_at, artifact_id, last_error, completed_at "
        "FROM assessment_report_finalizations WHERE project_id = 'p1'"
    ).fetchone()
    assert finalization["status"] == "running"
    assert finalization["claimed_by"] == "worker-a"
    assert finalization["claim_token"] is None
    assert finalization["claim_sha256"] == sha256(
        claim.claim_token.encode("utf-8")
    ).hexdigest()
    assert finalization["lease_expires_at"] is not None
    assert finalization["next_attempt_at"] is None
    assert finalization["artifact_id"] is None
    assert finalization["last_error"] == ""
    assert finalization["completed_at"] is None
    assert conn.execute(
        "SELECT COUNT(*) FROM assessment_report_finish_receipts"
    ).fetchone()[0] == 0


@pytest.mark.parametrize("status", ["ready", "failed"])
def test_finish_seals_claim_task_run_and_replays_exact_receipt(
    report_claim_db,
    tmp_path: Path,
    status: str,
):
    conn = report_claim_db
    claim = claim_assessment_report_finalization(
        "p1",
        "worker-a",
        principal=TEST_DISPATCHER,
    )
    if status == "ready":
        snapshot_id, snapshot_digest = _write_report_input_snapshot(
            conn,
            tmp_path / "artifacts",
            claim,
        )
        bind_assessment_report_input_snapshot(
            "p1",
            "worker-a",
            claim_token=claim.claim_token,
            artifact_id=snapshot_id,
            digest_sha256=snapshot_digest,
            principal=TEST_DISPATCHER,
        )
        artifact_id, artifact_digest = _write_report_artifact(
            conn,
            tmp_path / "artifacts",
            claim,
            snapshot_artifact_id=snapshot_id,
            snapshot_sha256=snapshot_digest,
        )
        request = _ready_finish_request(
            claim,
            artifact_id=artifact_id,
            artifact_sha256=artifact_digest,
            snapshot_artifact_id=snapshot_id,
            snapshot_sha256=snapshot_digest,
        )
    else:
        request = _failed_finish_request(
            claim,
            error_message="临时失败：保留 Unicode café",
        )

    finished = finish_assessment_report_finalization(
        "p1",
        request,
        principal=TEST_DISPATCHER,
    )
    repeated = finish_assessment_report_finalization(
        "p1",
        request,
        principal=TEST_DISPATCHER,
    )
    assert repeated == finished
    if status == "failed":
        reordered = request.model_dump(mode="json")
        for key in (
            "artifact_id",
            "artifact_sha256",
            "input_snapshot_artifact_id",
            "input_snapshot_sha256",
        ):
            reordered.pop(key)
        task_result = reordered["task_run_result"]
        assert isinstance(task_result, dict)
        task_result.pop("worker_failure")
        reordered = dict(reversed(list(reordered.items())))
        semantic_equivalent = AssessmentReportFinishRequest.model_validate(reordered)
        assert finish_assessment_report_finalization(
            "p1",
            semantic_equivalent,
            principal=TEST_DISPATCHER,
        ) == finished
    assert conn.execute(
        "SELECT COUNT(*) FROM assessment_report_finish_receipts"
    ).fetchone()[0] == 1
    stored_receipt = conn.execute(
        "SELECT request_json FROM assessment_report_finish_receipts"
    ).fetchone()
    assert request.claim_token not in stored_receipt["request_json"]
    assert sha256(request.claim_token.encode("utf-8")).hexdigest() in stored_receipt[
        "request_json"
    ]
    run = conn.execute(
        "SELECT * FROM task_runs WHERE id = ?",
        (claim.task_run_id,),
    ).fetchone()
    assert run["status"] == ("succeeded" if status == "ready" else "failed")
    assert run["ended_at"] is not None
    stored_claim = conn.execute(
        "SELECT claim_token, claim_sha256 FROM assessment_report_finalizations "
        "WHERE project_id = 'p1'"
    ).fetchone()
    assert stored_claim["claim_token"] is None
    assert stored_claim["claim_sha256"] is None

    changed_requests = [
        request.model_copy(update={"claim_token": "wrong-token"}),
        request.model_copy(update={"worker": "worker-b"}),
        request.model_copy(update={"generation_token": "other-generation"}),
    ]
    if status == "ready":
        changed_requests.extend(
            [
                request.model_copy(update={"artifact_id": "other-report"}),
                request.model_copy(update={"artifact_sha256": "1" * 64}),
                request.model_copy(
                    update={"input_snapshot_artifact_id": "other-snapshot"}
                ),
                request.model_copy(update={"input_snapshot_sha256": "2" * 64}),
                AssessmentReportFinishRequest(
                    protocol="cairn-assessment-report-finish-v1",
                    worker=request.worker,
                    claim_token=request.claim_token,
                    generation_token=request.generation_token,
                    task_run_id=request.task_run_id,
                    status="failed",
                    task_run_result=AssessmentReportTaskRunResult(
                        status="failed",
                        exit_code=1,
                        error_type="container_failed",
                        error_message="changed status",
                    ),
                ),
            ]
        )
    else:
        changed_requests.extend(
            [
                request.model_copy(
                    update={
                        "input_snapshot_artifact_id": "other-snapshot",
                        "input_snapshot_sha256": "2" * 64,
                    }
                ),
                request.model_copy(
                    update={
                        "task_run_result": AssessmentReportTaskRunResult(
                            status="failed",
                            exit_code=2,
                            error_type="container_failed",
                            error_message=request.task_run_result.error_message,
                        )
                    }
                ),
                request.model_copy(
                    update={
                        "task_run_result": AssessmentReportTaskRunResult(
                            status="failed",
                            exit_code=1,
                            error_type="artifact_error",
                            error_message=request.task_run_result.error_message,
                        )
                    }
                ),
                request.model_copy(
                    update={
                        "task_run_result": AssessmentReportTaskRunResult(
                            status="failed",
                            exit_code=1,
                            error_type="container_failed",
                            error_message="不同的 Unicode 失败原因",
                        )
                    }
                ),
                request.model_copy(
                    update={
                        "task_run_result": AssessmentReportTaskRunResult(
                            status="failed",
                            exit_code=1,
                            error_type="container_failed",
                            error_message=request.task_run_result.error_message,
                            worker_failure={
                                "origin": "dispatcher_validation",
                                "code": "worker_command_failed",
                                "phase": "report_execute",
                                "error_type": "container_failed",
                                "message": "nested failure",
                                "exit_code": 1,
                            },
                        )
                    }
                ),
                AssessmentReportFinishRequest(
                    protocol="cairn-assessment-report-finish-v1",
                    worker=request.worker,
                    claim_token=request.claim_token,
                    generation_token=request.generation_token,
                    task_run_id=request.task_run_id,
                    status="ready",
                    artifact_id="other-report",
                    artifact_sha256="1" * 64,
                    input_snapshot_artifact_id="other-snapshot",
                    input_snapshot_sha256="2" * 64,
                    task_run_result=AssessmentReportTaskRunResult(
                        status="succeeded",
                        exit_code=0,
                    ),
                ),
            ]
        )
    for changed in changed_requests:
        with pytest.raises(HTTPException) as conflict:
            finish_assessment_report_finalization(
                "p1",
                changed,
                principal=TEST_DISPATCHER,
            )
        assert conflict.value.detail == "assessment_report_finish_receipt_conflict"

    with pytest.raises(HTTPException) as owner_conflict:
        finish_assessment_report_finalization(
            "p1",
            request,
            principal=RequestPrincipal(kind="machine", subject_id="other-dispatcher"),
        )
    assert owner_conflict.value.detail == "assessment_report_finish_receipt_conflict"


@pytest.mark.parametrize("status", ["ready", "failed"])
def test_token_free_finish_replay_matches_normal_finish_digest_and_receipt(
    report_claim_db,
    tmp_path: Path,
    status: str,
):
    conn = report_claim_db
    claim = claim_assessment_report_finalization(
        "p1",
        "worker-a",
        principal=TEST_DISPATCHER,
    )
    if status == "ready":
        snapshot_id, snapshot_digest = _write_report_input_snapshot(
            conn,
            tmp_path / "artifacts",
            claim,
            artifact_id="replay-snapshot",
        )
        bind_assessment_report_input_snapshot(
            "p1",
            "worker-a",
            claim_token=claim.claim_token,
            artifact_id=snapshot_id,
            digest_sha256=snapshot_digest,
            principal=TEST_DISPATCHER,
        )
        artifact_id, artifact_digest = _write_report_artifact(
            conn,
            tmp_path / "artifacts",
            claim,
            snapshot_artifact_id=snapshot_id,
            snapshot_sha256=snapshot_digest,
            artifact_id="replay-report",
        )
        request = _ready_finish_request(
            claim,
            artifact_id=artifact_id,
            artifact_sha256=artifact_digest,
            snapshot_artifact_id=snapshot_id,
            snapshot_sha256=snapshot_digest,
        )
    else:
        request = _failed_finish_request(
            claim,
            error_message="durable failed terminal facts",
        )

    replay = _replay_finish_request(request)
    serialized = replay.model_dump_json()
    assert claim.claim_token not in serialized
    assert replay.claim_sha256 == sha256(claim.claim_token.encode("utf-8")).hexdigest()

    accepted = replay_assessment_report_finalization(
        "p1",
        replay,
        principal=TEST_DISPATCHER,
    )
    repeated = replay_assessment_report_finalization(
        "p1",
        replay,
        principal=TEST_DISPATCHER,
    )
    normal_replay = finish_assessment_report_finalization(
        "p1",
        request,
        principal=TEST_DISPATCHER,
    )

    assert repeated == accepted == normal_replay
    assert accepted.request_sha256 == replay.request_sha256
    assert conn.execute(
        "SELECT COUNT(*) FROM assessment_report_finish_receipts"
    ).fetchone()[0] == 1


def test_token_free_finish_replay_accepts_same_claim_after_lease_expiry(
    report_claim_db,
):
    conn = report_claim_db
    claim = claim_assessment_report_finalization(
        "p1",
        "worker-a",
        principal=TEST_DISPATCHER,
    )
    request = _failed_finish_request(
        claim,
        error_message="original failure survives restart",
    )
    conn.execute(
        "UPDATE assessment_report_finalizations SET lease_expires_at = ? WHERE project_id = 'p1'",
        ("2000-01-01T00:00:00Z",),
    )
    conn.commit()

    receipt = replay_assessment_report_finalization(
        "p1",
        _replay_finish_request(request),
        principal=TEST_DISPATCHER,
    )

    assert receipt.status == "failed"
    run = conn.execute(
        "SELECT status, error_type, error_message FROM task_runs WHERE id = ?",
        (claim.task_run_id,),
    ).fetchone()
    assert tuple(run) == (
        "failed",
        "container_failed",
        "original failure survives restart",
    )


def test_token_free_finish_replay_rejects_request_digest_mismatch(report_claim_db):
    claim = claim_assessment_report_finalization(
        "p1",
        "worker-a",
        principal=TEST_DISPATCHER,
    )
    replay = _replay_finish_request(_failed_finish_request(claim)).model_copy(
        update={"request_sha256": "f" * 64}
    )

    with pytest.raises(HTTPException) as rejected:
        replay_assessment_report_finalization(
            "p1",
            replay,
            principal=TEST_DISPATCHER,
        )
    assert rejected.value.status_code == 409
    assert rejected.value.detail == "assessment_report_finish_request_digest_mismatch"


@pytest.mark.parametrize(
    ("mutation", "expected_status", "expected_detail"),
    [
        ("claim_digest", 409, "assessment_report_claim_mismatch"),
        ("generation", 409, "assessment_report_claim_mismatch"),
        ("attempt", 409, "assessment_report_claim_mismatch"),
        ("superseded", 409, "assessment_report_claim_mismatch"),
        ("project_generation", 409, "execution_generation_mismatch"),
        ("owner", 403, "execution_owner_mismatch"),
    ],
)
def test_token_free_finish_replay_preserves_claim_generation_attempt_and_owner_fences(
    report_claim_db,
    mutation: str,
    expected_status: int,
    expected_detail: str,
):
    conn = report_claim_db
    claim = claim_assessment_report_finalization(
        "p1",
        "worker-a",
        principal=TEST_DISPATCHER,
    )
    request = _failed_finish_request(claim)
    principal = TEST_DISPATCHER
    kwargs = {}
    if mutation == "claim_digest":
        kwargs["claim_sha256"] = "f" * 64
    elif mutation == "generation":
        kwargs["generation_token"] = "other-generation"
    elif mutation == "attempt":
        conn.execute(
            "UPDATE assessment_report_finalizations SET attempt_count = attempt_count + 1 WHERE project_id = 'p1'"
        )
        conn.commit()
    elif mutation == "superseded":
        replacement_digest = sha256(b"replacement-claim").hexdigest()
        conn.execute(
            "UPDATE assessment_report_finalizations SET claim_sha256 = ? "
            "WHERE project_id = 'p1'",
            (replacement_digest,),
        )
        conn.commit()
    elif mutation == "project_generation":
        conn.execute(
            "UPDATE projects SET execution_generation = execution_generation + 1 WHERE id = 'p1'"
        )
        conn.commit()
    elif mutation == "owner":
        principal = RequestPrincipal(kind="machine", subject_id="other-dispatcher")
        kwargs["dispatcher_id"] = principal.subject_id

    replay = _replay_finish_request(request, **kwargs)
    with pytest.raises(HTTPException) as rejected:
        replay_assessment_report_finalization(
            "p1",
            replay,
            principal=principal,
        )
    assert rejected.value.status_code == expected_status
    assert rejected.value.detail == expected_detail


def test_token_free_finish_replay_rejects_snapshot_and_receipt_conflicts(
    report_claim_db,
    tmp_path: Path,
):
    conn = report_claim_db
    claim = claim_assessment_report_finalization(
        "p1",
        "worker-a",
        principal=TEST_DISPATCHER,
    )
    snapshot_id, snapshot_digest = _write_report_input_snapshot(
        conn,
        tmp_path / "artifacts",
        claim,
        artifact_id="bound-snapshot",
    )
    bind_assessment_report_input_snapshot(
        "p1",
        "worker-a",
        claim_token=claim.claim_token,
        artifact_id=snapshot_id,
        digest_sha256=snapshot_digest,
        principal=TEST_DISPATCHER,
    )
    request = _failed_finish_request(claim, error_message="first failure")
    snapshot_conflict = _replay_finish_request(
        request,
        input_snapshot_artifact_id="other-snapshot",
        input_snapshot_sha256="a" * 64,
    )
    with pytest.raises(HTTPException) as rejected_snapshot:
        replay_assessment_report_finalization(
            "p1",
            snapshot_conflict,
            principal=TEST_DISPATCHER,
        )
    assert rejected_snapshot.value.detail == "assessment_report_snapshot_receipt_conflict"

    replay = _replay_finish_request(request)
    accepted = replay_assessment_report_finalization(
        "p1",
        replay,
        principal=TEST_DISPATCHER,
    )
    # failed 请求刻意省略 snapshot；receipt 必须描述被接受的 immutable request，
    # 不能回显 finalization 上此前已经绑定的 snapshot。
    assert accepted.input_snapshot_artifact_id is None
    assert accepted.input_snapshot_sha256 is None

    from cairn.dispatcher.protocol.client import ApiResult
    from cairn.dispatcher.tasks import report
    from cairn.dispatcher.tasks.report_finish_outbox import (
        PendingReportFinish,
        validate_finish_receipt,
    )

    pending = PendingReportFinish(
        project_id="p1",
        authenticated_dispatcher_id=TEST_DISPATCHER.subject_id,
        worker=request.worker,
        claim_sha256=replay.claim_sha256,
        request_sha256=replay.request_sha256,
        generation_token=request.generation_token,
        task_run_id=request.task_run_id,
        status=request.status,
        artifact_id=request.artifact_id,
        artifact_sha256=request.artifact_sha256,
        input_snapshot_artifact_id=request.input_snapshot_artifact_id,
        input_snapshot_sha256=request.input_snapshot_sha256,
        task_run_result=request.task_run_result,
    )
    response = ApiResult(status_code=200, data=accepted.model_dump(mode="json"))
    assert validate_finish_receipt(response, pending).status_code == 200
    assert report._validate_report_finish_receipt(
        response,
        project_id="p1",
        task_run_id=request.task_run_id,
        generation_token=request.generation_token,
        status=request.status,
        artifact_id=None,
        input_snapshot_artifact_id=None,
        input_snapshot_sha256=None,
        request_sha256=replay.request_sha256,
    ).status_code == 200

    changed_result = request.task_run_result.model_copy(
        update={"error_message": "different failure"}
    )
    conflicting = _replay_finish_request(
        request,
        task_run_result=changed_result.model_dump(mode="json"),
    )
    with pytest.raises(HTTPException) as rejected_receipt:
        replay_assessment_report_finalization(
            "p1",
            conflicting,
            principal=TEST_DISPATCHER,
        )
    assert rejected_receipt.value.detail == "assessment_report_finish_receipt_conflict"
    assert accepted.request_sha256 != conflicting.request_sha256


def test_concurrent_identical_finishes_replay_one_receipt(
    concurrent_report_finish_db,
):
    database_path, claim = concurrent_report_finish_db
    request = _failed_finish_request(claim, error_message="same terminal result")
    barrier = Barrier(4)

    def finish_once():
        barrier.wait(timeout=10)
        return finish_assessment_report_finalization(
            "p1",
            request,
            principal=TEST_DISPATCHER,
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(lambda _: finish_once(), range(4)))

    assert all(response == responses[0] for response in responses)
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute(
            "SELECT COUNT(*) FROM assessment_report_finish_receipts"
        ).fetchone()[0] == 1
        run = conn.execute(
            "SELECT status, ended_at FROM task_runs WHERE id = ?",
            (claim.task_run_id,),
        ).fetchone()
        assert run["status"] == "failed"
        assert run["ended_at"] is not None


def test_concurrent_conflicting_finishes_accept_exactly_one_request(
    concurrent_report_finish_db,
):
    database_path, claim = concurrent_report_finish_db
    barrier = Barrier(2)
    requests = {
        label: _failed_finish_request(claim, error_message=label)
        for label in ("terminal result a", "terminal result b")
    }

    def finish_once(item):
        label, request = item
        barrier.wait(timeout=10)
        try:
            response = finish_assessment_report_finalization(
                "p1",
                request,
                principal=TEST_DISPATCHER,
            )
        except HTTPException as exc:
            return label, exc
        return label, response

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(finish_once, requests.items()))

    accepted = [(label, value) for label, value in outcomes if not isinstance(value, HTTPException)]
    rejected = [(label, value) for label, value in outcomes if isinstance(value, HTTPException)]
    assert len(accepted) == 1
    assert len(rejected) == 1
    assert rejected[0][1].status_code == 409
    assert rejected[0][1].detail == "assessment_report_finish_receipt_conflict"

    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        receipt = conn.execute(
            "SELECT request_json FROM assessment_report_finish_receipts"
        ).fetchone()
        assert receipt is not None
        stored_request = json.loads(receipt["request_json"])
        assert stored_request["task_run_result"]["error_message"] == accepted[0][0]
        assert conn.execute(
            "SELECT COUNT(*) FROM assessment_report_finish_receipts"
        ).fetchone()[0] == 1
        finalization = conn.execute(
            "SELECT status, last_error FROM assessment_report_finalizations "
            "WHERE project_id = 'p1'"
        ).fetchone()
        assert finalization["status"] == "failed"
        assert finalization["last_error"] == accepted[0][0]


def test_ready_rejects_current_task_run_artifact_from_wrong_generation(
    report_claim_db,
    tmp_path: Path,
):
    conn = report_claim_db
    claim = claim_assessment_report_finalization(
        "p1",
        "worker-a",
        principal=TEST_DISPATCHER,
    )
    snapshot_id, snapshot_digest = _write_report_input_snapshot(
        conn,
        tmp_path / "artifacts",
        claim,
    )
    bind_assessment_report_input_snapshot(
        "p1",
        "worker-a",
        claim_token=claim.claim_token,
        artifact_id=snapshot_id,
        digest_sha256=snapshot_digest,
        principal=TEST_DISPATCHER,
    )
    artifact_id, artifact_digest = _write_report_artifact(
        conn,
        tmp_path / "artifacts",
        claim,
        snapshot_artifact_id=snapshot_id,
        snapshot_sha256=snapshot_digest,
        artifact_id="report-wrong-generation",
        generation_token="wrong-generation",
    )
    with pytest.raises(HTTPException) as wrong:
        finish_assessment_report_finalization(
            "p1",
            _ready_finish_request(
                claim,
                artifact_id=artifact_id,
                artifact_sha256=artifact_digest,
                snapshot_artifact_id=snapshot_id,
                snapshot_sha256=snapshot_digest,
            ),
            principal=TEST_DISPATCHER,
        )
    assert wrong.value.detail == "assessment_report_artifact_invalid"
    assert get_assessment_report_finalization(conn, "p1").status == "running"
    assert conn.execute(
        "SELECT COUNT(*) FROM assessment_report_finish_receipts"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "mutation",
    [
        "non_object_retry",
        "extra_retry_key",
        "missing_source_id",
        "empty_source_id",
        "noncanonical_source_id",
        "missing_source_row",
        "foreign_project_source",
        "self_cycle",
        "two_node_cycle",
        "wrong_task_type",
        "wrong_worker",
        "wrong_execution_generation",
        "wrong_report_generation",
        "malformed_source_claim",
    ],
)
def test_snapshot_origin_rejects_invalid_delivery_retry_provenance(
    report_claim_db,
    mutation: str,
):
    conn = report_claim_db
    claim = claim_assessment_report_finalization(
        "p1",
        "worker-a",
        principal=TEST_DISPATCHER,
    )
    token = claim.generation_token
    source_id = "report-source"
    source_context: object = {"report_claim": {"generation_token": token}}
    source_project = "p1"
    task_type = "report"
    worker_name = "worker-a"
    execution_generation = None

    if mutation == "foreign_project_source":
        conn.execute(
            """INSERT INTO projects (id, title, status, created_at, scenario_key)
               VALUES ('p2', 'Foreign report', 'completed', ?, 'default')""",
            (claim.updated_at,),
        )
        source_project = "p2"
    elif mutation == "wrong_task_type":
        task_type = "reason"
    elif mutation == "wrong_worker":
        worker_name = "worker-b"
    elif mutation == "wrong_execution_generation":
        execution_generation = 99
    elif mutation == "wrong_report_generation":
        source_context = {"report_claim": {"generation_token": "other-generation"}}
    elif mutation == "malformed_source_claim":
        source_context = {"report_claim": "invalid"}
    elif mutation == "noncanonical_source_id":
        source_id = " report-source "

    _insert_report_chain_run(
        conn,
        claim,
        source_id,
        runtime_context=source_context,
        project_id=source_project,
        task_type=task_type,
        worker_name=worker_name,
        execution_generation=execution_generation,
    )
    current = conn.execute(
        "SELECT runtime_context_json FROM task_runs WHERE project_id = 'p1' AND id = ?",
        (claim.task_run_id,),
    ).fetchone()
    current_context = json.loads(current["runtime_context_json"])
    retry_value: object = {"source_task_run_id": source_id}
    if mutation == "non_object_retry":
        retry_value = source_id
    elif mutation == "extra_retry_key":
        retry_value = {"source_task_run_id": source_id, "unexpected": True}
    elif mutation == "missing_source_id":
        retry_value = {}
    elif mutation == "empty_source_id":
        retry_value = {"source_task_run_id": ""}
    elif mutation == "missing_source_row":
        retry_value = {"source_task_run_id": "missing-source"}
    elif mutation == "self_cycle":
        retry_value = {"source_task_run_id": claim.task_run_id}
    elif mutation == "two_node_cycle":
        source_context = {
            "report_claim": {"generation_token": token},
            "delivery_retry": {"source_task_run_id": claim.task_run_id},
        }
        conn.execute(
            "UPDATE task_runs SET runtime_context_json = ? WHERE project_id = 'p1' AND id = ?",
            (json.dumps(source_context), source_id),
        )
    current_context["delivery_retry"] = retry_value
    conn.execute(
        "UPDATE task_runs SET runtime_context_json = ? WHERE project_id = 'p1' AND id = ?",
        (json.dumps(current_context), claim.task_run_id),
    )
    conn.commit()

    with pytest.raises(HTTPException) as rejected:
        bind_assessment_report_input_snapshot(
            "p1",
            "worker-a",
            claim_token=claim.claim_token,
            artifact_id="untrusted-snapshot",
            digest_sha256="a" * 64,
            principal=TEST_DISPATCHER,
        )
    assert rejected.value.status_code == 409
    assert rejected.value.detail == "assessment_report_snapshot_artifact_invalid"
    finalization = get_assessment_report_finalization(conn, "p1")
    assert finalization.input_snapshot_artifact_id is None
    assert finalization.input_snapshot_sha256 is None


def test_ready_finish_accepts_two_hop_snapshot_origin(
    report_claim_db,
    tmp_path: Path,
):
    conn = report_claim_db
    artifact_root = tmp_path / "artifacts"
    claim = claim_assessment_report_finalization(
        "p1",
        "worker-a",
        principal=TEST_DISPATCHER,
    )
    token = claim.generation_token
    origin_id = "report-origin"
    middle_id = "report-middle"
    _insert_report_chain_run(
        conn,
        claim,
        origin_id,
        runtime_context={"report_claim": {"generation_token": token}},
    )
    _insert_report_chain_run(
        conn,
        claim,
        middle_id,
        runtime_context={
            "report_claim": {"generation_token": token},
            "delivery_retry": {"source_task_run_id": origin_id},
        },
    )
    current = conn.execute(
        "SELECT runtime_context_json FROM task_runs WHERE project_id = 'p1' AND id = ?",
        (claim.task_run_id,),
    ).fetchone()
    current_context = json.loads(current["runtime_context_json"])
    current_context["delivery_retry"] = {"source_task_run_id": middle_id}
    conn.execute(
        "UPDATE task_runs SET runtime_context_json = ? WHERE project_id = 'p1' AND id = ?",
        (json.dumps(current_context), claim.task_run_id),
    )
    conn.commit()

    origin_claim = claim.model_copy(update={"task_run_id": origin_id})
    snapshot_id, snapshot_digest = _write_report_input_snapshot(
        conn,
        artifact_root,
        origin_claim,
    )
    bound = bind_assessment_report_input_snapshot(
        "p1",
        "worker-a",
        claim_token=claim.claim_token,
        artifact_id=snapshot_id,
        digest_sha256=snapshot_digest,
        principal=TEST_DISPATCHER,
    )
    assert bound.input_snapshot_artifact_id == snapshot_id
    artifact_id, artifact_digest = _write_report_artifact(
        conn,
        artifact_root,
        claim,
        snapshot_artifact_id=snapshot_id,
        snapshot_sha256=snapshot_digest,
    )
    finished = finish_assessment_report_finalization(
        "p1",
        _ready_finish_request(
            claim,
            artifact_id=artifact_id,
            artifact_sha256=artifact_digest,
            snapshot_artifact_id=snapshot_id,
            snapshot_sha256=snapshot_digest,
        ),
        principal=TEST_DISPATCHER,
    )

    assert finished.status == "ready"
    assert conn.execute(
        "SELECT task_run_id FROM artifacts WHERE project_id = 'p1' AND id = ?",
        (snapshot_id,),
    ).fetchone()["task_run_id"] == origin_id
    assert conn.execute(
        "SELECT task_run_id FROM artifacts WHERE project_id = 'p1' AND id = ?",
        (artifact_id,),
    ).fetchone()["task_run_id"] == claim.task_run_id


@pytest.mark.parametrize(("link_count", "accepted"), [(15, True), (16, False)])
def test_snapshot_origin_chain_has_explicit_sixteen_node_limit(
    report_claim_db,
    tmp_path: Path,
    link_count: int,
    accepted: bool,
):
    conn = report_claim_db
    claim = claim_assessment_report_finalization(
        "p1",
        "worker-a",
        principal=TEST_DISPATCHER,
    )
    token = claim.generation_token
    run_ids = [f"report-chain-{index}" for index in range(1, link_count + 1)]
    for index, run_id in enumerate(run_ids):
        context = {"report_claim": {"generation_token": token}}
        if index + 1 < len(run_ids):
            context["delivery_retry"] = {
                "source_task_run_id": run_ids[index + 1]
            }
        _insert_report_chain_run(
            conn,
            claim,
            run_id,
            runtime_context=context,
        )
    current = conn.execute(
        "SELECT runtime_context_json FROM task_runs WHERE project_id = 'p1' AND id = ?",
        (claim.task_run_id,),
    ).fetchone()
    current_context = json.loads(current["runtime_context_json"])
    current_context["delivery_retry"] = {"source_task_run_id": run_ids[0]}
    conn.execute(
        "UPDATE task_runs SET runtime_context_json = ? WHERE project_id = 'p1' AND id = ?",
        (json.dumps(current_context), claim.task_run_id),
    )
    conn.commit()

    origin_claim = claim.model_copy(update={"task_run_id": run_ids[-1]})
    snapshot_id, snapshot_digest = _write_report_input_snapshot(
        conn,
        tmp_path / "artifacts",
        origin_claim,
    )
    if accepted:
        bound = bind_assessment_report_input_snapshot(
            "p1",
            "worker-a",
            claim_token=claim.claim_token,
            artifact_id=snapshot_id,
            digest_sha256=snapshot_digest,
            principal=TEST_DISPATCHER,
        )
        assert bound.input_snapshot_artifact_id == snapshot_id
    else:
        with pytest.raises(HTTPException) as rejected:
            bind_assessment_report_input_snapshot(
                "p1",
                "worker-a",
                claim_token=claim.claim_token,
                artifact_id=snapshot_id,
                digest_sha256=snapshot_digest,
                principal=TEST_DISPATCHER,
            )
        assert rejected.value.status_code == 409
        assert rejected.value.detail == "assessment_report_snapshot_artifact_invalid"


@pytest.mark.parametrize(
    "mutation",
    [
        "snapshot_bytes",
        "snapshot_size",
        "snapshot_symlink",
        "snapshot_non_regular",
        "snapshot_invalid_utf8",
        "snapshot_extra_metadata",
        "snapshot_uri",
        "snapshot_kind",
        "snapshot_category",
        "snapshot_task_run",
        "report_bytes",
        "report_size",
        "report_symlink",
        "report_non_regular",
        "report_invalid_utf8",
        "report_blank",
        "report_extra_metadata",
        "report_uri",
        "report_kind",
        "report_category",
        "report_task_run",
    ],
)
def test_finish_rejects_artifact_byte_or_metadata_tampering(
    report_claim_db,
    tmp_path: Path,
    mutation: str,
):
    conn = report_claim_db
    artifact_root = tmp_path / "artifacts"
    claim = claim_assessment_report_finalization(
        "p1",
        "worker-a",
        principal=TEST_DISPATCHER,
    )
    snapshot_id, snapshot_digest = _write_report_input_snapshot(
        conn,
        artifact_root,
        claim,
    )
    snapshot_path = (
        artifact_root
        / "projects"
        / "p1"
        / "runs"
        / claim.task_run_id
        / f"{snapshot_id}-report-input-snapshot.json"
    )
    if mutation == "snapshot_bytes":
        content = snapshot_path.read_bytes()
        snapshot_path.write_bytes(b"X" + content[1:])
    elif mutation == "snapshot_size":
        conn.execute(
            "UPDATE artifacts SET size = size + 1 WHERE id = ?",
            (snapshot_id,),
        )
        conn.commit()
    elif mutation == "snapshot_symlink":
        target = snapshot_path.with_name("snapshot-symlink-target.json")
        target.write_bytes(snapshot_path.read_bytes())
        snapshot_path.unlink()
        snapshot_path.symlink_to(target)
    elif mutation == "snapshot_non_regular":
        snapshot_path.unlink()
        snapshot_path.mkdir()
    elif mutation == "snapshot_invalid_utf8":
        invalid_content = b"\xff"
        snapshot_digest = sha256(invalid_content).hexdigest()
        snapshot_path.write_bytes(invalid_content)
        row = conn.execute(
            "SELECT metadata_json FROM artifacts WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
        metadata = json.loads(row["metadata_json"])
        metadata["snapshot_sha256"] = snapshot_digest
        conn.execute(
            """UPDATE artifacts
               SET sha256 = ?, size = ?, metadata_json = ?
               WHERE id = ?""",
            (
                snapshot_digest,
                len(invalid_content),
                json.dumps(metadata),
                snapshot_id,
            ),
        )
        conn.commit()
    elif mutation == "snapshot_extra_metadata":
        row = conn.execute(
            "SELECT metadata_json FROM artifacts WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
        metadata = json.loads(row["metadata_json"])
        metadata["unexpected"] = True
        conn.execute(
            "UPDATE artifacts SET metadata_json = ? WHERE id = ?",
            (json.dumps(metadata), snapshot_id),
        )
        conn.commit()
    elif mutation in {
        "snapshot_uri",
        "snapshot_kind",
        "snapshot_category",
        "snapshot_task_run",
    }:
        column = mutation.removeprefix("snapshot_")
        value = {
            "uri": "artifact://tampered",
            "kind": "file",
            "category": "input",
            "task_run": None,
        }[column]
        column = "task_run_id" if column == "task_run" else column
        conn.execute(
            f"UPDATE artifacts SET {column} = ? WHERE id = ?",
            (value, snapshot_id),
        )
        conn.commit()

    if mutation.startswith("snapshot_"):
        with pytest.raises(HTTPException) as rejected:
            bind_assessment_report_input_snapshot(
                "p1",
                "worker-a",
                claim_token=claim.claim_token,
                artifact_id=snapshot_id,
                digest_sha256=snapshot_digest,
                principal=TEST_DISPATCHER,
            )
        assert rejected.value.detail == "assessment_report_snapshot_artifact_invalid"
    else:
        bind_assessment_report_input_snapshot(
            "p1",
            "worker-a",
            claim_token=claim.claim_token,
            artifact_id=snapshot_id,
            digest_sha256=snapshot_digest,
            principal=TEST_DISPATCHER,
        )
        artifact_id, artifact_digest = _write_report_artifact(
            conn,
            artifact_root,
            claim,
            snapshot_artifact_id=snapshot_id,
            snapshot_sha256=snapshot_digest,
        )
        report_path = (
            artifact_root
            / "projects"
            / "p1"
            / "runs"
            / claim.task_run_id
            / f"{artifact_id}-vulnerability-report.md"
        )
        if mutation == "report_bytes":
            content = report_path.read_bytes()
            report_path.write_bytes(b"X" + content[1:])
        elif mutation == "report_size":
            conn.execute(
                "UPDATE artifacts SET size = size + 1 WHERE id = ?",
                (artifact_id,),
            )
            conn.commit()
        elif mutation == "report_symlink":
            target = report_path.with_name("report-symlink-target.md")
            target.write_bytes(report_path.read_bytes())
            report_path.unlink()
            report_path.symlink_to(target)
        elif mutation == "report_non_regular":
            report_path.unlink()
            report_path.mkdir()
        elif mutation in {"report_invalid_utf8", "report_blank"}:
            replacement = b"\xff" if mutation == "report_invalid_utf8" else b" \n"
            artifact_digest = sha256(replacement).hexdigest()
            report_path.write_bytes(replacement)
            conn.execute(
                "UPDATE artifacts SET sha256 = ?, size = ? WHERE id = ?",
                (artifact_digest, len(replacement), artifact_id),
            )
            conn.commit()
        elif mutation == "report_extra_metadata":
            row = conn.execute(
                "SELECT metadata_json FROM artifacts WHERE id = ?",
                (artifact_id,),
            ).fetchone()
            metadata = json.loads(row["metadata_json"])
            metadata["unexpected"] = True
            conn.execute(
                "UPDATE artifacts SET metadata_json = ? WHERE id = ?",
                (json.dumps(metadata), artifact_id),
            )
            conn.commit()
        else:
            column = mutation.removeprefix("report_")
            value = {
                "uri": "artifact://tampered",
                "kind": "file",
                "category": "input",
                "task_run": None,
            }[column]
            column = "task_run_id" if column == "task_run" else column
            conn.execute(
                f"UPDATE artifacts SET {column} = ? WHERE id = ?",
                (value, artifact_id),
            )
            conn.commit()

        with pytest.raises(HTTPException) as rejected:
            finish_assessment_report_finalization(
                "p1",
                _ready_finish_request(
                    claim,
                    artifact_id=artifact_id,
                    artifact_sha256=artifact_digest,
                    snapshot_artifact_id=snapshot_id,
                    snapshot_sha256=snapshot_digest,
                ),
                principal=TEST_DISPATCHER,
            )
        assert rejected.value.detail == "assessment_report_artifact_invalid"

    assert get_assessment_report_finalization(conn, "p1").status == "running"
    assert conn.execute(
        "SELECT COUNT(*) FROM assessment_report_finish_receipts"
    ).fetchone()[0] == 0


def test_expired_claim_recovers_valid_durable_report_without_new_task_run(
    report_claim_db,
    monkeypatch,
    tmp_path: Path,
):
    conn = report_claim_db
    clock = {"now": "2026-08-17T00:01:00Z"}
    monkeypatch.setattr(
        "cairn.server.services.assessment_reports.utcnow",
        lambda: clock["now"],
    )
    first = claim_assessment_report_finalization(
        "p1",
        "worker-a",
        lease_seconds=60,
        principal=TEST_DISPATCHER,
    )
    snapshot_id, snapshot_digest = _write_report_input_snapshot(
        conn,
        tmp_path / "artifacts",
        first,
    )
    bind_assessment_report_input_snapshot(
        "p1",
        "worker-a",
        claim_token=first.claim_token,
        artifact_id=snapshot_id,
        digest_sha256=snapshot_digest,
        principal=TEST_DISPATCHER,
    )
    artifact_id, artifact_digest = _write_report_artifact(
        conn,
        tmp_path / "artifacts",
        first,
        snapshot_artifact_id=snapshot_id,
        snapshot_sha256=snapshot_digest,
        artifact_id="report-before-ack",
    )
    ready_request = _ready_finish_request(
        first,
        artifact_id=artifact_id,
        artifact_sha256=artifact_digest,
        snapshot_artifact_id=snapshot_id,
        snapshot_sha256=snapshot_digest,
    )

    clock["now"] = "2026-08-17T00:02:01Z"
    with pytest.raises(HTTPException) as expired:
        finish_assessment_report_finalization(
            "p1",
            ready_request,
            principal=TEST_DISPATCHER,
        )
    assert expired.value.detail == "assessment_report_claim_mismatch"
    _confirm_report_execution_resolved(conn, first.task_run_id)

    recovered = claim_assessment_report_finalization(
        "p1",
        "worker-a",
        lease_seconds=60,
        principal=TEST_DISPATCHER,
    )
    assert recovered.finish_disposition == "recovered"
    assert recovered.task_run_id == first.task_run_id
    assert recovered.status == "ready"
    assert recovered.artifact_id == artifact_id
    assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 1
    old_run = conn.execute(
        "SELECT status, ended_at FROM task_runs WHERE id = ?",
        (first.task_run_id,),
    ).fetchone()
    assert old_run["status"] == "succeeded"
    assert old_run["ended_at"] == clock["now"]
    assert conn.execute(
        "SELECT COUNT(*) FROM assessment_report_finish_receipts"
    ).fetchone()[0] == 1
    replayed = finish_assessment_report_finalization(
        "p1",
        ready_request,
        principal=TEST_DISPATCHER,
    )
    assert replayed == recovered
    receipt = conn.execute(
        "SELECT request_json FROM assessment_report_finish_receipts"
    ).fetchone()
    assert first.claim_token not in receipt["request_json"]
    assert sha256(first.claim_token.encode("utf-8")).hexdigest() in receipt[
        "request_json"
    ]


def test_expired_report_retry_exhaustion_terminalizes_finalization(
    report_claim_db,
    monkeypatch,
) -> None:
    conn = report_claim_db
    clock = {"now": "2026-08-17T00:01:00Z"}
    monkeypatch.setattr(
        "cairn.server.services.assessment_reports.utcnow",
        lambda: clock["now"],
    )

    claim = claim_assessment_report_finalization(
        "p1",
        "worker-a",
        lease_seconds=1,
        principal=TEST_DISPATCHER,
    )
    for timestamp in (
        "2026-08-17T00:01:02Z",
        "2026-08-17T00:01:04Z",
    ):
        _confirm_report_execution_resolved(conn, claim.task_run_id)
        clock["now"] = timestamp
        claim = claim_assessment_report_finalization(
            "p1",
            "worker-a",
            lease_seconds=1,
            principal=TEST_DISPATCHER,
        )

    _confirm_report_execution_resolved(conn, claim.task_run_id)
    clock["now"] = "2026-08-17T00:01:06Z"
    with pytest.raises(HTTPException) as exhausted:
        claim_assessment_report_finalization(
            "p1",
            "worker-a",
            lease_seconds=1,
            principal=TEST_DISPATCHER,
        )

    assert exhausted.value.status_code == 409
    assert exhausted.value.detail == "execution_retry_exhausted"
    finalization = _finalization_row(conn)
    assert finalization["status"] == "failed"
    assert finalization["claimed_by"] is None
    assert finalization["claim_token"] is None
    assert finalization["claim_sha256"] is None
    assert finalization["lease_expires_at"] is None
    assert finalization["next_attempt_at"] is None
    assert finalization["last_error"] == "execution_retry_exhausted"
    runs = conn.execute(
        "SELECT status, error_type FROM task_runs ORDER BY started_at, id"
    ).fetchall()
    assert len(runs) == 3
    assert [(row["status"], row["error_type"]) for row in runs] == [
        ("timed_out", "worker_timeout"),
        ("timed_out", "worker_timeout"),
        ("timed_out", "worker_timeout"),
    ]


def test_expired_claim_with_invalid_frozen_plan_fails_stably(
    report_claim_db,
    monkeypatch,
) -> None:
    conn = report_claim_db
    clock = {"now": "2026-08-17T00:01:00Z"}
    monkeypatch.setattr(
        "cairn.server.services.assessment_reports.utcnow",
        lambda: clock["now"],
    )
    first = claim_assessment_report_finalization(
        "p1",
        "worker-a",
        lease_seconds=1,
        principal=TEST_DISPATCHER,
    )
    conn.execute(
        "UPDATE assessment_report_finalizations "
        "SET report_execution_plan_json = '{' WHERE project_id = 'p1'"
    )
    conn.commit()
    _confirm_report_execution_resolved(conn, first.task_run_id)
    clock["now"] = "2026-08-17T00:01:02Z"

    with pytest.raises(HTTPException) as invalid:
        claim_assessment_report_finalization(
            "p1",
            "worker-a",
            lease_seconds=1,
            principal=TEST_DISPATCHER,
        )

    assert invalid.value.status_code == 409
    assert invalid.value.detail == "assessment_report_execution_plan_invalid"
    finalization = _finalization_row(conn)
    assert finalization["status"] == "failed"
    assert finalization["claimed_by"] is None
    assert finalization["claim_token"] is None
    assert finalization["claim_sha256"] is None
    assert finalization["lease_expires_at"] is None
    assert finalization["next_attempt_at"] is None
    assert finalization["last_error"] == "assessment_report_execution_plan_invalid"
    run = conn.execute(
        "SELECT status, ended_at, error_type FROM task_runs WHERE id = ?",
        (first.task_run_id,),
    ).fetchone()
    assert run is not None
    assert run["status"] == "timed_out"
    assert run["ended_at"] == "2026-08-17T00:01:01Z"
    assert run["error_type"] == "worker_timeout"


def test_manual_report_request_freezes_template_and_is_pending_idempotent(monkeypatch) -> None:
    conn = make_conn()
    try:
        seed_project(conn)
        first = request_assessment_report_generation(
            conn,
            "p1",
            template_key="full-technical",
            template_version=1,
            requested_by="user-1",
        )

        def reject_live_prompt_reload(_policy):
            raise AssertionError("pending retry must not reload live prompt resources")

        monkeypatch.setattr(
            "cairn.server.services.assessment_reports.freeze_report_execution_plan",
            reject_live_prompt_reload,
        )
        second = request_assessment_report_generation(
            conn,
            "p1",
            template_key="full-technical",
            template_version=1,
            requested_by="user-1",
        )
        stored = conn.execute(
            "SELECT template_markdown FROM assessment_report_finalizations WHERE project_id = 'p1'"
        ).fetchone()

        assert first.generation_token == second.generation_token
        assert first.requested_at is not None
        assert first.requested_by == "user-1"
        assert first.template_key == "full-technical"
        assert first.template_version == 1
        assert first.template_digest_sha256
        assert stored is not None
        assert "Surface Manifest" in stored["template_markdown"]
    finally:
        conn.close()


def test_stopped_project_with_vulnerability_can_request_claim_and_finish(monkeypatch) -> None:
    conn = make_conn()
    seed_project(conn, status="stopped")
    seed_vulnerability(conn)
    monkeypatch.setattr(
        "cairn.server.services.assessment_reports.get_conn",
        lambda: _connection_context(conn),
    )
    try:
        requested = request_assessment_report_generation(
            conn,
            "p1",
            template_key="standard-pentest",
            template_version=1,
            requested_by="user-1",
        )
        assert requested.status == "pending"
        assert conn.execute("SELECT status FROM projects WHERE id = 'p1'").fetchone()["status"] == "stopped"

        claimed = claim_assessment_report_finalization("p1", "worker-a", principal=TEST_DISPATCHER)
        failed = finish_assessment_report_finalization(
            "p1",
            _failed_finish_request(claimed),
            principal=TEST_DISPATCHER,
        )

        assert failed.status == "failed"
        assert failed.next_attempt_at is not None
        assert conn.execute("SELECT status FROM projects WHERE id = 'p1'").fetchone()["status"] == "stopped"
    finally:
        conn.close()


def test_stopped_zero_findings_can_request_but_active_runs_prevent_freezing() -> None:
    conn = make_conn()
    try:
        seed_project(conn, status="stopped")
        requested = request_assessment_report_generation(
            conn,
            "p1",
            template_key="standard-pentest",
            template_version=1,
            requested_by="user-1",
        )
        assert requested.status == "pending"
        assert build_assessment_report_packet(conn, "p1").vulnerabilities == []

        seed_vulnerability(conn)
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, task_type, worker_name, worker_type, status
            ) VALUES ('run-active', 'p1', 'explore', 'worker-a', 'pi', 'running')
            """
        )
        with pytest.raises(HTTPException) as not_quiescent:
            request_assessment_report_generation(
                conn,
                "p1",
                template_key="standard-pentest",
                template_version=1,
                requested_by="user-1",
            )
        assert not_quiescent.value.detail == "assessment_report_tasks_not_quiescent"
    finally:
        conn.close()


def test_active_project_remains_ineligible_for_manual_report() -> None:
    conn = make_conn()
    try:
        seed_project(conn, status="active")
        seed_vulnerability(conn)
        with pytest.raises(HTTPException) as active:
            request_assessment_report_generation(
                conn,
                "p1",
                template_key="standard-pentest",
                template_version=1,
                requested_by="user-1",
            )
        assert active.value.detail == "assessment_report_project_active"
    finally:
        conn.close()


def test_stopped_report_claim_rechecks_assessment_run_quiescence(monkeypatch) -> None:
    conn = make_conn()
    seed_project(conn, status="stopped")
    seed_vulnerability(conn)
    request_assessment_report_generation(
        conn,
        "p1",
        template_key="standard-pentest",
        template_version=1,
        requested_by="user-1",
    )
    conn.execute(
        """
        INSERT INTO task_runs (
            id, project_id, task_type, worker_name, worker_type, status
        ) VALUES ('run-late', 'p1', 'reason', 'worker-a', 'pi', 'queued')
        """
    )
    conn.commit()
    monkeypatch.setattr(
        "cairn.server.services.assessment_reports.get_conn",
        lambda: _connection_context(conn),
    )
    try:
        with pytest.raises(HTTPException) as not_quiescent:
            claim_assessment_report_finalization("p1", "worker-a", principal=TEST_DISPATCHER)
        assert not_quiescent.value.detail == "assessment_report_tasks_not_quiescent"
        assert get_assessment_report_finalization(conn, "p1").status == "pending"
    finally:
        conn.close()


def test_stopped_project_cannot_resume_while_report_finalization_is_outstanding(monkeypatch) -> None:
    conn = make_conn()
    seed_project(conn, status="stopped")
    seed_vulnerability(conn)
    request_assessment_report_generation(
        conn,
        "p1",
        template_key="standard-pentest",
        template_version=1,
        requested_by="user-1",
    )
    conn.commit()
    monkeypatch.setattr(
        "cairn.server.services.project_records.get_conn",
        lambda: _connection_context(conn),
    )
    try:
        with pytest.raises(HTTPException) as pending:
            update_project_status_record("p1", UpdateProjectStatusRequest(status="active"))
        assert pending.value.detail == "assessment_new_project_required"

        conn.execute(
            """
            UPDATE assessment_report_finalizations
            SET status = 'ready', next_attempt_at = NULL
            WHERE project_id = 'p1'
            """
        )
        with pytest.raises(HTTPException) as ready:
            update_project_status_record("p1", UpdateProjectStatusRequest(status="active"))
        assert ready.value.detail == "assessment_new_project_required"
    finally:
        conn.close()


def test_report_finalization_claim_is_leased_and_retryable(monkeypatch) -> None:
    conn = make_conn()
    try:
        seed_project(conn)
        request_assessment_report_generation(
            conn,
            "p1",
            template_key="standard-pentest",
            template_version=1,
            requested_by="user-1",
        )
        row = get_assessment_report_finalization(conn, "p1")
        assert row.status == "pending"
        assert row.attempt_count == 0
    finally:
        conn.close()

    monkeypatch.setattr(
        "cairn.server.services.assessment_reports.utcnow",
        lambda: "2026-08-17T00:01:00Z",
    )
    conn = make_persisted_conn()
    monkeypatch.setattr(
        "cairn.server.services.assessment_reports.get_conn",
        lambda: _connection_context(conn),
    )
    try:
        claimed = claim_assessment_report_finalization("p1", "worker-a", lease_seconds=300, principal=TEST_DISPATCHER)
        assert claimed.status == "running"
        assert claimed.attempt_count == 1
        assert claimed.generation_token
        assert claimed.claim_token
        assert get_assessment_report_finalization(conn, "p1").claim_token is None

        with pytest.raises(HTTPException) as conflict:
            claim_assessment_report_finalization("p1", "worker-b", lease_seconds=300, principal=TEST_DISPATCHER)
        assert conflict.value.status_code == 409

        failed = finish_assessment_report_finalization(
            "p1",
            _failed_finish_request(claimed),
            principal=TEST_DISPATCHER,
        )
        assert failed.status == "failed"
        assert failed.next_attempt_at is not None
    finally:
        conn.close()


def make_persisted_conn() -> sqlite3.Connection:
    conn = make_conn()
    seed_project(conn)
    request_assessment_report_generation(
        conn,
        "p1",
        template_key="standard-pentest",
        template_version=1,
        requested_by="user-1",
    )
    return conn


def test_unrequested_completed_project_is_not_claimable(monkeypatch) -> None:
    conn = make_conn()
    seed_project(conn)
    monkeypatch.setattr(
        "cairn.server.services.assessment_reports.get_conn",
        lambda: _connection_context(conn),
    )
    try:
        assert conn.execute(
            "SELECT 1 FROM assessment_report_finalizations WHERE project_id = 'p1'"
        ).fetchone() is None

        with pytest.raises(HTTPException) as missing:
            claim_assessment_report_finalization("p1", "worker-a", principal=TEST_DISPATCHER)

        assert missing.value.status_code == 404
    finally:
        conn.close()


class _connection_context:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()


def test_report_finalization_ready_requires_durable_report_artifact(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "cairn.server.services.assessment_reports.utcnow",
        lambda: "2026-08-17T00:01:00Z",
    )
    artifact_root = _configure_report_artifact_root(monkeypatch, tmp_path)
    conn = make_persisted_conn()
    monkeypatch.setattr(
        "cairn.server.services.assessment_reports.get_conn",
        lambda: _connection_context(conn),
    )
    try:
        claimed = claim_assessment_report_finalization(
            "p1",
            "worker-a",
            lease_seconds=300,
            principal=TEST_DISPATCHER,
        )
        snapshot_id, snapshot_digest = _write_report_input_snapshot(
            conn,
            artifact_root,
            claimed,
        )
        bind_assessment_report_input_snapshot(
            "p1",
            "worker-a",
            claim_token=claimed.claim_token,
            artifact_id=snapshot_id,
            digest_sha256=snapshot_digest,
            principal=TEST_DISPATCHER,
        )
        with pytest.raises(HTTPException) as missing:
            finish_assessment_report_finalization(
                "p1",
                _ready_finish_request(
                    claimed,
                    artifact_id="missing-report",
                    artifact_sha256="f" * 64,
                    snapshot_artifact_id=snapshot_id,
                    snapshot_sha256=snapshot_digest,
                ),
                principal=TEST_DISPATCHER,
            )
        assert missing.value.status_code == 409
        assert missing.value.detail == "assessment_report_artifact_invalid"
        assert conn.execute(
            "SELECT COUNT(*) FROM assessment_report_finish_receipts"
        ).fetchone()[0] == 0

        artifact_id, artifact_digest = _write_report_artifact(
            conn,
            artifact_root,
            claimed,
            snapshot_artifact_id=snapshot_id,
            snapshot_sha256=snapshot_digest,
            artifact_id="a-report",
        )
        ready = finish_assessment_report_finalization(
            "p1",
            _ready_finish_request(
                claimed,
                artifact_id=artifact_id,
                artifact_sha256=artifact_digest,
                snapshot_artifact_id=snapshot_id,
                snapshot_sha256=snapshot_digest,
            ),
            principal=TEST_DISPATCHER,
        )
        assert ready.status == "ready"
        assert ready.artifact_id == "a-report"
    finally:
        conn.close()


def test_recompleted_project_does_not_reuse_previous_report(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "cairn.server.services.assessment_reports.utcnow",
        lambda: "2026-08-17T00:01:00Z",
    )
    artifact_root = _configure_report_artifact_root(monkeypatch, tmp_path)
    conn = make_persisted_conn()
    monkeypatch.setattr(
        "cairn.server.services.assessment_reports.get_conn",
        lambda: _connection_context(conn),
    )
    try:
        first_claim = claim_assessment_report_finalization(
            "p1",
            "worker-a",
            principal=TEST_DISPATCHER,
        )
        first_snapshot_id, first_snapshot_digest = _write_report_input_snapshot(
            conn,
            artifact_root,
            first_claim,
            artifact_id="snapshot-old",
        )
        bind_assessment_report_input_snapshot(
            "p1",
            "worker-a",
            claim_token=first_claim.claim_token,
            artifact_id=first_snapshot_id,
            digest_sha256=first_snapshot_digest,
            principal=TEST_DISPATCHER,
        )
        old_artifact_id, old_artifact_digest = _write_report_artifact(
            conn,
            artifact_root,
            first_claim,
            snapshot_artifact_id=first_snapshot_id,
            snapshot_sha256=first_snapshot_digest,
            artifact_id="a-old",
        )
        ready = finish_assessment_report_finalization(
            "p1",
            _ready_finish_request(
                first_claim,
                artifact_id=old_artifact_id,
                artifact_sha256=old_artifact_digest,
                snapshot_artifact_id=first_snapshot_id,
                snapshot_sha256=first_snapshot_digest,
            ),
            principal=TEST_DISPATCHER,
        )
        assert ready.artifact_id == "a-old"

        pending = request_assessment_report_generation(
            conn,
            "p1",
            template_key="standard-pentest",
            template_version=1,
            requested_by="user-1",
        )
        assert pending.status == "pending"
        assert pending.generation_started_at == "2026-08-17T00:01:00Z"
        assert pending.generation_token != first_claim.generation_token

        second_claim = claim_assessment_report_finalization(
            "p1",
            "worker-b",
            principal=TEST_DISPATCHER,
        )
        second_snapshot_id, second_snapshot_digest = _write_report_input_snapshot(
            conn,
            artifact_root,
            second_claim,
            artifact_id="snapshot-new",
        )
        bind_assessment_report_input_snapshot(
            "p1",
            "worker-b",
            claim_token=second_claim.claim_token,
            artifact_id=second_snapshot_id,
            digest_sha256=second_snapshot_digest,
            principal=TEST_DISPATCHER,
        )
        with pytest.raises(HTTPException) as stale_artifact:
            finish_assessment_report_finalization(
                "p1",
                _ready_finish_request(
                    second_claim,
                    artifact_id=old_artifact_id,
                    artifact_sha256=old_artifact_digest,
                    snapshot_artifact_id=second_snapshot_id,
                    snapshot_sha256=second_snapshot_digest,
                    worker="worker-b",
                ),
                principal=TEST_DISPATCHER,
            )
        assert stale_artifact.value.status_code == 409
        assert stale_artifact.value.detail == "assessment_report_artifact_invalid"
    finally:
        conn.close()


def test_report_finalization_rejects_stale_same_worker_claim(monkeypatch) -> None:
    clock = {"now": "2026-08-17T00:01:00Z"}
    monkeypatch.setattr(
        "cairn.server.services.assessment_reports.utcnow",
        lambda: clock["now"],
    )
    conn = make_persisted_conn()
    monkeypatch.setattr(
        "cairn.server.services.assessment_reports.get_conn",
        lambda: _connection_context(conn),
    )
    try:
        first = claim_assessment_report_finalization(
            "p1", "worker-a", lease_seconds=60,
            principal=TEST_DISPATCHER,
        )
        _confirm_report_execution_resolved(conn, first.task_run_id)
        clock["now"] = "2026-08-17T00:02:01Z"
        second = claim_assessment_report_finalization(
            "p1", "worker-a", lease_seconds=60,
            principal=TEST_DISPATCHER,
        )
        assert second.claim_token != first.claim_token

        with pytest.raises(HTTPException) as stale:
            finish_assessment_report_finalization(
                "p1",
                _failed_finish_request(first),
                principal=TEST_DISPATCHER,
            )
        assert stale.value.status_code == 409
        assert stale.value.detail == "assessment_report_claim_mismatch"

        finished = finish_assessment_report_finalization(
            "p1",
            _failed_finish_request(second, error_message="retry later"),
            principal=TEST_DISPATCHER,
        )
        assert finished.status == "failed"
    finally:
        conn.close()


def test_report_snapshot_binding_rejects_stale_claim(
    monkeypatch,
    tmp_path: Path,
) -> None:
    clock = {"now": "2026-08-17T00:01:00Z"}
    monkeypatch.setattr(
        "cairn.server.services.assessment_reports.utcnow",
        lambda: clock["now"],
    )
    artifact_root = _configure_report_artifact_root(monkeypatch, tmp_path)
    conn = make_persisted_conn()
    monkeypatch.setattr(
        "cairn.server.services.assessment_reports.get_conn",
        lambda: _connection_context(conn),
    )
    try:
        first = claim_assessment_report_finalization(
            "p1",
            "worker-a",
            lease_seconds=60,
            principal=TEST_DISPATCHER,
        )
        snapshot_id, snapshot_digest = _write_report_input_snapshot(
            conn,
            artifact_root,
            first,
        )
        bound = bind_assessment_report_input_snapshot(
            "p1",
            "worker-a",
            claim_token=first.claim_token,
            artifact_id=snapshot_id,
            digest_sha256=snapshot_digest,
            principal=TEST_DISPATCHER,
        )
        assert bound.input_snapshot_artifact_id == snapshot_id
        assert bound.input_snapshot_sha256 == snapshot_digest

        _confirm_report_execution_resolved(conn, first.task_run_id)
        clock["now"] = "2026-08-17T00:02:01Z"
        second = claim_assessment_report_finalization(
            "p1",
            "worker-a",
            lease_seconds=60,
            principal=TEST_DISPATCHER,
        )
        assert second.claim_token != first.claim_token
        assert second.input_snapshot_artifact_id is None
        assert second.input_snapshot_sha256 is None

        with pytest.raises(HTTPException) as invalid_artifact:
            bind_assessment_report_input_snapshot(
                "p1",
                "worker-a",
                claim_token=second.claim_token,
                artifact_id=snapshot_id,
                digest_sha256=snapshot_digest,
                principal=TEST_DISPATCHER,
            )
        assert invalid_artifact.value.status_code == 409
        assert (
            invalid_artifact.value.detail
            == "assessment_report_snapshot_artifact_invalid"
        )

        with pytest.raises(HTTPException) as stale:
            bind_assessment_report_input_snapshot(
                "p1",
                "worker-a",
                claim_token=first.claim_token,
                artifact_id=snapshot_id,
                digest_sha256="b" * 64,
                principal=TEST_DISPATCHER,
            )
        assert stale.value.status_code == 409
        assert stale.value.detail == "assessment_report_claim_mismatch"
    finally:
        conn.close()


def test_report_finalization_rejects_finish_after_project_reopen(monkeypatch) -> None:
    monkeypatch.setattr(
        "cairn.server.services.assessment_reports.utcnow",
        lambda: "2026-08-17T00:01:00Z",
    )
    conn = make_persisted_conn()
    monkeypatch.setattr(
        "cairn.server.services.assessment_reports.get_conn",
        lambda: _connection_context(conn),
    )
    try:
        claimed = claim_assessment_report_finalization("p1", "worker-a", principal=TEST_DISPATCHER)
        conn.execute("UPDATE projects SET status = 'active' WHERE id = 'p1'")

        with pytest.raises(HTTPException) as reopened:
            finish_assessment_report_finalization(
                "p1",
                _failed_finish_request(claimed),
                principal=TEST_DISPATCHER,
            )
        assert reopened.value.status_code == 409
        assert reopened.value.detail == "assessment_report_project_active"
    finally:
        conn.close()


def test_report_packet_preserves_all_observations_verifications_and_evidence() -> None:
    conn = make_conn()
    try:
        seed_project(conn)
        conn.execute(
            """
            INSERT INTO facts (id, project_id, description, creator, created_at)
            VALUES ('f1', 'p1', 'finding', 'worker-a', ?)
            """,
            ("2026-08-17T00:01:00Z",),
        )
        conn.execute(
            """
            INSERT INTO vulnerabilities (
                id, project_id, title, severity, confidence, status,
                source_fact_id, identity_json, created_at, updated_at
            ) VALUES ('v1', 'p1', 'IDOR', 'high', 0.8, 'confirmed', 'f1',
                      ?, ?, ?)
            """,
            (
                json.dumps(
                    {
                        "version": 1,
                        "vulnerability_class": "broken_object_level_authorization",
                        "asset": {"protocol": "https", "host": "example.com", "port": 443},
                        "entrypoint": {"method": "GET", "route": "/accounts/1"},
                        "vector": {"parameter": "id", "object_type": "account"},
                    }
                ),
                "2026-08-17T00:01:00Z",
                "2026-08-17T00:01:00Z",
            ),
        )
        for index in (1, 2):
            conn.execute(
                """
                INSERT INTO vulnerability_observations (
                    id, project_id, vulnerability_id, source_fact_id, worker,
                    identity_json, severity, confidence, observed_at
                ) VALUES (?, 'p1', 'v1', 'f1', 'worker-a', '{}',
                          'high', 0.8, ?)
                """,
                (f"o{index}", f"2026-08-17T00:0{index + 1}:00Z"),
            )
        conn.execute(
            """
            INSERT INTO artifacts (
                id, project_id, kind, category, uri, sha256, size,
                created_at, metadata_json
            ) VALUES ('a1', 'p1', 'packet', 'evidence', 'artifact://packet',
                      'digest', 1, ?, '{}')
            """,
            ("2026-08-17T00:02:00Z",),
        )
        conn.execute(
            """
            INSERT INTO fact_evidence (
                fact_id, project_id, artifact_id, pointer, created_at
            ) VALUES ('f1', 'p1', 'a1', '/response/body', ?)
            """,
            ("2026-08-17T00:02:00Z",),
        )
        for suffix, observation_id, outcome, independence_level in (
            ("2", "o2", "not_reproduced", "same_worker_new_session"),
            ("1", "o1", "confirmed", "different_worker"),
        ):
            intent_id = f"verify-{suffix}"
            task_run_id = f"verify-run-{suffix}"
            fact_id = f"verify-fact-{suffix}"
            conn.execute(
                """
                INSERT INTO intents (
                    id, project_id, description, creator, created_at,
                    assessment_role, target_observation_id, verifier_schedule_key
                ) VALUES (?, 'p1', 'verify', 'system', ?, 'verifier', ?, ?)
                """,
                (
                    intent_id,
                    "2026-08-17T00:03:00Z",
                    observation_id,
                    observation_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO task_runs (
                    id, project_id, task_type, worker_name, worker_type,
                    status, started_at, ended_at
                ) VALUES (?, 'p1', 'verify', ?, 'pi', 'succeeded', ?, ?)
                """,
                (
                    task_run_id,
                    f"worker-{suffix}",
                    "2026-08-17T00:03:00Z",
                    "2026-08-17T00:04:00Z",
                ),
            )
            conn.execute(
                """
                INSERT INTO facts (
                    id, project_id, description, creator, task_run_id, created_at
                ) VALUES (?, 'p1', ?, ?, ?, ?)
                """,
                (
                    fact_id,
                    f"verification {suffix}",
                    f"worker-{suffix}",
                    task_run_id,
                    "2026-08-17T00:04:00Z",
                ),
            )
            conn.execute(
                """
                INSERT INTO vulnerability_verifications (
                    id, project_id, vulnerability_id, observation_id, intent_id,
                    fact_id, verifier_task_run_id, verifier_worker,
                    discovery_task_run_id, discovery_worker, independence_level,
                    outcome, evidence_json, notes, created_at
                ) VALUES (?, 'p1', 'v1', ?, ?, ?, ?, ?, 'discovery-run',
                          'worker-a', ?, ?, ?, ?, ?)
                """,
                (
                    f"vv{suffix}",
                    observation_id,
                    intent_id,
                    fact_id,
                    task_run_id,
                    f"worker-{suffix}",
                    independence_level,
                    outcome,
                    json.dumps(
                        [
                            {
                                "artifact_id": "a1",
                                "pointer": f"/verification/{suffix}",
                                "verifier_run_id": task_run_id,
                            }
                        ]
                    ),
                    f"notes {suffix}",
                    "2026-08-17T00:05:00Z",
                ),
            )

        packet = build_assessment_report_packet(conn, "p1")

        assert packet.version == 2
        assert packet.project["coverage_stop_reason"] == "saturated"
        assert packet.assessment_summary["stop_reason"] == "saturated"
        assert [(item["id"], item["verification_status"]) for item in packet.observation_statuses] == [
            ("o1", "confirmed"), ("o2", "not_reproduced"),
        ]
        assert [item["id"] for item in packet.vulnerabilities] == ["v1"]
        assert [item["id"] for item in packet.observations] == ["o1", "o2"]
        assert [item["id"] for item in packet.verifications] == ["vv1", "vv2"]
        assert [
            {
                "observation_id": item["observation_id"],
                "outcome": item["outcome"],
                "independence_level": item["independence_level"],
                "evidence": item["evidence"],
            }
            for item in packet.verifications
        ] == [
            {
                "observation_id": "o1",
                "outcome": "confirmed",
                "independence_level": "different_worker",
                "evidence": [
                    {
                        "artifact_id": "a1",
                        "pointer": "/verification/1",
                        "verifier_run_id": "verify-run-1",
                    }
                ],
            },
            {
                "observation_id": "o2",
                "outcome": "not_reproduced",
                "independence_level": "same_worker_new_session",
                "evidence": [
                    {
                        "artifact_id": "a1",
                        "pointer": "/verification/2",
                        "verifier_run_id": "verify-run-2",
                    }
                ],
            },
        ]
        assert packet.evidence_links == [
            {
                "project_id": "p1",
                "fact_id": "f1",
                "artifact_id": "a1",
                "pointer": "/response/body",
                "verifier_run_id": None,
                "created_at": "2026-08-17T00:02:00Z",
            }
        ]
        assert packet.artifacts[0]["id"] == "a1"
    finally:
        conn.close()


def test_goal_driven_report_coverage_uses_experiment_records_and_intent_conclusions() -> None:
    conn = make_conn()
    try:
        seed_project(conn)
        records = {
            "fact-tested": {
                "version": 1,
                "kind": "experiment",
                "hypothesis": "对象读取应检查归属",
                "outcome": "tested_no_finding",
                "observations": ["非所有者请求返回 403"],
                "evidence_refs": [],
            },
            "fact-blocked": {
                "version": 1,
                "kind": "experiment",
                "hypothesis": "第二身份可能读取导出",
                "outcome": "temporarily_blocked",
                "limitation_reason": "缺少已授权第二身份",
                "limitation_details": "租户没有第二个测试账号",
                "limitations": ["身份对照未执行"],
                "attempted_strategies": ["检查可用测试身份"],
                "evidence_refs": [],
            },
            "fact-observation": {
                "version": 1,
                "kind": "experiment",
                "hypothesis": "响应可能泄露调试标记",
                "outcome": "observation",
                "limitations": ["仅看到配置提示，尚未完成对照实验"],
                "evidence_refs": [],
            },
            "fact-scope": {
                "version": 1,
                "kind": "experiment",
                "hypothesis": "管理端点可能暴露",
                "outcome": "out_of_scope",
                "limitation_reason": "目标不在授权范围",
                "evidence_refs": [],
            },
        }
        for index, (fact_id, record) in enumerate(records.items(), start=1):
            conn.execute(
                """
                INSERT INTO facts (
                    id, project_id, description, creator, created_at,
                    assessment_record_json
                ) VALUES (?, 'p1', ?, 'worker-a', ?, ?)
                """,
                (
                    fact_id,
                    record["hypothesis"],
                    f"2026-08-17T00:0{index}:00Z",
                    json.dumps(record),
                ),
            )
        for index, (intent_id, fact_id, conclusion, reason) in enumerate((
            ("intent-tested", "fact-tested", "completed", ""),
            ("intent-blocked", "fact-blocked", "completed", ""),
            ("intent-observation", "fact-observation", "completed", ""),
            ("intent-scope", "fact-scope", "completed", ""),
            ("intent-open", None, "abandoned", "user_stopped"),
        ), start=1):
            conn.execute(
                """
                INSERT INTO intents (
                    id, project_id, to_fact_id, description, creator, created_at,
                    concluded_at, conclusion, conclusion_reason
                ) VALUES (?, 'p1', ?, ?, 'reasoner', ?, ?, ?, ?)
                """,
                (
                    intent_id,
                    fact_id,
                    f"work {intent_id}",
                    f"2026-08-17T00:1{index}:00Z",
                    f"2026-08-17T00:2{index}:00Z",
                    conclusion,
                    reason,
                ),
            )
        conn.execute(
            """
            INSERT INTO intents (
                id, project_id, description, creator, created_at, assessment_role
            ) VALUES ('verify-ignored', 'p1', 'independent verification',
                      'system', '2026-08-17T00:30:00Z', 'verifier')
            """
        )
        conn.execute(
            """
            INSERT INTO intents (
                id, project_id, description, creator, created_at
            ) VALUES ('bootstrap-ignored', 'p1', 'bootstrap',
                      'dispatcher.bootstrap', '2026-08-17T00:31:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO intents (
                id, project_id, to_fact_id, description, creator, created_at,
                concluded_at, conclusion
            ) VALUES ('completion-ignored', 'p1', 'goal', 'natural completion',
                      'reasoner', '2026-08-17T00:32:00Z',
                      '2026-08-17T00:33:00Z', 'completed')
            """
        )

        coverage = assessment_reports._goal_driven_report_coverage(conn, "p1")

        assert [item["intent_id"] for item in coverage["tested"]] == [
            "intent-tested"
        ]
        assert coverage["tested"][0]["result"] == "tested_no_finding"
        assert [item["intent_id"] for item in coverage["blocked"]] == [
            "intent-blocked"
        ]
        assert coverage["blocked"][0]["reason"] == (
            "缺少已授权第二身份; 租户没有第二个测试账号; 身份对照未执行"
        )
        assert coverage["blocked"][0]["attempted_strategies"] == [
            "检查可用测试身份"
        ]
        assert [item["intent_id"] for item in coverage["untested"]] == [
            "intent-observation",
            "intent-scope",
            "intent-open",
        ]
        assert coverage["untested"][0]["result"] == "observation"
        assert coverage["untested"][1]["result"] == "out_of_scope"
        assert coverage["untested"][2]["reason"] == "user_stopped"
        assert {
            item["intent_id"]
            for values in coverage.values()
            for item in values
        }.isdisjoint({"verify-ignored", "bootstrap-ignored", "completion-ignored"})
    finally:
        conn.close()
