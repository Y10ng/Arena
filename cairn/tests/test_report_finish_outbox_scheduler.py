from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.scheduler.dispatch_cycle import DispatchCycleMixin
from cairn.dispatcher.scheduler.report_finish_outbox import ReportFinishOutboxMixin
from cairn.dispatcher.tasks.report_finish_outbox import (
    PendingReportExecution,
    PendingReportFinish,
    PendingReportOutput,
    ReportFinishOutbox,
    ReportFinishOutboxConflict,
    failed_finish_fallback,
    finish_request_sha256,
    pending_report_execution,
    pending_report_output,
    ready_finish_for_output,
    recover_report_artifact_id,
)
from cairn.server.models import AssessmentReportTaskRunResult


def _entry(
    *,
    project_id: str = "project-1",
    task_run_id: str = "run-1",
    error_message: str = "stable failure",
    status: str = "failed",
):
    ready = status == "ready"
    result = AssessmentReportTaskRunResult(
        status="succeeded" if ready else "failed",
        exit_code=0 if ready else 1,
        error_type=None if ready else "container_failed",
        error_message="" if ready else error_message,
    )
    values = {
        "project_id": project_id,
        "authenticated_dispatcher_id": "dispatcher-1",
        "worker": "worker-1",
        "claim_sha256": "a" * 64,
        "generation_token": "generation-1",
        "task_run_id": task_run_id,
        "status": status,
        "artifact_id": "report-1" if ready else None,
        "artifact_sha256": "b" * 64 if ready else None,
        "input_snapshot_artifact_id": "snapshot-1" if ready else None,
        "input_snapshot_sha256": "c" * 64 if ready else None,
        "task_run_result": result.model_dump(mode="json"),
    }
    return PendingReportFinish(
        **values,
        request_sha256=finish_request_sha256(**values),
    )


def _execution(
    *,
    project_id: str = "project-1",
    task_run_id: str = "run-1",
) -> PendingReportExecution:
    return pending_report_execution(
        project_id=project_id,
        authenticated_dispatcher_id="dispatcher-1",
        worker="worker-1",
        claim_sha256="a" * 64,
        generation_token="generation-1",
        task_run_id=task_run_id,
        input_snapshot_artifact_id="snapshot-1",
        input_snapshot_sha256="c" * 64,
    )


def _output(
    execution: PendingReportExecution | None = None,
    *,
    content: str = "# exact report\n\n内容 café  \n",
) -> PendingReportOutput:
    execution = execution or _execution()
    return pending_report_output(
        execution,
        content=content,
        metadata={
            "source": "report_task",
            "format": "markdown",
            "report_generation_token": execution.generation_token,
            "report_template_key": "default-copy-v2",
            "report_template_version": 2,
            "report_template_digest_sha256": "d" * 64,
            "report_policy_version": 1,
            "report_execution_plan_sha256": "e" * 64,
            "report_prompt_digest_sha256": "f" * 64,
            "input_snapshot_artifact_id": execution.input_snapshot_artifact_id,
            "input_snapshot_sha256": execution.input_snapshot_sha256,
            "completeness_rule_version": 1,
        },
    )


def _artifact_receipt(entry: PendingReportOutput, artifact_id: str = "report-1"):
    return {
        "id": artifact_id,
        "sha256": entry.artifact_sha256,
    }


def _artifact_list_item(
    entry: PendingReportOutput,
    artifact_id: str = "report-1",
):
    return {
        "id": artifact_id,
        "project_id": entry.project_id,
        "task_run_id": entry.task_run_id,
        "kind": entry.kind,
        "category": entry.category,
        "uri": (
            f"artifact://projects/{entry.project_id}/runs/{entry.task_run_id}/"
            f"{artifact_id}-{entry.filename}"
        ),
        "sha256": entry.artifact_sha256,
        "size": len(entry.content.encode("utf-8")),
        "metadata": {
            **entry.metadata.model_dump(mode="json"),
            "runtime_context": {"server_owned": True},
        },
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "bare_uri",
        "missing_runtime_context",
        "invalid_runtime_context",
        "extra_metadata",
    ],
)
def test_artifact_recovery_rejects_identity_server_finish_would_reject(
    mutation: str,
):
    output = _output()
    item = _artifact_list_item(output)
    metadata = item["metadata"]
    assert isinstance(metadata, dict)
    if mutation == "bare_uri":
        item["uri"] = output.filename
    elif mutation == "missing_runtime_context":
        metadata.pop("runtime_context")
    elif mutation == "invalid_runtime_context":
        metadata["runtime_context"] = "not-an-object"
    else:
        metadata["unexpected"] = True

    assert recover_report_artifact_id(
        ApiResult(status_code=200, data=[item]),
        output,
    ) is None


