"""浏览器会话、请求主体和安全审计的集中认证 Module。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import json
import math
import os
import re
import secrets
from typing import Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Cookie, Depends, Header, HTTPException, Request, Response

from cairn.server.db import database_config, get_conn
from cairn.server.migrations import LEGACY_TENANT_ID

SESSION_COOKIE = "cairn_session"
CSRF_HEADER = "X-Cairn-CSRF-Token"
DEFAULT_PROVISIONED_PASSWORD = "Arena@2026"
SESSION_TTL_ENV = "CAIRN_SESSION_TTL_SECONDS"
TRUSTED_ORIGINS_ENV = "CAIRN_TRUSTED_ORIGINS"
COOKIE_SECURE_ENV = "CAIRN_SESSION_COOKIE_SECURE"
MACHINE_TOKEN_MAX_LENGTH = 1024
_AUDIT_REDACTED = "[REDACTED]"
_AUDIT_TRUNCATED = "[TRUNCATED]"
_AUDIT_UNSUPPORTED = "[UNSUPPORTED]"
_AUDIT_MAX_DEPTH = 6
_AUDIT_MAX_ITEMS = 50
_AUDIT_MAX_STRING_LENGTH = 2048
_AUDIT_SENSITIVE_KEY_MARKERS = (
    "apikey",
    "auth",
    "authorization",
    "cookie",
    "credential",
    "passwd",
    "password",
    "privatekey",
    "secret",
    "session",
    "token",
)
_AUDIT_AUTH_PATTERN = re.compile(r"(?i)\b(?:bearer|basic)\s+[^\s,;]+")
_AUDIT_URL_PATTERN = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>\"']+")
_AUDIT_URL_SECRET_QUERY_PATTERN = re.compile(
    r"(?i)([?&](?:api[_-]?key|auth|credential|pass(?:word)?|secret|session|token)=)[^&#\s]*"
)

_password_hasher = PasswordHasher()


@dataclass(frozen=True)
class RequestPrincipal:
    kind: Literal["user", "machine", "integration"]
    subject_id: str
    tenant_id: str | None = None
    role: str | None = None
    must_change_password: bool = False

    @property
    def is_super_admin(self) -> bool:
        return self.kind == "user" and self.role == "super_admin"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hash_secret(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (InvalidHashError, VerifyMismatchError):
        return False


def create_user_with_personal_tenant(
    login: str,
    password: str,
    *,
    role: Literal["user", "super_admin"] = "user",
    must_change_password: bool = False,
    audit_actor: RequestPrincipal | None = None,
    audit_action: str = "user.create",
) -> tuple[str, str]:
    normalized = login.strip().casefold()
    if not normalized or not password:
        raise ValueError("login and password must not be blank")
    user_id = f"usr_{uuid4().hex}"
    tenant_id = f"tenant_{uuid4().hex}"
    now = utcnow()
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM users WHERE login = ?", (normalized,)).fetchone():
            raise ValueError(f"User already exists: {normalized}")
        conn.execute(
            """
            INSERT INTO users
                (id, login, password_hash, role, must_change_password, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                normalized,
                hash_password(password),
                role,
                int(must_change_password),
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO tenants (id, kind, name, created_at) VALUES (?, 'personal', ?, ?)",
            (tenant_id, normalized, now),
        )
        conn.execute(
            "INSERT INTO tenant_memberships (tenant_id, user_id, role, created_at) VALUES (?, ?, 'owner', ?)",
            (tenant_id, user_id, now),
        )
        write_security_audit(
            conn,
            actor_type=audit_actor.kind if audit_actor is not None else "system",
            actor_id=audit_actor.subject_id if audit_actor is not None else None,
            tenant_id=tenant_id,
            action=audit_action,
            resource_type="user",
            resource_id=user_id,
            outcome="success",
            metadata={"login": normalized} if audit_actor is not None else None,
        )
    return user_id, tenant_id


