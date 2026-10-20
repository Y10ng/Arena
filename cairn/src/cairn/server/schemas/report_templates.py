"""当前报告模板的目录与编辑协议。"""

from __future__ import annotations

from re import fullmatch

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ReportTemplateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    markdown_content: str
    compatibility_profiles: list[str] = Field(
        default_factory=lambda: ["assessment-report-packet-v1"]
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        text = value.strip()
        if not text or len(text) > 256:
            raise ValueError("must be between 1 and 256 characters")
        return text

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        if len(value) > 2000:
            raise ValueError("must be 2000 characters or fewer")
        return value.strip()

    @field_validator("markdown_content")
    @classmethod
    def validate_markdown(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        if len(value.encode("utf-8")) > 500_000:
            raise ValueError("must be 500000 UTF-8 bytes or fewer")
        # Markdown 缩进和末尾空格有语义，保存与 digest 均保留提交的正文。
        return value

    @field_validator("compatibility_profiles")
    @classmethod
    def validate_profiles(cls, value: list[str]) -> list[str]:
        profiles: list[str] = []
        for item in value:
            text = item.strip()
            if not fullmatch(r"[a-z0-9_-]{1,128}", text):
                raise ValueError("compatibility profile must be a stable identifier")
            if text not in profiles:
                profiles.append(text)
        if not profiles:
            raise ValueError("at least one compatibility profile is required")
        return profiles


class ReportTemplateCreate(ReportTemplateUpdate):
    key: str | None = None

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not fullmatch(r"[a-z0-9_-]{1,128}", text):
            raise ValueError(
                "must contain only lowercase letters, numbers, underscores, or hyphens"
            )
        return text


class ReportTemplate(BaseModel):
    key: str
    name: str
    description: str = ""
    markdown_content: str | None = None
    digest_sha256: str | None = None
    compatibility_profiles: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str


class ReportTemplateCatalogEntry(BaseModel):
    key: str
    name: str
    description: str = ""
    digest_sha256: str
    compatibility_profiles: list[str] = Field(default_factory=list)


__all__ = [name for name in globals() if not name.startswith("__")]
