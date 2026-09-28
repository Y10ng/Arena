"""项目组 schema。"""

from __future__ import annotations

from .common import *

# 项目组按 Tenant 隔离；owner 仅用于返回创建主体归因。
class ProjectGroup(BaseModel):
    id: str
    name: str
    owner: str
    created_at: str


class CreateProjectGroupRequest(BaseModel):
    name: str
    # 兼容旧客户端输入；服务端始终以认证主体覆盖该值。
    owner: str | None = None

    @field_validator("name")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        if len(text) > 128:
            raise ValueError("must be 128 characters or fewer")
        return text


class UpdateProjectGroupRequest(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        if len(text) > 128:
            raise ValueError("must be 128 characters or fewer")
        return text


__all__ = [name for name in globals() if not name.startswith("__")]
