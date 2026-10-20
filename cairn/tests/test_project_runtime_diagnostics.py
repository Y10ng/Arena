"""运行诊断只投影授权项目的可验证事实，不在读取时修改执行状态。"""
import json
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from fastapi.testclient import TestClient
import pytest

from cairn.server import db
from cairn.server.app import app
from cairn.server.auth import protect_cookie_request
from cairn.server.schemas.projects import CreateProjectRequest
from cairn.server.schemas.monitoring_dispatcher import DispatcherHeartbeatRequest
from cairn.server.services.monitoring.dispatcher import record_dispatcher_heartbeat
from cairn.server.services.project_records import create_project_record
from cairn.server.services.time_utils import utcnow
from test_project_creation_workers import configured_database, _principal, _worker


@pytest.fixture
def runtime_client():
    principal = _principal("runtime-owner")
    worker = _worker(principal, "Runtime Worker")
    project = create_project_record(CreateProjectRequest(
        title="Synthetic runtime", origin="https://example.test", goal="Synthetic fixture",
        scenario="goal-driven-v1", tenant_worker_ids=[worker.id],
        authorization_expires_at=None,
    ), tenant_id=principal.tenant_id)
    previous = dict(app.dependency_overrides)
    app.dependency_overrides[protect_cookie_request] = lambda: principal
    try:
        with TestClient(app) as client:
            yield client, principal, project.project.id
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def _heartbeat(
    project_id,
    *,
    code="no_candidates",
    generation=0,
    observed_at=None,
    status="idle",
    candidate_recovery=None,
):
    runtime = {
        "runtime_capabilities": ["project_runtime_v1", "project_pause_v1"],
        "project_decisions": [{
            "project_id": project_id, "execution_generation": generation,
            "observed_at": observed_at or utcnow(), "code": code,
            "message": "PRIVATE-CANARY", "config_path": "PRIVATE-CANARY",
        }],
    }
    if candidate_recovery is not None:
        runtime["candidate_recovery"] = candidate_recovery
    with db.get_conn() as conn:
        record_dispatcher_heartbeat(conn, DispatcherHeartbeatRequest(
            dispatcher_id="synthetic-dispatcher", status=status,
            host="PRIVATE-CANARY", error_message="PRIVATE-CANARY",
            runtime=runtime,
        ))


def _canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _seed_report_finalization(
    conn,
    project_id: str,
    *,
    status: str = "running",
    generation_token: str = "report-generation",
    next_attempt_at: str | None = None,
):
    now = "2026-09-21T00:00:00Z"
    conn.execute(
        """
        INSERT INTO assessment_report_finalizations (
            project_id, status, generation_started_at, generation_token,
            next_attempt_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            status,
            now,
            generation_token,
            next_attempt_at,
            now,
            now,
        ),
    )


def _seed_report_run(
    conn,
    project_id: str,
    run_id: str,
    *,
    status: str,
    lifecycle: dict,
    generation: int = 0,
    generation_token: str = "report-generation",
    error_type: str | None = None,
):
    ended_at = utcnow() if status in {"failed", "timed_out", "succeeded", "cancelled"} else None
    conn.execute(
        """
        INSERT INTO task_runs (
            id, project_id, task_type, worker_name, worker_type, status,
            started_at, ended_at, error_type, runtime_context_json,
            execution_generation
        ) VALUES (?, ?, 'report', 'synthetic', 'pi', ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            project_id,
            status,
            utcnow(),
            ended_at,
            error_type,
            json.dumps({
                "dispatcher_id": "synthetic-dispatcher",
                "report_claim": {"generation_token": generation_token},
                "execution_lifecycle": lifecycle,
            }),
            generation,
        ),
    )


