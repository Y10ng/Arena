from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

import pytest
import requests

from cairn.server import db
from cairn.server.app import app
from cairn.server.models import DispatcherConnectivityTestStart
from cairn.server.routers import infra
from cairn.server.services.dispatcher_config_store import DispatcherConfigStore
from cairn.server.services import connectivity_tests


ROOT = Path(__file__).resolve().parents[2]


class FakeResponse:
    def __init__(self, status_code: int, payload: object):
        self.status_code = status_code
        self._body = json.dumps(payload).encode("utf-8")
        self.closed = False

    def iter_content(self, chunk_size: int = 4096):
        yield self._body

    def close(self) -> None:
        self.closed = True


def error_body(message: str, error_type: str = "api_error") -> bytes:
    return json.dumps({"type": "error", "error": {"type": error_type, "message": message}}).encode()


def test_connectivity_response_classifies_success_auth_not_found_rate_limit_and_model() -> None:
    success = connectivity_tests.classify_connectivity_response(
        200,
        json.dumps({"type": "message", "content": []}).encode(),
        scope="messages_api",
        credential="secret",
        latency_ms=12,
    )
    assert success.category == "ok"

    for status in (401, 403):
        outcome = connectivity_tests.classify_connectivity_response(
            status,
            error_body("bad credential", "authentication_error"),
            scope="messages_api",
            credential="secret",
            latency_ms=10,
        )
        assert outcome.category == "auth"

    missing_endpoint = connectivity_tests.classify_connectivity_response(
        404,
        error_body("route not found", "not_found_error"),
        scope="messages_api",
        credential="secret",
        latency_ms=10,
    )
    assert missing_endpoint.category == "not_found"

    missing_model = connectivity_tests.classify_connectivity_response(
        404,
        error_body("model claude-missing was not found", "not_found_error"),
        scope="messages_api",
        credential="secret",
        latency_ms=10,
    )
    assert missing_model.category == "model"

    limited = connectivity_tests.classify_connectivity_response(
        429,
        error_body("slow down", "rate_limit_error"),
        scope="messages_api",
        credential="secret",
        latency_ms=10,
    )
    assert limited.category == "rate_limited"


def test_provider_without_model_reports_limited_scope_success() -> None:
    outcome = connectivity_tests.classify_connectivity_response(
        400,
        error_body("model is required", "invalid_request_error"),
        scope="transport_endpoint_only",
        credential="secret",
        latency_ms=8,
    )

    assert outcome.category == "ok"
    assert "inference and model access were not tested" in outcome.detail


def test_connectivity_timeout_and_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAIRN_CONNECTIVITY_ALLOW_PRIVATE_NETWORKS", "true")
    spec = connectivity_tests.ConnectivityTargetSpec(
        target_type="provider",
        target_id="provider-a",
        provider_id="provider-a",
        base_url="https://api.example.test",
        credential="top-secret-token",
        credential_fingerprint="fingerprint",
        config_revision="revision",
        model_id="model-a",
        upstream_model="claude-test",
        scope="messages_api",
    )

    def timeout_post(*_args, **_kwargs):
        raise requests.exceptions.ReadTimeout("Bearer top-secret-token timed out")

    monkeypatch.setattr(connectivity_tests.requests, "post", timeout_post)
    timeout = connectivity_tests.perform_connectivity_request(spec, timeout_seconds=1)
    assert timeout.category == "timeout"
    assert "top-secret-token" not in timeout.detail

    redacted = connectivity_tests.classify_connectivity_response(
        401,
        error_body("Authorization: Bearer top-secret-token API_KEY=another-secret", "authentication_error"),
        scope="messages_api",
        credential="top-secret-token",
        latency_ms=1,
    )
    assert redacted.category == "auth"
    assert "top-secret-token" not in redacted.detail
    assert "another-secret" not in redacted.detail
    assert len(redacted.detail) <= 320