def _receipt(entry: PendingReportFinish, **updates):
    payload = {
        "protocol": "cairn-assessment-report-finish-v1",
        "project_id": entry.project_id,
        "task_run_id": entry.task_run_id,
        "generation_token": entry.generation_token,
        "status": entry.status,
        "artifact_id": entry.artifact_id,
        "input_snapshot_artifact_id": entry.input_snapshot_artifact_id,
        "input_snapshot_sha256": entry.input_snapshot_sha256,
        "request_sha256": entry.request_sha256,
        "finish_disposition": "accepted",
    }
    payload.update(updates)
    return payload


class _ReplayClient:
    def __init__(self, responses: list[ApiResult]):
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def replay_assessment_report_finalization(self, project_id, worker, **kwargs):
        self.calls.append({"project_id": project_id, "worker": worker, **kwargs})
        return self.responses.pop(0)


class _OutputRecoveryClient:
    def __init__(
        self,
        *,
        list_response: ApiResult,
        create_response: ApiResult | None = None,
        finish_responses: list[ApiResult] | None = None,
    ):
        self.list_response = list_response
        self.create_response = create_response
        self.finish_responses = list(finish_responses or [])
        self.list_calls: list[str] = []
        self.create_calls: list[dict[str, object]] = []
        self.finish_calls: list[dict[str, object]] = []

    def list_artifacts(self, project_id, *, task_run_id=None):
        self.list_calls.append((project_id, task_run_id))
        return self.list_response

    def create_artifact(self, project_id, **kwargs):
        self.create_calls.append({"project_id": project_id, **kwargs})
        if self.create_response is None:
            pytest.fail("unexpected Artifact upload")
        return self.create_response

    def replay_assessment_report_finalization(self, project_id, worker, **kwargs):
        self.finish_calls.append({"project_id": project_id, "worker": worker, **kwargs})
        if not self.finish_responses:
            pytest.fail("unexpected finish replay")
        return self.finish_responses.pop(0)


class _FlushHarness(ReportFinishOutboxMixin):
    def __init__(self, outbox: ReportFinishOutbox, client):
        self.report_finish_outbox = outbox
        self.client = client
        self.container_manager = SimpleNamespace()
        self.dispatcher_id = "dispatcher-1"


@pytest.mark.parametrize(
    ("response_factory", "removed", "notified"),
    [
        (lambda entry: ApiResult(status_code=200, data=_receipt(entry)), True, True),
        (
            lambda entry: ApiResult(
                status_code=409,
                data={"detail": "assessment_report_claim_mismatch"},
            ),
            False,
            False,
        ),
        (
            lambda entry: ApiResult(
                status_code=403,
                data={"detail": "execution_owner_mismatch"},
            ),
            False,
            False,
        ),
        (
            lambda entry: ApiResult(
                status_code=422,
                data={"detail": "assessment_report_terminal_facts_invalid"},
            ),
            False,
            False,
        ),
        (
            lambda entry: ApiResult(status_code=503, data={"detail": "unavailable"}),
            False,
            False,
        ),
        (
            lambda entry: ApiResult(
                status_code=200,
                data=_receipt(entry, request_sha256="f" * 64),
            ),
            False,
            False,
        ),
    ],
)
def test_scheduler_flush_removes_only_strictly_accepted_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response_factory,
    removed: bool,
    notified: bool,
):
    entry = _entry()
    outbox = ReportFinishOutbox(tmp_path / "data" / "outbox")
    outbox.persist(entry)
    client = _ReplayClient([response_factory(entry)])
    notifications: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        "cairn.dispatcher.scheduler.report_finish_outbox.notify_browser_task",
        lambda *args, **kwargs: notifications.append((args, kwargs)),
    )

    _FlushHarness(outbox, client)._flush_pending_report_finishes()

    assert len(client.calls) == 1
    assert (outbox.entries() == []) is removed
    assert bool(notifications) is notified
    if notified:
        assert notifications[0][0][1:] == (entry.project_id, entry.task_run_id)
        assert notifications[0][1] == {"terminal_confirmed": True}