def _seed_receipt_backed_run(conn, project_id: str, receipt_kind: str) -> str:
    run_id = f"run-{receipt_kind}"
    task_type = (
        "reason"
        if receipt_kind in {"completion", "intent_prefix", "depth_review"}
        else "explore" if receipt_kind == "conclusion" else receipt_kind
    )
    context = {}
    intent_id = None
    if receipt_kind == "conclusion":
        intent_id = "intent-conclusion"
        context = {
            "accepted_result_digest": "1" * 64,
            "accepted_fact_id": "fact-conclusion",
        }
    elif receipt_kind == "completion":
        context = {
            "accepted_completion_digest": "2" * 64,
            "accepted_completion_intent_id": "intent-completion",
        }
    elif receipt_kind == "intent_prefix":
        context = {
            "accepted_intent_submissions": {
                "0": {"digest": "3" * 64, "intent_id": "intent-prefix"},
            },
        }
    conn.execute(
        """
        INSERT INTO task_runs (
            id, project_id, intent_id, task_type, worker_name, worker_type,
            status, started_at, ended_at, runtime_context_json,
            execution_generation
        ) VALUES (?, ?, ?, ?, 'synthetic', 'pi', 'succeeded', ?, ?, ?, 0)
        """,
        (
            run_id,
            project_id,
            intent_id,
            task_type,
            "2026-09-18T00:00:00Z",
            "2026-09-18T00:01:00Z",
            json.dumps(context),
        ),
    )
    if receipt_kind == "conclusion":
        conn.execute(
            """
            INSERT INTO facts (
                id, project_id, description, task_run_id, creator, created_at
            ) VALUES ('fact-conclusion', ?, 'accepted', ?, 'synthetic', ?)
            """,
            (project_id, run_id, "2026-09-18T00:01:00Z"),
        )
        conn.execute(
            """
            INSERT INTO intents (
                id, project_id, to_fact_id, description, creator, worker,
                created_at, concluded_at, conclusion
            ) VALUES ('intent-conclusion', ?, 'fact-conclusion', 'accepted',
                      'reason', 'synthetic', ?, ?, 'completed')
            """,
            (
                project_id,
                "2026-09-18T00:00:00Z",
                "2026-09-18T00:01:00Z",
            ),
        )
    elif receipt_kind == "completion":
        conn.execute(
            """
            INSERT INTO intents (
                id, project_id, to_fact_id, description, creator, task_run_id,
                worker, created_at, concluded_at, conclusion
            ) VALUES ('intent-completion', ?, 'goal', 'complete', 'synthetic', ?,
                      'synthetic', ?, ?, 'completed')
            """,
            (
                project_id,
                run_id,
                "2026-09-18T00:00:00Z",
                "2026-09-18T00:01:00Z",
            ),
        )
    elif receipt_kind == "intent_prefix":
        conn.execute(
            """
            INSERT INTO intents (
                id, project_id, description, creator, task_run_id, created_at
            ) VALUES ('intent-prefix', ?, 'accepted prefix', 'synthetic', ?, ?)
            """,
            (project_id, run_id, "2026-09-18T00:01:00Z"),
        )
    elif receipt_kind == "depth_review":
        record = {
            "version": 1,
            "kind": "depth_review",
            "input_digest": "4" * 64,
            "description": "review",
            "evidence_refs": [{"artifact_id": "a1"}],
            "selected": [],
            "worker": "synthetic",
            "task_run_id": run_id,
            "submission_digest": "5" * 64,
        }
        conn.execute(
            """
            INSERT INTO facts (
                id, project_id, description, task_run_id, creator, created_at,
                assessment_record_json
            ) VALUES ('fact-depth', ?, 'review', ?, 'synthetic', ?, ?)
            """,
            (
                project_id,
                run_id,
                "2026-09-18T00:01:00Z",
                json.dumps(record),
            ),
        )
        conn.execute(
            "UPDATE projects SET assessment_progress_json = ? WHERE id = ?",
            (
                json.dumps({
                    "version": 1,
                    "phase": "deep",
                    "depth_review_fact_id": "fact-depth",
                    "accepted_input_digest": "4" * 64,
                }),
                project_id,
            ),
        )
    elif receipt_kind == "report":
        request = {
            "protocol": "cairn-assessment-report-finish-v1",
            "project_id": project_id,
            "authenticated_dispatcher_id": "synthetic-dispatcher",
            "worker": "synthetic",
            "claim_sha256": "6" * 64,
            "generation_token": "report-generation",
            "task_run_id": run_id,
            "status": "ready",
            "artifact_id": "report-artifact",
            "artifact_sha256": "7" * 64,
            "input_snapshot_artifact_id": "snapshot-artifact",
            "input_snapshot_sha256": "8" * 64,
            "task_run_result": {"status": "succeeded"},
        }
        request_json = _canonical_json(request)
        request_digest = sha256(request_json.encode()).hexdigest()
        response = {
            "protocol": "cairn-assessment-report-finish-v1",
            "project_id": project_id,
            "task_run_id": run_id,
            "generation_token": "report-generation",
            "request_sha256": request_digest,
            "status": "ready",
            "finish_disposition": "accepted",
            "artifact_id": "report-artifact",
            "input_snapshot_artifact_id": "snapshot-artifact",
        }
        for artifact_id, digest in (
            ("report-artifact", "7" * 64),
            ("snapshot-artifact", "8" * 64),
        ):
            conn.execute(
                """
                INSERT INTO artifacts (
                    id, project_id, task_run_id, kind, uri, sha256, size,
                    created_at
                ) VALUES (?, ?, ?, 'report', ?, ?, 1, ?)
                """,
                (
                    artifact_id,
                    project_id,
                    run_id,
                    f"artifacts/{artifact_id}",
                    digest,
                    "2026-09-18T00:01:00Z",
                ),
            )
        conn.execute(
            """
            INSERT INTO assessment_report_finalizations (
                project_id, status, generation_started_at, generation_token,
                artifact_id, input_snapshot_artifact_id, input_snapshot_sha256,
                created_at, updated_at, completed_at
            ) VALUES (?, 'ready', ?, 'report-generation', 'report-artifact',
                      'snapshot-artifact', ?, ?, ?, ?)
            """,
            (
                project_id,
                "2026-09-18T00:00:00Z",
                "8" * 64,
                "2026-09-18T00:00:00Z",
                "2026-09-18T00:01:00Z",
                "2026-09-18T00:01:00Z",
            ),
        )
        conn.execute(
            """
            INSERT INTO assessment_report_finish_receipts (
                project_id, task_run_id, protocol, generation_token,
                request_sha256, request_json, response_json, artifact_id,
                artifact_sha256, input_snapshot_artifact_id,
                input_snapshot_sha256, accepted_at
            ) VALUES (?, ?, 'cairn-assessment-report-finish-v1',
                      'report-generation', ?, ?, ?, 'report-artifact', ?,
                      'snapshot-artifact', ?, ?)
            """,
            (
                project_id,
                run_id,
                request_digest,
                request_json,
                _canonical_json(response),
                "7" * 64,
                "8" * 64,
                "2026-09-18T00:01:00Z",
            ),
        )
    return run_id


@pytest.mark.parametrize("code", ["submission_missing", "submission_invalid"])
def test_gateway_submission_diagnostics_persist_and_reach_project_view(runtime_client, code):
    from cairn.dispatcher.contracts import validate_bootstrap_execute_payload
    from cairn.dispatcher.worker_result_gateway import decode_pi_result
    from cairn.server.auth import RequestPrincipal, require_tenant_project
    from cairn.server.schemas.tasks import CreateTaskRunRequest
    from cairn.server.services.audit_task_runs import insert_task_run_record

    client, principal, project_id = runtime_client
    machine = RequestPrincipal(kind="machine", subject_id="submission-fixture")
    session = "pi:07b53667-a6ab-44be-badb-e3ed6b347331"
    with db.get_conn() as conn:
        run = insert_task_run_record(conn, project_id, CreateTaskRunRequest(
            task_type="bootstrap", worker_name="fixture", worker_type="pi", status="running", session_id=session,
        ), principal=machine)
    payload = {
        "protocol": "cairn-agent-result-v1", "engine": "pi", "project_id": project_id,
        "task_run_id": run.id, "phase": "bootstrap_execute", "session": session,
        "status": "failed", "result": None, "submission_id": None, "usage": None,
        "error": {"code": code, "message": "PRIVATE-CANARY", "submission_attempts": 3,
                  "repair_rounds": 2, "max_repair_rounds": 2,
                  "issue": {"keyword": "type", "path": "/data/fact/description", "value": "PRIVATE-CANARY"}},
    }
    failure = decode_pi_result(json.dumps(payload), phase="bootstrap_execute", validator=validate_bootstrap_execute_payload).failure
    app.dependency_overrides[require_tenant_project] = lambda: machine
    try:
        updated = client.patch(f"/projects/{project_id}/task-runs/{run.id}", json={
            "status": "failed", "error_type": code, "error_message": failure.message,
            "runtime_context": {"worker_failure": failure.details()},
        })
        assert updated.status_code == 200, updated.text
        stored = client.get(f"/projects/{project_id}/task-runs")
        assert stored.status_code == 200, stored.text
        persisted = next(item for item in stored.json() if item["id"] == run.id)
        assert persisted["runtime_context"]["worker_failure"]["submission_attempts"] == 3
    finally:
        app.dependency_overrides.pop(require_tenant_project, None)
    response = client.get(f"/projects/{project_id}/runtime")
    assert response.status_code == 200, response.text
    error = response.json()["last_error"]
    assert error["code"] == code
    assert error["submission"] == {"code": code, "issue": {"keyword": "type", "path": "/data/fact/description"},
                                    "submission_attempts": 3, "repair_rounds": 2, "max_repair_rounds": 2}
    assert "PRIVATE-CANARY" not in response.text


def test_empty_runs_without_dispatcher_are_unknown_not_waiting(runtime_client):
    client, _, project_id = runtime_client
    response = client.get(f"/projects/{project_id}/runtime")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["state"] == "unknown"
    assert payload["code"] == "dispatcher_unobserved"
    assert payload["active_runs"] == []
    assert payload["last_activity"] is None


