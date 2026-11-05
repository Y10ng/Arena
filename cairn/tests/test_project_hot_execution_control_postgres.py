"""C1 Server 契约仅在一次性 PostgreSQL 验证，不执行模型、目标或真实研究。"""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4
import json
import os

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from cairn.server import db
from cairn.server.app import app
from cairn.server.auth import RequestPrincipal, create_machine_principal, create_user_with_personal_tenant
from cairn.server.models import ConcludeRequest, CreateArtifactRequest, CreateIntentRequest, HeartbeatRequest, UpdateTaskRunRequest
from cairn.server.schemas.candidates import CandidateFrameManifest, canonical_json_bytes, canonical_value_sha256
from cairn.server.schemas.execution_events import ExecutionEventEnvelope
from cairn.server.schemas.projects import ReauthorizeProjectRequest
from cairn.server.services import assessment_reports, project_authorization
from cairn.server.services.audit_artifacts import create_artifact_record, read_artifact_preview_record
from cairn.server.services.audit_task_runs import update_task_run_record
from cairn.server.services import verification_requests
from cairn.server.services.candidate_store import capture_candidate
from cairn.server.services.execution_events import append_execution_event
from cairn.server.routers import intents
from cairn.server.services import project_container_pause, project_execution_control as control, project_records, project_reason, projects
from cairn.server.services.intent_creation import create_intent_record
from cairn.server.services.project_authorization import initialize_project_authorization
from cairn.server.services.project_runtime_diagnostics import build_project_runtime
from cairn.server.services.time_utils import parse_utc_timestamp, utcnow
from test_postgres_bootstrap import empty_postgres_url, postgres_instance

pytestmark = pytest.mark.skipif(
    os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1", reason="仅使用一次性 PostgreSQL",
)


@pytest.fixture
def hot_db(empty_postgres_url, tmp_path, monkeypatch):
    db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")
    uid, tenant = create_user_with_personal_tenant("hot-control-owner", "local-test-password")
    now = utcnow()
    later = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(projects, "lease_clock", lambda: ("controlled-boot", 10**18))
    with db.get_conn() as conn:
        features = {
            "execution_contract_version": 1, "execution_engine": "pi",
            "assessment_contract_version": 2, "outcome_contract_version": 2, "verifier_contract_version": 1,
        }
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, tenant_id, scenario_key, assessment_protocol, "
            "scenario_snapshot_json, execution_generation, explore_deadline, authorization_expires_at) "
            "VALUES ('p', '受控热现场', 'active', ?, ?, 'goal-driven-v1', 'goal-driven-v1', ?, 7, ?, ?)",
            (now, tenant, json.dumps({"features": features}), deadline, later),
        )
        for fid in ("origin", "goal"):
            conn.execute("INSERT INTO facts (id, project_id, description, creator, created_at) VALUES (?, 'p', ?, 'system', ?)",
                         (fid, "仅授权本测试合成材料，不连接目标", now))
        initialize_project_authorization(conn, "p", actor_id=uid, authorized_at=now, origin_fact_id="origin")
        for owner in ("owner-a", "owner-b"):
            conn.execute("INSERT INTO dispatcher_status (id, status, updated_at, runtime_json) VALUES (?, 'idle', ?, ?)",
                         (owner, now, json.dumps({"runtime_capabilities": ["project_runtime_v1", "project_hot_pause_v1"]})))
    return RequestPrincipal("user", uid, tenant_id=tenant), later, deadline


def project():
    with db.get_conn() as conn:
        return conn.execute("SELECT * FROM projects WHERE id = 'p'").fetchone()


def run(run_id, task_type, owner, *, intent_id=None):
    worker = run_id + "-worker"
    context = {"dispatcher_id": owner, "execution_container_ids": ["controlled-" + owner], "execution_lifecycle": {
        "generation": 7, "process_started": True, "termination_confirmed": False, "cleanup_confirmed": False,
    }} if owner else {}
    with db.get_conn() as conn:
        if intent_id:
            conn.execute("INSERT INTO intents (id, project_id, description, creator, worker, created_at, last_heartbeat_at, "
                         "heartbeat_boot_id, heartbeat_boottime_ns) VALUES (?, 'p', '合成义务', 'system', ?, ?, ?, 'controlled-boot', 1)",
                         (intent_id, worker, utcnow(), utcnow()))
            conn.execute("INSERT INTO intent_sources (intent_id, project_id, fact_id, source_order) VALUES (?, 'p', 'origin', 0)", (intent_id,))
        conn.execute("INSERT INTO task_runs (id, project_id, intent_id, task_type, worker_name, worker_type, status, session_id, "
                     "started_at, runtime_context_json, execution_generation) VALUES (?, 'p', ?, ?, ?, 'pi', 'running', ?, ?, ?, 7)",
                     (run_id, intent_id, task_type, worker, "same-session-" + run_id, utcnow(), json.dumps(context)))
        if task_type == "reason":
            conn.execute("UPDATE projects SET reason_worker = ?, reason_trigger = 'controlled', reason_started_at = ?, "
                         "reason_last_heartbeat_at = ?, reason_heartbeat_boot_id = 'controlled-boot', "
                         "reason_heartbeat_boottime_ns = 1 WHERE id = 'p'", (worker, utcnow(), utcnow()))
    return worker


def confirm(request, owner="owner-a", state="paused", resources=None, error=None):
    if resources is None:
        resources = [{"kind": "research", "container_id": "controlled-" + owner, "state": state}] if state not in {"failed", "lost"} else []
    return control.confirm_execution_control("p", owner, request_id=request["request_id"], state=state,
                                             resources=resources, error=error)