@pytest.mark.parametrize(
    ("safe_code", "snapshot_retained"),
    [
        ("assessment_report_artifact_invalid", True),
        ("assessment_report_snapshot_artifact_invalid", False),
    ],
)
def test_scheduler_transitions_rejected_ready_to_durable_failed_before_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    safe_code: str,
    snapshot_retained: bool,
):
    ready = _entry(status="ready")
    failed = failed_finish_fallback(
        ready,
        safe_code=safe_code,
    )
    outbox = ReportFinishOutbox(tmp_path / "data" / "outbox")
    outbox.persist(ready)
    client = _ReplayClient(
        [
            ApiResult(status_code=422, data={"detail": safe_code}),
            ApiResult(status_code=200, data=_receipt(failed)),
        ]
    )
    notifications: list[dict[str, object]] = []
    monkeypatch.setattr(
        "cairn.dispatcher.scheduler.report_finish_outbox.notify_browser_task",
        lambda *args, **kwargs: notifications.append(kwargs),
    )
    harness = _FlushHarness(outbox, client)

    harness._flush_pending_report_finishes()

    assert outbox.entries() == [failed]
    assert failed.task_run_result.error_type == "artifact_error"
    assert (failed.input_snapshot_artifact_id is not None) is snapshot_retained
    assert len(client.calls) == 1
    assert notifications == []

    harness._flush_pending_report_finishes()

    assert outbox.entries() == []
    assert len(client.calls) == 2
    assert client.calls[1]["request_sha256"] == failed.request_sha256
    assert notifications == [{"terminal_confirmed": True}]


@pytest.mark.parametrize(
    ("status_code", "detail"),
    [
        (404, "route_not_found"),
        (409, "assessment_report_unknown_conflict"),
        (422, "assessment_report_terminal_facts_invalid"),
        (403, "execution_owner_mismatch"),
    ],
)
def test_scheduler_retains_ready_entry_for_non_allowlisted_rejection(
    tmp_path: Path,
    status_code: int,
    detail: str,
):
    ready = _entry(status="ready")
    outbox = ReportFinishOutbox(tmp_path / "data" / "outbox")
    outbox.persist(ready)
    client = _ReplayClient(
        [ApiResult(status_code=status_code, data={"detail": detail})]
    )

    _FlushHarness(outbox, client)._flush_pending_report_finishes()

    assert outbox.entries() == [ready]
    assert len(client.calls) == 1


def test_scheduler_flush_is_bounded_to_eight_entries(tmp_path: Path):
    outbox = ReportFinishOutbox(tmp_path / "data" / "outbox")
    entries = [_entry(task_run_id=f"run-{index}") for index in range(9)]
    for entry in entries:
        outbox.persist(entry)
    client = _ReplayClient(
        [ApiResult(status_code=200, data=_receipt(entry)) for entry in sorted(
            entries,
            key=lambda item: ReportFinishOutbox._entry_name(
                item.project_id,
                item.task_run_id,
            ),
        )[:8]]
    )

    _FlushHarness(outbox, client)._flush_pending_report_finishes()

    assert len(client.calls) == 8
    assert len(outbox.entries()) == 1