def test_connectivity_rejects_private_destination_and_redacts_query(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = connectivity_tests.ConnectivityTargetSpec(
        target_type="provider",
        target_id="provider-a",
        provider_id="provider-a",
        base_url="http://127.0.0.1:8000",
        credential="secret",
        credential_fingerprint="fingerprint",
        config_revision="revision",
        model_id=None,
        upstream_model=None,
        scope="transport_endpoint_only",
    )
    monkeypatch.delenv("CAIRN_CONNECTIVITY_ALLOW_PRIVATE_NETWORKS", raising=False)
    outcome = connectivity_tests.perform_connectivity_request(spec, timeout_seconds=1)
    assert outcome.category == "connect"
    redacted = connectivity_tests._sanitize_detail("failed https://example.test/path?token=value&name=test", "")
    assert "token=value" not in redacted and "name=test" not in redacted
    assert "[REDACTED]" in redacted


def test_transport_failures_classify_dns_connect_and_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAIRN_CONNECTIVITY_ALLOW_PRIVATE_NETWORKS", "true")
    spec = connectivity_tests.ConnectivityTargetSpec(
        target_type="provider",
        target_id="provider-a",
        provider_id="provider-a",
        base_url="https://api.example.test",
        credential="secret",
        credential_fingerprint="fingerprint",
        config_revision="revision",
        model_id="model-a",
        upstream_model="claude-test",
        scope="messages_api",
    )

    failures = [
        (requests.exceptions.ConnectionError("Name resolution failed"), "dns"),
        (requests.exceptions.ConnectionError("Connection refused"), "connect"),
        (requests.exceptions.SSLError("certificate verify failed"), "tls"),
    ]
    for error, expected in failures:
        monkeypatch.setattr(
            connectivity_tests.requests,
            "post",
            lambda *_args, _error=error, **_kwargs: (_ for _ in ()).throw(_error),
        )
        outcome = connectivity_tests.perform_connectivity_request(spec, timeout_seconds=1)
        assert outcome.category == expected


def test_async_connectivity_record_is_sanitized_and_becomes_stale(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CAIRN_CONNECTIVITY_ALLOW_PRIVATE_NETWORKS", "true")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    config_path = runtime / "dispatch.yaml"
    config = (ROOT / "deploy/dispatch.yaml.example").read_text(encoding="utf-8")
    config = config.replace(
        "providers: []",
        """providers:
  - id: provider-a
    base_url: https://api.example.test
    provider_api: anthropic-messages
    credential_ref: PROVIDER_A_KEY""",
    ).replace(
        "models: []",
        """models:
  - id: model-a
    provider_id: provider-a
    upstream_model: claude-test""",
    ).replace(
        "common_env: {}",
        """common_env:
  PROVIDER_A_KEY: database-must-not-contain-this-token""",
    )
    config_path.write_text(config, encoding="utf-8")
    store = DispatcherConfigStore(config_path)

    db_path = tmp_path / "connectivity.db"
    with sqlite3.connect(db_path) as setup:
        setup.row_factory = sqlite3.Row
        setup.executescript(db.SCHEMA)

    @contextmanager
    def file_conn():
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    response = FakeResponse(200, {"type": "message", "content": [{"type": "text", "text": "pong"}]})
    monkeypatch.setattr(connectivity_tests.requests, "post", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(connectivity_tests.db, "get_conn", file_conn)

    with file_conn() as conn:
        accepted = connectivity_tests.start_connectivity_test(
            conn,
            store,
            "model",
            "model-a",
            timeout_seconds=2,
        )

    deadline = time.monotonic() + 3
    while True:
        with file_conn() as conn:
            result = connectivity_tests.get_connectivity_test(conn, store, accepted.id)
        if result.status == "completed":
            break
        assert time.monotonic() < deadline
        time.sleep(0.01)

    assert result.category == "ok"
    assert result.stale is False
    assert result.scope == "messages_api"
    assert result.credential_fingerprint != "database-must-not-contain-this-token"

    with file_conn() as conn:
        row = conn.execute("SELECT * FROM dispatcher_connectivity_tests WHERE id = ?", (result.id,)).fetchone()
    persisted = " ".join(str(value) for value in row)
    assert "database-must-not-contain-this-token" not in persisted
    assert "authorization" not in persisted.lower()
    assert "pong" not in persisted

    current, _content, revision = store.read()
    current["common_env"]["PROVIDER_A_KEY"] = "rotated-token"
    store.write_config(current, expected_revision=revision)
    with file_conn() as conn:
        rotated = connectivity_tests.get_connectivity_test(conn, store, result.id)
    assert rotated.stale is True


def test_connectivity_result_is_stale_after_config_revision_change(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    config_path = runtime / "dispatch.yaml"
    config_path.write_text(
        (ROOT / "deploy/dispatch.yaml.example").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    store = DispatcherConfigStore(config_path)
    _config, _content, revision = store.read()

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.execute(
        """
        INSERT INTO dispatcher_connectivity_tests
            (id, target_type, target_id, target_key, provider_id, scope, status,
             category, detail, config_revision, credential_fingerprint, created_at)
        VALUES ('ct_stale', 'provider', 'provider-a', 'provider:provider-a', 'provider-a',
                'transport_endpoint_only', 'completed', 'ok', 'done', ?, ?, '2026-08-09T00:00:00Z')
        """,
        (revision, connectivity_tests._credential_fingerprint("token")),
    )

    config = (ROOT / "deploy/dispatch.yaml.example").read_text(encoding="utf-8").replace(
        'dispatcher_id: "cairny-portable"',
        'dispatcher_id: "changed"',
    )
    store.write_content(config, expected_revision=revision)

    result = connectivity_tests.get_connectivity_test(conn, store, "ct_stale")
    assert result.stale is True
    conn.close()


def test_worker_connectivity_target_resolves_provider_model_and_secret(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    config_path = runtime / "dispatch.yaml"
    config_path.write_text(
        """server: http://127.0.0.1:8001
providers:
  - id: provider-a
    base_url: https://api.example.test
    provider_api: anthropic-messages
    credential_ref: PROVIDER_A_KEY
common_env:
  PROVIDER_A_KEY: worker-secret
models:
  - id: model-a
    provider_id: provider-a
    upstream_model: claude-test
workers:
  - id: worker-a
    name: worker-a
    engine: claudecode
    model_id: model-a
    task_types: [bootstrap, reason, explore]
    concurrency: 1
    priority: 0
    enabled: true
    env: {}
""",
        encoding="utf-8",
    )
    store = DispatcherConfigStore(config_path)

    spec = connectivity_tests.resolve_connectivity_target(store, "worker", "worker-a")

    assert spec.target_type == "worker"
    assert spec.target_id == "worker-a"
    assert spec.target_key == "worker:worker-a"
    assert spec.provider_id == "provider-a"
    assert spec.model_id == "model-a"
    assert spec.upstream_model == "claude-test"
    assert spec.credential == "worker-secret"
    assert spec.scope == "messages_api"


def test_connectivity_uses_environment_override_before_common_env(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "dispatch.yaml"
    config_path.write_text(
        """providers:
  - id: provider-a
    base_url: https://api.example.test
    provider_api: anthropic-messages
    credential_ref: PROVIDER_A_KEY
common_env:
  PROVIDER_A_KEY: yaml-secret
models: []
workers: []
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("PROVIDER_A_KEY", "environment-secret")

    spec = connectivity_tests.resolve_connectivity_target(
        DispatcherConfigStore(config_path),
        "provider",
        "provider-a",
    )

    assert spec.credential == "environment-secret"
    assert spec.credential_fingerprint == connectivity_tests._credential_fingerprint("environment-secret")


def test_worker_connectivity_router_starts_diagnostic_and_is_exposed(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = object()
    store = object()
    captured: dict[str, object] = {}

    @contextmanager
    def fake_get_conn():
        yield connection

    def fake_start(conn, actual_store, target_type, target_id, *, timeout_seconds):
        captured.update(
            conn=conn,
            store=actual_store,
            target_type=target_type,
            target_id=target_id,
            timeout_seconds=timeout_seconds,
        )
        return SimpleNamespace(id="ct-worker", status="queued")

    monkeypatch.setattr(infra, "get_conn", fake_get_conn)
    monkeypatch.setattr(infra, "dispatcher_config_store", lambda conn: store)
    monkeypatch.setattr(infra, "start_connectivity_test", fake_start)

    accepted = infra.test_dispatcher_worker(
        "worker-a",
        DispatcherConnectivityTestStart(timeout_seconds=7),
    )

    assert captured == {
        "conn": connection,
        "store": store,
        "target_type": "worker",
        "target_id": "worker-a",
        "timeout_seconds": 7,
    }
    assert accepted.test_id == "ct-worker"
    assert accepted.status == "queued"
    assert accepted.poll_url == "/dispatcher/connectivity-tests/ct-worker"

    app.openapi_schema = None
    assert "/dispatcher/workers/{worker_id}/connectivity-tests" in app.openapi()["paths"]
