"""Tenant Model credential 加密、版本轮换与隔离测试。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from fastapi import HTTPException

from cairn.server import db
from cairn.server.auth import RequestPrincipal, create_user_with_personal_tenant
from cairn.server.schemas.models import CreateTenantModelRequest
from cairn.server.services import model_credentials
from cairn.server.services.tenant_models import create_tenant_model


@pytest.fixture(autouse=True)
def configured_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db._reset_for_tests()
    db.configure(tmp_path / "models.db")
    keyring = tmp_path / "model-keys.json"
    _write_keyring(keyring, active="key-v1", keys={"key-v1": b"1" * 32})
    monkeypatch.setenv(model_credentials.KEYRING_FILE_ENV, str(keyring))
    yield keyring
    db._reset_for_tests()


def _write_keyring(path: Path, *, active: str, keys: dict[str, bytes]) -> None:
    path.write_text(
        json.dumps(
            {
                "active_version": active,
                "keys": {
                    version: base64.b64encode(value).decode("ascii")
                    for version, value in keys.items()
                },
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _principal(login: str = "model-user") -> RequestPrincipal:
    user_id, tenant_id = create_user_with_personal_tenant(login, "safe password")
    return RequestPrincipal(kind="user", subject_id=user_id, tenant_id=tenant_id, role="user")


def _model(principal: RequestPrincipal) -> str:
    return create_tenant_model(
        CreateTenantModelRequest(
            display_name="Credential Test Model",
            protocol="anthropic-messages",
            base_url="https://api.example.test/",
            model_name="claude-test",
        ),
        principal=principal,
    ).id


def test_aes_gcm_round_trip_rotation_and_nonce_uniqueness(configured_database: Path) -> None:
    principal = _principal()
    model_id = _model(principal)
    with db.get_conn() as conn:
        first = model_credentials.set_or_rotate_credential(
            conn,
            tenant_id=principal.tenant_id,
            model_id=model_id,
            plaintext="secret-first",
        )
        plaintext, metadata = model_credentials.decrypt_active_credential(
            conn,
            tenant_id=principal.tenant_id,
            model_id=model_id,
        )
        assert plaintext == "secret-first"
        assert metadata["version_number"] == 1

        second = model_credentials.set_or_rotate_credential(
            conn,
            tenant_id=principal.tenant_id,
            model_id=model_id,
            plaintext="secret-second",
        )
        rows = conn.execute(
            "SELECT id, nonce, ciphertext, retired_at FROM model_credential_versions "
            "WHERE credential_id = ? ORDER BY version_number",
            (first["credential_id"],),
        ).fetchall()
        assert first["credential_version_id"] != second["credential_version_id"]
        assert rows[0]["nonce"] != rows[1]["nonce"]
        assert rows[0]["ciphertext"] != rows[1]["ciphertext"]
        assert rows[0]["retired_at"] is not None
        assert rows[1]["retired_at"] is None
        assert b"secret-first" not in rows[0]["ciphertext"]
        assert b"secret-second" not in rows[1]["ciphertext"]
        assert model_credentials.decrypt_active_credential(
            conn,
            tenant_id=principal.tenant_id,
            model_id=model_id,
        )[0] == "secret-second"


def test_tamper_and_aad_substitution_fail_closed() -> None:
    principal = _principal()
    model_id = _model(principal)
    with db.get_conn() as conn:
        result = model_credentials.set_or_rotate_credential(
            conn,
            tenant_id=principal.tenant_id,
            model_id=model_id,
            plaintext="tamper-secret",
        )
        conn.execute(
            "UPDATE model_credential_versions SET ciphertext = ? WHERE id = ?",
            (b"invalid-ciphertext", result["credential_version_id"]),
        )
        with pytest.raises(model_credentials.CredentialDecryptionError):
            model_credentials.decrypt_active_credential(
                conn,
                tenant_id=principal.tenant_id,
                model_id=model_id,
            )

    key = b"1" * 32
    nonce = b"0" * 12
    aad = model_credentials._credential_aad("tenant-a", "cred-a", "version-a")
    ciphertext = model_credentials.AESGCM(key).encrypt(nonce, b"secret", aad)
    with pytest.raises(InvalidTag):
        model_credentials.AESGCM(key).decrypt(
            nonce,
            ciphertext,
            model_credentials._credential_aad("tenant-b", "cred-a", "version-a"),
        )


def test_historical_master_key_remains_decryptable(
    configured_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = _principal()
    model_id = _model(principal)
    with db.get_conn() as conn:
        model_credentials.set_or_rotate_credential(
            conn,
            tenant_id=principal.tenant_id,
            model_id=model_id,
            plaintext="historical-secret",
        )

    _write_keyring(
        configured_database,
        active="key-v2",
        keys={"key-v1": b"1" * 32, "key-v2": b"2" * 32},
    )
    monkeypatch.setenv(model_credentials.KEYRING_FILE_ENV, str(configured_database))
    with db.get_conn() as conn:
        plaintext, metadata = model_credentials.decrypt_active_credential(
            conn,
            tenant_id=principal.tenant_id,
            model_id=model_id,
        )
    assert plaintext == "historical-secret"
    assert metadata["key_version"] == "key-v1"


def test_keyring_symlink_is_rejected(
    configured_database: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target-keyring.json"
    _write_keyring(target, active="key-v1", keys={"key-v1": b"1" * 32})
    symlink = tmp_path / "linked-keyring.json"
    symlink.symlink_to(target)
    monkeypatch.setenv(model_credentials.KEYRING_FILE_ENV, str(symlink))
    principal = _principal()
    model_id = _model(principal)
    with db.get_conn() as conn:
        with pytest.raises(model_credentials.CredentialConfigurationError, match="unavailable"):
            model_credentials.set_or_rotate_credential(
                conn,
                tenant_id=principal.tenant_id,
                model_id=model_id,
                plaintext="never-stored",
            )


def test_keyring_permissions_and_missing_historical_key_fail_closed(
    configured_database: Path,
) -> None:
    principal = _principal()
    model_id = _model(principal)
    configured_database.chmod(0o644)
    with db.get_conn() as conn:
        with pytest.raises(model_credentials.CredentialConfigurationError, match="permissions"):
            model_credentials.set_or_rotate_credential(
                conn,
                tenant_id=principal.tenant_id,
                model_id=model_id,
                plaintext="never-stored",
            )

    _write_keyring(configured_database, active="key-v2", keys={"key-v2": b"2" * 32})
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO model_credentials (id, tenant_id, model_id, created_at, updated_at) "
            "VALUES ('cred-old', ?, ?, '2026-08-13T00:00:00Z', '2026-08-13T00:00:00Z')",
            (principal.tenant_id, model_id),
        )
        conn.execute(
            """
            INSERT INTO model_credential_versions
                (id, credential_id, tenant_id, version_number, algorithm, key_version,
                 nonce, ciphertext, credential_fingerprint, created_at)
            VALUES ('version-old', 'cred-old', ?, 1, 'AES-256-GCM', 'key-v1', ?, ?, 'fingerprint',
                    '2026-08-13T00:00:00Z')
            """,
            (principal.tenant_id, b"0" * 12, b"ciphertext"),
        )
        conn.execute(
            "UPDATE model_credentials SET active_version_id = 'version-old' WHERE id = 'cred-old'"
        )
        with pytest.raises(model_credentials.CredentialConfigurationError, match="unavailable"):
            model_credentials.decrypt_active_credential(
                conn,
                tenant_id=principal.tenant_id,
                model_id=model_id,
            )


def test_plaintext_validation_and_cross_tenant_lookup() -> None:
    tenant_a = _principal("tenant-a-user")
    tenant_b = _principal("tenant-b-user")
    model_id = _model(tenant_a)
    with db.get_conn() as conn:
        with pytest.raises(HTTPException) as blank:
            model_credentials.set_or_rotate_credential(
                conn,
                tenant_id=tenant_a.tenant_id,
                model_id=model_id,
                plaintext=" secret ",
            )
        assert blank.value.status_code == 422

        with pytest.raises(HTTPException) as hidden:
            model_credentials.set_or_rotate_credential(
                conn,
                tenant_id=tenant_b.tenant_id,
                model_id=model_id,
                plaintext="secret",
            )
        assert hidden.value.status_code == 404
