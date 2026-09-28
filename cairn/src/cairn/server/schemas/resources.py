"""项目级辅助资源 schema。"""

from __future__ import annotations

from .common import *


ProjectResourceKind = Literal["note", "script", "file"]


class ProjectResourceLimits(BaseModel):
    max_active_count: int
    max_file_bytes: int
    max_active_total_bytes: int
    max_zip_upload_bytes: int
    max_zip_entries: int
    max_compression_ratio: int
    max_open_batches_per_user: int
    max_staged_bytes_per_user: int
    max_path_bytes: int
    max_path_segment_bytes: int


class ProjectResourceConfig(BaseModel):
    kinds: list[ProjectResourceKind]
    limits: ProjectResourceLimits
    reserved_names: list[str]
    batch_ttl_seconds: int
    zip_policy: dict[str, Any]


class ProjectResourceBatchMember(BaseModel):
    id: str
    kind: ProjectResourceKind
    path: str
    size_bytes: int
    sha256: str
    source: Literal["file", "zip"]


class ProjectResourceBatch(BaseModel):
    id: str
    state: Literal["open", "consumed"]
    created_at: str
    expires_at: str
    resource_count: int
    total_bytes: int
    members: list[ProjectResourceBatchMember] = Field(default_factory=list)


class UpdateProjectResourceBatchMemberRequest(BaseModel):
    path: str | None = None
    kind: ProjectResourceKind | None = None

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from cairn.server.services.project_resources import validate_project_resource_path

        return validate_project_resource_path(value)


class ProjectResourceImportRequest(BaseModel):
    resource_batch_id: str

    @field_validator("resource_batch_id")
    @classmethod
    def validate_batch_id(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("resource_batch_id must not be empty")
        return text


class ProjectResourceImportResponse(BaseModel):
    resources: list["ProjectResource"]


class ProjectResource(BaseModel):
    id: str
    project_id: str
    kind: ProjectResourceKind
    scope: Literal["project"] = "project"
    path: str
    size_bytes: int
    sha256: str
    active: bool
    created_by: str
    created_at: str
    deactivated_at: str | None = None


class CreateProjectResourceRequest(BaseModel):
    kind: ProjectResourceKind
    path: str
    content: str

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        from cairn.server.services.project_resources import validate_project_resource_path

        return validate_project_resource_path(value)


__all__ = [name for name in globals() if not name.startswith("__")]
