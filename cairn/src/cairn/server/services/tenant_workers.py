"""Tenant Worker CRUD、Model 绑定与安全可用性投影。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from psycopg.errors import ForeignKeyViolation, UniqueViolation

from cairn.server.auth import RequestPrincipal, write_security_audit
from cairn.server.db import get_conn
from cairn.server.schemas.tenant_workers import (
    CreateTenantWorkerRequest,
    TenantWorker,
    TenantWorkerExecutionBinding,
    UpdateTenantWorkerRequest,
)

from .infra import dispatcher_executor_available
from .model_credentials import credential_status, decrypt_active_credential
from .model_transport import ModelTransportError, resolve_model_endpoint
from .time_utils import utcnow


_INFERENCE_ENDPOINTS = {
    "anthropic-messages": "/v1/messages",
    "openai-chat-completions": "/v1/chat/completions",
    "openai-responses": "/v1/responses",
}


def list_tenant_workers(*, tenant_id: str) -> list[TenantWorker]:
    _require_tenant_id(tenant_id)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tenant_workers WHERE tenant_id = ? ORDER BY created_at, id",
            (tenant_id,),
        ).fetchall()
        return [_worker_from_row(conn, row) for row in rows]


def get_tenant_worker(worker_id: str, *, tenant_id: str) -> TenantWorker:
    _require_tenant_id(tenant_id)
    with get_conn() as conn:
        return _worker_from_row(conn, get_tenant_worker_row(conn, worker_id, tenant_id=tenant_id))


def get_tenant_worker_row(conn: Any, worker_id: str, *, tenant_id: str):
    row = conn.execute(
        "SELECT * FROM tenant_workers WHERE id = ? AND tenant_id = ?",
        (worker_id, tenant_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Worker not found")
    return row


def create_tenant_worker(
    body: CreateTenantWorkerRequest,
    *,
    principal: RequestPrincipal,
) -> TenantWorker:
    tenant_id = _principal_tenant(principal)
    worker_id = f"twr_{uuid4().hex}"
    now = utcnow()
    with get_conn() as conn:
        _validate_model_binding(conn, body.model_id, tenant_id=tenant_id)
        try:
            conn.execute(
                """
                INSERT INTO tenant_workers
                    (id, tenant_id, display_name, model_id, engine, task_types_json,
                     max_running, priority, enabled, revision, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pi', ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    worker_id,
                    tenant_id,
                    body.display_name,
                    body.model_id,
                    json.dumps(body.task_types, ensure_ascii=False),
                    body.max_running,
                    body.priority,
                    int(body.enabled),
                    principal.subject_id,
                    now,
                    now,
                ),
            )
        except (sqlite3.IntegrityError, UniqueViolation, ForeignKeyViolation) as exc:
            raise HTTPException(409, "Worker name already exists") from exc
        write_security_audit(
            conn,
            actor_type=principal.kind,
            actor_id=principal.subject_id,
            tenant_id=tenant_id,
            action="worker.create",
            resource_type="worker",
            resource_id=worker_id,
            outcome="success",
            metadata={"model_id": body.model_id, "revision": 1},
        )
        return _worker_from_row(conn, get_tenant_worker_row(conn, worker_id, tenant_id=tenant_id))


def update_tenant_worker(
    worker_id: str,
    body: UpdateTenantWorkerRequest,
    *,
    principal: RequestPrincipal,
) -> TenantWorker:
    tenant_id = _principal_tenant(principal)
    now = utcnow()
    with get_conn() as conn:
        row = get_tenant_worker_row(conn, worker_id, tenant_id=tenant_id)
        if row["engine"] != "pi":
            raise HTTPException(422, "Worker is unavailable: engine_unsupported; create a new pi Worker")
        _validate_model_binding(conn, body.model_id, tenant_id=tenant_id)
        try:
            cursor = conn.execute(
                """
                UPDATE tenant_workers
                SET display_name = ?, model_id = ?, task_types_json = ?, max_running = ?,
                    priority = ?, enabled = ?, revision = revision + 1, updated_at = ?
                WHERE id = ? AND tenant_id = ? AND revision = ?
                """,
                (
                    body.display_name,
                    body.model_id,
                    json.dumps(body.task_types, ensure_ascii=False),
                    body.max_running,
                    body.priority,
                    int(body.enabled),
                    now,
                    worker_id,
                    tenant_id,
                    body.expected_revision,
                ),
            )
        except (sqlite3.IntegrityError, UniqueViolation, ForeignKeyViolation) as exc:
            raise HTTPException(409, "Worker name already exists") from exc
        if cursor.rowcount != 1:
            raise HTTPException(409, "Worker revision conflict")
        write_security_audit(
            conn,
            actor_type=principal.kind,
            actor_id=principal.subject_id,
            tenant_id=tenant_id,
            action="worker.update",
            resource_type="worker",
            resource_id=worker_id,
            outcome="success",
            metadata={"model_id": body.model_id, "revision": body.expected_revision + 1},
        )
        return _worker_from_row(conn, get_tenant_worker_row(conn, worker_id, tenant_id=tenant_id))