def claim_legacy_tenant(login: str) -> dict[str, object]:
    """将升级遗留资源原子归入已存在用户的个人 Tenant。"""
    normalized = login.strip().casefold()
    if not normalized:
        raise ValueError("login must not be blank")

    resource_tables = ("projects", "project_groups", "worker_templates")
    with get_conn() as conn:
        if database_config().backend == "postgresql":
            conn.execute("SELECT pg_advisory_xact_lock(?)", (0x434149524E4C4547,))
        # 无操作 UPDATE 同时校验 legacy 身份并锁定该行，阻止 claim 期间新增外键引用。
        locked_legacy = conn.execute(
            "UPDATE tenants SET id = id WHERE id = ? AND kind = 'legacy'",
            (LEGACY_TENANT_ID,),
        )
        if locked_legacy.rowcount != 1:
            raise ValueError("Legacy tenant is missing or is not marked legacy")

        memberships = conn.execute(
            """
            SELECT u.id AS user_id, u.role AS user_role, tm.tenant_id, tm.role AS membership_role,
                   t.kind AS tenant_kind
            FROM users u
            JOIN tenant_memberships tm ON tm.user_id = u.id
            JOIN tenants t ON t.id = tm.tenant_id
            WHERE u.login = ?
            """,
            (normalized,),
        ).fetchall()
        if not memberships:
            raise ValueError(f"User or personal tenant not found: {normalized}")
        if len(memberships) != 1:
            raise ValueError(f"User has ambiguous tenant memberships: {normalized}")
        target = memberships[0]
        if target["membership_role"] != "owner" or target["tenant_kind"] != "personal":
            raise ValueError(f"Target user does not own a personal tenant: {normalized}")
        target_tenant_id = str(target["tenant_id"])
        if target_tenant_id == LEGACY_TENANT_ID:
            raise ValueError("Legacy tenant cannot be used as the target personal tenant")

        counts: dict[str, int] = {}
        for table in resource_tables:
            unassigned = int(
                conn.execute(
                    f"SELECT COUNT(*) AS count FROM {table} WHERE tenant_id IS NULL"
                ).fetchone()["count"]
            )
            if unassigned:
                raise ValueError(f"Resource ownership is incomplete in {table}: {unassigned} unassigned rows")
            counts[table] = int(
                conn.execute(
                    f"SELECT COUNT(*) AS count FROM {table} WHERE tenant_id = ?",
                    (LEGACY_TENANT_ID,),
                ).fetchone()["count"]
            )

        for table in resource_tables:
            conn.execute(
                f"UPDATE {table} SET tenant_id = ? WHERE tenant_id = ?",
                (target_tenant_id, LEGACY_TENANT_ID),
            )

        remaining = {
            table: int(
                conn.execute(
                    f"SELECT COUNT(*) AS count FROM {table} WHERE tenant_id = ?",
                    (LEGACY_TENANT_ID,),
                ).fetchone()["count"]
            )
            for table in resource_tables
        }
        if any(remaining.values()):
            raise RuntimeError("Legacy tenant claim left resources in a partial state")

        write_security_audit(
            conn,
            actor_type="system",
            actor_id=None,
            tenant_id=target_tenant_id,
            action="tenant.legacy.claim",
            resource_type="tenant",
            resource_id=LEGACY_TENANT_ID,
            outcome="success",
            metadata={"counts": counts},
        )

    return {
        "login": normalized,
        "user_id": str(target["user_id"]),
        "tenant_id": target_tenant_id,
        "counts": counts,
    }


def login(response: Response, login_name: str, password: str) -> tuple[RequestPrincipal, str]:
    normalized = login_name.strip().casefold()
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT u.*, tm.tenant_id
            FROM users u
            JOIN tenant_memberships tm ON tm.user_id = u.id AND tm.role = 'owner'
            WHERE u.login = ?
            """,
            (normalized,),
        ).fetchone()
        if row is None or row["disabled"] or not verify_password(row["password_hash"], password):
            raise HTTPException(401, "Invalid login or password")
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        session_id = f"ses_{uuid4().hex}"
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=_session_ttl())
        conn.execute(
            """
            INSERT INTO auth_sessions
                (id, token_hash, csrf_token_hash, user_id, created_at, expires_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                hash_secret(token),
                hash_secret(csrf),
                row["id"],
                now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
        )
        principal = RequestPrincipal(
            "user",
            row["id"],
            row["tenant_id"],
            row["role"],
            bool(row["must_change_password"]),
        )
        write_security_audit(
            conn,
            actor_type="user",
            actor_id=row["id"],
            tenant_id=row["tenant_id"],
            action="auth.login",
            resource_type="session",
            resource_id=session_id,
            outcome="success",
        )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=_session_ttl(),
        path="/",
    )
    return principal, csrf


