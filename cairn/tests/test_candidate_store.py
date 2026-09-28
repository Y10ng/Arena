from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path

import pytest
from fastapi import Response

from cairn.dispatcher.worker_protocol import submission as submission_protocol
from cairn.dispatcher.worker_protocol.submission import build_submit_result_schema
from cairn.server import db
from cairn.server.app import app
from cairn.server.auth import create_machine_principal, create_user_with_personal_tenant, login
from cairn.server.schemas.candidates import canonical_json_bytes, canonical_value_sha256
from cairn.server.services import candidate_store as candidate_store_service

_SESSION = "pi:11111111-1111-4111-8111-111111111111"
_CAPTURE = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def configured_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db._reset_for_tests()
    database_path = tmp_path / "candidates.db"
    monkeypatch.setenv("CAIRN_DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("CAIRN_TRUSTED_ORIGINS", "http://testserver")
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
    body: bytes = b"",
    manifest: dict[str, object] | None = None,
    manifest_bytes: bytes | None = None,
    content_type: str = "application/octet-stream",
    duplicate_manifest_header: bool = False,
) -> tuple[int, dict[str, str], bytes]:
    headers = [
        (b"host", b"testserver"),
        (b"content-type", content_type.encode()),
        (b"content-length", str(len(body)).encode()),
        (b"origin", b"http://testserver"),
    ]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    if session_cookie is not None:
        headers.append((b"cookie", f"cairn_session={session_cookie}".encode()))
    if manifest is not None and manifest_bytes is not None:
        raise AssertionError("manifest and manifest_bytes are mutually exclusive")
    if manifest is not None:
        manifest_bytes = canonical_json_bytes(manifest)
    if manifest_bytes is not None:
        encoded = base64.urlsafe_b64encode(manifest_bytes).rstrip(b"=")
        headers.append((b"x-cairn-candidate-manifest", encoded))
        if duplicate_manifest_header:
            headers.append((b"x-cairn-candidate-manifest", encoded))

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
    response_headers = {
        key.decode().casefold(): value.decode()
        for key, value in start.get("headers", [])
    }
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return int(start["status"]), response_headers, response_body


def _json(body: bytes) -> object:
    return json.loads(body.decode("utf-8"))


def _seed_identity(*, project_generation: int = 0, run_generation: int = 0) -> dict[str, str]:
    _, tenant_id = create_user_with_personal_tenant("candidate-owner", "safe-password")
    owner_cookie = _session("candidate-owner", "safe-password")
    _, foreign_tenant_id = create_user_with_personal_tenant(
        "candidate-foreign", "safe-password"
    )
    foreign_cookie = _session("candidate-foreign", "safe-password")
    machine_id, machine_token = create_machine_principal(
        "candidate-dispatcher", "candidate-token"
    )
    _other_machine_id, other_machine_token = create_machine_principal(
        "candidate-other-dispatcher", "candidate-other-token"
    )
    binding = None
    delivery = {
        "session_id": _SESSION,
        "phase": "explore_execute",
        "deadline_at": "2026-09-19T12:00:00Z",
        "container_id": "container-candidate",
        "model_binding_sha256": "a" * 64,
        "result_schema_sha256": canonical_value_sha256(
            build_submit_result_schema("explore_execute", binding=binding)
        ),
        "binding": binding,
        "inputs": {},
    }
    context = {
        "dispatcher_id": machine_id,
        "execution_delivery": delivery,
    }
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO projects (
                id, title, status, created_at, tenant_id, execution_generation
            ) VALUES ('candidate-project', 'Candidates', 'active',
                      '2026-09-19T00:00:00Z', ?, ?)
            """,
            (tenant_id, project_generation),
        )
        conn.execute(
            """
            INSERT INTO projects (
                id, title, status, created_at, tenant_id, execution_generation
            ) VALUES ('candidate-foreign-project', 'Foreign', 'active',
                      '2026-09-19T00:00:00Z', ?, 0)
            """,
            (foreign_tenant_id,),
        )
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, task_type, worker_name, worker_type, status,
                session_id, runtime_context_json, execution_generation
            ) VALUES ('candidate-run', 'candidate-project', 'explore',
                      'worker', 'pi', 'running', ?, ?, ?)
            """,
            (_SESSION, json.dumps(context), run_generation),
        )
    return {
        "owner_cookie": owner_cookie,
        "foreign_cookie": foreign_cookie,
        "machine_token": machine_token,
        "other_machine_token": other_machine_token,
    }


