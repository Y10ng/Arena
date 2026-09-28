from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Barrier

import pytest
from fastapi import HTTPException, Response

from cairn.server import db
from cairn.server.app import app
from cairn.server.auth import (
    RequestPrincipal,
    create_machine_principal,
    create_user_with_personal_tenant,
    login,
)
from cairn.server.schemas.execution_events import ExecutionEventEnvelope
from cairn.server.services import execution_events
from cairn.server.services.execution_events import append_execution_event


@pytest.fixture(autouse=True)
def configured_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db._reset_for_tests()
    database_path = tmp_path / "execution-events.db"
    monkeypatch.setenv("CAIRN_DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("CAIRN_TRUSTED_ORIGINS", "http://testserver")
    monkeypatch.delenv("CAIRN_EXECUTION_EVENTS_ENABLED", raising=False)
    db.configure(database_path)
    yield
    db._reset_for_tests()


def _session(login_name: str, password: str) -> str:
    response = Response()
    login(response, login_name, password)
    return response.headers["set-cookie"].split("cairn_session=", 1)[1].split(";", 1)[0]


def _request(
    method: str,
    path: str,
    *,
    authorization: str | None = None,
    session_cookie: str | None = None,
    payload: dict[str, object] | None = None,
) -> tuple[int, object, str]:
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else b""
    headers = [
        (b"host", b"testserver"),
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"origin", b"http://testserver"),
    ]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    if session_cookie is not None:
        headers.append((b"cookie", f"cairn_session={session_cookie}".encode()))
    raw_path, _, query_string = path.partition("?")
    messages: list[dict[str, object]] = []
    received = False

    async def receive() -> dict[str, object]:
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": raw_path,
        "raw_path": raw_path.encode(),
        "query_string": query_string.encode(),
        "root_path": "",
        "headers": headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))
    start = next(message for message in messages if message["type"] == "http.response.start")
    raw = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    ).decode()
    return int(start["status"]), json.loads(raw), raw


def _seed_identity() -> dict[str, str]:
    _, tenant_id = create_user_with_personal_tenant("event-owner", "safe-password")
    owner_cookie = _session("event-owner", "safe-password")
    _, foreign_tenant_id = create_user_with_personal_tenant("event-foreign", "safe-password")
    foreign_cookie = _session("event-foreign", "safe-password")
    machine_id, machine_token = create_machine_principal("event-dispatcher", "event-token")
    other_machine_id, other_machine_token = create_machine_principal(
        "other-dispatcher", "other-event-token"
    )
    context = {
        "dispatcher_id": machine_id,
        "execution_delivery": {"phase": "explore_execute"},
    }
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO projects (
                id, title, status, created_at, tenant_id, execution_generation
            ) VALUES ('event-project', 'Execution events', 'active', '2026-09-17T00:00:00Z', ?, 0)
            """,
            (tenant_id,),
        )
        conn.execute(
            """
            INSERT INTO projects (
                id, title, status, created_at, tenant_id, execution_generation
            ) VALUES ('foreign-project', 'Foreign', 'active', '2026-09-17T00:00:00Z', ?, 0)
            """,
            (foreign_tenant_id,),
        )
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, task_type, worker_name, worker_type, status,
                runtime_context_json, execution_generation
            ) VALUES ('run-1', 'event-project', 'explore', 'display-worker', 'pi',
                      'running', ?, 0)
            """,
            (json.dumps(context),),
        )
    return {
        "tenant_id": tenant_id,
        "owner_cookie": owner_cookie,
        "foreign_cookie": foreign_cookie,
        "machine_id": machine_id,
        "machine_token": machine_token,
        "other_machine_id": other_machine_id,
        "other_machine_token": other_machine_token,
    }


def _event_payload(
    *,
    checkpoint_updates: dict[str, object] | None = None,
    **updates: object,
) -> dict[str, object]:
    checkpoint: dict[str, object] = {
        "category": "observation",
        "summary": "Observed a bounded execution checkpoint",
        "details": {"counts": {"tested": 2, "remaining": 1}},
    }
    checkpoint.update(checkpoint_updates or {})
    payload: dict[str, object] = {
        "protocol": "cairn-execution-event-v1",
        "producer_event_key": "checkpoint-001",
        "kind": "checkpoint",
        "phase": "explore_execute",
        "recorded_at": "2026-09-17T12:34:56.123Z",
        "checkpoint": checkpoint,
    }
    payload.update(updates)
    return payload


