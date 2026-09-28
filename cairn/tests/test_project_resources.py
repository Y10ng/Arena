from __future__ import annotations

import asyncio
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import zipfile

from fastapi import HTTPException, Request
from pydantic import ValidationError
import pytest

from cairn.server import database_admin, db
from cairn.server.app import app
from route_helpers import iter_app_routes
from cairn.server.auth import (
    RequestPrincipal,
    require_tenant_project,
    require_user_tenant_project,
)
from cairn.server.models import (
    CreateProjectResourceRequest,
    LibraryFileWrite,
    LibraryRevisionRef,
)
from cairn.server.routers.project_resources import (
    create_project_resource,
    list_project_advisory_library_files,
    read_project_advisory_library_file,
    read_project_resource,
    upload_resource_batch_file,
)
from cairn.server.services import project_resources
from cairn.server.services.libraries import (
    bind_library_revision,
    create_library,
    create_library_version,
)
from cairn.server.services.project_resources import (
    cleanup_expired_project_resource_batches,
    commit_project_resource_batch,
    create_project_resource_batch,
    create_project_resource_record,
    deactivate_project_resource_record,
    get_project_resource_batch,
    import_project_resource_batch_zip,
    list_project_resource_records,
    project_resource_config,
    read_project_resource_content,
    upload_project_resource_batch_file,
)


@pytest.fixture(autouse=True)
def configured_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db._reset_for_tests()
    database_path = tmp_path / "resources.db"
    resource_root = tmp_path / "project-resources"
    monkeypatch.setenv("CAIRN_PROJECT_RESOURCE_ROOT", str(resource_root))
    monkeypatch.setenv(
        "CAIRN_ADVISORY_LIBRARY_ROOT",
        str(tmp_path / "advisory-library-content"),
    )
    db.configure(database_path)
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO users (
                id, login, password_hash, role, must_change_password, created_at, updated_at
            ) VALUES (
                'usr_owner', 'resource-owner', 'unused', 'user', 0,
                '2026-08-19T00:00:00Z', '2026-08-19T00:00:00Z'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO tenant_memberships (tenant_id, user_id, role, created_at)
            VALUES ('tenant_legacy', 'usr_owner', 'owner', '2026-08-19T00:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO projects (id, title, status, created_at, tenant_id)
            VALUES ('proj_resources', 'Resources', 'stopped', '2026-08-19T00:00:00Z', 'tenant_legacy')
            """
        )
    yield resource_root
    db._reset_for_tests()


def _body(
    *,
    path: str = "scripts/login.sh",
    content: str = "#!/bin/sh\nprintf ' token=%s  \\n' \"$TOKEN\"\n",
    kind: str = "script",
) -> CreateProjectResourceRequest:
    return CreateProjectResourceRequest(kind=kind, path=path, content=content)


def test_create_list_and_raw_content_preserve_exact_utf8_bytes(
    configured_database: Path,
) -> None:
    content = "  #!/usr/bin/env python3\n密钥 = 'secret-value'\n\n"

    created = create_project_resource_record(
        "proj_resources",
        _body(path="helpers/login.py", content=content),
        created_by="usr_owner",
    )

    expected = content.encode("utf-8")
    assert created.id == "r001"
    assert created.path == "helpers/login.py"
    assert created.kind == "script"
    assert created.created_by == "usr_owner"
    assert created.size_bytes == len(expected)
    assert created.sha256 == sha256(expected).hexdigest()
    assert created.active is True
    assert (configured_database / "proj_resources" / "r001").read_bytes() == expected
    assert not (configured_database / "proj_resources" / "helpers").exists()

    listed = list_project_resource_records("proj_resources")
    assert listed == [created]
    assert "content" not in listed[0].model_dump()

    raw = read_project_resource_content("proj_resources", "r001")
    assert raw.content == expected
    assert raw.path == "helpers/login.py"
    response = read_project_resource("proj_resources", "r001")
    assert response.body == expected
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-type"] == "application/octet-stream"


def test_resource_paths_are_structural_only_and_allow_all_extensions() -> None:
    for path in ("run", "scripts/check.sh", "tools/check.js", "payloads/test.py", "notes/a.b.c"):
        assert _body(path=path).path == path

    invalid = (
        "",
        " ",
        "/absolute.sh",
        "../escape.sh",
        "safe/../escape.sh",
        "safe//file.sh",
        "safe\\file.sh",
        "C:/windows/file.txt",
        "manifest.json",
        "safe/MANIFEST.JSON",
        "safe/\x00file",
    )
    for path in invalid:
        with pytest.raises(ValidationError):
            _body(path=path)


def test_mutations_require_stopped_project_without_active_task_run() -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET status = 'active' WHERE id = 'proj_resources'")
    with pytest.raises(HTTPException, match="only be changed") as active_error:
        create_project_resource_record(
            "proj_resources", _body(), created_by="usr_owner"
        )
    assert active_error.value.status_code == 409

    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET status = 'stopped' WHERE id = 'proj_resources'")
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, task_type, worker_name, worker_type, status
            ) VALUES ('tr001', 'proj_resources', 'explore', 'worker', 'claudecode', 'queued')
            """
        )
    with pytest.raises(HTTPException, match="queued or running") as run_error:
        create_project_resource_record(
            "proj_resources", _body(), created_by="usr_owner"
        )
    assert run_error.value.status_code == 409


def test_size_count_total_and_duplicate_path_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(project_resources, "PROJECT_RESOURCE_MAX_FILE_BYTES", 4)
    with pytest.raises(HTTPException) as file_error:
        create_project_resource_record(
            "proj_resources", _body(path="large", content="12345"), created_by="usr_owner"
        )
    assert file_error.value.status_code == 413

    monkeypatch.setattr(project_resources, "PROJECT_RESOURCE_MAX_FILE_BYTES", 10)
    monkeypatch.setattr(project_resources, "PROJECT_RESOURCE_MAX_COUNT", 2)
    monkeypatch.setattr(project_resources, "PROJECT_RESOURCE_MAX_TOTAL_BYTES", 5)
    create_project_resource_record(
        "proj_resources", _body(path="one", content="12"), created_by="usr_owner"
    )
    with pytest.raises(HTTPException, match="already exists") as duplicate_error:
        create_project_resource_record(
            "proj_resources", _body(path="one", content="x"), created_by="usr_owner"
        )
    assert duplicate_error.value.status_code == 409

    create_project_resource_record(
        "proj_resources", _body(path="two", content="123"), created_by="usr_owner"
    )
    with pytest.raises(HTTPException, match="maximum") as count_error:
        create_project_resource_record(
            "proj_resources", _body(path="three", content=""), created_by="usr_owner"
        )
    assert count_error.value.status_code == 409

    deactivate_project_resource_record("proj_resources", "r002")
    with pytest.raises(HTTPException, match="total limit") as total_error:
        create_project_resource_record(
            "proj_resources", _body(path="three", content="1234"), created_by="usr_owner"
        )
    assert total_error.value.status_code == 413


def test_soft_deactivate_hides_metadata_but_preserves_raw_content(
    configured_database: Path,
) -> None:
    created = create_project_resource_record(
        "proj_resources",
        _body(path="notes/operator.txt", content="credential=keep-me\n", kind="note"),
        created_by="usr_owner",
    )

    deactivate_project_resource_record("proj_resources", created.id)

    assert list_project_resource_records("proj_resources") == []
    assert read_project_resource_content("proj_resources", created.id).content == b"credential=keep-me\n"
    assert (configured_database / "proj_resources" / created.id).is_file()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT active, deactivated_at FROM project_resources WHERE id = ? AND project_id = ?",
            (created.id, "proj_resources"),
        ).fetchone()
    assert row["active"] == 0
    assert row["deactivated_at"] is not None


