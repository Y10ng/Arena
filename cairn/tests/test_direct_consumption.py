"""受控本地 HTTP 与一次性 PostgreSQL 验证直接推理计量，不访问供应商。"""

from __future__ import annotations

import base64
from datetime import datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
import subprocess
import sys
from threading import Thread

from fastapi import HTTPException
import psycopg
import pytest

from test_postgres_bootstrap import empty_postgres_url, postgres_instance  # noqa: F401
from cairn.server import db
from cairn.server.auth import RequestPrincipal, create_user_with_personal_tenant
from cairn.server.schemas.models import CreateTenantModelRequest, DiscoverTenantModelRequest, TestTenantModelRequest as ModelTestRequest
from cairn.server.services import connectivity_tests, direct_consumption, model_operations, model_transport
from cairn.server.services.tenant_models import create_tenant_model

pytestmark = pytest.mark.skipif(
    os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1",
    reason="requires explicit disposable local PostgreSQL integration opt-in",
)

USAGE = {"input_tokens": 80, "output_tokens": 10, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 0}
SECRET = "fixture-secret-not-for-receipts"


@pytest.fixture
def configured(empty_postgres_url, tmp_path, monkeypatch):
    db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")
    keyring = tmp_path / "keyring.json"
    keyring.write_text(json.dumps({"active_version": "fixture", "keys": {
        "fixture": base64.b64encode(b"k" * 32).decode("ascii"),
    }}))
    keyring.chmod(0o600)
    monkeypatch.setenv("CAIRN_MODEL_CREDENTIAL_KEYRING_FILE", str(keyring))
    user, tenant = create_user_with_personal_tenant("direct-fixture", "controlled password")
    return RequestPrincipal(kind="user", subject_id=user, tenant_id=tenant, role="user")