@pytest.mark.parametrize("schema_version", [1, 2])
def test_pending_runtime_single_and_mixed_batch_preserve_schedule_gate(
    runtime_client, schema_version,
):
    client, principal, active_id = runtime_client
    pending = create_project_record(
        CreateProjectRequest(
            title="Synthetic pending runtime", origin="https://example.test",
            goal="Synthetic fixture", scenario="goal-driven-v1",
            tenant_worker_ids=[_worker(principal, "Pending Runtime Worker").id],
            authorization_expires_at=None,
        ),
        tenant_id=principal.tenant_id,
        initial_status="pending",
    )
    pending_id = pending.project.id
    single = client.get(
        f"/projects/{pending_id}/runtime", params={"schema_version": schema_version},
    )
    batch = client.get(
        "/projects/runtime",
        params={"project_ids": f"{pending_id},{active_id}", "schema_version": schema_version},
    )
    assert single.status_code == 200, single.text
    assert batch.status_code == 200, batch.text
    by_id = {item["project_id"]: item for item in batch.json()["items"]}
    for payload in (single.json(), by_id[pending_id]):
        assert payload["schema_version"] == schema_version
        assert payload["project_status"] == payload["state"] == "pending"
        assert payload["code"] == "project_pending_schedule_required"
        assert "启动计划" in payload["message"]
        assert payload["scheduling"] is None
        assert payload["active_runs"] == []
        assert payload["actions"]["resume"] == {
            "allowed": False, "reason": "project_pending_schedule_required",
        }
        if schema_version == 2:
            assert payload["axes"]["execution"]["state"] == "pending"
            assert payload["recovery"]["route"] == "none"
    assert by_id[active_id]["code"] == "dispatcher_unobserved"
    denied = client.post(f"/projects/{pending_id}/resume")
    assert denied.status_code == 409
    assert denied.json()["detail"] == "project_pending_schedule_required"
    with db.get_conn() as conn:
        assert conn.execute("SELECT status FROM projects WHERE id = ?", (pending_id,)).fetchone()["status"] == "pending"
        assert conn.execute("SELECT COUNT(*) AS n FROM task_runs WHERE project_id = ?", (pending_id,)).fetchone()["n"] == 0


def test_runtime_schema_negotiation_preserves_v1_and_rejects_unknown_version(
    runtime_client,
):
    client, _, project_id = runtime_client

    v1 = client.get(f"/projects/{project_id}/runtime")
    v2 = client.get(
        f"/projects/{project_id}/runtime",
        params={"schema_version": 2},
    )
    batch_v1 = client.get(
        "/projects/runtime",
        params={"project_ids": project_id},
    )
    batch_v2 = client.get(
        "/projects/runtime",
        params={"project_ids": project_id, "schema_version": 2},
    )

    assert v1.status_code == 200
    assert v1.json()["schema_version"] == 1
    assert "axes" not in v1.json()
    assert v2.status_code == 200
    assert v2.json()["schema_version"] == 2
    assert v2.json()["checkpoint_staging"] == {
        "state": "none",
        "received_count": 0,
        "current_generation_count": 0,
        "historical_generation_count": 0,
        "last_received_at": None,
        "last_task_run_id": None,
        "business_acceptance": "not_evaluated",
    }
    assert batch_v1.status_code == 200
    assert batch_v1.json()["items"][0]["schema_version"] == 1
    assert "axes" not in batch_v1.json()["items"][0]
    assert batch_v2.status_code == 200
    assert batch_v2.json()["items"][0]["schema_version"] == 2
    assert batch_v2.json()["items"][0]["checkpoint_staging"]["state"] == "none"
    assert client.get(
        f"/projects/{project_id}/runtime",
        params={"schema_version": 3},
    ).status_code == 422
    assert client.get(
        "/projects/runtime",
        params={"project_ids": project_id, "schema_version": 3},
    ).status_code == 422


