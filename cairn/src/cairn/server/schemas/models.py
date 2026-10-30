"""Tenant Model 配置、write-only credential 与草稿测试 schema。"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ModelProtocol = Literal[
    "anthropic-messages",
    "openai-chat-completions",
    "openai-responses",
]
ModelAuthHeader = Literal["authorization-bearer", "x-api-key"]


class CustomModelHeader(BaseModel):
    name: str
    value: str


class TenantModelExecutionBinding(BaseModel):
    model_id: str
    model_revision: int
    credential_version_id: str
    credential_version: int
    credential_fingerprint: str
    engine: Literal["pi"] = "pi"
    protocol: ModelProtocol
    model_name: str
    base_url: str
    inference_endpoint: str
    auth_header: ModelAuthHeader
    custom_headers: list[CustomModelHeader] = Field(default_factory=list)
    api_key: str = Field(repr=False)

    def execution_provenance(self) -> dict:
        """执行审计只保存凭据版本与摘要，不保存可恢复的认证信息。"""
        headers = json.dumps(
            [header.model_dump() for header in self.custom_headers],
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        return {
            "source": "tenant_model_binding",
            "model_id": self.model_id,
            "model_name": self.model_name,
            "model_revision": self.model_revision,
            "credential_version_id": self.credential_version_id,
            "credential_version": self.credential_version,
            "credential_fingerprint": self.credential_fingerprint,
            "engine": self.engine,
            "protocol": self.protocol,
            "auth_header": self.auth_header,
            "inference_endpoint": self.inference_endpoint,
            "custom_headers_sha256": sha256(headers.encode("utf-8")).hexdigest(),
        }


class TenantModelConfig(BaseModel):
    display_name: str
    protocol: ModelProtocol
    base_url: str
    auth_header: ModelAuthHeader = "authorization-bearer"
    custom_headers: list[CustomModelHeader] = Field(default_factory=list)
    models_endpoint: str | None = None
    inference_endpoint: str | None = None
    model_name: str = ""
    enabled: bool = True

    @field_validator("display_name", "base_url", "model_name")
    @classmethod
    def normalize_text(cls, value: str, info) -> str:
        text = value.strip()
        if info.field_name in {"display_name", "base_url"} and not text:
            raise ValueError("must not be empty")
        limits = {"display_name": 128, "base_url": 2048, "model_name": 256}
        limit = limits[info.field_name]
        if len(text) > limit:
            raise ValueError(f"must be {limit} characters or fewer")
        return text

    @field_validator("models_endpoint", "inference_endpoint")
    @classmethod
    def validate_endpoint_length(cls, value: str | None) -> str | None:
        if value is not None and len(value) > 2048:
            raise ValueError("must be 2048 characters or fewer")
        return value


class TenantModel(TenantModelConfig):
    id: str
    revision: int
    credential_configured: bool = False
    credential_version: int | None = None
    credential_key_version: str | None = None
    created_at: str
    updated_at: str


class CreateTenantModelRequest(TenantModelConfig):
    # secret 的内容校验在 service 边界完成，避免验证错误回显 write-only 值。
    api_key: str | None = Field(default=None, repr=False)


class UpdateTenantModelRequest(TenantModelConfig):
    expected_revision: int = Field(gt=0)
    # 不传表示保留当前 credential，传入时与配置在同一 transaction 中轮换。
    api_key: str | None = Field(default=None, repr=False)


class DraftTenantModelRequest(TenantModelConfig):
    model_id: str | None = None
    api_key: str | None = Field(default=None, repr=False)
    timeout_seconds: int = Field(default=15, ge=2, le=60)


class DiscoverTenantModelRequest(DraftTenantModelRequest):
    pass


class TestTenantModelRequest(DraftTenantModelRequest):
    prompt: str = Field(default="hello", min_length=1, max_length=16_384)

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("must not contain NUL")
        return value


class ModelDiscoveryResult(BaseModel):
    model_names: list[str]
    http_status: int
    latency_ms: int
    endpoint: str


class ModelTestResult(BaseModel):
    success: bool
    http_status: int
    latency_ms: int
    endpoint: str
    response_body: str


class DeleteTenantModelRequest(BaseModel):
    expected_revision: int = Field(gt=0)


class DeleteModelCredentialRequest(BaseModel):
    expected_revision: int = Field(gt=0)


__all__ = [name for name in globals() if not name.startswith("__")]
