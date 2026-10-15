"""Tenant Model credential 的 AES-GCM 加密与版本管理。"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import secrets
import stat
from typing import Any
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException

from cairn.server.auth import hash_secret

from .time_utils import utcnow

KEYRING_FILE_ENV = "CAIRN_MODEL_CREDENTIAL_KEYRING_FILE"
ALGORITHM = "AES-256-GCM"
MAX_CREDENTIAL_LENGTH = 16_384


class CredentialConfigurationError(RuntimeError):
    """Master Key 配置缺失或不可安全使用。"""


class CredentialDecryptionError(RuntimeError):
    """密文损坏、AAD 不匹配或历史 Key 不可用。"""


def validate_plaintext_credential(value: str) -> str:
    if not value or value != value.strip():
        raise HTTPException(422, "API key must not be blank or contain surrounding whitespace")
    if len(value) > MAX_CREDENTIAL_LENGTH:
        raise HTTPException(422, f"API key must be {MAX_CREDENTIAL_LENGTH} characters or fewer")
    return value


def set_or_rotate_credential(
    conn: Any,
    *,
    tenant_id: str,
    model_id: str,
    plaintext: str,
) -> dict[str, object]:
    plaintext = validate_plaintext_credential(plaintext)
    model = conn.execute(
        "SELECT id FROM tenant_models WHERE id = ? AND tenant_id = ?",
        (model_id, tenant_id),
    ).fetchone()
    if model is None:
        raise HTTPException(404, "Model not found")

    key_version, key = _load_active_key()
    credential = conn.execute(
        "SELECT * FROM model_credentials WHERE model_id = ? AND tenant_id = ?",
        (model_id, tenant_id),
    ).fetchone()
    now = utcnow()
    if credential is None:
        credential_id = f"mcred_{uuid4().hex}"
        version_number = 1
        conn.execute(
            "INSERT INTO model_credentials (id, tenant_id, model_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (credential_id, tenant_id, model_id, now, now),
        )
    else:
        credential_id = str(credential["id"])
        version_number = int(
            conn.execute(
                "SELECT COALESCE(MAX(version_number), 0) AS value FROM model_credential_versions WHERE credential_id = ?",
                (credential_id,),
            ).fetchone()["value"]
        ) + 1

    version_id = f"mcv_{uuid4().hex}"
    nonce = secrets.token_bytes(12)
    aad = _credential_aad(tenant_id, credential_id, version_id)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), aad)
    conn.execute(
        "UPDATE model_credential_versions SET retired_at = ? WHERE credential_id = ? AND retired_at IS NULL",
        (now, credential_id),
    )
    conn.execute(
        """
        INSERT INTO model_credential_versions
            (id, credential_id, tenant_id, version_number, algorithm, key_version,
             nonce, ciphertext, credential_fingerprint, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version_id,
            credential_id,
            tenant_id,
            version_number,
            ALGORITHM,
            key_version,
            nonce,
            ciphertext,
            hash_secret(plaintext),
            now,
        ),
    )
    conn.execute(
        "UPDATE model_credentials SET active_version_id = ?, updated_at = ? WHERE id = ?",
        (version_id, now, credential_id),
    )
    return {
        "credential_id": credential_id,
        "credential_version_id": version_id,
        "version_number": version_number,
        "key_version": key_version,
    }


def decrypt_active_credential(conn: Any, *, tenant_id: str, model_id: str) -> tuple[str, dict[str, object]]:
    row = conn.execute(
        """
        SELECT c.id AS credential_id, c.active_version_id, v.version_number, v.algorithm,
               v.key_version, v.nonce, v.ciphertext, v.credential_fingerprint
        FROM model_credentials c
        JOIN model_credential_versions v ON v.id = c.active_version_id
        WHERE c.model_id = ? AND c.tenant_id = ? AND v.tenant_id = ?
        """,
        (model_id, tenant_id, tenant_id),
    ).fetchone()
    if row is None:
        raise HTTPException(422, "Model credential is not configured")
    return _decrypt_credential_row(row, tenant_id=tenant_id)


