from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import struct

import pytest

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.tasks.candidate_capture_outbox import (
    CandidateCaptureOutbox,
    CandidateCaptureOutboxError,
    PendingCandidateCapture,
)
from cairn.dispatcher.tasks.candidate_intake import (
    CandidateIntakeError,
    decode_candidate_spool,
    intake_worker_candidates,
    replay_candidate_outbox,
)
from cairn.dispatcher.workers.base import WorkerCandidateSpool


_CAPTURE_ID = "12345678-1234-4234-8234-123456789abc"
_SESSION = "pi:87654321-4321-4321-8321-cba987654321"
_BINDING_SHA256 = sha256(b"null").hexdigest()
_SCHEMA_SHA256 = "b" * 64


def _canonical(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _frame(manifest: dict[str, object], raw: bytes) -> bytes:
    manifest_bytes = _canonical(manifest)
    body = b"CRNCND01" + struct.pack(">II", len(manifest_bytes), len(raw)) + manifest_bytes + raw
    return body + sha256(body).digest()


def _candidate_manifest(raw: bytes, **updates: object) -> dict[str, object]:
    manifest: dict[str, object] = {
        "artifacts": [
            {
                "handle": "local-00000000-0000-4000-8000-000000000001",
                "kind": "log",
                "sha256": "a" * 64,
                "size_bytes": 9,
            }
        ],
        "assistant_turn": 1,
        "binding_sha256": _BINDING_SHA256,
        "capture_id": _CAPTURE_ID,
        "content_index": 0,
        "parent_producer_candidate_key": None,
        "phase": "explore_execute",
        "producer_candidate_key": f"{_CAPTURE_ID}:1:0",
        "project_id": "project-1",
        "protocol": "cairn-candidate-frame-v1",
        "raw_sha256": sha256(raw).hexdigest(),
        "raw_size_bytes": len(raw),
        "record_kind": "candidate",
        "result_schema_sha256": _SCHEMA_SHA256,
        "session": _SESSION,
        "task_run_id": "run-1",
    }
    manifest.update(updates)
    return manifest


def _outcome_manifest(**updates: object) -> dict[str, object]:
    manifest: dict[str, object] = {
        "artifact_status": "not_evaluated",
        "binding_status": "not_evaluated",
        "capture_id": _CAPTURE_ID,
        "code": "submission_invalid",
        "count": 1,
        "keyword": "required",
        "outcome_sequence": 1,
        "path": "/data",
        "producer_candidate_key": f"{_CAPTURE_ID}:1:0",
        "protocol": "cairn-candidate-frame-v1",
        "record_kind": "outcome",
    }
    manifest.update(updates)
    return manifest


def _descriptor() -> WorkerCandidateSpool:
    return WorkerCandidateSpool(
        path=f"/tmp/cairn-prompts/pi-candidates-{_CAPTURE_ID}.bin",
        capture_id=_CAPTURE_ID,
        binding_sha256=_BINDING_SHA256,
        result_schema_sha256=_SCHEMA_SHA256,
    )


def _decode(spool: bytes) -> list[PendingCandidateCapture]:
    return decode_candidate_spool(
        spool,
        _descriptor(),
        project_id="project-1",
        task_run_id="run-1",
        phase="explore_execute",
        session=_SESSION,
    )


def test_decode_candidate_spool_preserves_exact_raw_and_ordered_outcome() -> None:
    raw = b'{\r\n"accepted": tru, "note": "\xe9\x9b\xaa"\r\n}'
    candidate = _frame(_candidate_manifest(raw), raw)
    outcome = _frame(_outcome_manifest(), b"")

    entries = _decode(candidate + outcome)

    assert [entry.record_kind for entry in entries] == ["candidate", "outcome"]
    assert entries[0].raw == raw
    assert entries[0].frame_bytes == candidate
    assert entries[0].manifest_bytes == candidate[16 : 16 + len(_canonical(_candidate_manifest(raw)))]
    assert entries[0].raw_sha256 == sha256(raw).hexdigest()
    assert entries[1].raw == b""
    assert entries[1].frame_bytes == outcome
    assert entries[1].outcome_sequence == 1
    assert entries[1].raw_sha256 is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda frame: frame[:-1],
        lambda frame: frame[:-1] + bytes([frame[-1] ^ 1]),
        lambda frame: b"BADMAGIC" + frame[8:],
        lambda frame: frame + b"tail",
    ],
    ids=["truncated", "checksum", "magic", "trailing-tail"],
)
def test_decode_candidate_spool_rejects_any_corrupt_frame(mutate) -> None:
    raw = b"{}"
    frame = _frame(_candidate_manifest(raw), raw)

    with pytest.raises(CandidateIntakeError):
        _decode(mutate(frame))


