"""项目出口的唯一存储与私有消费边界；认证不进入普通 JSON 投影。"""

from __future__ import annotations

import base64
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException

from cairn.server.auth import RequestPrincipal
from cairn.server.db import get_conn
from cairn.server.schemas.test_egress import TestEgressInput, TestEgressView
from .model_credentials import CredentialConfigurationError, _load_active_key, _load_key


def _aad(tenant_id: str, owner_kind: str, owner_id: str) -> bytes:
    return json.dumps(["cairn:test-egress:v1", tenant_id, owner_kind, owner_id], separators=(",", ":")).encode()


def _encrypt(auth: dict, *, tenant_id: str, owner_kind: str, owner_id: str) -> str:
    try:
        version, key = _load_active_key()
    except CredentialConfigurationError:
        raise HTTPException(503, "test_egress_key_unavailable") from None
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, json.dumps(auth, ensure_ascii=False).encode(), _aad(tenant_id, owner_kind, owner_id))
    return json.dumps({"key_version": version, "nonce": base64.b64encode(nonce).decode(), "ciphertext": base64.b64encode(ciphertext).decode()})


def _decrypt(envelope: str | None, *, tenant_id: str, owner_kind: str, owner_id: str) -> dict:
    if envelope is None:
        return {}
    try:
        value = json.loads(envelope)
        plaintext = AESGCM(_load_key(value["key_version"])).decrypt(
            base64.b64decode(value["nonce"], validate=True), base64.b64decode(value["ciphertext"], validate=True),
            _aad(tenant_id, owner_kind, owner_id),
        )
        return json.loads(plaintext)
    except (CredentialConfigurationError, InvalidTag, ValueError, KeyError, TypeError):
        raise HTTPException(503, "test_egress_auth_unavailable") from None


def public_project_egress(row) -> TestEgressView:
    return TestEgressView.model_validate_json(row["test_egress_json"])


def stored_settings(body: TestEgressInput, *, tenant_id: str, owner_kind: str, owner_id: str,
                    current: TestEgressView | None = None, encrypted_auth: str | None = None) -> tuple[TestEgressView, str | None]:
    # 切回直连即清除认证，避免以后切换代理时意外沿用旧身份。
    if body.mode == "direct" or body.auth_action == "clear":
        encrypted_auth = None
    elif body.auth_action == "replace":
        encrypted_auth = _encrypt({"username": body.username.get_secret_value(), "password": body.password.get_secret_value()},
                                  tenant_id=tenant_id, owner_kind=owner_kind, owner_id=owner_id)
    view = TestEgressView(mode=body.mode, host=body.host, port=body.port, auth_configured=encrypted_auth is not None,
                          revision=current.revision + 1 if current is not None else 1)
    return view, encrypted_auth


def save_project_egress(conn, project_id: str, body: TestEgressInput) -> TestEgressView:
    row = conn.execute("SELECT * FROM projects WHERE id = ? FOR UPDATE", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    view, auth = stored_settings(body, tenant_id=row["tenant_id"], owner_kind="project", owner_id=project_id,
                                 current=public_project_egress(row), encrypted_auth=row["test_egress_auth"])
    conn.execute("UPDATE projects SET test_egress_json = ?, test_egress_auth = ? WHERE id = ?",
                 (view.model_dump_json(), auth, project_id))
    return view


def inherited_input(view: TestEgressView, encrypted_auth: str | None, *, tenant_id: str,
                    owner_kind: str, owner_id: str, override: TestEgressInput | None = None) -> TestEgressInput:
    body = override or TestEgressInput(mode=view.mode, host=view.host, port=view.port)
    if body.mode == "direct" or body.auth_action != "keep" or encrypted_auth is None:
        return body
    auth = _decrypt(encrypted_auth, tenant_id=tenant_id, owner_kind=owner_kind, owner_id=owner_id)
    return TestEgressInput(**(body.model_dump() | auth | {"auth_action": "replace"}))


def project_egress_input(conn, project_id: str, override: TestEgressInput | None = None) -> TestEgressInput:
    row = conn.execute("SELECT * FROM projects WHERE id = ? FOR SHARE", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    return inherited_input(public_project_egress(row), row["test_egress_auth"], tenant_id=row["tenant_id"],
                           owner_kind="project", owner_id=project_id, override=override)


def resolve_task_test_egress(project_id: str, task_run_id: str, principal: RequestPrincipal) -> dict:
    if principal.kind != "machine":
        raise HTTPException(403, "Machine authentication required")
    with get_conn() as conn:
        project = conn.execute("SELECT * FROM projects WHERE id = ? FOR SHARE", (project_id,)).fetchone()
        if project is None:
            raise HTTPException(404, "Project not found")
        run = conn.execute("SELECT * FROM task_runs WHERE project_id = ? AND id = ? FOR SHARE", (project_id, task_run_id)).fetchone()
        if run is None:
            raise HTTPException(404, "test_egress_task_run_not_found")
        if json.loads(run["runtime_context_json"]).get("dispatcher_id") != principal.subject_id:
            raise HTTPException(403, "test_egress_dispatcher_mismatch")
        if run["status"] not in {"queued", "running"} or run["execution_generation"] != project["execution_generation"]:
            raise HTTPException(409, "test_egress_task_run_not_current")
        if project["status"] != "active":
            raise HTTPException(409, "test_egress_project_not_active")
        view = public_project_egress(project)
        result = view.model_dump(exclude={"auth_configured"})
        result.update(_decrypt(project["test_egress_auth"], tenant_id=project["tenant_id"], owner_kind="project", owner_id=project_id))
        return result
