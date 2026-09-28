from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import threading
import time
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.containers import CandidateExecutionCheck, ContainerManager
from cairn.dispatcher.scheduler import candidate_recovery
from cairn.dispatcher.scheduler.candidate_recovery import CandidateRecoveryMixin
from cairn.dispatcher.scheduler.cleanup import CleanupMixin
from cairn.dispatcher.scheduler.dispatch_cycle import DispatchCycleMixin
from cairn.dispatcher.scheduler.report_finish_outbox import ReportFinishOutboxMixin
from cairn.dispatcher.scheduler.stale_reconcile import StaleReconcileMixin
from cairn.dispatcher.scheduler.task_lifecycle import TaskLifecycleMixin
from cairn.dispatcher.tasks.candidate_capture_outbox import (
    CandidateCaptureOutbox,
    CandidateCaptureOutboxError,
    CandidateExecutionFence,
)
from cairn.dispatcher.tasks.candidate_intake import CandidateIntakeSummary
from cairn.dispatcher.tasks.report_finish_outbox import (
    FINISH_PROTOCOL,
    PendingReportFinish,
    ReportFinishOutbox,
    pending_report_execution,
    pending_report_output,
)


_CONTROL_PATH = "/tmp/cairn-exec-0123456789abcdef0123456789abcdef.json"


def _fence(
    *,
    terminal_confirmed: bool = False,
    bound: bool = True,
    start_attempted: bool = True,
) -> CandidateExecutionFence:
    prepared = CandidateExecutionFence.create(
        owner_id="dispatcher-1",
        project_id="project-1",
        task_run_id="run-1",
        container_name="cairn-project-1",
        phase="explore_execute",
        session="pi:87654321-4321-4321-8321-cba987654321",
        spool_path=(
            "/tmp/cairn-prompts/"
            "pi-candidates-12345678-1234-4234-8234-123456789abc.bin"
        ),
        capture_id="12345678-1234-4234-8234-123456789abc",
        max_candidates=16,
        max_candidate_bytes=2 * 1024 * 1024,
        max_manifest_bytes=4096,
        max_spool_bytes=34 * 1024 * 1024,
        binding_sha256="a" * 64,
        result_schema_sha256="b" * 64,
        docker_daemon_id="daemon-1",
    )
    if not bound:
        return prepared
    bound_fence = prepared.bind_execution(
        container_id="container-id",
        exec_id="exec-id",
        control_path=_CONTROL_PATH,
    )
    if start_attempted:
        bound_fence = bound_fence.mark_start_attempted()
    return bound_fence.confirm_terminal() if terminal_confirmed else bound_fence


class _ContainerManager:
    def __init__(
        self,
        *,
        check: CandidateExecutionCheck | None = None,
    ) -> None:
        self.check = check or CandidateExecutionCheck(
            "stopped",
            "exec_terminal_descendants_drained",
            0,
        )
        self.inspections: list[dict[str, str | None]] = []
        self.spool_removals: list[tuple[str, str, str]] = []
        self.control_removals: list[dict[str, str]] = []
        self.terminations: list[dict[str, str | None]] = []

    def candidate_execution_status(self, **kwargs: str | None) -> CandidateExecutionCheck:
        self.inspections.append(kwargs)
        return self.check

    def terminate_candidate_execution(self, **kwargs: object) -> CandidateExecutionCheck:
        reserve = kwargs.pop("reserve_attempt")
        if self.check.status != "running":
            return self.check
        assert callable(reserve)
        blocked = reserve()
        if blocked is not None:
            return blocked
        self.terminations.append(kwargs)
        return CandidateExecutionCheck("stopped", "exec_terminal_descendants_drained", 143)

    def docker_daemon_id(self) -> str:
        return "daemon-1"

    def remove_file(
        self,
        container_id: str,
        path: str,
        *,
        allowed_root: str,
    ) -> None:
        self.spool_removals.append((container_id, path, allowed_root))

    def cleanup_candidate_execution_control(self, **kwargs: str) -> None:
        self.control_removals.append(kwargs)

    def build_exec_process(self, *_args: object, **_kwargs: object) -> None:
        pytest.fail("candidate recovery must not rebuild or restart the model")

    def ensure_running(self, *_args: object, **_kwargs: object) -> None:
        pytest.fail("candidate recovery must not start a container")


class _Client:
    def __init__(self) -> None:
        self.updates: list[tuple[str, str, dict[str, object]]] = []
        self.run_status = "running"

    def list_task_runs(self, _project_id: str) -> ApiResult:
        return ApiResult(
            status_code=200,
            data=[
                {
                    "id": "run-1",
                    "status": self.run_status,
                    "task_type": "explore",
                    "execution_generation": 0,
                    "runtime_context": {
                        "dispatcher_id": "dispatcher-1",
                        "execution_lifecycle": {
                            "generation": 0,
                            "process_started": False,
                            "termination_confirmed": False,
                            "cleanup_confirmed": False,
                        }
                    },
                }
            ],
        )

    def update_task_run(
        self,
        project_id: str,
        task_run_id: str,
        **kwargs: object,
    ) -> ApiResult:
        self.updates.append((project_id, task_run_id, kwargs))
        return ApiResult(status_code=200, data={})


class _Harness(CandidateRecoveryMixin, ReportFinishOutboxMixin):
    def __init__(
        self,
        outbox: CandidateCaptureOutbox,
        manager: _ContainerManager,
        *,
        report_finish_outbox: ReportFinishOutbox | None = None,
        client: _Client | None = None,
    ) -> None:
        self.candidate_capture_outbox = outbox
        self.report_finish_outbox = report_finish_outbox
        self.container_manager = manager
        self.client = client or _Client()
        self.dispatcher_id = "dispatcher-1"
        self.futures: dict[str, object] = {}
        self._candidate_recovery_cursor = 0
        self._candidate_fence_invalid = False
        self._candidate_fenced_projects: set[str] = set()
        self._candidate_fenced_task_runs: set[tuple[str, str]] = set()
        self._candidate_fenced_containers: set[str] = set()


def _outbox(tmp_path: Path, fence: CandidateExecutionFence | None = None):
    outbox = CandidateCaptureOutbox(
        tmp_path / "candidate-outbox",
        owner_id="dispatcher-1",
    )
    if fence is not None:
        outbox.persist_fence(fence)
    return outbox


def _successful_replay(*_args: object, **_kwargs: object) -> CandidateIntakeSummary:
    return CandidateIntakeSummary(host_durable=True)


_REPORT_CLAIM_SHA256 = sha256(b"report-claim").hexdigest()