@pytest.fixture
def http_fixture(monkeypatch):
    state = {"status": 200, "payload": {"type": "message", "content": [], "usage": USAGE}, "calls": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length", "0")))
            self.respond()

        def do_GET(self):
            self.respond()

        def respond(self):
            state["calls"] += 1
            body = json.dumps(state["payload"]).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # 仅本测试固定本地地址，不放宽正式 transport 的目的地校验。
    monkeypatch.setattr(model_transport, "_resolve_safe_addresses", lambda *_args: [ipaddress.ip_address("127.0.0.1")])
    monkeypatch.setenv("CAIRN_CONNECTIVITY_ALLOW_PRIVATE_NETWORKS", "1")
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _body(url, **changes):
    values = dict(display_name="受控模型", model_name="fixture-model", protocol="anthropic-messages",
                  base_url=url, api_key=SECRET, prompt="PRIVATE-PROMPT-NOT-FOR-RECEIPTS")
    values.update(changes)
    return ModelTestRequest(**values)


def _rows():
    with db.get_conn() as conn:
        return conn.execute("SELECT b.*, c.* FROM model_consumption c JOIN model_consumption_bindings b ON b.id=c.binding_id ORDER BY b.created_at,b.id").fetchall()


def test_saved_configuration_price_is_frozen_before_business_validation(configured, http_fixture):
    url, state = http_fixture
    body = _body(url)
    model = create_tenant_model(
        CreateTenantModelRequest(**body.model_dump(exclude={"prompt", "model_id", "timeout_seconds"})),
        principal=configured,
    )
    with db.get_conn() as conn:
        conn.execute("UPDATE tenant_models SET pricing_json=? WHERE id=?", (json.dumps({
            "currency": "USD", "input": "1.25", "output": "5", "cache_read": "0.25", "cache_write": "0",
        }), model.id))
    body = _body(url, model_id=model.id, api_key=None, pricing={"currency": "EUR", "input": "999"})
    state["payload"] = {"invalid_business_envelope": True, "usage": USAGE, "echo": SECRET}
    with pytest.raises(HTTPException):
        with db.get_conn() as outer:
            outer.execute("SELECT 1")
            model_operations.test_model_draft(body, principal=configured)
    assert direct_consumption.replay_direct_consumption() == 1
    row = _rows()[0]
    assert row["source"] == "model_test"
    assert row["amount"] == Decimal("0.000155") and row["currency"] == "USD"
    assert row["input_tokens"] == 80 and row["cache_read_tokens"] == 20 and not row["unpriced"]
    assert row["tenant_id"] == configured.tenant_id and row["owner_id"] == configured.subject_id
    assert SECRET not in row["model_json"] and body.prompt not in row["model_json"]

    state["payload"] = {"type": "message", "content": [], "usage": USAGE}
    for changes in ({"model_name": "different"}, {"api_key": "different-credential"},
                    {"inference_endpoint": "/different"}, {"custom_headers": [{"name": "x-channel", "value": "other"}]},
                    {"auth_header": "x-api-key"}):
        model_operations.test_model_draft(_body(url, model_id=model.id, **changes), principal=configured)
    assert direct_consumption.replay_direct_consumption() == 5
    drafts = [row for row in _rows() if row["source"] == "model_test_draft"]
    assert len(drafts) == 5
    assert all(row["amount"] is None and row["unpriced"] for row in drafts)
    assert all("model_id" not in json.loads(row["model_json"]) for row in drafts)
    with db.get_conn() as conn:
        conn.execute("UPDATE tenant_models SET pricing_json='{}', revision=revision+1 WHERE id=?", (model.id,))
        conn.execute("DELETE FROM tenant_models WHERE id=?", (model.id,))
    assert len(_rows()) == 6 and _rows()[0]["amount"] == Decimal("0.000155")


def test_failed_http_usage_is_durable_and_replayed_exactly_without_http(configured, http_fixture, monkeypatch):
    url, state = http_fixture
    state.update(status=500, payload={"error": {"message": "business failed"}, "usage": USAGE, "secret_echo": "PRIVATE-RESPONSE-BODY"})
    original_submit = direct_consumption._submit_receipt

    def unavailable(_receipt):
        raise psycopg.OperationalError("controlled database outage")

    monkeypatch.setattr(direct_consumption, "_submit_receipt", unavailable)
    result = model_operations.test_model_draft(_body(url), principal=configured)
    assert not result.success and state["calls"] == 1 and not _rows()
    assert direct_consumption.replay_direct_consumption() == 0
    paths = list((db.artifact_root() / "model-consumption").glob("direct_*/receipt.json"))
    assert len(paths) == 1
    raw = paths[0].read_text()
    assert SECRET not in raw and "PRIVATE-PROMPT" not in raw and "secret_echo" not in raw
    receipt = json.loads(raw)
    assert receipt["event"]["usage"] == dict(input_tokens=80, output_tokens=10, cache_read_tokens=20, cache_write_tokens=0)
    monkeypatch.setattr(direct_consumption, "_submit_receipt", original_submit)
    # 模拟 DB 已提交但本地确认尚未删除：重放只能确认原响应，不增加消费。
    original_submit(receipt)
    assert direct_consumption.replay_direct_consumption() == 1
    assert direct_consumption.replay_direct_consumption() == 0
    rows = _rows()
    assert len(rows) == 1 and rows[0]["output_tokens"] == 10 and state["calls"] == 1
    assert rows[0]["occurred_at"] == datetime.fromisoformat(receipt["event"]["occurred_at"])
    assert not paths[0].exists()


def test_platform_inference_unknown_failure_and_non_inference_exclusion(configured, http_fixture, monkeypatch):
    url, state = http_fixture
    spec = connectivity_tests.ConnectivityTargetSpec(
        target_type="model", target_id="platform-model", provider_id="platform-provider", base_url=url,
        credential=SECRET, credential_fingerprint="fixture", config_revision="fixture-revision",
        model_id="platform-model", upstream_model="fixture-model", scope="messages_api",
    )
    state.update(status=500, payload={"error": {"message": "failed"}, "usage": USAGE})
    outcome = connectivity_tests.perform_connectivity_request(spec, timeout_seconds=2)
    assert outcome.category == "protocol"
    assert direct_consumption.replay_direct_consumption() == 1
    row = _rows()[0]
    assert row["tenant_id"] is None and row["source"] == "connectivity_test"
    assert row["input_tokens"] == 80 and row["amount"] is None

    spec.upstream_model = None
    spec.scope = "transport_endpoint_only"
    state.update(status=400, payload={"type": "error", "error": {"message": "model required"}})
    assert connectivity_tests.perform_connectivity_request(spec, timeout_seconds=2).category == "ok"
    state.update(status=200, payload={"data": [{"id": "fixture-model"}]})
    model_operations.discover_model_draft(DiscoverTenantModelRequest(**_body(url).model_dump(exclude={"prompt"})), principal=configured)
    assert len(_rows()) == 1

    def timeout(*_args, **_kwargs):
        raise model_transport.ModelTransportError("timeout", "controlled timeout")

    monkeypatch.setattr(model_operations, "request_json", timeout)
    with pytest.raises(model_transport.ModelTransportError):
        model_operations.test_model_draft(_body(url), principal=configured)
    assert direct_consumption.replay_direct_consumption() == 1
    unknown = _rows()[-1]
    assert unknown["measurement"] == "interrupted" and unknown["missing_responses"] == 1
    assert all(unknown[field] is None for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))


def test_active_receipt_is_not_replayed_and_started_receipt_survives(configured):
    submit = direct_consumption._submit_receipt
    started = None
    with direct_consumption.record_direct_consumption(
        tenant_id=configured.tenant_id, owner_id=configured.subject_id, source="model_test_draft",
        model={"protocol": "anthropic-messages", "model_name": "fixture-model", "pricing": {}},
    ) as recording:
        started = json.loads((recording.directory / "receipt.json").read_text())
        assert started["event"]["measurement"] == "started"
        assert direct_consumption.replay_direct_consumption() == 0 and not _rows()
        recording.observe_response("anthropic-messages", json.dumps({"usage": USAGE}).encode())
    assert direct_consumption.replay_direct_consumption() == 1
    assert len(_rows()) == 1
    # 崩溃后的开始回执只能补未知，不倒退已有同一响应的已知量。
    submit(started)
    assert len(_rows()) == 1 and _rows()[0]["input_tokens"] == 80
    subprocess.run([sys.executable, "-c", """
import os, sys
from pathlib import Path
from cairn.server import db
from cairn.server.services.direct_consumption import record_direct_consumption
db.artifact_root = lambda: Path(sys.argv[1])
with record_direct_consumption(tenant_id=sys.argv[2], owner_id=sys.argv[3],
        source='model_test_draft', model={'model_name':'interrupted-fixture','pricing':{}}):
    os._exit(0)
""", str(db.artifact_root()), configured.tenant_id, configured.subject_id], check=True)
    assert direct_consumption.replay_direct_consumption() == 1
    interrupted = _rows()[-1]
    assert interrupted["measurement"] == "started" and interrupted["missing_responses"] == 1
    assert interrupted["input_tokens"] is None and interrupted["amount"] is None
