"""系统配置接口，维护全局超时。"""

from fastapi import APIRouter, Depends

from cairn.server.auth import require_dispatcher_or_super_admin, require_super_admin
from cairn.server.db import get_conn
from cairn.server.models import Settings

router = APIRouter(tags=["settings"])


@router.get(
    "/settings",
    response_model=Settings,
    dependencies=[Depends(require_dispatcher_or_super_admin)],
)
def get_settings():
    with get_conn() as conn:
        # settings 目前是单行表，id=1 作为全局配置槽位。
        row = conn.execute("SELECT intent_timeout, reason_timeout FROM settings WHERE id = 1").fetchone()
        return Settings(intent_timeout=row["intent_timeout"], reason_timeout=row["reason_timeout"])


@router.put(
    "/settings",
    response_model=Settings,
    dependencies=[Depends(require_super_admin)],
)
def update_settings(body: Settings):
    with get_conn() as conn:
        conn.execute(
            "UPDATE settings SET intent_timeout = ?, reason_timeout = ? WHERE id = 1",
            (body.intent_timeout, body.reason_timeout),
        )
        return body
