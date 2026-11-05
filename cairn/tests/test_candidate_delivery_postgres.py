"""第 26.14 的正式交付、候选留存和恢复 fence 接缝；仅使用隔离 PostgreSQL。"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
import os
from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.pause_clock import PauseClock
from cairn.dispatcher.tasks import delivery
from cairn.dispatcher.tasks.candidate_capture_outbox import CandidateCaptureOutbox
from cairn.dispatcher.tasks.candidate_intake import decode_candidate_spool
from cairn.server import db
from cairn.server.app import app
from cairn.server.auth import RequestPrincipal, create_machine_principal
from cairn.server.schemas.candidates import canonical_json_bytes, canonical_value_sha256
from cairn.server.services import candidate_store, projects
from cairn.server.services.project_records import get_project_detail
from cairn.server.services.time_utils import utcnow
from test_candidate_intake import _candidate_manifest, _frame, _outcome_manifest
from test_candidate_recovery import _ContainerManager, _Harness, _fence
from test_pi_execution import worker
from test_postgres_bootstrap import empty_postgres_url, postgres_instance
from test_project_hot_execution_control_postgres import hot_db

pytestmark = pytest.mark.skipif(
    os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1", reason="仅使用一次性 PostgreSQL",
)

SESSION = "pi:11111111-1111-4111-8111-111111111111"


class _LocalClient:
    def __init__(self, token):
        self.http = TestClient(app, headers={"Authorization": "Bearer " + token})
        self.receipts = []

    def get_project(self, project_id):
        return get_project_detail(project_id)

    def update_task_run(self, project_id, task_run_id, **updates):
        response = self.http.patch(f"/projects/{project_id}/task-runs/{task_run_id}", json=updates)
        return ApiResult(status_code=response.status_code, data=response.json())

    def list_task_runs(self, project_id):
        response = self.http.get(f"/projects/{project_id}/task-runs")
        return ApiResult(status_code=response.status_code, data=response.json())

    def capture_candidate(self, project_id, task_run_id, manifest_bytes, raw):
        response = self.http.post(
            f"/projects/{project_id}/task-runs/{task_run_id}/candidates",
            content=raw,
            headers={"Content-Type": "application/octet-stream", "X-Cairn-Candidate-Manifest":
                     base64.urlsafe_b64encode(manifest_bytes).rstrip(b"=").decode("ascii")},
        )
        result = ApiResult(status_code=response.status_code, data=response.json())
        self.receipts.append(result)
        return result


@pytest.fixture
def registered_candidate(hot_db, tmp_path):
    owner, token = create_machine_principal("candidate-delivery-owner")
    client = _LocalClient(token)
    lifecycle = {"generation": 7, "process_started": True, "termination_confirmed": False, "cleanup_confirmed": False}
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO task_runs (id, project_id, task_type, worker_name, worker_type, status, started_at, "
            "runtime_context_json, execution_generation) VALUES ('tr001', 'p', 'bootstrap', 'worker', 'pi', 'running', ?, ?, 7)",
            (utcnow(), json.dumps({"dispatcher_id": owner, "execution_lifecycle": lifecycle,
                                  "execution_container_ids": ["container-id"]})),
        )
    manager = _ContainerManager()
    manager.runtime_metadata = lambda _: {"container_id": "container-id", "container_state": "running"}
    snapshot = delivery.register_delivery(
        client, manager, "cairn-p", client.get_project("p"), "tr001", worker(),
        phase="bootstrap_execute", session=SESSION, timeout_seconds=60,
        binding={"assessment_protocol": "goal-driven-v1"},
        budget=delivery.ResearchDeliveryBudget(PauseClock()),
    )
    assert len(snapshot) == 10 and {"paused_seconds", "authorization_expires_at"} <= snapshot.keys()
    fence = replace(
        _fence(terminal_confirmed=True), owner_id=owner, project_id="p", task_run_id="tr001",
        phase=snapshot["phase"], session=SESSION, binding_sha256=canonical_value_sha256(snapshot["binding"]),
        result_schema_sha256=snapshot["result_schema_sha256"],
    )
    raw = '  {"合成观察": "原始候选字节保持不变"}\n'.encode()
    manifest = _candidate_manifest(
        raw, project_id="p", task_run_id="tr001", phase=snapshot["phase"], session=SESSION,
        binding_sha256=fence.binding_sha256, result_schema_sha256=fence.result_schema_sha256, artifacts=[],
    )
    outcome = _outcome_manifest(code="locally_validated", keyword=None, path=None)
    spool_bytes = _frame(manifest, raw) + _frame(outcome, b"")
    spool_file = tmp_path / "synthetic-spool.bin"
    spool_file.write_bytes(spool_bytes)

    manager.materializations = []

    @contextmanager
    def materialize(container_id, path):
        assert container_id == fence.container_id and path == fence.spool_path
        manager.materializations.append((container_id, path))
        yield SimpleNamespace(path=spool_file, size=len(spool_bytes))

    manager.materialize_file = materialize
    outbox = CandidateCaptureOutbox(tmp_path / "candidate-outbox", owner_id=owner)
    outbox.persist_fence(fence)
    return SimpleNamespace(client=client, manager=manager, outbox=outbox, fence=fence, owner=owner,
                           snapshot=snapshot, raw=raw, manifest=manifest, outcome=outcome, spool=spool_bytes)


def test_registered_delivery_capture_replay_preserves_success_and_waits_for_resume(registered_candidate):
    case = registered_candidate
    frozen = deepcopy(case.snapshot)
    # 模拟已经业务接受的 Bootstrap；本测试不重新提交 Fact，也不执行任何 Worker。
    with db.get_conn() as conn:
        conn.execute("INSERT INTO facts (id, project_id, description, creator, created_at) VALUES ('f001', 'p', '合成已接受事实', 'worker', ?)", (utcnow(),))
        row = conn.execute("SELECT runtime_context_json FROM task_runs WHERE project_id='p' AND id='tr001'").fetchone()
        context = json.loads(row["runtime_context_json"])
        context["accepted_fact_id"] = "f001"
        conn.execute("UPDATE task_runs SET runtime_context_json=? WHERE project_id='p' AND id='tr001'", (json.dumps(context),))
    ended = case.client.update_task_run("p", "tr001", status="succeeded", runtime_context={"execution_lifecycle": {
        "generation": 7, "process_started": True, "termination_confirmed": True, "cleanup_confirmed": False,
    }})
    assert ended.ok
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET execution_control_json=? WHERE id='p'", (json.dumps({"action": "resume", "state": "lost"}),))

    entries = decode_candidate_spool(case.spool, case.fence.candidate_spool(), project_id="p", task_run_id="tr001",
                                     phase=case.fence.phase, session=SESSION)
    for entry in entries:
        case.outbox.persist(entry)
    # 重启后的 durable replay 与容器 spool 再取回共用真实 HTTP capture；第二次必须 exact replay。
    case.outbox = CandidateCaptureOutbox(case.outbox.root, owner_id=case.owner)
    assert case.outbox.entries() == entries
    harness = _Harness(case.outbox, case.manager, client=case.client)
    harness.dispatcher_id = case.owner
    assert not harness._refresh_candidate_recovery()
    assert case.outbox.entries() == [] and case.outbox.rejections() == []
    assert [item.status_code for item in case.client.receipts] == [201, 201]
    assert harness._candidate_project_blocked("p") and case.outbox.fences() == [case.fence]
    assert case.manager.materializations == case.manager.inspections == case.manager.terminations == []
    assert case.manager.spool_removals == case.manager.control_removals == []
    with db.get_conn() as conn:
        with pytest.raises(HTTPException, match="execution_control_pending"):
            projects.check_project_active(conn, "p")
        # 仅模拟观察端已给出有效 running；物理恢复由 observer 定向用例覆盖。
        conn.execute("UPDATE projects SET execution_control_json=? WHERE id='p'", (json.dumps({"action": "resume", "state": "running"}),))
    assert harness._refresh_candidate_recovery()
    assert not harness._candidate_project_blocked("p") and case.outbox.fences() == []
    assert [item.status_code for item in case.client.receipts] == [201, 201, 200, 200]
    with db.get_conn() as conn:
        projects.check_project_active(conn, "p")
        run = conn.execute("SELECT * FROM task_runs WHERE project_id='p' AND id='tr001'").fetchone()
        context = json.loads(run["runtime_context_json"])
        assert run["status"] == "succeeded" and run["execution_generation"] == 7 and run["session_id"] == SESSION
        assert context["execution_delivery"] == frozen and context["accepted_fact_id"] == "f001"
        assert context["execution_lifecycle"]["cleanup_confirmed"] is False
        assert conn.execute("SELECT COUNT(*) AS n FROM task_runs WHERE project_id='p'").fetchone()["n"] == 1
        stored = conn.execute("SELECT raw_bytes, manifest_json, request_sha256 FROM task_run_candidates WHERE project_id='p'").fetchone()
        assert bytes(stored["raw_bytes"]) == case.raw and stored["manifest_json"].encode() == entries[0].manifest_bytes
        assert stored["request_sha256"] == sha256(entries[0].manifest_bytes + case.raw).hexdigest()
        assert conn.execute("SELECT COUNT(*) AS n FROM task_run_candidate_outcomes WHERE project_id='p'").fetchone()["n"] == 1


def test_candidate_identity_and_frozen_delivery_rejections(registered_candidate):
    case = registered_candidate
    _, wrong_token = create_machine_principal("candidate-wrong-owner")
    wrong_owner = _LocalClient(wrong_token).capture_candidate("p", "tr001", canonical_json_bytes(case.manifest), case.raw)
    assert wrong_owner.status_code == 403 and wrong_owner.data["detail"] == "execution_owner_mismatch"
    for changes, expected in (
        ({"phase": "reason_execute"}, "candidate_phase_mismatch"),
        ({"session": "pi:33333333-3333-4333-8333-333333333333"}, "candidate_session_mismatch"),
        ({"binding_sha256": "a" * 64}, "candidate_binding_digest_mismatch"),
        ({"result_schema_sha256": "b" * 64}, "candidate_result_schema_digest_mismatch"),
        ({"parent_producer_candidate_key": case.manifest["capture_id"] + ":2:0"}, "candidate_parent_missing"),
    ):
        response = case.client.capture_candidate("p", "tr001", canonical_json_bytes(case.manifest | changes), case.raw)
        assert response.status_code == 409 and response.data["detail"] == expected
    changed = case.client.update_task_run("p", "tr001", runtime_context={"execution_delivery": case.snapshot | {"paused_seconds": 1}})
    assert changed.status_code == 422 and changed.data["detail"] == "execution_delivery_is_server_owned"
    first = case.client.capture_candidate("p", "tr001", canonical_json_bytes(case.manifest), case.raw)
    assert first.status_code == 201
    changed_raw = case.raw + b" "
    changed_manifest = case.manifest | {"raw_size_bytes": len(changed_raw), "raw_sha256": sha256(changed_raw).hexdigest()}
    conflict = case.client.capture_candidate("p", "tr001", canonical_json_bytes(changed_manifest), changed_raw)
    assert conflict.status_code == 409 and conflict.data["detail"] == "candidate_receipt_conflict"


def test_candidate_delivery_identity_missing_or_malformed_is_explicit(registered_candidate):
    case = registered_candidate
    principal = RequestPrincipal("machine", case.owner)
    for field, malformed in (("session_id", []), ("phase", "bad phase"), ("binding", []), ("result_schema_sha256", "invalid")):
        for missing in (True, False):
            snapshot = deepcopy(case.snapshot)
            if missing:
                snapshot.pop(field)
            else:
                snapshot[field] = malformed
            row = {"session_id": SESSION, "runtime_context_json": json.dumps({"dispatcher_id": case.owner, "execution_delivery": snapshot})}
            with pytest.raises(HTTPException) as error:
                candidate_store._verified_delivery(row, principal=principal)
            assert error.value.status_code == 409 and error.value.detail == "candidate_execution_delivery_unavailable"