def test_capabilities_are_machine_only_and_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    identity = _seed_identity()
    path = "/execution-events/capabilities"

    status, error, _ = _request("GET", path)
    assert (status, error["detail"]) == (401, "Machine authentication required")
    status, error, _ = _request("GET", path, session_cookie=identity["owner_cookie"])
    assert (status, error["detail"]) == (401, "Machine authentication required")
    status, capabilities, _ = _request(
        "GET", path, authorization=f"Bearer {identity['machine_token']}"
    )
    assert status == 200
    assert capabilities == {
        "enabled": False,
        "protocol": "cairn-execution-event-v1",
        "kinds": ["checkpoint"],
        "categories": [
            "progress",
            "observation",
            "hypothesis",
            "limitation",
            "handoff",
        ],
        "max_events": 64,
        "max_event_bytes": 16384,
        "max_spool_bytes": 524288,
        "max_summary_chars": 1000,
        "max_details_bytes": 16384,
        "max_details_depth": 6,
    }
    status, error, _ = _request(
        "POST",
        "/projects/event-project/task-runs/run-1/execution-events",
        authorization=f"Bearer {identity['machine_token']}",
        payload=_event_payload(),
    )
    assert (status, error["detail"]) == (409, "execution_events_disabled")

    monkeypatch.setenv("CAIRN_EXECUTION_EVENTS_ENABLED", "true")
    status, capabilities, _ = _request(
        "GET", path, authorization=f"Bearer {identity['machine_token']}"
    )
    assert status == 200
    assert capabilities["enabled"] is True