def test_failed_readiness_activation_absence_requires_original_owner_token_daemon(hot_db):
    owner, token = create_machine_principal("controlled-readiness-owner")
    _, other_token = create_machine_principal("controlled-readiness-other")
    headers = {"Authorization": "Bearer " + token}
    client = TestClient(app)
    path = "/projects/p/container-activation"
    registration = client.post(path, headers=headers, json={"generation": 7, "docker_daemon_id": "controlled-daemon"})
    assert registration.status_code == 200
    first_token = registration.json()["activation_token"]
    abort = {"activation_token": first_token, "docker_daemon_id": "controlled-daemon"}
    before = project_container_pause.container_activation("p", owner)
    for identity in (None, ""):
        assert client.post(path + "/finish", headers=headers, json={"activation_token": first_token, "container_id": identity}).status_code == 422
    with pytest.raises(HTTPException) as missing_identity:
        project_container_pause.finish_container_activation("p", owner, first_token, None)
    assert missing_identity.value.detail == "container_identity_missing"
    assert client.post(path + "/abort", json=abort).status_code == 401
    assert client.post(path + "/abort", headers={"Authorization": "Bearer " + other_token}, json=abort).status_code == 409
    for changes, expected in (({"activation_token": "stale"}, 409), ({"docker_daemon_id": "other-daemon"}, 409), ({"docker_daemon_id": ""}, 422)):
        assert client.post(path + "/abort", headers=headers, json=abort | changes).status_code == expected
    assert project_container_pause.container_activation("p", owner) == before

    assert client.post(path + "/finish", headers=headers, json={"activation_token": first_token, "container_id": "controlled-existing"}).json() == {"state": "running"}
    assert client.post(path + "/abort", headers=headers, json=abort).status_code == 409
    registration = client.post(path, headers=headers, json={"generation": 7, "docker_daemon_id": "controlled-daemon"})
    assert registration.status_code == 200
    activation_token = registration.json()["activation_token"]
    assert activation_token != first_token
    assert client.post(path + "/abort", headers=headers, json=abort).status_code == 409
    abort["activation_token"] = activation_token
    # 历史未知库存和旧 running NULL 均不能借新接口直接宣称已停止。
    for state, identity in (("inventory", None), ("running", None), ("activating", "controlled-known")):
        with db.get_conn() as conn:
            conn.execute("UPDATE project_container_activations SET state = ?, container_id = ? WHERE project_id = 'p' AND dispatcher_id = ?", (state, identity, owner))
        assert client.post(path + "/abort", headers=headers, json=abort).status_code == 409
    with db.get_conn() as conn:
        conn.execute("UPDATE project_container_activations SET state = 'activating', container_id = NULL WHERE project_id = 'p' AND dispatcher_id = ?", (owner,))
    request = project_records.project_lifecycle_action("p", "finish").execution_control
    assert project()["execution_generation"] == 8 and request["dispatcher_ids"] == [owner]
    receipt = {"request_id": request["request_id"], "state": "stopped", "resources": []}
    assert client.post("/projects/p/execution-control/confirm", headers=headers, json=receipt).status_code == 409
    assert client.post(path + "/abort", headers=headers, json=abort).json() == {"state": "stopped"}
    settled = project_container_pause.container_activation("p", owner)
    assert settled["container_id"] is None and settled["state"] == "stopped"
    assert settled["activation_generation"] == 7 and settled["activation_token"] == activation_token
    assert client.post(path + "/abort", headers=headers, json=abort).json() == {"state": "stopped"}
    assert project_container_pause.container_activation("p", owner) == settled
    response = client.post("/projects/p/execution-control/confirm", headers=headers, json=receipt)
    assert response.status_code == 200 and response.json()["state"] == "stopped"


def test_paused_admitted_test_egress_keeps_owner_generation_and_admission_fences(hot_db):
    owner, token = create_machine_principal("controlled-egress-owner")
    _, other_token = create_machine_principal("controlled-egress-other")
    headers = {"Authorization": "Bearer " + token}
    client = TestClient(app)
    run("queued", "bootstrap", owner)
    run("running", "explore", owner, intent_id="egress-intent")
    with db.get_conn() as conn:
        conn.execute("UPDATE task_runs SET status = 'queued' WHERE project_id = 'p' AND id = 'queued'")
    path = "/projects/p/task-runs/{}/test-egress"
    baseline = client.post(path.format("running"), headers=headers)
    assert baseline.status_code == 200 and baseline.json()["mode"] == "direct"
    project_records.project_lifecycle_action("p", "pause")
    for run_id in ("queued", "running"):
        response = client.post(path.format(run_id), headers=headers)
        assert response.status_code == 200 and response.json() == baseline.json()
        assert response.headers["Cache-Control"] == "no-store"
    assert client.post(path.format("running")).status_code == 401
    assert client.post(path.format("running"), headers={"Authorization": "Bearer " + other_token}).status_code == 403
    assert client.post(path.format("missing"), headers=headers).status_code == 404
    with db.get_conn() as conn:
        with pytest.raises(HTTPException) as admission:
            projects.check_project_active(conn, "p")
        assert admission.value.status_code == 403
    assert client.post("/projects/p/container-activation", headers=headers, json={"generation": 7, "docker_daemon_id": "controlled-daemon"}).status_code == 409
    for status, generation, cancelled in (("succeeded", 7, 0), ("running", 6, 0), ("running", 7, 1)):
        with db.get_conn() as conn:
            conn.execute("UPDATE task_runs SET status = ?, execution_generation = ?, pause_requested = ? WHERE project_id = 'p' AND id = 'running'", (status, generation, cancelled))
        response = client.post(path.format("running"), headers=headers)
        assert response.status_code == 409 and response.json()["detail"] == "test_egress_task_run_not_current"
    with db.get_conn() as conn:
        conn.execute("UPDATE task_runs SET status = 'running', execution_generation = 7, pause_requested = 0 WHERE project_id = 'p' AND id = 'running'")
        assert conn.execute("SELECT COUNT(*) AS n FROM task_runs WHERE project_id = 'p'").fetchone()["n"] == 2
    assert project()["execution_generation"] == 7
    project_records.project_lifecycle_action("p", "finish")
    for run_id in ("queued", "running"):
        assert client.post(path.format(run_id), headers=headers).status_code == 409