def test_runtime_v2_separates_terminal_process_cleanup_and_checkpoint_axes(
    runtime_client,
):
    client, _, project_id = runtime_client
    lifecycle = {
        "generation": 1,
        "process_started": True,
        "termination_confirmed": False,
        "cleanup_confirmed": False,
    }
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE projects SET execution_generation = 1 WHERE id = ?",
            (project_id,),
        )
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, task_type, worker_name, worker_type, status,
                started_at, ended_at, runtime_context_json, execution_generation
            ) VALUES ('run-v2', ?, 'explore', 'synthetic', 'pi', 'succeeded', ?, ?, ?, 1)
            """,
            (
                project_id,
                "2026-09-18T00:00:00Z",
                "2026-09-18T00:01:00Z",
                json.dumps({"execution_lifecycle": lifecycle}),
            ),
        )
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, task_type, worker_name, worker_type, status,
                started_at, ended_at, runtime_context_json, execution_generation
            ) VALUES ('run-old', ?, 'explore', 'synthetic', 'pi', 'failed', ?, ?, ?, 0)
            """,
            (
                project_id,
                "2026-09-17T00:00:00Z",
                "2026-09-17T00:01:00Z",
                json.dumps({"execution_lifecycle": {"generation": 0}}),
            ),
        )
        for event_id, task_run_id, generation, disposition, received_at in (
            ("xev-current", "run-v2", 1, "current_at_receipt", "2026-09-18T00:02:00Z"),
            ("xev-historical", "run-old", 0, "historical_at_receipt", "2026-09-18T00:03:00Z"),
        ):
            conn.execute(
                """
                INSERT INTO execution_events (
                    id, project_id, task_run_id, protocol, producer_event_key,
                    event_kind, phase, category, producer_recorded_at, received_at,
                    verified_dispatcher_id, execution_generation,
                    generation_disposition, payload_digest_sha256, payload_json
                ) VALUES (?, ?, ?, 'cairn-execution-event-v1', ?,
                          'checkpoint', 'explore_execute', 'progress', ?, ?,
                          'synthetic-dispatcher', ?, ?, ?, ?)
                """,
                (
                    event_id,
                    project_id,
                    task_run_id,
                    f"key-{event_id}",
                    received_at,
                    received_at,
                    generation,
                    disposition,
                    "0" * 64,
                    json.dumps({"private": "PRIVATE-CANARY"}),
                ),
            )

    response = client.get(
        f"/projects/{project_id}/runtime",
        params={"schema_version": 2},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["axes"]["task_run_id"] == "run-v2"
    assert payload["axes"]["delivery"] == {
        "state": "unconfirmed",
        "code": "business_delivery_unconfirmed",
    }
    assert payload["axes"]["acceptance"] == {
        "state": "not_evaluated",
        "code": "business_acceptance_unproven",
        "evidence": None,
    }
    assert payload["axes"]["process"] == {
        "state": "running",
        "code": "process_started",
        "termination_confirmed": False,
    }
    assert payload["axes"]["resources"] == {
        "state": "in_use",
        "code": "process_resources_active",
        "cleanup_confirmed": False,
    }
    assert payload["checkpoint_staging"] == {
        "state": "staged",
        "received_count": 2,
        "current_generation_count": 1,
        "historical_generation_count": 1,
        "last_received_at": "2026-09-18T00:03:00Z",
        "last_task_run_id": "run-old",
        "business_acceptance": "not_evaluated",
    }
    assert "PRIVATE-CANARY" not in response.text


@pytest.mark.parametrize(
    "receipt_kind,expected_state,expected_code,expected_evidence",
    [
        ("conclusion", "accepted", "conclusion_accepted", "conclusion_receipt"),
        ("completion", "accepted", "completion_accepted", "completion_receipt"),
        (
            "intent_prefix",
            "partially_accepted",
            "intent_prefix_accepted",
            "intent_submission_prefix",
        ),
        ("depth_review", "accepted", "depth_review_accepted", "depth_review_receipt"),
        ("report", "accepted", "report_accepted", "report_finish_receipt"),
    ],
)
def test_runtime_v2_acceptance_requires_verified_server_receipts(
    runtime_client,
    receipt_kind,
    expected_state,
    expected_code,
    expected_evidence,
):
    client, _, project_id = runtime_client
    with db.get_conn() as conn:
        run_id = _seed_receipt_backed_run(conn, project_id, receipt_kind)

    response = client.get(
        f"/projects/{project_id}/runtime",
        params={"schema_version": 2},
    )

    assert response.status_code == 200, response.text
    axes = response.json()["axes"]
    assert axes["task_run_id"] == run_id
    assert axes["delivery"] == {
        "state": "confirmed",
        "code": "business_receipt_persisted",
    }
    assert axes["acceptance"] == {
        "state": expected_state,
        "code": expected_code,
        "evidence": expected_evidence,
    }


def test_failed_report_receipt_confirms_delivery_not_acceptance(runtime_client):
    client, _, project_id = runtime_client
    request = {
        "protocol": "cairn-assessment-report-finish-v1",
        "project_id": project_id,
        "authenticated_dispatcher_id": "synthetic-dispatcher",
        "worker": "synthetic",
        "claim_sha256": "6" * 64,
        "generation_token": "failed-generation",
        "task_run_id": "run-report-failed",
        "status": "failed",
        "artifact_id": None,
        "artifact_sha256": None,
        "input_snapshot_artifact_id": None,
        "input_snapshot_sha256": None,
        "task_run_result": {
            "status": "failed",
            "error_type": "provider_error",
        },
    }
    request_json = _canonical_json(request)
    request_digest = sha256(request_json.encode()).hexdigest()
    response = {
        "protocol": "cairn-assessment-report-finish-v1",
        "project_id": project_id,
        "task_run_id": "run-report-failed",
        "generation_token": "failed-generation",
        "request_sha256": request_digest,
        "status": "failed",
        "finish_disposition": "accepted",
        "artifact_id": None,
        "input_snapshot_artifact_id": None,
    }
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, task_type, worker_name, worker_type, status,
                ended_at, execution_generation
            ) VALUES ('run-report-failed', ?, 'report', 'synthetic', 'pi',
                      'failed', ?, 0)
            """,
            (project_id, "2026-09-18T00:01:00Z"),
        )
        conn.execute(
            """
            INSERT INTO assessment_report_finish_receipts (
                project_id, task_run_id, protocol, generation_token,
                request_sha256, request_json, response_json, accepted_at
            ) VALUES (?, 'run-report-failed',
                      'cairn-assessment-report-finish-v1',
                      'failed-generation', ?, ?, ?, ?)
            """,
            (
                project_id,
                request_digest,
                request_json,
                _canonical_json(response),
                "2026-09-18T00:01:00Z",
            ),
        )

    axes = client.get(
        f"/projects/{project_id}/runtime",
        params={"schema_version": 2},
    ).json()["axes"]

    assert axes["delivery"] == {
        "state": "confirmed",
        "code": "business_receipt_persisted",
    }
    assert axes["acceptance"] == {
        "state": "not_evaluated",
        "code": "report_finish_failed",
        "evidence": None,
    }


def test_runtime_v2_rejects_dangling_conclusion_receipt(runtime_client):
    client, _, project_id = runtime_client
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, intent_id, task_type, worker_name, worker_type,
                status, ended_at, runtime_context_json, execution_generation
            ) VALUES ('run-dangling', ?, 'intent-dangling', 'explore',
                      'synthetic', 'pi', 'succeeded', ?, ?, 0)
            """,
            (
                project_id,
                "2026-09-18T00:01:00Z",
                json.dumps({
                    "accepted_result_digest": "a" * 64,
                    "accepted_fact_id": "fact-other-run",
                }),
            ),
        )
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, task_type, worker_name, worker_type, status,
                ended_at, execution_generation
            ) VALUES ('run-other', ?, 'explore', 'synthetic', 'pi',
                      'succeeded', ?, 0)
            """,
            (project_id, "2026-09-17T00:01:00Z"),
        )
        conn.execute(
            """
            INSERT INTO facts (
                id, project_id, description, task_run_id, creator, created_at
            ) VALUES ('fact-other-run', ?, 'other', 'run-other', 'synthetic', ?)
            """,
            (project_id, "2026-09-17T00:01:00Z"),
        )
        conn.execute(
            """
            INSERT INTO intents (
                id, project_id, to_fact_id, description, creator, created_at,
                concluded_at, conclusion
            ) VALUES ('intent-dangling', ?, 'fact-other-run', 'dangling',
                      'synthetic', ?, ?, 'completed')
            """,
            (
                project_id,
                "2026-09-17T00:00:00Z",
                "2026-09-17T00:01:00Z",
            ),
        )

    axes = client.get(
        f"/projects/{project_id}/runtime",
        params={"schema_version": 2},
    ).json()["axes"]

    assert axes["task_run_id"] == "run-dangling"
    assert axes["delivery"]["state"] == "unconfirmed"
    assert axes["acceptance"] == {
        "state": "not_evaluated",
        "code": "business_acceptance_unproven",
        "evidence": None,
    }


@pytest.mark.parametrize("code,state", [
    ("engine_unsupported", "blocked"), ("worker_capacity", "waiting"),
    ("worker_cooldown", "waiting"), ("no_candidates", "blocked"),
    ("no_work", "waiting"), ("PRIVATE-CANARY", "unknown"),
])
def test_real_decision_is_project_scoped_and_whitelisted(runtime_client, code, state):
    client, _, project_id = runtime_client
    _heartbeat(project_id, code=code)
    response = client.get(f"/projects/{project_id}/runtime")
    assert response.status_code == 200, response.text
    assert response.json()["state"] == state
    assert "PRIVATE-CANARY" not in response.text


@pytest.mark.parametrize("block_reason", [
    "project_authorization_scope_upgrade_required",
    "project_authorization_scope_changed",
    "project_authorization_expired",
    "project_reauthorization_required",
])
def test_idle_project_reports_current_authorization_before_worker_wait(runtime_client, block_reason):
    client, _, project_id = runtime_client
    _heartbeat(project_id, code="worker_capacity")
    with db.get_conn() as conn:
        if block_reason == "project_authorization_scope_upgrade_required":
            grant = conn.execute(
                "SELECT * FROM project_authorization_grants_v2 WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            descriptor = json.loads(grant["scope_descriptor_json"])
            descriptor["version"] = 2
            digest = "authorization-scope:" + sha256((
                "cairn-project-authorization-scope-v2\n" + _canonical_json(descriptor)
            ).encode()).hexdigest()
            # 新增历史格式的测试 grant，不修改不可变原件。
            conn.execute(
                """INSERT INTO project_authorization_grants_v2 (
                    project_id, authorization_epoch, protocol, source, actor_type,
                    actor_id, scope_digest, scope_descriptor_json,
                    authorization_expires_at, execution_generation, authorized_at
                ) SELECT project_id, 2, protocol, source, actor_type, actor_id, ?, ?,
                         authorization_expires_at, execution_generation, authorized_at
                  FROM project_authorization_grants_v2 WHERE project_id = ?""",
                (digest, _canonical_json(descriptor), project_id),
            )
            conn.execute(
                "UPDATE projects SET authorization_epoch = 2, authorization_scope_digest = ? WHERE id = ?",
                (digest, project_id),
            )
        elif block_reason == "project_authorization_scope_changed":
            conn.execute("UPDATE targets SET value = 'https://changed.test/' WHERE project_id = ?", (project_id,))
        elif block_reason == "project_authorization_expired":
            conn.execute("UPDATE projects SET authorization_expires_at = '2000-01-01T00:00:00Z' WHERE id = ?", (project_id,))
        else:
            conn.execute("UPDATE projects SET authorization_epoch = 0 WHERE id = ?", (project_id,))
        before = "\n".join(conn.iterdump())

    for path, params in [
        (f"/projects/{project_id}/runtime", {"schema_version": 2}),
        ("/projects/runtime", {"project_ids": project_id, "schema_version": 2}),
    ]:
        response = client.get(path, params=params)
        assert response.status_code == 200, response.text
        payload = response.json()
        if "items" in payload:
            payload = payload["items"][0]
        assert payload["code"] == block_reason
        assert payload["state"] == "blocked"
        assert payload["scheduling"]["code"] == block_reason
        assert payload["active_runs"] == []
        assert "新任务尚未开始" in payload["message"]
        if block_reason == "project_authorization_scope_upgrade_required":
            assert "显式确认一次授权" in payload["message"]
            assert "不是等待 Worker" in payload["message"]
    with db.get_conn() as conn:
        assert "\n".join(conn.iterdump()) == before


@pytest.mark.parametrize("old_reason", [
    "project_authorization_scope_upgrade_required",
    "project_authorization_scope_changed",
    "project_authorization_expired",
    "project_reauthorization_required",
])
def test_valid_authorization_discards_old_decision_in_same_generation(runtime_client, old_reason):
    client, _, project_id = runtime_client
    _heartbeat(project_id, code=old_reason)
    response = client.get(f"/projects/{project_id}/runtime")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["execution_generation"] == 0
    assert payload["code"] == "scheduling_unobserved"
    assert payload["state"] == "unknown"
    assert payload["scheduling"] is None


@pytest.mark.parametrize(
    "decision_code,expected_state,expected_route",
    [
        ("worker_capacity", "waiting", "wait"),
        ("engine_unsupported", "blocked", "manual"),
        ("task_create_failed", "failed", "manual"),
    ],
)
def test_runtime_v2_recovery_route_matches_waiting_and_blocked_state(
    runtime_client,
    decision_code,
    expected_state,
    expected_route,
):
    client, _, project_id = runtime_client
    _heartbeat(project_id, code=decision_code)

    payload = client.get(
        f"/projects/{project_id}/runtime",
        params={"schema_version": 2},
    ).json()

    assert payload["state"] == expected_state
    assert payload["recovery"] == {
        "route": expected_route,
        "code": decision_code,
    }


@pytest.mark.parametrize("generation,observed_at", [(999, None), (0, "2000-01-01T00:00:00Z")])
def test_old_or_other_generation_decision_is_not_current(runtime_client, generation, observed_at):
    client, _, project_id = runtime_client
    _heartbeat(project_id, generation=generation, observed_at=observed_at)
    payload = client.get(f"/projects/{project_id}/runtime").json()
    assert payload["state"] == "unknown"
    assert payload["scheduling"] is None


def test_runtime_get_does_not_expire_or_change_database(runtime_client):
    client, _, project_id = runtime_client
    _heartbeat(project_id)
    with db.get_conn() as conn:
        conn.execute("UPDATE dispatcher_status SET updated_at = '2000-01-01T00:00:00Z'")
        before = "\n".join(conn.iterdump())
    for _ in range(2):
        response = client.get(f"/projects/{project_id}/runtime")
        assert response.status_code == 200
        assert response.json()["dispatcher"]["freshness"] == "stale"
    with db.get_conn() as conn:
        assert "\n".join(conn.iterdump()) == before


def test_project_runtime_not_limited_by_tenant_queue_and_does_not_invent_activity(runtime_client):
    client, _, project_id = runtime_client
    _heartbeat(project_id, code="dispatched")
    with db.get_conn() as conn:
        for index in range(14):
            conn.execute(
                "INSERT INTO task_runs (id, project_id, task_type, worker_name, worker_type, status, runtime_context_json) VALUES (?, ?, 'explore', 'synthetic', 'pi', 'running', ?)",
                (f"run-{index:02d}", project_id, json.dumps({"dispatcher_id": "synthetic-dispatcher"})),
            )
    payload = client.get(f"/projects/{project_id}/runtime").json()
    assert len(payload["active_runs"]) == 14
    assert all(run["execution_stage"] == "assigned" for run in payload["active_runs"])
    assert payload["state"] == "starting"
    assert payload["last_activity"] is None


@pytest.mark.parametrize("owner", ["stale-owner", "unknown-owner", None])
def test_unrelated_fresh_dispatcher_never_replaces_execution_owner(runtime_client, owner):
    client, _, project_id = runtime_client
    _heartbeat(project_id, code="dispatched")
    with db.get_conn() as conn:
        if owner == "stale-owner":
            conn.execute("INSERT INTO dispatcher_status (id, status, updated_at, runtime_json) VALUES ('stale-owner', 'running', '2000-01-01T00:00:00Z', ?)", (json.dumps({"runtime_capabilities": ["project_runtime_v1", "project_pause_v1"]}),))
        conn.execute(
            "INSERT INTO task_runs (id, project_id, task_type, worker_name, worker_type, status, runtime_context_json) VALUES ('owned', ?, 'reason', 'synthetic', 'pi', 'running', ?)",
            (project_id, json.dumps({"dispatcher_id": owner, "execution_lifecycle": {"generation": 0, "process_started": True}})),
        )
    for payload in (client.get(f"/projects/{project_id}/runtime").json(), client.get("/projects/runtime", params={"project_ids": project_id}).json()["items"][0]):
        assert payload["state"] == "unknown"
        assert payload["dispatcher"]["freshness"] == ("stale" if owner == "stale-owner" else "unknown")
        assert payload["scheduling"] is None
        assert payload["actions"]["pause"]["allowed"] is True


def test_foreign_project_hidden_in_single_and_batch(runtime_client):
    client, _, project_id = runtime_client
    other = _principal("runtime-other")
    app.dependency_overrides[protect_cookie_request] = lambda: other
    assert client.get(f"/projects/{project_id}/runtime").status_code == 404
    assert client.get("/projects/runtime", params={"project_ids": project_id}).status_code == 404


def test_batch_has_same_projection_without_private_fields(runtime_client):
    client, _, project_id = runtime_client
    _heartbeat(project_id, code="engine_unsupported")
    response = client.get("/projects/runtime", params={"project_ids": project_id})
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["code"] == "engine_unsupported"
    assert items[0]["project_id"] == project_id
    assert "PRIVATE-CANARY" not in response.text


@pytest.mark.parametrize("runtime", [
    {"runtime_capabilities": None},
    {"runtime_capabilities": ["project_runtime_v1"], "project_decisions": None},
    {"runtime_capabilities": ["project_runtime_v1"], "project_decisions": [{"code": []}]},
    {"runtime_capabilities": ["project_runtime_v1"], "candidate_recovery": [{"status": []}]},
    {"runtime_capabilities": "project_runtime_v1"},
])
def test_malformed_or_unsupported_observations_are_unknown(runtime_client, runtime):
    client, _, project_id = runtime_client
    runtime = dict(runtime)
    if isinstance(runtime.get("project_decisions"), list):
        runtime["project_decisions"] = [
            {**item, "project_id": project_id, "execution_generation": 0, "observed_at": utcnow()}
            for item in runtime["project_decisions"]
        ]
    if isinstance(runtime.get("candidate_recovery"), list):
        runtime["candidate_recovery"] = [
            {**item, "project_id": project_id, "reason": "test", "observed_at": utcnow()}
            for item in runtime["candidate_recovery"]
        ]
    with db.get_conn() as conn:
        record_dispatcher_heartbeat(conn, DispatcherHeartbeatRequest(
            dispatcher_id="synthetic", runtime=runtime,
        ))
    response = client.get(f"/projects/{project_id}/runtime")
    assert response.status_code == 200
    assert response.json()["state"] == "unknown"
    assert response.json()["scheduling"] is None


@pytest.mark.parametrize("status,code,state", [
    ("starting", "dispatcher_starting", "starting"),
    ("error", "startup_healthcheck_failed", "failed"),
    ("stopping", "dispatcher_unavailable", "unknown"),
])
def test_startup_state_is_not_normal_waiting(runtime_client, status, code, state):
    client, _, project_id = runtime_client
    with db.get_conn() as conn:
        record_dispatcher_heartbeat(conn, DispatcherHeartbeatRequest(
            dispatcher_id="synthetic", status=status,
            error_message="PRIVATE-CANARY",
            runtime={"startup_check": {"status": "failed", "code": "startup_healthcheck_failed"}},
        ))
    response = client.get(f"/projects/{project_id}/runtime")
    assert response.status_code == 200
    assert response.json()["state"] == state
    assert response.json()["code"] == code
    assert "PRIVATE-CANARY" not in response.text


def test_same_second_precise_dispatcher_observation_is_fresh(runtime_client, monkeypatch):
    from cairn.server.services import project_runtime_diagnostics, time_utils

    client, _, project_id = runtime_client
    fixed = datetime.now(timezone.utc).replace(microsecond=900000)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(time_utils, "datetime", FixedDatetime)
    monkeypatch.setattr(project_runtime_diagnostics, "datetime", FixedDatetime, raising=False)
    _heartbeat(project_id, code="engine_unsupported", observed_at=fixed.isoformat())
    payload = client.get(f"/projects/{project_id}/runtime").json()
    assert payload["code"] == "engine_unsupported"
    assert payload["state"] == "blocked"


def test_future_decision_is_not_treated_as_fresh(runtime_client):
    client, _, project_id = runtime_client
    _heartbeat(project_id, observed_at="2999-01-01T00:00:00Z")
    response = client.get(f"/projects/{project_id}/runtime")
    assert response.json()["state"] == "unknown"
    assert response.json()["scheduling"] is None


@pytest.mark.parametrize("generation,expected", [(0, "running"), (999, "starting")])
def test_process_start_requires_matching_execution_generation(runtime_client, generation, expected):
    client, _, project_id = runtime_client
    _heartbeat(project_id, code="dispatched")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO task_runs (id, project_id, task_type, worker_name, worker_type, status, runtime_context_json) VALUES ('run', ?, 'explore', 'synthetic', 'pi', 'running', ?)",
            (project_id, json.dumps({"dispatcher_id": "synthetic-dispatcher", "execution_lifecycle": {"generation": generation, "process_started": True}})),
        )
    response = client.get(f"/projects/{project_id}/runtime")
    assert response.json()["state"] == expected
    assert response.json()["last_activity"] is None


def test_candidate_recovery_unknown_is_project_scoped_and_actionable(runtime_client):
    client, _, project_id = runtime_client
    observed_at = datetime.now(timezone.utc)
    recovery = {
        "project_id": project_id,
        "task_run_id": "run",
        "status": "unknown",
        "reason": "docker_daemon_unavailable",
        "observed_at": (observed_at - timedelta(seconds=1)).isoformat(),
    }
    _heartbeat(
        project_id,
        code="dispatched",
        candidate_recovery=[
            recovery,
            {
                "project_id": project_id,
                "task_run_id": "other-run",
                "status": "running",
                "reason": "exec_running",
                "observed_at": observed_at.isoformat(),
            },
        ],
    )
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO task_runs (id, project_id, task_type, worker_name, worker_type, status, runtime_context_json) VALUES ('run', ?, 'explore', 'synthetic', 'pi', 'running', ?)",
            (project_id, json.dumps({"dispatcher_id": "synthetic-dispatcher", "execution_lifecycle": {"generation": 0, "process_started": True}})),
        )

    payload = client.get(
        f"/projects/{project_id}/runtime",
        params={"schema_version": 2},
    ).json()

    assert payload["state"] == "blocked"
    assert payload["code"] == "candidate_recovery_unknown"
    assert payload["recovery"] == {
        "route": "automatic_retry",
        "code": "candidate_recovery_unknown",
    }
    assert payload["message"] == (
        "Docker daemon 当前不可达，无法确认原执行是否仍在运行，请恢复 Docker 后等待自动重试"
    )

    _heartbeat(
        project_id,
        code="dispatched",
        candidate_recovery=[{**recovery, "project_id": "unrelated-project"}],
    )
    unrelated = client.get(f"/projects/{project_id}/runtime").json()
    assert unrelated["state"] == "running"
    assert unrelated["code"] == "process_started"

    _heartbeat(project_id, code="dispatched", candidate_recovery=[recovery])
    assert client.post(f"/projects/{project_id}/pause").status_code == 200
    paused = client.get(f"/projects/{project_id}/runtime").json()
    assert paused["project_status"] == "paused"
    assert paused["state"] == "blocked"
    assert paused["code"] == "candidate_recovery_unknown"

    with db.get_conn() as conn:
        conn.execute("UPDATE task_runs SET status = 'cancelled' WHERE id = 'run'")
    _heartbeat(project_id, code="no_candidates", generation=1, candidate_recovery=[recovery])
    old_generation = client.get(
        f"/projects/{project_id}/runtime", params={"schema_version": 2},
    ).json()
    assert old_generation["code"] == "candidate_recovery_unknown"
    assert old_generation["axes"]["task_run_id"] == "run"
    assert old_generation["axes"]["process"]["termination_confirmed"] is False

    with db.get_conn() as conn:
        conn.execute(
            "UPDATE task_runs SET runtime_context_json = ? WHERE id = 'run'",
            (json.dumps({"dispatcher_id": "synthetic-dispatcher", "execution_lifecycle": {
                "generation": 0, "process_started": True,
                "termination_confirmed": True, "cleanup_confirmed": True,
            }}),),
        )
    confirmed = client.get(f"/projects/{project_id}/runtime").json()
    assert confirmed["code"] != "candidate_recovery_unknown"


@pytest.mark.parametrize(
    "reason,expected_route",
    [
        ("docker_daemon_unavailable", "automatic_retry"),
        ("exec_inspection_unavailable", "automatic_retry"),
        ("exec_missing_process_unconfirmed", "wait"),
        ("termination_proof_unavailable", "wait"),
        ("docker_daemon_changed", "manual"),
        ("container_identity_mismatch", "manual"),
    ],
)
def test_candidate_recovery_reason_selects_truthful_recovery_route(
    runtime_client,
    reason,
    expected_route,
):
    client, _, project_id = runtime_client
    _heartbeat(
        project_id,
        code="dispatched",
        candidate_recovery=[{
            "project_id": project_id,
            "task_run_id": "run-candidate",
            "status": "unknown",
            "reason": reason,
            "observed_at": utcnow(),
        }],
    )
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, task_type, worker_name, worker_type, status,
                runtime_context_json
            ) VALUES ('run-candidate', ?, 'explore', 'synthetic', 'pi',
                      'running', ?)
            """,
            (
                project_id,
                json.dumps({
                    "dispatcher_id": "synthetic-dispatcher",
                    "execution_lifecycle": {
                        "generation": 0,
                        "process_started": True,
                    },
                }),
            ),
        )

    payload = client.get(
        f"/projects/{project_id}/runtime",
        params={"schema_version": 2},
    ).json()

    assert payload["code"] == "candidate_recovery_unknown"
    assert payload["recovery"] == {
        "route": expected_route,
        "code": "candidate_recovery_unknown",
    }


