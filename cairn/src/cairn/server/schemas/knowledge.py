"""知识库 schema。"""

from __future__ import annotations

from .common import *

class KnowledgeSetCreate(BaseModel):
    key: str

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        text = value.strip()
        # knowledge_base 是内置只读入口，用户知识集只能使用较窄的文件系统安全字符集。
        if not fullmatch(r"[a-z0-9_-]+", text):
            raise ValueError("must contain only lowercase letters, numbers, underscores, or hyphens")
        if text == "knowledge_base":
            raise ValueError("knowledge_base is reserved")
        if len(text) > 128:
            raise ValueError("must be 128 characters or fewer")
        return text


class KnowledgeFileWrite(BaseModel):
    content: str = ""

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 2_000_000:
            raise ValueError("must be 2000000 UTF-8 bytes or fewer")
        return value


class KnowledgeFile(BaseModel):
    name: str
    path: str
    size: int
    updated_at: str | None = None
    warnings: list[str] = Field(default_factory=list)


class KnowledgeFileContent(KnowledgeFile):
    content: str


class KnowledgeSet(BaseModel):
    key: str
    path: str
    builtin: bool = False
    file_count: int = 0
    referenced_by: list[str] = Field(default_factory=list)


__all__ = [name for name in globals() if not name.startswith("__")]