def test_hot_pause_resume_preserves_run_claim_and_budget(hot_db, monkeypatch):
    _, authorization, original_deadline = hot_db
    reason = run("reason", "reason", "owner-a")
    explore = run("explore", "explore", "owner-b", intent_id="i")
    run("report", "report", "report-owner")
    run("chat", "agent_chat", "chat-owner")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    clock = [now]

    class ControlledDateTime:
        @staticmethod
        def now(tz):
            return clock[0]

    monkeypatch.setattr(control, "datetime", ControlledDateTime)
    monkeypatch.setattr(control, "utcnow", lambda: clock[0].strftime("%Y-%m-%dT%H:%M:%SZ"))
    paused = project_records.project_lifecycle_action("p", "pause")
    request = paused.execution_control
    assert paused.execution_generation == 7 and request["dispatcher_ids"] == ["owner-a", "owner-b"]
    summary = project_records.list_project_summaries()[0]
    assert summary.execution_control == request and summary.explore_deadline == paused.explore_deadline
    with db.get_conn() as conn:
        assert conn.execute("SELECT reason_worker FROM projects WHERE id='p'").fetchone()["reason_worker"] == reason
        assert conn.execute("SELECT worker FROM intents WHERE id='i' AND project_id='p'").fetchone()["worker"] == explore
        for row in conn.execute("SELECT * FROM task_runs WHERE project_id='p'"):
            assert row["status"] == "running" and row["pause_requested"] == 0
            assert row["session_id"] == "same-session-" + row["id"]
            assert not json.loads(row["runtime_context_json"]).get("pause_interrupted")
        projects.expire_workers(conn, "p")
        assert not projects.expire_reason_leases(conn, "p")
    assert confirm(request)["state"] == "requested"
    receipt = confirm(request, "owner-b", resources=[{"kind": "research", "container_id": "controlled-owner-b", "task_run_id": "explore", "state": "paused"}])
    assert receipt["state"] == "paused"
    with db.get_conn() as conn:
        view = build_project_runtime(conn, project(), schema_version=2)
        assert view.state == "paused" and view.axes.process.state == "paused"
        assert view.axes.resources.state == "retained"
    clock[0] += timedelta(minutes=10)
    summary = project_records.list_project_summaries()[0]
    assert summary.execution_control["state"] == "paused"
    assert parse_utc_timestamp(summary.explore_deadline) - parse_utc_timestamp(original_deadline) == timedelta(minutes=10)
    resumed = project_records.project_lifecycle_action("p", "resume")
    assert resumed.execution_generation == 7 and resumed.execution_control["state"] == "requested"
    assert project_records.list_project_summaries()[0].execution_control == resumed.execution_control
    with db.get_conn() as conn:
        projects.expire_workers(conn, "p")
        assert not projects.expire_reason_leases(conn, "p")
        with pytest.raises(HTTPException) as blocked:
            projects.check_project_active(conn, "p")
        assert blocked.value.detail == "execution_control_pending"
    assert confirm(resumed.execution_control, state="running")["state"] == "requested"
    assert confirm(resumed.execution_control, "owner-b", state="running")["state"] == "running"
    row = project()
    assert row["execution_generation"] == 7 and row["authorization_expires_at"] == authorization
    assert parse_utc_timestamp(row["explore_deadline"]) - parse_utc_timestamp(original_deadline) == timedelta(minutes=10)
    assert control.execution_control(row)["paused_seconds"] == 600
    with db.get_conn() as conn:
        assert projects.check_project_active(conn, "p")["status"] == "active"
        assert not projects.expire_reason_leases(conn, "p")
        assert conn.execute("SELECT heartbeat_boottime_ns FROM intents WHERE id='i' AND project_id='p'").fetchone()["heartbeat_boottime_ns"] == 10**18


@pytest.mark.parametrize("failure", ["failed", "lost"])
def test_active_resume_retry_uses_post_and_keeps_original_scene(hot_db, monkeypatch, failure):
    from cairn.server.auth import require_user_tenant_project

    monkeypatch.setitem(app.dependency_overrides, require_user_tenant_project, lambda: hot_db[0])
    client = TestClient(app)
    run("bootstrap", "bootstrap", "owner-a", intent_id="i")
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM task_runs WHERE project_id='p' AND id='bootstrap'").fetchone()
        context = json.loads(row["runtime_context_json"])
        context["execution_lifecycle"]["termination_confirmed"] = True
        conn.execute("UPDATE task_runs SET status='succeeded', ended_at=?, runtime_context_json=? WHERE project_id='p' AND id='bootstrap'",
                     (utcnow(), json.dumps(context)))
    paused = project_records.project_lifecycle_action("p", "pause").execution_control
    pause_receipt = confirm(paused)
    first = client.post("/projects/p/resume")
    assert first.status_code == 200
    request = first.json()["execution_control"]
    failed = confirm(request, state=failure)
    assert failed["resources"] == pause_receipt["resources"]
    assert failed["resources_request_id"] == paused["request_id"]
    assert failed["resources_observed_at"] == pause_receipt["confirmed_at"]
    assert failed["receipts"]["owner-a"]["resources"] == []
    with db.get_conn() as conn:
        view = build_project_runtime(conn, project(), schema_version=2)
        assert view.state == "blocked" and view.actions.resume.allowed
        assert view.recovery.route == "resume"
        with pytest.raises(HTTPException, match="execution_control_pending"):
            projects.check_project_active(conn, "p")
    retry = client.post("/projects/p/resume")
    assert retry.status_code == 200
    new = retry.json()["execution_control"]
    assert new["request_id"] != request["request_id"]
    assert new["state"] == "requested" and new["confirmed_at"] is None
    assert new["dispatcher_ids"] == ["owner-a"]
    assert new["resources"] == pause_receipt["resources"]
    assert new["resources_request_id"] == paused["request_id"]
    assert retry.json()["execution_generation"] == 7
    assert client.post("/projects/p/resume").json()["execution_control"]["request_id"] == new["request_id"]
    with db.get_conn() as conn:
        view = build_project_runtime(conn, project(), schema_version=2)
        assert not view.actions.resume.allowed and view.recovery.route == "wait"
        assert view.code == "project_resuming"
        with pytest.raises(HTTPException, match="execution_control_pending"):
            projects.check_project_active(conn, "p")
    with pytest.raises(HTTPException, match="execution_control_unconfirmed"):
        confirm(new, state="running", resources=[])
    confirmed = confirm(new, state="running")
    assert confirmed["state"] == "running" and confirmed["resources_request_id"] == new["request_id"]
    assert client.post("/projects/p/resume").json()["execution_control"]["request_id"] == new["request_id"]
    with db.get_conn() as conn:
        assert projects.check_project_active(conn, "p")["status"] == "active"
        rows = conn.execute("SELECT status, session_id, execution_generation, runtime_context_json FROM task_runs WHERE project_id='p'").fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "succeeded" and rows[0]["session_id"] == "same-session-bootstrap"
        assert rows[0]["execution_generation"] == 7
        assert json.loads(rows[0]["runtime_context_json"]) == context


