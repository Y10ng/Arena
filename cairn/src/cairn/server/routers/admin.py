"""超级管理员显式管理接口，避免普通业务路由隐式切换为跨 Tenant。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from cairn.server.auth import (
    DEFAULT_PROVISIONED_PASSWORD,
    SESSION_COOKIE,
    RequestPrincipal,
    create_user_with_personal_tenant,
    hash_password,
    require_admin_route_access,
    require_super_admin,
    sanitize_security_audit_metadata,
    utcnow,
    write_security_audit,
)
from cairn.server.db import get_conn
from cairn.server.models import (
    AdminMonitoringView,
    AdminProjectView,
    AdminSecurityAuditEventView,
    AdminTenantView,
    AdminUserCreateRequest,
    AdminUserPasswordResetRequest,
    AdminUserStatusRequest,
    AdminUserView,
    AdminVulnerabilityView,
)
from cairn.server.services.monitoring import SummaryProfile, build_monitoring_summary

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin_route_access)])


def _audit_admin_access(principal: RequestPrincipal, action: str) -> None:
    """记录全局管理读取，不持久化查询内容或响应数据。"""
    with get_conn() as conn:
        write_security_audit(
            conn,
            actor_type="user",
            actor_id=principal.subject_id,
            tenant_id=principal.tenant_id,
            action=action,
            resource_type="admin_read_model",
            resource_id=None,
            outcome="success",
        )


def _audit_user_create_failure(
    principal: RequestPrincipal,
    *,
    reason: str,
    login: str | None = None,
) -> None:
    """仅记录脱敏后的失败原因，禁止写入密码或完整请求体。"""
    metadata: dict[str, object] = {"reason": reason}
    if login is not None:
        metadata["login"] = login
    with get_conn() as conn:
        write_security_audit(
            conn,
            actor_type="user",
            actor_id=principal.subject_id,
            tenant_id=principal.tenant_id,
            action="admin.users.create",
            resource_type="user",
            resource_id=None,
            outcome="failure",
            metadata=metadata,
        )


def _next_updated_at(current: str) -> str:
    """在秒级时间戳下仍保证每次成功写入都产生新的并发版本。"""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        previous = datetime.fromisoformat(current.replace("Z", "+00:00"))
    except ValueError:
        previous = now
    return max(now, previous + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _admin_user_view(conn, user_id: str) -> AdminUserView | None:
    row = conn.execute(
        """
        SELECT u.id, u.login, u.role, u.disabled, u.must_change_password,
               u.created_at, u.updated_at,
               tm.tenant_id,
               COUNT(CASE
                   WHEN s.revoked_at IS NULL AND s.expires_at > ? THEN 1
                   ELSE NULL
               END) AS active_session_count
        FROM users u
        LEFT JOIN tenant_memberships tm ON tm.user_id = u.id AND tm.role = 'owner'
        LEFT JOIN auth_sessions s ON s.user_id = u.id
        WHERE u.id = ?
        GROUP BY u.id, u.login, u.role, u.disabled, u.must_change_password,
                 u.created_at, u.updated_at,
                 tm.tenant_id
        """,
        (utcnow(), user_id),
    ).fetchone()
    return AdminUserView(**dict(row)) if row is not None else None


def _write_user_lifecycle_audit(
    conn,
    principal: RequestPrincipal,
    *,
    action: str,
    user_id: str,
    tenant_id: str | None,
    outcome: str,
    reason: str | None = None,
) -> None:
    metadata = {"reason": reason} if reason is not None else None
    write_security_audit(
        conn,
        actor_type="user",
        actor_id=principal.subject_id,
        tenant_id=tenant_id or principal.tenant_id,
        action=action,
        resource_type="user",
        resource_id=user_id,
        outcome=outcome,
        metadata=metadata,
    )


def _deny_self_lifecycle(principal: RequestPrincipal, user_id: str, action: str) -> None:
    if user_id != principal.subject_id:
        return
    with get_conn() as conn:
        _write_user_lifecycle_audit(
            conn,
            principal,
            action=action,
            user_id=user_id,
            tenant_id=principal.tenant_id,
            outcome="failure",
            reason="self_operation",
        )
    raise HTTPException(409, "Self lifecycle operation is not allowed")


@router.post("/users", response_model=AdminUserView, status_code=201)
def create_admin_user(
    body: AdminUserCreateRequest,
    principal: RequestPrincipal = Depends(require_super_admin),
):
    normalized = body.login.strip().casefold()
    if not normalized or len(normalized) > 200:
        _audit_user_create_failure(principal, reason="invalid_input")
        raise HTTPException(422, "Invalid user input")
    try:
        user_id, tenant_id = create_user_with_personal_tenant(
            normalized,
            DEFAULT_PROVISIONED_PASSWORD,
            must_change_password=True,
            audit_actor=principal,
            audit_action="admin.users.create",
        )
    except ValueError as exc:
        if str(exc).startswith("User already exists:"):
            _audit_user_create_failure(principal, reason="duplicate", login=normalized)
            raise HTTPException(409, "User already exists") from None
        _audit_user_create_failure(principal, reason="invalid_input")
        raise HTTPException(422, "Invalid user input") from None

    with get_conn() as conn:
        view = _admin_user_view(conn, user_id)
    if view is None or view.tenant_id != tenant_id:
        raise HTTPException(500, "Created user unavailable")
    return view


@router.get("/users", response_model=list[AdminUserView])
def list_admin_users(
    principal: RequestPrincipal = Depends(require_super_admin),
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT u.id, u.login, u.role, u.disabled, u.must_change_password,
                   u.created_at, u.updated_at,
                   tm.tenant_id,
                   COUNT(CASE
                       WHEN s.revoked_at IS NULL AND s.expires_at > ? THEN 1
                       ELSE NULL
                   END) AS active_session_count
            FROM users u
            LEFT JOIN tenant_memberships tm ON tm.user_id = u.id AND tm.role = 'owner'
            LEFT JOIN auth_sessions s ON s.user_id = u.id
            GROUP BY u.id, u.login, u.role, u.disabled, u.must_change_password,
                     u.created_at, u.updated_at, tm.tenant_id
            ORDER BY u.created_at DESC, u.id DESC
            LIMIT ? OFFSET ?
            """,
            (utcnow(), limit, offset),
        ).fetchall()
    _audit_admin_access(principal, "admin.users.read")
    return [AdminUserView(**dict(row)) for row in rows]


