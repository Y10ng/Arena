"""对当前 Tenant Model 草稿执行同步 Models 发现与真实请求测试。"""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import HTTPException

from cairn.server.auth import RequestPrincipal
from cairn.server.db import get_conn
from cairn.server.schemas.models import (
    DiscoverTenantModelRequest,
    ModelDiscoveryResult,
    ModelTestResult,
    TenantModelConfig,
    TestTenantModelRequest,
)

from .model_credentials import decrypt_active_credential, validate_plaintext_credential
from .model_transport import request_json, resolve_model_endpoint
from .tenant_models import get_tenant_model_row, normalize_model_base_url

_MAX_HEADERS = 32
_MAX_HEADER_BYTES = 16 * 1024
_MAX_MODELS = 200
_MAX_MODEL_NAME_LENGTH = 256
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_RESERVED_HEADERS = {
    "authorization",
    "x-api-key",
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
}
_PROTOCOL_DEFAULTS = {
    "anthropic-messages": ("/v1/models", "/v1/messages"),
    "openai-chat-completions": ("/v1/models", "/v1/chat/completions"),
    "openai-responses": ("/v1/models", "/v1/responses"),
}


def validate_custom_headers(config: TenantModelConfig) -> None:
    """只验证 HTTP 边界，不改写用户保存的 Header 原值。"""
    if len(config.custom_headers) > _MAX_HEADERS:
        raise HTTPException(422, f"Custom Headers cannot exceed {_MAX_HEADERS} entries")
    seen: set[str] = set()
    total_bytes = 0
    for index, header in enumerate(config.custom_headers, start=1):
        if not _HEADER_NAME.fullmatch(header.name):
            raise HTTPException(422, f"Custom Header {index} name is invalid")
        folded = header.name.casefold()
        if folded in seen:
            raise HTTPException(422, f"Custom Header {index} duplicates an earlier name")
        if folded in _RESERVED_HEADERS:
            raise HTTPException(422, f"Custom Header {header.name} is reserved")
        if any(ord(character) < 32 or ord(character) == 127 for character in header.value):
            raise HTTPException(422, f"Custom Header {header.name} value contains control characters")
        try:
            header.value.encode("latin-1")
        except UnicodeEncodeError as exc:
            raise HTTPException(
                422,
                f"Custom Header {header.name} value cannot be represented as an HTTP field value",
            ) from exc
        seen.add(folded)
        total_bytes += len(header.name.encode("utf-8")) + len(header.value.encode("utf-8"))
    if total_bytes > _MAX_HEADER_BYTES:
        raise HTTPException(422, "Custom Headers exceed the total size limit")


def validate_model_endpoints(config: TenantModelConfig, *, base_url: str | None = None) -> None:
    normalized_base = base_url or normalize_model_base_url(config.base_url)
    models_default, inference_default = _PROTOCOL_DEFAULTS[config.protocol]
    resolve_model_endpoint(normalized_base, config.models_endpoint, models_default)
    resolve_model_endpoint(normalized_base, config.inference_endpoint, inference_default)


def discover_model_draft(
    body: DiscoverTenantModelRequest,
    *,
    principal: RequestPrincipal,
) -> ModelDiscoveryResult:
    tenant_id = _principal_tenant(principal)
    credential = _resolve_credential(body, tenant_id=tenant_id)
    base_url = normalize_model_base_url(body.base_url)
    validate_custom_headers(body)
    models_default, _ = _PROTOCOL_DEFAULTS[body.protocol]
    endpoint = resolve_model_endpoint(base_url, body.models_endpoint, models_default)
    response = request_json(
        "GET",
        endpoint,
        headers=_request_headers(body, credential),
        payload=None,
        timeout_seconds=body.timeout_seconds,
    )
    text = _response_text(response.body, credential)
    if not 200 <= response.status_code < 300:
        raise HTTPException(
            502,
            {
                "message": "Models API returned a non-success status",
                "upstream_status": response.status_code,
                "response_body": text,
            },
        )
    payload = _json_object(text, "Models API returned invalid UTF-8 JSON")
    raw_items = payload.get("data")
    if not isinstance(raw_items, list):
        raise HTTPException(502, "Models API returned an invalid data[].id envelope")
    if len(raw_items) > _MAX_MODELS:
        raise HTTPException(502, "Models API returned too many model entries")
    names: list[str] = []
    for item in raw_items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise HTTPException(502, "Models API returned an invalid data[].id envelope")
        if len(item["id"]) > _MAX_MODEL_NAME_LENGTH:
            raise HTTPException(502, "Models API returned a model ID above the size limit")
        names.append(item["id"])
    return ModelDiscoveryResult(
        model_names=names,
        http_status=response.status_code,
        latency_ms=response.latency_ms,
        endpoint=response.url,
    )