def test_machine_append_replay_conflict_and_tenant_safe_read(monkeypatch: pytest.MonkeyPatch) -> None:
    identity = _seed_identity()
    monkeypatch.setenv("CAIRN_EXECUTION_EVENTS_ENABLED", "1")
    path = "/projects/event-project/task-runs/run-1/execution-events"
    payload = _event_payload(
        producer_event_key="PRIVATE_PRODUCER_KEY",
        checkpoint_updates={
            "summary": "PRIVATE_SUMMARY_CANARY",
            "details": {"note": "PRIVATE_DETAILS_CANARY", "counts": {"tested": 2}},
        },
    )

    status, error, _ = _request(
        "POST", path, session_cookie=identity["owner_cookie"], payload=payload
    )
    assert (status, error["detail"]) == (401, "Machine authentication required")
    status, error, _ = _request(
        "POST",
        path,
        authorization=f"Bearer {identity['other_machine_token']}",
        payload=payload,
    )
    assert (status, error["detail"]) == (403, "execution_owner_mismatch")

    status, first, _ = _request(
        "POST",
        path,
        authorization=f"Bearer {identity['machine_token']}",
        payload=payload,
    )
    assert status == 201
    assert set(first) == {
        "protocol",
        "event_id",
        "producer_event_key",
        "payload_digest_sha256",
        "disposition",
        "generation_disposition",
        "received_at",
    }
    assert first["protocol"] == "cairn-execution-event-receipt-v1"
    assert first["producer_event_key"] == "PRIVATE_PRODUCER_KEY"
    assert first["disposition"] == "received"
    assert first["generation_disposition"] == "current_at_receipt"
    assert len(first["payload_digest_sha256"]) == 64

    reordered = _event_payload(
        producer_event_key="PRIVATE_PRODUCER_KEY",
        checkpoint_updates={
            "summary": "PRIVATE_SUMMARY_CANARY",
            "details": {"counts": {"tested": 2}, "note": "PRIVATE_DETAILS_CANARY"},
        },
    )
    status, replay, _ = _request(
        "POST",
        path,
        authorization=f"Bearer {identity['machine_token']}",
        payload=reordered,
    )
    assert status == 200
    assert replay["disposition"] == "replayed"
    assert replay["event_id"] == first["event_id"]
    assert replay["payload_digest_sha256"] == first["payload_digest_sha256"]
    assert replay["received_at"] == first["received_at"]

    status, error, _ = _request(
        "POST",
        path,
        authorization=f"Bearer {identity['machine_token']}",
        payload=_event_payload(
            producer_event_key="PRIVATE_PRODUCER_KEY",
            checkpoint_updates={"summary": "different receipt content"},
        ),
    )
    assert (status, error["detail"]) == (409, "execution_event_receipt_conflict")

    status, events, raw = _request("GET", path, session_cookie=identity["owner_cookie"])
    assert status == 200
    assert len(events) == 1
    assert events[0]["event_id"] == first["event_id"]
    assert set(events[0]) == {
        "event_id",
        "task_run_id",
        "protocol",
        "kind",
        "category",
        "phase",
        "producer_recorded_at",
        "received_at",
        "execution_generation",
        "generation_disposition",
    }
    assert events[0]["generation_disposition"] == "current_at_receipt"
    for private_value in (
        "PRIVATE_PRODUCER_KEY",
        "PRIVATE_SUMMARY_CANARY",
        "PRIVATE_DETAILS_CANARY",
        identity["machine_id"],
        first["payload_digest_sha256"],
        "payload_json",
        "verified_dispatcher_id",
    ):
        assert private_value not in raw

    status, error, _ = _request("GET", path, session_cookie=identity["foreign_cookie"])
    assert (status, error["detail"]) == (404, "Project not found")
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM execution_events").fetchone()["n"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        _event_payload(protocol="cairn-execution-event-v2"),
        _event_payload(kind="observation"),
        _event_payload(producer_event_key="unsafe/key"),
        _event_payload(recorded_at="2026-09-17T12:34:56+00:00"),
        _event_payload(checkpoint_updates={"category": "artifact"}),
        _event_payload(artifacts=[]),
        _event_payload(artifact_refs=[]),
        _event_payload(paths=[]),
        _event_payload(project_id="spoofed"),
        _event_payload(task_run_id="spoofed"),
        _event_payload(owner_subject="spoofed"),
        _event_payload(execution_generation=99),
        _event_payload(checkpoint_updates={"artifacts": []}),
        _event_payload(checkpoint_updates={"details": {"artifact_path": "/tmp/private"}}),
        _event_payload(checkpoint_updates={"details": {"artifactid": "artifact-1"}}),
        _event_payload(checkpoint_updates={"details": {"evidence_refs": ["artifact-1"]}}),
        _event_payload(checkpoint_updates={"details": {"nested": {"path": "/tmp/private"}}}),
        _event_payload(checkpoint_updates={"details": {"filepath": "/tmp/private"}}),
        _event_payload(checkpoint_updates={"details": {"projectid": "spoofed"}}),
        _event_payload(checkpoint_updates={"summary": "x" * 1001}),
        _event_payload(
            checkpoint_updates={
                "details": {"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}
            }
        ),
        _event_payload(
            checkpoint_updates={"details": {"values": ["x" * 4000 for _ in range(5)]}}
        ),
    ],
)
def test_strict_envelope_rejects_artifacts_paths_server_fields_and_bounds(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    identity = _seed_identity()
    monkeypatch.setenv("CAIRN_EXECUTION_EVENTS_ENABLED", "1")
    status, _, _ = _request(
        "POST",
        "/projects/event-project/task-runs/run-1/execution-events",
        authorization=f"Bearer {identity['machine_token']}",
        payload=payload,
    )
    assert status == 422


def test_phase_and_generation_integrity_accept_history_reject_future(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _seed_identity()
    monkeypatch.setenv("CAIRN_EXECUTION_EVENTS_ENABLED", "1")
    path = "/projects/event-project/task-runs/run-1/execution-events"

    status, error, _ = _request(
        "POST",
        path,
        authorization=f"Bearer {identity['machine_token']}",
        payload=_event_payload(phase="reason_execute"),
    )
    assert (status, error["detail"]) == (409, "execution_event_phase_mismatch")

    context = {
        "dispatcher_id": identity["machine_id"],
        "execution_delivery": {"phase": "explore_execute"},
    }
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET execution_generation = 2 WHERE id = 'event-project'")
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, task_type, worker_name, worker_type, status,
                runtime_context_json, execution_generation
            ) VALUES ('run-future', 'event-project', 'explore', 'display-worker', 'pi',
                      'running', ?, 3)
            """,
            (json.dumps(context),),
        )

    status, historical, _ = _request(
        "POST",
        path,
        authorization=f"Bearer {identity['machine_token']}",
        payload=_event_payload(),
    )
    assert status == 201
    assert historical["generation_disposition"] == "historical_at_receipt"

    status, error, _ = _request(
        "POST",
        "/projects/event-project/task-runs/run-future/execution-events",
        authorization=f"Bearer {identity['machine_token']}",
        payload=_event_payload(producer_event_key="future-checkpoint"),
    )
    assert (status, error["detail"]) == (409, "execution_event_future_generation")


def test_phase_requires_frozen_execution_delivery_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    identity = _seed_identity()
    monkeypatch.setenv("CAIRN_EXECUTION_EVENTS_ENABLED", "1")
    context = {
        "dispatcher_id": identity["machine_id"],
        "assessment_phase": "explore_execute",
    }
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE task_runs SET runtime_context_json = ? WHERE project_id = ? AND id = ?",
            (json.dumps(context), "event-project", "run-1"),
        )

    status, error, _ = _request(
        "POST",
        "/projects/event-project/task-runs/run-1/execution-events",
        authorization=f"Bearer {identity['machine_token']}",
        payload=_event_payload(),
    )
    assert (status, error["detail"]) == (409, "execution_event_phase_unavailable")


def test_task_run_event_limit_rejects_new_keys_but_allows_receipt_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _seed_identity()
    monkeypatch.setenv("CAIRN_EXECUTION_EVENTS_ENABLED", "1")
    monkeypatch.setattr(execution_events, "EXECUTION_EVENT_MAX_EVENTS", 2)
    path = "/projects/event-project/task-runs/run-1/execution-events"
    authorization = f"Bearer {identity['machine_token']}"

    first_payload = _event_payload(producer_event_key="checkpoint-1")
    for key in ("checkpoint-1", "checkpoint-2"):
        status, receipt, _ = _request(
            "POST",
            path,
            authorization=authorization,
            payload=_event_payload(producer_event_key=key),
        )
        assert status == 201
        assert receipt["disposition"] == "received"

    status, error, _ = _request(
        "POST",
        path,
        authorization=authorization,
        payload=_event_payload(producer_event_key="checkpoint-3"),
    )
    assert (status, error["detail"]) == (409, "execution_event_limit_reached")

    status, replay, _ = _request(
        "POST",
        path,
        authorization=authorization,
        payload=first_payload,
    )
    assert status == 200
    assert replay["disposition"] == "replayed"
    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM execution_events"
        ).fetchone()["n"] == 2


@pytest.mark.parametrize("conflicting", [False, True])
def test_concurrent_receipt_race_is_idempotent_or_conflicts(
    monkeypatch: pytest.MonkeyPatch,
    conflicting: bool,
) -> None:
    identity = _seed_identity()
    monkeypatch.setenv("CAIRN_EXECUTION_EVENTS_ENABLED", "1")
    principal = RequestPrincipal(kind="machine", subject_id=identity["machine_id"])
    barrier = Barrier(4)

    def submit(index: int):
        body = ExecutionEventEnvelope.model_validate(
            _event_payload(
                checkpoint_updates={
                    "summary": f"contender-{index}" if conflicting else "same-content"
                }
            )
        )
        barrier.wait(timeout=10)
        try:
            return append_execution_event(
                "event-project",
                "run-1",
                body,
                principal=principal,
            )
        except HTTPException as exc:
            assert conflicting
            assert (exc.status_code, exc.detail) == (409, "execution_event_receipt_conflict")
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = [receipt for receipt in pool.map(submit, range(4)) if receipt is not None]

    assert len(receipts) == (1 if conflicting else 4)
    assert len({receipt.event_id for receipt in receipts}) == 1
    assert {receipt.disposition for receipt in receipts} == (
        {"received"} if conflicting else {"received", "replayed"}
    )
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM execution_events").fetchone()["n"] == 1
