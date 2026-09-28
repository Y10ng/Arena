"""版本化 advisory library 对外协议。"""

from __future__ import annotations

from re import fullmatch
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class LibraryCreate(BaseModel):
    key: str
    name: str | None = None

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        text = value.strip()
        if not fullmatch(r"[a-z0-9_-]{1,128}", text):
            raise ValueError("must contain only lowercase letters, numbers, underscores, or hyphens")
        return text

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text or len(text) > 256:
            raise ValueError("must be between 1 and 256 characters")
        return text


class LibraryFileWrite(BaseModel):
    path: str
    content: str = ""

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        # 仅约束原始 UTF-8 字节大小，不检查或改写凭据、脚本等业务内容。
        if len(value.encode("utf-8")) > 2_000_000:
            raise ValueError("must be 2000000 UTF-8 bytes or fewer")
        return value


class LibraryVersionCreate(BaseModel):
    files: list[LibraryFileWrite] = Field(default_factory=list)
    message: str | None = None

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) > 1000:
            raise ValueError("must be 1000 characters or fewer")
        return value


class LibraryFile(BaseModel):
    path: str
    size_bytes: int
    sha256: str


class LibraryFileContent(LibraryFile):
    content: str


class LibraryVersion(BaseModel):
    library_key: str
    version: int
    digest_sha256: str
    source: Literal["api", "legacy_filesystem"]
    message: str | None = None
    created_by: str | None = None
    created_at: str
    file_count: int = 0


class Library(BaseModel):
    key: str
    name: str
    latest_version: int | None = None
    file_count: int = 0
    created_at: str
    archived_at: str | None = None


class LibraryRevisionRef(BaseModel):
    library_key: str
    version: int
    digest_sha256: str


class LibraryBinding(BaseModel):
    project_id: str
    library_key: str
    version: int
    created_at: str


AdvisoryLibraryCreate = LibraryCreate
AdvisoryLibraryFileWrite = LibraryFileWrite
AdvisoryLibraryVersionCreate = LibraryVersionCreate
AdvisoryLibraryFile = LibraryFile
AdvisoryLibraryFileContent = LibraryFileContent
AdvisoryLibraryVersion = LibraryVersion
AdvisoryLibrary = Library
AdvisoryLibraryRevisionRef = LibraryRevisionRef
AdvisoryLibraryBinding = LibraryBinding

__all__ = [name for name in globals() if not name.startswith("__")]