def test_active_resume_retry_rechecks_authorization_contract_and_owner(hot_db, monkeypatch):
    from cairn.server.auth import require_user_tenant_project

    monkeypatch.setitem(app.dependency_overrides, require_user_tenant_project, lambda: hot_db[0])
    client = TestClient(app)
    run("reason", "reason", "owner-a")
    pause = project_records.project_lifecycle_action("p", "pause").execution_control
    confirm(pause)
    request = project_records.project_lifecycle_action("p", "resume").execution_control
    confirm(request, state="lost")
    with db.get_conn() as conn:
        original = dict(project())
        owner_runtime = conn.execute("SELECT runtime_json FROM dispatcher_status WHERE id='owner-a'").fetchone()["runtime_json"]
    cases = (
        ("UPDATE projects SET authorization_expires_at='2000-01-01T00:00:00Z' WHERE id='p'", (), "project_reauthorization_required"),
        ("UPDATE projects SET scenario_snapshot_json='{}' WHERE id='p'", (), "execution_contract_unsupported"),
        ("UPDATE dispatcher_status SET runtime_json='{}' WHERE id='owner-a'", (), "project_pause_unsupported"),
        ("UPDATE task_runs SET execution_generation=6 WHERE project_id='p'", (), "execution_scene_generation_mismatch"),
    )
    for sql, values, reason in cases:
        with db.get_conn() as conn:
            conn.execute(sql, values)
        response = client.post("/projects/p/resume")
        assert response.status_code == 409 and response.json()["detail"] == reason
        assert control.execution_control(project())["request_id"] == request["request_id"]
        with db.get_conn() as conn:
            view = build_project_runtime(conn, project(), schema_version=2)
            assert not view.actions.resume.allowed and view.recovery.route != "resume"
            conn.execute("UPDATE projects SET authorization_expires_at=?, scenario_snapshot_json=? WHERE id='p'",
                         (original["authorization_expires_at"], original["scenario_snapshot_json"]))
            conn.execute("UPDATE dispatcher_status SET runtime_json=? WHERE id='owner-a'", (owner_runtime,))
            conn.execute("UPDATE task_runs SET execution_generation=7 WHERE project_id='p'")


def test_paused_inflight_heartbeat_results_and_release_not_new_admission(hot_db):
    reason = run("reason", "reason", "owner-a")
    bootstrap = run("bootstrap", "bootstrap", "owner-a", intent_id="i")
    request = project_records.project_lifecycle_action("p", "pause").execution_control
    confirm(request, resources=[{"kind": "research", "container_id": "controlled-owner-a", "task_run_id": "reason", "state": "paused"}])
    assert project_reason.heartbeat_project_reason_record("p", HeartbeatRequest(worker=reason, task_run_id="reason")).reason.worker == reason
    assert intents.heartbeat("p", "i", HeartbeatRequest(worker=bootstrap, task_run_id="bootstrap")).worker == bootstrap
    body = CreateIntentRequest(from_=["origin"], description="暂停前在途的 Reason 产出", creator=reason, task_run_id="reason", submission_index=0)
    with db.get_conn() as conn:
        accepted = create_intent_record(conn, "p", body, RequestPrincipal("machine", "owner-a"))
        assert accepted.task_run_id == "reason" and accepted.worker is None
    result = ConcludeRequest(worker=bootstrap, task_run_id="bootstrap", description="暂停前在途结论")
    concluded = intents.conclude("p", "i", result)
    assert concluded.intent.to == concluded.fact.id
    assert project()["status"] == "paused"
    for heartbeat in (HeartbeatRequest(worker=reason), HeartbeatRequest(worker=reason, task_run_id="missing")):
        with pytest.raises(HTTPException):
            project_reason.heartbeat_project_reason_record("p", heartbeat)
    assert project_reason.release_project_reason_record("p", HeartbeatRequest(worker=reason, task_run_id="reason")).reason is None
    with db.get_conn() as conn:
        with pytest.raises(HTTPException):
            projects.check_project_active(conn, "p")
        assert conn.execute("SELECT description FROM facts WHERE project_id='p' AND id=?", (concluded.fact.id,)).fetchone()["description"] == "暂停前在途结论"
    project_records.project_lifecycle_action("p", "finish")
    assert intents.conclude("p", "i", result).fact.id == concluded.fact.id
    with db.get_conn() as conn:
        assert create_intent_record(conn, "p", body, RequestPrincipal("machine", "owner-a")).id == accepted.id
        with pytest.raises(HTTPException):
            projects.validate_execution_generation(conn, "p", "reason")


