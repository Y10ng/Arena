"""Tenant Worker 配置、可用性与 optimistic revision schema。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cairn.server.schemas.models import TenantModelExecutionBinding

TenantWorkerTaskType = Literal["reason", "bootstrap", "explore"]
WorkerUnavailableReason = Literal[
    "engine_unsupported",
    "worker_disabled",
    "model_disabled",
    "model_unconfigured",
    "model_protocol_unsupported",
    "model_incomplete",
    "credential_missing",
]


class TenantWorker(BaseModel):
    id: str
    display_name: str
    model_id: str | None
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


class UpdateTenantWorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str | None
    max_running: int = Field(ge=1, le=64)
    priority: int = Field(ge=-10_000, le=10_000)
    enabled: bool
    expected_revision: int = Field(gt=0)

    @field_validator("model_id")
    @classmethod
    def normalize_model_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text or len(text) > 128:
            raise ValueError("model_id must be 1–128 characters or null")
        return text


class TenantWorkerExecutionBinding(TenantModelExecutionBinding):
    worker_id: str
    display_name: str
    worker_revision: int
    task_types: list[TenantWorkerTaskType]
    max_running: int
    priority: int

    def execution_provenance(self) -> dict:
        return {
            **super().execution_provenance(),
            "source": "tenant_worker_binding",
            "worker_id": self.worker_id,
            "display_name": self.display_name,
            "worker_revision": self.worker_revision,
        }


__all__ = [name for name in globals() if not name.startswith("__")]