def delete_tenant_worker(
    worker_id: str,
    *,
    expected_revision: int,
    principal: RequestPrincipal,
) -> None:
    tenant_id = _principal_tenant(principal)
    with get_conn() as conn:
        row = get_tenant_worker_row(conn, worker_id, tenant_id=tenant_id)
        try:
            cursor = conn.execute(
                "DELETE FROM tenant_workers WHERE id = ? AND tenant_id = ? AND revision = ?",
                (worker_id, tenant_id, expected_revision),
            )
        except (sqlite3.IntegrityError, UniqueViolation, ForeignKeyViolation) as exc:
            raise HTTPException(409, "Worker is referenced by a project or template") from exc
        if cursor.rowcount != 1:
            raise HTTPException(409, "Worker revision conflict")
        write_security_audit(
            conn,
            actor_type=principal.kind,
            actor_id=principal.subject_id,
            tenant_id=tenant_id,
            action="worker.delete",
            resource_type="worker",
            resource_id=worker_id,
            outcome="success",
            metadata={"model_id": row["model_id"], "revision": expected_revision},
        )


def validate_tenant_worker_selection(
    conn: Any,
    *,
    tenant_id: str,
    worker_ids: list[str],
) -> list[Any]:
    """在调用方事务内验证 Worker ownership 与当前 deterministic availability。"""
    rows: list[Any] = []
    for worker_id in worker_ids:
        row = get_tenant_worker_row(conn, worker_id, tenant_id=tenant_id)
        worker = _worker_from_row(conn, row)
        if not worker.available:
            raise HTTPException(422, f"Worker is unavailable: {worker.unavailable_reason}")
        rows.append(row)
    return rows


def resolve_project_worker_execution_binding(
    project_id: str,
    worker_id: str,
    *,
    task_type: str,
) -> TenantWorkerExecutionBinding:
    """从 project durable binding 解析一次性的内存执行配置。"""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT w.*, p.status AS project_status, b.tenant_worker_revision,
                   m.protocol, m.base_url, m.auth_header, m.custom_headers_json,
                   m.inference_endpoint, m.model_name, m.enabled AS model_enabled,
                   m.revision AS model_revision
            FROM projects p
            JOIN project_tenant_workers b
              ON b.project_id = p.id AND b.tenant_id = p.tenant_id
            JOIN tenant_workers w
              ON w.id = b.tenant_worker_id AND w.tenant_id = b.tenant_id
            JOIN tenant_models m
              ON m.id = w.model_id AND m.tenant_id = w.tenant_id
            WHERE p.id = ? AND w.id = ?
            """,
            (project_id, worker_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Project Worker binding not found")
        if row["engine"] != "pi":
            raise HTTPException(422, "Worker is unavailable: engine_unsupported")
        project_status = str(row["project_status"])
        if task_type == "report":
            if project_status not in {"completed", "stopped"}:
                raise HTTPException(409, "Project is not eligible for report generation")
        elif project_status != "active":
            raise HTTPException(409, "Project is not active")
        if not bool(row["enabled"]):
            raise HTTPException(409, "Worker is disabled")
        if int(row["revision"]) != int(row["tenant_worker_revision"]):
            raise HTTPException(409, "Worker binding revision is stale")
        task_types = json.loads(row["task_types_json"])
        if task_type == "report":
            # Report 是完成后的执行用途，复用 reason/bootstrap 能力而不扩张 Worker 能力模型。
            if not any(capability in task_types for capability in ("reason", "bootstrap")):
                raise HTTPException(422, "Worker does not support report generation")
        elif task_type not in task_types:
            raise HTTPException(422, "Worker does not support the task type")
        if row["protocol"] not in _INFERENCE_ENDPOINTS:
            raise HTTPException(422, "Worker is unavailable: model_protocol_unsupported")
        if not bool(row["model_enabled"]) or not str(row["model_name"] or "").strip():
            raise HTTPException(409, "Model is unavailable")
        try:
            inference_endpoint = resolve_model_endpoint(
                row["base_url"],
                row["inference_endpoint"],
                _INFERENCE_ENDPOINTS[row["protocol"]],
            )
        except ModelTransportError as exc:
            raise HTTPException(422, exc.detail) from exc
        api_key, credential = decrypt_active_credential(
            conn,
            tenant_id=row["tenant_id"],
            model_id=row["model_id"],
        )
        custom_headers = json.loads(row["custom_headers_json"] or "[]")
        return TenantWorkerExecutionBinding(
            worker_id=row["id"],
            display_name=row["display_name"],
            worker_revision=int(row["revision"]),
            model_id=row["model_id"],
            model_revision=int(row["model_revision"]),
            credential_version_id=str(credential["credential_version_id"]),
            credential_version=int(credential["version_number"]),
            credential_fingerprint=str(credential["credential_fingerprint"]),
            engine=row["engine"],
            task_types=task_types,
            max_running=int(row["max_running"]),
            priority=int(row["priority"]),
            model_name=row["model_name"],
            protocol=row["protocol"],
            base_url=row["base_url"],
            inference_endpoint=inference_endpoint,
            auth_header=row["auth_header"],
            custom_headers=custom_headers,
            api_key=api_key,
        )


def project_worker_bindings(conn: Any, project_id: str) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT w.*, b.tenant_worker_revision
        FROM project_tenant_workers b
        JOIN tenant_workers w ON w.id = b.tenant_worker_id AND w.tenant_id = b.tenant_id
        WHERE b.project_id = ?
        ORDER BY b.position
        """,
        (project_id,),
    ).fetchall()
    bindings = []
    for row in rows:
        worker = _worker_from_row(conn, row)
        reason = worker.unavailable_reason
        if reason is None and int(row["tenant_worker_revision"]) != int(row["revision"]):
            reason = "binding_stale"
        # 真实引擎和不可用原因必须跨过项目响应 Schema，不能让调度端猜默认值。
        bindings.append({
            "id": row["id"],
            "display_name": row["display_name"],
            "engine": row["engine"],
            "revision_at_bind": int(row["tenant_worker_revision"]),
            "task_types": json.loads(row["task_types_json"]),
            "max_running": int(row["max_running"]),
            "priority": int(row["priority"]),
            "enabled": bool(row["enabled"]),
            "currently_available": worker.available and reason is None,
            "unavailable_reason": reason,
        })
    return bindings


