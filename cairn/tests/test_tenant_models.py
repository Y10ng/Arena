"""Tenant Model 配置、revision、Header 与凭据原子性测试。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from fastapi import HTTPException
import pytest

from cairn.server import db
from cairn.server.auth import RequestPrincipal, create_user_with_personal_tenant
from cairn.server.schemas.models import CreateTenantModelRequest, UpdateTenantModelRequest
from cairn.server.services import model_credentials
from cairn.server.services.tenant_models import (
    create_tenant_model,
    delete_tenant_model,
    delete_tenant_model_credential,
    get_tenant_model,
    list_tenant_models,
    normalize_model_base_url,
    update_tenant_model,
)


@pytest.fixture(autouse=True)
def configured_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db._reset_for_tests()
    db.configure(tmp_path / "tenant-models.db")
    keyring = tmp_path / "keyring.json"
    keyring.write_text(
        json.dumps({
            "active_version": "key-v1",
            "keys": {"key-v1": base64.b64encode(b"k" * 32).decode("ascii")},
        }),
        encoding="utf-8",
    )
    keyring.chmod(0o600)
    monkeypatch.setenv(model_credentials.KEYRING_FILE_ENV, str(keyring))
    yield
    db._reset_for_tests()


def _principal(login: str, role: str = "user") -> RequestPrincipal:
    user_id, tenant_id = create_user_with_personal_tenant(login, "safe password", role=role)
    return RequestPrincipal(kind="user", subject_id=user_id, tenant_id=tenant_id, role=role)


def _create_body(**overrides) -> CreateTenantModelRequest:
    values = {
        "display_name": "Primary Model",
        "protocol": "anthropic-messages",
        "base_url": "https://api.example.test/v1/",
        "auth_header": "authorization-bearer",
        "custom_headers": [],
        "models_endpoint": None,
        "inference_endpoint": None,
        "model_name": "claude-test",
        "enabled": True,
    }
    values.update(overrides)
    return CreateTenantModelRequest(**values)


def _update_body(model, **overrides) -> UpdateTenantModelRequest:
    values = {
        "display_name": model.display_name,
        "protocol": model.protocol,
        "base_url": model.base_url,
        "auth_header": model.auth_header,
        "custom_headers": model.custom_headers,
        "models_endpoint": model.models_endpoint,
        "inference_endpoint": model.inference_endpoint,
        "model_name": model.model_name,
        "enabled": model.enabled,
        "expected_revision": model.revision,
    }
    values.update(overrides)
    return UpdateTenantModelRequest(**values)


def test_base_url_normalization_allows_private_hosts_and_rejects_unsafe_shapes() -> None:
    assert normalize_model_base_url("http://10.20.30.40:8080/v1/") == "http://10.20.30.40:8080/v1"
    assert normalize_model_base_url("https://[2001:db8::1]:8443/api/") == "https://[2001:db8::1]:8443/api"
    for value in (
        "ftp://api.example.test",
        "https://user:secret@api.example.test",
        "https://api.example.test/path?token=value",
        "https://api.example.test/path#fragment",
        "https://api.example.test:invalid",
    ):
        with pytest.raises(HTTPException) as exc:
            normalize_model_base_url(value)
        assert exc.value.status_code == 422


def test_crud_is_tenant_scoped_and_returns_complete_plaintext_header_config() -> None:
    tenant_a = _principal("tenant-a")
    tenant_b = _principal("tenant-b")
    created = create_tenant_model(
        _create_body(
            custom_headers=[{"name": "X-Gateway-Route", "value": "café/route-A"}],
            models_endpoint="catalog?scope=all",
            inference_endpoint="https://gateway.example.test/invoke?mode=raw",
        ),
        principal=tenant_a,
    )

    assert created.base_url == "https://api.example.test/v1"
    assert created.custom_headers[0].value == "café/route-A"
    assert created.models_endpoint == "catalog?scope=all"
    assert [item.id for item in list_tenant_models(tenant_id=tenant_a.tenant_id)] == [created.id]
    assert list_tenant_models(tenant_id=tenant_b.tenant_id) == []


def test_create_without_test_can_save_without_key_and_update_can_rotate_key_once() -> None:
    principal = _principal("atomic-user")
    created = create_tenant_model(_create_body(), principal=principal)
    assert created.revision == 1
    assert created.credential_configured is False

    secret = "rotated-secret-456"
    updated = update_tenant_model(
        created.id,
        _update_body(
            created,
            display_name="Updated Model",
            protocol="openai-chat-completions",
            auth_header="x-api-key",
            api_key=secret,
        ),
        principal=principal,
    )
    assert updated.revision == 2
    assert updated.credential_configured is True
    assert updated.credential_version == 1
    assert updated.protocol == "openai-chat-completions"
    assert secret not in updated.model_dump_json()

    with db.get_conn() as conn:
        ciphertext = conn.execute("SELECT ciphertext FROM model_credential_versions").fetchone()["ciphertext"]
        audits = conn.execute("SELECT metadata_json FROM security_audit_events").fetchall()
    assert secret.encode() not in bytes(ciphertext)
    assert secret not in json.dumps([row["metadata_json"] for row in audits])


def test_update_without_api_key_preserves_existing_credential() -> None:
    principal = _principal("preserve-user")
    created = create_tenant_model(_create_body(api_key="original-secret"), principal=principal)
    updated = update_tenant_model(
        created.id,
        _update_body(created, model_name="claude-renamed"),
        principal=principal,
    )
    assert updated.revision == 2
    assert updated.credential_version == created.credential_version == 1


def test_revision_conflict_rolls_back_configuration_and_credential_rotation() -> None:
    principal = _principal("revision-user")
    created = create_tenant_model(_create_body(api_key="original-secret"), principal=principal)
    with pytest.raises(HTTPException) as stale:
        update_tenant_model(
            created.id,
            _update_body(
                created,
                expected_revision=created.revision + 1,
                base_url="https://changed.example.test",
                api_key="must-not-rotate",
            ),
            principal=principal,
        )
    assert stale.value.status_code == 409
    current = get_tenant_model(created.id, tenant_id=principal.tenant_id)
    assert current.base_url == created.base_url
    assert current.credential_version == created.credential_version
    assert current.revision == created.revision


def test_display_name_is_unique_per_tenant() -> None:
    tenant_a = _principal("display-a")
    tenant_b = _principal("display-b")
    create_tenant_model(_create_body(display_name="Shared Model"), principal=tenant_a)
    create_tenant_model(_create_body(display_name="Shared Model"), principal=tenant_b)
    with pytest.raises(HTTPException) as duplicate:
        create_tenant_model(
            _create_body(display_name="Shared Model", base_url="https://other.example.test"),
            principal=tenant_a,
        )
    assert duplicate.value.status_code == 409


def test_invalid_endpoint_override_is_rejected_as_input_error() -> None:
    principal = _principal("endpoint-user")
    with pytest.raises(HTTPException) as exc:
        create_tenant_model(
            _create_body(models_endpoint="javascript:models"),
            principal=principal,
        )
    assert exc.value.status_code == 422


def test_custom_headers_reject_duplicates_reserved_names_and_control_values() -> None:
    principal = _principal("header-user")
    invalid = (
        [{"name": "X-Route", "value": "a"}, {"name": "x-route", "value": "b"}],
        [{"name": "Authorization", "value": "custom"}],
        [{"name": "X-Route", "value": "line\nbreak"}],
        [{"name": "X-Route", "value": "无法发送"}],
    )
    for headers in invalid:
        with pytest.raises(HTTPException) as exc:
            create_tenant_model(_create_body(custom_headers=headers), principal=principal)
        assert exc.value.status_code == 422
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM tenant_models").fetchone()["count"] == 0


def test_delete_credential_and_model_require_current_revision() -> None:
    principal = _principal("delete-user")
    created = create_tenant_model(_create_body(api_key="secret"), principal=principal)
    without_key = delete_tenant_model_credential(
        created.id,
        expected_revision=created.revision,
        principal=principal,
    )
    assert without_key.revision == 2
    assert without_key.credential_configured is False

    with pytest.raises(HTTPException) as conflict:
        delete_tenant_model(created.id, expected_revision=created.revision, principal=principal)
    assert conflict.value.status_code == 409
    delete_tenant_model(created.id, expected_revision=without_key.revision, principal=principal)
    with pytest.raises(HTTPException) as missing:
        get_tenant_model(created.id, tenant_id=principal.tenant_id)
    assert missing.value.status_code == 404