def test_manual_candidate_recovery_outranks_newer_transient_unknown(
    runtime_client,
):
    client, _, project_id = runtime_client
    observed_at = datetime.now(timezone.utc)
    _heartbeat(
        project_id,
        code="dispatched",
        candidate_recovery=[
            {
                "project_id": project_id,
                "task_run_id": "run-manual",
                "status": "unknown",
                "reason": "docker_daemon_changed",
                "observed_at": (observed_at - timedelta(seconds=1)).isoformat(),
            },
            {
                "project_id": project_id,
                "task_run_id": "run-transient",
                "status": "unknown",
                "reason": "docker_daemon_unavailable",
                "observed_at": observed_at.isoformat(),
            },
        ],
    )
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, task_type, worker_name, worker_type, status,
                runtime_context_json
            ) VALUES ('run-manual', ?, 'explore', 'synthetic', 'pi',
                      'running', ?)
            """,
            (
                project_id,
                json.dumps({
                    "dispatcher_id": "synthetic-dispatcher",
                    "execution_lifecycle": {
                        "generation": 0,
                        "process_started": True,
                    },
                }),
            ),
        )

    payload = client.get(
        f"/projects/{project_id}/runtime",
        params={"schema_version": 2},
    ).json()

    assert payload["recovery"]["route"] == "manual"
    assert "Docker daemon 与原执行不一致" in payload["message"]


@pytest.mark.parametrize("project_status", ["stopped", "completed"])
@pytest.mark.parametrize(
    "run_status,lifecycle,expected_state,expected_code,process_state,resource_state",
    [
        (
            "queued",
            {
                "generation": 0,
                "process_started": False,
                "termination_confirmed": False,
                "cleanup_confirmed": False,
            },
            "starting",
            "execution_assigned",
            "unconfirmed",
            "unconfirmed",
        ),
        (
            "running",
            {
                "generation": 0,
                "process_started": True,
                "termination_confirmed": False,
                "cleanup_confirmed": False,
            },
            "running",
            "process_started",
            "running",
            "in_use",
        ),
        (
            "failed",
            {
                "generation": 0,
                "process_started": True,
                "termination_confirmed": True,
                "cleanup_confirmed": False,
            },
            "waiting",
            "cleanup_pending",
            "terminated",
            "cleanup_pending",
        ),
    ],
)
def test_terminal_project_projects_current_report_execution_until_cleanup(
    runtime_client,
    project_status,
    run_status,
    lifecycle,
    expected_state,
    expected_code,
    process_state,
    resource_state,
):
    client, _, project_id = runtime_client
    _heartbeat(project_id, code="no_work")
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE projects SET status = ? WHERE id = ?",
            (project_status, project_id),
        )
        _seed_report_finalization(conn, project_id)
        _seed_report_run(
            conn,
            project_id,
            "run-report-current",
            status=run_status,
            lifecycle=lifecycle,
        )

    payload = client.get(
        f"/projects/{project_id}/runtime",
        params={"schema_version": 2},
    ).json()

    assert payload["project_status"] == project_status
    assert payload["state"] == expected_state
    assert payload["code"] == expected_code
    assert payload["axes"]["task_run_id"] == "run-report-current"
    assert payload["axes"]["execution"] == {
        "state": expected_state,
        "code": expected_code,
    }
    assert payload["axes"]["process"]["state"] == process_state
    assert payload["axes"]["resources"]["state"] == resource_state
    if expected_state == "waiting":
        assert payload["recovery"] == {
            "route": "wait",
            "code": "cleanup_pending",
        }


@pytest.mark.parametrize(
    "project_status,generation,generation_token,expected_state,expected_code",
    [
        ("stopped", 0, "old-report-generation", "stopping", "project_finish_draining"),
        ("completed", 1, "report-generation", "completed", "completed"),
    ],
)
def test_terminal_project_does_not_project_old_report_attempt_as_current(
    runtime_client,
    project_status,
    generation,
    generation_token,
    expected_state,
    expected_code,
):
    client, _, project_id = runtime_client
    _heartbeat(project_id, code="no_work")
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE projects SET status = ? WHERE id = ?",
            (project_status, project_id),
        )
        _seed_report_finalization(conn, project_id)
        _seed_report_run(
            conn,
            project_id,
            "run-report-old",
            status="running",
            lifecycle={
                "generation": generation,
                "process_started": True,
                "termination_confirmed": False,
                "cleanup_confirmed": False,
            },
            generation=generation,
            generation_token=generation_token,
        )

    payload = client.get(f"/projects/{project_id}/runtime").json()

    assert payload["state"] == expected_state
    assert payload["code"] == expected_code


@pytest.mark.parametrize(
    "failure_count,error_type,next_attempt_at,expected_state,expected_code,expected_route",
    [
        (
            1,
            "provider_error",
            "2999-01-01T00:00:00Z",
            "waiting",
            "execution_retry_cooldown",
            "automatic_retry",
        ),
        (
            1,
            "contract_error",
            None,
            "blocked",
            "execution_retry_blocked",
            "manual",
        ),
        (
            3,
            "provider_error",
            None,
            "blocked",
            "execution_retry_exhausted",
            "manual",
        ),
    ],
)
def test_terminal_project_projects_report_retry_state(
    runtime_client,
    failure_count,
    error_type,
    next_attempt_at,
    expected_state,
    expected_code,
    expected_route,
):
    client, _, project_id = runtime_client
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE projects SET status = 'stopped' WHERE id = ?",
            (project_id,),
        )
        _seed_report_finalization(
            conn,
            project_id,
            status="failed",
            next_attempt_at=next_attempt_at,
        )
        for index in range(failure_count):
            _seed_report_run(
                conn,
                project_id,
                f"run-report-failed-{index}",
                status="failed",
                error_type=error_type,
                lifecycle={
                    "generation": 0,
                    "process_started": True,
                    "termination_confirmed": True,
                    "cleanup_confirmed": True,
                },
            )

    payload = client.get(
        f"/projects/{project_id}/runtime",
        params={"schema_version": 2},
    ).json()

    assert payload["state"] == expected_state
    assert payload["code"] == expected_code
    assert payload["scheduling"]["code"] == expected_code
    assert payload["recovery"] == {
        "route": expected_route,
        "code": expected_code,
    }


def test_terminal_state_precedes_old_scheduler_observation(runtime_client):
    client, _, project_id = runtime_client
    _heartbeat(project_id, code="dispatched")
    assert client.post(f"/projects/{project_id}/finish").status_code == 200
    response = client.get(f"/projects/{project_id}/runtime")
    assert response.json()["state"] == "stopped"
    assert response.json()["actions"]["resume"]["allowed"] is False


def test_finished_project_with_unconfirmed_pause_does_not_claim_physical_stop(runtime_client):
    client, _, project_id = runtime_client
    _heartbeat(project_id, code="no_work")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO task_runs (id, project_id, task_type, worker_name, worker_type, status, pause_requested) VALUES ('run', ?, 'explore', 'synthetic', 'pi', 'cancelled', 1)",
            (project_id,),
        )
    assert client.post(f"/projects/{project_id}/finish").status_code == 200
    payload = client.get(f"/projects/{project_id}/runtime").json()
    assert payload["project_status"] == "stopped"
    assert payload["state"] == "stopping"
    assert payload["code"] == "project_finish_draining"
    assert payload["actions"]["resume"]["allowed"] is False


@pytest.mark.parametrize(
    "lifecycle,expected_state,expected_code",
    [
        (
            {
                "generation": 0,
                "process_started": True,
                "termination_confirmed": True,
                "cleanup_confirmed": True,
            },
            "stopped",
            "stopped",
        ),
        (
            {
                "generation": 1,
                "process_started": True,
                "termination_confirmed": True,
                "cleanup_confirmed": True,
            },
            "stopping",
            "project_finish_draining",
        ),
        (
            {
                "generation": 0,
                "process_started": True,
                "termination_confirmed": True,
                "cleanup_confirmed": False,
            },
            "stopping",
            "project_finish_draining",
        ),
    ],
)
def test_finished_project_requires_matching_generation_and_complete_drain_proof(
    runtime_client,
    lifecycle,
    expected_state,
    expected_code,
):
    client, _, project_id = runtime_client
    _heartbeat(project_id, code="no_work")
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, task_type, worker_name, worker_type, status,
                runtime_context_json, execution_generation
            ) VALUES ('run-drain-proof', ?, 'explore', 'synthetic', 'pi',
                      'failed', ?, 0)
            """,
            (
                project_id,
                json.dumps({
                    "dispatcher_id": "synthetic-dispatcher",
                    "execution_lifecycle": lifecycle,
                }),
            ),
        )
    assert client.post(f"/projects/{project_id}/finish").status_code == 200

    payload = client.get(f"/projects/{project_id}/runtime").json()

    assert payload["state"] == expected_state
    assert payload["code"] == expected_code


