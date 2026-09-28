from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException

from cairn.server import db, migrations
from cairn.server.schemas.libraries import LibraryFileWrite
from cairn.server.services.libraries import (
    archive_library,
    bind_library_revision,
    create_library,
    create_library_version,
    import_legacy_knowledge,
    list_library_revision_files,
    list_library_versions,
    list_selectable_current_libraries,
    library_storage_path,
    read_library_file,
    read_library_revision_content,
    resolve_current_library_revisions,
    remove_library_file,
    replace_library_file,
)


@pytest.fixture(autouse=True)
def isolate_library_content(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CAIRN_ADVISORY_LIBRARY_ROOT", str(tmp_path / "library-content"))


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(db.SCHEMA)
    db._ensure_schema_migrations(conn, initialize_runtime=False)
    migrations.SQLITE_OPTIONAL_PROJECT_RESOURCES.apply(conn)
    migrations.SQLITE_ADVISORY_LIBRARIES.apply(conn)
    migrations.SQLITE_ADVISORY_LIBRARY_BINDINGS.apply(conn)
    return conn


def test_v21_clears_live_scenario_knowledge_without_changing_project_snapshot() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(db.SCHEMA)
    db._ensure_schema_migrations(conn, initialize_runtime=False)
    migrations.SQLITE_OPTIONAL_PROJECT_RESOURCES.apply(conn)
    try:
        conn.execute("UPDATE scenarios SET knowledge = 'knowledge_base'")
        snapshot = '{"knowledge":"knowledge_base","prompt_group":"default"}'
        conn.execute(
            """INSERT INTO projects
               (id, title, status, created_at, scenario_snapshot_json)
               VALUES ('snapshot-project', 'Snapshot', 'active', '2026-08-19T00:00:00Z', ?)""",
            (snapshot,),
        )

        migrations.SQLITE_ADVISORY_LIBRARIES.apply(conn)

        assert conn.execute(
            "SELECT COUNT(*) AS count FROM scenarios WHERE knowledge IS NOT NULL"
        ).fetchone()["count"] == 0
        assert conn.execute(
            "SELECT scenario_snapshot_json FROM projects WHERE id = 'snapshot-project'"
        ).fetchone()["scenario_snapshot_json"] == snapshot
    finally:
        conn.close()


def test_library_versions_preserve_raw_credentials_and_scripts() -> None:
    conn = make_conn()
    try:
        create_library(conn, "operator_notes", "Operator notes")
        credentials = "username=admin\npassword=p@ssw0rd\napi_key=sk-live-secret\n"
        script = "#!/bin/sh\ncurl -u admin:p@ssw0rd https://target.invalid/\n"

        first = create_library_version(
            conn,
            "operator_notes",
            [
                LibraryFileWrite(path="credentials/.env", content=credentials),
                LibraryFileWrite(path="scripts/login.sh", content=script),
            ],
        )
        replace_library_file(conn, "operator_notes", "scripts/login.sh", script + "id\n")

        assert read_library_file(conn, "operator_notes", "credentials/.env", first.version).content == credentials
        assert read_library_file(conn, "operator_notes", "scripts/login.sh", first.version).content == script
        assert read_library_file(conn, "operator_notes", "scripts/login.sh").content == script + "id\n"
        assert [item.version for item in list_library_versions(conn, "operator_notes")] == [2, 1]
    finally:
        conn.close()


def test_library_content_tampering_is_detected() -> None:
    conn = make_conn()
    try:
        create_library(conn, "integrity")
        create_library_version(
            conn,
            "integrity",
            [LibraryFileWrite(path="credential.txt", content="password=exact\n")],
        )
        row = conn.execute(
            "SELECT storage_key FROM advisory_library_files WHERE library_key = 'integrity'"
        ).fetchone()
        library_storage_path(row["storage_key"]).write_bytes(b"tampered")

        with pytest.raises(RuntimeError, match="mismatch"):
            read_library_file(conn, "integrity", "credential.txt")
    finally:
        conn.close()


def test_removing_last_file_publishes_new_empty_version() -> None:
    conn = make_conn()
    try:
        create_library(conn, "temporary")
        create_library_version(conn, "temporary", [LibraryFileWrite(path="note", content="raw")])

        remove_library_file(conn, "temporary", "note")

        versions = list_library_versions(conn, "temporary")
        assert [item.version for item in versions] == [2, 1]
        assert versions[0].file_count == 0
        assert read_library_file(conn, "temporary", "note", 1).content == "raw"
    finally:
        conn.close()


def test_legacy_filesystem_import_is_exact_scoped_and_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    (root / "playbooks").mkdir(parents=True)
    (root / "README.md").write_text("根知识\n", encoding="utf-8")
    raw = "user=root\npassword=不得脱敏\n#!/bin/sh\necho ok\n"
    (root / "playbooks/access.conf").write_text(raw, encoding="utf-8")
    conn = make_conn()
    try:
        import_legacy_knowledge(conn, root)
        import_legacy_knowledge(conn, root)

        assert read_library_file(conn, "knowledge_base", "README.md").content == "根知识\n"
        assert read_library_file(conn, "playbooks", "access.conf").content == raw
        assert len(list_library_versions(conn, "knowledge_base")) == 1
        assert len(list_library_versions(conn, "playbooks")) == 1
    finally:
        conn.close()


def test_current_revision_resolution_dispatcher_reads_and_binding_protection() -> None:
    conn = make_conn()
    try:
        create_library(conn, "shared")
        assert list_selectable_current_libraries(conn) == []
        create_library_version(
            conn,
            "shared",
            [LibraryFileWrite(path="bin/login.py", content="password = 'raw-secret'\n")],
        )

        revision = resolve_current_library_revisions(conn, ["shared"])[0]
        assert [item.key for item in list_selectable_current_libraries(conn)] == ["shared"]
        assert [item.path for item in list_library_revision_files(conn, revision)] == ["bin/login.py"]
        assert read_library_revision_content(conn, revision, "bin/login.py") == b"password = 'raw-secret'\n"

        conn.execute(
            """INSERT INTO projects (id, title, status, created_at)
               VALUES ('project-1', 'Bound', 'active', '2026-08-19T00:00:00Z')"""
        )
        bind_library_revision(conn, "project-1", revision)
        with pytest.raises(HTTPException) as exc:
            archive_library(conn, "shared")
        assert exc.value.status_code == 409

        conn.execute("DELETE FROM projects WHERE id = 'project-1'")
        assert conn.execute(
            "SELECT 1 FROM project_advisory_library_bindings"
        ).fetchone() is None
        archive_library(conn, "shared")
    finally:
        conn.close()


def test_app_exposes_canonical_and_knowledge_compatibility_routes() -> None:
    from cairn.server.app import app

    paths = app.openapi()["paths"]
    assert "/libraries" in paths
    assert "/libraries/{key}/versions" in paths
    assert "/libraries/{key}/versions/{version}/files/{path}" in paths
    assert "/knowledge" in paths
    assert "/knowledge/{key}/files/{name}" in paths