def test_decode_candidate_spool_normalizes_parser_recursion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b"{}"
    frame = _frame(_candidate_manifest(raw), raw)

    def recurse(*_args, **_kwargs):
        raise RecursionError("injected parser recursion")

    monkeypatch.setattr(
        "cairn.dispatcher.tasks.candidate_intake.json.loads",
        recurse,
    )

    with pytest.raises(CandidateIntakeError, match="valid UTF-8 JSON"):
        _decode(frame)


def test_decode_candidate_spool_rejects_noncanonical_or_wrong_authority_manifest() -> None:
    raw = b"{}"
    manifest = _candidate_manifest(raw)
    noncanonical = json.dumps(manifest, ensure_ascii=False).encode()
    body = b"CRNCND01" + struct.pack(">II", len(noncanonical), len(raw)) + noncanonical + raw
    noncanonical_frame = body + sha256(body).digest()

    with pytest.raises(CandidateIntakeError, match="canonical"):
        _decode(noncanonical_frame)
    with pytest.raises(CandidateIntakeError, match="authority"):
        _decode(_frame(_candidate_manifest(raw, task_run_id="other"), raw))
    with pytest.raises(CandidateIntakeError, match="binding"):
        _decode(_frame(_candidate_manifest(raw, binding_sha256="c" * 64), raw))


def test_decode_candidate_spool_rejects_boolean_raw_size() -> None:
    raw = b"x"

    with pytest.raises(CandidateIntakeError, match="raw identity"):
        _decode(_frame(_candidate_manifest(raw, raw_size_bytes=True), raw))


def test_decode_candidate_spool_rejects_nonmonotonic_or_oversized_coordinates() -> None:
    first_raw = b"first"
    second_raw = b"second"
    first_key = f"{_CAPTURE_ID}:2:0"
    first = _frame(
        _candidate_manifest(
            first_raw,
            assistant_turn=2,
            producer_candidate_key=first_key,
        ),
        first_raw,
    )
    second = _frame(
        _candidate_manifest(
            second_raw,
            assistant_turn=1,
            content_index=1,
            producer_candidate_key=f"{_CAPTURE_ID}:1:1",
            parent_producer_candidate_key=first_key,
            raw_sha256=sha256(second_raw).hexdigest(),
            raw_size_bytes=len(second_raw),
        ),
        second_raw,
    )

    with pytest.raises(CandidateIntakeError, match="producer identity"):
        _decode(first + second)
    with pytest.raises(CandidateIntakeError, match="producer identity"):
        _decode(
            _frame(
                _candidate_manifest(
                    b"{}",
                    assistant_turn=1_000_001,
                    producer_candidate_key=f"{_CAPTURE_ID}:1000001:0",
                ),
                b"{}",
            )
        )


def test_decode_candidate_spool_requires_immediately_previous_parent() -> None:
    first_raw = b"first"
    second_raw = b"second"
    third_raw = b"third"
    first_key = f"{_CAPTURE_ID}:1:0"
    second_key = f"{_CAPTURE_ID}:2:0"
    spool = b"".join(
        (
            _frame(_candidate_manifest(first_raw), first_raw),
            _frame(
                _candidate_manifest(
                    second_raw,
                    assistant_turn=2,
                    producer_candidate_key=second_key,
                    parent_producer_candidate_key=first_key,
                    raw_sha256=sha256(second_raw).hexdigest(),
                    raw_size_bytes=len(second_raw),
                ),
                second_raw,
            ),
            _frame(
                _candidate_manifest(
                    third_raw,
                    assistant_turn=3,
                    producer_candidate_key=f"{_CAPTURE_ID}:3:0",
                    parent_producer_candidate_key=first_key,
                    raw_sha256=sha256(third_raw).hexdigest(),
                    raw_size_bytes=len(third_raw),
                ),
                third_raw,
            ),
        )
    )

    with pytest.raises(CandidateIntakeError, match="lineage"):
        _decode(spool)


