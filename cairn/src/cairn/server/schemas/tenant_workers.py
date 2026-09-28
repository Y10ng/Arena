"""Tenant Worker 配置、可用性与 optimistic revision schema。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from cairn.server.schemas.models import CustomModelHeader, ModelAuthHeader, ModelProtocol

TenantWorkerTaskType = Literal["reason", "bootstrap", "explore"]
WorkerUnavailableReason = Literal[
    "engine_unsupported",
    "worker_disabled",
    "model_disabled",
    "model_protocol_unsupported",
    "model_incomplete",
    "credential_missing",
]


class TenantWorker(BaseModel):
    id: str
    display_name: str
    model_id: str
    model_display_name: str
    model_name: str
    # 历史引擎仅用于展示；创建与执行边界只接受 pi。
    engine: str
    task_types: list[TenantWorkerTaskType] = Field(default_factory=list)
    max_running: int
    priority: int
    enabled: bool
    available: bool
    unavailable_reason: WorkerUnavailableReason | None = None
    executor_available: bool
    revision: int
    created_at: str
    updated_at: str


class CreateTenantWorkerRequest(BaseModel):
    engine: Literal["pi"] = "pi"
    display_name: str
    model_id: str
    task_types: list[TenantWorkerTaskType] = Field(default_factory=lambda: ["reason", "bootstrap", "explore"])
    max_running: int = Field(default=1, ge=1, le=64)
    priority: int = Field(default=100, ge=-10_000, le=10_000)
    enabled: bool = True

    @field_validator("display_name", "model_id")
    @classmethod
    def normalize_required_text(cls, value: str, info) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        limit = 128 if info.field_name == "display_name" else 128
        if len(text) > limit:
            raise ValueError(f"must be {limit} characters or fewer")
        return text

    @field_validator("task_types")
    @classmethod
    def normalize_task_types(cls, value: list[TenantWorkerTaskType]) -> list[TenantWorkerTaskType]:
        cleaned = list(dict.fromkeys(value))
        if not cleaned:
            raise ValueError("must include at least one task type")
        return cleaned


class UpdateTenantWorkerRequest(CreateTenantWorkerRequest):
    expected_revision: int = Field(gt=0)


class DeleteTenantWorkerRequest(BaseModel):
    expected_revision: int = Field(gt=0)


class TenantWorkerExecutionBinding(BaseModel):
    worker_id: str
    display_name: str
    worker_revision: int
    model_id: str
    model_revision: int
    credential_version_id: str
    credential_version: int
    credential_fingerprint: str
    engine: Literal["pi"] = "pi"
    protocol: ModelProtocol
    task_types: list[TenantWorkerTaskType]
    max_running: int
    priority: int
    model_name: str
    base_url: str
    inference_endpoint: str
    auth_header: ModelAuthHeader
    custom_headers: list[CustomModelHeader] = Field(default_factory=list)
    api_key: str = Field(repr=False)


__all__ = [name for name in globals() if not name.startswith("__")]
