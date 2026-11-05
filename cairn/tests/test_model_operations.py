"""Tenant Model 草稿发现与三种协议真实请求测试。"""

from __future__ import annotations

import json

from fastapi import HTTPException
import pytest

from cairn.server.auth import RequestPrincipal
from cairn.server.schemas.models import (
    DiscoverTenantModelRequest,
    TestTenantModelRequest as ModelTestRequest,
)
from cairn.server.services import direct_consumption, model_operations
from cairn.server.services.model_transport import ModelTransportResponse


PRINCIPAL = RequestPrincipal(
    kind="user",
    subject_id="usr_test",
    tenant_id="tenant_test",
    role="user",
)


@pytest.fixture(autouse=True)
def isolated_receipt_submission(monkeypatch: pytest.MonkeyPatch, tmp_path):
    # 本文件只验证 HTTP 合同；实际回执写入由隔离 PostgreSQL 用例覆盖。
    monkeypatch.setattr(direct_consumption, "_spool_root", lambda: tmp_path)
    monkeypatch.setattr(direct_consumption, "_submit_receipt", lambda _receipt: None)
    monkeypatch.setattr(direct_consumption, "_memory_receipts", {})


def _config(**overrides):
    values = {
        "display_name": "Draft Model",
        "protocol": "anthropic-messages",
        "base_url": "https://api.example.test/root/v1",
        "auth_header": "authorization-bearer",
        "custom_headers": [],
        "models_endpoint": None,
        "inference_endpoint": None,
        "model_name": "provider-model",
        "enabled": True,
        "api_key": "known-api-key-123",
        "timeout_seconds": 15,
    }
    values.update(overrides)
    return values


def _response(body: object | str, *, status: int = 200, url: str = "https://api.example.test/final"):
    raw = body.encode("utf-8") if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode("utf-8")
    return ModelTransportResponse(status_code=status, body=raw, latency_ms=17, url=url)


@pytest.mark.parametrize(
    ("protocol", "endpoint", "envelope", "expected_payload"),
    [
        (
            "anthropic-messages",
            "https://api.example.test/root/v1/messages",
            {"type": "message", "content": []},
            {"model": "provider-model", "max_tokens": 256, "messages": [{"role": "user", "content": "hello原文"}]},
        ),
        (
            "openai-chat-completions",
            "https://api.example.test/root/v1/chat/completions",
            {"choices": []},
            {"model": "provider-model", "max_tokens": 256, "messages": [{"role": "user", "content": "hello原文"}]},
        ),
        (
            "openai-responses",
            "https://api.example.test/root/v1/responses",
            {"output": []},
            {"model": "provider-model", "input": "hello原文", "max_output_tokens": 256},
        ),
    ],
)
def test_exact_protocol_endpoint_payload_and_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    endpoint: str,
    envelope: dict,
    expected_payload: dict,
) -> None:
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _response(envelope, url=url)

    monkeypatch.setattr(model_operations, "request_json", request)
    result = model_operations.test_model_draft(
        ModelTestRequest(**_config(protocol=protocol, prompt="hello原文")),
        principal=PRINCIPAL,
    )

    assert result.success is True
    assert result.response_body == json.dumps(envelope, ensure_ascii=False)
    assert len(calls) == 1
    assert calls[0][0:2] == ("POST", endpoint)
    assert calls[0][2]["payload"] == expected_payload
    assert calls[0][2]["headers"]["Authorization"] == "Bearer known-api-key-123"


def test_header_precedence_preserves_custom_values_and_auth_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    def request(method, url, **kwargs):
        seen.update(kwargs["headers"])
        return _response({"type": "message", "content": []}, url=url)

    monkeypatch.setattr(model_operations, "request_json", request)
    model_operations.test_model_draft(
        ModelTestRequest(**_config(
            auth_header="x-api-key",
            custom_headers=[
                {"name": "anthropic-version", "value": "2025-01-01"},
                {"name": "X-Gateway", "value": "café value"},
            ],
        )),
        principal=PRINCIPAL,
    )
    assert seen["anthropic-version"] == "2025-01-01"
    assert seen["X-Gateway"] == "café value"
    assert seen["x-api-key"] == "known-api-key-123"
    assert "Authorization" not in seen


