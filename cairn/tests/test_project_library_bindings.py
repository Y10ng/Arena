"""项目创建时固定 advisory library revision。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from fastapi import HTTPException
from pydantic import ValidationError
import pytest

from cairn.server import db
from cairn.server.app import app
from route_helpers import iter_app_routes
from cairn.server.auth import RequestPrincipal, create_user_with_personal_tenant, require_user
from cairn.server.models import CreateProjectRequest
from cairn.server.routers.projects import get_library_catalog
from cairn.server.schemas.groups import CreateProjectGroupRequest
from cairn.server.schemas.libraries import LibraryFileWrite
from cairn.server.schemas.models import CreateTenantModelRequest
from cairn.server.schemas.tenant_workers import CreateTenantWorkerRequest
from cairn.server.services import model_credentials
from cairn.server.services.libraries import (
    archive_library,
    create_library,
    create_library_version,
)
from cairn.server.services.project_groups import create_project_group
from cairn.server.services.project_records import (
    create_project_record,
    delete_project_record,
    get_project_detail,
)
from cairn.server.services.tenant_models import create_tenant_model
from cairn.server.services.tenant_workers import create_tenant_worker
from cairn.server.services import tenant_workers as tenant_workers_service


@pytest.fixture(autouse=True)
def configured_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db._reset_for_tests()
    db.configure(tmp_path / "project-library-bindings.db")
    keyring = tmp_path / "keyring.json"
    keyring.write_text(
        json.dumps(
            {
                "active_version": "key-v1",
                "keys": {"key-v1": base64.b64encode(b"p" * 32).decode()},
            }
        ),
        encoding="utf-8",
    )
    keyring.chmod(0o600)
    monkeypatch.setenv(model_credentials.KEYRING_FILE_ENV, str(keyring))
    monkeypatch.setenv("CAIRN_PROJECT_RESOURCE_ROOT", str(tmp_path / "project-resources"))
    monkeypatch.setenv("CAIRN_ADVISORY_LIBRARY_ROOT", str(tmp_path / "library-content"))
    monkeypatch.setattr(
        tenant_workers_service,
        "dispatcher_executor_available",
        lambda conn: True,
    )
    yield
    app.dependency_overrides.clear()
    db._reset_for_tests()


def _principal(login: str) -> RequestPrincipal:
    user_id, tenant_id = create_user_with_personal_tenant(login, "safe password")
    return RequestPrincipal(
        kind="user",
        subject_id=user_id,
        tenant_id=tenant_id,
        role="user",
    )


def _worker(principal: RequestPrincipal):
    model = create_tenant_model(
        CreateTenantModelRequest(
            display_name="Project Library Model",
            protocol="anthropic-messages",
            base_url="https://api.example.test",
            model_name="claude-test",
            api_key="project-secret",
        ),
        principal=principal,
    )
    return create_tenant_worker(
        CreateTenantWorkerRequest(display_name="Project Library Worker", model_id=model.id),
        principal=principal,
    )


def _request(principal: RequestPrincipal, worker_id: str, **updates) -> CreateProjectRequest:
    group = create_project_group(
        CreateProjectGroupRequest(name="Library Projects"),
        tenant_id=principal.tenant_id,
        actor_id=principal.subject_id,
    )
    values = {
        "title": "Library-bound project",
        "origin": "https://example.com",
        "goal": "Audit the target",
        "scenario": "default",
        "tenant_worker_ids": [worker_id],
        "group_id": group.id,
        "authorization_expires_at": None,
    }
    values.update(updates)
    return CreateProjectRequest(**values)


def _publish_library(key: str = "web_checks", name: str = "Web Checks"):
    with db.get_conn() as conn:
        create_library(conn, key, name)
        return create_library_version(
            conn,
            key,
            [
                LibraryFileWrite(path="README.md", content="revision one"),
                LibraryFileWrite(path="checks/login.md", content="check login"),
            ],
        )


def test_project_creation_freezes_library_revision_after_global_update() -> None:
    principal = _principal("library-owner")
    worker = _worker(principal)
    first_revision = _publish_library()

    project = create_project_record(
        _request(principal, worker.id, library_keys=["web_checks"]),
        tenant_id=principal.tenant_id,
    )

    assert [item.model_dump() for item in project.library_bindings] == [
        {
            "library_key": "web_checks",
            "library_name": "Web Checks",
            "version": 1,
            "digest_sha256": first_revision.digest_sha256,
            "file_count": 2,
        }
    ]

    with db.get_conn() as conn:
        second_revision = create_library_version(
            conn,
            "web_checks",
            [LibraryFileWrite(path="README.md", content="revision two")],
        )
    assert second_revision.version == 2

    reloaded = get_project_detail(project.project.id, tenant_id=principal.tenant_id)
    assert reloaded.library_bindings == project.library_bindings
    assert reloaded.library_bindings[0].version == 1
    assert reloaded.library_bindings[0].digest_sha256 == first_revision.digest_sha256
    assert reloaded.library_bindings[0].file_count == 2


def test_project_library_selection_rejects_duplicate_missing_and_archived_keys() -> None:
    principal = _principal("library-validation")
    worker = _worker(principal)

    with pytest.raises(ValidationError, match="duplicate library key: repeated"):
        _request(
            principal,
            worker.id,
            library_keys=["repeated", " repeated "],
        )

    with pytest.raises(HTTPException) as missing:
        create_project_record(
            _request(principal, worker.id, library_keys=["missing"]),
            tenant_id=principal.tenant_id,
        )
    assert missing.value.status_code == 404

    _publish_library("archived", "Archived")
    with db.get_conn() as conn:
        archive_library(conn, "archived")
    with pytest.raises(HTTPException) as archived:
        create_project_record(
            _request(principal, worker.id, library_keys=["archived"]),
            tenant_id=principal.tenant_id,
        )
    assert archived.value.status_code == 404

    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM projects").fetchone()["count"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM project_advisory_library_bindings"
        ).fetchone()["count"] == 0


def test_project_delete_cascades_library_bindings() -> None:
    principal = _principal("library-delete")
    worker = _worker(principal)
    _publish_library()
    project = create_project_record(
        _request(principal, worker.id, library_keys=["web_checks"]),
        tenant_id=principal.tenant_id,
    )

    delete_project_record(project.project.id)

    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM project_advisory_library_bindings WHERE project_id = ?",
            (project.project.id,),
        ).fetchone()["count"] == 0


def test_project_library_metadata_respects_project_tenant_authorization() -> None:
    owner = _principal("library-tenant-owner")
    outsider = _principal("library-tenant-outsider")
    worker = _worker(owner)
    _publish_library()
    project = create_project_record(
        _request(owner, worker.id, library_keys=["web_checks"]),
        tenant_id=owner.tenant_id,
    )

    with pytest.raises(HTTPException) as hidden:
        get_project_detail(project.project.id, tenant_id=outsider.tenant_id)
    assert hidden.value.status_code == 404


def test_library_catalog_is_user_authenticated_and_metadata_only() -> None:
    principal = _principal("library-catalog-user")
    _publish_library()
    route = next(route for route in iter_app_routes(app) if getattr(route, "path", None) == "/library-catalog")
    assert require_user in {dependency.call for dependency in route.dependant.dependencies}

    catalog = get_library_catalog(principal)

    assert len(catalog) == 1
    assert catalog[0].model_dump() == {
        "key": "web_checks",
        "name": "Web Checks",
        "latest_version": 1,
        "file_count": 2,
        "created_at": catalog[0].created_at,
        "archived_at": None,
    }
    assert "content" not in type(catalog[0]).model_fields