class _ReportClient(_Client):
    def __init__(self, *, valid_receipt: bool = True) -> None:
        super().__init__()
        self.valid_receipt = valid_receipt
        self.finish_requests: list[dict[str, object]] = []
        self.artifact_requests: list[dict[str, object]] = []

    def list_task_runs(self, _project_id: str) -> ApiResult:
        return ApiResult(
            status_code=200,
            data=[
                {
                    "id": "run-1",
                    "status": "running",
                    "task_type": "report",
                    "worker_name": "worker-1",
                    "execution_generation": 0,
                    "runtime_context": {
                        "dispatcher_id": "dispatcher-1",
                        "report_claim": {
                            "claim_sha256": _REPORT_CLAIM_SHA256,
                            "generation_token": "generation-1",
                        },
                        "execution_lifecycle": {
                            "generation": 0,
                            "process_started": True,
                            "termination_confirmed": False,
                            "cleanup_confirmed": False,
                        },
                    },
                }
            ],
        )

    def replay_assessment_report_finalization(
        self,
        project_id: str,
        worker: str,
        **kwargs: object,
    ) -> ApiResult:
        request = {"project_id": project_id, "worker": worker, **kwargs}
        self.finish_requests.append(request)
        if not self.valid_receipt:
            return ApiResult(status_code=200, data={"status": "failed"})
        return ApiResult(
            status_code=200,
            data={
                "protocol": FINISH_PROTOCOL,
                "project_id": project_id,
                "task_run_id": kwargs["task_run_id"],
                "generation_token": kwargs["generation_token"],
                "status": kwargs["status"],
                "artifact_id": kwargs["artifact_id"],
                "input_snapshot_artifact_id": kwargs[
                    "input_snapshot_artifact_id"
                ],
                "input_snapshot_sha256": kwargs["input_snapshot_sha256"],
                "request_sha256": kwargs["request_sha256"],
                "finish_disposition": "accepted",
            },
        )

    def list_artifacts(self, _project_id: str, **_kwargs: object) -> ApiResult:
        return ApiResult(status_code=200, data=[])

    def create_artifact(self, project_id: str, **kwargs: object) -> ApiResult:
        request = {"project_id": project_id, **kwargs}
        self.artifact_requests.append(request)
        content = kwargs["content"]
        assert isinstance(content, str)
        return ApiResult(
            status_code=201,
            data={
                "id": "report-artifact-1",
                "sha256": sha256(content.encode("utf-8")).hexdigest(),
            },
        )


def _report_execution():
    return pending_report_execution(
        project_id="project-1",
        authenticated_dispatcher_id="dispatcher-1",
        worker="worker-1",
        claim_sha256=_REPORT_CLAIM_SHA256,
        generation_token="generation-1",
        task_run_id="run-1",
        input_snapshot_artifact_id="snapshot-1",
        input_snapshot_sha256="b" * 64,
    )


def test_candidate_recovery_reverifies_proof_confirms_fence_and_only_replays_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence()
    outbox = _outbox(tmp_path, fence)
    manager = _ContainerManager()
    harness = _Harness(outbox, manager)
    intakes: list[dict[str, object]] = []
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)

    def intake(*args: object, **kwargs: object) -> CandidateIntakeSummary:
        intakes.append({"args": args, "kwargs": kwargs})
        persisted = outbox.fences()
        assert len(persisted) == 1
        assert persisted[0].terminal_confirmed is True
        return CandidateIntakeSummary(host_durable=True, pending=1)

    monkeypatch.setattr(candidate_recovery, "intake_worker_candidates", intake)

    assert harness._refresh_candidate_recovery() is True

    assert outbox.fences() == []
    assert len(intakes) == 1
    assert len(harness.client.updates) == 1
    update = harness.client.updates[0][2]
    assert update["status"] == "failed"
    assert update["error_type"] == "delivery_unavailable"
    assert update["exit_code"] == 0
    assert update["runtime_context"] == {
        "execution_lifecycle": {
            "generation": 0,
            "process_started": True,
            "termination_confirmed": True,
            "cleanup_confirmed": True,
        },
        "candidate_recovery": {
            "status": "stopped",
            "reason": "exec_terminal_descendants_drained",
            "recovered_at": update["runtime_context"]["candidate_recovery"]["recovered_at"],
            "container_spool_recovered": True,
            "physical_proof_version": 2,
        },
        "execution_container_ids": ["container-id"],
    }
    assert manager.inspections == [
        {
            "docker_daemon_id": "daemon-1",
            "container_id": "container-id",
            "container_name": "cairn-project-1",
            "project_id": "project-1",
            "exec_id": "exec-id",
            "control_path": _CONTROL_PATH,
        }
    ]
    assert manager.spool_removals == [
        (
            "container-id",
            fence.spool_path,
            "/tmp/cairn-prompts",
        )
    ]
    assert manager.control_removals == [
        {
            "container_id": "container-id",
            "container_name": "cairn-project-1",
            "project_id": "project-1",
            "control_path": _CONTROL_PATH,
        }
    ]


def test_candidate_recovery_waits_for_late_stop_before_archive_or_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence()
    outbox = _outbox(tmp_path, fence)
    manager = _ContainerManager(check=CandidateExecutionCheck("running", "exec_running"))
    harness = _Harness(outbox, manager)
    intakes: list[bool] = []
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)

    def intake(*_args: object, **_kwargs: object) -> CandidateIntakeSummary:
        intakes.append(True)
        return CandidateIntakeSummary(host_durable=True)

    monkeypatch.setattr(candidate_recovery, "intake_worker_candidates", intake)
    assert harness._refresh_candidate_recovery() is False
    assert outbox.fences() == [fence]
    assert harness.client.updates == []
    assert intakes == []
    assert manager.spool_removals == []

    manager.check = CandidateExecutionCheck("stopped", "container_not_running")
    harness._candidate_recovery_retry_states.clear()
    assert harness._refresh_candidate_recovery() is True
    assert intakes == [True]
    assert len(harness.client.updates) == 1
    assert "尚未启动" not in harness.client.updates[0][2]["error_message"]
    assert outbox.fences() == []


def test_lost_run_stops_only_its_exec_while_sibling_future_remains_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence()
    outbox = _outbox(tmp_path, fence)
    manager = _ContainerManager(check=CandidateExecutionCheck("running", "exec_running"))
    client = _Client()
    client.run_status = "failed"
    harness = _Harness(outbox, manager, client=client)
    harness.futures = {
        "healthy": SimpleNamespace(project_id=fence.project_id, task_run_id="run-2"),
    }
    started = threading.Event()
    release = threading.Event()
    replay_calls: list[dict[str, object]] = []

    def stop(**identity: object) -> CandidateExecutionCheck:
        reserve_attempt = identity.pop("reserve_attempt")
        assert callable(reserve_attempt) and reserve_attempt() is None
        manager.terminations.append(identity)
        started.set()
        assert release.wait(5)
        return CandidateExecutionCheck("stopped", "exec_terminal_descendants_drained", 143)

    def replay(*_args: object, **kwargs: object) -> CandidateIntakeSummary:
        replay_calls.append(kwargs)
        return CandidateIntakeSummary(host_durable=True)

    monkeypatch.setattr(manager, "terminate_candidate_execution", stop)
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", replay)
    monkeypatch.setattr(candidate_recovery, "intake_worker_candidates", lambda *_args, **_kwargs: CandidateIntakeSummary(host_durable=True))
    with ThreadPoolExecutor(max_workers=1) as harness.cleanup_executor:
        before = time.monotonic()
        assert harness._refresh_candidate_recovery() is False
        assert time.monotonic() - before < 2
        assert started.wait(5)
        assert outbox.fences()[0].termination_attempted is True
        assert client.updates == []
        assert len(manager.terminations) == 1
        assert manager.terminations[0]["exec_id"] == "exec-id"
        assert all(item["exec_id"] != "sibling-exec-id" for item in manager.terminations)
        assert replay_calls[0]["excluded_project_ids"] == set()
        assert replay_calls[0]["excluded_task_runs"] == {(fence.project_id, "run-2")}
        release.set()
        assert harness._candidate_stop_futures[fence.capture_id].result(timeout=5).status == "stopped"
        assert harness._refresh_candidate_recovery() is True
    assert outbox.fences() == []
    assert len(client.updates) == 1
    assert client.updates[0][2]["status"] == "failed"
    assert "healthy" in harness.futures
    assert len(manager.terminations) == 1


