"""外部漏洞复现／复测的独立请求与窄入口协议。"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .projects import CreateProjectRequest, EvidenceRef
from .tasks import CreateTaskRunRequest


class InlineVerificationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["markdown", "text"]
    content: str = Field(min_length=1)

    @field_validator("content")
    @classmethod
    def nonempty_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("主资料不能为空")
        return value


class FileVerificationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["file"]
    batch_member_id: str = Field(min_length=1)


class PlatformVulnerabilityVerificationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["platform_vulnerability"]
    project_id: str = Field(min_length=1)
    vulnerability_id: str = Field(min_length=1)


class CreateVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("schema_version 必须为整数 1")
        return value

    request_id: UUID
    external_id: str | None = Field(default=None, max_length=512)
    mode: Literal["reproduce", "retest"]
    title: str = Field(min_length=1, max_length=512)
    origin: str = Field(min_length=1)
    report: Annotated[InlineVerificationReport | FileVerificationReport | PlatformVulnerabilityVerificationReport, Field(discriminator="type")]
    resource_batch_id: str | None = None
    previous_request_id: str | None = None
    authorization_expires_at: str | None
    execution_deadline: str | None = None

    @field_validator("title", "origin")
    @classmethod
    def nonempty_original(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("任务名称和授权说明不能为空")
        return value

    @field_validator("authorization_expires_at", "execution_deadline")
    @classmethod
    def absolute_timestamp(cls, value: str | None) -> str | None:
        return None if value is None else CreateProjectRequest._validate_absolute_timestamp(value)

    @model_validator(mode="after")
    def require_file_batch(self):
        if self.report.type == "file" and not self.resource_batch_id:
            raise ValueError("文件主资料必须指定 resource_batch_id")
        return self


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["confirmed", "not_reproduced", "environment_changed", "insufficient_evidence"]
    summary: str = Field(min_length=1)
    tested_scope: str = Field(min_length=1)
    notes: str = Field(min_length=1)
    limitations: list[str]
    reason_code: str | None = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)

    @field_validator("summary", "tested_scope", "notes")
    @classmethod
    def nonempty_original(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("结果必须说明结论、测试范围和实际观察")
        return value

    @model_validator(mode="after")
    def require_current_evidence(self):
        if self.outcome != "insufficient_evidence" and not self.evidence_refs:
            raise ValueError("本结论必须引用本轮实际证据")
        if self.outcome == "insufficient_evidence" and not any(item.strip() for item in self.limitations):
            raise ValueError("证据不足必须明确缺口和未测试事项")
        return self


class VerificationError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    message: str = Field(min_length=1, max_length=2048)


class VerificationExecutionBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    task_run_id: str
    execution_generation: int = Field(ge=0, strict=True)
    claim_token: str


class VerificationResultSubmission(VerificationExecutionBinding):
    result: VerificationResult


class VerificationFailureSubmission(VerificationExecutionBinding):
    error: VerificationError


class VerificationClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_run: CreateTaskRunRequest


class CreateVerificationResourceBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_request_id: str | None = None


class UpdateVerificationConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(default="漏洞复测专用 Worker", min_length=1, max_length=128)
    engine: Literal["pi"] = "pi"
    max_running: int = Field(default=1, ge=1, le=64)
    priority: int = Field(default=100, ge=-10_000, le=10_000)
    enabled: bool = True

    @field_validator("model_id", "display_name")
    @classmethod
    def nonempty_configuration_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("模型和 Worker 名称不能为空")
        return text


class CreateVerificationIntegration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=256)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("集成名称不能为空")
        return value


class UpdateVerificationIntegration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=256)
    enabled: bool | None = None

    @field_validator("enabled")
    @classmethod
    def enabled_not_null(cls, value: bool | None) -> bool:
        if value is None:
            raise ValueError("enabled 不能为空")
        return value


class VerificationCleanup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pending", "removed"]
    error: VerificationError | None = None