def test_dispatcher_reads_only_the_project_frozen_library_revision() -> None:
    content = "﻿username=admin\npassword=allowed-secret\n"
    with db.get_conn() as conn:
        create_library(conn, "runbook", "Runbook")
        revision = create_library_version(
            conn,
            "runbook",
            [LibraryFileWrite(path="auth/login.txt", content=content)],
        )
        bind_library_revision(
            conn,
            "proj_resources",
            LibraryRevisionRef(
                library_key="runbook",
                version=revision.version,
                digest_sha256=revision.digest_sha256,
            ),
        )
        create_library_version(
            conn,
            "runbook",
            [LibraryFileWrite(path="auth/login.txt", content="new version")],
        )

    files = list_project_advisory_library_files(
        "proj_resources",
        "runbook",
        revision.version,
    )
    response = read_project_advisory_library_file(
        "proj_resources",
        "runbook",
        revision.version,
        "auth/login.txt",
    )

    assert [item.path for item in files] == ["auth/login.txt"]
    assert response.body == content.encode("utf-8")
    with pytest.raises(HTTPException) as unbound_error:
        list_project_advisory_library_files(
            "proj_resources",
            "runbook",
            revision.version + 1,
        )
    assert unbound_error.value.status_code == 404


async def _byte_stream(content: bytes):
    for offset in range(0, len(content), 3):
        yield content[offset : offset + 3]


