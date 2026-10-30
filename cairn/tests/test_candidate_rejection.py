from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
import requests

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.containers import CandidateExecutionCheck
from cairn.dispatcher.scheduler import candidate_recovery
from cairn.dispatcher.tasks.candidate_capture_outbox import (
    PERMANENT_CANDIDATE_REJECTIONS,
    CandidateCaptureOutbox,
)
from cairn.dispatcher.tasks.candidate_intake import (
    decode_candidate_spool,
    deliver_candidate_entries,
    replay_candidate_outbox,
)
from cairn.dispatcher.tasks.report_finish_outbox import (
    FINISH_PROTOCOL,
    OUTBOX_PROTOCOL,
    PendingReportFinish,
    ReportFinishOutbox,
    canonical_json,
    failed_finish_for_recovery,
    finish_request_sha256,
)
from test_candidate_intake import (
    _BINDING_SHA256,
    _CAPTURE_ID,
    _CandidateClient,
    _MaterializedCandidateSpool,
    _candidate_manifest,
    _decode,
    _frame,
    _intake,
    _outcome_manifest,
    _receipt,
)
from test_candidate_recovery import (
    _ContainerManager as _RecoveryContainerManager,
    _Harness,
    _ReportClient,
    _fence,
    _outbox,
)


_REJECTION_CODE = "candidate_binding_digest_mismatch"
_RAW = b'{\r\n"accepted": tru, "note": "\xe9\x9b\xaa"\r\n}'


def _spool(*, phase: str = "explore_execute") -> bytes:
    return _frame(_candidate_manifest(_RAW, phase=phase), _RAW) + _frame(
        _outcome_manifest(), b"",
    )