def _candidate_manifest(
    raw: bytes,
    *,
    capture_id: str = _CAPTURE,
    assistant_turn: int = 1,
    content_index: int = 0,
    parent_producer_candidate_key: str | None = None,
    **updates: object,
) -> dict[str, object]:
    manifest: dict[str, object] = {
        "protocol": "cairn-candidate-frame-v1",
        "record_kind": "candidate",
        "capture_id": capture_id,
        "producer_candidate_key": f"{capture_id}:{assistant_turn}:{content_index}",
        "parent_producer_candidate_key": parent_producer_candidate_key,
        "assistant_turn": assistant_turn,
        "content_index": content_index,
        "project_id": "candidate-project",
        "task_run_id": "candidate-run",
        "phase": "explore_execute",
        "session": _SESSION,
        "binding_sha256": canonical_value_sha256(None),
        "result_schema_sha256": canonical_value_sha256(
            build_submit_result_schema("explore_execute", binding=None)
        ),
        "raw_sha256": sha256(raw).hexdigest(),
        "raw_size_bytes": len(raw),
        "artifacts": [],
    }
    manifest.update(updates)
    return manifest


def _outcome_manifest(
    *,
    sequence: int = 1,
    **updates: object,
) -> dict[str, object]:
    manifest: dict[str, object] = {
        "protocol": "cairn-candidate-frame-v1",
        "record_kind": "outcome",
        "capture_id": _CAPTURE,
        "producer_candidate_key": f"{_CAPTURE}:1:0",
        "outcome_sequence": sequence,
        "code": "submission_invalid",
        "path": "/data",
        "keyword": "required",
        "count": 1,
        "artifact_status": "not_evaluated",
        "binding_status": "not_evaluated",
    }
    manifest.update(updates)
    return manifest


def _post_candidate(
    token: str,
    manifest: dict[str, object],
    raw: bytes,
) -> tuple[int, dict[str, str], bytes]:
    return _request(
        "POST",
        "/projects/candidate-project/task-runs/candidate-run/candidates",
        authorization=f"Bearer {token}",
        manifest=manifest,
        body=raw,
    )


def test_frozen_delivery_uses_type_sensitive_identity() -> None:
    identity = _seed_identity()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT runtime_context_json FROM task_runs "
            "WHERE project_id='candidate-project' AND id='candidate-run'"
        ).fetchone()
        context = json.loads(row["runtime_context_json"])
        context["execution_delivery"]["inputs"] = {"budget": 1}
        conn.execute(
            "UPDATE task_runs SET runtime_context_json=? "
            "WHERE project_id='candidate-project' AND id='candidate-run'",
            (json.dumps(context),),
        )

    mutated = json.loads(json.dumps(context["execution_delivery"]))
    mutated["inputs"]["budget"] = True
    status, _, body = _request(
        "PATCH",
        "/projects/candidate-project/task-runs/candidate-run",
        authorization=f"Bearer {identity['machine_token']}",
        body=json.dumps(
            {"runtime_context": {"execution_delivery": mutated}}
        ).encode("utf-8"),
        content_type="application/json",
    )

    assert status == 422, body
    assert _json(body)["detail"] == "execution_delivery_is_server_owned"
    with db.get_conn() as conn:
        persisted = conn.execute(
            "SELECT runtime_context_json FROM task_runs "
            "WHERE project_id='candidate-project' AND id='candidate-run'"
        ).fetchone()
    assert json.loads(persisted["runtime_context_json"])["execution_delivery"] == context[
        "execution_delivery"
    ]