def _zip_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    target = BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in entries:
            archive.writestr(path, content)
    return target.getvalue()


def test_resource_config_exposes_approved_defaults_and_file_kind() -> None:
    config = project_resource_config()

    assert config.kinds == ["note", "script", "file"]
    assert config.limits.max_active_count == 200
    assert config.limits.max_file_bytes == 10 * 1024 * 1024
    assert config.limits.max_active_total_bytes == 100 * 1024 * 1024
    assert config.limits.max_zip_upload_bytes == 50 * 1024 * 1024
    assert config.limits.max_open_batches_per_user == 4
    assert config.limits.max_staged_bytes_per_user == 200 * 1024 * 1024
    assert config.zip_policy["nested_archives"] == "opaque"


def test_batch_creation_and_cross_batch_staging_are_user_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(project_resources, "PROJECT_RESOURCE_MAX_OPEN_BATCHES_PER_USER", 2)
    monkeypatch.setattr(project_resources, "PROJECT_RESOURCE_MAX_STAGED_BYTES_PER_USER", 5)
    first = create_project_resource_batch(
        tenant_id="tenant_legacy",
        user_id="usr_owner",
    )
    second = create_project_resource_batch(
        tenant_id="tenant_legacy",
        user_id="usr_owner",
    )

    with pytest.raises(HTTPException, match="2 open") as batch_error:
        create_project_resource_batch(
            tenant_id="tenant_legacy",
            user_id="usr_owner",
        )
    assert batch_error.value.status_code == 409

    asyncio.run(
        upload_project_resource_batch_file(
            first.id,
            _byte_stream(b"123"),
            path="first.bin",
            kind=None,
            tenant_id="tenant_legacy",
            user_id="usr_owner",
        )
    )
    with pytest.raises(HTTPException, match="5-byte") as staged_error:
        asyncio.run(
            upload_project_resource_batch_file(
                second.id,
                _byte_stream(b"456"),
                path="second.bin",
                kind=None,
                tenant_id="tenant_legacy",
                user_id="usr_owner",
            )
        )
    assert staged_error.value.status_code == 413
    assert get_project_resource_batch(
        second.id,
        tenant_id="tenant_legacy",
        user_id="usr_owner",
    ).members == []


def test_raw_upload_router_rejects_unsafe_path_as_client_error() -> None:
    principal = RequestPrincipal(
        kind="user",
        subject_id="usr_owner",
        tenant_id="tenant_legacy",
        role="user",
    )
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            upload_resource_batch_file(
                "unused",
                request,
                path="../escape.bin",
                kind=None,
                principal=principal,
            )
        )

    assert error.value.status_code == 422


def test_raw_binary_batch_is_owner_scoped_and_commits_atomically(
    configured_database: Path,
) -> None:
    batch = create_project_resource_batch(
        tenant_id="tenant_legacy",
        user_id="usr_owner",
    )
    content = b"\x00\xff\x80binary\n"

    member = asyncio.run(
        upload_project_resource_batch_file(
            batch.id,
            _byte_stream(content),
            path="payloads/blob.bin",
            kind="file",
            tenant_id="tenant_legacy",
            user_id="usr_owner",
        )
    )

    assert member.kind == "file"
    assert member.size_bytes == len(content)
    assert member.sha256 == sha256(content).hexdigest()
    with pytest.raises(HTTPException) as hidden:
        get_project_resource_batch(
            batch.id,
            tenant_id="tenant_legacy",
            user_id="usr_other",
        )
    assert hidden.value.status_code == 404

    resources = commit_project_resource_batch(
        "proj_resources",
        batch.id,
        tenant_id="tenant_legacy",
        user_id="usr_owner",
        created_by="usr_owner",
    )

    assert [resource.path for resource in resources] == ["payloads/blob.bin"]
    assert read_project_resource_content("proj_resources", resources[0].id).content == content
    assert not (configured_database / ".batches" / batch.id).exists()
    consumed = get_project_resource_batch(
        batch.id,
        tenant_id="tenant_legacy",
        user_id="usr_owner",
    )
    assert consumed.state == "consumed"