def _durable_bytes(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {path.name: path.read_bytes() for path in root.iterdir() if path.name != ".lock"}


class _RecoverySpoolManager(_RecoveryContainerManager):
    def __init__(self, path: Path, *, check: CandidateExecutionCheck | None = None) -> None:
        super().__init__(check=check)
        self.path = path
        self.spool_reads: list[tuple[str, str]] = []

    def materialize_file(self, container_name: str, path: str) -> _MaterializedCandidateSpool:
        assert container_name == "container-id"
        assert path == _fence().spool_path
        self.spool_reads.append((container_name, path))
        return _MaterializedCandidateSpool(self.path)


def _rejected_recovery(tmp_path: Path, *, terminal: bool = False, report: bool = False):
    fence = replace(
        _fence(terminal_confirmed=terminal),
        binding_sha256=_BINDING_SHA256,
        phase="report_execute" if report else "explore_execute",
    )
    spool = _spool(phase=fence.phase)
    path = tmp_path / "candidate-spool.bin"
    path.write_bytes(spool)
    entries = decode_candidate_spool(
        spool, fence.candidate_spool(), project_id=fence.project_id,
        task_run_id=fence.task_run_id, phase=fence.phase, session=fence.session,
    )
    outbox = _outbox(tmp_path, fence)
    for entry in entries:
        outbox.persist(entry)
    client = _CandidateClient([ApiResult(status_code=409, data={"detail": _REJECTION_CODE})])
    summary = deliver_candidate_entries(client, outbox, entries)
    assert summary.rejected == 2
    assert summary.received == summary.replayed == summary.pending == 0
    return fence, outbox, entries, _RecoverySpoolManager(path)


@pytest.mark.parametrize("code", [
    "candidate_phase_mismatch", "candidate_session_mismatch",
    "candidate_binding_digest_mismatch", "candidate_result_schema_digest_mismatch",
])
def test_allowlisted_409_preserves_exact_frames_and_stops_outcomes_after_restart(
    tmp_path: Path, code: str,
) -> None:
    spool = _spool()
    entries = _decode(spool)
    client = _CandidateClient([ApiResult(status_code=409, data={"detail": code})])

    summary, outbox = _intake(tmp_path, spool, client)

    assert summary.host_durable is True
    assert summary.rejected == 2
    assert summary.received == summary.replayed == summary.pending == 0
    assert summary.integrity_error == code
    assert summary.rejection_http_status == 409
    assert outbox.entries() == []
    assert outbox.entries(include_rejected=True) == entries
    assert client.requests == [("project-1", "run-1", entries[0].manifest_bytes, _RAW)]
    rejection, = outbox.rejections()
    assert rejection.code == code
    assert rejection.status_code == 409
    assert rejection.request_sha256 == entries[0].request_sha256
    assert rejection.matches(entries[0]) and rejection.matches(entries[1])
    before = _durable_bytes(outbox.root)

    restarted = CandidateCaptureOutbox(outbox.root, owner_id="dispatcher-1")
    no_http = _CandidateClient([])
    replay = replay_candidate_outbox(no_http, restarted)
    stale_replay = deliver_candidate_entries(no_http, restarted, entries)

    assert replay.pending == replay.received == replay.replayed == 0
    assert stale_replay.rejected == 2
    assert stale_replay.received == stale_replay.replayed == 0
    assert no_http.requests == []
    assert restarted.rejections() == [rejection]
    assert restarted.entries(include_rejected=True) == entries
    assert b"".join(entry.frame_bytes for entry in restarted.entries(include_rejected=True)) == spool
    assert _durable_bytes(restarted.root) == before


@pytest.mark.parametrize(
    ("status", "code"),
    [(409, "candidate_unknown_conflict"), (401, _REJECTION_CODE), (403, _REJECTION_CODE)],
    ids=["unknown-409", "unauthenticated", "forbidden"],
)
def test_unknown_conflict_and_auth_failure_remain_pending_after_restart(
    tmp_path: Path, status: int, code: str,
) -> None:
    entries = _decode(_spool())
    response = ApiResult(status_code=status, data={"detail": code})
    client = _CandidateClient([response])

    summary, outbox = _intake(tmp_path, _spool(), client)

    assert summary.pending == 2
    assert summary.rejected == summary.received == summary.replayed == 0
    assert summary.integrity_error == "candidate_capture_rejected"
    assert summary.rejection_http_status == status
    assert summary.blocked_project_ids == {"project-1"}
    assert outbox.entries() == entries
    assert outbox.rejections() == []
    restarted = CandidateCaptureOutbox(outbox.root, owner_id="dispatcher-1")
    retry_client = _CandidateClient([response])

    replay = replay_candidate_outbox(retry_client, restarted)

    assert replay.pending == 2
    assert replay.rejected == 0
    assert restarted.entries() == entries
    assert restarted.rejections() == []
    assert retry_client.requests == client.requests
    assert len(retry_client.requests) == 1


def test_durable_rejection_does_not_release_physically_unknown_execution(tmp_path: Path) -> None:
    fence, outbox, entries, manager = _rejected_recovery(tmp_path)
    manager.check = CandidateExecutionCheck("unknown", "exec_inspect_unavailable")
    harness = _Harness(outbox, manager)

    assert harness._refresh_candidate_recovery() is False

    assert outbox.fences() == [fence]
    assert outbox.entries(include_rejected=True) == entries
    assert len(manager.inspections) == 1
    assert manager.spool_reads == manager.spool_removals == manager.control_removals == []
    assert manager.terminations == harness.client.updates == []
    assert harness._candidate_fenced_projects == {"project-1"}


def test_v2_rejection_recovery_releases_owned_fence_without_forging_cleanup(tmp_path: Path) -> None:
    fence, outbox, entries, manager = _rejected_recovery(tmp_path, terminal=True)
    assert fence.terminal_proof_version == 2
    manager.check = CandidateExecutionCheck("unknown", "exec_inspect_unavailable")
    harness = _Harness(outbox, manager)

    assert harness._refresh_candidate_recovery() is True

    assert outbox.fences() == []
    assert outbox.entries(include_rejected=True) == entries
    assert outbox.entries() == []
    assert manager.inspections == []
    assert manager.spool_reads == [("container-id", fence.spool_path)]
    assert manager.spool_removals == [("container-id", fence.spool_path, "/tmp/cairn-prompts")]
    assert len(manager.control_removals) == 1
    update, = harness.client.updates
    assert update[2]["status"] == "failed"
    assert update[2]["error_type"] == "candidate_integrity"
    assert _REJECTION_CODE in update[2]["error_message"]
    assert "HTTP 409" in update[2]["error_message"]
    context = update[2]["runtime_context"]
    assert context["execution_lifecycle"] == {
        "generation": 0, "process_started": True,
        "termination_confirmed": True, "cleanup_confirmed": False,
    }
    assert context["candidate_recovery"]["physical_proof_version"] == 2
    assert context["candidate_recovery"]["delivery_rejection"]["code"] == _REJECTION_CODE
    assert context["candidate_recovery"]["delivery_rejection"]["http_status"] == 409


@pytest.mark.parametrize("owner_source", ["server", "dispatcher"])
def test_v2_rejection_cannot_release_fence_owned_by_another_dispatcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner_source: str,
) -> None:
    _frozen, outbox, entries, manager = _rejected_recovery(tmp_path, terminal=True)
    harness = _Harness(outbox, manager)
    if owner_source == "dispatcher":
        harness.dispatcher_id = "dispatcher-other"
    else:
        original_list = harness.client.list_task_runs

        def list_other_owner(project_id: str) -> ApiResult:
            response = original_list(project_id)
            response.data[0]["runtime_context"]["dispatcher_id"] = "dispatcher-other"
            return response

        monkeypatch.setattr(harness.client, "list_task_runs", list_other_owner)

    assert harness._refresh_candidate_recovery() is False

    stored, = outbox.fences()
    assert stored.terminal_proof_version == 2
    assert stored.recovery_status == "stopped"
    assert outbox.entries(include_rejected=True) == entries
    assert harness.client.updates == []
    assert manager.spool_removals == manager.control_removals == []