def test_capabilities_are_machine_only_and_always_required() -> None:
    # 与 Pi JSON.stringify 规范化向量共享，防止跨语言 digest 悄然漂移。
    assert canonical_value_sha256(None) == (
        "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b"
    )
    assert canonical_json_bytes(
        {"\U00010000": 1, "": 2, "中文": "值", "numbers": [-0.0, 1e-6, 1e-7, 1e21]}
    ) == (
        '{"numbers":[0,0.000001,1e-7,1e+21],"中文":"值",'
        '"𐀀":1,"":2}'
    ).encode("utf-8")
    assert canonical_value_sha256(
        build_submit_result_schema("explore_execute", binding=None)
    ) == "03f431a1b170e8d6f8823a2ba54c99194b4959328157c72ce4fba55f539eea9d"
    identity = _seed_identity()
    path = "/candidates/capabilities"

    status, _, body = _request("GET", path)
    assert (status, _json(body)["detail"]) == (401, "Machine authentication required")
    status, _, body = _request(
        "GET", path, session_cookie=identity["owner_cookie"]
    )
    assert (status, _json(body)["detail"]) == (401, "Machine authentication required")

    status, headers, body = _request(
        "GET", path, authorization=f"Bearer {identity['machine_token']}"
    )
    assert status == 200
    assert headers["cache-control"] == "private, no-store"
    assert _json(body) == {
        "required": True,
        "spool_protocol": "cairn-candidate-spool-v1",
        "frame_protocol": "cairn-candidate-frame-v1",
        "receipt_protocol": "cairn-candidate-capture-receipt-v1",
        "manifest_header": "X-Cairn-Candidate-Manifest",
        "manifest_encoding": "base64url-utf8-canonical-json-no-padding",
        "content_type": "application/octet-stream",
        "source_format": "pi-toolcall-delta-utf8-v1",
        "max_candidates_per_task_run": 16,
        "max_raw_bytes": 2 * 1024 * 1024,
        "max_manifest_bytes": 4 * 1024,
        "max_spool_bytes": 34 * 1024 * 1024,
        "max_outcomes_per_candidate": 64,
        "max_artifacts_per_candidate": 16,
    }