def test_decode_candidate_spool_rejects_invalid_lineage_outcome_and_artifacts() -> None:
    first_raw = b"first"
    second_raw = b"second"
    first = _frame(_candidate_manifest(first_raw), first_raw)

    bad_parent = _candidate_manifest(
        second_raw,
        assistant_turn=2,
        producer_candidate_key=f"{_CAPTURE_ID}:2:0",
        parent_producer_candidate_key="missing",
        raw_sha256=sha256(second_raw).hexdigest(),
        raw_size_bytes=len(second_raw),
    )
    with pytest.raises(CandidateIntakeError, match="lineage"):
        _decode(first + _frame(bad_parent, second_raw))

    with pytest.raises(CandidateIntakeError, match="outcome"):
        _decode(first + _frame(_outcome_manifest(outcome_sequence=2), b""))

    duplicate_artifacts = [
        {
            "handle": "local-00000000-0000-4000-8000-000000000002",
            "kind": "log",
            "sha256": "a" * 64,
            "size_bytes": 1,
        },
        {
            "handle": "local-00000000-0000-4000-8000-000000000002",
            "kind": "file",
            "sha256": "b" * 64,
            "size_bytes": 2,
        },
    ]
    with pytest.raises(CandidateIntakeError, match="unique and sorted"):
        _decode(_frame(_candidate_manifest(first_raw, artifacts=duplicate_artifacts), first_raw))


@pytest.mark.parametrize(
    "updates",
    [
        {"code": "arbitrary_canary"},
        {"path": None},
        {"keyword": "unknownKeyword"},
        {"path": "/not/a/schema/path"},
        {"keyword": "strict_json", "path": "/data"},
        {"count": 2},
        {
            "code": "locally_validated",
            "path": None,
            "keyword": None,
            "count": None,
        },
        {
            "code": "provider_error",
            "path": "/data",
            "keyword": "required",
        },
    ],
    ids=[
        "unknown-code",
        "unpaired-path-keyword",
        "unknown-keyword",
        "unknown-schema-path",
        "strict-json-nonroot",
        "count-over-one",
        "locally-validated-without-count",
        "non-submission-code-with-schema-issue",
    ],
)
def test_decode_candidate_spool_rejects_non_authoritative_outcomes(
    updates: dict[str, object],
) -> None:
    raw = b"{}"
    candidate = _frame(_candidate_manifest(raw), raw)

    with pytest.raises(CandidateIntakeError, match="diagnostic"):
        _decode(candidate + _frame(_outcome_manifest(**updates), b""))


@pytest.mark.parametrize(
    "updates",
    [
        {"artifact_status": []},
        {"binding_status": {}},
        {"keyword": [], "path": "/data"},
    ],
    ids=["artifact-status-type", "binding-status-type", "keyword-type"],
)
def test_decode_candidate_spool_rejects_non_string_outcome_enums(
    updates: dict[str, object],
) -> None:
    raw = b"{}"
    candidate = _frame(_candidate_manifest(raw), raw)

    with pytest.raises(CandidateIntakeError, match="diagnostic"):
        _decode(candidate + _frame(_outcome_manifest(**updates), b""))


def test_decode_candidate_spool_bounds_outcomes_per_candidate() -> None:
    raw = b"{}"
    candidate = _frame(_candidate_manifest(raw), raw)
    outcomes = b"".join(
        _frame(_outcome_manifest(outcome_sequence=sequence), b"")
        for sequence in range(1, 66)
    )

    with pytest.raises(CandidateIntakeError, match="outcome identity"):
        _decode(candidate + outcomes)


@pytest.mark.parametrize(
    "kind",
    ["log", "file", "report", "screenshot", "patch", "raw_output"],
)
def test_decode_candidate_spool_accepts_registry_artifact_kinds(kind: str) -> None:
    raw = b"{}"
    artifact = {
        "handle": "local-proof",
        "kind": kind,
        "sha256": "a" * 64,
        "size_bytes": 0,
    }

    entries = _decode(_frame(_candidate_manifest(raw, artifacts=[artifact]), raw))

    assert entries[0].record_kind == "candidate"


