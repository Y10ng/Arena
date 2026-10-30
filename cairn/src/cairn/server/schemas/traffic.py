"""采集器原记录的校验边界；存储仍保留入站原 JSON，不以展示投影回写。"""

from __future__ import annotations

import base64
import binascii
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .test_egress import TestEgressView


TRAFFIC_CHUNK_BYTES = 1024 * 1024
TRAFFIC_BATCH_FLOWS = 8
TRAFFIC_BATCH_BODY_BYTES = 64 * 1024
TRAFFIC_BATCH_BYTES = 1024 * 1024
TrafficReadMode = Literal["auto", "manual"]


class TrafficSummaryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, min_length=1, max_length=2048)
    project_id: str | None = Field(default=None, min_length=1, max_length=200)
    flow_id: str | None = Field(default=None, min_length=1, max_length=200)
    task_run_id: str | None = Field(default=None, min_length=1, max_length=200)
    method: str | None = Field(default=None, min_length=1, max_length=100)
    host: str | None = Field(default=None, min_length=1, max_length=2048)
    url: str | None = Field(default=None, min_length=1, max_length=8192)
    path: str | None = Field(default=None, min_length=1, max_length=8192)
    state: Literal["receiving", "complete", "partial", "failed"] | None = None
    status_family: Literal["1xx", "2xx", "3xx", "4xx", "5xx", "6xx", "7xx", "8xx", "9xx"] | None = None
    status_code: int | None = Field(default=None, ge=100, le=999)
    associated: bool | None = None
    error_code: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("method", "host")
    @classmethod
    def normalize_case(cls, value: str | None, info):
        if value is None:
            return None
        return value.upper() if info.field_name == "method" else value.lower()


class TrafficAssociationLocation(BaseModel):
    observation_id: str
    vulnerability_id: str
    status: str
    task_run_id: str | None


class TrafficAssociationSummary(BaseModel):
    observation_count: int
    vulnerability_count: int
    items: list[TrafficAssociationLocation]
    has_more: bool


class TrafficSummary(BaseModel):
    id: str
    project_id: str
    project_title: str
    capture_scope_id: str
    task_run_id: str
    revision: int
    received_at: str
    updated_at: str
    started_at: float
    finished_at: float | None
    method: str
    host: str
    url: str
    path: str
    connection_host: str
    address_issue: str | None
    status_code: int | None
    state: Literal["receiving", "complete", "partial", "failed"]
    source_state: Literal["receiving", "complete", "partial", "failed"]
    error_code: str | None
    sync: dict
    associations: TrafficAssociationSummary


class TrafficSummaryPage(BaseModel):
    items: list[TrafficSummary]
    next_cursor: str | None
    previous_cursor: str | None
    has_more: bool
    has_previous: bool


class TrafficBodySeal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    revision: int = Field(gt=0)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TrafficScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    project_id: str = Field(min_length=1, max_length=200)
    task_run_id: str = Field(min_length=1, max_length=200)
    generation: int = Field(ge=0)
    container_id: str = Field(min_length=1, max_length=255)
    test_egress: TestEgressView = Field(default_factory=TestEgressView)


class TrafficMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    http_version: str
    headers_base64: list[list[str]]
    body_bytes: int = Field(ge=0)
    body_complete: bool
    body_sealed: bool
    body_revision: int | None = Field(gt=0)
    body_file: str

    @field_validator("headers_base64")
    @classmethod
    def validate_headers(cls, values: list[list[str]]) -> list[list[str]]:
        for pair in values:
            if len(pair) != 2:
                raise ValueError("traffic_headers_invalid")
            for value in pair:
                _validate_base64(value)
        return values

    @model_validator(mode="after")
    def validate_seal(self):
        if self.body_sealed != (self.body_revision is not None):
            raise ValueError("traffic_body_seal_invalid")
        if self.body_complete and not self.body_sealed:
            raise ValueError("traffic_body_complete_unsealed")
        return self


def _validate_base64(value: str) -> str:
    try:
        base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("traffic_base64_invalid") from exc
    return value


class TrafficRequest(TrafficMessage):
    method: str
    host: str
    port: int = Field(ge=0, le=65535)
    scheme: str
    target_base64: str
    authority_base64: str

    @field_validator("target_base64", "authority_base64")
    @classmethod
    def validate_bytes(cls, value: str) -> str:
        return _validate_base64(value)


class TrafficResponse(TrafficMessage):
    status_code: int = Field(ge=100, le=999)
    reason_base64: str

    @field_validator("reason_base64")
    @classmethod
    def validate_bytes(cls, value: str) -> str:
        return _validate_base64(value)


class TrafficError(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: str
    message: str | None


class TrafficRecovery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    source: Literal["runtime"]
    last_capture_revision: int = Field(gt=0)
    writer_container_id: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_-]+$")
    writer_stopped_at: float = Field(ge=0)


class TrafficScopeStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    state: Literal["starting", "ready", "running", "draining", "stopping", "stopped", "blocked", "retrying", "watching"]
    code: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    failure_code: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    pending: int | None = Field(default=None, ge=0)
    pending_bytes: int | None = Field(default=None, ge=0)
    control_stopped: bool | None = None
    test_stopped: bool | None = None
    collector_stopped: bool | None = None
    upload_complete: bool | None = None


class TrafficRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    scope: TrafficScope
    revision: int = Field(gt=0)
    representation: Literal["structured-http-v1"]
    collector_version: str
    observation_point: Literal["task-network-output"]
    original_destination: list[str | int] | None
    connection_id: str
    started_at: float
    finished_at: float | None
    state: Literal["receiving", "complete", "partial", "failed"]
    request: TrafficRequest
    response: TrafficResponse | None
    error: TrafficError | None
    recovery: TrafficRecovery | None = None

    @model_validator(mode="after")
    def validate_progress(self):
        if self.recovery is not None and (
            self.state not in {"partial", "failed"} or self.finished_at is not None
            or self.error is None or self.error.code != "collector_stopped" or self.error.message is not None
            or self.recovery.last_capture_revision >= self.revision
        ):
            raise ValueError("traffic_recovery_invalid")
        for side in (self.request, self.response):
            if side is None:
                continue
            if side.body_revision is not None and side.body_revision > self.revision:
                raise ValueError("traffic_body_revision_future")
            if self.state != "receiving" and not side.body_sealed:
                raise ValueError("traffic_terminal_unsealed")
        if self.state == "complete" and (
            self.response is None or not self.request.body_complete or not self.response.body_complete
        ):
            raise ValueError("traffic_complete_incomplete")
        return self
