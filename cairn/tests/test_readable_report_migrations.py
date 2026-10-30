"""只验证本轮前向 PostgreSQL 迁移，不接触应用数据库。"""

from hashlib import sha256

import psycopg
import pytest

from cairn.server import migrations
from cairn.server.report_template_defaults_v60 import DEFAULT_REPORT_TEMPLATE_MARKDOWN_V60
from cairn.server.report_template_defaults_v62 import DEFAULT_REPORT_TEMPLATE_MARKDOWN_V62
from cairn.server.report_template_defaults_v69 import (
    DEFAULT_SINGLE_REPORT_TEMPLATE_KEY, DEFAULT_SINGLE_REPORT_TEMPLATE_MARKDOWN, SINGLE_REPORT_PROFILE,
)
from cairn.server.services import report_templates
from test_report_templates import defaults_v60_conn, private_postgres, _migrate_defaults_v60, _seed_project


@pytest.mark.parametrize("customized", [False, True])
def test_single_export_default_migration_isolated_from_custom_and_frozen(defaults_v60_conn, customized):
    conn = defaults_v60_conn
    _migrate_defaults_v60(conn, max_version=68)
    _seed_project(conn)
    if customized:
        content = DEFAULT_REPORT_TEMPLATE_MARKDOWN_V62 + "\n用户自己的默认正文。\n"
        conn.execute("UPDATE report_templates SET markdown_content = ?, digest_sha256 = ? WHERE key = 'vulnerability-report'",
                     (content, sha256(content.encode()).hexdigest()))
    conn.execute("""INSERT INTO report_templates
        (key, name, markdown_content, digest_sha256, created_at, updated_at)
        VALUES ('user-copy', '用户副本', ?, ?, 'before', 'before')""",
        (DEFAULT_REPORT_TEMPLATE_MARKDOWN_V62, sha256(DEFAULT_REPORT_TEMPLATE_MARKDOWN_V62.encode()).hexdigest()))
    conn.execute("""INSERT INTO assessment_report_finalizations
        (project_id, status, template_key, template_markdown, generation_started_at, created_at, updated_at)
        VALUES ('p1', 'ready', 'vulnerability-report', ?, 'before', 'before', 'before')""", (DEFAULT_REPORT_TEMPLATE_MARKDOWN_V62,))
    before = {row["key"]: dict(row) for row in conn.execute("SELECT * FROM report_templates").fetchall()}
    frozen = conn.execute("SELECT * FROM assessment_report_finalizations").fetchall()
    registry = conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
    migrations.verify_or_apply(conn, migrations.POSTGRES_SINGLE_REPORT_TEMPLATE, lambda _: None)
    after = {row["key"]: dict(row) for row in conn.execute("SELECT * FROM report_templates").fetchall()}
    assert all(after[key] == row for key, row in before.items())
    assert conn.execute("SELECT * FROM assessment_report_finalizations").fetchall() == frozen
    assert conn.execute("SELECT * FROM schema_migrations WHERE version <= 68 ORDER BY version").fetchall() == registry
    assert (DEFAULT_SINGLE_REPORT_TEMPLATE_KEY in after) is not customized
    if not customized:
        template = report_templates.get_report_template(conn, DEFAULT_SINGLE_REPORT_TEMPLATE_KEY)
        assert template.markdown_content == DEFAULT_SINGLE_REPORT_TEMPLATE_MARKDOWN
        assert template.supports_single_vulnerability
        assert template.compatibility_profiles == [SINGLE_REPORT_PROFILE]
        from fastapi import HTTPException
        with pytest.raises(HTTPException, match="assessment_report_template_incompatible"):
            report_templates.require_report_template_profile(conn, template.key, "assessment-report-packet-v1")
    assert report_templates.default_single_report_template_key(conn) == (
        "vulnerability-report" if customized else DEFAULT_SINGLE_REPORT_TEMPLATE_KEY
    )


