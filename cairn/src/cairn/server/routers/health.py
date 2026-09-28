"""服务健康检查，验证当前配置的数据库连接可用。"""

from fastapi import APIRouter

from cairn.server.db import get_conn

router = APIRouter(tags=["health"])


@router.get("/health")
def healthcheck() -> dict[str, str]:
    """执行轻量查询，确认 Server 及其配置数据库已 ready。"""
    with get_conn() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}
