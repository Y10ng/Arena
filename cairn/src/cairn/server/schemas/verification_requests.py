"""外部漏洞复现／复测的独立请求与窄入口协议。"""

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cairn.evidence_contracts import (
    EvidenceItem, VerificationKeyEvidence, VerificationTestStep, evidence_refs,
    validate_verification_evidence_links, has_evidence,
)

from .projects import CreateProjectRequest, EvidenceRef
from .tasks import CreateTaskRunRequest
from .test_egress import TestEgressInput


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
    observation_id: str | None = Field(default=None, min_length=1)


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
    origin: str | None = None
    target_url: str | None = Field(default=None, max_length=4096)

    @field_validator("target_url")
    @classmethod
    def explicit_target_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (not value or any(char.isspace() or ord(char) < 32 for char in value)
                or "\\" in value or parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None or parsed.fragment):
            raise ValueError("本次目标必须是明确的 HTTP(S) URL，不含凭据、空白或片段")
        # 检查端口但保留用户输入，不改协议、域名或路径。
        _ = parsed.port
        return value

    report: Annotated[InlineVerificationReport | FileVerificationReport | PlatformVulnerabilityVerificationReport, Field(discriminator="type")]
    resource_batch_id: str | None = None
    previous_request_id: str | None = None
    authorization_expires_at: str | None = None
    execution_deadline: str | None = None
    test_egress: TestEgressInput | None = None

    @field_validator("title")
    @classmethod
    def nonempty_original(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("任务名称不能为空")
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


class HistoricalVerificationResult(BaseModel):
    """仅供已接受 v1 结果重放；保持原始摘要输入，不增加默认字段。"""
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


class HistoricalVerificationResultV2(BaseModel):
    """冻结 v2 解析与序列化，仅用于历史读取和已接受结果的精确回执。"""
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[2]
    outcome: Literal["confirmed", "not_reproduced", "environment_changed", "insufficient_evidence"]
    summary: str = Field(min_length=1)
    tested_scope: str = Field(min_length=1)
    notes: str = Field(min_length=1)
    limitations: list[str]
    reason_code: str | None = None
    evidence_items: list[EvidenceItem]

    @field_validator("summary", "tested_scope", "notes")
    @classmethod
    def nonempty_original(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("结果必须说明结论、测试范围和实际观察")
        return value

    @property
    def evidence_refs(self) -> list[EvidenceRef]:
        return evidence_refs(self.evidence_items)

    @model_validator(mode="after")
    def require_current_evidence(self):
        if self.outcome != "insufficient_evidence" and not self.evidence_refs:
            raise ValueError("本结论必须引用本轮实际证据")
        if self.outcome == "insufficient_evidence" and not any(item.strip() for item in self.limitations):
            raise ValueError("证据不足必须明确缺口和未测试事项")
        if any(item.request_text is not None or item.response_text is not None or item.text is not None
               for item in self.evidence_items):
            raise ValueError("本轮证据必须引用原件，不能内联历史文本")
        if len({item.id for item in self.evidence_items}) != len(self.evidence_items):
            raise ValueError("本轮证据条目 ID 不能重复")
        return self


class HistoricalVerificationResultV3(BaseModel):
    """冻结 v3 读取与回执，不给已接受结果补写建议或改变摘要输入。"""
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[3]
    outcome: Literal["confirmed", "not_reproduced", "environment_changed", "insufficient_evidence"]
    summary: str = Field(min_length=1)
    tested_scope: str = Field(min_length=1)
    limitations: list[str]
    reason_code: str | None = None
    key_evidence: list[VerificationKeyEvidence]
    test_steps: list[VerificationTestStep]
    evidence_items: list[EvidenceItem]

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema_version(cls, value):
        if type(value) is not int or value != 3:
            raise ValueError("结果 schema_version 必须为整数 3")
        return value

    @field_validator("summary", "tested_scope")
    @classmethod
    def nonempty_original(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("结果必须说明结论和测试范围")
        return value

    @property
    def evidence_refs(self) -> list[EvidenceRef]:
        return evidence_refs(self.evidence_items)

    @model_validator(mode="after")
    def require_current_evidence(self):
        if self.outcome != "insufficient_evidence" and not has_evidence(self.evidence_items, current=True):
            raise ValueError("本结论必须引用本轮实际证据")
        if self.outcome == "insufficient_evidence" and not any(item.strip() for item in self.limitations):
            raise ValueError("证据不足必须明确缺口和未测试事项")
        if any(item.request_text is not None or item.response_text is not None or item.text is not None
               for item in self.evidence_items):
            raise ValueError("本轮证据必须引用原件，不能内联历史文本")
        validate_verification_evidence_links(self.evidence_items, self.key_evidence, self.test_steps)
        return self


class VerificationResult(HistoricalVerificationResultV3):
    schema_version: Literal[4]
    remediation: str = Field(min_length=1, pattern=r"\S")

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema_version(cls, value):
        if type(value) is not int or value != 4:
            raise ValueError("结果 schema_version 必须为整数 4")
        return value


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
    result: VerificationResult | HistoricalVerificationResultV3 | HistoricalVerificationResultV2 | HistoricalVerificationResult


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
    status: Literal["pending", "stopped", "removed"]
    error: VerificationError | None = None