@pytest.mark.parametrize("unknown", ["409", "receipt-conflict"])
def test_permanent_rejection_cannot_mask_unknown_conflict_or_release_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unknown: str,
) -> None:
    second_raw = b'{"accepted":true}'
    second_key = f"{_CAPTURE_ID}:2:0"
    spool = _spool() + _frame(
        _candidate_manifest(
            second_raw, assistant_turn=2, producer_candidate_key=second_key,
            parent_producer_candidate_key=f"{_CAPTURE_ID}:1:0",
        ), second_raw,
    ) + _frame(_outcome_manifest(producer_candidate_key=second_key), b"")
    entries = _decode(spool)
    response = (
        ApiResult(status_code=409, data={"detail": "candidate_unknown_conflict"})
        if unknown == "409" else _receipt(entries[2], request_sha256="0" * 64)
    )
    expected_error = "candidate_capture_rejected" if unknown == "409" else "candidate_receipt_conflict"
    client = _CandidateClient([
        ApiResult(status_code=409, data={"detail": _REJECTION_CODE}), response,
    ])

    summary, outbox = _intake(tmp_path, spool, client)

    assert summary.rejected == 2
    assert summary.pending == 2
    assert summary.integrity_error == expected_error
    assert summary.integrity_error not in PERMANENT_CANDIDATE_REJECTIONS
    assert summary.blocked_project_ids == {"project-1"}
    assert outbox.entries() == entries[2:]
    fence = replace(_fence(terminal_confirmed=True), binding_sha256=_BINDING_SHA256)
    outbox.persist_fence(fence)
    manager = _RecoverySpoolManager(tmp_path / "candidate-spool.bin")
    harness = _Harness(outbox, manager)
    retry_client = _CandidateClient([response, response])
    monkeypatch.setattr(harness.client, "capture_candidate", retry_client.capture_candidate, raising=False)

    assert harness._refresh_candidate_recovery() is False

    assert outbox.fences() == [fence]
    assert outbox.entries(include_rejected=True) == entries
    assert harness.client.updates == []
    assert manager.spool_removals == manager.control_removals == []
    assert len(retry_client.requests) == 2
    assert all(request[2] == entries[2].manifest_bytes for request in retry_client.requests)


