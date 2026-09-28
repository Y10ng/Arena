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
from cairn.server.services import model_operations
from cairn.server.services.model_transport import ModelTransportResponse


PRINCIPAL = RequestPrincipal(
    kind="user",
    subject_id="usr_test",
    tenant_id="tenant_test",
    role="user",
)


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