def test_failed_task_stop_retries_after_restart_with_durable_limit_and_no_early_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence()
    outbox = _outbox(tmp_path, fence)
    manager = _ContainerManager(check=CandidateExecutionCheck("running", "exec_running"))
    client = _Client()
    client.run_status = "failed"
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)
    monkeypatch.setattr(candidate_recovery, "intake_worker_candidates", lambda *_args, **_kwargs: pytest.fail("unproved process must not archive"))

    def uncertain(**identity: object) -> CandidateExecutionCheck:
        reserve_attempt = identity.pop("reserve_attempt")
        assert callable(reserve_attempt) and reserve_attempt() is None
        manager.terminations.append(identity)
        return CandidateExecutionCheck("unknown", "task_termination_unconfirmed")

    manager.terminate_candidate_execution = uncertain
    for attempt in range(1, 4):
        restarted = _Harness(CandidateCaptureOutbox(outbox.root, owner_id="dispatcher-1"), manager, client=client)
        with ThreadPoolExecutor(max_workers=1) as restarted.cleanup_executor:
            assert restarted._refresh_candidate_recovery() is False
            assert restarted._candidate_stop_futures[fence.capture_id].result(timeout=5).status == "unknown"
            assert restarted._refresh_candidate_recovery() is False
        assert outbox.fences()[0].termination_attempt_count == attempt
        assert client.updates == []
        assert manager.spool_removals == []

    blocked = _Harness(CandidateCaptureOutbox(outbox.root, owner_id="dispatcher-1"), manager, client=client)
    assert blocked._refresh_candidate_recovery() is False
    assert len(manager.terminations) == 3
    assert blocked._candidate_recovery_observations[fence.capture_id]["reason"] == "task_termination_budget_exhausted"
    assert blocked._candidate_recovery_observations[fence.capture_id]["diagnostic_exhausted"] is True
    assert client.updates == []

    # 预算耗尽后仍轮询物理证明；一旦真的排空，继续摄取而不是永久卡住。
    manager.check = CandidateExecutionCheck("stopped", "container_not_running")
    monkeypatch.setattr(candidate_recovery, "intake_worker_candidates", lambda *_args, **_kwargs: CandidateIntakeSummary(host_durable=True))
    blocked._candidate_recovery_retry_states.clear()
    assert blocked._refresh_candidate_recovery() is True
    assert outbox.fences() == []
    assert len(manager.terminations) == 3
    assert len(client.updates) == 1


def test_transient_stop_failure_is_retried_after_backoff_without_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence()
    outbox = _outbox(tmp_path, fence)
    manager = _ContainerManager(check=CandidateExecutionCheck("running", "exec_running"))
    client = _Client()
    client.run_status = "failed"
    harness = _Harness(outbox, manager, client=client)
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)
    monkeypatch.setattr(candidate_recovery, "intake_worker_candidates", lambda *_args, **_kwargs: CandidateIntakeSummary(host_durable=True))
    attempts = [0]

    def stop(**identity: object) -> CandidateExecutionCheck:
        reserve_attempt = identity.pop("reserve_attempt")
        assert callable(reserve_attempt) and reserve_attempt() is None
        attempts[0] += 1
        manager.terminations.append(identity)
        if attempts[0] == 1:
            return CandidateExecutionCheck("unknown", "task_termination_control_unavailable")
        return CandidateExecutionCheck("stopped", "exec_terminal_descendants_drained", 143)

    manager.terminate_candidate_execution = stop
    with ThreadPoolExecutor(max_workers=1) as harness.cleanup_executor:
        assert harness._refresh_candidate_recovery() is False
        assert harness._candidate_stop_futures[fence.capture_id].result(timeout=5).status == "unknown"
        assert harness._refresh_candidate_recovery() is False
        assert harness._candidate_recovery_retry_states[fence.capture_id][2] == 15.0
        assert harness._refresh_candidate_recovery() is False
        assert attempts == [1]
        prior = harness._candidate_recovery_retry_states[fence.capture_id]
        harness._candidate_recovery_retry_states[fence.capture_id] = (
            prior[0], time.monotonic() - 1, prior[2], prior[3],
        )
        assert harness._refresh_candidate_recovery() is False
        assert harness._candidate_stop_futures[fence.capture_id].result(timeout=5).status == "stopped"
        assert harness._refresh_candidate_recovery() is True
    assert attempts == [2]
    assert outbox.fences() == []
    assert len(client.updates) == 1


def test_stop_preflight_failure_does_not_spend_budget_or_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence()
    outbox = _outbox(tmp_path, fence)
    manager = _ContainerManager(check=CandidateExecutionCheck("running", "exec_running"))
    client = _Client()
    client.run_status = "failed"
    harness = _Harness(outbox, manager, client=client)
    harness.cleanup_executor = _DeferredExecutor()
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)
    monkeypatch.setattr(candidate_recovery, "intake_worker_candidates", lambda *_args, **_kwargs: pytest.fail("unproved process must not archive"))

    assert harness._refresh_candidate_recovery() is False
    executor = harness.cleanup_executor
    assert len(executor.calls) == 1
    function, args, kwargs = executor.calls[0]
    manager.check = CandidateExecutionCheck("unknown", "docker_daemon_unavailable")
    result = function(*args, **kwargs)
    assert result.status == "unknown"
    executor.future.set_result(result)
    assert harness._refresh_candidate_recovery() is False
    assert outbox.fences()[0].termination_attempt_count == 0
    assert manager.terminations == []
    assert client.updates == []


def test_task_run_revocation_is_rechecked_immediately_before_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence()
    outbox = _outbox(tmp_path, fence)
    manager = _ContainerManager(check=CandidateExecutionCheck("running", "exec_running"))
    client = _Client()
    client.run_status = "failed"
    harness = _Harness(outbox, manager, client=client)
    harness.cleanup_executor = _DeferredExecutor()
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)

    assert harness._refresh_candidate_recovery() is False
    function, args, kwargs = harness.cleanup_executor.calls[0]
    client.run_status = "running"
    result = function(*args, **kwargs)
    assert result.reason == "task_run_still_active"
    assert outbox.fences()[0].termination_attempt_count == 0
    assert manager.terminations == []
    assert client.updates == []


def test_stop_budget_reservation_failure_never_sends_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence()
    outbox = _outbox(tmp_path, fence)
    manager = _ContainerManager(check=CandidateExecutionCheck("running", "exec_running"))
    client = _Client()
    client.run_status = "failed"
    harness = _Harness(outbox, manager, client=client)
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)
    monkeypatch.setattr(outbox, "replace_fence", lambda *_args: (_ for _ in ()).throw(CandidateCaptureOutboxError("disk unavailable")))
    with ThreadPoolExecutor(max_workers=1) as harness.cleanup_executor:
        assert harness._refresh_candidate_recovery() is False
        assert harness._candidate_stop_futures[fence.capture_id].result(timeout=5).status == "unknown"
    assert outbox.fences()[0].termination_attempt_count == 0
    assert manager.terminations == []
    assert client.updates == []


def test_orphan_with_nonterminal_server_run_never_receives_stop_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence()
    manager = _ContainerManager(check=CandidateExecutionCheck("running", "exec_running"))
    harness = _Harness(_outbox(tmp_path, fence), manager)
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)

    assert harness._refresh_candidate_recovery() is False
    assert harness._candidate_recovery_observations[fence.capture_id]["reason"] == "task_run_still_active"
    assert manager.terminations == []
    assert harness.candidate_capture_outbox.fences() == [fence]