def test_discovery_preserves_provider_order_duplicates_and_original_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        model_operations,
        "request_json",
        lambda method, url, **kwargs: _response(
            {"data": [{"id": "模型-乙"}, {"id": "model-a"}, {"id": "model-a"}]},
            url=url,
        ),
    )
    result = model_operations.discover_model_draft(
        DiscoverTenantModelRequest(**_config(models_endpoint="catalog?scope=all")),
        principal=PRINCIPAL,
    )
    assert result.model_names == ["模型-乙", "model-a", "model-a"]
    assert result.endpoint == "https://api.example.test/root/v1/catalog?scope=all"


def test_absolute_endpoint_override_is_used_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []

    def request(method, url, **kwargs):
        seen.append(url)
        return _response({"output": []}, url=url)

    monkeypatch.setattr(model_operations, "request_json", request)
    model_operations.test_model_draft(
        ModelTestRequest(**_config(
            protocol="openai-responses",
            inference_endpoint="https://gateway.example.test/invoke?mode=raw",
        )),
        principal=PRINCIPAL,
    )
    assert seen == ["https://gateway.example.test/invoke?mode=raw"]


def test_consumption_spool_failure_keeps_http_result_and_writes_real_usage(monkeypatch, caplog):
    captured = []
    def unavailable():
        raise OSError('fixture spool unavailable')
    monkeypatch.setattr(direct_consumption, '_spool_root', unavailable)
    monkeypatch.setattr(direct_consumption, '_submit_receipt', lambda receipt: captured.append(receipt.copy()))
    monkeypatch.setattr(model_operations, 'request_json', lambda *args, **kwargs: _response({
        'type': 'message', 'content': [], 'usage': {'input_tokens': 8, 'output_tokens': 3},
    }))
    result = model_operations.test_model_draft(ModelTestRequest(**_config()), principal=PRINCIPAL)
    assert result.success and not captured
    assert direct_consumption.replay_direct_consumption() == 1
    assert len(captured) == 1
    assert captured[0]['event']['usage']['input_tokens'] == 8
    assert captured[0]['event']['usage']['output_tokens'] == 3
    assert 'spool unavailable' in caplog.text
    assert 'known-api-key-123' not in caplog.text


def test_connectivity_inference_survives_spool_failure(monkeypatch):
    from types import SimpleNamespace
    from cairn.server.services import connectivity_tests
    captured = []
    def unavailable():
        raise OSError('fixture spool unavailable')
    monkeypatch.setattr(direct_consumption, '_spool_root', unavailable)
    monkeypatch.setattr(direct_consumption, '_submit_receipt', lambda receipt: captured.append(receipt.copy()))
    monkeypatch.setattr(connectivity_tests, '_validate_connectivity_destination', lambda url: None)
    response = SimpleNamespace(status_code=200, close=lambda: None, iter_content=lambda **kwargs: iter([
        json.dumps({'type': 'message', 'content': [], 'usage': {'input_tokens': 4}}).encode(),
    ]))
    monkeypatch.setattr(connectivity_tests.requests, 'post', lambda *args, **kwargs: response)
    spec = connectivity_tests.ConnectivityTargetSpec(
        target_type='model', target_id='fixture', provider_id='fixture', base_url='https://fixture.test',
        credential='fixture-secret', credential_fingerprint='fixture', config_revision='fixture',
        model_id='fixture', upstream_model='fixture-model', scope='messages_api',
    )
    outcome = connectivity_tests.perform_connectivity_request(spec, timeout_seconds=2)
    assert outcome.category == 'ok' and not captured
    assert direct_consumption.replay_direct_consumption() == 1
    assert len(captured) == 1 and captured[0]['event']['usage']['input_tokens'] == 4
    assert captured[0]['binding']['tenant_id'] is None


