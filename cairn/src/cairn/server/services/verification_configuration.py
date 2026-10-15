"""每租户唯一的复测专用 Worker 配置；不参与普通资源选择。"""

from __future__ import annotations

import json
import sqlite3
from uuid import uuid4

from fastapi import HTTPException
from psycopg.errors import ForeignKeyViolation, UniqueViolation

from cairn.server.auth import RequestPrincipal, require_super_admin, write_security_audit
from cairn.server.db import get_conn, schedule_transaction
from cairn.server.schemas.verification_requests import UpdateVerificationConfiguration

from .infra import DISPATCHER_HEARTBEAT_STALE_SECONDS
from .model_credentials import credential_status
from .tenant_workers import _validate_model_binding, _worker_from_row, get_tenant_worker_row
from .time_utils import timestamp_delta_seconds, utcnow


_DEFAULTS = {
    "display_name": "漏洞复测专用 Worker", "engine": "pi",
    "max_running": 1, "priority": 100, "enabled": True,
}
_REASONS = {
    "engine_unsupported": "复测执行器不受支持，请保存 pi 执行配置。",
    "worker_disabled": "复测专用 Worker 已停用。",
    "model_disabled": "复测模型已停用或已删除。",
    "model_protocol_unsupported": "复测模型协议不受执行器支持。",
    "model_incomplete": "复测模型尚未填写实际模型名称。",
    "credential_missing": "复测模型尚未配置可用凭据。",
}


def require_configuration_admin(conn, principal: RequestPrincipal, tenant_id: str) -> None:
    require_super_admin(principal)
    if not tenant_id:
        raise HTTPException(422, "必须明确指定目标租户")
    if conn.execute("SELECT id FROM tenants WHERE id = ?", (tenant_id,)).fetchone() is None:
        raise HTTPException(404, "目标租户不存在")


def _execution_capacity_reason(conn) -> tuple[str | None, str]:
    available = []
    for row in conn.execute("SELECT status, updated_at, runtime_json FROM dispatcher_status"):
        age = timestamp_delta_seconds(row["updated_at"], utcnow())
        if row["status"] not in {"running", "idle"} or age is None or not 0 <= age <= DISPATCHER_HEARTBEAT_STALE_SECONDS:
            continue
        runtime = json.loads(row["runtime_json"] or "{}")
        capacity = runtime.get("verification_capacity")
        if type(capacity) is int and capacity > 0:
            return None, "专用 Worker 与模型可用；尚未开始的复测按开始时配置执行，满载时排队。"
        available.append(capacity)
    if not available:
        return "dispatcher_unavailable", "当前没有可用的复测执行器，请检查 Dispatcher 连接。"
    if all(type(capacity) is int and capacity == 0 for capacity in available):
        return "verification_capacity_zero", "Dispatcher 未配置复测独立容量；请提高执行硬上限，不能占用普通任务配额。"
    return "verification_capacity_unknown", "Dispatcher 尚未上报复测独立容量，暂不能确认可以执行。"


def _configuration_view(conn, tenant_id: str) -> dict:
    row = conn.execute("SELECT * FROM verification_configuration WHERE tenant_id = ?", (tenant_id,)).fetchone()
    view = {
        "tenant_id": tenant_id, "tenant_worker_id": None, "worker_name": None,
        "display_name": None, "engine": None, "model_id": None, "model_name": None,
        "model_display_name": None, "max_running": None, "priority": None,
        "enabled": None, "revision": None, "ready": False, "updated_at": None,
        "reason": "verification_not_configured", "message": "尚未配置漏洞复测专用 Worker 与模型，请联系管理员保存配置。",
    }
    if row is None:
        return view
    view["updated_at"] = row["updated_at"]
    if row["tenant_worker_id"] is None:
        view["message"] = "请重新保存复测专用 Worker 与模型配置；旧配置不再引用普通 Worker 或项目组。"
        return view
    worker_row = get_tenant_worker_row(conn, row["tenant_worker_id"], tenant_id=tenant_id, purpose="verification")
    worker = _worker_from_row(conn, worker_row)
    view.update(
        tenant_worker_id=worker.id, worker_name=worker.display_name, display_name=worker.display_name,
        engine=worker.engine, model_id=worker.model_id, model_name=worker.model_name,
        model_display_name=worker.model_display_name, max_running=worker.max_running,
        priority=worker.priority, enabled=worker.enabled, revision=worker.revision,
    )
    if not worker.available:
        view.update(reason=worker.unavailable_reason, message=_REASONS[worker.unavailable_reason])
        return view
    reason, message = _execution_capacity_reason(conn)
    view.update(ready=reason is None, reason=reason, message=message)
    return view


