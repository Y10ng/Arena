"""系统设置 schema。"""

from __future__ import annotations

from .common import *


class Settings(BaseModel):
    # intent/reason 租约下限避免 dispatcher loop 天然超过超时时间。
    intent_timeout: int = Field(ge=5)
    reason_timeout: int = Field(ge=5)


__all__ = [name for name in globals() if not name.startswith("__")]