def test_scoped_migration_preserves_history_and_refuses_inflight(defaults_v60_conn):
    conn = defaults_v60_conn
    _migrate_defaults_v60(conn)
    _seed_project(conn)
    conn.execute(
        """INSERT INTO assessment_report_finalizations
        (project_id, status, generation_token, generation_started_at, created_at, updated_at)
        VALUES ('p1', 'pending', 'historic-generation', 'historic', 'historic', 'historic')"""
    )
    conn.commit()
    with pytest.raises(psycopg.errors.RaiseException, match="outstanding_generation"):
        migrations.verify_or_apply(conn, migrations.POSTGRES_REPORT_SCOPES, lambda _: None)
    conn.rollback()
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE version = 61").fetchone() is None
    assert conn.execute("SELECT status FROM assessment_report_finalizations").fetchone()["status"] == "pending"
    # 测试自己的历史行进入终态后才能升级，迁移本身不得替它取消或清空请求。
    conn.execute("UPDATE assessment_report_finalizations SET status = 'ready'")
    before = dict(conn.execute("SELECT * FROM assessment_report_finalizations").fetchone())
    migrations.verify_or_apply(conn, migrations.POSTGRES_REPORT_SCOPES, lambda _: None)
    after = dict(conn.execute("SELECT * FROM assessment_report_finalizations").fetchone())
    assert all(after[key] == value for key, value in before.items())
    assert after["scope_key"] == ""
    assert after["accepted_generation_token"] == "historic-generation"
    for key in ("vulnerability:first", "vulnerability:second"):
        conn.execute(
            """INSERT INTO assessment_report_finalizations
            (project_id, scope_key, vulnerability_id, status, generation_token,
             generation_started_at, created_at, updated_at)
            VALUES ('p1', ?, ?, 'pending', ?, 'now', 'now', 'now')""",
            (key, key, key),
        )
    assert conn.execute("SELECT count(*) AS n FROM assessment_report_finalizations").fetchone()["n"] == 3
    assert conn.execute("SELECT status FROM projects WHERE id = 'p1'").fetchone()["status"] == "completed"


def test_key_evidence_defaults_upgrade_only_unchanged_seed_bodies(defaults_v60_conn):
    conn = defaults_v60_conn
    _migrate_defaults_v60(conn)
    edited = DEFAULT_REPORT_TEMPLATE_MARKDOWN_V60 + "\n用户修改。\n"
    conn.execute(
        "UPDATE report_templates SET markdown_content = ?, digest_sha256 = ? WHERE key = 'standard-pentest'",
        (edited, sha256(edited.encode()).hexdigest()),
    )
    # 用户副本的正文即使逐字相同也不是系统种子。
    conn.execute(
        """INSERT INTO report_templates
        (key, name, markdown_content, digest_sha256, created_at, updated_at)
        VALUES ('user-copy', '用户副本', ?, ?, 'before', 'before')""",
        (DEFAULT_REPORT_TEMPLATE_MARKDOWN_V60, sha256(DEFAULT_REPORT_TEMPLATE_MARKDOWN_V60.encode()).hexdigest()),
    )
    before = {row["key"]: dict(row) for row in conn.execute("SELECT * FROM report_templates").fetchall()}
    registry = conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
    frozen_v60 = migrations.POSTGRES_REPORT_TEMPLATE_DEFAULTS.checksum
    migrations.verify_or_apply(conn, migrations.POSTGRES_READABLE_REPORT_TEMPLATE_DEFAULTS, lambda _: None)
    after = {row["key"]: dict(row) for row in conn.execute("SELECT * FROM report_templates").fetchall()}
    assert after["user-copy"] == before["user-copy"]
    assert after["standard-pentest"] == before["standard-pentest"]
    assert after["vulnerability-report"]["markdown_content"] == DEFAULT_REPORT_TEMPLATE_MARKDOWN_V62
    assert after["vulnerability-report"]["digest_sha256"] == sha256(DEFAULT_REPORT_TEMPLATE_MARKDOWN_V62.encode()).hexdigest()
    assert "finding.key_evidence" in DEFAULT_REPORT_TEMPLATE_MARKDOWN_V62
    assert "evidence.content" not in DEFAULT_REPORT_TEMPLATE_MARKDOWN_V62
    assert migrations.POSTGRES_REPORT_TEMPLATE_DEFAULTS.checksum == frozen_v60
    assert conn.execute("SELECT * FROM schema_migrations WHERE version <= 60 ORDER BY version").fetchall() == registry