@pytest.mark.parametrize(
    "artifact",
    [
        {
            "handle": "evidence-1",
            "kind": "log",
            "sha256": "a" * 64,
            "size_bytes": 1,
        },
        {
            "handle": "local-proof",
            "kind": "http_exchange",
            "sha256": "a" * 64,
            "size_bytes": 1,
        },
        {
            "handle": "local-" + "a" * 123,
            "kind": "log",
            "sha256": "a" * 64,
            "size_bytes": 1,
        },
    ],
    ids=["nonlocal-handle", "unknown-kind", "oversized-handle"],
)
def test_decode_candidate_spool_rejects_non_registry_artifact_identity(
    artifact: dict[str, object],
) -> None:
    raw = b"{}"

    with pytest.raises(CandidateIntakeError, match="artifact identity"):
        _decode(_frame(_candidate_manifest(raw, artifacts=[artifact]), raw))


class _MaterializedCandidateSpool:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.size = path.stat().st_size

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info: object) -> None:
        return None


class _ContainerManager:
    def __init__(self, path: Path) -> None:
        self.path = path

    def materialize_file(
        self,
        container_name: str,
        path: str,
    ) -> _MaterializedCandidateSpool:
        assert container_name == "worker-container"
        assert path == _descriptor().path
        return _MaterializedCandidateSpool(self.path)


class _CandidateClient:
    def __init__(self, responses: list[ApiResult]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, str, bytes, bytes]] = []

    def capture_candidate(
        self,
        project_id: str,
        task_run_id: str,
        manifest_bytes: bytes,
        raw: bytes,
    ) -> ApiResult:
        self.requests.append((project_id, task_run_id, manifest_bytes, raw))
        return self.responses.pop(0)


def _receipt(
    entry: PendingCandidateCapture,
    *,
    status: int = 201,
    **updates: object,
) -> ApiResult:
    data: dict[str, object] = {
        "protocol": "cairn-candidate-capture-receipt-v1",
        "record_kind": entry.record_kind,
        "candidate_id": "candidate-server-1",
        "producer_candidate_key": entry.producer_candidate_key,
        "outcome_sequence": entry.outcome_sequence,
        "request_sha256": entry.request_sha256,
        "raw_sha256": entry.raw_sha256,
        "disposition": "received" if status == 201 else "replayed",
        "generation_disposition": "current_at_receipt",
        "received_at": "2026-09-19T12:00:00Z",
    }
    data.update(updates)
    return ApiResult(status_code=status, data=data)


def _intake(
    tmp_path: Path,
    spool: bytes,
    client: _CandidateClient,
):
    outbox = CandidateCaptureOutbox(
        tmp_path / "candidate-outbox",
        owner_id="dispatcher-1",
    )
    spool_path = tmp_path / "candidate-spool.bin"
    spool_path.write_bytes(spool)
    summary = intake_worker_candidates(
        client,  # type: ignore[arg-type]
        _ContainerManager(spool_path),  # type: ignore[arg-type]
        "worker-container",
        _descriptor(),
        outbox,
        project_id="project-1",
        task_run_id="run-1",
        phase="explore_execute",
        session=_SESSION,
    )
    return summary, outbox


def test_candidate_intake_persists_all_frames_then_retries_identical_unknown_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b'{\r\n"accepted": tru, "note": "\xe9\x9b\xaa"\r\n}'
    candidate_frame = _frame(_candidate_manifest(raw), raw)
    outcome_frame = _frame(_outcome_manifest(), b"")
    entries = _decode(candidate_frame + outcome_frame)
    client = _CandidateClient(
        [
            ApiResult(status_code=201, data={}),
            ApiResult(status_code=201, data={}),
            _receipt(entries[0]),
            _receipt(entries[1]),
        ]
    )
    monkeypatch.setattr("cairn.dispatcher.tasks.candidate_intake.time.sleep", lambda _: None)

    summary, outbox = _intake(tmp_path, candidate_frame + outcome_frame, client)

    assert summary.host_durable is True
    assert summary.frames_seen == 2
    assert summary.received == 2
    assert summary.pending == 0
    assert summary.integrity_error is None
    assert outbox.entries() == []
    assert client.requests[:3] == [
        ("project-1", "run-1", entries[0].manifest_bytes, raw),
        ("project-1", "run-1", entries[0].manifest_bytes, raw),
        ("project-1", "run-1", entries[0].manifest_bytes, raw),
    ]
    assert client.requests[3] == (
        "project-1",
        "run-1",
        entries[1].manifest_bytes,
        b"",
    )