def test_finished_project_explains_unknown_candidate_recovery(runtime_client):
    client, _, project_id = runtime_client
    _heartbeat(
        project_id,
        code="no_work",
        candidate_recovery=[{
            "project_id": project_id,
            "task_run_id": "run",
            "status": "unknown",
            "reason": "exec_missing_process_unconfirmed",
            "observed_at": utcnow(),
        }],
    )
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO task_runs (id, project_id, task_type, worker_name, worker_type, status, pause_requested, runtime_context_json) VALUES ('run', ?, 'explore', 'synthetic', 'pi', 'cancelled', 1, ?)",
            (project_id, json.dumps({"dispatcher_id": "synthetic-dispatcher"})),
        )
    assert client.post(f"/projects/{project_id}/finish").status_code == 200

    payload = client.get(f"/projects/{project_id}/runtime").json()

    assert payload["project_status"] == "stopped"
    assert payload["state"] == "stopping"
    assert payload["code"] == "candidate_recovery_unknown"
    assert payload["message"] == (
        "Docker 已找不到原 exec，但容器仍在且进程组停止尚未证实，暂不释放执行"
    )


@pytest.mark.parametrize("error_type,expected", [("contract_error", "execution_retry_blocked"), ("transport_error", "execution_retry_cooldown")])
def test_retry_diagnostics_use_durable_server_eligibility_without_dispatcher(runtime_client, error_type, expected):
    client, _, project_id = runtime_client
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO task_runs (id, project_id, task_type, worker_name, worker_type, status, error_type, ended_at) VALUES ('failed', ?, 'reason', 'synthetic', 'pi', 'failed', ?, ?)",
            (project_id, error_type, utcnow()),
        )
        before = "\n".join(conn.iterdump())
    payload = client.get(f"/projects/{project_id}/runtime").json()
    assert payload["code"] == expected
    assert payload["scheduling"]["code"] == expected
    assert bool(payload["scheduling"]["retry_at"]) == (error_type == "transport_error")
    assert payload["actions"]["pause"]["allowed"] is True
    with db.get_conn() as conn:
        assert "\n".join(conn.iterdump()) == before


