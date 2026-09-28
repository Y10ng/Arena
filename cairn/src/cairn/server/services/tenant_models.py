"""Tenant Model CRUD、原子 credential 保存与安全响应投影。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from fastapi import HTTPException
from psycopg.errors import UniqueViolation

from cairn.server.auth import RequestPrincipal, write_security_audit
from cairn.server.db import get_conn
from cairn.server.schemas.models import (
    CreateTenantModelRequest,
    TenantModel,
    TenantModelConfig,
    UpdateTenantModelRequest,
)

from .model_credentials import (
    credential_status,
    delete_credential,
    set_or_rotate_credential,
    validate_plaintext_credential,
)
from .model_transport import ModelTransportError
from .time_utils import utcnow


def normalize_model_base_url(value: str) -> str:
    text = value.strip().rstrip("/")
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(422, "Model Base URL is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(422, "Model Base URL must use http or https and include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise HTTPException(422, "Model Base URL must not contain user information")
    if parsed.query or parsed.fragment:
        raise HTTPException(422, "Model Base URL must not contain query or fragment")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))


def get_tenant_model_row(conn: Any, model_id: str, *, tenant_id: str):
    row = conn.execute(
        "SELECT * FROM tenant_models WHERE id = ? AND tenant_id = ?",
        (model_id, tenant_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Model not found")
    return row


def list_tenant_models(*, tenant_id: str) -> list[TenantModel]:
    _require_tenant_id(tenant_id)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tenant_models WHERE tenant_id = ? ORDER BY created_at, id",
            (tenant_id,),
        ).fetchall()
        return [_model_from_row(conn, row) for row in rows]


def get_tenant_model(model_id: str, *, tenant_id: str) -> TenantModel:
    _require_tenant_id(tenant_id)
    with get_conn() as conn:
        return _model_from_row(conn, get_tenant_model_row(conn, model_id, tenant_id=tenant_id))


def create_tenant_model(
    body: CreateTenantModelRequest,
    *,
    principal: RequestPrincipal,
) -> TenantModel:
    tenant_id = _principal_tenant(principal)
    base_url = _validate_config(body)
    plaintext = validate_plaintext_credential(body.api_key) if body.api_key is not None else None
    now = utcnow()
    model_id = f"mdl_{uuid4().hex}"
    with get_conn() as conn:
        _ensure_display_name_available(conn, tenant_id=tenant_id, display_name=body.display_name)
        try:
            conn.execute(
                """
                INSERT INTO tenant_models
                    (id, tenant_id, display_name, protocol, base_url, auth_header,
                     custom_headers_json, models_endpoint, inference_endpoint, model_name,
                     enabled, revision, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    model_id,
                    tenant_id,
                    body.display_name,
                    body.protocol,
                    base_url,
                    body.auth_header,
                    _headers_json(body),
                    body.models_endpoint,
                    body.inference_endpoint,
                    body.model_name,
                    int(body.enabled),
                    principal.subject_id,
                    now,
                    now,
                ),
            )
        except (sqlite3.IntegrityError, UniqueViolation) as exc:
            raise HTTPException(409, "Model name already exists") from exc
        credential = None
        if plaintext is not None:
            credential = set_or_rotate_credential(
                conn,
                tenant_id=tenant_id,
                model_id=model_id,
                plaintext=plaintext,
            )
        write_security_audit(
            conn,
            actor_type=principal.kind,
            actor_id=principal.subject_id,
            tenant_id=tenant_id,
            action="model.create",
            resource_type="model",
            resource_id=model_id,
            outcome="success",
            metadata=_audit_metadata(body, revision=1, credential=credential),
        )
        return _model_from_row(conn, get_tenant_model_row(conn, model_id, tenant_id=tenant_id))


def update_tenant_model(
    model_id: str,
    body: UpdateTenantModelRequest,
    *,
    principal: RequestPrincipal,
) -> TenantModel:
    tenant_id = _principal_tenant(principal)
    base_url = _validate_config(body)
    plaintext = validate_plaintext_credential(body.api_key) if body.api_key is not None else None
    now = utcnow()
    with get_conn() as conn:
        get_tenant_model_row(conn, model_id, tenant_id=tenant_id)
        _ensure_display_name_available(
            conn,
            tenant_id=tenant_id,
            display_name=body.display_name,
            exclude_model_id=model_id,
        )
        try:
            cursor = conn.execute(
                """
                UPDATE tenant_models
                SET display_name = ?, protocol = ?, base_url = ?, auth_header = ?,
                    custom_headers_json = ?, models_endpoint = ?, inference_endpoint = ?,
                    model_name = ?, enabled = ?, revision = revision + 1, updated_at = ?
                WHERE id = ? AND tenant_id = ? AND revision = ?
                """,
                (
                    body.display_name,
                    body.protocol,
                    base_url,
                    body.auth_header,
                    _headers_json(body),
                    body.models_endpoint,
                    body.inference_endpoint,
                    body.model_name,
                    int(body.enabled),
                    now,
                    model_id,
                    tenant_id,
                    body.expected_revision,
                ),
            )
        except (sqlite3.IntegrityError, UniqueViolation) as exc:
            raise HTTPException(409, "Model name already exists") from exc
        if cursor.rowcount != 1:
            raise HTTPException(409, "Model revision conflict")
        credential = None
        if plaintext is not None:
            credential = set_or_rotate_credential(
                conn,
                tenant_id=tenant_id,
                model_id=model_id,
                plaintext=plaintext,
            )
        revision = body.expected_revision + 1
        write_security_audit(
            conn,
            actor_type=principal.kind,
            actor_id=principal.subject_id,
            tenant_id=tenant_id,
            action="model.update",
            resource_type="model",
            resource_id=model_id,
            outcome="success",
            metadata=_audit_metadata(body, revision=revision, credential=credential),
        )
        return _model_from_row(conn, get_tenant_model_row(conn, model_id, tenant_id=tenant_id))


