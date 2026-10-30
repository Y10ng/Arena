"""日常运行参数的窄投影，不接收执行画像或凭据。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _RuntimePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before", check_fields=False)
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("字段不可为空")
        return value


class DispatcherRuntimePatch(_RuntimePatch):
    max_workers: int | None = Field(default=None, gt=0, strict=True)
    max_running_projects: int | None = Field(default=None, gt=0, strict=True)
    max_project_workers: int | None = Field(default=None, gt=0, strict=True)
    interval: int | None = Field(default=None, gt=0, strict=True)
    healthcheck_timeout: int | None = Field(default=None, gt=0, strict=True)


class DispatcherTaskTimeoutPatch(_RuntimePatch):
    timeout: int | None = Field(default=None, gt=0, strict=True)


class DispatcherConcludeTimeoutPatch(DispatcherTaskTimeoutPatch):
    conclude_timeout: int | None = Field(default=None, gt=0, strict=True)


class DispatcherTasksPatch(_RuntimePatch):
    bootstrap: DispatcherConcludeTimeoutPatch | None = None
    reason: DispatcherTaskTimeoutPatch | None = None
    explore: DispatcherConcludeTimeoutPatch | None = None
    report: DispatcherTaskTimeoutPatch | None = None


class DispatcherRuntimeSettingsUpdate(_RuntimePatch):
    revision: str = Field(min_length=1)
    runtime: DispatcherRuntimePatch | None = None
    tasks: DispatcherTasksPatch | None = None


class DispatcherRuntimeValues(BaseModel):
    max_workers: int
    max_running_projects: int
    max_project_workers: int
    interval: int
    healthcheck_timeout: int


class DispatcherTaskTimeout(BaseModel):
    timeout: int


class DispatcherConcludeTimeout(DispatcherTaskTimeout):
    conclude_timeout: int


class DispatcherTaskValues(BaseModel):
    bootstrap: DispatcherConcludeTimeout
    reason: DispatcherTaskTimeout
    explore: DispatcherConcludeTimeout
    report: DispatcherTaskTimeout


class DispatcherConfigLoadStatus(BaseModel):
    state: Literal["loaded", "pending", "failed", "unknown"]
    hard_cap: int | None = None
    updated_at: str | None = None
    message: str


class DispatcherRuntimeSettingsDocument(BaseModel):
    revision: str
    runtime: DispatcherRuntimeValues
    tasks: DispatcherTaskValues
    load_status: DispatcherConfigLoadStatus