def test_batch_limits_and_duplicate_ids(runtime_client):
    client, _, project_id = runtime_client
    response = client.get("/projects/runtime", params={"project_ids": f"{project_id},{project_id}"})
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert client.get("/projects/runtime", params={"project_ids": ",".join(str(i) for i in range(101))}).status_code == 422


@pytest.mark.parametrize("confirmed", [True, False])
def test_failed_run_error_and_activity_are_whitelisted(runtime_client, confirmed):
    client, _, project_id = runtime_client
    _heartbeat(project_id, code="no_work")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO task_runs (id, project_id, task_type, worker_name, worker_type, status, error_type, error_message, runtime_context_json) VALUES ('run', ?, 'explore', 'synthetic', 'pi', 'failed', 'provider_auth', 'PRIVATE-CANARY', ?)",
            (project_id, json.dumps({"execution_lifecycle": {"generation": 0, "process_started": confirmed, "termination_confirmed": confirmed}})),
        )
        conn.execute(
            "INSERT INTO agent_events (id, project_id, task_run_id, ts, kind, payload_json) VALUES ('event', ?, 'run', ?, 'runtime_context_updated', ?)",
            (project_id, utcnow(), json.dumps({"runtime_context": {
                "execution_lifecycle": {"generation": 0, "process_started": True, "termination_confirmed": True},
                "credential": "PRIVATE-CANARY",
            }})),
        )
    response = client.get(f"/projects/{project_id}/runtime")
    assert response.status_code == 200
    assert response.json()["last_error"]["code"] == "provider_auth"
    assert response.json()["last_error"]["task_run_id"] == "run"
    if confirmed:
        assert response.json()["last_activity"]["kind"] == "process_ended"
        assert response.json()["last_activity"]["task_run_id"] == "run"
    else:
        assert response.json()["last_activity"] is None
    assert "PRIVATE-CANARY" not in response.text