def delete_tenant_model_credential(
    model_id: str,
    *,
    expected_revision: int,
    principal: RequestPrincipal,
) -> TenantModel:
    tenant_id = _principal_tenant(principal)
    now = utcnow()
    with get_conn() as conn:
        get_tenant_model_row(conn, model_id, tenant_id=tenant_id)
        cursor = conn.execute(
            """
            UPDATE tenant_models
            SET revision = revision + 1, updated_at = ?
            WHERE id = ? AND tenant_id = ? AND revision = ?
            """,
            (now, model_id, tenant_id, expected_revision),
        )
        if cursor.rowcount != 1:
            raise HTTPException(409, "Model revision conflict")
        deleted = delete_credential(conn, tenant_id=tenant_id, model_id=model_id)
        write_security_audit(
            conn,
            actor_type=principal.kind,
            actor_id=principal.subject_id,
            tenant_id=tenant_id,
            action="model.credential.delete",
            resource_type="model",
            resource_id=model_id,
            outcome="success",
            metadata={"deleted": deleted, "revision": expected_revision + 1},
        )
        return _model_from_row(conn, get_tenant_model_row(conn, model_id, tenant_id=tenant_id))


def delete_tenant_model(
    model_id: str,
    *,
    expected_revision: int,
    principal: RequestPrincipal,
) -> None:
    tenant_id = _principal_tenant(principal)
    with get_conn() as conn:
        row = get_tenant_model_row(conn, model_id, tenant_id=tenant_id)
        cursor = conn.execute(
            "DELETE FROM tenant_models WHERE id = ? AND tenant_id = ? AND revision = ?",
            (model_id, tenant_id, expected_revision),
        )
        if cursor.rowcount != 1:
            raise HTTPException(409, "Model revision conflict")
        write_security_audit(
            conn,
            actor_type=principal.kind,
            actor_id=principal.subject_id,
            tenant_id=tenant_id,
            action="model.delete",
            resource_type="model",
            resource_id=model_id,
            outcome="success",
            metadata={"protocol": row["protocol"], "revision": expected_revision},
        )


def model_from_connection(conn: Any, model_id: str, *, tenant_id: str) -> TenantModel:
    return _model_from_row(conn, get_tenant_model_row(conn, model_id, tenant_id=tenant_id))


def _validate_config(body: TenantModelConfig) -> str:
    # 局部导入避免草稿测试服务与持久化服务形成模块初始化环。
    from .model_operations import validate_custom_headers, validate_model_endpoints

    base_url = normalize_model_base_url(body.base_url)
    validate_custom_headers(body)
    try:
        validate_model_endpoints(body, base_url=base_url)
    except ModelTransportError as exc:
        raise HTTPException(422, exc.detail) from exc
    return base_url


def _headers_json(body: TenantModelConfig) -> str:
    return json.dumps(
        [header.model_dump() for header in body.custom_headers],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _audit_metadata(
    body: TenantModelConfig,
    *,
    revision: int,
    credential: dict[str, Any] | None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "protocol": body.protocol,
        "auth_header": body.auth_header,
        "revision": revision,
        "custom_header_count": len(body.custom_headers),
        "models_endpoint_override": body.models_endpoint is not None,
        "inference_endpoint_override": body.inference_endpoint is not None,
    }
    if credential is not None:
        metadata["credential_version"] = credential["version_number"]
    return metadata


def _model_from_row(conn: Any, row: Any) -> TenantModel:
    status = credential_status(conn, tenant_id=row["tenant_id"], model_id=row["id"])
    custom_headers = json.loads(row["custom_headers_json"])
    return TenantModel(
        id=row["id"],
        display_name=row["display_name"],
        protocol=row["protocol"],
        base_url=row["base_url"],
        auth_header=row["auth_header"],
        custom_headers=custom_headers,
        models_endpoint=row["models_endpoint"],
        inference_endpoint=row["inference_endpoint"],
        model_name=row["model_name"],
        enabled=bool(row["enabled"]),
        revision=int(row["revision"]),
        **status,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _ensure_display_name_available(
    conn: Any,
    *,
    tenant_id: str,
    display_name: str,
    exclude_model_id: str | None = None,
) -> None:
    if exclude_model_id is None:
        row = conn.execute(
            "SELECT id FROM tenant_models WHERE tenant_id = ? AND display_name = ?",
            (tenant_id, display_name),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT id FROM tenant_models
            WHERE tenant_id = ? AND display_name = ? AND id != ?
            """,
            (tenant_id, display_name, exclude_model_id),
        ).fetchone()
    if row is not None:
        raise HTTPException(409, "Model name already exists")


def _principal_tenant(principal: RequestPrincipal) -> str:
    if principal.kind != "user" or not principal.tenant_id:
        raise HTTPException(403, "User tenant is required")
    return principal.tenant_id


def _require_tenant_id(tenant_id: str) -> str:
    if not tenant_id:
        raise ValueError("tenant_id is required")
    return tenant_id
