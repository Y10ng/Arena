from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import struct

import pytest

from cairn.dispatcher.tasks.candidate_capture_outbox import (
    CandidateCaptureOutbox,
    CandidateCaptureOutboxConflict,
    CandidateCaptureOutboxError,
    CandidateExecutionFence,
    PendingCandidateCapture,
)
from cairn.dispatcher.workers.base import WorkerCandidateSpool


def _canonical(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _frame(manifest: dict[str, object], raw: bytes) -> tuple[bytes, bytes]:
    manifest_bytes = _canonical(manifest)
    body = b"CRNCND01" + struct.pack(">II", len(manifest_bytes), len(raw)) + manifest_bytes + raw
    return manifest_bytes, body + sha256(body).digest()


def _entry(raw: bytes = b'{\r\n"accepted": tru, "note": "\xe9\x9b\xaa"\r\n}') -> PendingCandidateCapture:
    manifest = {
        "artifacts": [],
        "assistant_turn": 1,
        "binding_sha256": sha256(b"null").hexdigest(),
        "capture_id": "12345678-1234-4234-8234-123456789abc",
        "content_index": 0,
        "parent_producer_candidate_key": None,
        "phase": "explore_execute",
        "producer_candidate_key": "12345678-1234-4234-8234-123456789abc:1:0",
        "project_id": "project-1",
        "protocol": "cairn-candidate-frame-v1",
        "raw_sha256": sha256(raw).hexdigest(),
        "raw_size_bytes": len(raw),
        "record_kind": "candidate",
        "result_schema_sha256": "b" * 64,
        "session": "pi:87654321-4321-4321-8321-cba987654321",
        "task_run_id": "run-1",
    }
    manifest_bytes, frame_bytes = _frame(manifest, raw)
    return PendingCandidateCapture(
        project_id="project-1",
        task_run_id="run-1",
        phase="explore_execute",
        session="pi:87654321-4321-4321-8321-cba987654321",
        capture_id=str(manifest["capture_id"]),
        producer_candidate_key=str(manifest["producer_candidate_key"]),
        record_kind="candidate",
        outcome_sequence=None,
        manifest_bytes=manifest_bytes,
        raw=raw,
        frame_bytes=frame_bytes,
        request_sha256=sha256(manifest_bytes + raw).hexdigest(),
        raw_sha256=sha256(raw).hexdigest(),
    )


def _fence() -> CandidateExecutionFence:
    return CandidateExecutionFence.create(
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


def test_candidate_outbox_persists_exact_binary_entry_owner_only_and_reloads(tmp_path: Path) -> None:
    outbox = CandidateCaptureOutbox(tmp_path / "candidate-outbox", owner_id="dispatcher-1")
    entry = _entry()

    outbox.persist(entry)
    outbox.persist(entry)

    loaded = outbox.entries()
    assert len(loaded) == 1
    assert loaded[0] == entry
    assert loaded[0].manifest_bytes == entry.manifest_bytes
    assert loaded[0].raw == entry.raw
    assert loaded[0].frame_bytes == entry.frame_bytes
    assert stat.S_IMODE(os.lstat(outbox.root).st_mode) == 0o700
    files = [path for path in outbox.root.iterdir() if path.name.endswith(".capture.bin")]
    assert len(files) == 1
    assert stat.S_IMODE(os.lstat(files[0]).st_mode) == 0o600

    restarted = CandidateCaptureOutbox(outbox.root, owner_id="dispatcher-1")
    assert restarted.entries() == [entry]
    restarted.remove(entry)
    assert restarted.entries() == []


def test_candidate_outbox_durably_links_new_root_from_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "nested" / "candidate-outbox"
    synced_directories: list[Path] = []
    original_fsync = os.fsync

    def record_fsync(descriptor: int) -> None:
        synced_directories.append(
            Path(os.readlink(f"/proc/self/fd/{descriptor}")).resolve()
        )
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)

    CandidateCaptureOutbox(root, owner_id="dispatcher-1")

    assert root.parent.resolve() in synced_directories


def test_candidate_outbox_retries_parent_fsync_after_root_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "candidate-outbox"
    original_fsync_directory = CandidateCaptureOutbox._fsync_directory
    parent_calls = 0

    def fail_parent_once(path: Path) -> None:
        nonlocal parent_calls
        if path == root.parent:
            parent_calls += 1
            if parent_calls == 1:
                raise OSError("injected parent fsync failure")
        original_fsync_directory(path)

    monkeypatch.setattr(
        CandidateCaptureOutbox,
        "_fsync_directory",
        staticmethod(fail_parent_once),
    )

    with pytest.raises(CandidateCaptureOutboxError):
        CandidateCaptureOutbox(root, owner_id="dispatcher-1")
    CandidateCaptureOutbox(root, owner_id="dispatcher-1")

    assert parent_calls == 2


def test_candidate_outbox_retries_directory_fsync_for_idempotent_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outbox = CandidateCaptureOutbox(
        tmp_path / "candidate-outbox",
        owner_id="dispatcher-1",
    )
    entry = _entry()
    original_fsync = outbox._fsync_root
    calls = 0

    def fail_once() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected directory fsync failure")
        original_fsync()

    monkeypatch.setattr(outbox, "_fsync_root", fail_once)

    with pytest.raises(CandidateCaptureOutboxError):
        outbox.persist(entry)
    outbox.persist(entry)

    assert calls == 2
    assert outbox.entries() == [entry]


def test_candidate_outbox_reconciles_published_temporary_hard_link(
    tmp_path: Path,
) -> None:
    outbox = CandidateCaptureOutbox(
        tmp_path / "candidate-outbox",
        owner_id="dispatcher-1",
    )
    entry = _entry()
    outbox.persist(entry)
    target = next(outbox.root.glob("*.capture.bin"))
    temporary = outbox.root / f".{target.name}.{'f' * 32}.tmp"
    os.link(target, temporary)
    assert os.lstat(target).st_nlink == 2

    restarted = CandidateCaptureOutbox(
        outbox.root,
        owner_id="dispatcher-1",
    )

    assert not temporary.exists()
    assert os.lstat(target).st_nlink == 1
    assert restarted.entries() == [entry]


@pytest.mark.parametrize("owner_id", [True, 1, [], None])
def test_candidate_outbox_requires_string_owner_id(
    tmp_path: Path,
    owner_id: object,
) -> None:
    with pytest.raises(ValueError, match="string owner_id"):
        CandidateCaptureOutbox(
            tmp_path / "candidate-outbox",
            owner_id=owner_id,  # type: ignore[arg-type]
        )


def test_candidate_outbox_rejects_same_delivery_key_with_different_exact_request(tmp_path: Path) -> None:
    outbox = CandidateCaptureOutbox(tmp_path / "candidate-outbox", owner_id="dispatcher-1")
    outbox.persist(_entry(b"first"))

    with pytest.raises(CandidateCaptureOutboxConflict):
        outbox.persist(_entry(b"second"))

    assert outbox.entries() == [_entry(b"first")]


def test_candidate_execution_fence_is_metadata_only_and_survives_restart(tmp_path: Path) -> None:
    outbox = CandidateCaptureOutbox(tmp_path / "candidate-outbox", owner_id="dispatcher-1")
    fence = CandidateExecutionFence.create(
        owner_id="dispatcher-1",
        project_id="project-1",
        task_run_id="run-1",
        container_name="cairn-project-1",
        phase="explore_execute",
        session="pi:87654321-4321-4321-8321-cba987654321",
        spool_path="/tmp/cairn-prompts/pi-candidates-12345678-1234-4234-8234-123456789abc.bin",
        capture_id="12345678-1234-4234-8234-123456789abc",
        max_candidates=16,
        max_candidate_bytes=2 * 1024 * 1024,
        max_manifest_bytes=4096,
        max_spool_bytes=34 * 1024 * 1024,
        binding_sha256="a" * 64,
        result_schema_sha256="b" * 64,
        docker_daemon_id="daemon-1",
    )

    outbox.persist_fence(fence)

    fence_files = [path for path in outbox.root.iterdir() if path.name.endswith(".fence.json")]
    assert len(fence_files) == 1
    serialized = fence_files[0].read_text(encoding="utf-8")
    assert '"prompt"' not in serialized
    assert '"credential"' not in serialized
    assert '"token"' not in serialized
    assert '"header"' not in serialized
    restarted_fence = CandidateCaptureOutbox(
        outbox.root,
        owner_id="dispatcher-1",
    ).fences()[0]
    assert restarted_fence == fence
    assert restarted_fence.candidate_spool() == WorkerCandidateSpool(
        path=fence.spool_path,
        capture_id=fence.capture_id,
        protocol=fence.spool_protocol,
        max_candidates=fence.max_candidates,
        max_candidate_bytes=fence.max_candidate_bytes,
        max_manifest_bytes=fence.max_manifest_bytes,
        max_spool_bytes=fence.max_spool_bytes,
        binding_sha256="a" * 64,
        result_schema_sha256="b" * 64,
    )

    outbox.remove_fence(fence)
    assert outbox.fences() == []


def test_candidate_execution_fence_preserves_legacy_bound_bytes(
    tmp_path: Path,
) -> None:
    outbox = CandidateCaptureOutbox(
        tmp_path / "candidate-outbox",
        owner_id="dispatcher-1",
    )
    fence = _fence().bind_execution(
        container_id="container-id",
        exec_id="exec-id",
        control_path="/tmp/cairn-exec-0123456789abcdef0123456789abcdef.json",
    )
    outbox.persist_fence(fence)
    fence_path = next(outbox.root.glob("*.fence.json"))
    envelope = json.loads(fence_path.read_text(encoding="utf-8"))
    payload = envelope["payload"]
    payload.pop("docker_daemon_id")
    payload.pop("start_attempted")
    envelope["payload_sha256"] = sha256(_canonical(payload)).hexdigest()
    legacy_bytes = _canonical(envelope)
    fence_path.write_bytes(legacy_bytes)
    fence_path.chmod(0o600)

    loaded = outbox.fences()

    assert len(loaded) == 1
    assert loaded[0].docker_daemon_id is None
    assert loaded[0].start_attempted is None
    assert loaded[0].execution_bound is True
    assert outbox._encode_fence(loaded[0]) == legacy_bytes
    assert outbox.remove_fence(loaded[0]) is True
    assert outbox.fences() == []


def test_legacy_stop_flag_counts_as_one_and_retry_budget_is_durable(
    tmp_path: Path,
) -> None:
    outbox = CandidateCaptureOutbox(tmp_path / "candidate-outbox", owner_id="dispatcher-1")
    started = _fence().bind_execution(
        container_id="container-id",
        exec_id="exec-id",
        control_path="/tmp/cairn-exec-0123456789abcdef0123456789abcdef.json",
    ).mark_start_attempted()
    legacy = replace(started, termination_attempted=True)
    outbox.persist_fence(legacy)
    path = next(outbox.root.glob("*.fence.json"))
    original_bytes = path.read_bytes()
    assert b'"termination_attempts"' not in original_bytes
    assert b'"terminal_proof_version"' not in original_bytes

    loaded = outbox.fences()[0]
    with pytest.raises(CandidateCaptureOutboxError):
        replace(loaded, termination_attempts=True)
    with pytest.raises(CandidateCaptureOutboxError):
        replace(loaded, terminal_proof_version=True)
    assert loaded.termination_attempt_count == 1
    assert outbox._encode_fence(loaded) == original_bytes
    second = loaded.mark_termination_attempted()
    outbox.replace_fence(loaded, second)
    assert CandidateCaptureOutbox(outbox.root, owner_id="dispatcher-1").fences()[0].termination_attempt_count == 2
    third = second.mark_termination_attempted()
    outbox.replace_fence(second, third)
    with pytest.raises(CandidateCaptureOutboxConflict, match="cannot attempt termination"):
        third.mark_termination_attempted()
    assert outbox.fences()[0] == third

    terminal = third.confirm_terminal()
    outbox.replace_fence(third, terminal)
    assert outbox.fences()[0].terminal_proof_version == 2


def test_legacy_terminal_proof_flag_can_only_be_upgraded_via_cas(
    tmp_path: Path,
) -> None:
    outbox = CandidateCaptureOutbox(tmp_path / "candidate-outbox", owner_id="dispatcher-1")
    started = _fence().bind_execution(
        container_id="container-id",
        exec_id="exec-id",
        control_path="/tmp/cairn-exec-0123456789abcdef0123456789abcdef.json",
    ).mark_start_attempted()
    legacy = replace(started, terminal_confirmed=True)
    outbox.persist_fence(legacy)
    path = next(outbox.root.glob("*.fence.json"))
    original_bytes = path.read_bytes()
    assert b'"terminal_proof_version"' not in original_bytes
    assert outbox._encode_fence(outbox.fences()[0]) == original_bytes
    promoted = legacy.confirm_terminal()
    outbox.replace_fence(legacy, promoted)
    assert outbox.fences()[0].terminal_proof_version == 2
    assert promoted.terminal_confirmed is True


def test_candidate_execution_fence_binds_control_and_confirms_terminal_by_cas(
    tmp_path: Path,
) -> None:
    outbox = CandidateCaptureOutbox(
        tmp_path / "candidate-outbox",
        owner_id="dispatcher-1",
    )
    prepared = _fence()
    control_path = "/tmp/cairn-exec-0123456789abcdef0123456789abcdef.json"
    bound = prepared.bind_execution(
        container_id="container-id",
        exec_id="exec-id",
        control_path=control_path,
    )
    started = bound.mark_start_attempted()
    terminal = started.confirm_terminal()

    outbox.persist_fence(prepared)
    outbox.replace_fence(prepared, bound)
    assert outbox.fences() == [bound]
    outbox.replace_fence(bound, started)
    assert outbox.fences() == [started]
    outbox.replace_fence(started, terminal)

    restarted = CandidateCaptureOutbox(
        outbox.root,
        owner_id="dispatcher-1",
    )
    assert restarted.fences() == [terminal]
    assert restarted.remove_fence(terminal) is True
    assert restarted.fences() == []


def test_candidate_outbox_retries_directory_fsync_for_idempotent_fence_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outbox = CandidateCaptureOutbox(
        tmp_path / "candidate-outbox",
        owner_id="dispatcher-1",
    )
    prepared = _fence()
    bound = prepared.bind_execution(
        container_id="container-id",
        exec_id="exec-id",
        control_path="/tmp/cairn-exec-0123456789abcdef0123456789abcdef.json",
    )
    outbox.persist_fence(prepared)
    original_fsync = outbox._fsync_root
    calls = 0

    def fail_once() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected directory fsync failure")
        original_fsync()

    monkeypatch.setattr(outbox, "_fsync_root", fail_once)

    with pytest.raises(CandidateCaptureOutboxError):
        outbox.replace_fence(prepared, bound)
    outbox.replace_fence(prepared, bound)

    assert calls == 2
    assert outbox.fences() == [bound]


def test_candidate_outbox_retries_uncertain_fence_removal_without_losing_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outbox = CandidateCaptureOutbox(
        tmp_path / "candidate-outbox",
        owner_id="dispatcher-1",
    )
    fence = _fence().bind_execution(
        container_id="container-id",
        exec_id="exec-id",
        control_path="/tmp/cairn-exec-0123456789abcdef0123456789abcdef.json",
    ).mark_start_attempted().confirm_terminal()
    outbox.persist_fence(fence)
    original_fsync = outbox._fsync_root
    calls = 0

    def fail_after_fence_unlink() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected fence removal fsync failure")
        original_fsync()

    monkeypatch.setattr(outbox, "_fsync_root", fail_after_fence_unlink)

    with pytest.raises(CandidateCaptureOutboxError):
        outbox.remove_fence(fence)

    assert outbox.fences() == [fence]
    assert outbox.remove_fence(fence) is True
    assert outbox.fences() == []


def test_candidate_execution_fence_rejects_partial_or_changed_control_binding(
    tmp_path: Path,
) -> None:
    prepared = _fence()
    control_path = "/tmp/cairn-exec-0123456789abcdef0123456789abcdef.json"
    bound = prepared.bind_execution(
        container_id="container-id",
        exec_id="exec-id",
        control_path=control_path,
    )

    with pytest.raises(CandidateCaptureOutboxError):
        replace(prepared, container_id="container-id")
    with pytest.raises(CandidateCaptureOutboxError):
        prepared.bind_execution(
            container_id="container-id",
            exec_id="exec-id",
            control_path="/tmp/../candidate-proof.json",
        )
    with pytest.raises(CandidateCaptureOutboxConflict):
        bound.bind_execution(
            container_id="container-id",
            exec_id="exec-id",
            control_path="/tmp/cairn-exec-ffffffffffffffffffffffffffffffff.json",
        )

    outbox = CandidateCaptureOutbox(
        tmp_path / "candidate-outbox",
        owner_id="dispatcher-1",
    )
    outbox.persist_fence(prepared)
    outbox.replace_fence(prepared, bound)
    alternate = prepared.bind_execution(
        container_id="container-id",
        exec_id="exec-id",
        control_path="/tmp/cairn-exec-ffffffffffffffffffffffffffffffff.json",
    )
    with pytest.raises(CandidateCaptureOutboxConflict, match="changed before replacement"):
        outbox.replace_fence(prepared, alternate)
    assert outbox.fences() == [bound]


def test_candidate_execution_fence_rejects_legacy_bound_state_without_control_path(
    tmp_path: Path,
) -> None:
    outbox = CandidateCaptureOutbox(
        tmp_path / "candidate-outbox",
        owner_id="dispatcher-1",
    )
    bound = _fence().bind_execution(
        container_id="container-id",
        exec_id="exec-id",
        control_path="/tmp/cairn-exec-0123456789abcdef0123456789abcdef.json",
    )
    outbox.persist_fence(bound)
    fence_path = next(outbox.root.glob("*.fence.json"))
    envelope = json.loads(fence_path.read_text(encoding="utf-8"))
    payload = envelope["payload"]
    payload.pop("control_path")
    envelope["payload_sha256"] = sha256(_canonical(payload)).hexdigest()
    fence_path.write_bytes(_canonical(envelope))
    fence_path.chmod(0o600)

    with pytest.raises(CandidateCaptureOutboxError, match="invalid candidate fence fields"):
        outbox.fences()


def test_candidate_outbox_rejects_invalid_outcome_sequences() -> None:
    candidate = _entry()
    manifest = {
        "artifact_status": "not_evaluated",
        "binding_status": "not_evaluated",
        "capture_id": candidate.capture_id,
        "code": "schema_invalid",
        "count": None,
        "keyword": None,
        "outcome_sequence": 1,
        "path": None,
        "producer_candidate_key": candidate.producer_candidate_key,
        "protocol": "cairn-candidate-frame-v1",
        "record_kind": "outcome",
    }
    manifest_bytes, frame_bytes = _frame(manifest, b"")

    for invalid_sequence in (0, True):
        with pytest.raises(CandidateCaptureOutboxError):
            PendingCandidateCapture(
                project_id=candidate.project_id,
                task_run_id=candidate.task_run_id,
                phase=candidate.phase,
                session=candidate.session,
                capture_id=candidate.capture_id,
                producer_candidate_key=candidate.producer_candidate_key,
                record_kind="outcome",
                outcome_sequence=invalid_sequence,
                manifest_bytes=manifest_bytes,
                raw=b"",
                frame_bytes=frame_bytes,
                request_sha256=sha256(manifest_bytes).hexdigest(),
                raw_sha256=None,
            )


def test_candidate_outbox_rejects_corrupted_metadata_types(tmp_path: Path) -> None:
    outbox = CandidateCaptureOutbox(tmp_path / "candidate-outbox", owner_id="dispatcher-1")
    outbox.persist(_entry())
    capture_path = next(outbox.root.glob("*.capture.bin"))
    encoded = capture_path.read_bytes()
    metadata_length, frame_length = struct.unpack(">II", encoded[8:16])
    metadata = json.loads(encoded[16 : 16 + metadata_length])
    metadata["project_id"] = 123
    metadata_bytes = _canonical(metadata)
    frame = encoded[16 + metadata_length : 16 + metadata_length + frame_length]
    body = b"CRNCOB01" + struct.pack(">II", len(metadata_bytes), len(frame))
    body += metadata_bytes + frame
    capture_path.write_bytes(body + sha256(body).digest())
    capture_path.chmod(0o600)

    with pytest.raises(CandidateCaptureOutboxError):
        outbox.entries()


def test_candidate_outbox_normalizes_parser_recursion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outbox = CandidateCaptureOutbox(
        tmp_path / "candidate-outbox",
        owner_id="dispatcher-1",
    )
    fence_path = outbox.root / ("a" * 64 + ".fence.json")
    fence_path.write_bytes(b"{}")
    fence_path.chmod(0o600)

    def recurse(*_args, **_kwargs):
        raise RecursionError("injected parser recursion")

    monkeypatch.setattr(
        "cairn.dispatcher.tasks.candidate_capture_outbox.json.loads",
        recurse,
    )

    with pytest.raises(CandidateCaptureOutboxError, match="invalid durable candidate JSON"):
        outbox.fences()


def test_candidate_outbox_rejects_oversized_durable_file(tmp_path: Path) -> None:
    outbox = CandidateCaptureOutbox(tmp_path / "candidate-outbox", owner_id="dispatcher-1")
    oversized = outbox.root / ("a" * 64 + ".capture.bin")
    oversized.write_bytes(b"x" * (3 * 1024 * 1024))
    oversized.chmod(0o600)

    with pytest.raises(CandidateCaptureOutboxError, match="exceeds"):
        outbox.entries()