def test_finish_fences_only_research_and_closes_claims_after_all_receipts(hot_db, monkeypatch):
    clock = [datetime.now(timezone.utc).replace(microsecond=0)]
    monkeypatch.setattr(control, "utcnow", lambda: clock[0].strftime("%Y-%m-%dT%H:%M:%SZ"))
    run("reason", "reason", "owner-a")
    run("explore", "explore", "owner-b", intent_id="i")
    run("report", "report", "report-owner")
    run("chat", "agent_chat", "chat-owner")
    pause = project_records.project_lifecycle_action("p", "pause").execution_control
    confirm(pause)
    confirm(pause, "owner-b")
    clock[0] += timedelta(minutes=1)
    finished = project_records.project_lifecycle_action("p", "finish")
    assert finished.status == "stopped" and finished.execution_generation == 8
    request = finished.execution_control
    assert request["dispatcher_ids"] == ["owner-a", "owner-b"]
    with db.get_conn() as conn:
        assert conn.execute("SELECT reason_worker FROM projects WHERE id='p'").fetchone()["reason_worker"]
        for row in conn.execute("SELECT task_type, pause_requested FROM task_runs WHERE project_id='p'"):
            assert bool(row["pause_requested"]) == (row["task_type"] in {"reason", "explore"})
        assert conn.execute("SELECT COUNT(*) AS n FROM assessment_report_finalizations WHERE project_id='p'").fetchone()["n"] == 0
    confirm(request, state="stopped")
    assert project()["reason_worker"]
    stopped = confirm(request, "owner-b", state="stopped")
    assert stopped["state"] == "stopped" and stopped["paused_at"] is None
    assert stopped["paused_seconds"] == 60
    row = project()
    assert parse_utc_timestamp(row["explore_deadline"]) - parse_utc_timestamp(hot_db[2]) == timedelta(minutes=1)
    assert control.effective_explore_deadline(row, row["explore_deadline"]) == row["explore_deadline"]
    with db.get_conn() as conn:
        row = conn.execute("SELECT worker, conclusion_reason FROM intents WHERE id='i' AND project_id='p'").fetchone()
        assert row["worker"] is None and row["conclusion_reason"] == "user_stopped"
        assert project()["reason_worker"] is None
        assert conn.execute("SELECT COUNT(*) AS n FROM facts WHERE project_id='p'").fetchone()["n"] == 2
        assert build_project_runtime(conn, project()).state == "stopped"
    assert project_records.project_lifecycle_action("p", "finish").execution_generation == 8
    with pytest.raises(HTTPException):
        project_records.project_lifecycle_action("p", "resume")


def test_confirm_auth_owner_stale_resource_and_secret_filter(hot_db):
    owner, token = create_machine_principal("controlled-hot-owner")
    run("reason", "reason", owner)
    run("report", "report", owner)
    request = project_records.project_lifecycle_action("p", "pause").execution_control
    headers = {"Authorization": "Bearer " + token}
    client = TestClient(app)
    path = "/projects/p/execution-control/confirm"
    body = {"request_id": request["request_id"], "state": "paused", "resources": []}
    assert client.post(path, json=body).status_code == 401
    other, other_token = create_machine_principal("controlled-other-owner")
    assert other != owner
    assert client.post(path, json=body, headers={"Authorization": "Bearer " + other_token}).status_code == 403
    assert client.post(path, json={**body, "request_id": "stale"}, headers=headers).status_code == 409
    assert client.post(path, json={**body, "resources": [{"private_token": "PRIVATE-CANARY"}]}, headers=headers).status_code == 422
    assert client.post(path, json={**body, "resources": [{"task_run_id": "report"}]}, headers=headers).status_code == 422
    assert client.post(path, json=body, headers=headers).status_code == 409
    resource = {"kind": "research", "container_id": "controlled-" + owner, "state": "paused"}
    for invalid in (
        {"kind": "research", "state": "paused"},
        {"container_id": resource["container_id"], "state": "paused"},
        {**resource, "container_id": "another-owners-container"},
        {**resource, "state": "running"},
    ):
        assert client.post(path, json={**body, "resources": [invalid]}, headers=headers).status_code == 422
    failed = client.post(path, json={**body, "state": "failed", "error": "PRIVATE-CANARY"}, headers=headers)
    assert failed.status_code == 200 and "PRIVATE-CANARY" not in failed.text
    assert failed.json()["error"] == "execution_control_failed"
    body["resources"] = [resource]
    accepted = client.post(path, json=body, headers=headers)
    assert accepted.status_code == 200 and accepted.json()["state"] == "paused"
    assert client.post(path, json=body, headers=headers).json() == accepted.json()
    assert client.post(path, json={**body, "resources": [{"kind": "changed"}]}, headers=headers).status_code == 409
    with pytest.raises(HTTPException) as legacy:
        project_container_pause.claim_container_pause("p", owner, 7, "controlled-daemon")
    assert legacy.value.detail == "project_hot_control_required"


