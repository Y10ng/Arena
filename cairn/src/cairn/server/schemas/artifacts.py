"""Artifact 和 evidence schema。"""

from __future__ import annotations

import json

from .common import *

class Artifact(BaseModel):
    id: str
    project_id: str
    task_run_id: str | None = None
    kind: ArtifactKind
    kind_deprecated: bool = False
    deprecation_message: str | None = None
    category: ArtifactCategory = "evidence"
    uri: str
    sha256: str
    size: int
    created_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactPreview(BaseModel):
    artifact_id: str
    project_id: str
    task_run_id: str | None = None
    kind: ArtifactKind
    kind_deprecated: bool = False
    deprecation_message: str | None = None
    content: str
    size: int
    bytes_read: int
    truncated: bool = False
    sha256: str


# Artifact 写入请求要求二选一提供文本或 base64，避免内容来源歧义。
class CreateArtifactRequest(BaseModel):
    task_run_id: str | None = None
    kind: CreateArtifactKind
    category: ArtifactCategory | None = None
    content: str | None = None
    content_base64: str | None = None
    filename: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("task_run_id", "filename")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("content")
    @classmethod
    def validate_optional_content(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # Artifact digest 和收据必须描述调用方提交的精确字节，校验非空时不可改写内容。
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("content_base64")
    @classmethod
    def validate_optional_base64(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        try:
            b64decode(text, validate=True)
        except BinasciiError as exc:
            raise ValueError("must be valid base64") from exc
        return text

    @classmethod
    def _validate_content_fields(cls, content: str | None, content_base64: str | None) -> None:
        if (content is None) == (content_base64 is None):
            raise ValueError("exactly one of content or content_base64 is required")

    def model_post_init(self, __context: Any) -> None:
        self._validate_content_fields(self.content, self.content_base64)


class CreateRawArtifactRequest(BaseModel):
    task_run_id: str | None = None
    kind: CreateArtifactKind
    category: ArtifactCategory | None = None
    filename: str | None = None
    metadata_json: str = "{}"

    @field_validator("task_run_id", "filename")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("metadata_json")
    @classmethod
    def validate_metadata_json(cls, value: str) -> str:
        try:
            metadata = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("must be valid JSON") from exc
        if not isinstance(metadata, dict):
            raise ValueError("must be a JSON object")
        return value

    @property
    def metadata(self) -> dict[str, Any]:
        return json.loads(self.metadata_json)


class FactEvidence(BaseModel):
    project_id: str
    fact_id: str
    artifact_id: str
    pointer: str
    verifier_run_id: str | None = None
    artifact_exists: bool = True
    verifier_run_exists: bool | None = None
    evidence_valid: bool = True
    created_at: str


__all__ = [name for name in globals() if not name.startswith("__")]
