"""系统一键更新路由：仅超级管理员可访问。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from cairn import __version__
from cairn.server.auth import require_super_admin
from cairn.server.db import get_conn
from cairn.server.services import system_update
from cairn.server.services.dispatcher_config_store import DispatcherConfigStore

router = APIRouter(
    tags=["system-update"],
    dependencies=[Depends(require_super_admin)],
)


class UpdateStatusResponse(BaseModel):
    version: str
    install_type: str
    state: dict[str, Any]
    channel_configured: bool
    gateway_image: str | None
    running_task_count: int
    queued_task_count: int
    running_project_count: int


class CheckResponse(BaseModel):
    phase: str
    current_version: str
    channel_version: str | None
    update_available: bool


class PrepareRequest(BaseModel):
    pass


def _running_work() -> dict[str, int]:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running_task_count,
                SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued_task_count,
                COUNT(DISTINCT CASE WHEN status IN ('queued', 'running')
                    THEN project_id END) AS running_project_count
            FROM task_runs
            """
        ).fetchone()
    return {
        "running_task_count": row["running_task_count"] or 0,
        "queued_task_count": row["queued_task_count"] or 0,
        "running_project_count": row["running_project_count"] or 0,
    }


def _current_gateway_image() -> str | None:
    """读取 dispatch.yaml 的当前网关引用；读取失败不阻塞状态展示。"""
    raw_path = os.environ.get("CAIRN_DISPATCH_CONFIG", "").strip()
    if not raw_path:
        return None
    try:
        store = DispatcherConfigStore(Path(raw_path))
        config, _content, _revision = store.read()
    except Exception:
        return None
    container = config.get("container")
    if isinstance(container, dict):
        value = container.get("traffic_gateway_image")
        if isinstance(value, str):
            return value
    return None


@router.get("/system/update/status", response_model=UpdateStatusResponse)
def get_status() -> UpdateStatusResponse:
    state = system_update.read_state()
    work = _running_work()
    return UpdateStatusResponse(
        version=__version__,
        install_type=system_update.install_type(),
        state=state,
        channel_configured=True,
        gateway_image=_current_gateway_image(),
        **work,
    )


@router.post("/system/update/check", response_model=CheckResponse)
def check() -> CheckResponse:
    try:
        system_update.start_check()
    except system_update.UpdateError as exc:
        raise HTTPException(409, str(exc)) from None
    state = system_update.read_state()
    channel_version = state.get("channel_version")
    return CheckResponse(
        phase=str(state.get("phase")),
        current_version=__version__,
        channel_version=channel_version if isinstance(channel_version, str) else None,
        update_available=state.get("phase") == system_update.PHASE_AVAILABLE,
    )


@router.post("/system/update/prepare")
def prepare() -> dict[str, str]:
    try:
        system_update.start_prepare()
    except system_update.UpdateError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"phase": system_update.PHASE_PREPARING}


@router.post("/system/update/cancel")
def cancel() -> dict[str, str]:
    try:
        system_update.cancel_prepare()
    except system_update.UpdateError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"phase": system_update.PHASE_PREPARING}


@router.post("/system/update/apply")
def apply() -> dict[str, int]:
    try:
        result = system_update.apply()
    except system_update.UpdateError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"updater_pid": result.updater_pid}
