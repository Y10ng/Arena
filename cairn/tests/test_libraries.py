"""当前文件／实际使用资料的隔离 PostgreSQL 回归。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from cairn.server import db, migrations
from cairn.server.schemas.libraries import LibraryFileWrite, LibrarySelection
from cairn.server.services import libraries as service
from test_postgres_bootstrap import postgres_instance, empty_postgres_url

pytestmark = pytest.mark.skipif(
    os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1",
    reason="需显式启用隔离 PostgreSQL",
)


@pytest.fixture
def configured(empty_postgres_url, tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_ADVISORY_LIBRARY_ROOT", str(tmp_path / "content"))
    db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")
    yield
    db._reset_for_tests()


def choice(conn, key, paths=None):
    manifest = service.get_selectable_library_manifest(conn, key)
    return LibrarySelection(library_key=key, source="current", paths=paths,
        expected_files={f.path: f.sha256 for f in manifest.files if paths is None or f.path in paths})


def resolve(conn, selections, **kwargs):
    return service.resolve_library_selections(conn, selections, tenant_id="fixture", user_id="fixture", **kwargs)


def test_current_edits_do_not_publish_history_and_use_snapshot_is_subset(configured):
    with db.get_conn() as conn:
        service.create_library(conn, "notes")
        raw = "﻿user=fixture\r\npassword=fixture-only\r\n  # 原文\n"
        service.replace_library_file(conn, "notes", "a.txt", raw)
        service.replace_library_file(conn, "notes", "other.txt", "one")
        selected = choice(conn, "notes", ["a.txt"])
        service.replace_library_file(conn, "notes", "other.txt", "two")
        assert service.list_library_versions(conn, "notes") == []
        assert service.read_library_file(conn, "notes", "a.txt").content == raw
        revision = resolve(conn, [selected])[0]
        assert [f.path for f in service.list_library_files(conn, "notes", revision.version)] == ["a.txt"]
        assert service.read_library_revision_content(conn, revision, "a.txt") == raw.encode()
        service.replace_library_file(conn, "notes", "a.txt", "changed")
        service.remove_library_file(conn, "notes", "other.txt")
        assert len(service.list_library_versions(conn, "notes")) == 1
        assert service.read_library_revision_content(conn, revision, "a.txt") == raw.encode()
        assert service.get_selectable_library_manifest(conn, "notes").files[0].sha256 != selected.expected_files["a.txt"]


def test_selection_conflicts_and_saved_scope(configured):
    with db.get_conn() as conn:
        service.create_library(conn, "checks")
        service.replace_library_file(conn, "checks", "a", "one")
        whole = choice(conn, "checks")
        subset = choice(conn, "checks", ["a"])
        service.replace_library_file(conn, "checks", "b", "two")
        with pytest.raises(HTTPException) as conflict:
            resolve(conn, [whole])
        assert conflict.value.status_code == 409
        revision = resolve(conn, [subset])[0]
        saved = LibrarySelection(library_key="checks", source="saved", paths=None)
        with pytest.raises(HTTPException) as denied:
            resolve(conn, [saved])
        assert denied.value.status_code == 403
        assert resolve(conn, [saved], existing=[revision]) == [revision]
        with pytest.raises(HTTPException):
            resolve(conn, [saved.model_copy(update={"paths": ["b"]})], existing=[revision])
        service.replace_library_file(conn, "checks", "a", "changed")
        with pytest.raises(HTTPException) as changed:
            resolve(conn, [subset])
        assert changed.value.status_code == 409
        assert resolve(conn, [saved], existing=[revision]) == [revision]
        with pytest.raises(ValidationError):
            LibrarySelection(library_key="checks", version=revision.version, proof="old", paths=None)


def test_root_initialization_and_common_write_protection(configured):
    with db.get_conn() as conn:
        root, path = service.ROOT_LIBRARY_KEY, service.SAFETY_FILE_PATH
        assert service.read_library_file(conn, root, path).content.startswith("# 执行安全约束")
        assert service.list_library_versions(conn, root) == []
        raw = "﻿管理员原文\r\n  保留空格\n"
        service.replace_library_file(conn, root, path, raw)
        service.initialize_root_library(conn)
        service.initialize_root_library(conn)
        assert service.read_library_file(conn, root, path).content == raw
        for operation in (
            lambda: service.remove_library_file(conn, root, path),
            lambda: service.replace_library_files(conn, root, []),
            lambda: service.archive_library(conn, root),
        ):
            with pytest.raises(HTTPException) as blocked:
                operation()
            assert blocked.value.status_code == 409
        service.create_library(conn, "ordinary")
        service.replace_library_file(conn, "ordinary", path, "not protected")
        service.remove_library_file(conn, "ordinary", path)
        service.archive_library(conn, "ordinary")
        assert service.read_library_file(conn, root, path).content == raw


def test_legacy_import_never_overwrites_database_owned_libraries(configured, tmp_path):
    root = tmp_path / "legacy"
    (root / "legacy_notes").mkdir(parents=True)
    (root / "legacy_notes/a.txt").write_text("disk", encoding="utf-8")
    with db.get_conn() as conn:
        service.import_legacy_knowledge(conn, root)
        service.replace_library_file(conn, "legacy_notes", "a.txt", "database")
        service.import_legacy_knowledge(conn, root)
        assert service.read_library_file(conn, "legacy_notes", "a.txt").content == "database"
        service.remove_library_file(conn, "legacy_notes", "a.txt")
        service.import_legacy_knowledge(conn, root)
        assert service.get_selectable_library_manifest(conn, "legacy_notes").files == []
        assert service.list_library_versions(conn, "legacy_notes") == []


def test_snapshot_integrity_and_transaction_rollback(configured):
    with db.get_conn() as conn:
        service.create_library(conn, "integrity")
        service.replace_library_file(conn, "integrity", "note", "exact")
    with pytest.raises(RuntimeError, match="fixture rollback"):
        with db.get_conn() as conn:
            resolve(conn, [choice(conn, "integrity")])
            raise RuntimeError("fixture rollback")
    with db.get_conn() as conn:
        assert service.list_library_versions(conn, "integrity") == []
        revision = resolve(conn, [choice(conn, "integrity")])[0]
        row = conn.execute("SELECT storage_key FROM advisory_library_files WHERE library_key = 'integrity'").fetchone()
        service.library_storage_path(row["storage_key"]).write_bytes(b"tampered")
        with pytest.raises(RuntimeError, match="mismatch"):
            service.read_library_revision_content(conn, revision, "note")


def test_forward_migration_preserves_latest_raw_content_and_old_snapshots(empty_postgres_url, tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_ADVISORY_LIBRARY_ROOT", str(tmp_path / "content"))
    current_migrations = migrations.POSTGRES_MIGRATIONS
    # 只搭建旧 schema；不运行依赖新当前表的安装初始化。
    with monkeypatch.context() as patch:
        patch.setattr(db, "POSTGRES_MIGRATIONS", tuple(m for m in current_migrations if m.version < 82))
        patch.setattr(service, "initialize_root_library", lambda conn: None)
        db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")
    with db.get_conn() as conn:
        conn.execute("INSERT INTO advisory_libraries(key,name,created_at,archived_at) VALUES ('archived','Archived','2026-01-01','2026-02-01')")
        raw = "﻿旧原文\r\npassword=fixture-only\n"
        content = raw.encode()
        storage_key = service._store_content(content)
        digest = service._library_digest({"note": content})
        conn.execute("INSERT INTO advisory_library_versions(library_key,version,digest_sha256,source,created_at) VALUES ('archived',7,?,'api','2026-01-01')", (digest,))
        conn.execute("INSERT INTO advisory_library_files(library_key,version,path,storage_key,size_bytes,sha256) VALUES ('archived',7,'note',?,?,?)", (storage_key, len(content), storage_key))
    with db.get_conn() as conn:
        db.run_migrations(conn, "postgresql", initialize_runtime=True)
    with db.get_conn() as conn:
        row = conn.execute("SELECT content FROM advisory_library_current_files WHERE library_key='archived'").fetchone()
        assert row["content"] == raw
        assert service.get_library(conn, "archived", include_archived=True).archived_at == "2026-02-01"
        assert service.read_library_file(conn, "archived", "note", 7).content == raw
        assert service.read_library_file(conn, service.ROOT_LIBRARY_KEY, service.SAFETY_FILE_PATH).content
        assert len(service.list_library_versions(conn, "archived")) == 1


def test_management_routes_no_longer_publish_edit_versions():
    from cairn.server.app import app
    paths = app.openapi()["paths"]
    assert "/libraries/{key}/files" in paths
    assert "/libraries/{key}/versions" not in paths
    assert "/knowledge/{key}/files/{name}" in paths