def decrypt_credential_version(conn: Any, *, tenant_id: str, model_id: str,
                               version_id: str) -> tuple[str, dict[str, object]]:
    """按当次执行来源读取既有密文版本，不以当前活动凭据冒充历史凭据。"""
    row = conn.execute(
        "SELECT c.id AS credential_id, v.id AS active_version_id, v.version_number, v.algorithm, "
        "v.key_version, v.nonce, v.ciphertext, v.credential_fingerprint "
        "FROM model_credentials c JOIN model_credential_versions v ON v.credential_id = c.id "
        "WHERE c.model_id = ? AND c.tenant_id = ? AND v.tenant_id = ? AND v.id = ?",
        (model_id, tenant_id, tenant_id, version_id),
    ).fetchone()
    if row is None:
        raise HTTPException(422, "执行时的模型凭据版本已不可读取")
    return _decrypt_credential_row(row, tenant_id=tenant_id)


def _decrypt_credential_row(row: Any, *, tenant_id: str) -> tuple[str, dict[str, object]]:
    if row["algorithm"] != ALGORITHM:
        raise CredentialDecryptionError("Unsupported credential algorithm")
    key = _load_key(str(row["key_version"]))
    aad = _credential_aad(tenant_id, str(row["credential_id"]), str(row["active_version_id"]))
    try:
        plaintext = AESGCM(key).decrypt(bytes(row["nonce"]), bytes(row["ciphertext"]), aad).decode("utf-8")
    except (InvalidTag, UnicodeDecodeError, ValueError) as exc:
        raise CredentialDecryptionError("Credential authentication failed") from exc
    return plaintext, {
        "credential_id": row["credential_id"],
        "credential_version_id": row["active_version_id"],
        "version_number": row["version_number"],
        "key_version": row["key_version"],
        "credential_fingerprint": row["credential_fingerprint"],
    }


def delete_credential(conn: Any, *, tenant_id: str, model_id: str) -> bool:
    credential = conn.execute(
        "SELECT id FROM model_credentials WHERE model_id = ? AND tenant_id = ?",
        (model_id, tenant_id),
    ).fetchone()
    if credential is None:
        return False
    conn.execute("DELETE FROM model_credentials WHERE id = ?", (credential["id"],))
    return True


def credential_status(conn: Any, *, tenant_id: str, model_id: str) -> dict[str, object]:
    row = conn.execute(
        """
        SELECT v.version_number, v.key_version
        FROM model_credentials c
        LEFT JOIN model_credential_versions v ON v.id = c.active_version_id
        WHERE c.model_id = ? AND c.tenant_id = ?
        """,
        (model_id, tenant_id),
    ).fetchone()
    return {
        "credential_configured": row is not None and row["version_number"] is not None,
        "credential_version": int(row["version_number"]) if row is not None and row["version_number"] else None,
        "credential_key_version": str(row["key_version"]) if row is not None and row["key_version"] else None,
    }


def _credential_aad(tenant_id: str, credential_id: str, version_id: str) -> bytes:
    return f"cairn:model-credential:v1:{tenant_id}:{credential_id}:{version_id}".encode("utf-8")


def _load_active_key() -> tuple[str, bytes]:
    document = _load_keyring()
    active_version = document.get("active_version")
    if not isinstance(active_version, str) or not active_version:
        raise CredentialConfigurationError("Credential keyring active version is invalid")
    return active_version, _decode_key(document, active_version)


def _load_key(version: str) -> bytes:
    return _decode_key(_load_keyring(), version)


def _load_keyring() -> dict[str, object]:
    raw_path = os.environ.get(KEYRING_FILE_ENV, "").strip()
    if not raw_path:
        raise CredentialConfigurationError("Credential keyring is not configured")
    path = Path(raw_path).expanduser()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CredentialConfigurationError("Credential keyring is unavailable") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or stat.S_IMODE(file_stat.st_mode) != 0o600:
            raise CredentialConfigurationError("Credential keyring permissions are unsafe")
        if hasattr(os, "geteuid") and file_stat.st_uid != os.geteuid():
            raise CredentialConfigurationError("Credential keyring ownership is unsafe")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            document = json.load(handle)
    except CredentialConfigurationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CredentialConfigurationError("Credential keyring is invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(document, dict) or not isinstance(document.get("keys"), dict):
        raise CredentialConfigurationError("Credential keyring is invalid")
    return document


def _decode_key(document: dict[str, object], version: str) -> bytes:
    keys = document.get("keys")
    encoded = keys.get(version) if isinstance(keys, dict) else None
    if not isinstance(encoded, str):
        raise CredentialConfigurationError("Credential key version is unavailable")
    try:
        key = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise CredentialConfigurationError("Credential key material is invalid") from exc
    if len(key) != 32:
        raise CredentialConfigurationError("Credential key must contain 32 bytes")
    return key