def test_active_run_recheck_uses_backoff_without_resetting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence()
    manager = _ContainerManager(check=CandidateExecutionCheck("running", "exec_running"))
    harness = _Harness(_outbox(tmp_path, fence), manager)
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)
    clock = [100.0]
    monkeypatch.setattr(candidate_recovery.time, "monotonic", lambda: clock[0])

    assert harness._refresh_candidate_recovery() is False
    state = harness._candidate_recovery_retry_states[fence.capture_id]
    assert state[2] == 15.0
    assert manager.terminations == []

    clock[0] += 16
    assert harness._refresh_candidate_recovery() is False
    next_state = harness._candidate_recovery_retry_states[fence.capture_id]
    assert next_state[2] == 30.0
    assert next_state[3] == state[3]

    clock[0] += 1
    assert harness._refresh_candidate_recovery() is False
    assert harness._candidate_recovery_retry_states[fence.capture_id] == next_state
    assert len(manager.inspections) == 2
    assert manager.terminations == []


def test_lost_run_does_not_signal_without_live_execution_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence()
    outbox = _outbox(tmp_path, fence)
    manager = _ContainerManager(
        check=CandidateExecutionCheck("unknown", "termination_proof_unavailable")
    )
    client = _Client()
    client.run_status = "failed"
    harness = _Harness(outbox, manager, client=client)
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)
    monkeypatch.setattr(candidate_recovery, "intake_worker_candidates", lambda *_args, **_kwargs: CandidateIntakeSummary(host_durable=True))

    assert harness._refresh_candidate_recovery() is False
    assert outbox.fences()[0].termination_attempt_count == 0
    assert manager.terminations == []
    assert client.updates == []
    assert harness._candidate_recovery_observations[fence.capture_id]["reason"] == "termination_proof_unavailable"

    manager.check = CandidateExecutionCheck("stopped", "container_not_running")
    harness._candidate_recovery_retry_states.clear()
    assert harness._refresh_candidate_recovery() is True
    assert len(client.updates) == 1
    assert outbox.fences() == []


def test_stop_helper_success_without_drained_proof_is_not_an_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = object.__new__(ContainerManager)
    checks = iter([
        CandidateExecutionCheck("running", "exec_running"),
        CandidateExecutionCheck("unknown", "termination_proof_unavailable"),
    ])
    commands: list[tuple[str, list[str], float]] = []
    monkeypatch.setattr(manager, "candidate_execution_status", lambda **_kwargs: next(checks))

    def control(container_id: str, command: list[str], *, timeout: float) -> int:
        commands.append((container_id, command, timeout))
        return 0

    monkeypatch.setattr(manager, "_run_candidate_control", control)
    outcome = manager.terminate_candidate_execution(
        docker_daemon_id="daemon-1", container_id="container-id",
        container_name="cairn-project-1", project_id="project-1",
        exec_id="exec-id", control_path=_CONTROL_PATH,
    )

    assert outcome.status == "unknown"
    assert outcome.reason == "termination_proof_unavailable"
    assert len(commands) == 1
    assert commands[0][0] == "container-id"
    assert commands[0][1][-1] == _CONTROL_PATH
    assert commands[0][2] >= 18


def test_legacy_terminal_flag_needs_new_physical_proof_before_intake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = replace(_fence(terminal_confirmed=True), terminal_proof_version=0)
    outbox = _outbox(tmp_path, legacy)
    manager = _ContainerManager(
        check=CandidateExecutionCheck("unknown", "execution_control_unconfirmed")
    )
    harness = _Harness(outbox, manager)
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)
    monkeypatch.setattr(candidate_recovery, "intake_worker_candidates", lambda *_args, **_kwargs: pytest.fail("old group proof cannot authorize intake"))

    assert harness._refresh_candidate_recovery() is False
    assert outbox.fences() == [legacy]
    assert harness.client.updates == []

    manager.check = CandidateExecutionCheck("stopped", "container_not_running")
    harness._candidate_recovery_retry_states.clear()
    monkeypatch.setattr(candidate_recovery, "intake_worker_candidates", lambda *_args, **_kwargs: CandidateIntakeSummary(host_durable=True))
    assert harness._refresh_candidate_recovery() is True
    assert manager.terminations == []
    assert outbox.fences() == []


def test_legacy_ready_fence_reverifies_before_releasing_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = replace(_fence(terminal_confirmed=True), terminal_proof_version=0).mark_recovered(
        status="stopped",
        reason="exec_terminal_process_group_drained",
        container_spool_recovered=True,
    )
    outbox = _outbox(tmp_path, legacy)
    manager = _ContainerManager(
        check=CandidateExecutionCheck("unknown", "execution_control_unconfirmed")
    )
    harness = _Harness(outbox, manager)
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)
    monkeypatch.setattr(candidate_recovery, "intake_worker_candidates", lambda *_args, **_kwargs: pytest.fail("ready candidate already ingested"))

    assert harness._refresh_candidate_recovery() is False
    assert outbox.fences() == [legacy]
    assert harness.client.updates == []

    manager.check = CandidateExecutionCheck("stopped", "container_not_running")
    harness._candidate_recovery_retry_states.clear()
    assert harness._refresh_candidate_recovery() is True
    assert outbox.fences() == []
    assert len(harness.client.updates) == 1


def test_candidate_recovery_retains_terminal_fence_when_spool_state_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence(terminal_confirmed=True)
    outbox = _outbox(tmp_path, fence)
    manager = _ContainerManager(
        check=CandidateExecutionCheck("unknown", "docker_daemon_unavailable")
    )
    harness = _Harness(outbox, manager)
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)
    monkeypatch.setattr(
        candidate_recovery,
        "intake_worker_candidates",
        lambda *_args, **_kwargs: CandidateIntakeSummary(
            integrity_error="candidate_spool_read_failed"
        ),
    )

    assert harness._refresh_candidate_recovery() is False

    assert outbox.fences() == [fence]
    assert harness._candidate_project_blocked(fence.project_id) is True
    assert manager.spool_removals == []
    assert manager.control_removals == []
    assert len(manager.inspections) == 1
    observation = harness._candidate_recovery_observations[fence.capture_id]
    assert observation["project_id"] == fence.project_id
    assert observation["task_run_id"] == fence.task_run_id
    assert observation["status"] == "unknown"
    assert observation["reason"] == "docker_daemon_unavailable"
    assert isinstance(observation["observed_at"], str)


def test_candidate_recovery_closes_terminal_fence_after_container_disappears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence(terminal_confirmed=True)
    outbox = _outbox(tmp_path, fence)
    manager = _ContainerManager(
        check=CandidateExecutionCheck("gone", "container_missing_on_same_daemon")
    )
    harness = _Harness(outbox, manager)
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)
    monkeypatch.setattr(
        candidate_recovery,
        "intake_worker_candidates",
        lambda *_args, **_kwargs: CandidateIntakeSummary(
            integrity_error="candidate_spool_read_failed"
        ),
    )

    assert harness._refresh_candidate_recovery() is True

    assert outbox.fences() == []
    assert len(manager.inspections) == 1
    update = harness.client.updates[0][2]
    assert update["status"] == "failed"
    assert update["error_type"] == "container_failed"
    assert update["runtime_context"]["candidate_recovery"] == {
        "status": "gone",
        "reason": "container_missing_on_same_daemon",
        "recovered_at": update["runtime_context"]["candidate_recovery"]["recovered_at"],
        "container_spool_recovered": False,
        "physical_proof_version": 2,
    }
    assert update["runtime_context"]["execution_lifecycle"]["process_started"] is True