def test_candidate_intake_preserves_fence_on_outbox_remove_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b"{}"
    frame = _frame(_candidate_manifest(raw), raw)
    entry = _decode(frame)[0]
    client = _CandidateClient([_receipt(entry)])

    def fail_remove(
        _outbox: CandidateCaptureOutbox,
        _entry: PendingCandidateCapture,
    ) -> bool:
        raise CandidateCaptureOutboxError("injected remove failure")

    monkeypatch.setattr(CandidateCaptureOutbox, "remove", fail_remove)

    summary, outbox = _intake(tmp_path, frame, client)

    assert summary.host_durable is True
    assert summary.received == 0
    assert summary.pending == 1
    assert summary.integrity_error == "candidate_outbox_invalid"
    assert summary.blocked_project_ids == {"project-1"}
    assert outbox.entries() == [entry]


def test_candidate_intake_retains_same_outbox_entry_on_transport_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b"{}"
    frame = _frame(_candidate_manifest(raw), raw)
    client = _CandidateClient([ApiResult(status_code=503)] * 3)
    monkeypatch.setattr("cairn.dispatcher.tasks.candidate_intake.time.sleep", lambda _: None)

    summary, outbox = _intake(tmp_path, frame, client)

    assert summary.host_durable is True
    assert summary.pending == 1
    assert summary.integrity_error is None
    assert len(outbox.entries()) == 1
    assert len(client.requests) == 3
    assert all(request == client.requests[0] for request in client.requests)


def test_candidate_intake_retains_and_blocks_on_strict_receipt_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b"{}"
    frame = _frame(_candidate_manifest(raw), raw)
    entry = _decode(frame)[0]
    client = _CandidateClient([_receipt(entry, request_sha256="0" * 64)])
    monkeypatch.setattr("cairn.dispatcher.tasks.candidate_intake.time.sleep", lambda _: None)

    summary, outbox = _intake(tmp_path, frame, client)

    assert summary.host_durable is True
    assert summary.pending == 1
    assert summary.integrity_error == "candidate_receipt_conflict"
    assert summary.blocked_project_ids == {"project-1"}
    assert outbox.entries() == [entry]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("record_kind", []),
        ("disposition", {}),
        ("generation_disposition", []),
    ],
)
def test_candidate_intake_treats_malformed_receipt_enums_as_unknown_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    raw = b"{}"
    frame = _frame(_candidate_manifest(raw), raw)
    entry = _decode(frame)[0]
    malformed = _receipt(entry, **{field: value})
    client = _CandidateClient([malformed, malformed, malformed])
    monkeypatch.setattr("cairn.dispatcher.tasks.candidate_intake.time.sleep", lambda _: None)

    summary, outbox = _intake(tmp_path, frame, client)

    assert summary.host_durable is True
    assert summary.pending == 1
    assert summary.integrity_error is None
    assert outbox.entries() == [entry]


def test_candidate_intake_propagates_decoder_programming_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b"{}"
    frame = _frame(_candidate_manifest(raw), raw)

    def fail_decode(*_args, **_kwargs):
        raise RuntimeError("decoder programming bug")

    monkeypatch.setattr(
        "cairn.dispatcher.tasks.candidate_intake.decode_candidate_spool",
        fail_decode,
    )

    with pytest.raises(RuntimeError, match="decoder programming bug"):
        _intake(tmp_path, frame, _CandidateClient([]))


def test_candidate_intake_never_persists_prefix_of_invalid_spool(tmp_path: Path) -> None:
    raw = b"{}"
    valid = _frame(_candidate_manifest(raw), raw)
    invalid = _frame(_outcome_manifest(), b"")[:-1]
    client = _CandidateClient([])

    summary, outbox = _intake(tmp_path, valid + invalid, client)

    assert summary.host_durable is False
    assert summary.integrity_error == "candidate_spool_invalid"
    assert outbox.entries() == []
    assert client.requests == []