def resolve_verification_configuration(conn, tenant_id: str) -> str:
    view = _configuration_view(conn, tenant_id)
    if not view["ready"]:
        raise HTTPException(422, view["message"])
    return view["tenant_worker_id"]


def verification_readiness(principal: RequestPrincipal) -> dict:
    if principal.kind not in {"user", "integration"} or not principal.tenant_id:
        raise HTTPException(403, "复测需要租户用户或集成身份")
    with get_conn() as conn:
        view = _configuration_view(conn, principal.tenant_id)
        return {
            "configured": view["tenant_worker_id"] is not None,
            **{key: view[key] for key in ("ready", "worker_name", "model_name", "model_display_name", "enabled", "reason", "message")},
        }


def get_verification_configuration(principal: RequestPrincipal, *, tenant_id: str) -> dict:
    with get_conn() as conn:
        require_configuration_admin(conn, principal, tenant_id)
        return _configuration_view(conn, tenant_id)


def update_verification_configuration(body: UpdateVerificationConfiguration, principal: RequestPrincipal,
                                      *, tenant_id: str) -> dict:
    with schedule_transaction() as conn:
        require_configuration_admin(conn, principal, tenant_id)
        # 与首次 claim 共用租户锁；只修改稳定身份的配置，不热改已开始的执行。
        conn.execute("UPDATE tenants SET id = id WHERE id = ?", (tenant_id,))
        _validate_model_binding(conn, body.model_id, tenant_id=tenant_id)
        current = conn.execute("SELECT * FROM verification_configuration WHERE tenant_id = ?", (tenant_id,)).fetchone()
        now = utcnow()
        worker_id = current["tenant_worker_id"] if current is not None else None
        try:
            if worker_id is None:
                worker_id = f"twr_{uuid4().hex}"
                conn.execute(
                    "INSERT INTO tenant_workers(id, tenant_id, purpose, display_name, model_id, engine, task_types_json, "
                    "max_running, priority, enabled, revision, created_by, created_at, updated_at) "
                    "VALUES (?, ?, 'verification', ?, ?, 'pi', '[\"explore\"]', ?, ?, ?, 1, ?, ?, ?)",
                    (worker_id, tenant_id, body.display_name, body.model_id, body.max_running, body.priority,
                     int(body.enabled), principal.subject_id, now, now),
                )
            else:
                get_tenant_worker_row(conn, worker_id, tenant_id=tenant_id, purpose="verification")
                conn.execute(
                    "UPDATE tenant_workers SET display_name = ?, model_id = ?, engine = 'pi', task_types_json = '[\"explore\"]', "
                    "max_running = ?, priority = ?, enabled = ?, revision = revision + 1, updated_at = ? "
                    "WHERE id = ? AND tenant_id = ? AND purpose = 'verification'",
                    (body.display_name, body.model_id, body.max_running, body.priority, int(body.enabled), now, worker_id, tenant_id),
                )
        except (sqlite3.IntegrityError, UniqueViolation, ForeignKeyViolation) as exc:
            raise HTTPException(409, "复测 Worker 名称已存在，请使用其他名称") from exc
        conn.execute(
            "INSERT INTO verification_configuration(tenant_id, tenant_worker_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (tenant_id) DO UPDATE SET tenant_worker_id = excluded.tenant_worker_id, updated_at = excluded.updated_at",
            (tenant_id, worker_id, now),
        )
        write_security_audit(
            conn, actor_type=principal.kind, actor_id=principal.subject_id, tenant_id=tenant_id,
            action="verification.configuration.update", resource_type="worker", resource_id=worker_id,
            outcome="success", metadata={"model_id": body.model_id},
        )
        return _configuration_view(conn, tenant_id)


def verification_configuration_options(principal: RequestPrincipal, *, tenant_id: str) -> dict:
    with get_conn() as conn:
        require_configuration_admin(conn, principal, tenant_id)
        models = [
            {**{key: row[key] for key in ("id", "display_name", "model_name", "protocol")},
             "enabled": bool(row["enabled"]),
             "credential_configured": credential_status(conn, tenant_id=tenant_id, model_id=row["id"])["credential_configured"]}
            for row in conn.execute("SELECT * FROM tenant_models WHERE tenant_id = ? ORDER BY created_at, id", (tenant_id,)).fetchall()
        ]
    return {"models": models, "engines": [{"id": "pi", "name": "pi"}], "defaults": dict(_DEFAULTS)}