def test_exact_raw_replay_conflict_outcome_and_tenant_safe_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _seed_identity()
    raw = b'{"broken":\r\n"\xe4\xb8\xad\xe6\x96\x87",}'
    manifest = _candidate_manifest(raw)

    def deployed_schema_must_not_be_rebuilt(*_args, **_kwargs):
        raise AssertionError("candidate capture rebuilt the deployed result schema")

    monkeypatch.setattr(
        submission_protocol,
        "build_submit_result_schema",
        deployed_schema_must_not_be_rebuilt,
    )

    status, _, body = _request(
        "POST",
        "/projects/candidate-project/task-runs/candidate-run/candidates",
        authorization=f"Bearer {identity['machine_token']}",
        manifest=manifest,
        body=raw,
        duplicate_manifest_header=True,
    )
    assert (status, _json(body)["detail"]) == (
        422,
        "candidate_manifest_header_ambiguous",
    )

    status, _, body = _post_candidate(identity["other_machine_token"], manifest, raw)
    assert (status, _json(body)["detail"]) == (403, "execution_owner_mismatch")

    status, _, body = _post_candidate(identity["machine_token"], manifest, raw)
    first = _json(body)
    assert status == 201
    assert first["disposition"] == "received"
    assert first["record_kind"] == "candidate"
    assert first["raw_sha256"] == sha256(raw).hexdigest()
    assert first["request_sha256"] == sha256(canonical_json_bytes(manifest) + raw).hexdigest()

    status, _, body = _post_candidate(identity["machine_token"], manifest, raw)
    replay = _json(body)
    assert status == 200
    assert replay == {**first, "disposition": "replayed"}

    conflicting_raw = b"different"
    conflicting = _candidate_manifest(conflicting_raw)
    status, _, body = _post_candidate(
        identity["machine_token"], conflicting, conflicting_raw
    )
    assert (status, _json(body)["detail"]) == (409, "candidate_receipt_conflict")

    status, _, body = _post_candidate(
        identity["machine_token"], _outcome_manifest(), b""
    )
    outcome = _json(body)
    assert status == 201
    assert outcome["record_kind"] == "outcome"
    assert outcome["candidate_id"] == first["candidate_id"]
    assert outcome["outcome_sequence"] == 1
    assert outcome["raw_sha256"] is None

    status, _, body = _post_candidate(
        identity["machine_token"], _outcome_manifest(), b""
    )
    assert status == 200
    assert _json(body)["disposition"] == "replayed"
    status, _, body = _post_candidate(
        identity["machine_token"],
        _outcome_manifest(
            code="result_schema",
            path=None,
            keyword=None,
            count=None,
        ),
        b"",
    )
    assert (status, _json(body)["detail"]) == (
        409,
        "candidate_outcome_receipt_conflict",
    )

    list_path = "/projects/candidate-project/task-runs/candidate-run/candidates"
    status, _, body = _request(
        "GET", list_path, session_cookie=identity["foreign_cookie"]
    )
    assert (status, _json(body)["detail"]) == (404, "Project not found")
    status, _, body = _request(
        "GET",
        list_path,
        authorization=f"Bearer {identity['machine_token']}",
    )
    assert (status, _json(body)["detail"]) == (401, "Authentication required")
    status, headers, body = _request(
        "GET", list_path, session_cookie=identity["owner_cookie"]
    )
    metadata = _json(body)
    assert status == 200
    assert headers["cache-control"] == "private, no-store"
    assert metadata == [
        {
            "candidate_id": first["candidate_id"],
            "task_run_id": "candidate-run",
            "protocol": "cairn-candidate-frame-v1",
            "source_format": "pi-toolcall-delta-utf8-v1",
            "phase": "explore_execute",
            "assistant_turn": 1,
            "content_index": 0,
            "parent_candidate_id": None,
            "raw_size_bytes": len(raw),
            "received_at": first["received_at"],
            "execution_generation": 0,
            "generation_disposition": "current_at_receipt",
            "latest_outcome_code": "submission_invalid",
            "latest_outcome_path": "/data",
            "latest_outcome_keyword": "required",
            "latest_outcome_count": 1,
            "artifact_status": "not_evaluated",
            "binding_status": "not_evaluated",
        }
    ]
    for private_value in (
        raw,
        manifest["producer_candidate_key"].encode(),
        manifest["session"].encode(),
        manifest["binding_sha256"].encode(),
        manifest["result_schema_sha256"].encode(),
        identity["machine_token"].encode(),
    ):
        assert private_value not in body

    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO task_run_candidate_outcomes (
                id, candidate_id, project_id, task_run_id, protocol,
                producer_candidate_key, outcome_sequence, code,
                diagnostic_path, diagnostic_keyword, diagnostic_count,
                artifact_status, binding_status, request_sha256,
                manifest_json, verified_dispatcher_id, execution_generation,
                generation_disposition, received_at
            ) VALUES (
                'cno_unsafe_metadata', ?, 'candidate-project', 'candidate-run',
                'cairn-candidate-frame-v1', ?, 2, 'raw_boundary_canary',
                '/data/RAW_BOUNDARY_CANARY', 'RAW_BOUNDARY_CANARY', 1,
                'not_evaluated', 'not_evaluated', ?, '{}',
                'candidate-dispatcher', 0, 'current_at_receipt',
                '2026-09-19T00:00:03Z'
            )
            """,
            (
                first["candidate_id"],
                manifest["producer_candidate_key"],
                "f" * 64,
            ),
        )
    status, _, body = _request(
        "GET", list_path, session_cookie=identity["owner_cookie"]
    )
    assert (status, _json(body)["detail"]) == (
        409,
        "candidate_outcome_diagnostic_invalid",
    )
    assert b"RAW_BOUNDARY_CANARY" not in body

    raw_path = f"{list_path}/{first['candidate_id']}/raw"
    status, _, body = _request(
        "GET", raw_path, session_cookie=identity["foreign_cookie"]
    )
    assert (status, _json(body)["detail"]) == (404, "Project not found")
    status, headers, body = _request(
        "GET", raw_path, session_cookie=identity["owner_cookie"]
    )
    assert status == 200
    assert body == raw
    assert headers["content-type"] == "application/octet-stream"
    assert headers["cache-control"] == "private, no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["content-disposition"].endswith('.bin"')


def test_outcome_replay_and_metadata_use_the_frozen_acceptance_witness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _seed_identity()
    raw = b'{}'
    status, _, body = _post_candidate(
        identity["machine_token"], _candidate_manifest(raw), raw
    )
    assert status == 201, body
    status, _, body = _post_candidate(
        identity["machine_token"], _outcome_manifest(), b""
    )
    assert status == 201, body

    monkeypatch.setattr(
        candidate_store_service,
        "submission_schema_paths",
        lambda _phase: frozenset({"$"}),
    )

    status, _, body = _post_candidate(
        identity["machine_token"], _outcome_manifest(), b""
    )
    assert status == 200, body
    assert _json(body)["disposition"] == "replayed"

    status, _, body = _request(
        "GET",
        "/projects/candidate-project/task-runs/candidate-run/candidates",
        session_cookie=identity["owner_cookie"],
    )
    assert status == 200, body
    assert _json(body)[0]["latest_outcome_path"] == "/data"

    status, _, body = _post_candidate(
        identity["machine_token"], _outcome_manifest(sequence=2), b""
    )
    assert (status, _json(body)["detail"]) == (
        422,
        "candidate_outcome_diagnostic_invalid",
    )


def test_candidate_replay_uses_the_frozen_admission_witness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _seed_identity()
    raw = b'{"note":"future-policy-marker"}'
    manifest = _candidate_manifest(raw)
    status, _, body = _post_candidate(identity["machine_token"], manifest, raw)
    assert status == 201, body

    monkeypatch.setattr(
        candidate_store_service,
        "_PRIVATE_KEY_MARKERS",
        (*candidate_store_service._PRIVATE_KEY_MARKERS, b"future-policy-marker"),
    )

    status, _, body = _post_candidate(identity["machine_token"], manifest, raw)
    assert status == 200, body
    assert _json(body)["disposition"] == "replayed"

    new_capture = "77777777-7777-4777-8777-777777777777"
    status, _, body = _post_candidate(
        identity["machine_token"],
        _candidate_manifest(raw, capture_id=new_capture, assistant_turn=2),
        raw,
    )
    assert (status, _json(body)["detail"]) == (
        422,
        "candidate_sensitive_content_rejected",
    )


def test_deeply_nested_manifest_is_rejected_without_server_failure() -> None:
    identity = _seed_identity()
    manifest_bytes = b'{"x":' + b"[" * 1_100 + b"0" + b"]" * 1_100 + b"}"

    status, _, body = _request(
        "POST",
        "/projects/candidate-project/task-runs/candidate-run/candidates",
        authorization=f"Bearer {identity['machine_token']}",
        manifest_bytes=manifest_bytes,
    )

    assert (status, _json(body)["detail"]) == (422, "candidate_manifest_invalid")


def test_canonical_manifest_delivery_identity_lineage_and_generation() -> None:
    identity = _seed_identity(project_generation=2, run_generation=1)
    raw = b"{}"
    path = "/projects/candidate-project/task-runs/candidate-run/candidates"

    noncanonical = json.dumps(_candidate_manifest(raw), indent=2).encode()
    status, _, body = _request(
        "POST",
        path,
        authorization=f"Bearer {identity['machine_token']}",
        manifest_bytes=noncanonical,
        body=raw,
    )
    assert (status, _json(body)["detail"]) == (422, "candidate_manifest_not_canonical")

    checks = (
        ("phase", "report", "candidate_phase_mismatch"),
        (
            "session",
            "pi:33333333-3333-4333-8333-333333333333",
            "candidate_session_mismatch",
        ),
        ("binding_sha256", "b" * 64, "candidate_binding_digest_mismatch"),
        (
            "result_schema_sha256",
            "c" * 64,
            "candidate_result_schema_digest_mismatch",
        ),
    )
    for field, value, error in checks:
        status, _, body = _post_candidate(
            identity["machine_token"],
            _candidate_manifest(raw, **{field: value}),
            raw,
        )
        assert (status, _json(body)["detail"]) == (409, error)

    missing_parent = "44444444-4444-4444-8444-444444444444:1:0"
    status, _, body = _post_candidate(
        identity["machine_token"],
        _candidate_manifest(raw, parent_producer_candidate_key=missing_parent),
        raw,
    )
    assert (status, _json(body)["detail"]) == (409, "candidate_parent_missing")

    status, _, body = _post_candidate(
        identity["machine_token"], _candidate_manifest(raw), raw
    )
    parent = _json(body)
    assert status == 201
    assert parent["generation_disposition"] == "historical_at_receipt"

    status, _, body = _post_candidate(
        identity["machine_token"],
        _candidate_manifest(
            raw,
            assistant_turn=2,
            content_index=1,
            parent_producer_candidate_key=f"{_CAPTURE}:1:0",
        ),
        raw,
    )
    child = _json(body)
    assert status == 201

    cross_capture = "55555555-5555-4555-8555-555555555555"
    status, _, body = _post_candidate(
        identity["machine_token"],
        _candidate_manifest(
            raw,
            capture_id=cross_capture,
            assistant_turn=3,
            parent_producer_candidate_key=f"{_CAPTURE}:2:1",
        ),
        raw,
    )
    assert (status, _json(body)["detail"]) == (
        409,
        "candidate_parent_capture_mismatch",
    )

    status, _, body = _post_candidate(
        identity["machine_token"],
        _candidate_manifest(
            raw,
            assistant_turn=3,
            parent_producer_candidate_key=f"{_CAPTURE}:1:0",
        ),
        raw,
    )
    assert (status, _json(body)["detail"]) == (
        409,
        "candidate_parent_lineage_mismatch",
    )

    status, _, body = _post_candidate(
        identity["machine_token"],
        _candidate_manifest(
            raw,
            assistant_turn=2,
            content_index=0,
            parent_producer_candidate_key=f"{_CAPTURE}:2:1",
        ),
        raw,
    )
    assert (status, _json(body)["detail"]) == (
        409,
        "candidate_position_not_monotonic",
    )

    status, _, body = _request(
        "GET",
        path,
        session_cookie=identity["owner_cookie"],
    )
    rows = _json(body)
    assert rows[1]["candidate_id"] == child["candidate_id"]
    assert rows[1]["parent_candidate_id"] == parent["candidate_id"]

    with db.get_conn() as conn:
        stored_context = json.loads(
            conn.execute(
                "SELECT runtime_context_json FROM task_runs WHERE id = 'candidate-run'"
            ).fetchone()["runtime_context_json"]
        )
        stored_context["execution_delivery"]["result_schema_sha256"] = "invalid"
        conn.execute(
            "UPDATE task_runs SET runtime_context_json = ? WHERE id = 'candidate-run'",
            (json.dumps(stored_context),),
        )
    new_capture = "77777777-7777-4777-8777-777777777777"
    status, _, body = _post_candidate(
        identity["machine_token"],
        _candidate_manifest(raw, capture_id=new_capture, assistant_turn=4),
        raw,
    )
    assert (status, _json(body)["detail"]) == (
        409,
        "candidate_execution_delivery_unavailable",
    )

    del stored_context["execution_delivery"]["result_schema_sha256"]
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE task_runs SET runtime_context_json = ? WHERE id = 'candidate-run'",
            (json.dumps(stored_context),),
        )
    status, _, body = _post_candidate(
        identity["machine_token"],
        _candidate_manifest(raw, capture_id=new_capture, assistant_turn=4),
        raw,
    )
    assert (status, _json(body)["detail"]) == (
        409,
        "candidate_execution_delivery_unavailable",
    )


def test_rejects_mismatched_raw_outcome_identity_private_key_and_future_generation() -> None:
    identity = _seed_identity(project_generation=0, run_generation=1)
    raw = b"{}"
    status, _, body = _post_candidate(
        identity["machine_token"], _candidate_manifest(raw), raw
    )
    assert (status, _json(body)["detail"]) == (409, "candidate_future_generation")

    with db.get_conn() as conn:
        conn.execute(
            "UPDATE task_runs SET execution_generation = 0 "
            "WHERE project_id = 'candidate-project' AND id = 'candidate-run'"
        )
    status, _, body = _post_candidate(
        identity["machine_token"],
        _candidate_manifest(raw, raw_size_bytes=3),
        raw,
    )
    assert (status, _json(body)["detail"]) == (422, "candidate_raw_size_mismatch")
    status, _, body = _post_candidate(
        identity["machine_token"],
        _candidate_manifest(raw, raw_sha256="d" * 64),
        raw,
    )
    assert (status, _json(body)["detail"]) == (422, "candidate_raw_digest_mismatch")

    for private_raw in (
        b"-----BEGIN PRIVATE KEY-----\ncanary",
        b"-----BEGIN ENCRYPTED PRIVATE KEY-----\ncanary",
        b"-----BEGIN DSA PRIVATE KEY-----\ncanary",
        b"-----BEGIN PGP PRIVATE KEY BLOCK-----\ncanary",
    ):
        status, _, body = _post_candidate(
            identity["machine_token"],
            _candidate_manifest(private_raw),
            private_raw,
        )
        assert (status, _json(body)["detail"]) == (
            422,
            "candidate_sensitive_content_rejected",
        )

    status, _, body = _post_candidate(
        identity["machine_token"], _candidate_manifest(raw), raw
    )
    assert status == 201
    status, _, body = _post_candidate(
        identity["machine_token"],
        _outcome_manifest(capture_id="66666666-6666-4666-8666-666666666666"),
        b"",
    )
    assert (status, _json(body)["detail"]) == (
        409,
        "candidate_capture_identity_mismatch",
    )
    status, _, body = _post_candidate(
        identity["machine_token"], _outcome_manifest(), b"not-empty"
    )
    assert (status, _json(body)["detail"]) == (
        422,
        "candidate_outcome_raw_must_be_empty",
    )

    unsafe_diagnostics = (
        {"code": "raw_boundary_canary"},
        {"path": "/data/RAW_BOUNDARY_CANARY"},
        {"keyword": "RAW_BOUNDARY_CANARY"},
        {"count": 2},
        {"path": None},
        {"keyword": "strict_json", "path": "/data"},
    )
    for updates in unsafe_diagnostics:
        status, _, body = _post_candidate(
            identity["machine_token"],
            _outcome_manifest(**updates),
            b"",
        )
        assert (status, _json(body)["detail"]) == (
            422,
            "candidate_outcome_diagnostic_invalid",
        )
        assert b"RAW_BOUNDARY_CANARY" not in body


def test_concurrent_same_candidate_has_one_receipt_and_deterministic_replay() -> None:
    identity = _seed_identity()
    raw = b'{"candidate":"concurrent"}'
    manifest = _candidate_manifest(raw)

    def post(_: int) -> tuple[int, dict[str, str], bytes]:
        return _post_candidate(identity["machine_token"], manifest, raw)

    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(post, range(4)))

    assert sorted(status for status, _, _ in responses) == [200, 200, 200, 201]
    receipts = [_json(body) for _, _, body in responses]
    assert {receipt["candidate_id"] for receipt in receipts} == {
        receipts[0]["candidate_id"]
    }
    assert {receipt["request_sha256"] for receipt in receipts} == {
        receipts[0]["request_sha256"]
    }
    assert sorted(receipt["disposition"] for receipt in receipts) == [
        "received",
        "replayed",
        "replayed",
        "replayed",
    ]
    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM task_run_candidates"
        ).fetchone()["count"] == 1


def test_candidate_limit_is_per_task_run_and_repairs_are_append_only() -> None:
    identity = _seed_identity()
    raw = b"{}"
    previous: str | None = None
    ids: list[str] = []
    for index in range(16):
        manifest = _candidate_manifest(
            raw,
            assistant_turn=index + 1,
            parent_producer_candidate_key=previous,
        )
        status, _, body = _post_candidate(identity["machine_token"], manifest, raw)
        assert status == 201
        ids.append(_json(body)["candidate_id"])
        previous = manifest["producer_candidate_key"]

    status, _, body = _post_candidate(
        identity["machine_token"],
        _candidate_manifest(
            raw,
            assistant_turn=17,
            parent_producer_candidate_key=previous,
        ),
        raw,
    )
    assert (status, _json(body)["detail"]) == (409, "candidate_limit_reached")
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, parent_candidate_id, assistant_turn FROM task_run_candidates
            WHERE project_id = 'candidate-project' AND task_run_id = 'candidate-run'
            ORDER BY assistant_turn
            """
        ).fetchall()
    assert len(rows) == 16
    assert rows[0]["parent_candidate_id"] is None
    assert {row["id"] for row in rows} == set(ids)
