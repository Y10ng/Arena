from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
from threading import Barrier, Event

import pytest

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.tasks.report_finish_outbox import (
    EXECUTION_PROTOCOL,
    MAX_ENTRY_BYTES,
    MAX_FINISH_ENTRY_BYTES,
    MAX_REPORT_MARKDOWN_BYTES,
    OUTPUT_PROTOCOL,
    PendingReportExecution,
    PendingReportFinish,
    PendingReportOutput,
    ReportFinishOutbox,
    ReportFinishOutboxConflict,
    canonical_json,
    failed_finish_fallback,
    failed_finish_for_recovery,
    finish_request_sha256,
    pending_report_execution,
    pending_report_output,
    ready_finish_for_output,
    validate_finish_receipt,
)
from cairn.server.models import AssessmentReportTaskRunResult


CLAIM_TOKEN = "claim-token-must-never-be-persisted"
CLAIM_SHA256 = sha256(CLAIM_TOKEN.encode("utf-8")).hexdigest()


def _entry(
    *,
    project_id: str = "project-1",
    task_run_id: str = "run-1",
    status: str = "failed",
    error_message: str = "worker failed",
) -> PendingReportFinish:
    if status == "ready":
        artifact_id = "report-1"
        artifact_sha256 = "a" * 64
        snapshot_id = "snapshot-1"
        snapshot_sha256 = "b" * 64
        result = AssessmentReportTaskRunResult(
            status="succeeded",
            exit_code=0,
        )
    else:
        artifact_id = None
        artifact_sha256 = None
        snapshot_id = "snapshot-1"
        snapshot_sha256 = "b" * 64
        result = AssessmentReportTaskRunResult(
            status="failed",
            exit_code=1,
            error_type="container_failed",
            error_message=error_message,
        )
    request_sha256 = finish_request_sha256(
        project_id=project_id,
        authenticated_dispatcher_id="dispatcher-1",
        worker="worker-1",
        claim_sha256=CLAIM_SHA256,
        generation_token="generation-1",
        task_run_id=task_run_id,
        status=status,
        artifact_id=artifact_id,
        artifact_sha256=artifact_sha256,
        input_snapshot_artifact_id=snapshot_id,
        input_snapshot_sha256=snapshot_sha256,
        task_run_result=result.model_dump(mode="json"),
    )
    return PendingReportFinish(
        project_id=project_id,
        authenticated_dispatcher_id="dispatcher-1",
        worker="worker-1",
        claim_sha256=CLAIM_SHA256,
        request_sha256=request_sha256,
        generation_token="generation-1",
        task_run_id=task_run_id,
        status=status,
        artifact_id=artifact_id,
        artifact_sha256=artifact_sha256,
        input_snapshot_artifact_id=snapshot_id,
        input_snapshot_sha256=snapshot_sha256,
        task_run_result=result,
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
        claim_sha256=CLAIM_SHA256,
        generation_token="generation-1",
        task_run_id=task_run_id,
        input_snapshot_artifact_id="snapshot-1",
        input_snapshot_sha256="b" * 64,
    )


def _output(
    execution: PendingReportExecution | None = None,
    *,
    content: str = "# 报告\n\n精确内容 café  \n",
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
            "report_template_digest_sha256": "c" * 64,
            "report_policy_version": 1,
            "report_execution_plan_sha256": "d" * 64,
            "report_prompt_digest_sha256": "e" * 64,
            "input_snapshot_artifact_id": execution.input_snapshot_artifact_id,
            "input_snapshot_sha256": execution.input_snapshot_sha256,
            "completeness_rule_version": 1,
        },
    )


def _entry_payload(
    entry: PendingReportExecution | PendingReportOutput | PendingReportFinish,
) -> bytes:
    return canonical_json(entry.model_dump(mode="json")).encode("utf-8")


def _prepare_root(tmp_path: Path) -> Path:
    parent = tmp_path / "data"
    parent.mkdir(mode=0o700)
    root = parent / "outbox"
    root.mkdir(mode=0o700)
    return root


def _write_entry_file(root: Path, name: str, content: bytes, *, mode: int = 0o600) -> Path:
    path = root / name
    path.write_bytes(content)
    path.chmod(mode)
    return path