def resolve_session(token: str | None) -> tuple[RequestPrincipal, dict[str, object]] | None:
    if not token:
        return None
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT s.*, u.role, u.disabled, u.must_change_password, tm.tenant_id
            FROM auth_sessions s
            JOIN users u ON u.id = s.user_id
            JOIN tenant_memberships tm ON tm.user_id = u.id AND tm.role = 'owner'
            WHERE s.token_hash = ?
            """,
            (hash_secret(token),),
        ).fetchone()
        if row is None or row["revoked_at"] is not None or row["disabled"]:
            return None
        expires = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
        if expires <= datetime.now(timezone.utc):
            return None
        conn.execute("UPDATE auth_sessions SET last_seen_at = ? WHERE id = ?", (utcnow(), row["id"]))
        return (
            RequestPrincipal(
                "user",
                row["user_id"],
                row["tenant_id"],
                row["role"],
                bool(row["must_change_password"]),
            ),
            dict(row),
        )


def rotate_session_csrf(request: Request, token: str | None) -> tuple[RequestPrincipal, str]:
    """仅为可信 Origin 中的有效浏览器会话签发新的 tab CSRF token。"""
    resolved = resolve_session(token)
    if resolved is None:
        raise HTTPException(401, "Authentication required")
    if not _request_origin_is_trusted(request):
        raise HTTPException(403, "Untrusted request origin")

    principal, session = resolved
    csrf = secrets.token_urlsafe(24)
    with get_conn() as conn:
        conn.execute(
            "UPDATE auth_sessions SET csrf_token_hash = ? WHERE id = ? AND revoked_at IS NULL",
            (hash_secret(csrf), session["id"]),
        )
        write_security_audit(
            conn,
            actor_type="user",
            actor_id=principal.subject_id,
            tenant_id=principal.tenant_id,
            action="auth.csrf.rotate",
            resource_type="session",
            resource_id=str(session["id"]),
            outcome="success",
        )
    return principal, csrf


def change_user_password(
    token: str | None,
    current_password: str,
    new_password: str,
) -> RequestPrincipal:
    """修改当前用户密码，保留本会话并撤销该用户的其他会话。"""
    resolved = resolve_session(token)
    if resolved is None:
        raise HTTPException(401, "Authentication required")
    principal, session = resolved

    failure: tuple[int, str] | None = None
    failure_reason: str | None = None
    updated_principal: RequestPrincipal | None = None
    with get_conn() as conn:
        user = conn.execute(
            "SELECT password_hash FROM users WHERE id = ?",
            (principal.subject_id,),
        ).fetchone()
        if user is None or not verify_password(str(user["password_hash"]), current_password):
            failure = (401, "Current password is incorrect")
            failure_reason = "invalid_current_password"
        elif not new_password.strip() or len(new_password) > 1024:
            failure = (422, "Invalid new password")
            failure_reason = "invalid_new_password"
        elif new_password == DEFAULT_PROVISIONED_PASSWORD:
            failure = (422, "New password must not be the provisioned password")
            failure_reason = "provisioned_password_reuse"
        elif verify_password(str(user["password_hash"]), new_password):
            failure = (422, "New password must differ from current password")
            failure_reason = "current_password_reuse"
        else:
            now = utcnow()
            conn.execute(
                """
                UPDATE users
                SET password_hash = ?, must_change_password = 0, updated_at = ?
                WHERE id = ?
                """,
                (hash_password(new_password), now, principal.subject_id),
            )
            conn.execute(
                """
                UPDATE auth_sessions
                SET revoked_at = ?
                WHERE user_id = ? AND id <> ? AND revoked_at IS NULL
                """,
                (now, principal.subject_id, session["id"]),
            )
            updated_principal = RequestPrincipal(
                principal.kind,
                principal.subject_id,
                principal.tenant_id,
                principal.role,
                False,
            )

        write_security_audit(
            conn,
            actor_type="user",
            actor_id=principal.subject_id,
            tenant_id=principal.tenant_id,
            action="auth.password.change",
            resource_type="user",
            resource_id=principal.subject_id,
            outcome="failure" if failure is not None else "success",
            metadata={"reason": failure_reason} if failure_reason is not None else None,
        )

    if failure is not None:
        raise HTTPException(*failure)
    assert updated_principal is not None
    return updated_principal


def _machine_credential(token: str | None) -> str:
    """生成或校验 machine token，确保签发值与认证边界使用完全一致。"""
    if token is None:
        return secrets.token_urlsafe(32)
    if not token or token != token.strip():
        raise ValueError("token must not be blank or contain surrounding whitespace")
    if len(token) > MACHINE_TOKEN_MAX_LENGTH:
        raise ValueError(f"token must not exceed {MACHINE_TOKEN_MAX_LENGTH} characters")
    return token


def create_machine_principal(name: str, token: str | None = None) -> tuple[str, str]:
    """创建 Dispatcher 主体，仅返回一次明文凭据并在数据库中保存摘要。"""
    normalized = name.strip()
    if not normalized:
        raise ValueError("name must not be blank")
    credential = _machine_credential(token)
    principal_id = f"mch_{uuid4().hex}"
    now = utcnow()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO machine_principals (id, name, token_hash, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (principal_id, normalized, hash_secret(credential), now),
        )
        write_security_audit(
            conn,
            actor_type="system",
            actor_id=None,
            tenant_id=None,
            action="machine.create",
            resource_type="machine_principal",
            resource_id=principal_id,
            outcome="success",
        )
    return principal_id, credential


def ensure_local_runtime_principals(
    *,
    admin_login: str,
    admin_password: str,
    dispatcher_name: str,
    dispatcher_token: str,
) -> dict[str, object]:
    """幂等初始化本机首个管理员和固定 Dispatcher 主体，不轮换既有凭据。"""
    normalized_login = admin_login.strip().casefold()
    normalized_dispatcher = dispatcher_name.strip()
    if not normalized_login or not admin_password:
        raise ValueError("admin login and password must not be blank")
    credential = _machine_credential(dispatcher_token)
    if not normalized_dispatcher:
        raise ValueError("dispatcher name must not be blank")

    with get_conn() as conn:
        users = conn.execute(
            "SELECT id, login, role FROM users ORDER BY created_at, id"
        ).fetchall()
        admin_created = False
        if users:
            super_admins = [row for row in users if row["role"] == "super_admin"]
            if not super_admins:
                raise ValueError("Database has users but no super administrator")
            admin = next(
                (row for row in super_admins if row["login"] == normalized_login),
                super_admins[0],
            )
            admin_user_id = str(admin["id"])
            normalized_login = str(admin["login"])
            membership = conn.execute(
                "SELECT tenant_id FROM tenant_memberships WHERE user_id = ? AND role = 'owner'",
                (admin_user_id,),
            ).fetchone()
            if membership is None:
                raise ValueError("Super administrator personal tenant is missing")
            admin_tenant_id = str(membership["tenant_id"])

    if not users:
        admin_user_id, admin_tenant_id = create_user_with_personal_tenant(
            normalized_login,
            admin_password,
            role="super_admin",
            audit_action="runtime.bootstrap.admin",
        )
        admin_created = True

    with get_conn() as conn:
        machines = conn.execute(
            "SELECT id, token_hash, enabled FROM machine_principals WHERE name = ? ORDER BY created_at, id",
            (normalized_dispatcher,),
        ).fetchall()
    if len(machines) > 1:
        raise ValueError(f"Dispatcher principal name is ambiguous: {normalized_dispatcher}")
    dispatcher_created = False
    if not machines:
        dispatcher_id, _ = create_machine_principal(normalized_dispatcher, credential)
        dispatcher_created = True
    else:
        machine = machines[0]
        if not bool(machine["enabled"]):
            raise ValueError("Dispatcher principal is disabled")
        if not hmac.compare_digest(str(machine["token_hash"]), hash_secret(credential)):
            raise ValueError("Dispatcher credential does not match the registered principal")
        dispatcher_id = str(machine["id"])

    return {
        "admin_created": admin_created,
        "admin_user_id": admin_user_id,
        "admin_tenant_id": admin_tenant_id,
        "admin_login": normalized_login,
        "dispatcher_created": dispatcher_created,
        "dispatcher_principal_id": dispatcher_id,
    }


def rotate_machine_principal(principal_id: str, token: str | None = None) -> str:
    """原子替换 Dispatcher bearer 摘要，使旧凭据立即失效。"""
    credential = _machine_credential(token)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM machine_principals WHERE id = ?",
            (principal_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Machine principal not found: {principal_id}")
        conn.execute(
            "UPDATE machine_principals SET token_hash = ?, enabled = 1 WHERE id = ?",
            (hash_secret(credential), principal_id),
        )
        write_security_audit(
            conn,
            actor_type="system",
            actor_id=None,
            tenant_id=None,
            action="machine.rotate",
            resource_type="machine_principal",
            resource_id=principal_id,
            outcome="success",
        )
    return credential


def resolve_machine_token(token: str | None) -> RequestPrincipal | None:
    if not token or len(token) > MACHINE_TOKEN_MAX_LENGTH:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM machine_principals WHERE token_hash = ? AND enabled = 1",
            (hash_secret(token),),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE machine_principals SET last_seen_at = ? WHERE id = ?",
            (utcnow(), row["id"]),
        )
        return RequestPrincipal("machine", row["id"])


def require_verification_caller(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    cairn_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_token: str | None = Header(default=None, alias=CSRF_HEADER),
) -> RequestPrincipal:
    """集成 Bearer 仅接入复测窄入口；失败不回退 Cookie，也不接受 machine。"""
    if authorization:
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.casefold() != "bearer" or not token.strip() or len(token) > MACHINE_TOKEN_MAX_LENGTH:
            raise HTTPException(401, "复测集成身份认证失败")
        with get_conn() as conn:
            row = conn.execute(
                "SELECT id, tenant_id FROM verification_integrations WHERE token_hash = ? AND enabled = 1",
                (hash_secret(token.strip()),),
            ).fetchone()
            if row is None:
                raise HTTPException(401, "复测集成凭据无效或已停用")
            conn.execute("UPDATE verification_integrations SET last_seen_at = ? WHERE id = ?", (utcnow(), row["id"]))
            return RequestPrincipal("integration", row["id"], tenant_id=row["tenant_id"])
    return protect_cookie_request(request, cairn_session, csrf_token)


def require_dispatcher(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> RequestPrincipal:
    scheme, separator, token = (authorization or "").partition(" ")
    if not separator or scheme.casefold() != "bearer":
        raise HTTPException(401, "Machine authentication required")
    principal = resolve_machine_token(token.strip())
    if principal is None:
        raise HTTPException(401, "Invalid machine credential")
    return principal


def require_dispatcher_protocol(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    cairn_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_token: str | None = Header(default=None, alias=CSRF_HEADER),
) -> RequestPrincipal:
    """允许 machine bearer 或原有浏览器会话进入 Dispatcher 数据协议路由。"""
    if authorization:
        return require_dispatcher(authorization)
    return protect_cookie_request(request, cairn_session, csrf_token)


def require_dispatcher_or_super_admin(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    cairn_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_token: str | None = Header(default=None, alias=CSRF_HEADER),
) -> RequestPrincipal:
    """只允许 Dispatcher machine 或超级管理员读取共享运行配置。"""
    if authorization:
        return require_dispatcher(authorization)
    principal = protect_cookie_request(request, cairn_session, csrf_token)
    return require_super_admin(principal)


def _enforce_password_change_completed(principal: RequestPrincipal) -> RequestPrincipal:
    if principal.kind == "user" and principal.must_change_password:
        raise HTTPException(403, "Password change required")
    return principal


def require_user(cairn_session: str | None = Cookie(default=None, alias=SESSION_COOKIE)) -> RequestPrincipal:
    resolved = resolve_session(cairn_session)
    if resolved is None:
        raise HTTPException(401, "Authentication required")
    return _enforce_password_change_completed(resolved[0])


def protect_auth_cookie_request(
    request: Request,
    cairn_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_token: str | None = Header(default=None, alias=CSRF_HEADER),
) -> RequestPrincipal:
    """校验认证生命周期路由的会话与 CSRF，但允许待改密用户进入。"""
    resolved = resolve_session(cairn_session)
    if resolved is None:
        raise HTTPException(401, "Authentication required")
    principal, session = resolved
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if not _request_origin_is_trusted(request):
            raise HTTPException(403, "Untrusted request origin")
        if not csrf_token or not hmac.compare_digest(
            hash_secret(csrf_token),
            str(session["csrf_token_hash"]),
        ):
            raise HTTPException(403, "Invalid CSRF token")
    return principal


def protect_cookie_request(
    request: Request,
    cairn_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_token: str | None = Header(default=None, alias=CSRF_HEADER),
) -> RequestPrincipal:
    principal = protect_auth_cookie_request(request, cairn_session, csrf_token)
    return _enforce_password_change_completed(principal)


def require_super_admin(
    principal: RequestPrincipal = Depends(protect_cookie_request),
) -> RequestPrincipal:
    """仅允许经过浏览器会话校验的超级管理员访问全局管理路由。"""
    if not principal.is_super_admin:
        raise HTTPException(403, "Super administrator required")
    return principal


def require_admin_route_access(
    request: Request,
    principal: RequestPrincipal = Depends(protect_cookie_request),
) -> RequestPrincipal:
    """在 /admin Router 边界审计可归因的普通用户越权尝试。"""
    if not principal.is_super_admin:
        with get_conn() as conn:
            write_security_audit(
                conn,
                actor_type="user",
                actor_id=principal.subject_id,
                tenant_id=principal.tenant_id,
                action="admin.access.denied",
                resource_type="route",
                resource_id=request.url.path,
                outcome="denied",
                metadata={"method": request.method},
            )
        raise HTTPException(403, "Super administrator required")
    return principal


def require_tenant_project(
    project_id: str,
    request: Request,
    principal: RequestPrincipal = Depends(require_dispatcher_protocol),
) -> RequestPrincipal:
    """普通浏览器按 Tenant 隔离；Dispatcher 与超级管理员可访问全部项目。"""
    with get_conn() as conn:
        if principal.kind == "machine" or principal.is_super_admin:
            row = conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone()
        elif principal.tenant_id is None:
            row = None
        else:
            row = conn.execute(
                "SELECT 1 FROM projects WHERE id = ? AND tenant_id = ?",
                (project_id, principal.tenant_id),
            ).fetchone()
        if row is not None and principal.kind != "machine" and request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            from .services.project_records import require_unmanaged_project
            require_unmanaged_project(conn, project_id)
    if row is None:
        # 对具体资源统一返回 404，避免泄漏其他 Tenant 的资源是否存在。
        raise HTTPException(404, "Project not found")
    return principal


def require_user_tenant_project(
    project_id: str,
    request: Request,
    principal: RequestPrincipal = Depends(protect_cookie_request),
) -> RequestPrincipal:
    """项目管理仅接受浏览器用户；超级管理员可管理全部 Tenant 项目。"""
    if principal.kind != "user":
        raise HTTPException(403, "User authentication required")
    with get_conn() as conn:
        if principal.is_super_admin:
            row = conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone()
        elif principal.tenant_id is None:
            row = None
        else:
            row = conn.execute(
                "SELECT 1 FROM projects WHERE id = ? AND tenant_id = ?",
                (project_id, principal.tenant_id),
            ).fetchone()
        if row is not None and request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            from .services.project_records import require_unmanaged_project
            require_unmanaged_project(conn, project_id)
    if row is None:
        raise HTTPException(404, "Project not found")
    return principal


def logout(response: Response, token: str | None) -> None:
    if token:
        with get_conn() as conn:
            conn.execute(
                "UPDATE auth_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                (utcnow(), hash_secret(token)),
            )
    response.delete_cookie(SESSION_COOKIE, path="/")


def _is_sensitive_audit_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.casefold())
    return any(marker in normalized for marker in _AUDIT_SENSITIVE_KEY_MARKERS)


def _redact_url_userinfo(match: re.Match[str]) -> str:
    """使用标准 URL 解析重建 authority，完整移除含 @ 的 userinfo。"""
    candidate = match.group(0)
    try:
        parsed = urlsplit(candidate)
        if parsed.username is None and parsed.password is None:
            return candidate
        hostname = parsed.hostname
        if not hostname:
            return candidate
        host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit((parsed.scheme, f"{_AUDIT_REDACTED}@{host}{port}", parsed.path, parsed.query, parsed.fragment))
    except (ValueError, UnicodeError):
        # 无法可靠解析时宁可替换整个 URL，避免错误边界泄漏凭据后缀。
        return _AUDIT_REDACTED


def _sanitize_audit_text(value: str) -> str:
    """移除字符串中的认证头和 URL userinfo，并限制持久化长度。"""
    safe = _AUDIT_AUTH_PATTERN.sub(
        lambda match: match.group(0).split(None, 1)[0] + " " + _AUDIT_REDACTED,
        value,
    )
    safe = _AUDIT_URL_PATTERN.sub(_redact_url_userinfo, safe)
    safe = _AUDIT_URL_SECRET_QUERY_PATTERN.sub(r"\1[REDACTED]", safe)
    if len(safe) > _AUDIT_MAX_STRING_LENGTH:
        return safe[:_AUDIT_MAX_STRING_LENGTH] + _AUDIT_TRUNCATED
    return safe


def _sanitize_security_audit_metadata(metadata: dict[str, object]) -> dict[str, object]:
    """复制并递归清洗审计元数据，避免秘密或无界对象进入持久层。"""
    active_containers: set[int] = set()

    def sanitize(value: object, *, depth: int, sensitive: bool = False) -> object:
        if sensitive:
            return _AUDIT_REDACTED
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else _AUDIT_UNSUPPORTED
        if isinstance(value, str):
            return _sanitize_audit_text(value)
        if depth >= _AUDIT_MAX_DEPTH:
            return _AUDIT_TRUNCATED
        if not isinstance(value, (dict, list)):
            return _AUDIT_UNSUPPORTED

        identity = id(value)
        if identity in active_containers:
            return _AUDIT_UNSUPPORTED
        active_containers.add(identity)
        try:
            if isinstance(value, list):
                safe_items = [sanitize(item, depth=depth + 1) for item in value[:_AUDIT_MAX_ITEMS]]
                if len(value) > _AUDIT_MAX_ITEMS:
                    safe_items.append(_AUDIT_TRUNCATED)
                return safe_items

            safe_mapping: dict[str, object] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= _AUDIT_MAX_ITEMS:
                    safe_mapping["_truncated"] = _AUDIT_TRUNCATED
                    break
                safe_key = _sanitize_audit_text(key) if isinstance(key, str) else f"_unsupported_key_{index}"
                safe_mapping[safe_key] = sanitize(
                    item,
                    depth=depth + 1,
                    sensitive=isinstance(key, str) and _is_sensitive_audit_key(key),
                )
            return safe_mapping
        finally:
            active_containers.remove(identity)

    return sanitize(metadata, depth=0)  # type: ignore[return-value]


def sanitize_security_audit_metadata(metadata: dict[str, object]) -> dict[str, object]:
    """在读取边界再次清洗历史审计元数据，兼容清洗器上线前的数据。"""
    return _sanitize_security_audit_metadata(metadata)


def write_security_audit(
    conn,
    *,
    actor_type: str,
    actor_id: str | None,
    tenant_id: str | None,
    action: str,
    resource_type: str | None,
    resource_id: str | None,
    outcome: str,
    metadata: dict[str, object] | None = None,
) -> None:
    safe_metadata = _sanitize_security_audit_metadata(metadata or {})
    conn.execute(
        """
        INSERT INTO security_audit_events
            (id, actor_type, actor_id, tenant_id, action, resource_type, resource_id,
             outcome, metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"sae_{uuid4().hex}", actor_type, actor_id, tenant_id, action,
            resource_type, resource_id, outcome,
            json.dumps(safe_metadata, ensure_ascii=False, sort_keys=True), utcnow(),
        ),
    )


def _session_ttl() -> int:
    return max(300, int(os.environ.get(SESSION_TTL_ENV, "43200")))


def _trusted_origins() -> set[str]:
    raw = os.environ.get(TRUSTED_ORIGINS_ENV, "http://127.0.0.1:8000,http://localhost:8000")
    return {item.strip().rstrip("/") for item in raw.split(",") if item.strip()}


def _request_origin_is_trusted(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return False
    normalized = origin.rstrip("/")
    if normalized in _trusted_origins():
        return True

    # 直接同源部署可由浏览器 Origin 与实际 Host 自证；反向代理场景继续显式配置可信 Origin。
    try:
        parsed = urlsplit(normalized)
    except ValueError:
        return False
    request_host = request.headers.get("host", "").strip()
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.netloc
        and parsed.netloc == request_host
    )


def _cookie_secure() -> bool:
    return os.environ.get(COOKIE_SECURE_ENV, "0").strip().lower() in {"1", "true", "yes", "on"}