def test_candidate_recovery_refreshes_observation_time_on_each_physical_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence()
    harness = _Harness(_outbox(tmp_path), _ContainerManager())
    check = CandidateExecutionCheck("unknown", "docker_daemon_unavailable")
    timestamps = iter(["2026-09-21T00:00:00+00:00", "2026-09-21T00:00:01+00:00"])
    monkeypatch.setattr(candidate_recovery, "_utcnow", lambda: next(timestamps))

    harness._observe_candidate_recovery(fence, check)
    first = dict(harness._candidate_recovery_observations[fence.capture_id])
    harness._observe_candidate_recovery(fence, check)
    second = harness._candidate_recovery_observations[fence.capture_id]

    assert first["observed_at"] == "2026-09-21T00:00:00+00:00"
    assert second["observed_at"] == "2026-09-21T00:00:01+00:00"


def test_candidate_recovery_closes_gone_execution_without_reading_container_spool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence()
    outbox = _outbox(tmp_path, fence)
    manager = _ContainerManager(
        check=CandidateExecutionCheck("gone", "container_missing_on_same_daemon")
    )
    harness = _Harness(outbox, manager)
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)
    monkeypatch.setattr(
        candidate_recovery,
        "intake_worker_candidates",
        lambda *_args, **_kwargs: pytest.fail("gone container has no readable spool"),
    )

    assert harness._refresh_candidate_recovery() is True

    assert outbox.fences() == []
    assert manager.spool_removals == []
    assert manager.control_removals == []
    update = harness.client.updates[0][2]
    assert update["status"] == "failed"
    assert update["error_type"] == "container_failed"
    assert update["runtime_context"]["candidate_recovery"]["status"] == "gone"
    assert update["runtime_context"]["execution_lifecycle"] == {
        "generation": 0,
        "process_started": False,
        "termination_confirmed": True,
        "cleanup_confirmed": True,
    }


def test_candidate_recovery_closes_prepared_fence_before_exec_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence(start_attempted=False)
    outbox = _outbox(tmp_path, fence)
    manager = _ContainerManager()
    harness = _Harness(outbox, manager)
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)
    monkeypatch.setattr(
        candidate_recovery,
        "intake_worker_candidates",
        lambda *_args, **_kwargs: pytest.fail("prepared execution has no candidate writer"),
    )

    assert harness._refresh_candidate_recovery() is True

    assert outbox.fences() == []
    assert manager.inspections == []
    assert manager.spool_removals == [
        ("container-id", fence.spool_path, "/tmp/cairn-prompts")
    ]
    assert manager.control_removals == [{
        "container_id": "container-id",
        "container_name": "cairn-project-1",
        "project_id": "project-1",
        "control_path": _CONTROL_PATH,
    }]
    update = harness.client.updates[0][2]
    assert update["status"] == "failed"
    assert update["runtime_context"]["execution_lifecycle"]["process_started"] is False
    assert update["runtime_context"]["candidate_recovery"] == {
        "status": "stopped",
        "reason": "execution_not_started",
        "recovered_at": update["runtime_context"]["candidate_recovery"]["recovered_at"],
        "container_spool_recovered": False,
        "physical_proof_version": 2,
    }


def test_candidate_recovery_closes_unbound_fence_as_not_started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence(bound=False)
    outbox = _outbox(tmp_path, fence)
    manager = _ContainerManager()
    harness = _Harness(outbox, manager)
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)
    monkeypatch.setattr(
        candidate_recovery,
        "intake_worker_candidates",
        lambda *_args, **_kwargs: pytest.fail("unbound execution has no candidate writer"),
    )

    assert harness._refresh_candidate_recovery() is True

    assert outbox.fences() == []
    assert manager.inspections == []
    assert manager.spool_removals == [
        ("cairn-project-1", fence.spool_path, "/tmp/cairn-prompts")
    ]
    update = harness.client.updates[0][2]
    assert update["status"] == "failed"
    assert update["runtime_context"]["execution_lifecycle"]["process_started"] is False
    assert update["runtime_context"]["candidate_recovery"]["container_spool_recovered"] is False
    assert "尚未启动" in update["error_message"]


def test_candidate_recovery_retains_fence_until_task_run_terminal_is_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence()
    outbox = _outbox(tmp_path, fence)
    manager = _ContainerManager()
    harness = _Harness(outbox, manager)
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)
    monkeypatch.setattr(
        candidate_recovery,
        "intake_worker_candidates",
        lambda *_args, **_kwargs: CandidateIntakeSummary(host_durable=True),
    )
    monkeypatch.setattr(
        harness.client,
        "update_task_run",
        lambda *_args, **_kwargs: ApiResult(status_code=409, data={}),
    )

    assert harness._refresh_candidate_recovery() is False

    stored = outbox.fences()
    assert len(stored) == 1
    assert stored[0].terminal_confirmed is True
    assert manager.spool_removals == []
    assert manager.control_removals == []


@pytest.mark.parametrize("existing_execution_journal", [False, True])
def test_report_candidate_recovery_requires_strict_finish_receipt_before_fence_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_execution_journal: bool,
) -> None:
    fence = _fence()
    candidate_outbox = _outbox(tmp_path, fence)
    report_outbox = ReportFinishOutbox(tmp_path / "report-outbox")
    if existing_execution_journal:
        report_outbox.persist(_report_execution())
    client = _ReportClient()
    manager = _ContainerManager()
    harness = _Harness(
        candidate_outbox,
        manager,
        report_finish_outbox=report_outbox,
        client=client,
    )
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)
    monkeypatch.setattr(
        candidate_recovery,
        "intake_worker_candidates",
        lambda *_args, **_kwargs: CandidateIntakeSummary(host_durable=True),
    )

    assert harness._refresh_candidate_recovery() is True

    assert candidate_outbox.fences() == []
    assert len(client.updates) == 1
    update = client.updates[0][2]
    assert set(update) == {"runtime_context"}
    assert update["runtime_context"]["execution_lifecycle"] == {
        "generation": 0,
        "process_started": True,
        "termination_confirmed": True,
        "cleanup_confirmed": True,
    }
    entries = report_outbox.entries()
    assert len(entries) == 1
    finish = entries[0]
    assert isinstance(finish, PendingReportFinish)
    assert finish.status == "failed"
    assert finish.input_snapshot_artifact_id is None
    assert finish.task_run_result.error_type == "delivery_unavailable"
    assert len(client.finish_requests) == 1
    assert client.finish_requests[0]["request_sha256"] == finish.request_sha256
    # strict receipt 已权威终结 TaskRun；journal 留到 fence 删除后供普通 outbox flush 清理。
    assert manager.spool_removals == [
        ("container-id", fence.spool_path, "/tmp/cairn-prompts")
    ]


def test_report_candidate_recovery_closes_gone_execution_via_finish_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence()
    candidate_outbox = _outbox(tmp_path, fence)
    report_outbox = ReportFinishOutbox(tmp_path / "report-outbox")
    client = _ReportClient()
    harness = _Harness(
        candidate_outbox,
        _ContainerManager(
            check=CandidateExecutionCheck(
                "gone",
                "container_missing_on_same_daemon",
            )
        ),
        report_finish_outbox=report_outbox,
        client=client,
    )
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)
    monkeypatch.setattr(
        candidate_recovery,
        "intake_worker_candidates",
        lambda *_args, **_kwargs: pytest.fail("gone container has no readable spool"),
    )

    assert harness._refresh_candidate_recovery() is True

    assert candidate_outbox.fences() == []
    finish = report_outbox.entries()[0]
    assert isinstance(finish, PendingReportFinish)
    assert finish.task_run_result.error_type == "container_failed"
    assert client.updates[0][2]["runtime_context"]["candidate_recovery"]["status"] == "gone"
    assert "status" not in client.updates[0][2]


