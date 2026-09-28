"""Server 对外协议模型兼容 facade。

实际模型按领域拆到 `cairn.server.schemas`；旧的 `cairn.server.models` 导入路径继续保留。
"""

from __future__ import annotations

from pydantic import BaseModel

from .schemas.common import *
from .schemas.settings import *
from .schemas.scenarios import *
from .schemas.knowledge import *
from .schemas.libraries import *
from .schemas.report_templates import *
from .schemas.prompts import *
from .schemas.projects import *
from .schemas.groups import *
from .schemas.worker_templates import *
from .schemas.tenant_workers import *
from .schemas.tasks import *
from .schemas.events import *
from .schemas.artifacts import *
from .schemas.resources import *
from .schemas.injection_payloads import *
from .schemas.monitoring import *
from .schemas.admin import *

for _name, _value in list(globals().items()):
    if (
        isinstance(_value, type)
        and issubclass(_value, BaseModel)
        and getattr(_value, "__module__", "").startswith("cairn.server.schemas.")
    ):
        _value.__module__ = __name__

__all__ = [name for name in globals() if not name.startswith("_")]