def test_confirmation_requires_all_owned_containers_and_research_test_scopes(hot_db):
    run("reason", "reason", "owner-a")
    run("explore", "explore", "owner-b", intent_id="i")
    run("external", "external_verify", "owner-a")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO project_container_activations (project_id, dispatcher_id, activation_generation, activation_token, "
            "container_id, docker_daemon_id, state, updated_at) VALUES ('p', 'owner-a', 7, 'activation', 'readiness-container', 'daemon', 'running', ?)",
            (utcnow(),),
        )
        for scope_id, task_id, owner in (("scope-a", "reason", "owner-a"), ("scope-b", "explore", "owner-b"), ("external-scope", "external", "owner-a")):
            conn.execute(
                "INSERT INTO http_capture_scopes (id, project_id, task_run_id, generation, container_id, dispatcher_id, created_at) "
                "VALUES (?, 'p', ?, 7, ?, ?, ?)", (scope_id, task_id, "test-" + scope_id, owner, utcnow()),
            )
    request = project_records.project_lifecycle_action("p", "pause").execution_control
    assert request["dispatcher_ids"] == ["owner-a", "owner-b"]
    with pytest.raises(HTTPException) as missing:
        confirm(request)
    assert missing.value.detail == "execution_control_unconfirmed"
    research = [{"kind": "research", "container_id": identity, "state": "paused"}
                for identity in ("controlled-owner-a", "readiness-container")]
    with pytest.raises(HTTPException) as missing:
        confirm(request, resources=research)
    assert missing.value.detail == "execution_control_unconfirmed"
    scope = {"kind": "test", "container_id": "test-scope-a", "scope_id": "scope-a", "task_run_id": "reason", "state": "paused"}
    with pytest.raises(HTTPException) as mismatch:
        confirm(request, resources=[*research, {**scope, "scope_id": "scope-b", "task_run_id": "explore", "container_id": "test-scope-b"}])
    assert mismatch.value.detail == "execution_control_resource_invalid"
    assert confirm(request, resources=[*research, scope])["state"] == "requested"
    with db.get_conn() as conn:
        conn.execute("UPDATE http_capture_scopes SET status_json = ? WHERE project_id='p' AND id='scope-b'", (json.dumps({"test_stopped": True}),))
    assert confirm(request, "owner-b")["state"] == "paused"
    finished = project_records.project_lifecycle_action("p", "finish").execution_control
    confirm(finished, state="stopped", resources=[{**resource, "state": "stopped"} for resource in [*research, scope]])
    assert confirm(finished, "owner-b", state="stopped")["state"] == "stopped"
    with db.get_conn() as conn:
        assert conn.execute("SELECT state FROM project_container_activations WHERE project_id='p' AND dispatcher_id='owner-a'").fetchone()["state"] == "stopped"


def test_unknown_owner_never_becomes_confirmed_after_failed_retry(hot_db):
    run("reason", "reason", "owner-a")
    run("unknown", "explore", None)
    request = project_records.project_lifecycle_action("p", "pause").execution_control
    assert request["owner_unknown"]
    confirm(request, state="failed")
    result = confirm(request)
    assert result["owner_unknown"] and result["state"] == "requested"
    assert result["error"] == "project_execution_owner_unknown"
    with pytest.raises(HTTPException):
        project_records.project_lifecycle_action("p", "resume")


def test_no_execution_pause_is_dispatch_only_and_lost_is_not_rebuilt(hot_db):
    paused = project_records.project_lifecycle_action("p", "pause")
    assert paused.execution_control["state"] == "paused"
    assert paused.execution_control["resources"] == [] and paused.execution_control["dispatcher_ids"] == []
    assert project_records.project_lifecycle_action("p", "resume").execution_control["state"] == "running"
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM task_runs WHERE project_id='p'").fetchone()["n"] == 0
    # 初始 paused 且从未执行的项目也只恢复派发，不要求伪造热现场。
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET status='paused', execution_control_json='{}' WHERE id='p'")
    assert project_records.project_lifecycle_action("p", "resume").execution_control["state"] == "running"
    run("reason", "reason", "owner-a")
    request = project_records.project_lifecycle_action("p", "pause").execution_control
    result = confirm(request, state="lost", error="raw private detail")
    assert result["error"] == "execution_scene_lost"
    with pytest.raises(HTTPException) as rejected:
        confirm(request)
    assert rejected.value.detail == "execution_scene_lost"
    with pytest.raises(HTTPException) as rejected:
        project_records.project_lifecycle_action("p", "resume")
    assert rejected.value.detail == "execution_scene_lost"


def test_external_verification_cleanup_stopped_retains_truth_and_requires_proof(hot_db):
    run("verify", "external_verify", "owner-a")
    run("legacy-verify", "external_verify", "owner-a")
    principal = RequestPrincipal("machine", "owner-a")
    cleanup = {"external_verification_cleanup": {"status": "stopped", "error": None}}
    with pytest.raises(HTTPException) as unconfirmed:
        update_task_run_record("p", "verify", UpdateTaskRunRequest(runtime_context=cleanup), principal=principal)
    assert unconfirmed.value.detail == "verification_cleanup_requires_confirmation"
    proof = {"generation": 7, "process_started": True, "termination_confirmed": True, "cleanup_confirmed": True}
    update_task_run_record("p", "verify", UpdateTaskRunRequest(
        status="succeeded", runtime_context={**cleanup, "execution_lifecycle": proof}), principal=principal)
    update_task_run_record("p", "legacy-verify", UpdateTaskRunRequest(
        status="succeeded", runtime_context={"external_verification_cleanup": {"status": "removed"}, "execution_lifecycle": proof}), principal=principal)
    with db.get_conn() as conn:
        retained = conn.execute("SELECT * FROM task_runs WHERE project_id='p' AND id='verify'").fetchone()
        removed = conn.execute("SELECT * FROM task_runs WHERE project_id='p' AND id='legacy-verify'").fetchone()
        assert verification_requests._cleanup([retained]) == {"status": "stopped", "error": None}
        assert verification_requests._cleanup([removed]) == {"status": "removed", "error": None}
        assert verification_requests._cleanup([retained, removed])["status"] == "stopped"
        assert verification_requests._delete_block_reason({"terminal_kind": "result"}, [retained], project()) is None
    for run_id, status in (("verify", "pending"), ("legacy-verify", "stopped")):
        with pytest.raises(HTTPException) as conflict:
            update_task_run_record("p", run_id, UpdateTaskRunRequest(
                runtime_context={"external_verification_cleanup": {"status": status}}), principal=principal)
        assert conflict.value.detail == "verification_cleanup_confirmation_conflict"
    with db.get_conn() as conn:
        assert json.loads(conn.execute("SELECT runtime_context_json FROM task_runs WHERE project_id='p' AND id='legacy-verify'").fetchone()["runtime_context_json"])["external_verification_cleanup"]["status"] == "removed"