def test_report_candidate_recovery_retains_fence_without_claim_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence()
    candidate_outbox = _outbox(tmp_path, fence)
    report_outbox = ReportFinishOutbox(tmp_path / "report-outbox")
    client = _ReportClient()
    original_list = client.list_task_runs

    def list_without_claim(project_id: str) -> ApiResult:
        response = original_list(project_id)
        assert isinstance(response.data, list)
        response.data[0]["runtime_context"].pop("report_claim")
        return response

    monkeypatch.setattr(client, "list_task_runs", list_without_claim)
    harness = _Harness(
        candidate_outbox,
        _ContainerManager(),
        report_finish_outbox=report_outbox,
        client=client,
    )
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)
    monkeypatch.setattr(
        candidate_recovery,
        "intake_worker_candidates",
        lambda *_args, **_kwargs: CandidateIntakeSummary(host_durable=True),
    )

    assert harness._refresh_candidate_recovery() is False

    assert len(candidate_outbox.fences()) == 1
    assert report_outbox.entries() == []
    assert client.finish_requests == []


def test_report_candidate_recovery_replays_same_finish_after_uncertain_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence()
    candidate_outbox = _outbox(tmp_path, fence)
    report_outbox = ReportFinishOutbox(tmp_path / "report-outbox")
    report_outbox.persist(_report_execution())
    client = _ReportClient(valid_receipt=False)
    harness = _Harness(
        candidate_outbox,
        _ContainerManager(),
        report_finish_outbox=report_outbox,
        client=client,
    )
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)
    monkeypatch.setattr(
        candidate_recovery,
        "intake_worker_candidates",
        lambda *_args, **_kwargs: CandidateIntakeSummary(host_durable=True),
    )

    assert harness._refresh_candidate_recovery() is False

    stored_fences = candidate_outbox.fences()
    assert len(stored_fences) == 1
    assert stored_fences[0].terminal_confirmed is True
    entries = report_outbox.entries()
    assert len(entries) == 1
    finish = entries[0]
    assert isinstance(finish, PendingReportFinish)
    assert len(client.finish_requests) == 1
    request_sha256 = client.finish_requests[0]["request_sha256"]

    client.valid_receipt = True
    assert harness._refresh_candidate_recovery() is True

    assert candidate_outbox.fences() == []
    assert len(client.finish_requests) == 2
    assert [item["request_sha256"] for item in client.finish_requests] == [
        request_sha256,
        request_sha256,
    ]
    assert report_outbox.entries() == [finish]


def test_report_candidate_recovery_delivers_durable_output_before_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence()
    candidate_outbox = _outbox(tmp_path, fence)
    report_outbox = ReportFinishOutbox(tmp_path / "report-outbox")
    execution = _report_execution()
    output = pending_report_output(
        execution,
        content="# 已恢复报告\n\n保留原始结果。\n",
        metadata={
            "source": "report_task",
            "format": "markdown",
            "report_generation_token": execution.generation_token,
            "input_snapshot_artifact_id": execution.input_snapshot_artifact_id,
            "input_snapshot_sha256": execution.input_snapshot_sha256,
        },
    )
    report_outbox.persist(output)
    client = _ReportClient()
    harness = _Harness(
        candidate_outbox,
        _ContainerManager(),
        report_finish_outbox=report_outbox,
        client=client,
    )
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)
    monkeypatch.setattr(
        candidate_recovery,
        "intake_worker_candidates",
        lambda *_args, **_kwargs: CandidateIntakeSummary(host_durable=True),
    )

    assert harness._refresh_candidate_recovery() is True

    assert candidate_outbox.fences() == []
    assert len(client.artifact_requests) == 1
    entries = report_outbox.entries()
    assert len(entries) == 1
    finish = entries[0]
    assert isinstance(finish, PendingReportFinish)
    assert finish.status == "ready"
    assert finish.artifact_id == "report-artifact-1"
    assert client.finish_requests[0]["status"] == "ready"


def test_candidate_recovery_retains_terminal_fence_on_permanent_capture_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence()
    outbox = _outbox(tmp_path, fence)
    manager = _ContainerManager()
    harness = _Harness(outbox, manager)
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)
    monkeypatch.setattr(
        candidate_recovery,
        "intake_worker_candidates",
        lambda *_args, **_kwargs: CandidateIntakeSummary(
            host_durable=True,
            pending=1,
            integrity_error="candidate_receipt_conflict",
        ),
    )

    assert harness._refresh_candidate_recovery() is False

    stored = outbox.fences()
    assert len(stored) == 1
    assert stored[0].terminal_confirmed is True
    assert manager.spool_removals == []
    assert manager.control_removals == []


def test_candidate_recovery_retains_resources_when_exact_fence_removal_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence(terminal_confirmed=True)
    outbox = _outbox(tmp_path, fence)
    manager = _ContainerManager()
    harness = _Harness(outbox, manager)
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)
    monkeypatch.setattr(
        candidate_recovery,
        "intake_worker_candidates",
        lambda *_args, **_kwargs: CandidateIntakeSummary(host_durable=True),
    )
    monkeypatch.setattr(outbox, "remove_fence", lambda _fence: False)

    assert harness._refresh_candidate_recovery() is False

    stored = outbox.fences()
    assert len(stored) == 1
    assert stored[0].recovery_status == "stopped"
    assert stored[0].container_spool_recovered is True
    assert manager.spool_removals == []
    assert manager.control_removals == []


def test_candidate_recovery_waits_for_every_run_fence_and_retries_original_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = replace(_fence(), container_id="container-a", exec_id="exec-a")
    second_id = "abcdef12-1234-4234-8234-123456789abc"
    second = replace(
        _fence(), capture_id=second_id,
        spool_path=f"/tmp/cairn-prompts/pi-candidates-{second_id}.bin",
        container_id="container-b", exec_id="exec-b",
    )
    outbox = _outbox(tmp_path, first)
    outbox.persist_fence(second)
    manager = _ContainerManager()
    client = _Client()
    list_task_runs = client.list_task_runs

    def with_all_ids(project_id: str) -> ApiResult:
        response = list_task_runs(project_id)
        response.data[0]["runtime_context"]["execution_container_ids"] = [
            "container-b", "container-a",
        ]
        return response

    client.list_task_runs = with_all_ids
    update_task_run = client.update_task_run
    accepted = False

    def update_with_retry(*args: object, **kwargs: object) -> ApiResult:
        update_task_run(*args, **kwargs)
        return ApiResult(status_code=200 if accepted else 503)

    client.update_task_run = update_with_retry
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)
    intakes: list[str] = []

    def intake(*args: object, **_kwargs: object) -> CandidateIntakeSummary:
        intakes.append(args[2])
        return CandidateIntakeSummary(host_durable=True)

    monkeypatch.setattr(candidate_recovery, "intake_worker_candidates", intake)

    def inspect(**kwargs: str | None) -> CandidateExecutionCheck:
        manager.inspections.append(kwargs)
        if kwargs["exec_id"] == "exec-a":
            return CandidateExecutionCheck("gone", "container_missing_on_same_daemon")
        if kwargs["exec_id"] == "exec-b" and not ready_second:
            return CandidateExecutionCheck("unknown", "docker_daemon_unavailable")
        return CandidateExecutionCheck("stopped", "exec_terminal_descendants_drained")

    ready_second = False
    manager.candidate_execution_status = inspect
    harness = _Harness(outbox, manager, client=client)
    assert harness._refresh_candidate_recovery() is False
    assert {item.capture_id for item in outbox.fences()} == {
        first.capture_id, second.capture_id,
    }
    assert client.updates == []
    assert manager.spool_removals == []
    assert intakes == []

    ready_second = True
    harness._candidate_recovery_retry_states.clear()
    assert harness._refresh_candidate_recovery() is False
    assert len(client.updates) == 1
    assert client.updates[0][2]["runtime_context"]["execution_container_ids"] == [
        "container-b", "container-a",
    ]
    assert client.updates[0][2]["runtime_context"]["execution_lifecycle"] == {
        "generation": 0, "process_started": True,
        "termination_confirmed": True, "cleanup_confirmed": True,
    }
    assert {item.recovery_status for item in outbox.fences()} == {"stopped", "gone"}
    assert manager.spool_removals == []
    assert intakes == ["container-b"]

    # 重启后以 durable ready fence 重放原 Run ACK，不复启模型，也不再次读取 spool。
    accepted = True
    restarted = _Harness(outbox, manager, client=client)
    assert restarted._refresh_candidate_recovery() is True
    assert outbox.fences() == []
    assert len(client.updates) == 2
    assert intakes == ["container-b"]
    assert len(manager.inspections) == 3
    assert manager.spool_removals == [
        ("container-b", second.spool_path, "/tmp/cairn-prompts"),
    ]


