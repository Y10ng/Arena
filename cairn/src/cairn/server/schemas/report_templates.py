"""不可变报告模板版本的对外协议。"""

from __future__ import annotations

from re import fullmatch

from pydantic import BaseModel, Field, field_validator


class ReportTemplateCreate(BaseModel):
    key: str
    name: str
    description: str = ""

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        text = value.strip()
        if not fullmatch(r"[a-z0-9_-]{1,128}", text):
            raise ValueError(
                "must contain only lowercase letters, numbers, underscores, or hyphens"
            )
        return text

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


class ReportTemplateVersionCreate(BaseModel):
    markdown_content: str
    message: str | None = None
    compatibility_profiles: list[str] = Field(
        default_factory=lambda: ["assessment-report-packet-v1"]
    )

    @field_validator("markdown_content")
    @classmethod
    def validate_markdown(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        if len(text.encode("utf-8")) > 500_000:
            raise ValueError("must be 500000 UTF-8 bytes or fewer")
        return text

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) > 1000:
            raise ValueError("must be 1000 characters or fewer")
        return value.strip() or None

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


class ReportTemplateVersion(BaseModel):
    template_key: str
    version: int
    digest_sha256: str
    compatibility_profiles: list[str] = Field(default_factory=list)
    message: str | None = None
    created_by: str | None = None
    created_at: str


class ReportTemplateVersionDetail(ReportTemplateVersion):
    markdown_content: str


class ReportTemplate(BaseModel):
    key: str
    name: str
    description: str = ""
    latest_version: int | None = None
    created_at: str
    archived_at: str | None = None


class ReportTemplateCatalogEntry(BaseModel):
    key: str
    name: str
    description: str = ""
    version: int
    digest_sha256: str
    compatibility_profiles: list[str] = Field(default_factory=list)


class ReportTemplateArchiveResponse(BaseModel):
    key: str
    archived: bool = True


__all__ = [name for name in globals() if not name.startswith("__")]
