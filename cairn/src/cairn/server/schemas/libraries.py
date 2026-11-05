"""资料库当前文件、选择与实际使用绑定的对外协议。"""

from __future__ import annotations

from re import fullmatch
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


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


class LibraryFilesWrite(BaseModel):
    files: list[LibraryFileWrite] = Field(default_factory=list)


class LibraryFile(BaseModel):
    path: str
    size_bytes: int
    sha256: str


class LibraryFileContent(LibraryFile):
    content: str


class SelectableLibraryManifest(BaseModel):
    library_key: str
    library_name: str
    file_count: int
    files: list[LibraryFile]
    safety_path: str | None = None


class LibrarySelection(BaseModel):
    model_config = {"extra": "forbid"}

    library_key: str
    paths: list[str] | None
    source: Literal["current", "saved"] = "current"
    expected_files: dict[str, str] | None = None

    @model_validator(mode="after")
    def validate_source(self):
        # saved 只由服务端从当前计划自己的绑定解析，不接受客户端快照 ID。
        if self.source == "saved":
            if self.expected_files is not None:
                raise ValueError("已保存资料不能同时指定当前文件摘要")
        elif not self.expected_files or any(
            not fullmatch(r"[0-9a-f]{64}", digest) for digest in self.expected_files.values()
        ):
            raise ValueError("请重新加载并选择当前资料")
        return self

    @field_validator("library_key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        text = value.strip()
        if not fullmatch(r"[a-z0-9_-]{1,128}", text):
            raise ValueError("invalid library key")
        return text

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("library paths must not be empty")
        normalized = []
        for path in value:
            text = path.replace("\\", "/")
            parts = text.split("/")
            if (not text or len(text) > 512 or any(
                not part or part in {".", ".."} or len(part) > 255
                or any(ord(char) < 32 or ord(char) == 127 for char in part)
                for part in parts
            )):
                raise ValueError("invalid library path")
            normalized.append(text)
        if len(set(normalized)) != len(normalized):
            raise ValueError("duplicate library path")
        return sorted(normalized)


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
    builtin: bool = False
    file_count: int = 0
    created_at: str
    archived_at: str | None = None


class LibraryRevisionRef(BaseModel):
    library_key: str
    version: int
    digest_sha256: str
    paths: list[str] | None = None
    selection_sha256: str | None = None


class LibraryBinding(BaseModel):
    project_id: str
    library_key: str
    version: int
    created_at: str
    paths: list[str] | None = None
    selection_sha256: str


AdvisoryLibraryCreate = LibraryCreate
AdvisoryLibraryFileWrite = LibraryFileWrite
AdvisoryLibraryFile = LibraryFile
AdvisoryLibraryFileContent = LibraryFileContent
AdvisoryLibraryVersion = LibraryVersion
AdvisoryLibrary = Library
AdvisoryLibraryRevisionRef = LibraryRevisionRef
AdvisoryLibraryBinding = LibraryBinding

__all__ = [name for name in globals() if not name.startswith("__")]