def test_direct_consumption_failure_never_masks_model_error(monkeypatch, caplog):
    from cairn.server.services.model_transport import ModelTransportError
    def unavailable(*args, **kwargs):
        raise OSError('fixture disk outage')
    def database_unavailable(*args, **kwargs):
        raise RuntimeError('fixture database outage')
    def model_failure(*args, **kwargs):
        raise ModelTransportError('timeout', 'controlled timeout')
    monkeypatch.setattr(direct_consumption, '_spool_root', unavailable)
    monkeypatch.setattr(direct_consumption, '_submit_receipt', database_unavailable)
    monkeypatch.setattr(model_operations, 'request_json', model_failure)
    with pytest.raises(ModelTransportError, match='controlled timeout'):
        model_operations.test_model_draft(ModelTestRequest(**_config()), principal=PRINCIPAL)
    assert 'receipt only in memory' in caplog.text
    assert direct_consumption.replay_direct_consumption() == 0
    assert len(direct_consumption._memory_receipts) == 1


def test_direct_response_spool_failure_preserves_unknown_start_for_replay(monkeypatch, tmp_path, caplog):
    written = direct_consumption._write_receipt
    def fail_response(directory, receipt):
        if receipt['event']['revision'] == 1:
            raise OSError('fixture response write failed')
        written(directory, receipt)
    def database_unavailable(*args, **kwargs):
        raise RuntimeError('fixture database outage')
    monkeypatch.setattr(direct_consumption, '_write_receipt', fail_response)
    monkeypatch.setattr(direct_consumption, '_submit_receipt', database_unavailable)
    monkeypatch.setattr(model_operations, 'request_json', lambda *args, **kwargs: _response({
        'type': 'message', 'content': [], 'usage': {'input_tokens': 8},
    }))
    result = model_operations.test_model_draft(ModelTestRequest(**_config()), principal=PRINCIPAL)
    assert result.success
    receipt_path, = tmp_path.glob('direct_*/receipt.json')
    receipt = json.loads(receipt_path.read_text())
    assert receipt['event']['measurement'] == 'started'
    assert receipt['event']['missing_responses'] == 1
    assert all(value is None for value in receipt['event']['usage'].values())
    assert 'receipt only in memory' in caplog.text
    assert direct_consumption.replay_direct_consumption() == 0
    submitted = []
    monkeypatch.setattr(direct_consumption, '_submit_receipt', lambda body: submitted.append(body))
    assert direct_consumption.replay_direct_consumption() == 2
    assert submitted[0]['event']['usage']['input_tokens'] == 8
    assert submitted[0]['binding'] == receipt['binding'] and submitted[1] == receipt


def test_durable_direct_receipt_defers_database_until_replay(monkeypatch, tmp_path):
    def forbidden(_receipt):
        raise AssertionError('database must not be called from business request')
    monkeypatch.setattr(direct_consumption, '_submit_receipt', forbidden)
    monkeypatch.setattr(model_operations, 'request_json', lambda *args, **kwargs: _response({
        'type': 'message', 'content': [], 'usage': {'input_tokens': 8},
    }))
    result = model_operations.test_model_draft(ModelTestRequest(**_config()), principal=PRINCIPAL)
    assert result.success
    receipt_path, = tmp_path.glob('direct_*/receipt.json')
    assert json.loads(receipt_path.read_text())['event']['usage']['input_tokens'] == 8


def test_direct_spool_close_failure_does_not_hide_result(monkeypatch, caplog):
    import os
    original_open, original_close = os.open, os.close
    lock_descriptor = []
    def tracked_open(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags, *args, **kwargs)
        if not lock_descriptor and str(path).split('/')[-1].startswith('direct_') and flags & os.O_DIRECTORY:
            lock_descriptor.append(descriptor)
        return descriptor
    def failed_close(descriptor):
        original_close(descriptor)
        if lock_descriptor and descriptor == lock_descriptor[0]:
            raise OSError('fixture close failed')
    monkeypatch.setattr(direct_consumption.os, 'open', tracked_open)
    monkeypatch.setattr(direct_consumption.os, 'close', failed_close)
    monkeypatch.setattr(model_operations, 'request_json', lambda *args, **kwargs: _response({
        'type': 'message', 'content': [], 'usage': {'input_tokens': 8},
    }))
    assert model_operations.test_model_draft(ModelTestRequest(**_config()), principal=PRINCIPAL).success
    assert 'spool close failed' in caplog.text


