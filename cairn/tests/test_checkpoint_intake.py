from __future__ import annotations

from hashlib import sha256
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.tasks import checkpoint_intake
from cairn.dispatcher.workers.base import WorkerCheckpointSpool


class _MaterializedSpool:
    def __init__(self, path: Path):
        self.path = path
        self.size = path.stat().st_size
        self.cleaned = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.cleaned = True


class _Container:
    def __init__(self, path: Path):
        self.path = path
        self.materialized: _MaterializedSpool | None = None

    def materialize_file(self, container_name: str, path: str) -> _MaterializedSpool:
        assert container_name == "worker-container"
        assert path.startswith("/tmp/cairn-prompts/pi-checkpoints-")
        self.materialized = _MaterializedSpool(self.path)
        return self.materialized


class _Client:
    def __init__(self, responses: list[ApiResult | Exception | int]):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def create_execution_event(
        self,
        project_id: str,
        task_run_id: str,
        event: dict[str, object],
    ) -> ApiResult:
        self.calls.append((project_id, task_run_id, event))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, int):
            return ApiResult(status_code=response, data=_receipt(event, response))
        return response


def _event(
    *,
    key: str = "checkpoint-001",
    phase: str = "explore_execute",
    recorded_at: str = "2026-09-18T12:34:56.123Z",
    checkpoint_updates: dict[str, object] | None = None,
    **updates: object,
) -> dict[str, object]:
    checkpoint: dict[str, object] = {
        "category": "observation",
        "summary": "bounded observation",
        "details": {"counts": {"tested": 2}},
    }
    checkpoint.update(checkpoint_updates or {})
    event: dict[str, object] = {
        "protocol": "cairn-execution-event-v1",
        "producer_event_key": key,
        "kind": "checkpoint",
        "phase": phase,
        "recorded_at": recorded_at,
        "checkpoint": checkpoint,
    }
    event.update(updates)
    return event


def _digest(event: dict[str, object]) -> str:
    encoded = json.dumps(
        event,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _receipt(
    event: dict[str, object],
    status: int,
    **updates: object,
) -> dict[str, object]:
    receipt: dict[str, object] = {
        "protocol": "cairn-execution-event-receipt-v1",
        "event_id": "xev_test",
        "producer_event_key": event["producer_event_key"],
        "payload_digest_sha256": _digest(event),
        "disposition": "received" if status == 201 else "replayed",
        "generation_disposition": "current_at_receipt",
        "received_at": "2026-09-18T12:35:00Z",
    }
    receipt.update(updates)
    return receipt


def _write_spool(path: Path, *records: bytes) -> None:
    path.write_bytes(b"".join(records))


def _line(event: dict[str, object]) -> bytes:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def _descriptor(**updates: Any) -> WorkerCheckpointSpool:
    return WorkerCheckpointSpool(
        path="/tmp/cairn-prompts/pi-checkpoints-test.jsonl",
        **updates,
    )


def _intake(
    path: Path,
    client: _Client,
    descriptor: WorkerCheckpointSpool | None = None,
):
    container = _Container(path)
    summary = checkpoint_intake.intake_worker_checkpoints(
        client,
        container,
        "worker-container",
        descriptor or _descriptor(),
        project_id="project-1",
        task_run_id="run-1",
        phase="explore_execute",
    )
    assert container.materialized is not None
    assert container.materialized.cleaned is True
    return summary


def test_intake_keeps_valid_records_when_other_lines_are_untrusted(tmp_path: Path) -> None:
    first = _event(key="checkpoint-1")
    second = _event(
        key="checkpoint-2",
        recorded_at="2026-09-18T12:34:56.123000Z",
        checkpoint_updates={"category": "handoff"},
    )
    incomplete = _event(key="checkpoint-incomplete")
    path = tmp_path / "checkpoints.jsonl"
    _write_spool(
        path,
        _line(first),
        b"{bad-json}\n",
        b'{"invalid":"utf-8-\xff"}\n',
        _line(second),
        json.dumps(incomplete).encode("utf-8"),
    )
    client = _Client([201, 200])

    summary = _intake(path, client)

    assert summary.spool_bytes == path.stat().st_size
    assert summary.events_seen == 5
    assert summary.valid_events == 2
    assert summary.invalid_events == 3
    assert summary.received == 1
    assert summary.replayed == 1
    assert summary.error_code == "spool_framing_invalid"
    assert [call[:2] for call in client.calls] == [
        ("project-1", "run-1"),
        ("project-1", "run-1"),
    ]
    assert client.calls[1][2]["recorded_at"] == "2026-09-18T12:34:56.123Z"


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        (
            b'{"protocol":"cairn-execution-event-v1","producer_event_key":"dup","producer_event_key":"dup-2","kind":"checkpoint","phase":"explore_execute","recorded_at":"2026-09-18T12:34:56.123Z","checkpoint":{"category":"progress","summary":"x","details":{}}}\n',
            "event_duplicate_field",
        ),
        (_line(_event(checkpoint_updates={"details": {"score": float("nan")}})), "event_non_finite_number"),
        (_line(_event(phase="reason_execute")), "event_phase_mismatch"),
        (_line(_event(project_id="spoofed")), "event_shape_invalid"),
        (_line(_event(checkpoint_updates={"details": {"nested": {"filePath": "/tmp/private"}}})), "details_key_invalid"),
        (_line(_event(checkpoint_updates={"details": {"projectid": "spoofed"}})), "details_key_invalid"),
        (_line(_event(checkpoint_updates={"details": {"businessAcceptance": "accepted"}})), "details_key_invalid"),
        (
            _line(
                _event(
                    checkpoint_updates={
                        "details": {"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}
                    }
                )
            ),
            "details_depth_limit",
        ),
        (
            _line(_event(checkpoint_updates={"details": {"items": list(range(65))}})),
            "details_collection_limit",
        ),
    ],
)
def test_intake_rejects_invalid_json_identity_and_details(
    tmp_path: Path,
    content: bytes,
    expected_code: str,
) -> None:
    path = tmp_path / "invalid.jsonl"
    path.write_bytes(content)
    client = _Client([])

    summary = _intake(path, client)

    assert summary.events_seen == 1
    assert summary.valid_events == 0
    assert summary.invalid_events == 1
    assert summary.error_code == expected_code
    assert client.calls == []


