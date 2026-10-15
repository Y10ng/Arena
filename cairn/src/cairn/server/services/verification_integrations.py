"""复测集成身份、凭据与启停；旧资源默认字段仅保留历史，不参与受理。"""

from __future__ import annotations

import secrets
from uuid import uuid4

from fastapi import HTTPException

from cairn.server.auth import RequestPrincipal, hash_secret
from cairn.server.db import get_conn, schedule_transaction
from cairn.server.schemas.verification_requests import CreateVerificationIntegration, UpdateVerificationIntegration

from .time_utils import utcnow
from .verification_configuration import require_configuration_admin


def integration_view(row) -> dict:
    return {key: row[key] for key in ("id", "name", "created_at", "updated_at", "last_seen_at")} | {"enabled": bool(row["enabled"])}


def get_integration(conn, integration_id: str, tenant_id: str):
    row = conn.execute("SELECT * FROM verification_integrations WHERE id = ? AND tenant_id = ?", (integration_id, tenant_id)).fetchone()
    if row is None:
        raise HTTPException(404, "复测集成不存在")
    return row


def list_integrations(principal: RequestPrincipal, *, tenant_id: str) -> list[dict]:
    with get_conn() as conn:
        require_configuration_admin(conn, principal, tenant_id)
        return [integration_view(row) for row in conn.execute("SELECT * FROM verification_integrations WHERE tenant_id = ? ORDER BY created_at, id", (tenant_id,)).fetchall()]


def create_integration(body: CreateVerificationIntegration, principal: RequestPrincipal, *, tenant_id: str) -> dict:
    with schedule_transaction() as conn:
        require_configuration_admin(conn, principal, tenant_id)
        token = "avr_" + secrets.token_urlsafe(32)
        integration_id = "vi_" + uuid4().hex
        now = utcnow()
        conn.execute(
            "INSERT INTO verification_integrations(id, tenant_id, name, token_hash, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (integration_id, tenant_id, body.name, hash_secret(token), int(body.enabled), now, now),
        )
        view = integration_view(get_integration(conn, integration_id, tenant_id))
    return {"integration": view, "token": token}


def update_integration(integration_id: str, body: UpdateVerificationIntegration, principal: RequestPrincipal, *, tenant_id: str) -> dict:
    with schedule_transaction() as conn:
        require_configuration_admin(conn, principal, tenant_id)
        conn.execute("UPDATE verification_integrations SET id = id WHERE id = ? AND tenant_id = ?", (integration_id, tenant_id))
        get_integration(conn, integration_id, tenant_id)
        changes = body.model_dump(exclude_unset=True)
        if "name" in changes and (changes["name"] is None or not changes["name"].strip()):
            raise HTTPException(422, "集成名称不能为空")
        for key, value in changes.items():
            conn.execute(f"UPDATE verification_integrations SET {key} = ? WHERE id = ?", (int(value) if key == "enabled" else value, integration_id))
        conn.execute("UPDATE verification_integrations SET updated_at = ? WHERE id = ?", (utcnow(), integration_id))
        return integration_view(get_integration(conn, integration_id, tenant_id))


def disable_integration(integration_id: str, principal: RequestPrincipal, *, tenant_id: str) -> dict:
    with get_conn() as conn:
        require_configuration_admin(conn, principal, tenant_id)
        get_integration(conn, integration_id, tenant_id)
        conn.execute("UPDATE verification_integrations SET enabled = 0, updated_at = ? WHERE id = ?", (utcnow(), integration_id))
        return integration_view(get_integration(conn, integration_id, tenant_id))


def rotate_integration_token(integration_id: str, principal: RequestPrincipal, *, tenant_id: str) -> dict:
    with get_conn() as conn:
        require_configuration_admin(conn, principal, tenant_id)
        get_integration(conn, integration_id, tenant_id)
        token = "avr_" + secrets.token_urlsafe(32)
        # 轮换只替换 secret，不改变稳定调用身份，也不擅自重新启用已停用入口。
        conn.execute("UPDATE verification_integrations SET token_hash = ?, updated_at = ? WHERE id = ?", (hash_secret(token), utcnow(), integration_id))
        view = integration_view(get_integration(conn, integration_id, tenant_id))
    return {"integration": view, "token": token}