def test_candidate_recovery_cannot_ack_unproved_historical_execution_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence()
    outbox = _outbox(tmp_path, fence)
    manager = _ContainerManager()
    client = _Client()
    list_task_runs = client.list_task_runs

    def with_unproved_id(project_id: str) -> ApiResult:
        response = list_task_runs(project_id)
        response.data[0]["runtime_context"]["execution_container_ids"] = [
            "container-id", "other-unproved-container",
        ]
        return response

    client.list_task_runs = with_unproved_id
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)
    monkeypatch.setattr(
        candidate_recovery, "intake_worker_candidates",
        lambda *_args, **_kwargs: CandidateIntakeSummary(host_durable=True),
    )

    assert _Harness(outbox, manager, client=client)._refresh_candidate_recovery() is False
    assert client.updates == []
    assert len(outbox.fences()) == 1
    assert outbox.fences()[0].recovery_status == "stopped"
    assert manager.spool_removals == []


def test_prior_group_only_ack_cannot_cover_missing_execution_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence()
    outbox = _outbox(tmp_path, fence)
    manager = _ContainerManager()
    client = _Client()
    original_list = client.list_task_runs

    def with_old_ack(project_id: str) -> ApiResult:
        response = original_list(project_id)
        context = response.data[0]["runtime_context"]
        context["execution_container_ids"] = ["container-id", "older-unproved-container"]
        context["execution_lifecycle"]["termination_confirmed"] = True
        context["candidate_recovery"] = {"status": "stopped", "reason": "exec_terminal_process_group_drained"}
        return response

    client.list_task_runs = with_old_ack
    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", _successful_replay)
    monkeypatch.setattr(
        candidate_recovery, "intake_worker_candidates",
        lambda *_args, **_kwargs: CandidateIntakeSummary(host_durable=True),
    )

    harness = _Harness(outbox, manager, client=client)
    assert harness._refresh_candidate_recovery() is False
    assert client.updates == []
    assert outbox.fences()[0].recovery_status == "stopped"

    # 新版完整 ACK 可以覆盖先前已释放的子 fence，重启时不覆写原恢复审计。
    def with_new_ack(project_id: str) -> ApiResult:
        response = with_old_ack(project_id)
        response.data[0]["runtime_context"]["candidate_recovery"]["physical_proof_version"] = 2
        return response

    client.list_task_runs = with_new_ack
    restarted = _Harness(CandidateCaptureOutbox(outbox.root, owner_id="dispatcher-1"), manager, client=client)
    assert restarted._refresh_candidate_recovery() is True
    assert outbox.fences() == []
    assert client.updates[0][2]["runtime_context"]["candidate_recovery"]["physical_proof_version"] == 2


def test_candidate_recovery_fails_closed_on_invalid_backlog_without_fences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outbox = _outbox(tmp_path)
    harness = _Harness(outbox, _ContainerManager())
    monkeypatch.setattr(
        candidate_recovery,
        "replay_candidate_outbox",
        lambda *_args, **_kwargs: CandidateIntakeSummary(
            integrity_error="candidate_outbox_invalid"
        ),
    )

    assert harness._refresh_candidate_recovery() is False
    assert harness._candidate_project_blocked("unrelated-project") is True
    assert harness._candidate_container_blocked("unrelated-container") is True


def test_candidate_recovery_isolates_rejected_candidate_to_its_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outbox = _outbox(tmp_path)
    harness = _Harness(outbox, _ContainerManager())
    monkeypatch.setattr(
        candidate_recovery,
        "replay_candidate_outbox",
        lambda *_args, **_kwargs: CandidateIntakeSummary(
            host_durable=True,
            pending=1,
            integrity_error="candidate_capture_rejected",
            blocked_project_ids={"project-1"},
        ),
    )

    assert harness._refresh_candidate_recovery() is True
    assert harness._candidate_project_blocked("project-1") is True
    assert harness._candidate_project_blocked("unrelated-project") is False
    assert harness._candidate_container_blocked("unrelated-container") is False


def test_candidate_recovery_allows_transport_pending_backlog_without_fences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outbox = _outbox(tmp_path)
    harness = _Harness(outbox, _ContainerManager())
    monkeypatch.setattr(
        candidate_recovery,
        "replay_candidate_outbox",
        lambda *_args, **_kwargs: CandidateIntakeSummary(
            host_durable=True,
            pending=1,
        ),
    )

    assert harness._refresh_candidate_recovery() is True
    assert harness._candidate_project_blocked("project-1") is False


