from __future__ import annotations

import sqlite3

import pytest
from fastapi import HTTPException

from cairn.server import db
from cairn.server.services.report_templates import (
    archive_report_template,
    create_report_template,
    create_report_template_version,
    get_report_template_version,
    list_report_template_catalog,
    list_report_template_versions,
    report_template_digest,
)


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(db.SCHEMA)
    db._ensure_schema_migrations(conn)
    return conn


def test_default_report_templates_have_verified_immutable_versions() -> None:
    conn = make_conn()
    try:
        catalog = list_report_template_catalog(conn)

        assert [item.key for item in catalog] == [
            "full-technical",
            "management-summary",
            "standard-pentest",
        ]
        for item in catalog:
            detail = get_report_template_version(conn, item.key, item.version)
            assert detail.version == 1
            assert detail.digest_sha256 == report_template_digest(
                detail.markdown_content
            )
    finally:
        conn.close()


def test_report_template_versions_append_and_archive_without_deleting_history() -> None:
    conn = make_conn()
    try:
        created = create_report_template(conn, "custom-report", "自定义报告")
        assert created.latest_version is None

        first = create_report_template_version(
            conn,
            created.key,
            "# 第一版\n\n## 执行摘要\n",
            created_by="admin-1",
        )
        second = create_report_template_version(
            conn,
            created.key,
            "# 第二版\n\n## 执行摘要\n",
            message="补充章节",
            created_by="admin-1",
        )

        assert (first.version, second.version) == (1, 2)
        assert [item.version for item in list_report_template_versions(conn, created.key)] == [
            2,
            1,
        ]

        archive_report_template(conn, created.key)
        assert created.key not in {item.key for item in list_report_template_catalog(conn)}
        assert get_report_template_version(conn, created.key, 1).markdown_content.startswith(
            "# 第一版"
        )
        with pytest.raises(HTTPException) as archived:
            get_report_template_version(
                conn,
                created.key,
                2,
                allow_archived=False,
            )
        assert archived.value.status_code == 404
    finally:
        conn.close()