@router.patch("/users/{user_id}/status", response_model=AdminUserView)
def update_admin_user_status(
    user_id: str,
    body: AdminUserStatusRequest,
    principal: RequestPrincipal = Depends(require_super_admin),
):
    action = "admin.users.status.update"
    _deny_self_lifecycle(principal, user_id, action)
    if not body.expected_updated_at:
        with get_conn() as conn:
            _write_user_lifecycle_audit(
                conn, principal, action=action, user_id=user_id,
                tenant_id=None, outcome="failure", reason="invalid_input",
            )
        raise HTTPException(422, "Invalid user status input")

    failure: tuple[int, str] | None = None
    view: AdminUserView | None = None
    with get_conn() as conn:
        current = _admin_user_view(conn, user_id)
        if current is None:
            failure = (404, "User not found")
            reason = "not_found"
        elif current.updated_at != body.expected_updated_at:
            failure = (409, "User update conflict")
            reason = "conflict"
        else:
            enabled_admins = None
            if body.disabled and current.role == "super_admin":
                enabled_admins = conn.execute(
                    "SELECT COUNT(*) AS count FROM users WHERE role = 'super_admin' AND disabled = 0"
                ).fetchone()["count"]
            if enabled_admins is not None and enabled_admins <= 1:
                failure = (409, "Last enabled super administrator cannot be disabled")
                reason = "last_enabled_super_admin"
            else:
                next_updated_at = _next_updated_at(current.updated_at)
                cursor = conn.execute(
                    "UPDATE users SET disabled = ?, updated_at = ? WHERE id = ? AND updated_at = ?",
                    (body.disabled, next_updated_at, user_id, body.expected_updated_at),
                )
                if cursor.rowcount != 1:
                    failure = (409, "User update conflict")
                    reason = "conflict"
                else:
                    if body.disabled:
                        conn.execute(
                            "UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                            (next_updated_at, user_id),
                        )
                    view = _admin_user_view(conn, user_id)
        _write_user_lifecycle_audit(
            conn,
            principal,
            action=action,
            user_id=user_id,
            tenant_id=current.tenant_id if current is not None else None,
            outcome="failure" if failure is not None else "success",
            reason=reason if failure is not None else None,
        )
    if failure is not None:
        raise HTTPException(*failure)
    return view


@router.post("/users/{user_id}/password-reset", response_model=AdminUserView)
def reset_admin_user_password(
    user_id: str,
    body: AdminUserPasswordResetRequest,
    response: Response,
    principal: RequestPrincipal = Depends(require_super_admin),
):
    action = "admin.users.password_reset"
    if not body.password.strip() or len(body.password) > 1024 or not body.expected_updated_at:
        with get_conn() as conn:
            _write_user_lifecycle_audit(
                conn, principal, action=action, user_id=user_id,
                tenant_id=None, outcome="failure", reason="invalid_input",
            )
        raise HTTPException(422, "Invalid password reset input")

    password_hash = hash_password(body.password)
    failure: tuple[int, str] | None = None
    view: AdminUserView | None = None
    with get_conn() as conn:
        current = _admin_user_view(conn, user_id)
        if current is None:
            failure = (404, "User not found")
            reason = "not_found"
        elif current.updated_at != body.expected_updated_at:
            failure = (409, "User update conflict")
            reason = "conflict"
        else:
            next_updated_at = _next_updated_at(current.updated_at)
            cursor = conn.execute(
                """
                UPDATE users
                SET password_hash = ?, must_change_password = 0, updated_at = ?
                WHERE id = ? AND updated_at = ?
                """,
                (password_hash, next_updated_at, user_id, body.expected_updated_at),
            )
            if cursor.rowcount != 1:
                failure = (409, "User update conflict")
                reason = "conflict"
            else:
                conn.execute(
                    "UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                    (next_updated_at, user_id),
                )
                view = _admin_user_view(conn, user_id)
        _write_user_lifecycle_audit(
            conn,
            principal,
            action=action,
            user_id=user_id,
            tenant_id=current.tenant_id if current is not None else None,
            outcome="failure" if failure is not None else "success",
            reason=reason if failure is not None else None,
        )
    if failure is not None:
        raise HTTPException(*failure)
    if user_id == principal.subject_id:
        response.delete_cookie(SESSION_COOKIE, path="/")
    return view


