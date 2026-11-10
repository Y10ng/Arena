"""流量停止事实、恢复证明和同步积压的局部交接。"""

from concurrent.futures import Future
import json
from types import SimpleNamespace
import threading

import pytest

from cairn.dispatcher.runtime.containers import CandidateExecutionCheck
from cairn.dispatcher.runtime.traffic_execution import TrafficExecutionBridge
from cairn.dispatcher.scheduler import candidate_recovery
from cairn.server.schemas.traffic import TrafficScopeStatus
from test_candidate_recovery import _Client, _ContainerManager, _Harness, _fence, _outbox, _successful_replay


def test_stopped_capture_keeps_failure_history_without_reporting_unknown_backlog_as_zero():
    stopping = threading.Event()
    stopping.set()
    execution = SimpleNamespace(failure="traffic_capture_stalled", stopping=stopping,
                                process=SimpleNamespace(execution_completed=False))
    facts = {"entry_closed": True, "termination_confirmed": True, "test_stopped": True,
             "collector_stopped": True, "sealed_at": 100.0}
    status = TrafficExecutionBridge._scope_status(facts, execution, {}, {})
    assert status["state"] == "stopped" and status["code"] is None
    assert status["failure_code"] == "traffic_capture_stalled"
    assert status["pending"] is status["pending_bytes"] is None
    assert status["upload_complete"] is False
    assert TrafficScopeStatus.model_validate(status).pending is None
    upload = {"state": "watching", "pending": 0, "pending_bytes": 0,
              "upload_complete": True, "scan_started_at": 99.0}
    assert TrafficExecutionBridge._scope_status(facts, execution, upload, {})["upload_complete"] is False
    upload["scan_started_at"] = 101.0
    assert TrafficExecutionBridge._scope_status(facts, execution, upload, {})["upload_complete"] is True
    facts["termination_confirmed"] = False
    assert TrafficExecutionBridge._scope_status(facts, execution, upload, {})["state"] == "stopping"
    upload.update(state="retrying", code="traffic_upload_unavailable")
    status = TrafficExecutionBridge._scope_status(facts, execution, upload, {})
    assert status["state"] == "retrying" and status["failure_code"] == "traffic_capture_stalled"


@pytest.mark.parametrize("failure_at", ["write", "fsync"])
def test_exited_sync_persistence_failure_overrides_nonempty_scope_snapshot(tmp_path, monkeypatch, failure_at):
    from cairn.dispatcher.runtime import traffic_sync

    sync = traffic_sync.TrafficSync(SimpleNamespace(), capture=tmp_path / "capture", receipts=tmp_path / "receipts")
    upload = {"state": "watching", "code": None, "pending": 3, "pending_bytes": 17,
              "upload_complete": False, "scan_started_at": 101.0}
    save = traffic_sync._save_receipt

    def fail(*_):
        raise OSError("synthetic status persistence failure")

    def save_status(path, value):
        if not value["scopes"]:
            return save(path, value)
        assert path == tmp_path / "receipts" / "status.json"
        if failure_at == "write":
            fail()
        with monkeypatch.context() as patch:
            patch.setattr(traffic_sync.os, "fsync", fail)
            save(path, value)

    monkeypatch.setattr(traffic_sync, "_save_receipt", save_status)
    monkeypatch.setattr(sync, "_step", lambda: True)
    monkeypatch.setattr(sync, "_publish_inventory", lambda: sync._publish_status(state="watching", scopes={"scope": upload}))
    sync.start()
    sync._thread.join(timeout=2)
    assert not sync._thread.is_alive()
    snapshot = sync.status()
    assert snapshot["running"] is False and snapshot["code"] == "status_persist_failed"
    assert snapshot["scopes"]["scope"] == upload
    facts = {"termination_confirmed": True, "test_stopped": True, "collector_stopped": True, "sealed_at": 100.0}
    execution = SimpleNamespace(failure=None, stopping=threading.Event(), process=SimpleNamespace(execution_completed=True))
    for local_state in ("watching", "retrying"):
        status = TrafficExecutionBridge._scope_status(facts, execution, {**upload, "state": local_state}, snapshot)
        assert status["state"] == "blocked" and status["code"] == "status_persist_failed"
        assert status["pending"] == 3 and status["pending_bytes"] == 17 and status["upload_complete"] is False
        assert all(status[key] is True for key in ("control_stopped", "test_stopped", "collector_stopped"))
        TrafficScopeStatus.model_validate(status)
    complete = {**upload, "pending": 0, "pending_bytes": 0, "upload_complete": True}
    status = TrafficExecutionBridge._scope_status(facts, execution, complete, snapshot)
    assert status["upload_complete"] is True and status["state"] == "stopped" and status["code"] is None
    # 局部错误汇总不代表上传者退出，不能污染其他 scope。
    status = TrafficExecutionBridge._scope_status(facts, execution, upload, {"state": "blocked", "running": True, "code": None})
    assert status["state"] == "stopped" and status["code"] is None