def test_raw_artifact_candidate_and_checkpoint_survive_pause_and_finish(hot_db, monkeypatch):
    monkeypatch.setenv("CAIRN_EXECUTION_EVENTS_ENABLED", "1")
    run("explore", "explore", "owner-a", intent_id="i")
    session = "pi:" + str(uuid4())
    delivery = {
        "session_id": session, "phase": "explore_execute", "deadline_at": hot_db[2],
        "container_id": "controlled-container", "model_binding_sha256": "a" * 64,
        "result_schema_sha256": "b" * 64, "binding": None, "inputs": {},
    }
    with db.get_conn() as conn:
        context = json.loads(conn.execute("SELECT runtime_context_json FROM task_runs WHERE id='explore' AND project_id='p'").fetchone()["runtime_context_json"])
        context["execution_delivery"] = delivery
        conn.execute("UPDATE task_runs SET session_id=?, runtime_context_json=? WHERE id='explore' AND project_id='p'",
                     (session, json.dumps(context)))
    principal = RequestPrincipal("machine", "owner-a")
    saved = []
    for index, action in enumerate(("pause", "finish"), 1):
        project_records.project_lifecycle_action("p", action)
        artifact = create_artifact_record("p", CreateArtifactRequest(
            task_run_id="explore", kind="raw_output", content="在途原始材料 " + action, filename=action + ".txt"))
        saved.append(artifact)
        raw = ("原始候选 " + action).encode()
        capture_id = str(uuid4())
        value = {
            "protocol": "cairn-candidate-frame-v1", "record_kind": "candidate", "capture_id": capture_id,
            "producer_candidate_key": f"{capture_id}:1:0", "parent_producer_candidate_key": None,
            "assistant_turn": 1, "content_index": 0, "project_id": "p", "task_run_id": "explore",
            "phase": "explore_execute", "session": session, "binding_sha256": canonical_value_sha256(None),
            "result_schema_sha256": "b" * 64, "raw_sha256": sha256(raw).hexdigest(), "raw_size_bytes": len(raw), "artifacts": [],
        }
        receipt = capture_candidate("p", "explore", CandidateFrameManifest.model_validate(value),
                                    canonical_json_bytes(value), raw, principal=principal)
        disposition = "current_at_receipt" if action == "pause" else "historical_at_receipt"
        assert receipt.generation_disposition == disposition
        event = ExecutionEventEnvelope.model_validate({
            "protocol": "cairn-execution-event-v1", "producer_event_key": f"controlled-{index}", "kind": "checkpoint",
            "phase": "explore_execute", "recorded_at": utcnow(),
            "checkpoint": {"category": "observation", "summary": "在途检查点", "details": {"step": index}},
        })
        assert append_execution_event("p", "explore", event, principal=principal).generation_disposition == disposition
    for artifact, action in zip(saved, ("pause", "finish")):
        assert read_artifact_preview_record("p", artifact.id, tail=False).content == "在途原始材料 " + action
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM task_run_candidates WHERE project_id='p'").fetchone()["n"] == 2
        assert conn.execute("SELECT COUNT(*) AS n FROM execution_events WHERE project_id='p'").fetchone()["n"] == 2
        with pytest.raises(HTTPException):
            projects.validate_execution_generation(conn, "p", "explore")


@pytest.fixture
def running_report(hot_db):
    # 走真实受理和 claim，不仅插入一个 report 类型的 Run；不启动 Worker 或模型。
    with db.get_conn() as conn:
        snapshot = json.loads(project()["scenario_snapshot_json"])
        snapshot.update(key="goal-driven-v1", prompt_group="goal-driven-v1", profile_id="default")
        conn.execute("UPDATE projects SET scenario_snapshot_json = ? WHERE id = 'p'", (json.dumps(snapshot),))
        requested = assessment_reports.request_assessment_report_generation(
            conn, "p", template_key="standard-pentest", requested_by=hot_db[0].subject_id,
        )
    claim = assessment_reports.claim_assessment_report_finalization(
        "p", "report-worker", generation_token=requested.generation_token,
        principal=RequestPrincipal("machine", "report-owner"),
    )
    with db.get_conn() as conn:
        finalization = dict(conn.execute("SELECT * FROM assessment_report_finalizations WHERE project_id = 'p'").fetchone())
        task_run = dict(conn.execute("SELECT * FROM task_runs WHERE project_id = 'p' AND id = ?", (claim.task_run_id,)).fetchone())
    assert finalization["status"] == task_run["status"] == "running"
    assert finalization["generation_token"] == requested.generation_token
    assert task_run["execution_generation"] == 7 and task_run["pause_requested"] == 0
    yield claim
    # 项目动作不得取消、重派或改写正在运行的独立报告及其冻结配置。
    with db.get_conn() as conn:
        assert dict(conn.execute("SELECT * FROM assessment_report_finalizations WHERE project_id = 'p'").fetchone()) == finalization
        assert dict(conn.execute("SELECT * FROM task_runs WHERE project_id = 'p' AND id = ?", (claim.task_run_id,)).fetchone()) == task_run
        assert conn.execute("SELECT COUNT(*) AS n FROM task_runs WHERE project_id = 'p' AND task_type = 'report'").fetchone()["n"] == 1