def test_blocked_replay_does_not_block_business_or_evict_older_memory_receipts(monkeypatch, caplog):
    from threading import Event, Thread
    entered, release = Event(), Event()
    submitted = []
    def unavailable():
        raise OSError('fixture spool unavailable')
    def blocked(receipt):
        entered.set()
        assert release.wait(timeout=10)
        submitted.append(receipt)
    monkeypatch.setattr(direct_consumption, '_spool_root', unavailable)
    monkeypatch.setattr(direct_consumption, '_submit_receipt', blocked)
    monkeypatch.setattr(direct_consumption, '_MEMORY_LIMIT', 2)
    monkeypatch.setattr(model_operations, 'request_json', lambda *args, **kwargs: _response({
        'type': 'message', 'content': [], 'usage': {'input_tokens': 8},
    }))
    assert model_operations.test_model_draft(
        ModelTestRequest(**_config(prompt='fixture-private-prompt')), principal=PRINCIPAL).success
    first_id, = direct_consumption._memory_receipts
    worker = Thread(target=direct_consumption.replay_direct_consumption)
    worker.start()
    try:
        assert entered.wait(timeout=5)
        assert model_operations.test_model_draft(ModelTestRequest(**_config()), principal=PRINCIPAL).success
        original_ids = set(direct_consumption._memory_receipts)
        assert first_id in original_ids and len(original_ids) == 2
        assert model_operations.test_model_draft(ModelTestRequest(**_config()), principal=PRINCIPAL).success
        assert set(direct_consumption._memory_receipts) == original_ids
        assert 'gap not fully retained' in caplog.text
        memory = json.dumps(direct_consumption._memory_receipts)
        assert 'known-api-key-123' not in memory and 'fixture-private-prompt' not in memory
    finally:
        release.set()
        worker.join(timeout=10)
    assert not worker.is_alive() and submitted[0]['binding']['id'] == first_id
    assert first_id not in direct_consumption._memory_receipts


def test_non_success_test_returns_complete_raw_body(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = '{"error":{"message":"原始错误  保留空格"}}\n'
    monkeypatch.setattr(
        model_operations,
        "request_json",
        lambda method, url, **kwargs: _response(raw, status=429, url=url),
    )
    result = model_operations.test_model_draft(
        ModelTestRequest(**_config(protocol="openai-chat-completions")),
        principal=PRINCIPAL,
    )
    assert result.success is False
    assert result.http_status == 429
    assert result.response_body == raw


def test_response_containing_current_key_is_blocked_without_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "credential-exposure-123"
    monkeypatch.setattr(
        model_operations,
        "request_json",
        lambda method, url, **kwargs: _response(f'{{"echo":"{secret}"}}', url=url),
    )
    with pytest.raises(HTTPException) as exc:
        model_operations.test_model_draft(
            ModelTestRequest(**_config(api_key=secret)),
            principal=PRINCIPAL,
        )
    assert exc.value.status_code == 502
    assert secret not in str(exc.value.detail)
    assert "blocked" in str(exc.value.detail)


def test_invalid_success_envelope_and_discovery_limits_fail_whole_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        model_operations,
        "request_json",
        lambda method, url, **kwargs: _response({"unexpected": []}, url=url),
    )
    with pytest.raises(HTTPException) as invalid:
        model_operations.test_model_draft(
            ModelTestRequest(**_config(protocol="openai-responses")),
            principal=PRINCIPAL,
        )
    assert invalid.value.status_code == 502

    monkeypatch.setattr(
        model_operations,
        "request_json",
        lambda method, url, **kwargs: _response(
            {"data": [{"id": f"model-{index}"} for index in range(201)]},
            url=url,
        ),
    )
    with pytest.raises(HTTPException) as too_many:
        model_operations.discover_model_draft(
            DiscoverTenantModelRequest(**_config()),
            principal=PRINCIPAL,
        )
    assert too_many.value.status_code == 502