def test_scope_failure_survives_normal_close_and_bridge_restart(tmp_path, monkeypatch):
    from cairn.dispatcher.runtime import traffic

    runtime = traffic.ProjectTrafficRuntime(
        None, project_id="synthetic", root=tmp_path, gateway_image="gateway", executor_image="executor",
    )
    runtime.manifest = {"scopes": {"scope": {"test_stopped": False, "collector_stopped": False}}, "resources": {}}
    saved = []
    runtime._save = lambda: saved.append(json.loads(json.dumps(runtime.manifest)))
    runtime._stop_container = lambda _: True
    runtime._seal_interrupted = lambda _: None
    monkeypatch.setattr(traffic.time, "sleep", lambda _: None)
    runtime.stop_scope("scope", "traffic_capture_stalled")
    runtime.stop_scope("scope", "project_closed")
    facts = saved[-1]["scopes"]["scope"]
    assert facts["code"] == "project_closed"
    assert facts["failure_code"] == "traffic_capture_stalled"
    status = TrafficExecutionBridge._scope_status(facts, None, {}, {})
    assert status["state"] == "stopped" and status["code"] is None
    assert status["failure_code"] == "traffic_capture_stalled"


@pytest.mark.parametrize("proof_source", ["durable_fence", "completed_stop"])
def test_arrived_terminal_proof_does_not_wait_for_previous_recovery_backoff(tmp_path, monkeypatch, proof_source):
    fence = _fence()
    outbox = _outbox(tmp_path, fence)
    manager = _ContainerManager(check=CandidateExecutionCheck("unknown", "termination_proof_unavailable"))
    client = _Client()
    client.run_status = "cancelled"
    harness = _Harness(outbox, manager, client=client)
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)
    monkeypatch.setattr(candidate_recovery, "intake_worker_candidates", _successful_replay)
    assert harness._refresh_candidate_recovery() is False
    assert harness._candidate_recovery_due(fence) is False
    assert harness._refresh_candidate_recovery() is False
    assert len(manager.inspections) == 1 and client.updates == []
    if proof_source == "durable_fence":
        outbox.replace_fence(fence, fence.confirm_terminal())
    else:
        future = Future()
        future.set_result(CandidateExecutionCheck("stopped", "exec_terminal_descendants_drained", 143))
        harness._candidate_stop_futures = {fence.capture_id: future}
    assert harness._refresh_candidate_recovery() is True
    assert outbox.fences() == []
    assert client.updates[0][2]["runtime_context"]["execution_lifecycle"]["termination_confirmed"] is True
    assert len(manager.inspections) == 1 and manager.terminations == []


@pytest.mark.parametrize("proof_generation,proof_version,expected_unknown", [(0, 2, False), (1, 2, True), (0, 1, True)])
def test_diagnostics_consumes_only_matching_accepted_terminal_proof(proof_generation, proof_version, expected_unknown):
    from cairn.server.services.project_runtime_diagnostics import _candidate_recovery

    now = "2026-10-23T10:00:00Z"
    item = {"project_id": "synthetic", "task_run_id": "run", "status": "unknown",
            "reason": "termination_proof_unavailable", "observed_at": now}
    context = {"dispatcher_id": "dispatcher", "execution_lifecycle": {
        "generation": proof_generation, "termination_confirmed": True, "cleanup_confirmed": False},
        "candidate_recovery": {"physical_proof_version": proof_version}}
    rows = [{"id": "run", "execution_generation": 0, "runtime_context_json": json.dumps(context)}]
    conn = SimpleNamespace(execute=lambda *_: SimpleNamespace(fetchall=lambda: rows))
    result = _candidate_recovery(conn, {"candidate_recovery": [item]},
                                 {"id": "synthetic", "execution_generation": 0}, "dispatcher", now)
    assert (result is not None) is expected_unknown