def test_candidate_recovery_skips_project_owned_by_active_local_future(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = _fence()
    outbox = _outbox(tmp_path, fence)
    manager = _ContainerManager()
    harness = _Harness(outbox, manager)
    harness.futures = {
        "active": SimpleNamespace(project_id=fence.project_id),
    }
    replay_calls: list[dict[str, object]] = []

    def replay(*_args: object, **kwargs: object) -> CandidateIntakeSummary:
        replay_calls.append(kwargs)
        return CandidateIntakeSummary(host_durable=True)

    monkeypatch.setattr(candidate_recovery, "replay_candidate_outbox", replay)
    monkeypatch.setattr(
        candidate_recovery,
        "intake_worker_candidates",
        lambda *_args, **_kwargs: pytest.fail("active future owns candidate intake"),
    )

    assert harness._refresh_candidate_recovery() is False
    assert outbox.fences() == [fence]
    assert manager.inspections == []
    assert replay_calls == [{
        "max_entries": 16,
        "excluded_project_ids": {fence.project_id},
        "excluded_task_runs": set(),
    }]


class _SchedulerHarness(
    CandidateRecoveryMixin,
    DispatchCycleMixin,
    TaskLifecycleMixin,
    StaleReconcileMixin,
    CleanupMixin,
):
    def __init__(self) -> None:
        self._candidate_fence_invalid = False
        self._candidate_fenced_projects: set[str] = set()
        self._candidate_fenced_task_runs: set[tuple[str, str]] = set()
        self._candidate_fenced_containers: set[str] = set()
        self.decisions: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.logs: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _observe_decision(self, *args: object, **kwargs: object) -> None:
        self.decisions.append((args, kwargs))

    def _log_changed(self, *args: object, **kwargs: object) -> None:
        self.logs.append((args, kwargs))


def _forbidden(label: str):
    def fail(*_args: object, **_kwargs: object):
        pytest.fail(label)

    return fail


def test_candidate_fence_blocks_project_and_report_admission_before_server_reads() -> None:
    harness = _SchedulerHarness()
    harness._set_candidate_fence_snapshot([_fence()], invalid=False)
    harness.client = SimpleNamespace(
        get_project=_forbidden("candidate fence must block project lookup")
    )
    harness.container_manager = SimpleNamespace(
        container_name=_forbidden("candidate fence must block container lookup")
    )
    harness.report_finish_outbox = SimpleNamespace(
        has_project=_forbidden("candidate fence must precede report claim state")
    )

    assert harness._try_dispatch_project(
        SimpleNamespace(id="project-1")
    ) is False
    assert harness._try_dispatch_completed_report("project-1") is False

    assert harness.decisions == [
        (("project-1", "cleanup_pending"), {}),
        (("project-1", "cleanup_pending"), {"task_type": "report"}),
    ]


@pytest.mark.parametrize(
    ("method", "args", "task_type"),
    [
        ("_dispatch_bootstrap", (SimpleNamespace(id="intent-1"),), "bootstrap"),
        (
            "_dispatch_explore",
            ("version: 1", SimpleNamespace(id="intent-1")),
            "explore",
        ),
    ],
)
def test_candidate_fence_blocks_direct_model_dispatch_entry_points(
    method: str,
    args: tuple[object, ...],
    task_type: str,
) -> None:
    harness = _SchedulerHarness()
    harness._set_candidate_fence_snapshot([_fence()], invalid=False)
    harness.container_manager = SimpleNamespace(
        ensure_running=_forbidden("candidate fence must block model container start")
    )
    project = SimpleNamespace(project=SimpleNamespace(id="project-1"))

    assert getattr(harness, method)(project, *args) is False
    assert harness.decisions == [
        (("project-1", "cleanup_pending"), {"task_type": task_type})
    ]


def test_stale_reconcile_excludes_fenced_run_until_fence_release() -> None:
    harness = _SchedulerHarness()
    harness._set_candidate_fence_snapshot([_fence()], invalid=False)
    harness.futures = {}
    harness.report_finish_outbox = SimpleNamespace(entries=lambda: [])
    harness.dispatcher_id = "dispatcher-1"
    event_queries: list[tuple[str, list[str]]] = []
    updates: list[tuple[str, str]] = []

    class Client:
        def list_task_runs(self, project_id: str) -> ApiResult:
            return ApiResult(
                status_code=200,
                data=[
                    {"id": "run-1", "status": "running"},
                    {"id": "run-2", "status": "running"},
                ],
            )

        def list_task_run_command_events(
            self,
            project_id: str,
            task_run_ids: list[str],
        ) -> ApiResult:
            event_queries.append((project_id, task_run_ids))
            return ApiResult(status_code=200, data=[])

        def update_task_run(
            self,
            project_id: str,
            task_run_id: str,
            **_kwargs: object,
        ) -> ApiResult:
            updates.append((project_id, task_run_id))
            return ApiResult(status_code=200, data={})

    harness.client = Client()
    summary = SimpleNamespace(id="project-1")

    harness._reconcile_stale_running_task_runs([summary])

    assert event_queries == [("project-1", ["run-2"])]
    assert updates == [("project-1", "run-2")]

    harness._set_candidate_fence_snapshot([], invalid=False)
    event_queries.clear()
    updates.clear()
    harness.client.list_task_runs = lambda _project_id: ApiResult(
        status_code=200,
        data=[{"id": "run-1", "status": "running"}],
    )

    harness._reconcile_stale_running_task_runs([summary])

    assert event_queries == [("project-1", ["run-1"])]
    assert updates == [("project-1", "run-1")]


class _ImmediateFailureExecutor:
    def submit(self, *_args: object, **_kwargs: object) -> None:
        pytest.fail("candidate-fenced cleanup must not be queued")


class _DeferredExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []
        self.future: Future[bool] = Future()

    def submit(self, function, *args: object, **kwargs: object) -> Future[bool]:
        self.calls.append((function, args, kwargs))
        return self.future


def _cleanup_harness() -> tuple[_SchedulerHarness, SimpleNamespace]:
    harness = _SchedulerHarness()
    harness.futures = {}
    harness.cleanup_futures = {}
    harness._cleanup_pending = set()
    harness._inactive_cleanup_done = {}
    harness._pause_cleanup_runs = {}
    harness._pause_cleanup_confirmations = {}
    harness.dispatcher_id = "dispatcher-1"
    harness.client = SimpleNamespace(
        get_assessment_report_finalization=lambda _project_id: ApiResult(
            status_code=404,
            data=None,
        ),
        list_task_runs=_forbidden(
            "candidate-fenced paused cleanup must not query task runs"
        ),
    )
    manager = SimpleNamespace(
        container_name=lambda _project_id: "cairn-project-1",
        managed_container_names=lambda: ["cairn-project-1"],
        project_id_from_container_name=_forbidden(
            "candidate-fenced orphan cleanup must not resolve project"
        ),
        needs_completed_cleanup=_forbidden(
            "candidate-fenced completed cleanup must stop before eligibility"
        ),
        needs_stopped_cleanup=_forbidden(
            "candidate-fenced stopped cleanup must stop before eligibility"
        ),
        needs_orphan_cleanup=_forbidden(
            "candidate-fenced orphan cleanup must stop before eligibility"
        ),
        cleanup_completed=_forbidden("candidate-fenced container was cleaned"),
        cleanup_stopped=_forbidden("candidate-fenced container was cleaned"),
        cleanup_paused=_forbidden("candidate-fenced container was cleaned"),
        cleanup_orphan=_forbidden("candidate-fenced container was cleaned"),
    )
    harness.container_manager = manager
    harness.cleanup_executor = _ImmediateFailureExecutor()
    harness._set_candidate_fence_snapshot([_fence()], invalid=False)
    return harness, manager


def test_candidate_fence_blocks_destructive_cleanup_paths() -> None:
    harness, _manager = _cleanup_harness()

    harness._cleanup_completed_containers(
        [SimpleNamespace(id="project-1", status="completed")]
    )
    harness._cleanup_stopped_containers(
        [SimpleNamespace(id="project-1", status="stopped")]
    )
    harness._reconcile_orphan_containers([])

    assert harness.cleanup_futures == {}
    assert harness._cleanup_pending == set()
    assert harness._inactive_cleanup_done == {}


def test_cleanup_worker_rechecks_candidate_fence_after_queueing() -> None:
    harness, manager = _cleanup_harness()
    harness._set_candidate_fence_snapshot([], invalid=False)
    executor = _DeferredExecutor()
    harness.cleanup_executor = executor
    cleanup_calls: list[str] = []
    manager.needs_completed_cleanup = lambda _project_id: True
    manager.cleanup_completed = lambda project_id: cleanup_calls.append(project_id) or True

    harness._cleanup_completed_containers(
        [SimpleNamespace(id="project-1", status="completed")]
    )

    assert len(executor.calls) == 1
    function, args, kwargs = executor.calls[0]
    harness._set_candidate_fence_snapshot([_fence()], invalid=False)
    result = function(*args, **kwargs)
    executor.future.set_result(result)
    harness._reap_cleanup_futures()

    assert result is False
    assert cleanup_calls == []
    assert harness._cleanup_pending == set()
    assert harness._inactive_cleanup_done == {}