@router.post("/users/{user_id}/sessions/revoke", response_model=AdminUserView)
def revoke_admin_user_sessions(
    user_id: str,
    principal: RequestPrincipal = Depends(require_super_admin),
):
    action = "admin.users.sessions.revoke"
    _deny_self_lifecycle(principal, user_id, action)
    failure: tuple[int, str] | None = None
    view: AdminUserView | None = None
    with get_conn() as conn:
        current = _admin_user_view(conn, user_id)
        if current is None:
            failure = (404, "User not found")
            reason = "not_found"
        else:
            conn.execute(
                "UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (utcnow(), user_id),
            )
            view = _admin_user_view(conn, user_id)
        _write_user_lifecycle_audit(
            conn,
            principal,
            action=action,
            user_id=user_id,
            tenant_id=current.tenant_id if current is not None else None,
            outcome="failure" if failure is not None else "success",
            reason=reason if failure is not None else None,
        )
    if failure is not None:
        raise HTTPException(*failure)
    return view


@router.get("/tenants", response_model=list[AdminTenantView])
def list_admin_tenants(
    principal: RequestPrincipal = Depends(require_super_admin),
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT t.id, t.kind, t.name, t.created_at,
                   COUNT(DISTINCT tm.user_id) AS member_count,
                   COUNT(DISTINCT p.id) AS project_count
            FROM tenants t
            LEFT JOIN tenant_memberships tm ON tm.tenant_id = t.id
            LEFT JOIN projects p ON p.tenant_id = t.id
            GROUP BY t.id, t.kind, t.name, t.created_at
            ORDER BY t.created_at DESC, t.id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    _audit_admin_access(principal, "admin.tenants.read")
    return [AdminTenantView(**dict(row)) for row in rows]


@router.get("/projects", response_model=list[AdminProjectView])
def list_admin_projects(
    principal: RequestPrincipal = Depends(require_super_admin),
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, tenant_id, title, status, scenario_key, group_id, created_at
            FROM projects
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    _audit_admin_access(principal, "admin.projects.read")
    return [AdminProjectView(**dict(row)) for row in rows]


@router.get("/vulnerabilities", response_model=list[AdminVulnerabilityView])
def list_admin_vulnerabilities(
    principal: RequestPrincipal = Depends(require_super_admin),
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT v.id, v.project_id, p.tenant_id, p.title AS project_title,
                   v.title, v.severity, COALESCE(v.vuln_type, '') AS vuln_type,
                   COALESCE(v.target, '') AS target, v.confidence, v.status,
                   v.created_at, v.updated_at
            FROM vulnerabilities v
            JOIN projects p ON p.id = v.project_id
            ORDER BY v.created_at DESC, v.id DESC, v.project_id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    _audit_admin_access(principal, "admin.vulnerabilities.read")
    return [AdminVulnerabilityView(**dict(row)) for row in rows]


@router.get("/monitoring", response_model=AdminMonitoringView)
def get_admin_monitoring(
    long_running_seconds: Annotated[int, Query(ge=0)] = 1800,
    profile: Annotated[SummaryProfile, Query()] = "full",
    principal: RequestPrincipal = Depends(require_super_admin),
):
    with get_conn() as conn:
        summary = build_monitoring_summary(
            conn,
            long_running_seconds=long_running_seconds,
            profile=profile,
        )
    _audit_admin_access(principal, "admin.monitoring.read")
    return summary


@router.get("/audit", response_model=list[AdminSecurityAuditEventView])
def list_admin_security_audit(
    principal: RequestPrincipal = Depends(require_super_admin),
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, actor_type, actor_id, tenant_id, action, resource_type,
                   resource_id, outcome, metadata_json, created_at
            FROM security_audit_events
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    events: list[AdminSecurityAuditEventView] = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        # 读取时再次清洗，防止历史记录或手工导入绕过当前写入清洗器。
        events.append(
            AdminSecurityAuditEventView(
                id=row["id"],
                actor_type=row["actor_type"],
                actor_id=row["actor_id"],
                tenant_id=row["tenant_id"],
                action=row["action"],
                resource_type=row["resource_type"],
                resource_id=row["resource_id"],
                outcome=row["outcome"],
                metadata=sanitize_security_audit_metadata(metadata),
                created_at=row["created_at"],
            )
        )
    _audit_admin_access(principal, "admin.audit.read")
    return events