def test_outbox_persists_owner_only_token_free_entry_and_loads_after_restart(
    tmp_path: Path,
):
    entry = _entry(error_message="Unicode failure café")
    outbox = ReportFinishOutbox(tmp_path / "data" / "outbox")

    outbox.persist(entry)
    paths = [path for path in outbox.root.iterdir() if path.suffix == ".json"]
    assert len(paths) == 1
    path = paths[0]
    lock_path = outbox.root / ".lock"
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(outbox.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    stored = path.read_text(encoding="utf-8")
    assert CLAIM_TOKEN not in stored
    assert CLAIM_SHA256 in stored

    inode = path.stat().st_ino
    outbox.persist(entry)
    assert path.stat().st_ino == inode
    restarted = ReportFinishOutbox(outbox.root)
    assert restarted.entries() == [entry]
    assert restarted.has_project(entry.project_id) is True

    restarted.remove(entry)
    assert restarted.entries() == []
    assert restarted.has_project(entry.project_id) is False


def test_outbox_rejects_conflicting_identity_and_refuses_wrong_removal(tmp_path: Path):
    outbox = ReportFinishOutbox(tmp_path / "data" / "outbox")
    original = _entry(error_message="first")
    conflicting = _entry(error_message="second")
    outbox.persist(original)

    with pytest.raises(ReportFinishOutboxConflict):
        outbox.persist(conflicting)
    with pytest.raises(ReportFinishOutboxConflict):
        outbox.remove(conflicting)
    assert outbox.entries() == [original]


def test_two_outbox_instances_cannot_overwrite_same_task_run_concurrently(
    tmp_path: Path,
):
    root = tmp_path / "data" / "outbox"
    first = ReportFinishOutbox(root)
    second = ReportFinishOutbox(root)
    entries = [_entry(error_message="first"), _entry(error_message="second")]
    barrier = Barrier(2)

    def persist_once(item):
        outbox, entry = item
        barrier.wait(timeout=10)
        try:
            outbox.persist(entry)
        except ReportFinishOutboxConflict:
            return "conflict"
        return "persisted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(persist_once, zip((first, second), entries)))

    assert sorted(outcomes) == ["conflict", "persisted"]
    stored = ReportFinishOutbox(root).entries()
    assert len(stored) == 1
    assert stored[0] in entries


def test_failed_finish_for_recovery_is_token_free_and_drops_snapshot_identity(
    tmp_path: Path,
):
    execution = _execution()
    failed = failed_finish_for_recovery(
        project_id=execution.project_id,
        authenticated_dispatcher_id=execution.authenticated_dispatcher_id,
        worker=execution.worker,
        claim_sha256=execution.claim_sha256,
        generation_token=execution.generation_token,
        task_run_id=execution.task_run_id,
        exit_code=None,
        error_type="delivery_unavailable",
        error_message="原执行已停止",
    )
    outbox = ReportFinishOutbox(tmp_path / "data" / "outbox")
    outbox.persist(execution)

    outbox.replace(execution, failed)

    assert outbox.entries() == [failed]
    assert failed.status == "failed"
    assert failed.input_snapshot_artifact_id is None
    assert failed.input_snapshot_sha256 is None
    assert failed.task_run_result.error_type == "delivery_unavailable"
    stored = next(outbox.root.glob("*.json")).read_text(encoding="utf-8")
    assert CLAIM_TOKEN not in stored
    assert CLAIM_SHA256 in stored


def test_failed_fallback_preserves_or_drops_snapshot_by_rejection_code():
    ready = _entry(status="ready")

    artifact_failed = failed_finish_fallback(
        ready,
        safe_code="assessment_report_artifact_invalid",
    )
    assert artifact_failed.status == "failed"
    assert artifact_failed.artifact_id is None
    assert artifact_failed.input_snapshot_artifact_id == ready.input_snapshot_artifact_id
    assert artifact_failed.input_snapshot_sha256 == ready.input_snapshot_sha256
    assert artifact_failed.task_run_result.error_type == "artifact_error"

    snapshot_failed = failed_finish_fallback(
        ready,
        safe_code="assessment_report_snapshot_artifact_invalid",
    )
    assert snapshot_failed.input_snapshot_artifact_id is None
    assert snapshot_failed.input_snapshot_sha256 is None
    assert snapshot_failed.task_run_result.error_type == "artifact_error"


@pytest.mark.parametrize(
    "safe_code",
    [None, "assessment_report_terminal_facts_invalid", "route_not_found"],
)
def test_failed_fallback_rejects_non_artifact_errors(safe_code: str | None):
    with pytest.raises(ValueError, match="cannot transition"):
        failed_finish_fallback(_entry(status="ready"), safe_code=safe_code)


def test_replace_is_atomic_against_concurrent_remove(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    ready = _entry(status="ready")
    failed = failed_finish_fallback(
        ready,
        safe_code="assessment_report_artifact_invalid",
    )
    root = tmp_path / "data" / "outbox"
    replacing = ReportFinishOutbox(root)
    removing = ReportFinishOutbox(root)
    replacing.persist(ready)
    replace_entered = Event()
    release_replace = Event()
    remove_started = Event()
    original_replace = os.replace

    def delayed_replace(source, destination):
        replace_entered.set()
        assert release_replace.wait(10)
        original_replace(source, destination)

    monkeypatch.setattr(
        "cairn.dispatcher.tasks.report_finish_outbox.os.replace",
        delayed_replace,
    )

    def remove_ready():
        remove_started.set()
        removing.remove(ready)

    with ThreadPoolExecutor(max_workers=2) as executor:
        replace_future = executor.submit(replacing.replace, ready, failed)
        assert replace_entered.wait(10)
        remove_future = executor.submit(remove_ready)
        assert remove_started.wait(10)
        assert not remove_future.done()
        release_replace.set()
        replace_future.result(timeout=10)
        with pytest.raises(ReportFinishOutboxConflict):
            remove_future.result(timeout=10)

    assert ReportFinishOutbox(root).entries() == [failed]


def test_cutover_fences_legacy_writers_and_next_start_drains_late_write(
    tmp_path: Path,
):
    legacy = ReportFinishOutbox(tmp_path / "data" / "legacy")
    canonical = ReportFinishOutbox(tmp_path / "data" / "canonical")
    original = _entry(task_run_id="run-original")
    late = _entry(task_run_id="run-late")
    legacy.persist(original)
    old_writer = ReportFinishOutbox(legacy.root)
    cutover = legacy.cutover_to(canonical)
    assert cutover.__enter__() == 1
    cutover_active = True
    started = Event()

    def write_from_old_dispatcher():
        started.set()
        old_writer.persist(late)

    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(write_from_old_dispatcher)
    try:
        assert started.wait(10)
        assert not future.done()
        assert canonical.entries() == [original]
        assert not list(legacy.root.glob("*.json"))
    finally:
        if cutover_active:
            cutover.__exit__(None, None, None)
            cutover_active = False
        executor.shutdown(wait=True)
    future.result(timeout=10)

    assert legacy.entries() == [late]
    with legacy.cutover_to(canonical) as migrated:
        assert migrated == 1
    assert sorted(
        entry.task_run_id for entry in canonical.entries()
    ) == ["run-late", "run-original"]
    assert legacy.entries() == []


def test_cutover_keeps_advanced_failed_transition(
    tmp_path: Path,
):
    ready = _entry(status="ready")
    failed = failed_finish_fallback(
        ready,
        safe_code="assessment_report_artifact_invalid",
    )
    legacy = ReportFinishOutbox(tmp_path / "data" / "legacy")
    canonical = ReportFinishOutbox(tmp_path / "data" / "canonical")
    legacy.persist(failed)
    canonical.persist(ready)

    with legacy.cutover_to(canonical) as migrated:
        assert migrated == 1

    assert canonical.entries() == [failed]
    assert legacy.entries() == []


def test_entries_holds_shared_lock_until_each_visible_entry_is_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    entry = _entry()
    root = tmp_path / "data" / "outbox"
    reader = ReportFinishOutbox(root)
    remover = ReportFinishOutbox(root)
    reader.persist(entry)
    read_entered = Event()
    release_read = Event()
    remove_started = Event()
    original_read = reader._read_path

    def delayed_read(path):
        read_entered.set()
        assert release_read.wait(10)
        return original_read(path)

    monkeypatch.setattr(reader, "_read_path", delayed_read)

    def remove_entry():
        remove_started.set()
        remover.remove(entry)

    with ThreadPoolExecutor(max_workers=2) as executor:
        entries_future = executor.submit(reader.entries)
        assert read_entered.wait(10)
        remove_future = executor.submit(remove_entry)
        assert remove_started.wait(10)
        assert not remove_future.done()
        release_read.set()
        assert entries_future.result(timeout=10) == [entry]
        remove_future.result(timeout=10)

    assert ReportFinishOutbox(root).entries() == []


@pytest.mark.parametrize(
    ("content_factory", "expected"),
    [
        (lambda entry: b"\xff", "entry is invalid"),
        (lambda entry: b"{", "entry is invalid"),
        (
            lambda entry: canonical_json(
                {**entry.model_dump(mode="json"), "unknown": "PRIVATE-MARKER"}
            ).encode("utf-8"),
            "entry is invalid",
        ),
        (
            lambda entry: canonical_json(
                {**entry.model_dump(mode="json"), "request_sha256": "f" * 64}
            ).encode("utf-8"),
            "entry is invalid",
        ),
        (lambda entry: b"x" * (MAX_ENTRY_BYTES + 1), "permissions or size invalid"),
    ],
)
def test_outbox_rejects_malformed_entries_without_echoing_content(
    tmp_path: Path,
    content_factory,
    expected: str,
):
    entry = _entry()
    root = _prepare_root(tmp_path)
    name = ReportFinishOutbox._entry_name(entry.project_id, entry.task_run_id)
    _write_entry_file(root, name, content_factory(entry))

    with pytest.raises(OSError, match=expected) as rejected:
        ReportFinishOutbox(root).entries()
    assert "PRIVATE-MARKER" not in str(rejected.value)
    assert CLAIM_TOKEN not in str(rejected.value)


def test_outbox_rejects_filename_mismatch(tmp_path: Path):
    entry = _entry()
    root = _prepare_root(tmp_path)
    _write_entry_file(root, "0" * 64 + ".json", _entry_payload(entry))

    with pytest.raises(OSError, match="filename mismatch"):
        ReportFinishOutbox(root).entries()


@pytest.mark.parametrize("entry_kind", ["symlink", "directory", "wrong_mode"])
def test_outbox_rejects_unsafe_entry_types_and_permissions(
    tmp_path: Path,
    entry_kind: str,
):
    entry = _entry()
    root = _prepare_root(tmp_path)
    name = ReportFinishOutbox._entry_name(entry.project_id, entry.task_run_id)
    path = root / name
    if entry_kind == "symlink":
        target = root / "target"
        target.write_bytes(_entry_payload(entry))
        target.chmod(0o600)
        path.symlink_to(target)
    elif entry_kind == "directory":
        path.mkdir(mode=0o700)
    else:
        _write_entry_file(root, name, _entry_payload(entry), mode=0o640)

    with pytest.raises(OSError):
        ReportFinishOutbox(root).entries()


@pytest.mark.parametrize("unsafe", ["root_symlink", "root_mode", "parent_mode"])
def test_outbox_rejects_unsafe_root_or_parent(tmp_path: Path, unsafe: str):
    parent = tmp_path / "data"
    parent.mkdir(mode=0o700)
    root = parent / "outbox"
    if unsafe == "root_symlink":
        target = tmp_path / "target"
        target.mkdir(mode=0o700)
        root.symlink_to(target, target_is_directory=True)
    else:
        root.mkdir(mode=0o700)
        if unsafe == "root_mode":
            root.chmod(0o750)
        else:
            parent.chmod(0o750)

    with pytest.raises(OSError, match="owner-only"):
        ReportFinishOutbox(root).entries()


def test_outbox_rejects_unexpected_visible_file(tmp_path: Path):
    root = _prepare_root(tmp_path)
    _write_entry_file(root, "README", b"not an entry")

    with pytest.raises(OSError, match="unexpected report finish outbox entry"):
        ReportFinishOutbox(root).entries()


def test_pending_entry_replays_without_plaintext_claim_token():
    entry = _entry()
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class Client:
        def replay_assessment_report_finalization(self, *args, **kwargs):
            calls.append((args, kwargs))
            return ApiResult(status_code=503, data={"detail": "unavailable"})

    response = entry.replay(Client())

    assert response.status_code == 503
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == (entry.project_id, entry.worker)
    assert "claim_token" not in kwargs
    assert kwargs["claim_sha256"] == CLAIM_SHA256
    assert CLAIM_TOKEN not in json.dumps(kwargs, ensure_ascii=False)


def test_finish_receipt_validation_requires_exact_identity():
    entry = _entry(status="ready")
    receipt = {
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

    accepted = validate_finish_receipt(ApiResult(status_code=200, data=receipt), entry)
    assert accepted.status_code == 200

    for changed in (
        None,
        {**receipt, "request_sha256": "f" * 64},
        {**receipt, "task_run_id": "other-run"},
        {**receipt, "finish_disposition": "pending"},
        {**receipt, "finish_disposition": []},
        {**receipt, "finish_disposition": {}},
    ):
        uncertain = validate_finish_receipt(
            ApiResult(status_code=200, data=changed),
            entry,
        )
        assert uncertain.status_code == 0
        assert uncertain.data == {
            "detail": "assessment_report_finish_receipt_invalid"
        }


def test_pending_entry_model_rejects_incomplete_or_inconsistent_facts():
    failed = _entry()
    with pytest.raises(ValueError, match="request_sha256"):
        PendingReportFinish.model_validate(
            {**failed.model_dump(mode="json"), "artifact_id": "report-1"}
        )

    ready = _entry(status="ready")
    bad_result = AssessmentReportTaskRunResult(
        status="failed",
        exit_code=1,
        error_type="container_failed",
        error_message="failed",
    )
    payload = ready.model_dump(mode="json")
    payload["task_run_result"] = bad_result.model_dump(mode="json")
    payload["request_sha256"] = finish_request_sha256(
        project_id=ready.project_id,
        authenticated_dispatcher_id=ready.authenticated_dispatcher_id,
        worker=ready.worker,
        claim_sha256=ready.claim_sha256,
        generation_token=ready.generation_token,
        task_run_id=ready.task_run_id,
        status=ready.status,
        artifact_id=ready.artifact_id,
        artifact_sha256=ready.artifact_sha256,
        input_snapshot_artifact_id=ready.input_snapshot_artifact_id,
        input_snapshot_sha256=ready.input_snapshot_sha256,
        task_run_result=payload["task_run_result"],
    )
    with pytest.raises(ValueError, match="ready finish requires succeeded"):
        PendingReportFinish.model_validate(payload)


def test_pending_execution_digest_is_canonical_and_token_free():
    entry = _execution()
    expected = sha256(
        canonical_json(
            {
                "protocol": EXECUTION_PROTOCOL,
                "stage": "execution_pending",
                "project_id": entry.project_id,
                "authenticated_dispatcher_id": entry.authenticated_dispatcher_id,
                "worker": entry.worker,
                "claim_sha256": entry.claim_sha256,
                "generation_token": entry.generation_token,
                "task_run_id": entry.task_run_id,
                "input_snapshot_artifact_id": entry.input_snapshot_artifact_id,
                "input_snapshot_sha256": entry.input_snapshot_sha256,
            }
        ).encode("utf-8")
    ).hexdigest()

    assert entry.entry_sha256 == expected
    serialized = _entry_payload(entry).decode("utf-8")
    assert CLAIM_TOKEN not in serialized
    assert CLAIM_SHA256 in serialized


def test_pending_output_preserves_exact_unicode_whitespace_and_newlines(
    tmp_path: Path,
):
    content = "# 报告\r\n\r\n末尾空格  \n最后一行无换行 café"
    output = _output(content=content)
    outbox = ReportFinishOutbox(tmp_path / "data" / "outbox")

    outbox.persist(output)
    restarted = ReportFinishOutbox(outbox.root)
    loaded = restarted.entries()

    assert loaded == [output]
    assert isinstance(loaded[0], PendingReportOutput)
    assert loaded[0].content == content
    assert loaded[0].content.encode("utf-8") == content.encode("utf-8")
    assert loaded[0].artifact_sha256 == sha256(content.encode("utf-8")).hexdigest()
    assert CLAIM_TOKEN not in next(outbox.root.glob("*.json")).read_text(encoding="utf-8")


def test_pending_output_digest_covers_exact_request_material():
    output = _output()
    expected = sha256(
        canonical_json(
            {
                "protocol": OUTPUT_PROTOCOL,
                "stage": "validated_output",
                "project_id": output.project_id,
                "authenticated_dispatcher_id": output.authenticated_dispatcher_id,
                "worker": output.worker,
                "claim_sha256": output.claim_sha256,
                "generation_token": output.generation_token,
                "task_run_id": output.task_run_id,
                "input_snapshot_artifact_id": output.input_snapshot_artifact_id,
                "input_snapshot_sha256": output.input_snapshot_sha256,
                "artifact_sha256": output.artifact_sha256,
                "kind": output.kind,
                "category": output.category,
                "filename": output.filename,
                "content": output.content,
                "metadata": output.metadata.model_dump(mode="json"),
            }
        ).encode("utf-8")
    ).hexdigest()

    assert output.entry_sha256 == expected


@pytest.mark.parametrize("legacy", [True, False])
def test_exact_output_metadata_survives_restart_and_replay_without_added_defaults(tmp_path: Path, legacy: bool):
    execution = _execution()
    original = _output(execution).model_dump(mode="json")
    metadata = dict(original["metadata"])
    if legacy:
        assert metadata["report_template_version"] == 2
        assert not {"packet_version", "output_contract_version", "report_data_schema_version"} & metadata.keys()
    else:
        metadata.pop("report_template_version")
        metadata.update(packet_version=2, output_contract_version=3, report_data_schema_version=2)
    output = pending_report_output(execution, content=original["content"], metadata=metadata)
    if legacy:
        assert output.entry_sha256 == original["entry_sha256"]
    outbox = ReportFinishOutbox(tmp_path / "outbox")
    outbox.persist(output)
    loaded = ReportFinishOutbox(outbox.root).entries()[0]
    assert loaded.entry_sha256 == output.entry_sha256
    assert loaded.model_dump(mode="json")["metadata"] == metadata
    requests = []

    class Client:
        def create_artifact(self, project_id, **kwargs):
            requests.append((project_id, kwargs))
            return ApiResult(status_code=201, data={})

    loaded.replay(Client())
    assert requests == [(execution.project_id, {
        "task_run_id": execution.task_run_id, "kind": output.kind, "category": output.category,
        "filename": output.filename, "content": original["content"], "metadata": metadata,
    })]
    assert canonical_json(loaded.model_dump(mode="json")) == canonical_json(output.model_dump(mode="json"))


def test_pending_output_rejects_digest_and_metadata_identity_changes():
    output = _output()
    payload = output.model_dump(mode="json")
    payload["artifact_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="artifact_sha256"):
        PendingReportOutput.model_validate(payload)

    execution = _execution()
    metadata = _output(execution).metadata.model_dump(mode="json")
    metadata["report_generation_token"] = "other-generation"
    with pytest.raises(ValueError, match="metadata changed immutable identity"):
        pending_report_output(execution, content="exact", metadata=metadata)

    metadata = _output(execution).metadata.model_dump(mode="json")
    metadata["input_snapshot_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="metadata changed immutable identity"):
        pending_report_output(execution, content="exact", metadata=metadata)


def test_pending_output_accepts_more_than_finish_limit_without_truncation(
    tmp_path: Path,
):
    content = "界" * (MAX_FINISH_ENTRY_BYTES // 2)
    output = _output(content=content)
    assert len(_entry_payload(output)) > MAX_FINISH_ENTRY_BYTES

    outbox = ReportFinishOutbox(tmp_path / "data" / "outbox")
    outbox.persist(output)

    assert outbox.entries() == [output]
    assert outbox.entries()[0].content == content


def test_pending_output_at_markdown_limit_survives_maximum_json_escaping(
    tmp_path: Path,
):
    content = "\0" * MAX_REPORT_MARKDOWN_BYTES
    output = _output(content=content)
    payload = _entry_payload(output)
    assert len(payload) <= MAX_ENTRY_BYTES
    assert len(payload) > 6 * MAX_REPORT_MARKDOWN_BYTES

    outbox = ReportFinishOutbox(tmp_path / "data" / "outbox")
    outbox.persist(output)

    assert outbox.entries() == [output]
    assert outbox.entries()[0].content == content


def test_pending_output_over_markdown_limit_is_rejected_without_truncation():
    content = "x" * (MAX_REPORT_MARKDOWN_BYTES + 1)

    with pytest.raises(ValueError, match="durable size limit"):
        _output(content=content)


def test_outbox_rejects_duplicate_json_keys_for_staged_entry(tmp_path: Path):
    entry = _execution()
    root = _prepare_root(tmp_path)
    name = ReportFinishOutbox._entry_name(entry.project_id, entry.task_run_id)
    payload = _entry_payload(entry).decode("utf-8")
    duplicate = payload.replace(
        '"project_id":"project-1"',
        '"project_id":"project-1","project_id":"project-1"',
        1,
    ).encode("utf-8")
    _write_entry_file(root, name, duplicate)

    with pytest.raises(OSError, match="entry is invalid"):
        ReportFinishOutbox(root).entries()


@pytest.mark.parametrize(
    "transition",
    [
        "execution_to_output",
        "execution_to_failed",
        "output_to_ready",
        "ready_to_failed",
    ],
)
def test_report_journal_allows_only_forward_atomic_transitions(
    tmp_path: Path,
    transition: str,
):
    execution = _execution()
    output = _output(execution)
    ready = ready_finish_for_output(output, artifact_id="report-1")
    failed_from_execution = _entry()
    failed_from_ready = failed_finish_fallback(
        ready,
        safe_code="assessment_report_artifact_invalid",
    )
    expected, replacement = {
        "execution_to_output": (execution, output),
        "execution_to_failed": (execution, failed_from_execution),
        "output_to_ready": (output, ready),
        "ready_to_failed": (ready, failed_from_ready),
    }[transition]
    outbox = ReportFinishOutbox(tmp_path / transition / "outbox")
    outbox.persist(expected)

    outbox.replace(expected, replacement)

    assert outbox.entries() == [replacement]


@pytest.mark.parametrize(
    "transition",
    [
        "execution_to_ready",
        "output_to_failed",
        "output_to_different_output",
        "ready_to_output",
    ],
)
def test_report_journal_rejects_skipped_changed_or_backward_transitions(
    tmp_path: Path,
    transition: str,
):
    execution = _execution()
    output = _output(execution)
    different_output = _output(execution, content="different exact bytes")
    ready = ready_finish_for_output(output, artifact_id="report-1")
    failed = _entry()
    expected, replacement = {
        "execution_to_ready": (execution, ready),
        "output_to_failed": (output, failed),
        "output_to_different_output": (output, different_output),
        "ready_to_output": (ready, output),
    }[transition]
    outbox = ReportFinishOutbox(tmp_path / transition / "outbox")
    outbox.persist(expected)

    with pytest.raises(ReportFinishOutboxConflict, match="allowed transition"):
        outbox.replace(expected, replacement)

    assert outbox.entries() == [expected]


@pytest.mark.parametrize(
    ("legacy_stage", "canonical_stage", "expected_stage"),
    [
        ("execution", "output", "output"),
        ("output", "execution", "output"),
        ("output", "finish", "finish"),
        ("finish", "output", "finish"),
    ],
)
def test_cutover_preserves_most_advanced_report_journal_stage(
    tmp_path: Path,
    legacy_stage: str,
    canonical_stage: str,
    expected_stage: str,
):
    execution = _execution()
    output = _output(execution)
    finish = ready_finish_for_output(output, artifact_id="report-1")
    stages = {"execution": execution, "output": output, "finish": finish}
    legacy = ReportFinishOutbox(tmp_path / "data" / "legacy")
    canonical = ReportFinishOutbox(tmp_path / "data" / "canonical")
    legacy.persist(stages[legacy_stage])
    canonical.persist(stages[canonical_stage])

    with legacy.cutover_to(canonical) as migrated:
        assert migrated == 1

    assert canonical.entries() == [stages[expected_stage]]
    assert legacy.entries() == []


def test_cutover_conflict_preserves_both_staged_copies(tmp_path: Path):
    execution = _execution()
    legacy_output = _output(execution, content="legacy exact bytes")
    canonical_output = _output(execution, content="canonical exact bytes")
    legacy = ReportFinishOutbox(tmp_path / "data" / "legacy")
    canonical = ReportFinishOutbox(tmp_path / "data" / "canonical")
    legacy.persist(legacy_output)
    canonical.persist(canonical_output)

    with pytest.raises(ReportFinishOutboxConflict, match="conflicting"):
        with legacy.cutover_to(canonical):
            pass

    assert legacy.entries() == [legacy_output]
    assert canonical.entries() == [canonical_output]
