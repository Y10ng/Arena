"""项目创建时固定 advisory library revision。"""

from __future__ import annotations

import base64
import json
import os
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
from cairn.server.schemas.libraries import LibraryFileWrite, LibrarySelection
from cairn.server.services import model_credentials
from cairn.server.services.libraries import (
    archive_library,
    create_library,
    replace_library_files,
    list_library_versions,
    get_selectable_library_manifest,
)
from cairn.server.services.project_records import (
    create_project_record,
    delete_project_record,
    get_project_detail,
)
from cairn.server.services import tenant_workers as tenant_workers_service
from test_project_creation_workers import _worker as configure_workers
from test_postgres_bootstrap import postgres_instance, empty_postgres_url


@pytest.fixture(autouse=True)
def configured_database(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1":
        pytest.skip("需显式启用隔离 PostgreSQL")
    db._reset_for_tests()
    url = request.getfixturevalue("empty_postgres_url")
    monkeypatch.setenv("CAIRN_KNOWLEDGE_ROOT", str(tmp_path / "knowledge"))
    monkeypatch.setenv("CAIRN_ADVISORY_LIBRARY_ROOT", str(tmp_path / "library-content"))
    db.configure(database_url=url, artifact_path=tmp_path / "artifacts")
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
    return configure_workers(principal, "Project Library Model")[0]


def _request(principal: RequestPrincipal, worker_id: str, **updates) -> CreateProjectRequest:
    values = {
        "title": "Library-bound project",
        "origin": "https://example.com",
        "goal": "Audit the target",
        "scenario": "goal-driven-v1",
        "authorization_expires_at": None,
        "library_selections": [],
    }
    values.update(updates)
    return CreateProjectRequest(**values)


def _publish_library(key: str = "web_checks", name: str = "Web Checks"):
    with db.get_conn() as conn:
        create_library(conn, key, name)
        return replace_library_files(
            conn,
            key,
            [
                LibraryFileWrite(path="README.md", content="revision one"),
                LibraryFileWrite(path="checks/login.md", content="check login"),
            ],
        )


def _selection(principal: RequestPrincipal, key: str) -> LibrarySelection:
    with db.get_conn() as conn:
        manifest = get_selectable_library_manifest(conn, key)
    return LibrarySelection(library_key=key, source="current", paths=None,
        expected_files={file.path: file.sha256 for file in manifest.files})


def test_project_creation_freezes_library_revision_after_global_update() -> None:
    principal = _principal("library-owner")
    worker = _worker(principal)
    _publish_library()

    project = create_project_record(
        _request(principal, worker.id, library_selections=[_selection(principal, "web_checks")]),
        tenant_id=principal.tenant_id, creator=principal.subject_id,
    )

    assert [item.model_dump() for item in project.library_bindings] == [
        {
            "library_key": "web_checks",
            "library_name": "Web Checks",
            "version": 1,
            "digest_sha256": project.library_bindings[0].digest_sha256,
            "file_count": 2,
            "paths": None,
            "selection_sha256": project.library_bindings[0].selection_sha256,
        }
    ]

    with db.get_conn() as conn:
        second_revision = replace_library_files(
            conn,
            "web_checks",
            [LibraryFileWrite(path="README.md", content="revision two")],
        )
    assert second_revision.files[0].path == "README.md"
    with db.get_conn() as conn:
        assert len(list_library_versions(conn, "web_checks")) == 1

    reloaded = get_project_detail(project.project.id, tenant_id=principal.tenant_id)
    assert reloaded.library_bindings == project.library_bindings
    assert reloaded.library_bindings[0].version == 1
    assert reloaded.library_bindings[0].digest_sha256 == project.library_bindings[0].digest_sha256
    assert reloaded.library_bindings[0].file_count == 2


def test_project_library_selection_rejects_duplicate_missing_and_archived_keys() -> None:
    principal = _principal("library-validation")
    worker = _worker(principal)

    with pytest.raises(ValidationError, match="duplicate library key"):
        _request(
            principal,
            worker.id,
            library_selections=[
                {"library_key": "repeated", "source": "current", "paths": None, "expected_files": {"file": "a" * 64}},
                {"library_key": "repeated", "source": "current", "paths": None, "expected_files": {"file": "a" * 64}},
            ],
        )

    with pytest.raises(HTTPException) as missing:
        create_project_record(
            _request(principal, worker.id, library_selections=[{"library_key": "missing", "source": "current", "paths": None, "expected_files": {"file": "a" * 64}}]),
            tenant_id=principal.tenant_id,
        )
    assert missing.value.status_code == 404

    _publish_library("archived", "Archived")
    archived_selection = _selection(principal, "archived")
    with db.get_conn() as conn:
        archive_library(conn, "archived")
    with pytest.raises(HTTPException) as archived:
        create_project_record(
            _request(principal, worker.id, library_selections=[archived_selection]),
            tenant_id=principal.tenant_id, creator=principal.subject_id,
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
        _request(principal, worker.id, library_selections=[_selection(principal, "web_checks")]),
        tenant_id=principal.tenant_id, creator=principal.subject_id,
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
        _request(owner, worker.id, library_selections=[_selection(owner, "web_checks")]),
        tenant_id=owner.tenant_id, creator=owner.subject_id,
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

    assert len(catalog) == 2
    assert any(item.builtin for item in catalog)
    catalog = [item for item in catalog if item.key == "web_checks"]
    assert catalog[0].model_dump() == {
        "key": "web_checks",
        "name": "Web Checks",
        "builtin": False,
        "file_count": 2,
        "created_at": catalog[0].created_at,
        "archived_at": None,
    }
    assert "content" not in type(catalog[0]).model_fields


def test_root_safety_management_http_permissions_and_protection() -> None:
    from fastapi.testclient import TestClient

    create_user_with_personal_tenant("library-reader", "safe password")
    create_user_with_personal_tenant("library-admin", "safe password", role="super_admin")
    path = "/knowledge/knowledge_base/files/执行安全约束.md"
    with TestClient(app) as http:
        assert http.get("/library-catalog/knowledge_base/manifest").status_code == 401
        login = http.post("/auth/login", json={"login": "library-reader", "password": "safe password"})
        assert login.status_code == 200
        http.headers.update({"Origin": "http://testserver", "X-Cairn-CSRF-Token": login.json()["csrf_token"]})
        manifest = http.get("/library-catalog/knowledge_base/manifest")
        assert manifest.status_code == 200
        assert manifest.headers["cache-control"] == "no-store"
        assert manifest.json()["safety_path"] == "执行安全约束.md"
        assert "content" not in manifest.text
        assert http.get("/library-catalog/knowledge_base/manifest?version=1").status_code == 422
        assert http.put(path, json={"content": "不应写入"}).status_code == 403
        assert http.delete(path).status_code == 403
        assert http.put("/libraries/knowledge_base/files", json={"files": []}).status_code == 403
        login = http.post("/auth/login", json={"login": "library-admin", "password": "safe password"})
        assert login.status_code == 200
        http.headers["X-Cairn-CSRF-Token"] = login.json()["csrf_token"]
        content = "﻿# 隔离管理员正文\r\n保留原文。\r\n"
        assert http.put(path, json={"content": content}).status_code == 200
        assert http.get(path).json()["content"] == content
        assert http.delete(path).status_code == 409
        assert http.put("/libraries/knowledge_base/files", json={"files": []}).status_code == 409
        assert http.delete("/knowledge/knowledge_base").status_code == 409
        assert http.delete("/libraries/knowledge_base").status_code == 409
        assert http.get(path).json()["content"] == content
    with db.get_conn() as conn:
        assert list_library_versions(conn, "knowledge_base") == []