def test_candidate_replay_bounds_each_pass(tmp_path: Path) -> None:
    raw = b"{}"
    entries = _decode(
        _frame(_candidate_manifest(raw), raw) + _frame(_outcome_manifest(), b"")
    )
    outbox = CandidateCaptureOutbox(
        tmp_path / "candidate-outbox",
        owner_id="dispatcher-1",
    )
    for entry in entries:
        outbox.persist(entry)
    client = _CandidateClient([_receipt(entries[0])])

    summary = replay_candidate_outbox(
        client,  # type: ignore[arg-type]
        outbox,
        max_entries=1,
    )

    assert summary.frames_seen == 1
    assert summary.received == 1
    assert len(client.requests) == 1
    assert outbox.entries() == [entries[1]]


def test_candidate_replay_excludes_projects_owned_by_active_futures(tmp_path: Path) -> None:
    entry = _decode(_frame(_candidate_manifest(b"{}"), b"{}"))[0]
    outbox = CandidateCaptureOutbox(
        tmp_path / "candidate-outbox",
        owner_id="dispatcher-1",
    )
    outbox.persist(entry)
    client = _CandidateClient([])

    summary = replay_candidate_outbox(
        client,  # type: ignore[arg-type]
        outbox,
        excluded_project_ids={entry.project_id},
    )

    assert summary.frames_seen == 0
    assert client.requests == []
    assert outbox.entries() == [entry]


def test_candidate_replay_excludes_only_the_locally_owned_run(tmp_path: Path) -> None:
    raw = b"{}"
    orphan = _decode(_frame(_candidate_manifest(raw), raw))[0]
    sibling_capture = "22345678-1234-4234-8234-123456789abc"
    sibling_manifest = _candidate_manifest(
        raw, capture_id=sibling_capture,
        producer_candidate_key=f"{sibling_capture}:1:0", task_run_id="run-2",
    )
    sibling = decode_candidate_spool(
        _frame(sibling_manifest, raw),
        WorkerCandidateSpool(
            path=f"/tmp/cairn-prompts/pi-candidates-{sibling_capture}.bin",
            capture_id=sibling_capture,
            binding_sha256=_BINDING_SHA256,
            result_schema_sha256=_SCHEMA_SHA256,
        ),
        project_id="project-1", task_run_id="run-2",
        phase="explore_execute", session=_SESSION,
    )[0]
    outbox = CandidateCaptureOutbox(tmp_path / "candidate-outbox", owner_id="dispatcher-1")
    outbox.persist(orphan)
    outbox.persist(sibling)
    client = _CandidateClient([_receipt(orphan)])

    summary = replay_candidate_outbox(
        client, outbox, excluded_task_runs={("project-1", "run-2")},
    )

    assert summary.received == 1
    assert len(client.requests) == 1
    assert outbox.entries() == [sibling]


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_candidate_replay_rejects_invalid_entry_limit(
    tmp_path: Path,
    limit: object,
) -> None:
    outbox = CandidateCaptureOutbox(
        tmp_path / "candidate-outbox",
        owner_id="dispatcher-1",
    )

    with pytest.raises(ValueError, match="positive integer"):
        replay_candidate_outbox(
            _CandidateClient([]),  # type: ignore[arg-type]
            outbox,
            max_entries=limit,  # type: ignore[arg-type]
        )


def test_candidate_replay_propagates_programming_errors_after_durable_persist(
    tmp_path: Path,
) -> None:
    entry = _decode(_frame(_candidate_manifest(b"{}"), b"{}"))[0]
    outbox = CandidateCaptureOutbox(
        tmp_path / "candidate-outbox",
        owner_id="dispatcher-1",
    )
    outbox.persist(entry)

    class _BrokenClient:
        def capture_candidate(self, *_args: object, **_kwargs: object) -> ApiResult:
            raise ValueError("programming bug")

    with pytest.raises(ValueError, match="programming bug"):
        replay_candidate_outbox(
            _BrokenClient(),  # type: ignore[arg-type]
            outbox,
        )

    assert outbox.entries() == [entry]