def test_super_admin_can_commit_owned_batch_to_authorized_cross_tenant_project() -> None:
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO tenants (id, kind, name, created_at)
            VALUES ('tenant_other', 'personal', 'Other tenant', '2026-08-19T00:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO projects (id, title, status, created_at, tenant_id)
            VALUES ('proj_other', 'Other', 'stopped', '2026-08-19T00:00:00Z', 'tenant_other')
            """
        )
    batch = create_project_resource_batch(
        tenant_id="tenant_legacy",
        user_id="usr_owner",
    )
    asyncio.run(
        upload_project_resource_batch_file(
            batch.id,
            _byte_stream(b"admin upload"),
            path="docs/admin.txt",
            kind=None,
            tenant_id="tenant_legacy",
            user_id="usr_owner",
        )
    )

    with pytest.raises(HTTPException) as denied:
        commit_project_resource_batch(
            "proj_other",
            batch.id,
            tenant_id="tenant_legacy",
            user_id="usr_owner",
            created_by="usr_owner",
        )
    assert denied.value.status_code == 404

    resources = commit_project_resource_batch(
        "proj_other",
        batch.id,
        tenant_id="tenant_legacy",
        user_id="usr_owner",
        created_by="usr_owner",
        allow_cross_tenant=True,
    )
    assert [(resource.path, resource.kind) for resource in resources] == [
        ("docs/admin.txt", "note")
    ]


def test_batch_upload_infers_resource_kind_when_kind_is_omitted() -> None:
    batch = create_project_resource_batch(
        tenant_id="tenant_legacy",
        user_id="usr_owner",
    )

    members = [
        asyncio.run(
            upload_project_resource_batch_file(
                batch.id,
                _byte_stream(content),
                path=path,
                kind=None,
                tenant_id="tenant_legacy",
                user_id="usr_owner",
            )
        )
        for path, content in (
            ("scripts/check.py", b"print('ok')\n"),
            ("docs/readme.md", b"# Readme\n"),
            ("images/logo.png", b"\x89PNG\r\n\x1a\n"),
        )
    ]

    assert [(member.path, member.kind) for member in members] == [
        ("scripts/check.py", "script"),
        ("docs/readme.md", "note"),
        ("images/logo.png", "file"),
    ]


def test_zip_preflight_rejects_traversal_without_partial_members() -> None:
    batch = create_project_resource_batch(
        tenant_id="tenant_legacy",
        user_id="usr_owner",
    )
    archive = _zip_bytes([
        ("safe/one.txt", b"one"),
        ("../escape.txt", b"escape"),
    ])

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            import_project_resource_batch_zip(
                batch.id,
                _byte_stream(archive),
                tenant_id="tenant_legacy",
                user_id="usr_owner",
            )
        )

    assert error.value.status_code == 400
    refreshed = get_project_resource_batch(
        batch.id,
        tenant_id="tenant_legacy",
        user_id="usr_owner",
    )
    assert refreshed.members == []


def test_batch_rejects_file_directory_ancestor_collisions() -> None:
    batch = create_project_resource_batch(
        tenant_id="tenant_legacy",
        user_id="usr_owner",
    )
    asyncio.run(
        upload_project_resource_batch_file(
            batch.id,
            _byte_stream(b"file"),
            path="tools",
            kind="file",
            tenant_id="tenant_legacy",
            user_id="usr_owner",
        )
    )

    with pytest.raises(HTTPException, match="conflicts"):
        asyncio.run(
            upload_project_resource_batch_file(
                batch.id,
                _byte_stream(b"child"),
                path="tools/run.sh",
                kind="script",
                tenant_id="tenant_legacy",
                user_id="usr_owner",
            )
        )
    assert get_project_resource_batch(
        batch.id,
        tenant_id="tenant_legacy",
        user_id="usr_owner",
    ).resource_count == 1


def test_zip_entry_limit_counts_directory_records(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(project_resources, "PROJECT_RESOURCE_MAX_ZIP_ENTRIES", 2)
    batch = create_project_resource_batch(
        tenant_id="tenant_legacy",
        user_id="usr_owner",
    )
    target = BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("one/", b"")
        archive.writestr("two/", b"")
        archive.writestr("three/", b"")

    with pytest.raises(HTTPException, match="more than 2 entries"):
        asyncio.run(
            import_project_resource_batch_zip(
                batch.id,
                _byte_stream(target.getvalue()),
                tenant_id="tenant_legacy",
                user_id="usr_owner",
            )
        )


def test_zip_preflight_rejects_symlinks_and_high_compression_ratio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = create_project_resource_batch(
        tenant_id="tenant_legacy",
        user_id="usr_owner",
    )
    target = BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        link = zipfile.ZipInfo("link")
        link.create_system = 3
        link.external_attr = (0o120777 << 16)
        archive.writestr(link, "target")
    with pytest.raises(HTTPException, match="special files"):
        asyncio.run(
            import_project_resource_batch_zip(
                batch.id,
                _byte_stream(target.getvalue()),
                tenant_id="tenant_legacy",
                user_id="usr_owner",
            )
        )

    monkeypatch.setattr(project_resources, "PROJECT_RESOURCE_MAX_COMPRESSION_RATIO", 2)
    compressed = _zip_bytes([("repeated.txt", b"a" * 4096)])
    with pytest.raises(HTTPException, match="compression ratio"):
        asyncio.run(
            import_project_resource_batch_zip(
                batch.id,
                _byte_stream(compressed),
                tenant_id="tenant_legacy",
                user_id="usr_owner",
            )
        )
    assert get_project_resource_batch(
        batch.id,
        tenant_id="tenant_legacy",
        user_id="usr_owner",
    ).resource_count == 0


def test_zip_import_preserves_paths_and_rejects_casefold_collisions() -> None:
    batch = create_project_resource_batch(
        tenant_id="tenant_legacy",
        user_id="usr_owner",
    )
    archive = _zip_bytes([
        ("docs/readme.txt", b"readme"),
        ("bin/tool.dat", b"\x00\x01"),
    ])

    imported = asyncio.run(
        import_project_resource_batch_zip(
            batch.id,
            _byte_stream(archive),
            tenant_id="tenant_legacy",
            user_id="usr_owner",
        )
    )
    assert [(member.path, member.kind) for member in imported.members] == [
        ("bin/tool.dat", "file"),
        ("docs/readme.txt", "note"),
    ]

    colliding = _zip_bytes([("DOCS/README.TXT", b"duplicate")])
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            import_project_resource_batch_zip(
                batch.id,
                _byte_stream(colliding),
                tenant_id="tenant_legacy",
                user_id="usr_owner",
            )
        )
    assert error.value.status_code == 409
    assert get_project_resource_batch(
        batch.id,
        tenant_id="tenant_legacy",
        user_id="usr_owner",
    ).resource_count == 2


def test_expired_batch_cleanup_removes_rows_and_staged_files(
    configured_database: Path,
) -> None:
    batch = create_project_resource_batch(
        tenant_id="tenant_legacy",
        user_id="usr_owner",
    )
    asyncio.run(
        upload_project_resource_batch_file(
            batch.id,
            _byte_stream(b"temporary"),
            path="temporary.bin",
            kind="file",
            tenant_id="tenant_legacy",
            user_id="usr_owner",
        )
    )
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE project_resource_batches SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (batch.id,),
        )

    assert cleanup_expired_project_resource_batches() == 1
    assert not (configured_database / ".batches" / batch.id).exists()
    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT 1 FROM project_resource_batches WHERE id = ?", (batch.id,)
        ).fetchone() is None


def test_router_registration_uses_tenant_read_and_user_mutation_auth() -> None:
    assert "project_resources" in database_admin.APPLICATION_TABLES
    routes = {
        (route.path, method): route
        for route in iter_app_routes(app)
        for method in (getattr(route, "methods", None) or ())
    }
    list_route = routes[("/projects/{project_id}/resources", "GET")]
    raw_route = routes[("/projects/{project_id}/resources/{resource_id}", "GET")]
    library_list_route = routes[
        (
            "/projects/{project_id}/advisory-libraries/{key}/versions/{version}/files",
            "GET",
        )
    ]
    library_read_route = routes[
        (
            "/projects/{project_id}/advisory-libraries/{key}/versions/{version}/files/{path:path}",
            "GET",
        )
    ]
    create_route = routes[("/projects/{project_id}/resources", "POST")]
    delete_route = routes[("/projects/{project_id}/resources/{resource_id}", "DELETE")]

    assert {dependency.call for dependency in list_route.dependant.dependencies} == {
        require_tenant_project
    }
    assert {dependency.call for dependency in raw_route.dependant.dependencies} == {
        require_tenant_project
    }
    assert {
        dependency.call for dependency in library_list_route.dependant.dependencies
    } == {require_tenant_project}
    assert {
        dependency.call for dependency in library_read_route.dependant.dependencies
    } == {require_tenant_project}
    assert {dependency.call for dependency in create_route.dependant.dependencies} == {
        require_user_tenant_project
    }
    assert {dependency.call for dependency in delete_route.dependant.dependencies} == {
        require_user_tenant_project
    }


def test_router_upload_uses_authenticated_user_as_creator() -> None:
    principal = RequestPrincipal(
        kind="user",
        subject_id="usr_authenticated",
        tenant_id="tenant_legacy",
        role="user",
    )

    created = create_project_resource(
        "proj_resources",
        _body(path="login", content="token=allowed"),
        principal,
    )

    assert created.created_by == "usr_authenticated"
    assert read_project_resource_content("proj_resources", created.id).content == b"token=allowed"