class _TerminalReportClient(_ReportClient):
    def __init__(self, receipt: ApiResult) -> None:
        super().__init__()
        self.receipt = receipt
        self.run_status = "failed"
        self.receipt_reads: list[tuple[str, str]] = []

    def list_task_runs(self, project_id: str) -> ApiResult:
        response = super().list_task_runs(project_id)
        response.data[0]["status"] = self.run_status
        return response

    def get_assessment_report_finish_receipt(self, project_id: str, task_run_id: str) -> ApiResult:
        self.receipt_reads.append((project_id, task_run_id))
        return self.receipt


def _failed_receipt(**updates: object) -> ApiResult:
    timestamp = "2026-10-22T00:00:00Z"
    data = {
        "protocol": FINISH_PROTOCOL, "project_id": "project-1", "task_run_id": "run-1",
        "generation_token": "generation-1", "status": "failed",
        "generation_started_at": timestamp, "created_at": timestamp, "updated_at": timestamp,
        "accepted_at": timestamp, "completed_at": timestamp,
        "request_sha256": sha256(b"original-server-failed-finish").hexdigest(),
        "finish_disposition": "accepted", "last_error_code": _REJECTION_CODE,
        "last_error": "历史执行身份不一致，原失败记录不改写",
    }
    data.update(updates)
    return ApiResult(status_code=200, data=data)


@pytest.mark.parametrize("conflicting_journal", [False, True])
def test_terminal_report_reads_existing_failure_and_preserves_conflicting_local_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflicting_journal: bool,
) -> None:
    _frozen, outbox, entries, manager = _rejected_recovery(tmp_path, terminal=True, report=True)
    report_outbox = ReportFinishOutbox(tmp_path / "report-outbox")
    local_entries = []
    if conflicting_journal:
        local = failed_finish_for_recovery(
            project_id="project-1", authenticated_dispatcher_id="dispatcher-1", worker="worker-1",
            claim_sha256=sha256(b"report-claim").hexdigest(), generation_token="generation-1",
            task_run_id="run-1", exit_code=0, error_type="delivery_unavailable",
            error_message="另一个本地失败请求，不得冒充原收据",
        )
        report_outbox.persist(local)
        local_entries.append(local)
    before = _durable_bytes(report_outbox.root)
    receipt = _failed_receipt()
    if conflicting_journal:
        assert local.request_sha256 != receipt.data["request_sha256"]
    client = _TerminalReportClient(receipt)
    harness = _Harness(outbox, manager, client=client, report_finish_outbox=report_outbox)
    monkeypatch.setattr(
        candidate_recovery, "failed_finish_for_recovery",
        lambda **_kwargs: pytest.fail("已有 terminal receipt，不得重新拼失败请求"),
    )

    assert harness._refresh_candidate_recovery() is True

    assert outbox.fences() == []
    assert outbox.entries(include_rejected=True) == entries
    assert client.receipt_reads == [("project-1", "run-1")]
    assert client.finish_requests == client.artifact_requests == []
    assert report_outbox.entries() == local_entries
    assert _durable_bytes(report_outbox.root) == before
    assert receipt.data["last_error"] == "历史执行身份不一致，原失败记录不改写"
    assert client.updates[0][2]["runtime_context"]["execution_lifecycle"]["cleanup_confirmed"] is False


