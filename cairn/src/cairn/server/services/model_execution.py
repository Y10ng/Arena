"""研究与项目 Agent 共用的租户模型解析，不依赖 Worker 身份。"""

from __future__ import annotations

import json

from fastapi import HTTPException

from cairn.server.schemas.models import TenantModelExecutionBinding
from .model_credentials import (
    CredentialConfigurationError, CredentialDecryptionError, credential_status, decrypt_active_credential,
)
from .model_transport import ModelTransportError, resolve_model_endpoint


INFERENCE_ENDPOINTS = {
    "anthropic-messages": "/v1/messages",
    "openai-chat-completions": "/v1/chat/completions",
    "openai-responses": "/v1/responses",
}


def model_availability(conn, *, tenant_id, model_id):
    row = conn.execute(
        "SELECT * FROM tenant_models WHERE tenant_id = ? AND id = ?", (tenant_id, model_id),
    ).fetchone()
    reason = None
    if model_id is None:
        reason = "model_unconfigured"
    elif row is None or not row["enabled"]:
        reason = "model_disabled"
    elif row["protocol"] not in INFERENCE_ENDPOINTS:
        reason = "model_protocol_unsupported"
    elif not str(row["model_name"] or "").strip():
        reason = "model_incomplete"
    elif not credential_status(conn, tenant_id=tenant_id, model_id=model_id)["credential_configured"]:
        reason = "credential_missing"
    else:
        try:
            resolve_model_endpoint(row["base_url"], row["inference_endpoint"], INFERENCE_ENDPOINTS[row["protocol"]])
        except ModelTransportError:
            reason = "model_endpoint_invalid"
    return {
        "model_id": model_id,
        "model_display_name": row["display_name"] if row else "",
        "model_name": row["model_name"] if row else "",
        "available": reason is None,
        "unavailable_reason": reason,
    }


def resolve_model_execution_binding(conn, *, tenant_id, model_id) -> TenantModelExecutionBinding:
    state = model_availability(conn, tenant_id=tenant_id, model_id=model_id)
    if not state["available"]:
        labels = {
            "model_unconfigured": "尚未配置模型，请选择现有租户模型。",
            "model_disabled": "会话模型已被停用或删除，请检查模型配置。",
            "model_protocol_unsupported": "模型协议不支持执行。",
            "model_incomplete": "模型名称尚未配置。",
            "credential_missing": "模型凭据尚未配置，请在模型管理中保存凭据。",
            "model_endpoint_invalid": "模型推理接口配置无效。",
        }
        raise HTTPException(422, {"code": state["unavailable_reason"], "message": labels[state["unavailable_reason"]]})
    row = conn.execute(
        "SELECT * FROM tenant_models WHERE tenant_id = ? AND id = ?", (tenant_id, model_id),
    ).fetchone()
    try:
        api_key, credential = decrypt_active_credential(conn, tenant_id=tenant_id, model_id=model_id)
    except (CredentialConfigurationError, CredentialDecryptionError) as exc:
        raise HTTPException(422, {
            "code": "credential_unavailable", "message": "模型凭据无法解密，请检查服务器密钥配置或重新保存模型凭据。",
        }) from exc
    return TenantModelExecutionBinding(
        model_id=model_id,
        model_revision=int(row["revision"]),
        credential_version_id=str(credential["credential_version_id"]),
        credential_version=int(credential["version_number"]),
        credential_fingerprint=str(credential["credential_fingerprint"]),
        protocol=row["protocol"],
        model_name=row["model_name"],
        base_url=row["base_url"],
        inference_endpoint=resolve_model_endpoint(row["base_url"], row["inference_endpoint"], INFERENCE_ENDPOINTS[row["protocol"]]),
        auth_header=row["auth_header"],
        custom_headers=json.loads(row["custom_headers_json"] or "[]"),
        api_key=api_key,
    )