def _validate_model_binding(conn: Any, model_id: str, *, tenant_id: str) -> None:
    row = conn.execute(
        "SELECT protocol FROM tenant_models WHERE id = ? AND tenant_id = ?",
        (model_id, tenant_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Model not found")
    if row["protocol"] not in _INFERENCE_ENDPOINTS:
        raise HTTPException(422, "Worker is unavailable: model_protocol_unsupported")


def _worker_from_row(conn: Any, row: Any) -> TenantWorker:
    model = conn.execute(
        "SELECT * FROM tenant_models WHERE id = ? AND tenant_id = ?",
        (row["model_id"], row["tenant_id"]),
    ).fetchone()
    reason = None
    if row["engine"] != "pi":
        reason = "engine_unsupported"
    elif not bool(row["enabled"]):
        reason = "worker_disabled"
    elif model is None or not bool(model["enabled"]):
        reason = "model_disabled"
    elif model["protocol"] not in _INFERENCE_ENDPOINTS:
        reason = "model_protocol_unsupported"
    elif not str(model["model_name"] or "").strip():
        reason = "model_incomplete"
    elif not credential_status(conn, tenant_id=row["tenant_id"], model_id=row["model_id"])["credential_configured"]:
        reason = "credential_missing"
    try:
        task_types = json.loads(row["task_types_json"])
    except (TypeError, ValueError):
        task_types = []
    return TenantWorker(
        id=row["id"],
        display_name=row["display_name"],
        model_id=row["model_id"],
        model_display_name="" if model is None else model["display_name"],
        model_name="" if model is None else model["model_name"],
        engine=row["engine"],
        task_types=task_types,
        max_running=int(row["max_running"]),
        priority=int(row["priority"]),
        enabled=bool(row["enabled"]),
        available=reason is None,
        unavailable_reason=reason,
        executor_available=dispatcher_executor_available(conn),
        revision=int(row["revision"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _principal_tenant(principal: RequestPrincipal) -> str:
    if principal.kind != "user" or not principal.tenant_id:
        raise HTTPException(403, "User tenant is required")
    return principal.tenant_id


def _require_tenant_id(tenant_id: str | None) -> str:
    if not tenant_id:
        raise HTTPException(403, "User tenant is required")
    return tenant_id
