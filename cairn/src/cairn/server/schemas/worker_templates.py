"""Tenant Worker 模板 schema。

新写路径只保存同 Tenant Worker ID；历史 Worker 名称仅保留只读兼容。
"""

from __future__ import annotations

from .common import *

class WorkerTemplate(BaseModel):
    id: str
    name: str
    owner: str
    tenant_worker_ids: list[str] = Field(default_factory=list)
    # 仅用于历史数据读取兼容，新写路径不再接受全局 Worker 名称。
    worker_names: list[str] = Field(default_factory=list)
    created_at: str


class CreateWorkerTemplateRequest(BaseModel):
    name: str
    # 兼容旧客户端输入；服务端始终以认证主体覆盖该值。
    owner: str | None = None
    tenant_worker_ids: list[str] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        if len(text) > 128:
            raise ValueError("must be 128 characters or fewer")
        return text

    @field_validator("tenant_worker_ids")
    @classmethod
    def validate_tenant_worker_ids(cls, value: list[str]) -> list[str]:
        return _clean_worker_ids(value)


class UpdateWorkerTemplateRequest(BaseModel):
    name: str | None = None
    tenant_worker_ids: list[str] | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        if len(text) > 128:
            raise ValueError("must be 128 characters or fewer")
        return text

    @field_validator("tenant_worker_ids")
    @classmethod
    def validate_tenant_worker_ids(
        cls,
        value: list[str] | None,
    ) -> list[str] | None:
        if value is None:
            return None
        return _clean_worker_ids(value)


def _clean_worker_ids(value: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item).strip()
        if not text:
            raise ValueError("worker ids must not be empty")
        if text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    if not cleaned:
        raise ValueError("at least one worker is required")
    return cleaned


__all__ = [name for name in globals() if not name.startswith("__")]