def test_running_report_allows_hot_resume_without_bypassing_authorization(hot_db, running_report):
    run("research", "explore", "owner-a", intent_id="research-intent")
    pause = project_records.project_lifecycle_action("p", "pause").execution_control
    assert pause["dispatcher_ids"] == ["owner-a"]
    assert confirm(pause)["state"] == "paused"
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET authorization_expires_at = '2000-01-01T00:00:00Z' WHERE id = 'p'")
        before = dict(conn.execute("SELECT * FROM projects WHERE id = 'p'").fetchone())
        assert project_records.project_lifecycle_actions(conn, before)["resume"] == {
            "allowed": False, "reason": "project_reauthorization_required",
        }
    with pytest.raises(HTTPException) as expired:
        project_records.project_lifecycle_action("p", "resume")
    assert expired.value.status_code == 409 and expired.value.detail == "project_reauthorization_required"
    assert dict(project()) == before
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET authorization_expires_at = ? WHERE id = 'p'", (hot_db[1],))
        restored = conn.execute("SELECT * FROM projects WHERE id = 'p'").fetchone()
        assert project_records.project_lifecycle_actions(conn, restored)["resume"] == {"allowed": True, "reason": None}
    resumed = project_records.project_lifecycle_action("p", "resume")
    assert resumed.status == "active" and resumed.execution_generation == 7
    assert resumed.execution_control["dispatcher_ids"] == ["owner-a"]
    assert resumed.execution_control["state"] == "requested"
    assert confirm(resumed.execution_control, state="running")["state"] == "running"
    with db.get_conn() as conn:
        assert projects.check_project_active(conn, "p")["status"] == "active"
        research = conn.execute("SELECT * FROM task_runs WHERE project_id = 'p' AND id = 'research'").fetchone()
        assert research["status"] == "running" and research["pause_requested"] == 0
        assert research["session_id"] == "same-session-research" and research["execution_generation"] == 7
        assert conn.execute("SELECT worker FROM intents WHERE project_id = 'p' AND id = 'research-intent'").fetchone()["worker"] == "research-worker"
        assert conn.execute("SELECT COUNT(*) AS n FROM project_authorization_grants_v2 WHERE project_id = 'p'").fetchone()["n"] == 1
    assert project()["authorization_epoch"] == before["authorization_epoch"]
    assert project()["authorization_expires_at"] == hot_db[1]


def test_running_report_allows_reauthorization_without_resuming_research(hot_db, running_report):
    assert project_records.project_lifecycle_action("p", "pause").execution_control["state"] == "paused"
    before = dict(project())
    with db.get_conn() as conn:
        digest = project_authorization.authorization_scope_projection(conn, before).scope_digest
    request = ReauthorizeProjectRequest(
        confirmed=True, expected_authorization_epoch=before["authorization_epoch"],
        expected_scope_digest=digest, authorization_expires_at=hot_db[1],
    )
    with pytest.raises(HTTPException) as current:
        project_authorization.reauthorize_project_record("p", request, actor_id=hot_db[0].subject_id)
    assert current.value.status_code == 409 and current.value.detail == "project_authorization_current"
    assert dict(project()) == before
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET authorization_expires_at = '2000-01-01T00:00:00Z' WHERE id = 'p'")
        before = dict(conn.execute("SELECT * FROM projects WHERE id = 'p'").fetchone())
        assert project_records.project_lifecycle_actions(conn, before)["reauthorize"] == {"allowed": True, "reason": None}
    for changes, reason in (
        ({"expected_authorization_epoch": before["authorization_epoch"] - 1}, "authorization_epoch_mismatch"),
        ({"expected_scope_digest": "stale-scope"}, "authorization_scope_mismatch"),
    ):
        with pytest.raises(HTTPException) as stale:
            project_authorization.reauthorize_project_record(
                "p", request.model_copy(update=changes), actor_id=hot_db[0].subject_id,
            )
        assert stale.value.status_code == 409 and stale.value.detail == reason
        assert dict(project()) == before
    result = project_authorization.reauthorize_project_record("p", request, actor_id=hot_db[0].subject_id)
    assert result.status == "paused" and result.execution_generation == 8
    assert result.authorization_epoch == before["authorization_epoch"] + 1
    assert result.authorization_expires_at == hot_db[1]
    assert project()["execution_control_json"] == before["execution_control_json"]
    with db.get_conn() as conn:
        grants = conn.execute("SELECT * FROM project_authorization_grants_v2 WHERE project_id = 'p' ORDER BY authorization_epoch").fetchall()
        assert len(grants) == 2
        assert grants[1]["source"] == "explicit_reauthorization"
        assert grants[1]["actor_id"] == hot_db[0].subject_id and grants[1]["scope_digest"] == digest
        assert grants[1]["execution_generation"] == 8
        assert project_authorization.project_authorization_is_current(conn, project())
        assert project_records.project_lifecycle_actions(conn, project())["resume"] == {"allowed": True, "reason": None}
        assert conn.execute("SELECT COUNT(*) AS n FROM task_runs WHERE project_id = 'p'").fetchone()["n"] == 1


@pytest.mark.parametrize("status", ["stopped", "completed"])
def test_running_report_does_not_allow_terminal_resume_or_reauthorization(hot_db, running_report, status):
    if status == "stopped":
        assert project_records.project_lifecycle_action("p", "finish").status == "stopped"
    with db.get_conn() as conn:
        # completed 使用合成终态，不为生命周期拒绝测试运行自主研究或完成判定链。
        conn.execute("UPDATE projects SET status = ?, authorization_expires_at = '2000-01-01T00:00:00Z' WHERE id = 'p'", (status,))
        before = dict(conn.execute("SELECT * FROM projects WHERE id = 'p'").fetchone())
        actions = project_records.project_lifecycle_actions(conn, before)
        lifecycle_reason = "assessment_new_project_required" if status == "stopped" else "Completed projects cannot change status"
        for action in ("resume", "reauthorize"):
            assert actions[action] == {"allowed": False, "reason": lifecycle_reason}
        digest = project_authorization.authorization_scope_projection(conn, before).scope_digest
    with pytest.raises(HTTPException) as resume:
        project_records.project_lifecycle_action("p", "resume")
    assert resume.value.status_code == 409 and resume.value.detail == lifecycle_reason
    with pytest.raises(HTTPException) as reauthorize:
        project_authorization.reauthorize_project_record(
            "p", ReauthorizeProjectRequest(confirmed=True, expected_authorization_epoch=before["authorization_epoch"],
                                            expected_scope_digest=digest, authorization_expires_at=hot_db[1]),
            actor_id=hot_db[0].subject_id,
        )
    assert reauthorize.value.status_code == 409 and reauthorize.value.detail == "project_status_invalid"
    assert dict(project()) == before
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM project_authorization_grants_v2 WHERE project_id = 'p'").fetchone()["n"] == 1