def test_scheduler_flush_rotates_past_eight_retryable_entries(tmp_path: Path):
    outbox = ReportFinishOutbox(tmp_path / "data" / "outbox")
    entries = [_entry(task_run_id=f"run-{index}") for index in range(9)]
    for entry in entries:
        outbox.persist(entry)
    ordered = sorted(
        entries,
        key=lambda item: ReportFinishOutbox._entry_name(
            item.project_id,
            item.task_run_id,
        ),
    )
    succeeding = ordered[8]

    class FairReplayClient:
        def __init__(self):
            self.calls: list[str] = []

        def replay_assessment_report_finalization(
            self,
            project_id,
            worker,
            **kwargs,
        ):
            task_run_id = str(kwargs["task_run_id"])
            self.calls.append(task_run_id)
            entry = next(item for item in entries if item.task_run_id == task_run_id)
            if entry == succeeding:
                return ApiResult(status_code=200, data=_receipt(entry))
            return ApiResult(status_code=503, data={"detail": "unavailable"})

    client = FairReplayClient()
    harness = _FlushHarness(outbox, client)

    harness._flush_pending_report_finishes()
    assert client.calls == [entry.task_run_id for entry in ordered[:8]]
    harness._flush_pending_report_finishes()

    assert client.calls[8] == succeeding.task_run_id
    assert succeeding not in outbox.entries()
    assert len(outbox.entries()) == 8


def test_scheduler_flush_rejects_different_authenticated_owner_without_sending(
    tmp_path: Path,
):
    entry = _entry()
    outbox = ReportFinishOutbox(tmp_path / "data" / "outbox")
    outbox.persist(entry)
    client = _ReplayClient([])
    harness = _FlushHarness(outbox, client)
    harness.dispatcher_id = "other-dispatcher"

    with pytest.raises(RuntimeError, match="report_finish_outbox_owner_mismatch"):
        harness._flush_pending_report_finishes()

    assert client.calls == []
    assert outbox.entries() == [entry]


def test_scheduler_flush_preserves_replacement_entry_during_cleanup_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    original = _entry()
    replacement = _entry(error_message="replacement failure")
    outbox = ReportFinishOutbox(tmp_path / "data" / "outbox")
    outbox.persist(original)
    client = _ReplayClient(
        [ApiResult(status_code=200, data=_receipt(original))]
    )
    original_remove = outbox.remove

    def replace_before_cleanup(entry):
        original_remove(entry)
        outbox.persist(replacement)
        raise ReportFinishOutboxConflict("entry replaced")

    monkeypatch.setattr(outbox, "remove", replace_before_cleanup)

    _FlushHarness(outbox, client)._flush_pending_report_finishes()

    assert outbox.entries() == [replacement]


def test_scheduler_flush_fails_closed_on_corrupt_outbox_without_echoing_payload(
    tmp_path: Path,
):
    root = tmp_path / "data" / "outbox"
    root.mkdir(mode=0o700, parents=True)
    root.parent.chmod(0o700)
    path = root / ("0" * 64 + ".json")
    path.write_text('{"private":"DO-NOT-LOG"}', encoding="utf-8")
    path.chmod(0o600)
    harness = _FlushHarness(ReportFinishOutbox(root), _ReplayClient([]))

    with pytest.raises(RuntimeError) as rejected:
        harness._flush_pending_report_finishes()
    assert str(rejected.value) == "report_finish_outbox_invalid"
    assert "DO-NOT-LOG" not in str(rejected.value)


def test_scheduler_flush_fails_closed_when_entry_cannot_be_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    entry = _entry()
    outbox = ReportFinishOutbox(tmp_path / "data" / "outbox")
    outbox.persist(entry)
    client = _ReplayClient([ApiResult(status_code=200, data=_receipt(entry))])
    monkeypatch.setattr(outbox, "remove", lambda _entry: (_ for _ in ()).throw(OSError()))

    with pytest.raises(RuntimeError, match="report_finish_outbox_cleanup_failed"):
        _FlushHarness(outbox, client)._flush_pending_report_finishes()
    assert outbox.entries() == [entry]


class _DispatchHarness(DispatchCycleMixin):
    def __init__(self, outbox: ReportFinishOutbox):
        self.report_finish_outbox = outbox
        self.decisions: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.logs: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.client = SimpleNamespace(
            get_project=lambda project_id: pytest.fail(
                f"pending outbox must block project read: {project_id}"
            )
        )

    def _candidate_project_blocked(self, project_id: str) -> bool:
        return False

    def _observe_decision(self, *args, **kwargs):
        self.decisions.append((args, kwargs))

    def _log_changed(self, *args, **kwargs):
        self.logs.append((args, kwargs))