def test_model_draft(
    body: TestTenantModelRequest,
    *,
    principal: RequestPrincipal,
) -> ModelTestResult:
    if not body.model_name:
        raise HTTPException(422, "Model name is required for testing")
    tenant_id = _principal_tenant(principal)
    credential = _resolve_credential(body, tenant_id=tenant_id)
    base_url = normalize_model_base_url(body.base_url)
    validate_custom_headers(body)
    _, inference_default = _PROTOCOL_DEFAULTS[body.protocol]
    endpoint = resolve_model_endpoint(base_url, body.inference_endpoint, inference_default)
    response = request_json(
        "POST",
        endpoint,
        headers=_request_headers(body, credential),
        payload=_request_payload(body),
        timeout_seconds=body.timeout_seconds,
    )
    text = _response_text(response.body, credential)
    success = 200 <= response.status_code < 300
    if success:
        _validate_success_envelope(body.protocol, text)
    return ModelTestResult(
        success=success,
        http_status=response.status_code,
        latency_ms=response.latency_ms,
        endpoint=response.url,
        response_body=text,
    )


def _resolve_credential(body: TenantModelConfig, *, tenant_id: str) -> str:
    supplied = getattr(body, "api_key", None)
    if supplied is not None:
        return validate_plaintext_credential(supplied)
    model_id = getattr(body, "model_id", None)
    if not model_id:
        raise HTTPException(422, "API Key is required for an unsaved Model")
    with get_conn() as conn:
        get_tenant_model_row(conn, model_id, tenant_id=tenant_id)
        plaintext, _ = decrypt_active_credential(conn, tenant_id=tenant_id, model_id=model_id)
        return plaintext


def _request_headers(config: TenantModelConfig, credential: str) -> dict[str, str]:
    headers = {"accept": "application/json", "user-agent": "Arena-model-test/1"}
    if config.protocol == "anthropic-messages":
        headers["anthropic-version"] = "2023-06-01"
    for custom in config.custom_headers:
        _set_header(headers, custom.name, custom.value)
    if config.auth_header == "authorization-bearer":
        _set_header(headers, "Authorization", f"Bearer {credential}")
    else:
        _set_header(headers, "x-api-key", credential)
    return headers


def _set_header(headers: dict[str, str], name: str, value: str) -> None:
    folded = name.casefold()
    for existing in tuple(headers):
        if existing.casefold() == folded:
            del headers[existing]
    headers[name] = value


def _request_payload(body: TestTenantModelRequest) -> dict[str, Any]:
    if body.protocol == "anthropic-messages":
        return {
            "model": body.model_name,
            "max_tokens": 256,
            "messages": [{"role": "user", "content": body.prompt}],
        }
    if body.protocol == "openai-chat-completions":
        return {
            "model": body.model_name,
            "max_tokens": 256,
            "messages": [{"role": "user", "content": body.prompt}],
        }
    return {
        "model": body.model_name,
        "input": body.prompt,
        "max_output_tokens": 256,
    }


def _response_text(body: bytes, credential: str) -> str:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(502, "Model API returned a non-UTF-8 response") from exc
    if credential in text:
        raise HTTPException(502, "Model API response contained the current API Key and was blocked")
    return text


def _json_object(text: str, error: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(502, error) from exc
    if not isinstance(payload, dict):
        raise HTTPException(502, error)
    return payload


def _validate_success_envelope(protocol: str, text: str) -> None:
    payload = _json_object(text, "Model API returned an invalid success envelope")
    if protocol == "anthropic-messages":
        valid = payload.get("type") == "message" and isinstance(payload.get("content"), list)
    elif protocol == "openai-chat-completions":
        valid = isinstance(payload.get("choices"), list)
    else:
        valid = isinstance(payload.get("output"), list)
    if not valid:
        raise HTTPException(502, "Model API returned an invalid success envelope")


def _principal_tenant(principal: RequestPrincipal) -> str:
    if principal.kind != "user" or not principal.tenant_id:
        raise HTTPException(403, "User tenant is required")
    return principal.tenant_id