def test_intake_enforces_spool_event_and_line_limits(tmp_path: Path) -> None:
    path = tmp_path / "limits.jsonl"
    _write_spool(path, _line(_event(key="one")), _line(_event(key="two")))

    event_limited = _intake(path, _Client([]), _descriptor(max_events=1))
    assert event_limited.error_code == "spool_event_limit"
    assert event_limited.invalid_events == 2

    spool_limited = _intake(path, _Client([]), _descriptor(max_spool_bytes=32))
    assert spool_limited.error_code == "spool_size_limit"
    assert spool_limited.events_seen == 0

    line_limited = _intake(path, _Client([]), _descriptor(max_event_bytes=64))
    assert line_limited.events_seen == 2
    assert line_limited.invalid_events == 2
    assert line_limited.error_code == "event_size_invalid"


def test_delivery_replays_only_same_event_with_bounded_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event()
    path = tmp_path / "retry.jsonl"
    path.write_bytes(_line(event))
    client = _Client([ApiResult(status_code=0), ApiResult(status_code=503), 201])
    delays: list[float] = []
    monkeypatch.setattr(checkpoint_intake.time, "sleep", delays.append)

    summary = _intake(path, client)

    assert summary.received == 1
    assert summary.transient_failures == 0
    assert delays == [0.5, 1.0]
    assert len(client.calls) == 3
    assert client.calls[0][2] == client.calls[1][2] == client.calls[2][2]


def test_missing_or_malformed_receipt_is_repaired_without_rerunning_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event()
    path = tmp_path / "receipt.jsonl"
    path.write_bytes(_line(event))
    invalid_receipt = ApiResult(status_code=201, data={"private": "RESPONSE_SECRET"})
    client = _Client([invalid_receipt, 200])
    delays: list[float] = []
    monkeypatch.setattr(checkpoint_intake.time, "sleep", delays.append)

    summary = _intake(path, client)

    assert summary.replayed == 1
    assert summary.received == 0
    assert summary.transient_failures == 0
    assert delays == [0.5]
    assert len(client.calls) == 2


@pytest.mark.parametrize(
    ("response", "field", "expected"),
    [
        (
            ApiResult(
                status_code=409,
                data={"detail": "execution_event_receipt_conflict"},
                text="PRIVATE_CONFLICT_BODY",
            ),
            "receipt_conflicts",
            1,
        ),
        (ApiResult(status_code=422, text="PRIVATE_REJECTION_BODY"), "permanent_failures", 1),
        (ApiResult(status_code=503, text="PRIVATE_TRANSIENT_BODY"), "transient_failures", 1),
    ],
)
def test_delivery_outcomes_are_separate_and_logs_omit_response_bodies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    response: ApiResult,
    field: str,
    expected: int,
) -> None:
    path = tmp_path / "outcome.jsonl"
    path.write_bytes(_line(_event()))
    client = _Client([response, response, response])
    monkeypatch.setattr(checkpoint_intake.time, "sleep", lambda _delay: None)

    with caplog.at_level(logging.INFO):
        summary = _intake(path, client)

    assert getattr(summary, field) == expected
    assert "PRIVATE_" not in caplog.text
    assert len(client.calls) == (3 if response.status_code == 503 else 1)


def test_exhausted_invalid_receipts_remain_unconfirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "invalid-receipt.jsonl"
    path.write_bytes(_line(_event()))
    response = ApiResult(status_code=201, data={"protocol": "wrong"})
    client = _Client([response, response, response])
    monkeypatch.setattr(checkpoint_intake.time, "sleep", lambda _delay: None)

    summary = _intake(path, client)

    assert summary.received == 0
    assert summary.replayed == 0
    assert summary.transient_failures == 1
    assert summary.error_code == "execution_event_receipt_invalid"
    assert len(client.calls) == 3