def test_pending_outbox_blocks_new_report_dispatch_before_claim(tmp_path: Path):
    entry = _entry()
    outbox = ReportFinishOutbox(tmp_path / "data" / "outbox")
    outbox.persist(entry)
    harness = _DispatchHarness(outbox)

    assert harness._try_dispatch_completed_report(entry.project_id) is False
    assert harness.decisions == [
        ((entry.project_id, "delivery_pending"), {"task_type": "report"})
    ]
    assert len(harness.logs) == 1


@pytest.mark.parametrize("entry_factory", [_execution, _output])
def test_pending_staged_journal_blocks_new_report_dispatch_before_claim(
    tmp_path: Path,
    entry_factory,
):
    entry = entry_factory()
    outbox = ReportFinishOutbox(tmp_path / entry.stage / "outbox")
    outbox.persist(entry)
    harness = _DispatchHarness(outbox)

    assert harness._try_dispatch_completed_report(entry.project_id) is False
    assert harness.decisions == [
        ((entry.project_id, "delivery_pending"), {"task_type": "report"})
    ]


def test_scheduler_execution_fence_never_calls_model_or_delivery_api(tmp_path: Path):
    execution = _execution()
    outbox = ReportFinishOutbox(tmp_path / "data" / "outbox")
    outbox.persist(execution)
    client = SimpleNamespace(
        list_artifacts=lambda *args, **kwargs: pytest.fail("不得查询 Artifact"),
        create_artifact=lambda *args, **kwargs: pytest.fail("不得上传 Artifact"),
        replay_assessment_report_finalization=lambda *args, **kwargs: pytest.fail(
            "不得发送 finish"
        ),
        run_worker_process=lambda *args, **kwargs: pytest.fail("不得重跑模型"),
        resume_session=lambda *args, **kwargs: pytest.fail("不得恢复 session"),
    )

    _FlushHarness(outbox, client)._flush_pending_report_finishes()

    assert outbox.entries() == [execution]


def test_scheduler_active_report_future_prevents_output_delivery_race(tmp_path: Path):
    output = _output()
    outbox = ReportFinishOutbox(tmp_path / "data" / "outbox")
    outbox.persist(output)
    client = SimpleNamespace(
        list_artifacts=lambda *args, **kwargs: pytest.fail("active future owns delivery"),
        create_artifact=lambda *args, **kwargs: pytest.fail("active future owns delivery"),
    )
    harness = _FlushHarness(outbox, client)
    harness.futures = {
        "future": SimpleNamespace(project_id=output.project_id, task_type="report")
    }

    harness._flush_pending_report_finishes()

    assert outbox.entries() == [output]


def test_scheduler_reconciles_exact_existing_artifact_then_finishes_next_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    output = _output()
    outbox = ReportFinishOutbox(tmp_path / "data" / "outbox")
    outbox.persist(output)
    client = _OutputRecoveryClient(
        list_response=ApiResult(
            status_code=200,
            data=[_artifact_list_item(output, artifact_id="existing-report")],
        )
    )
    notifications: list[dict[str, object]] = []
    monkeypatch.setattr(
        "cairn.dispatcher.scheduler.report_finish_outbox.notify_browser_task",
        lambda *args, **kwargs: notifications.append(kwargs),
    )
    harness = _FlushHarness(outbox, client)

    harness._flush_pending_report_finishes()

    stored = outbox.entries()
    assert len(stored) == 1
    finish = stored[0]
    assert isinstance(finish, PendingReportFinish)
    assert finish.artifact_id == "existing-report"
    assert finish.artifact_sha256 == output.artifact_sha256
    assert client.list_calls == [(output.project_id, output.task_run_id)]
    assert client.create_calls == []
    assert client.finish_calls == []
    assert notifications == []

    client.finish_responses.append(
        ApiResult(status_code=200, data=_receipt(finish))
    )
    harness._flush_pending_report_finishes()

    assert outbox.entries() == []
    assert len(client.finish_calls) == 1
    assert client.finish_calls[0]["task_run_id"] == output.task_run_id
    assert notifications == [{"terminal_confirmed": True}]


