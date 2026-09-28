"""Worker 模板的 Tenant 隔离与 durable Worker ID binding 测试。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from fastapi import HTTPException
import pytest

from cairn.server import db
from cairn.server.auth import RequestPrincipal, create_user_with_personal_tenant
from cairn.server.schemas.models import CreateTenantModelRequest
from cairn.server.models import CreateWorkerTemplateRequest, UpdateWorkerTemplateRequest
from cairn.server.schemas.tenant_workers import CreateTenantWorkerRequest
from cairn.server.services import model_credentials
from cairn.server.services.tenant_models import create_tenant_model
from cairn.server.services.tenant_workers import create_tenant_worker
from cairn.server.services import tenant_workers as tenant_workers_service
from cairn.server.services import worker_templates as templates_service


@pytest.fixture(autouse=True)
def configured_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db._reset_for_tests()
    db.configure(tmp_path / "worker-templates.db")
    keyring = tmp_path / "keyring.json"
    keyring.write_text(
        json.dumps(
            {
                "active_version": "key-v1",
                "keys": {"key-v1": base64.b64encode(b"t" * 32).decode()},
            }
        ),
        encoding="utf-8",
    )
    keyring.chmod(0o600)
    monkeypatch.setenv(model_credentials.KEYRING_FILE_ENV, str(keyring))
    monkeypatch.setattr(
        tenant_workers_service,
        "dispatcher_executor_available",
        lambda conn: True,
    )
    yield
    db._reset_for_tests()


def _principal(login: str) -> RequestPrincipal:
    user_id, tenant_id = create_user_with_personal_tenant(login, "safe password")
    return RequestPrincipal(kind="user", subject_id=user_id, tenant_id=tenant_id, role="user")


def _worker(principal: RequestPrincipal, name: str):
    model = create_tenant_model(
        CreateTenantModelRequest(
            display_name=f"{name} Model",
            protocol="anthropic-messages",
            base_url="https://api.example.test",
            model_name="claude-test",
            api_key="template-secret",
        ),
        principal=principal,
    )
    return create_tenant_worker(
        CreateTenantWorkerRequest(display_name=name, model_id=model.id),
        principal=principal,
    )


def test_tenant_is_required_by_service_contract() -> None:
    body = CreateWorkerTemplateRequest(name="模板", tenant_worker_ids=["worker-id"])
    with pytest.raises(TypeError):
        templates_service.create_worker_template(body)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        templates_service.list_worker_templates()  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="tenant_id is required"):
        templates_service.list_worker_templates(tenant_id="")


def test_create_ignores_spoofed_owner_and_lists_by_tenant() -> None:
    tenant_a = _principal("template-a")
    tenant_b = _principal("template-b")
    first = _worker(tenant_a, "First")
    second = _worker(tenant_a, "Second")

    template = templates_service.create_worker_template(
        CreateWorkerTemplateRequest(
            name="Tenant A",
            owner="forged-owner",
            tenant_worker_ids=[first.id, first.id, second.id],
        ),
        tenant_id=tenant_a.tenant_id,
        actor_id=tenant_a.subject_id,
    )

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT owner, tenant_id, worker_names_json FROM worker_templates WHERE id = ?",
            (template.id,),
        ).fetchone()
    assert dict(row) == {
        "owner": tenant_a.subject_id,
        "tenant_id": tenant_a.tenant_id,
        "worker_names_json": "[]",
    }
    assert template.tenant_worker_ids == [first.id, second.id]
    assert template.worker_names == []
    assert [item.id for item in templates_service.list_worker_templates(tenant_id=tenant_a.tenant_id)] == [template.id]
    assert templates_service.list_worker_templates(tenant_id=tenant_b.tenant_id) == []


def test_historical_template_remains_readable_but_legacy_workers_cannot_be_bound() -> None:
    principal = _principal("template-legacy")
    worker = _worker(principal, "Historical")
    template = templates_service.create_worker_template(
        CreateWorkerTemplateRequest(name="Before switch", tenant_worker_ids=[worker.id]),
        tenant_id=principal.tenant_id,
        actor_id=principal.subject_id,
    )
    with db.get_conn() as conn:
        conn.execute("UPDATE tenant_workers SET engine = 'claudecode' WHERE id = ?", (worker.id,))
    assert templates_service.list_worker_templates(tenant_id=principal.tenant_id)[0].tenant_worker_ids == [worker.id]
    with pytest.raises(HTTPException, match="engine_unsupported"):
        templates_service.create_worker_template(
            CreateWorkerTemplateRequest(name="Must reject", tenant_worker_ids=[worker.id]),
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
        )
    with pytest.raises(HTTPException, match="engine_unsupported"):
        templates_service.update_worker_template(
            template.id,
            UpdateWorkerTemplateRequest(tenant_worker_ids=[worker.id]),
            tenant_id=principal.tenant_id,
        )


def test_update_delete_and_cross_tenant_worker_are_isolated() -> None:
    tenant_a = _principal("template-update-a")
    tenant_b = _principal("template-update-b")
    first = _worker(tenant_a, "First")
    second = _worker(tenant_a, "Second")
    foreign = _worker(tenant_b, "Foreign")
    template = templates_service.create_worker_template(
        CreateWorkerTemplateRequest(name="模板", tenant_worker_ids=[first.id]),
        tenant_id=tenant_a.tenant_id,
        actor_id=tenant_a.subject_id,
    )

    updated = templates_service.update_worker_template(
        template.id,
        UpdateWorkerTemplateRequest(name="新模板", tenant_worker_ids=[second.id]),
        tenant_id=tenant_a.tenant_id,
    )
    assert updated.name == "新模板"
    assert updated.tenant_worker_ids == [second.id]

    with pytest.raises(HTTPException) as foreign_worker:
        templates_service.update_worker_template(
            template.id,
            UpdateWorkerTemplateRequest(tenant_worker_ids=[foreign.id]),
            tenant_id=tenant_a.tenant_id,
        )
    assert foreign_worker.value.status_code == 404

    with pytest.raises(HTTPException) as hidden:
        templates_service.delete_worker_template(template.id, tenant_id=tenant_b.tenant_id)
    assert hidden.value.status_code == 404

    templates_service.delete_worker_template(template.id, tenant_id=tenant_a.tenant_id)
    assert templates_service.list_worker_templates(tenant_id=tenant_a.tenant_id) == []
