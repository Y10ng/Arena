"""Prompt 模板组 schema。"""

from __future__ import annotations

from .common import *

class PromptGroupCreate(BaseModel):
    key: str
    clone_from: str | None = None

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        text = value.strip()
        if not fullmatch(r"[a-z0-9_-]+", text):
            raise ValueError("must contain only lowercase letters, numbers, underscores, or hyphens")
        if len(text) > 128:
            raise ValueError("must be 128 characters or fewer")
        return text

    @field_validator("clone_from")
    @classmethod
    def validate_clone_from(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        if not fullmatch(r"[a-z0-9_-]+", text):
            raise ValueError("must contain only lowercase letters, numbers, underscores, or hyphens")
        if len(text) > 128:
            raise ValueError("must be 128 characters or fewer")
        return text


class PromptGroupRepair(BaseModel):
    clone_from: str | None = None

    @field_validator("clone_from")
    @classmethod
    def validate_clone_from(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        if not fullmatch(r"[a-z0-9_-]+", text):
            raise ValueError("must contain only lowercase letters, numbers, underscores, or hyphens")
        if len(text) > 128:
            raise ValueError("must be 128 characters or fewer")
        return text


class PromptFileWrite(BaseModel):
    content: str = ""

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        if len(value) > 2_000_000:
            raise ValueError("must be 2000000 characters or fewer")
        return value


class PromptFile(BaseModel):
    name: str
    path: str
    size: int
    updated_at: str | None = None
    required_tokens: list[str] = Field(default_factory=list)
    missing_tokens: list[str] = Field(default_factory=list)


class PromptFileContent(PromptFile):
    content: str


# Prompt 组校验结果只描述缺失，不在模型层决定如何修复。
class PromptGroupValidationResult(BaseModel):
    valid: bool
    missing_files: list[str] = Field(default_factory=list)
    missing_tokens: dict[str, list[str]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class PromptGroup(BaseModel):
    key: str
    path: str
    builtin: bool = False
    managed: bool = False
    file_count: int = 0
    referenced_by: list[str] = Field(default_factory=list)
    overrides_builtin: bool = False
    capabilities: list[str] = Field(default_factory=list)
    visibility: str = "visible"
    valid: bool = True
    warnings: list[str] = Field(default_factory=list)


__all__ = [name for name in globals() if not name.startswith("__")]