def test_scheduler_replays_exact_output_and_advances_without_rerender(
    tmp_path: Path,
):
    content = "# 报告\r\n\r\nexact café  \n"
    output = _output(content=content)
    outbox = ReportFinishOutbox(tmp_path / "data" / "outbox")
    outbox.persist(output)
    client = _OutputRecoveryClient(
        list_response=ApiResult(status_code=200, data=[]),
        create_response=ApiResult(
            status_code=201,
            data=_artifact_receipt(output, artifact_id="uploaded-report"),
        ),
    )

    _FlushHarness(outbox, client)._flush_pending_report_finishes()

    assert len(client.create_calls) == 1
    request = client.create_calls[0]
    assert request == {
        "project_id": output.project_id,
        "task_run_id": output.task_run_id,
        "kind": "report",
        "content": content,
        "filename": "vulnerability-report.md",
        "metadata": output.metadata.model_dump(mode="json"),
        "category": "deliverable",
    }
    stored = outbox.entries()
    assert len(stored) == 1
    finish = stored[0]
    assert isinstance(finish, PendingReportFinish)
    assert finish.artifact_id == "uploaded-report"
    assert finish.artifact_sha256 == output.artifact_sha256
    assert client.finish_calls == []


def test_scheduler_retains_output_when_authoritative_list_fails(tmp_path: Path):
    output = _output()
    outbox = ReportFinishOutbox(tmp_path / "data" / "outbox")
    outbox.persist(output)
    client = _OutputRecoveryClient(
        list_response=ApiResult(status_code=503, data={"detail": "unavailable"}),
    )

    _FlushHarness(outbox, client)._flush_pending_report_finishes()

    assert outbox.entries() == [output]
    assert client.list_calls == [(output.project_id, output.task_run_id)]
    assert client.create_calls == []
    assert client.finish_calls == []


@pytest.mark.parametrize(
    ("status_code", "data"),
    [
        (200, {"id": "report-1", "sha256": "0" * 64}),
        (200, {"sha256": "0" * 64}),
        (403, {"detail": "execution_owner_mismatch"}),
        (404, {"detail": "route_not_found"}),
        (409, {"detail": "assessment_report_claim_mismatch"}),
        (422, {"detail": "artifact_invalid"}),
        (503, {"detail": "unavailable"}),
    ],
)
def test_scheduler_retains_exact_output_for_unaccepted_artifact_response(
    tmp_path: Path,
    status_code: int,
    data: object,
):
    output = _output()
    outbox = ReportFinishOutbox(tmp_path / str(status_code) / str(len(str(data))))
    outbox.persist(output)
    client = _OutputRecoveryClient(
        list_response=ApiResult(status_code=200, data=[]),
        create_response=ApiResult(status_code=status_code, data=data),
    )

    _FlushHarness(outbox, client)._flush_pending_report_finishes()

    assert outbox.entries() == [output]
    assert len(client.create_calls) == 1
    assert client.finish_calls == []


def test_scheduler_output_transition_failure_keeps_durable_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    output = _output()
    outbox = ReportFinishOutbox(tmp_path / "data" / "outbox")
    outbox.persist(output)
    client = _OutputRecoveryClient(
        list_response=ApiResult(status_code=200, data=[]),
        create_response=ApiResult(
            status_code=201,
            data=_artifact_receipt(output),
        ),
    )
    monkeypatch.setattr(
        outbox,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError()),
    )

    with pytest.raises(RuntimeError, match="report_finish_outbox_transition_failed"):
        _FlushHarness(outbox, client)._flush_pending_report_finishes()

    assert outbox.entries() == [output]
    assert len(client.create_calls) == 1
    assert client.finish_calls == []


def test_scheduler_output_owner_mismatch_sends_nothing(tmp_path: Path):
    output = _output()
    outbox = ReportFinishOutbox(tmp_path / "data" / "outbox")
    outbox.persist(output)
    client = _OutputRecoveryClient(
        list_response=ApiResult(status_code=200, data=[]),
    )
    harness = _FlushHarness(outbox, client)
    harness.dispatcher_id = "other-dispatcher"

    with pytest.raises(RuntimeError, match="report_finish_outbox_owner_mismatch"):
        harness._flush_pending_report_finishes()

    assert outbox.entries() == [output]
    assert client.list_calls == []
    assert client.create_calls == []
    assert client.finish_calls == []