@pytest.mark.parametrize("unconfirmed", ["generation", "status", "malformed", "transport"])
def test_terminal_report_without_confirmed_receipt_retains_fence_without_rebuilding_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unconfirmed: str,
) -> None:
    _frozen, outbox, _entries, manager = _rejected_recovery(tmp_path, terminal=True, report=True)
    response = _failed_receipt()
    if unconfirmed == "generation":
        response = _failed_receipt(generation_token="other-generation")
    elif unconfirmed == "status":
        response = _failed_receipt(status="ready")
    elif unconfirmed == "malformed":
        response = ApiResult(status_code=200, data={"status": "failed"})
    client = _TerminalReportClient(response)
    if unconfirmed == "transport":
        def unavailable(*_args: object) -> ApiResult:
            raise requests.ConnectionError("receipt unavailable")

        monkeypatch.setattr(client, "get_assessment_report_finish_receipt", unavailable)
    report_outbox = ReportFinishOutbox(tmp_path / "report-outbox")
    harness = _Harness(outbox, manager, client=client, report_finish_outbox=report_outbox)
    monkeypatch.setattr(
        candidate_recovery, "failed_finish_for_recovery",
        lambda **_kwargs: pytest.fail("terminal receipt 未确认，不得重拼替代请求"),
    )

    assert harness._refresh_candidate_recovery() is False

    assert len(outbox.fences()) == 1
    assert report_outbox.entries() == []
    assert client.finish_requests == []
    assert manager.spool_removals == manager.control_removals == []


def test_terminal_report_without_receipt_requires_strict_post_ack_before_fence_release(tmp_path: Path) -> None:
    _frozen, outbox, entries, manager = _rejected_recovery(tmp_path, terminal=True, report=True)
    client = _TerminalReportClient(ApiResult(status_code=404))
    client.run_status = "cancelled"
    client.valid_receipt = False
    report_outbox = ReportFinishOutbox(tmp_path / "report-outbox")
    harness = _Harness(outbox, manager, client=client, report_finish_outbox=report_outbox)

    assert harness._refresh_candidate_recovery() is False

    assert len(outbox.fences()) == 1
    assert outbox.entries(include_rejected=True) == entries
    assert client.receipt_reads == [("project-1", "run-1")]
    finish, = report_outbox.entries()
    assert isinstance(finish, PendingReportFinish)
    assert finish.status == "failed"
    assert client.finish_requests[0]["request_sha256"] == finish.request_sha256
    assert manager.spool_removals == manager.control_removals == []


@pytest.mark.parametrize("http_status", [None, 409], ids=["historical-without-status", "new-409"])
def test_worker_failure_finish_roundtrip_preserves_exact_hash_and_explicit_http_status(
    tmp_path: Path, http_status: int | None,
) -> None:
    failure = {
        "origin": "candidate_capture", "code": _REJECTION_CODE, "phase": "report_execute",
        "error_type": "candidate_integrity", "message": "执行身份不一致，原件已保留",
        "path": None, "exit_code": 1, "issue": None, "submission_attempts": 1,
        "repair_rounds": 0, "max_repair_rounds": 2,
    }
    if http_status is not None:
        failure["http_status"] = http_status
    values = {
        "project_id": "project-1", "authenticated_dispatcher_id": "dispatcher-1",
        "worker": "worker-1", "claim_sha256": sha256(b"report-claim").hexdigest(),
        "generation_token": "generation-1", "task_run_id": "run-1", "status": "failed",
        "artifact_id": None, "artifact_sha256": None,
        "input_snapshot_artifact_id": None, "input_snapshot_sha256": None,
        "task_run_result": {
            "status": "failed", "exit_code": 1, "error_type": "candidate_integrity",
            "error_message": failure["message"], "worker_failure": failure,
        },
    }
    original_hash = finish_request_sha256(**values)
    original = canonical_json({"protocol": OUTBOX_PROTOCOL, **values, "request_sha256": original_hash})

    entry = PendingReportFinish.model_validate_json(original)
    outbox = ReportFinishOutbox(tmp_path / "report-outbox")
    outbox.persist(entry)
    before = _durable_bytes(outbox.root)
    restarted = ReportFinishOutbox(outbox.root)
    restored, = restarted.entries()

    assert isinstance(restored, PendingReportFinish)
    assert restored.request_sha256 == original_hash
    assert canonical_json(restored.model_dump(mode="json")) == original
    assert restored.task_run_result.worker_failure.model_dump(mode="json") == failure
    assert ("http_status" in restored.task_run_result.worker_failure.model_dump(mode="json")) is (http_status is not None)
    assert _durable_bytes(restarted.root) == before
