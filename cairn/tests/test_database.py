from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from cairn.server import db, migrations
from cairn.server.db_defaults import (
    DEFAULT_COPY_V2_GOAL_TEMPLATE,
    DEFAULT_COPY_V2_ORIGIN_TEMPLATE,
)
from cairn.server.migrations import (
    AI_AGENT_SECURITY_GOAL_TEMPLATE_V30 as AI_AGENT_SECURITY_GOAL_TEMPLATE,
    AI_AGENT_SECURITY_ORIGIN_TEMPLATE_V30 as AI_AGENT_SECURITY_ORIGIN_TEMPLATE,
)
from cairn.server.schemas.projects import CreateProjectRequest
from cairn.server.services.assessment_policy import policy_from_project_row
from cairn.server.services.project_records import create_project_record, get_project_detail


@pytest.fixture(autouse=True)
def reset_database_configuration():
    db._reset_for_tests()
    yield
    db._reset_for_tests()


def test_configure_initializes_database_and_separate_artifact_root(tmp_path: Path) -> None:
    database_path = tmp_path / "state" / "cairn.db"
    artifacts_path = tmp_path / "evidence"

    db.configure(database_path, artifact_path=artifacts_path)

    assert database_path.is_file()
    assert db.artifact_root() == artifacts_path
    assert artifacts_path.is_dir()
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM scenarios").fetchone()["count"] > 0
        migrations = conn.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [(row["version"], row["name"]) for row in migrations] == [
            (1, "current-sqlite-baseline"),
            (2, "identity-personal-tenants"),
            (3, "tenant-models-encrypted-credentials"),
            (4, "tenant-workers-project-bindings"),
            (5, "tenant-model-display-names"),
            (6, "tenant-model-protocol-config"),
            (7, "remove-digest-worker"),
            (8, "tenant-model-display-name-required"),
            (9, "default-copy-assessment-v1"),
            (10, "assessment-attempts"),
            (11, "blocker-cooldown"),
            (12, "surface-manifest"),
            (13, "minimum-coverage"),
            (14, "independent-vulnerability-verifier"),
            (15, "assessment-report-finalization"),
            (16, "default-copy-policy-v2"),
            (17, "assessment-report-generation-claims"),
            (18, "default-copy-v2-scenario"),
            (19, "optional-project-resources-v1"),
            (20, "retire-legacy-assessment-scenarios"),
            (21, "immutable-advisory-libraries-v1"),
            (22, "advisory-library-bindings-v1"),
            (23, "user-password-lifecycle-v1"),
            (24, "scenario-creation-templates-v1"),
            (25, "manual-versioned-assessment-reports-v1"),
            (26, "capability-based-report-execution-v1"),
            (27, "safe-project-resource-imports-v1"),
            (28, "project-resource-batch-expiry-index-v1"),
            (29, "default-copy-v2-display-label-v1"),
            (30, "ai-agent-security-scenario-v1"),
            (31, "injection-payload-records-v1"),
            (32, "canonical-vulnerability-consistency-v1"),
            (33, "verifier-schedule-key-consistency-v1"),
            (34, "assessment-progress-and-pi-workers-v1"),
            (35, "project-execution-lifecycle-v1"),
            (36, "execution-event-ledger-v1"),
            (37, "assessment-report-finish-receipts-v1"),
            (38, "assessment-report-claim-digests-v1"),
            (39, "assessment-charge-compensations-v1"),
            (40, "project-authorization-epochs-v1"),
            (41, "task-run-candidate-store-v1"),
            (42, "goal-driven-execution-v1"),
            (43, "assessment-runtime-retry-limit-v1"),
            (44, "project-container-pause-v1"),
            (45, "lease-elapsed-clock-v1"),
            (46, "goal-driven-default-scenario-v1"),
        ]
        assert all(len(row["checksum"]) == 64 for row in migrations)
        checksums = {row["version"]: row["checksum"] for row in migrations}
        assert checksums[30] == (
            "6649ae98e7aa70b80bf15214e0acb40495e053fc2d33ab4f25c5f469d4abde14"
        )
        assert checksums[31] == (
            "039cb1d7528b56f0947e0db06503586b860725d667d79f9d2dd4d1ed8d21809b"
        )
        assert checksums[32] == (
            "008ddd650cc63bcc55f7c25321982c9dc66d77612ddff622e4de8d2b4c2eff51"
        )
        assert checksums[33] == (
            "11d30fa6403130c13d45b1ffaf31c1a33079baccf326470074c380ff62f1db51"
        )
        scenarios = {
            row["key"]: row
            for row in conn.execute(
                "SELECT key, prompt_group, features_json FROM scenarios"
            ).fetchall()
        }
        assert set(scenarios) == {"goal-driven-v1"}
        assert scenarios["goal-driven-v1"]["prompt_group"] == "goal-driven-v1"
        assert json.loads(scenarios["goal-driven-v1"]["features_json"]) == {
            "sandbox": True, "exploration_limit": "balanced",
        }
        resource_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'project_resources'"
        ).fetchone()["sql"]
        assert "'file'" in resource_sql
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'project_resource_batches'"
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'project_resource_batch_members'"
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'ix_project_resource_batches_expiry'"
        ).fetchone()
        password_lifecycle_column = next(
            row for row in conn.execute("PRAGMA table_info(users)").fetchall()
            if row["name"] == "must_change_password"
        )
        assert password_lifecycle_column["type"] == "INTEGER"
        assert password_lifecycle_column["notnull"] == 1
        assert password_lifecycle_column["dflt_value"] == "0"
        legacy = conn.execute(
            "SELECT id, kind FROM tenants WHERE id = 'tenant_legacy'"
        ).fetchone()
        assert dict(legacy) == {"id": "tenant_legacy", "kind": "legacy"}
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'digest_worker'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'assessment_attempts'"
        ).fetchone() is not None
        task_run_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(task_runs)").fetchall()
        }
        assert "assessment_attempt_id" in task_run_columns
        intent_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(intents)").fetchall()
        }
        slot_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(coverage_slots)").fetchall()
        }
        assert "strategy_key" in intent_columns
        assert {
            "blocker_fingerprint",
            "blocker_strategy_key",
            "blocker_cooldown_until",
        } <= slot_columns
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_task_runs_assessment_attempt'"
        ).fetchone() is not None
        scenario = conn.execute(
            "SELECT knowledge, origin_template, goal_template, is_default "
            "FROM scenarios WHERE key = 'goal-driven-v1'"
        ).fetchone()
        assert dict(scenario) == {
            "knowledge": None, "origin_template": None, "goal_template": None,
            "is_default": 1,
        }
        assert conn.execute("SELECT COUNT(*) FROM report_templates").fetchone()[0] == 3
        knowledge_column = next(
            row for row in conn.execute("PRAGMA table_info(scenarios)").fetchall()
            if row["name"] == "knowledge"
        )
        assert knowledge_column["notnull"] == 0
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'project_resources'"
        ).fetchone() is not None
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_project_resources_project_active'"
        ).fetchone() is not None
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(("registry", "digest"), [
    (migrations.SQLITE_MIGRATIONS, "f3ac9dd716baa610d595be8cc55609879acae56081dfbdfd91ead43412cccba5"),
    (migrations.POSTGRES_MIGRATIONS, "7a4aa3f11c9158d7befe0c9d795af880ff5206823d5b7d65c387f3bb466256ec"),
])
def test_v46_keeps_all_published_migration_checksums(registry, digest: str) -> None:
    from hashlib import sha256
    historical = [(item.version, item.name, item.checksum) for item in registry if item.version <= 45]
    assert sha256(json.dumps(historical).encode()).hexdigest() == digest


def _scenario_catalog_v45() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    migrations.ensure_registry(conn)
    for migration in migrations.SQLITE_MIGRATIONS:
        if migration.version > 45:
            break
        migrations.verify_or_apply(
            conn, migration,
            (lambda target: db._ensure_schema_migrations(target, initialize_runtime=False))
            if migration.version == 1 else (lambda _target: None),
        )
    conn.execute("DELETE FROM scenarios")
    _insert_historical_scenarios_v45(conn)
    return conn


def _insert_historical_scenarios_v45(conn: sqlite3.Connection) -> None:
    # 历史迁移测试显式准备旧目录，不依赖当前版本的默认场景。
    for expected in migrations.RETIRED_SCENARIOS_V46:
        values = [json.dumps(value, ensure_ascii=False) if key.endswith("_json") else value
                  for key, value in expected.items()]
        conn.execute(
            f"INSERT INTO scenarios ({', '.join(expected)}) VALUES ({', '.join('?' for _ in values)}) "
            "ON CONFLICT(key) DO NOTHING", values,
        )


def test_v46_retires_only_unchanged_builtins_and_keeps_configuration() -> None:
    conn = _scenario_catalog_v45()
    try:
        conn.execute("INSERT INTO users (id, login, password_hash, role, created_at, updated_at) "
                     "VALUES ('usr_test', 'synthetic', 'synthetic-hash', 'user', '2026-09-28', '2026-09-28')")
        conn.execute("INSERT INTO tenant_models (id, tenant_id, display_name, protocol, base_url, model_name, "
                     "created_by, created_at, updated_at) VALUES ('model_test', 'tenant_legacy', '合成模型', "
                     "'openai-responses', 'https://model.example', 'test', 'usr_test', '2026-09-28', '2026-09-28')")
        conn.execute("INSERT INTO model_credentials (id, tenant_id, model_id, created_at, updated_at) "
                     "VALUES ('cred_test', 'tenant_legacy', 'model_test', '2026-09-28', '2026-09-28')")
        conn.execute("INSERT INTO model_credential_versions "
                     "(id, credential_id, tenant_id, version_number, algorithm, key_version, nonce, ciphertext, "
                     "credential_fingerprint, created_at) VALUES ('credv_test', 'cred_test', 'tenant_legacy', 1, "
                     "'AES-256-GCM', 'synthetic', X'000102', X'030405', 'synthetic', '2026-09-28')")
        conn.execute("UPDATE model_credentials SET active_version_id = 'credv_test' WHERE id = 'cred_test'")
        conn.execute("INSERT INTO tenant_workers (id, tenant_id, display_name, model_id, created_by, created_at, updated_at) "
                     "VALUES ('worker_test', 'tenant_legacy', '合成 Worker', 'model_test', 'usr_test', '2026-09-28', '2026-09-28')")
        tables = ("users", "tenants", "tenant_models", "model_credentials", "model_credential_versions", "tenant_workers", "report_templates")
        before = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")] for table in tables}
        old_checksums = [tuple(row) for row in conn.execute("SELECT version, checksum FROM schema_migrations ORDER BY version")]
        conn.commit()
        db.run_migrations(conn, "sqlite", initialize_runtime=False)
        assert [row["key"] for row in conn.execute("SELECT key FROM scenarios")] == ["goal-driven-v1"]
        assert {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")] for table in tables} == before
        assert [tuple(row) for row in conn.execute("SELECT version, checksum FROM schema_migrations WHERE version <= 45 ORDER BY version")] == old_checksums
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


@pytest.mark.parametrize(("key", "column", "value"), [
    ("default", "label", "用户场景"),
    ("default-copy-v2", "features_json", '{"sandbox":false}'),
    ("ai-agent-security", "origin_template", "用户背景模板"),
    ("goal-driven-v1", "goal_template", "用户目标模板"),
])
def test_v46_preserves_same_key_custom_scenarios(key: str, column: str, value: str) -> None:
    conn = _scenario_catalog_v45()
    try:
        if key == "goal-driven-v1":
            conn.execute(migrations.GOAL_DRIVEN_SCENARIO_V46)
        conn.execute(f"UPDATE scenarios SET {column} = ? WHERE key = ?", (value, key))
        before = dict(conn.execute("SELECT * FROM scenarios WHERE key = ?", (key,)).fetchone())
        conn.commit()
        db.run_migrations(conn, "sqlite", initialize_runtime=False)
        assert dict(conn.execute("SELECT * FROM scenarios WHERE key = ?", (key,)).fetchone()) == before
    finally:
        conn.close()


def test_v46_preserves_projects_and_their_referenced_scenarios() -> None:
    conn = _scenario_catalog_v45()
    try:
        conn.execute("INSERT INTO projects (id, title, created_at, scenario_key, scenario_snapshot_json) "
                     "VALUES ('proj_keep', '保留项目', '2026-09-28', 'default', '{\"key\":\"default\"}')")
        before = dict(conn.execute("SELECT * FROM projects WHERE id = 'proj_keep'").fetchone())
        conn.commit()
        db.run_migrations(conn, "sqlite", initialize_runtime=False)
        assert dict(conn.execute("SELECT * FROM projects WHERE id = 'proj_keep'").fetchone()) == before
        assert conn.execute("SELECT 1 FROM scenarios WHERE key = 'default'").fetchone()
    finally:
        conn.close()


def test_seed_empty_current_catalog_supports_null_knowledge() -> None:
    conn = _scenario_catalog_v45()
    try:
        conn.execute("DELETE FROM scenarios")
        db._seed_default_scenarios(conn)
        assert dict(conn.execute("SELECT key, knowledge, is_default FROM scenarios").fetchone()) == {
            "key": "goal-driven-v1", "knowledge": None, "is_default": 1,
        }
    finally:
        conn.close()


def test_sqlite_v23_adds_bounded_password_lifecycle_without_forcing_existing_users() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(migrations.SQLITE_V1_SCHEMA)
        migrations.SQLITE_IDENTITY_TENANT.apply(conn)
        conn.execute(
            """
            INSERT INTO users (id, login, password_hash, role, created_at, updated_at)
            VALUES ('usr_existing', 'existing', 'hash', 'user',
                    '2026-08-19T00:00:00Z', '2026-08-19T00:00:00Z')
            """
        )

        migrations.SQLITE_USER_PASSWORD_LIFECYCLE.apply(conn)

        row = conn.execute(
            "SELECT must_change_password FROM users WHERE id = 'usr_existing'"
        ).fetchone()
        assert row["must_change_password"] == 0
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE users SET must_change_password = 2 WHERE id = 'usr_existing'"
            )
    finally:
        conn.close()


def test_sqlite_v24_adds_and_seeds_creation_templates_idempotently() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(migrations.SQLITE_V1_SCHEMA)
        conn.execute(
            """
            INSERT INTO scenarios (
                key, label, is_default, profile_id, worker_ids_json, image,
                prompt_group, knowledge, cap_add_json, features_json,
                enabled_workers_json, sort_order
            ) VALUES ('default-copy-v2', 'V2', 0, 'default-copy-v2', '[]', '',
                      'default-copy-v2', 'pentest', '[]', '{}', '[]', 1)
            """
        )
        conn.execute(
            """
            INSERT INTO scenarios (
                key, label, is_default, profile_id, worker_ids_json, image,
                prompt_group, knowledge, cap_add_json, features_json,
                enabled_workers_json, sort_order
            ) VALUES ('custom', 'Custom', 0, 'default', '[]', '',
                      'default', 'knowledge_base', '[]', '{}', '[]', 2)
            """
        )

        migrations.SQLITE_SCENARIO_CREATION_TEMPLATES.apply(conn)
        migrations.SQLITE_SCENARIO_CREATION_TEMPLATES.apply(conn)

        column_rows = conn.execute("PRAGMA table_info(scenarios)").fetchall()
        columns = {row["name"] for row in column_rows}
        rows = conn.execute(
            "SELECT key, origin_template, goal_template FROM scenarios ORDER BY key"
        ).fetchall()
        assert {"origin_template", "goal_template"} <= columns
        assert {
            row["name"]: row["notnull"]
            for row in column_rows
            if row["name"] in {"origin_template", "goal_template"}
        } == {"origin_template": 0, "goal_template": 0}
        assert [dict(row) for row in rows] == [
            {"key": "custom", "origin_template": None, "goal_template": None},
            {
                "key": "default-copy-v2",
                "origin_template": DEFAULT_COPY_V2_ORIGIN_TEMPLATE,
                "goal_template": DEFAULT_COPY_V2_GOAL_TEMPLATE,
            },
        ]
    finally:
        conn.close()


def test_postgres_v24_is_additive_and_seeds_matching_builtin() -> None:
    sql = migrations.POSTGRES_SCENARIO_CREATION_TEMPLATES_V24

    assert "ADD COLUMN IF NOT EXISTS origin_template TEXT" in sql
    assert "ADD COLUMN IF NOT EXISTS goal_template TEXT" in sql
    assert "WHERE key = 'default-copy-v2'" in sql
    assert DEFAULT_COPY_V2_ORIGIN_TEMPLATE in sql
    assert DEFAULT_COPY_V2_GOAL_TEMPLATE in sql


def test_default_copy_v2_migration_adds_scenario_without_overwriting_existing() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(migrations.SQLITE_V1_SCHEMA)
        conn.execute(
            """
            INSERT INTO scenarios (
                key, label, is_default, profile_id, worker_ids_json, image,
                prompt_group, knowledge, cap_add_json, features_json,
                enabled_workers_json, sort_order
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "custom",
                "Custom",
                1,
                "default",
                "[]",
                "",
                "default",
                "knowledge_base",
                "[]",
                "{}",
                "[]",
                7,
            ),
        )

        migrations.SQLITE_DEFAULT_COPY_V2_SCENARIO.apply(conn)
        migrations.SQLITE_DEFAULT_COPY_V2_SCENARIO.apply(conn)

        rows = conn.execute(
            "SELECT key, profile_id, prompt_group, sort_order FROM scenarios ORDER BY sort_order"
        ).fetchall()
        assert [row["key"] for row in rows] == ["custom", "default-copy-v2"]
        assert dict(rows[1]) == {
            "key": "default-copy-v2",
            "profile_id": "default-copy-v2",
            "prompt_group": "default-copy-v2",
            "sort_order": 8,
        }
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("label", "profile_id", "prompt_group", "expected_label"),
    (
        (
            "默认探索（分阶段深度验证）",
            "default-copy-v2",
            "default-copy-v2",
            "渗透-覆盖面",
        ),
        ("渗透测试（V2）", "default-copy-v2", "default-copy-v2", "渗透-覆盖面"),
        (
            "团队自定义显示名",
            "default-copy-v2",
            "default-copy-v2",
            "团队自定义显示名",
        ),
        ("渗透测试（V2）", "custom-profile", "default-copy-v2", "渗透测试（V2）"),
        ("渗透测试（V2）", "default-copy-v2", "custom-prompts", "渗透测试（V2）"),
    ),
)
def test_default_copy_v2_display_label_migration_is_narrow(
    label: str,
    profile_id: str,
    prompt_group: str,
    expected_label: str,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(migrations.SQLITE_V1_SCHEMA)
        conn.execute(
            """
            INSERT INTO scenarios (
                key, label, is_default, profile_id, worker_ids_json, image,
                prompt_group, knowledge, cap_add_json, features_json,
                enabled_workers_json, sort_order
            ) VALUES ('default-copy-v2', ?, 0, ?, '[]', '', ?,
                      'pentest', '[]', '{}', '[]', 1)
            """,
            (label, profile_id, prompt_group),
        )

        migrations.SQLITE_DEFAULT_COPY_V2_DISPLAY_LABEL.apply(conn)
        migrations.SQLITE_DEFAULT_COPY_V2_DISPLAY_LABEL.apply(conn)

        row = conn.execute(
            "SELECT label, profile_id, prompt_group FROM scenarios "
            "WHERE key = 'default-copy-v2'"
        ).fetchone()
        assert dict(row) == {
            "label": expected_label,
            "profile_id": profile_id,
            "prompt_group": prompt_group,
        }
    finally:
        conn.close()


def test_default_copy_v2_display_label_migration_preserves_project_snapshot(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "display-label-v29.db"
    db.configure(database_path)
    snapshot_json = json.dumps(
        {
            "key": "default-copy-v2",
            "label": "渗透测试（V2）",
            "profile_id": "default-copy-v2",
            "prompt_group": "default-copy-v2",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    with db.get_conn() as conn:
        _insert_historical_scenarios_v45(conn)
        conn.execute(
            "UPDATE scenarios SET label = '渗透测试（V2）' "
            "WHERE key = 'default-copy-v2'"
        )
        conn.execute(
            """
            INSERT INTO projects (
                id, title, status, created_at, scenario_key, scenario_snapshot_json
            ) VALUES ('proj_v29_snapshot', 'Snapshot', 'active',
                      '2026-09-02T00:00:00Z', 'default-copy-v2', ?)
            """,
            (snapshot_json,),
        )

        migrations.SQLITE_DEFAULT_COPY_V2_DISPLAY_LABEL.apply(conn)

        scenario = conn.execute(
            "SELECT label FROM scenarios WHERE key = 'default-copy-v2'"
        ).fetchone()
        project = conn.execute(
            "SELECT scenario_key, scenario_snapshot_json FROM projects "
            "WHERE id = 'proj_v29_snapshot'"
        ).fetchone()
        assert scenario["label"] == "渗透-覆盖面"
        assert dict(project) == {
            "scenario_key": "default-copy-v2",
            "scenario_snapshot_json": snapshot_json,
        }


def test_postgres_v29_uses_the_same_narrow_display_label_update() -> None:
    sql = migrations.DEFAULT_COPY_V2_DISPLAY_LABEL_V29

    assert "WHERE key = 'default-copy-v2'" in sql
    assert "profile_id = 'default-copy-v2'" in sql
    assert "prompt_group = 'default-copy-v2'" in sql
    assert "'默认探索（分阶段深度验证）'" in sql
    assert "'渗透测试（V2）'" in sql
    assert "SET label = '渗透-覆盖面'" in sql


def _prepare_sqlite_ai_agent_migration_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(migrations.SQLITE_V1_SCHEMA)
    db._ensure_schema_migrations(conn, initialize_runtime=False)
    migrations.SQLITE_OPTIONAL_PROJECT_RESOURCES.apply(conn)
    migrations.SQLITE_SCENARIO_CREATION_TEMPLATES.apply(conn)


def test_ai_agent_security_migration_adds_scenario_idempotently() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        _prepare_sqlite_ai_agent_migration_schema(conn)
        conn.execute(
            """
            INSERT INTO scenarios (
                key, label, is_default, profile_id, worker_ids_json, image,
                prompt_group, knowledge, cap_add_json, features_json,
                enabled_workers_json, sort_order
            ) VALUES ('custom', 'Custom', 1, 'default', '[]', '',
                      'default', NULL, '[]', '{}', '[]', 7)
            """
        )

        migrations.SQLITE_AI_AGENT_SECURITY_SCENARIO.apply(conn)
        migrations.SQLITE_AI_AGENT_SECURITY_SCENARIO.apply(conn)

        row = conn.execute(
            "SELECT label, is_default, profile_id, prompt_group, knowledge, "
            "origin_template, goal_template, cap_add_json, features_json, sort_order "
            "FROM scenarios WHERE key = 'ai-agent-security'"
        ).fetchone()
        assert row is not None
        assert {
            **dict(row),
            "cap_add_json": json.loads(row["cap_add_json"]),
            "features_json": json.loads(row["features_json"]),
        } == {
            "label": "AI Agent 安全评估",
            "is_default": 0,
            "profile_id": "default",
            "prompt_group": "default",
            "knowledge": None,
            "origin_template": AI_AGENT_SECURITY_ORIGIN_TEMPLATE,
            "goal_template": AI_AGENT_SECURITY_GOAL_TEMPLATE,
            "cap_add_json": ["NET_RAW"],
            "features_json": {
                "report_policy_version": "1",
                "report_prompt_group": "default-durable-report-v1",
                "sandbox": True,
            },
            "sort_order": 8,
        }
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM scenarios WHERE key = 'ai-agent-security'"
        ).fetchone()["count"] == 1
    finally:
        conn.close()


def test_ai_agent_security_migration_preserves_same_key_custom_row() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        _prepare_sqlite_ai_agent_migration_schema(conn)
        conn.execute(
            """
            INSERT INTO scenarios (
                key, label, is_default, profile_id, worker_ids_json, image,
                prompt_group, knowledge, origin_template, goal_template,
                cap_add_json, features_json, enabled_workers_json, sort_order
            ) VALUES ('ai-agent-security', '团队自定义 Agent 场景', 1, 'custom-profile',
                      '["worker-a"]', 'custom:image', 'custom-prompts', 'custom-knowledge',
                      'custom origin', 'custom goal', '["SYS_ADMIN"]',
                      '{"custom":true}', '["worker-a"]', 4)
            """
        )
        before = dict(
            conn.execute(
                "SELECT * FROM scenarios WHERE key = 'ai-agent-security'"
            ).fetchone()
        )

        migrations.SQLITE_AI_AGENT_SECURITY_SCENARIO.apply(conn)

        after = dict(
            conn.execute(
                "SELECT * FROM scenarios WHERE key = 'ai-agent-security'"
            ).fetchone()
        )
        assert after == before
    finally:
        conn.close()


def test_ai_agent_security_migration_preserves_existing_project_records() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        _prepare_sqlite_ai_agent_migration_schema(conn)
        snapshot_json = json.dumps(
            {"key": "default", "prompt_group": "default", "features": {"sandbox": True}},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        conn.execute(
            "INSERT INTO projects "
            "(id, title, status, created_at, scenario_key, scenario_snapshot_json) "
            "VALUES ('proj_existing', 'Existing', 'active', '2026-09-02T00:00:00Z', "
            "'default', ?)",
            (snapshot_json,),
        )
        conn.executemany(
            "INSERT INTO facts (id, project_id, description, creator, created_at) "
            "VALUES (?, 'proj_existing', ?, 'system', '2026-09-02T00:00:00Z')",
            (("origin", "existing origin"), ("goal", "existing goal")),
        )

        migrations.SQLITE_AI_AGENT_SECURITY_SCENARIO.apply(conn)

        project = conn.execute(
            "SELECT scenario_key, scenario_snapshot_json FROM projects "
            "WHERE id = 'proj_existing'"
        ).fetchone()
        facts = conn.execute(
            "SELECT id, description FROM facts WHERE project_id = 'proj_existing' ORDER BY id"
        ).fetchall()
        assert dict(project) == {
            "scenario_key": "default",
            "scenario_snapshot_json": snapshot_json,
        }
        assert [dict(row) for row in facts] == [
            {"id": "goal", "description": "existing goal"},
            {"id": "origin", "description": "existing origin"},
        ]
    finally:
        conn.close()


def test_postgres_v30_uses_additive_ai_agent_scenario_insert() -> None:
    sql = migrations.AI_AGENT_SECURITY_SCENARIO_V30

    assert "INSERT INTO scenarios" in sql
    assert "'ai-agent-security'" in sql
    assert '"report_prompt_group":"default-durable-report-v1"' in sql
    assert "ON CONFLICT (key) DO NOTHING" in sql
    assert AI_AGENT_SECURITY_ORIGIN_TEMPLATE in sql
    assert AI_AGENT_SECURITY_GOAL_TEMPLATE in sql
    assert "UPDATE projects" not in sql
    assert "UPDATE facts" not in sql


def test_injection_payload_migration_creates_table_and_updates_only_builtin_scenario() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        _prepare_sqlite_ai_agent_migration_schema(conn)
        migrations.SQLITE_AI_AGENT_SECURITY_SCENARIO.apply(conn)
        snapshot_json = json.dumps(
            {"key": "ai-agent-security", "prompt_group": "default"},
            separators=(",", ":"),
        )
        conn.execute(
            "INSERT INTO projects "
            "(id, title, status, created_at, scenario_key, scenario_snapshot_json) "
            "VALUES ('proj_existing', 'Existing', 'active', '2026-09-02T00:00:00Z', "
            "'ai-agent-security', ?)",
            (snapshot_json,),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description, creator, created_at) "
            "VALUES ('origin', 'proj_existing', 'existing origin', 'system', "
            "'2026-09-02T00:00:00Z')"
        )

        migrations.SQLITE_INJECTION_PAYLOAD_RECORDS.apply(conn)
        migrations.SQLITE_INJECTION_PAYLOAD_RECORDS.apply(conn)

        assert conn.execute(
            "SELECT prompt_group FROM scenarios WHERE key = 'ai-agent-security'"
        ).fetchone()["prompt_group"] == "ai-agent-security-v1"
        assert conn.execute(
            "SELECT scenario_snapshot_json FROM projects WHERE id = 'proj_existing'"
        ).fetchone()["scenario_snapshot_json"] == snapshot_json
        assert conn.execute(
            "SELECT description FROM facts WHERE id = 'origin' AND project_id = 'proj_existing'"
        ).fetchone()["description"] == "existing origin"
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'injection_payload_records'"
        ).fetchone()
        indexes = {
            row["name"]
            for row in conn.execute("PRAGMA index_list('injection_payload_records')")
        }
        assert "idx_injection_payload_records_project_created" in indexes
        assert "idx_injection_payload_records_project_type" in indexes
        assert "idx_injection_payload_records_project_status" in indexes

        conn.execute(
            "UPDATE scenarios SET label = 'Custom AI', prompt_group = 'default' "
            "WHERE key = 'ai-agent-security'"
        )
        migrations.SQLITE_INJECTION_PAYLOAD_RECORDS.apply(conn)
        assert dict(
            conn.execute(
                "SELECT label, prompt_group FROM scenarios WHERE key = 'ai-agent-security'"
            ).fetchone()
        ) == {"label": "Custom AI", "prompt_group": "default"}
    finally:
        conn.close()


def test_postgres_v31_does_not_rewrite_project_snapshots_or_facts() -> None:
    sql = migrations.INJECTION_PAYLOAD_RECORDS_V31

    assert "CREATE TABLE IF NOT EXISTS injection_payload_records" in sql
    assert "UNIQUE (project_id, capture_id)" in sql
    assert "SET prompt_group = 'ai-agent-security-v1'" in sql
    assert "UPDATE projects" not in sql
    assert "UPDATE facts" not in sql


def test_v32_upgrades_only_untouched_builtin_scenarios(tmp_path: Path) -> None:
    database_path = tmp_path / "v32-scenarios.db"
    db.configure(database_path)
    snapshot_json = json.dumps(
        {"key": "default", "prompt_group": "default"},
        separators=(",", ":"),
    )
    with db.get_conn() as conn:
        _insert_historical_scenarios_v45(conn)
        conn.execute(
            "UPDATE scenarios SET prompt_group = 'default', features_json = ? "
            "WHERE key = 'default'",
            (migrations.DEFAULT_SCENARIO_FEATURES_V31,),
        )
        conn.execute(
            "UPDATE scenarios SET prompt_group = 'ai-agent-security-v1', "
            "features_json = ? WHERE key = 'ai-agent-security'",
            (migrations.AI_AGENT_SECURITY_FEATURES_JSON,),
        )
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, scenario_key, "
            "scenario_snapshot_json, vulnerability_fingerprint_version) "
            "VALUES ('proj_v32', 'Existing', 'active', '2026-09-03T00:00:00Z', "
            "'default', ?, NULL)",
            (snapshot_json,),
        )
        conn.execute("DELETE FROM schema_migrations WHERE version = 32")

    db._reset_for_tests()
    db.configure(database_path)
    with db.get_conn() as conn:
        rows = {
            row["key"]: row
            for row in conn.execute(
                "SELECT key, prompt_group, features_json FROM scenarios "
                "WHERE key IN ('default', 'default-copy-v2', 'ai-agent-security')"
            ).fetchall()
        }
        assert rows["default"]["prompt_group"] == "default-v2"
        assert rows["ai-agent-security"]["prompt_group"] == "ai-agent-security-v2"
        assert rows["default-copy-v2"]["prompt_group"] == "default-copy-v2"
        assert json.loads(rows["default"]["features_json"])[
            "vulnerability_fingerprint_version"
        ] == "1"
        project = conn.execute(
            "SELECT scenario_snapshot_json, vulnerability_fingerprint_version "
            "FROM projects WHERE id = 'proj_v32'"
        ).fetchone()
        assert project["scenario_snapshot_json"] == snapshot_json
        assert project["vulnerability_fingerprint_version"] is None


def test_v32_preserves_custom_same_key_scenario(tmp_path: Path) -> None:
    database_path = tmp_path / "v32-custom.db"
    db.configure(database_path)
    with db.get_conn() as conn:
        _insert_historical_scenarios_v45(conn)
        conn.execute(
            "UPDATE scenarios SET label = 'Custom Default', prompt_group = 'default', "
            "features_json = ? WHERE key = 'default'",
            (migrations.DEFAULT_SCENARIO_FEATURES_V31,),
        )
        conn.execute("DELETE FROM schema_migrations WHERE version = 32")

    db._reset_for_tests()
    db.configure(database_path)
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT label, prompt_group, features_json FROM scenarios WHERE key = 'default'"
        ).fetchone()
        assert dict(row) == {
            "label": "Custom Default",
            "prompt_group": "default",
            "features_json": migrations.DEFAULT_SCENARIO_FEATURES_V31,
        }


def test_retired_scenario_migration_deletes_projects_and_catalog_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "retired-scenarios.db"
    db.configure(database_path)
    with db.get_conn() as conn:
        _insert_historical_scenarios_v45(conn)
        conn.execute(
            "INSERT INTO scenarios "
            "(key, label, is_default, profile_id, worker_ids_json, image, prompt_group, "
            "knowledge, cap_add_json, features_json, enabled_workers_json, sort_order) "
            "VALUES ('pentest', '渗透测试', 0, 'default', '[]', '', 'pentest', "
            "'pentest', '[]', '{}', '[]', 9)"
        )
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, scenario_key) "
            "VALUES ('proj_retired', 'Retired', 'active', '2026-08-19T00:00:00Z', 'pentest')"
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description, creator, created_at) "
            "VALUES ('origin', 'proj_retired', 'origin', 'system', '2026-08-19T00:00:00Z')"
        )
        conn.execute(
            "UPDATE scenarios SET label = '默认探索（分阶段深度验证）' "
            "WHERE key = 'default-copy-v2'"
        )

        migrations.SQLITE_RETIRED_ASSESSMENT_SCENARIOS.apply(conn)
        migrations.SQLITE_RETIRED_ASSESSMENT_SCENARIOS.apply(conn)

        assert conn.execute(
            "SELECT id FROM projects WHERE id = 'proj_retired'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT id FROM facts WHERE project_id = 'proj_retired'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT key FROM scenarios WHERE key = 'pentest'"
        ).fetchone() is None
        v2 = conn.execute(
            "SELECT label, sort_order FROM scenarios WHERE key = 'default-copy-v2'"
        ).fetchone()
        assert dict(v2) == {"label": "渗透测试（V2）", "sort_order": 1}
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_postgres_default_copy_migration_tolerates_existing_schema_objects() -> None:
    sql = migrations.POSTGRES_DEFAULT_COPY_ASSESSMENT_V9

    assert "ALTER TABLE projects ADD COLUMN IF NOT EXISTS explore_budget INTEGER" in sql
    assert "ALTER TABLE coverage_slots ADD COLUMN IF NOT EXISTS surface_key TEXT" in sql
    assert "ALTER TABLE vulnerabilities ADD COLUMN IF NOT EXISTS fingerprint TEXT" in sql
    assert "CREATE TABLE IF NOT EXISTS vulnerability_observations" in sql
    assert (
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_vulnerabilities_project_fingerprint"
        in sql
    )


def test_sqlite_default_copy_migration_rolls_back_atomically() -> None:
    """v9 的列、索引、Observation 表与场景必须整体回滚。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(migrations.SQLITE_V1_SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        migrations.SQLITE_DEFAULT_COPY_ASSESSMENT.apply(conn)
        conn.rollback()

        project_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(projects)")
        }
        assert "scenario_snapshot_json" not in project_columns
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'vulnerability_observations'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT key FROM scenarios WHERE key = 'default-copy'"
        ).fetchone() is None
    finally:
        conn.close()


def test_sqlite_assessment_attempt_migration_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(migrations.SQLITE_V1_SCHEMA)
        migrations.SQLITE_ASSESSMENT_ATTEMPTS.apply(conn)
        migrations.SQLITE_ASSESSMENT_ATTEMPTS.apply(conn)

        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'assessment_attempts'"
        ).fetchone() is not None
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(task_runs)").fetchall()
        }
        assert "assessment_attempt_id" in columns
    finally:
        conn.close()


def test_sqlite_assessment_attempt_migration_rolls_back_atomically() -> None:
    """v10 DDL 必须留在调用方事务内，失败时不能留下半迁移结构。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(migrations.SQLITE_V1_SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        migrations.SQLITE_ASSESSMENT_ATTEMPTS.apply(conn)
        conn.rollback()

        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'assessment_attempts'"
        ).fetchone() is None
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(task_runs)").fetchall()
        }
        assert "assessment_attempt_id" not in columns
    finally:
        conn.close()


def test_postgres_assessment_attempt_migration_is_additive() -> None:
    sql = migrations.POSTGRES_ASSESSMENT_ATTEMPTS_V10

    assert "CREATE TABLE IF NOT EXISTS assessment_attempts" in sql
    assert (
        "ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS assessment_attempt_id TEXT"
        in sql
    )
    assert "CREATE INDEX IF NOT EXISTS idx_task_runs_assessment_attempt" in sql


def test_sqlite_blocker_cooldown_migration_is_idempotent() -> None:
    """v11 可重复应用，并补齐 Intent 与 Coverage blocker 投影列。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(migrations.SQLITE_V1_SCHEMA)
        migrations.SQLITE_DEFAULT_COPY_ASSESSMENT.apply(conn)
        migrations.SQLITE_BLOCKER_COOLDOWN.apply(conn)
        migrations.SQLITE_BLOCKER_COOLDOWN.apply(conn)

        intent_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(intents)").fetchall()
        }
        slot_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(coverage_slots)").fetchall()
        }
        assert "strategy_key" in intent_columns
        assert {
            "blocker_fingerprint",
            "blocker_strategy_key",
            "blocker_cooldown_until",
        } <= slot_columns
        scenario = conn.execute(
            "SELECT features_json FROM scenarios WHERE key = 'default-copy'"
        ).fetchone()
        assert json.loads(scenario["features_json"])["blocker_cooldown_version"] == "1"
    finally:
        conn.close()


def test_sqlite_blocker_cooldown_migration_rolls_back_atomically() -> None:
    """v11 的投影列与 feature 升级必须整体回滚。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(migrations.SQLITE_V1_SCHEMA)
        migrations.SQLITE_DEFAULT_COPY_ASSESSMENT.apply(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        migrations.SQLITE_BLOCKER_COOLDOWN.apply(conn)
        conn.rollback()

        intent_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(intents)")
        }
        slot_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(coverage_slots)")
        }
        assert "strategy_key" not in intent_columns
        assert "blocker_fingerprint" not in slot_columns
        scenario = conn.execute(
            "SELECT features_json FROM scenarios WHERE key = 'default-copy'"
        ).fetchone()
        assert "blocker_cooldown_version" not in json.loads(scenario["features_json"])
    finally:
        conn.close()


def test_postgres_blocker_cooldown_migration_is_additive() -> None:
    sql = migrations.POSTGRES_BLOCKER_COOLDOWN_V11

    assert "ALTER TABLE intents ADD COLUMN IF NOT EXISTS strategy_key" in sql
    assert (
        "ALTER TABLE coverage_slots ADD COLUMN IF NOT EXISTS blocker_fingerprint"
        in sql
    )
    assert (
        "ALTER TABLE coverage_slots ADD COLUMN IF NOT EXISTS blocker_cooldown_until"
        in sql
    )
    assert "jsonb_set" in sql
    assert "blocker_cooldown_version" in sql


def test_sqlite_surface_manifest_migration_is_idempotent() -> None:
    """v12 可重复应用，并只升级匹配的 default-copy 协议场景。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(migrations.SQLITE_V1_SCHEMA)
        migrations.SQLITE_DEFAULT_COPY_ASSESSMENT.apply(conn)
        migrations.SQLITE_SURFACE_MANIFEST.apply(conn)
        migrations.SQLITE_SURFACE_MANIFEST.apply(conn)

        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'surface_manifest_entries'"
        ).fetchone() is not None
        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        assert "idx_surface_manifest_project_surface" in indexes
        assert "ux_coverage_slots_project_surface_kind_family" in indexes
        assert "ux_coverage_slots_project_surface_family" not in indexes
        scenario = conn.execute(
            "SELECT features_json FROM scenarios WHERE key = 'default-copy'"
        ).fetchone()
        assert json.loads(scenario["features_json"])["surface_manifest_version"] == "1"
    finally:
        conn.close()


def test_sqlite_surface_manifest_migration_rolls_back_atomically() -> None:
    """v12 表、索引与场景升级必须随调用方事务一起回滚。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(migrations.SQLITE_V1_SCHEMA)
        migrations.SQLITE_DEFAULT_COPY_ASSESSMENT.apply(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        migrations.SQLITE_SURFACE_MANIFEST.apply(conn)
        conn.rollback()

        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'surface_manifest_entries'"
        ).fetchone() is None
        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        assert "ux_coverage_slots_project_surface_family" in indexes
        assert "ux_coverage_slots_project_surface_kind_family" not in indexes
        scenario = conn.execute(
            "SELECT features_json FROM scenarios WHERE key = 'default-copy'"
        ).fetchone()
        assert "surface_manifest_version" not in json.loads(scenario["features_json"])
    finally:
        conn.close()


def test_postgres_surface_manifest_migration_is_additive() -> None:
    sql = migrations.POSTGRES_SURFACE_MANIFEST_V12

    assert "CREATE TABLE IF NOT EXISTS surface_manifest_entries" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_surface_manifest_project_surface" in sql
    assert "DROP INDEX IF EXISTS ux_coverage_slots_project_surface_family" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS ux_coverage_slots_project_surface_kind_family" in sql
    assert "jsonb_set" in sql
    assert "surface_manifest_version" in sql


def test_sqlite_minimum_coverage_migration_is_idempotent() -> None:
    """v13 幂等补齐规则化 Coverage 身份、指标与范围处置审计表。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(migrations.SQLITE_V1_SCHEMA)
        migrations.SQLITE_DEFAULT_COPY_ASSESSMENT.apply(conn)
        migrations.SQLITE_SURFACE_MANIFEST.apply(conn)
        migrations.SQLITE_MINIMUM_COVERAGE.apply(conn)
        migrations.SQLITE_MINIMUM_COVERAGE.apply(conn)

        coverage_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(coverage_slots)").fetchall()
        }
        assert {"surface_id", "coverage_requirement", "coverage_rule_id"} <= coverage_columns
        project_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(projects)").fetchall()
        }
        assert {
            "coverage_checkpoint_required_count",
            "coverage_checkpoint_optional_count",
            "coverage_checkpoint_required_terminal_count",
        } <= project_columns
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'coverage_scope_dispositions'"
        ).fetchone() is not None
        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        assert "ux_coverage_slots_project_surface_id_family" in indexes
        assert "idx_coverage_slots_project_requirement_status" in indexes
        scenario = conn.execute(
            "SELECT features_json FROM scenarios WHERE key = 'default-copy'"
        ).fetchone()
        assert json.loads(scenario["features_json"])["minimum_coverage_version"] == "1"
    finally:
        conn.close()


def test_sqlite_minimum_coverage_migration_rolls_back_atomically() -> None:
    """v13 的规则列、范围处置表与 feature 升级必须整体回滚。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(migrations.SQLITE_V1_SCHEMA)
        migrations.SQLITE_DEFAULT_COPY_ASSESSMENT.apply(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        migrations.SQLITE_MINIMUM_COVERAGE.apply(conn)
        conn.rollback()

        slot_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(coverage_slots)")
        }
        assert "surface_id" not in slot_columns
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'coverage_scope_dispositions'"
        ).fetchone() is None
        scenario = conn.execute(
            "SELECT features_json FROM scenarios WHERE key = 'default-copy'"
        ).fetchone()
        assert "minimum_coverage_version" not in json.loads(
            scenario["features_json"]
        )
    finally:
        conn.close()


def test_postgres_minimum_coverage_migration_is_additive() -> None:
    sql = migrations.POSTGRES_MINIMUM_COVERAGE_V13

    assert "ALTER TABLE coverage_slots ADD COLUMN IF NOT EXISTS surface_id" in sql
    assert "ALTER TABLE projects ADD COLUMN IF NOT EXISTS coverage_checkpoint_required_count" in sql
    assert "CREATE TABLE IF NOT EXISTS coverage_scope_dispositions" in sql
    assert "ux_coverage_slots_project_surface_id_family" in sql
    assert "idx_coverage_slots_project_requirement_status" in sql
    assert "minimum_coverage_version" in sql


def test_sqlite_independent_verifier_migration_is_idempotent() -> None:
    """v14 可重复应用，并补齐 Verifier Intent 与 append-only 结果表。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(migrations.SQLITE_V1_SCHEMA)
        migrations.SQLITE_DEFAULT_COPY_ASSESSMENT.apply(conn)
        migrations.SQLITE_INDEPENDENT_VERIFIER.apply(conn)
        migrations.SQLITE_INDEPENDENT_VERIFIER.apply(conn)

        intent_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(intents)").fetchall()
        }
        assert {"assessment_role", "target_observation_id"} <= intent_columns
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'vulnerability_verifications'"
        ).fetchone() is not None
        scenario = conn.execute(
            "SELECT features_json FROM scenarios WHERE key = 'default-copy'"
        ).fetchone()
        assert json.loads(scenario["features_json"])["verifier_contract_version"] == "1"
    finally:
        conn.close()


def test_sqlite_independent_verifier_migration_rolls_back_atomically() -> None:
    """v14 的 Intent 列、Verification 表与 feature 升级必须整体回滚。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(migrations.SQLITE_V1_SCHEMA)
        migrations.SQLITE_DEFAULT_COPY_ASSESSMENT.apply(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        migrations.SQLITE_INDEPENDENT_VERIFIER.apply(conn)
        conn.rollback()

        intent_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(intents)")
        }
        assert "assessment_role" not in intent_columns
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'vulnerability_verifications'"
        ).fetchone() is None
        scenario = conn.execute(
            "SELECT features_json FROM scenarios WHERE key = 'default-copy'"
        ).fetchone()
        assert "verifier_contract_version" not in json.loads(
            scenario["features_json"]
        )
    finally:
        conn.close()


def test_postgres_independent_verifier_migration_is_additive() -> None:
    sql = migrations.POSTGRES_INDEPENDENT_VERIFIER_V14

    assert "ADD COLUMN IF NOT EXISTS assessment_role" in sql
    assert "ADD COLUMN IF NOT EXISTS target_observation_id" in sql
    assert "CREATE TABLE IF NOT EXISTS vulnerability_verifications" in sql
    assert "idx_intents_verifier_observation" in sql
    assert "verifier_contract_version" in sql


def test_sqlite_report_finalization_migration_is_idempotent() -> None:
    """v15 可重复应用，并保持 durable report 调度列完整。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(migrations.SQLITE_V1_SCHEMA)
        migrations.SQLITE_ASSESSMENT_REPORT_FINALIZATION.apply(conn)
        migrations.SQLITE_ASSESSMENT_REPORT_FINALIZATION.apply(conn)

        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(assessment_report_finalizations)"
            ).fetchall()
        }
        assert {
            "status",
            "attempt_count",
            "lease_expires_at",
            "next_attempt_at",
            "artifact_id",
            "generation_started_at",
        } <= columns
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_assessment_report_finalizations_dispatch'"
        ).fetchone() is not None
    finally:
        conn.close()


def test_sqlite_report_finalization_migration_rolls_back_atomically() -> None:
    """v15 的表与索引必须随调用方事务一起回滚。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(migrations.SQLITE_V1_SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        migrations.SQLITE_ASSESSMENT_REPORT_FINALIZATION.apply(conn)
        conn.rollback()

        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'assessment_report_finalizations'"
        ).fetchone() is None
    finally:
        conn.close()


def test_postgres_report_finalization_migration_is_additive() -> None:
    sql = migrations.POSTGRES_ASSESSMENT_REPORT_FINALIZATION_V15

    assert "CREATE TABLE IF NOT EXISTS assessment_report_finalizations" in sql
    assert "next_attempt_at TEXT" in sql
    assert "generation_started_at TEXT NOT NULL" in sql
    assert "idx_assessment_report_finalizations_dispatch" in sql


def test_sqlite_report_token_migration_is_idempotent_and_backfills() -> None:
    """v17 重复执行时保留列，并为历史代际补齐稳定 token。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(migrations.SQLITE_V1_SCHEMA)
        migrations.SQLITE_ASSESSMENT_REPORT_FINALIZATION.apply(conn)
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at) "
            "VALUES ('p1', 'Report', 'completed', '2026-08-17T00:00:00Z')"
        )
        conn.execute(
            """
            INSERT INTO assessment_report_finalizations (
                project_id, generation_started_at, created_at, updated_at
            ) VALUES ('p1', '2026-08-17T00:01:00Z',
                      '2026-08-17T00:01:00Z', '2026-08-17T00:01:00Z')
            """
        )

        migrations.SQLITE_ASSESSMENT_REPORT_TOKENS.apply(conn)
        migrations.SQLITE_ASSESSMENT_REPORT_TOKENS.apply(conn)

        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(assessment_report_finalizations)"
            ).fetchall()
        }
        row = conn.execute(
            "SELECT generation_token, claim_token "
            "FROM assessment_report_finalizations WHERE project_id = 'p1'"
        ).fetchone()
        assert {"generation_token", "claim_token"} <= columns
        assert row["generation_token"] == "legacy:p1:2026-08-17T00:01:00Z"
        assert row["claim_token"] is None
    finally:
        conn.close()


def test_sqlite_report_token_migration_rolls_back_atomically() -> None:
    """v17 新列必须随调用方事务一起回滚。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(migrations.SQLITE_V1_SCHEMA)
        migrations.SQLITE_ASSESSMENT_REPORT_FINALIZATION.apply(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        migrations.SQLITE_ASSESSMENT_REPORT_TOKENS.apply(conn)
        conn.rollback()

        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(assessment_report_finalizations)"
            ).fetchall()
        }
        assert "generation_token" not in columns
        assert "claim_token" not in columns
    finally:
        conn.close()


def test_postgres_report_token_migration_is_additive() -> None:
    sql = migrations.POSTGRES_ASSESSMENT_REPORT_TOKENS_V17

    assert "ADD COLUMN IF NOT EXISTS generation_token TEXT" in sql
    assert "ADD COLUMN IF NOT EXISTS claim_token TEXT" in sql
    assert "WHERE generation_token IS NULL OR generation_token = ''" in sql


def test_published_default_copy_v9_checksums_are_unchanged() -> None:
    assert migrations.SQLITE_DEFAULT_COPY_ASSESSMENT.checksum == (
        "6d7ff362ea0d0ec8c1c1ab495962367422d70f7ffd03a216abc4f0aa2b6c0611"
    )
    assert migrations.POSTGRES_DEFAULT_COPY_ASSESSMENT.checksum == (
        "2a513d32ef61ebeb79fa3fff1c001bd218a2dcc248bea64340fe7aa41beadddd"
    )


def test_sqlite_default_copy_policy_upgrade_is_idempotent() -> None:
    """v16 只补齐规范 default-copy 的当前 outcome 与 quality 版本。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(migrations.SQLITE_V1_SCHEMA)
        migrations.SQLITE_DEFAULT_COPY_ASSESSMENT.apply(conn)
        migrations.SQLITE_DEFAULT_COPY_POLICY.apply(conn)
        migrations.SQLITE_DEFAULT_COPY_POLICY.apply(conn)

        scenario = conn.execute(
            "SELECT features_json FROM scenarios WHERE key = 'default-copy'"
        ).fetchone()
        features = json.loads(scenario["features_json"])
        assert features["outcome_contract_version"] == "2"
        assert features["quality_gate_version"] == "1"
    finally:
        conn.close()


def test_sqlite_default_copy_policy_upgrade_rolls_back_atomically() -> None:
    """v16 的两个 feature 版本必须在同一事务中回滚。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(migrations.SQLITE_V1_SCHEMA)
        migrations.SQLITE_DEFAULT_COPY_ASSESSMENT.apply(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        migrations.SQLITE_DEFAULT_COPY_POLICY.apply(conn)
        conn.rollback()

        scenario = conn.execute(
            "SELECT features_json FROM scenarios WHERE key = 'default-copy'"
        ).fetchone()
        features = json.loads(scenario["features_json"])
        assert "outcome_contract_version" not in features
        assert "quality_gate_version" not in features
    finally:
        conn.close()


def test_postgres_default_copy_policy_upgrade_is_additive() -> None:
    sql = migrations.POSTGRES_DEFAULT_COPY_POLICY_V16

    assert "jsonb_set" in sql
    assert "outcome_contract_version" in sql
    assert "quality_gate_version" in sql
    assert "key = 'default-copy'" in sql
    assert "prompt_group = 'default-copy'" in sql
    assert "assessment_protocol' = 'default-copy-v1'" in sql


def test_v8_database_upgrade_freezes_current_goal_driven_policy(tmp_path: Path) -> None:
    """历史 v8 数据库升级后，新项目仍冻结 goal-driven，而不复活旧覆盖门。"""
    database_path = tmp_path / "v8-default-copy-policy.db"
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(migrations.SQLITE_V1_SCHEMA)
        migrations.ensure_registry(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
            "VALUES (?, ?, ?, ?)",
            (
                migrations.SQLITE_BASELINE.version,
                migrations.SQLITE_BASELINE.name,
                migrations.SQLITE_BASELINE.checksum,
                "2026-08-17T00:00:00Z",
            ),
        )
        for migration in migrations.SQLITE_MIGRATIONS[1:8]:
            migrations.verify_or_apply(conn, migration, lambda _target: None)
        conn.commit()
    finally:
        conn.close()

    db.configure(database_path)
    with db.get_conn() as upgraded:
        upgraded.execute(
            "INSERT INTO users "
            "(id, login, password_hash, role, created_at, updated_at) "
            "VALUES (?, ?, ?, 'user', ?, ?)",
            (
                "usr_upgrade",
                "upgrade-owner",
                "hash",
                "2026-08-17T00:00:00Z",
                "2026-08-17T00:00:00Z",
            ),
        )
        upgraded.execute(
            """
            INSERT INTO tenant_models
                (id, tenant_id, display_name, protocol, base_url, model_name,
                 created_by, created_at, updated_at)
            VALUES (?, ?, ?, 'anthropic-messages', ?, ?, ?, ?, ?)
            """,
            (
                "model_upgrade",
                migrations.LEGACY_TENANT_ID,
                "Upgrade Model",
                "https://api.example.test",
                "claude-test",
                "usr_upgrade",
                "2026-08-17T00:00:00Z",
                "2026-08-17T00:00:00Z",
            ),
        )
        upgraded.execute(
            "INSERT INTO model_credentials "
            "(id, tenant_id, model_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "cred_upgrade",
                migrations.LEGACY_TENANT_ID,
                "model_upgrade",
                "2026-08-17T00:00:00Z",
                "2026-08-17T00:00:00Z",
            ),
        )
        upgraded.execute(
            """
            INSERT INTO model_credential_versions
                (id, credential_id, tenant_id, version_number, algorithm,
                 key_version, nonce, ciphertext, credential_fingerprint, created_at)
            VALUES (?, ?, ?, 1, 'AES-256-GCM', ?, ?, ?, ?, ?)
            """,
            (
                "credv_upgrade",
                "cred_upgrade",
                migrations.LEGACY_TENANT_ID,
                "key-v1",
                b"012345678901",
                b"ciphertext",
                "fingerprint",
                "2026-08-17T00:00:00Z",
            ),
        )
        upgraded.execute(
            "UPDATE model_credentials SET active_version_id = ? WHERE id = ?",
            ("credv_upgrade", "cred_upgrade"),
        )
        upgraded.execute(
            """
            INSERT INTO tenant_workers
                (id, tenant_id, display_name, model_id, created_by,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "worker_upgrade",
                migrations.LEGACY_TENANT_ID,
                "Upgrade Worker",
                "model_upgrade",
                "usr_upgrade",
                "2026-08-17T00:00:00Z",
                "2026-08-17T00:00:00Z",
            ),
        )
        upgraded.execute(
            """
            INSERT INTO project_groups (id, name, owner, created_at, tenant_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "group_upgrade",
                "Upgrade Projects",
                "usr_upgrade",
                "2026-08-17T00:00:00Z",
                migrations.LEGACY_TENANT_ID,
            ),
        )
    project = create_project_record(
        CreateProjectRequest(
            title="Upgraded policy project",
            origin="https://example.test",
            goal="Audit the target",
            scenario="goal-driven-v1",
            tenant_worker_ids=["worker_upgrade"],
            authorization_expires_at=None,
        ),
        tenant_id=migrations.LEGACY_TENANT_ID,
    )
    reloaded = get_project_detail(
        project.project.id,
        tenant_id=migrations.LEGACY_TENANT_ID,
    )

    frozen_features = reloaded.project.scenario_snapshot["features"]
    assert frozen_features["assessment_protocol"] == "goal-driven-v1"
    assert frozen_features["outcome_contract_version"] == 2
    assert "quality_gate_version" not in frozen_features
    assert frozen_features["execution_engine"] == "pi"
    assert frozen_features["execution_contract_version"] == 1
    assert frozen_features["assessment_contract_version"] == 2
    assert reloaded.project.assessment_quality is None
    with db.get_conn() as upgraded:
        row = upgraded.execute(
            "SELECT * FROM projects WHERE id = ?",
            (project.project.id,),
        ).fetchone()
        policy = policy_from_project_row(row)
        assert policy.goal_driven
        assert policy.outcome_contract_version == 2
        assert policy.quality_gate_version == 0


def test_retirement_migration_removes_reintroduced_default_copy_scenario(tmp_path: Path) -> None:
    database_path = tmp_path / "custom-default-copy.db"
    db.configure(database_path)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO scenarios "
            "(key, label, is_default, profile_id, worker_ids_json, image, prompt_group, "
            "knowledge, cap_add_json, features_json, enabled_workers_json, sort_order) "
            "VALUES ('default-copy', 'User scenario', 0, 'default', '[]', '', "
            "'default', NULL, '[]', '{\"sandbox\":true}', '[]', 9)"
        )
        conn.execute("DELETE FROM schema_migrations WHERE version = 20")

    db._reset_for_tests()
    db.configure(database_path)

    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT key FROM scenarios WHERE key = 'default-copy'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT name FROM schema_migrations WHERE version = 20"
        ).fetchone()["name"] == "retire-legacy-assessment-scenarios"


def test_configure_accepts_sqlite_database_url(tmp_path: Path) -> None:
    database_path = tmp_path / "url-configured.db"

    db.configure(database_url=f"sqlite:///{database_path}")

    with db.get_conn() as conn:
        row = conn.execute("PRAGMA database_list").fetchone()
        assert Path(row["file"]) == database_path


def test_explicit_sqlite_path_overrides_database_url_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "explicit.db"
    monkeypatch.setenv(db.DEFAULT_DATABASE_URL_ENV, "postgresql://ignored.invalid/cairn")

    db.configure(database_path)

    assert db.database_config().backend == "sqlite"
    assert db.database_config().sqlite_path == database_path


def test_configure_upgrades_legacy_settings_table(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(database_path)
    try:
        conn.executescript(
            """
            CREATE TABLE settings (
                intent_timeout INTEGER NOT NULL DEFAULT 60,
                reason_timeout INTEGER NOT NULL DEFAULT 60,
                coverage_idle_rounds INTEGER NOT NULL DEFAULT 3
            );
            INSERT INTO settings (intent_timeout, reason_timeout)
            VALUES (60, 60);
            """
        )
        conn.commit()
    finally:
        conn.close()

    db.configure(database_path)

    with db.get_conn() as upgraded:
        settings = upgraded.execute(
            "SELECT id, intent_timeout, reason_timeout FROM settings"
        ).fetchone()
        assert dict(settings) == {"id": 1, "intent_timeout": 60, "reason_timeout": 180}
        assert upgraded.execute(
            "SELECT name FROM schema_migrations WHERE version = 1"
        ).fetchone()["name"] == "current-sqlite-baseline"


def test_tenant_model_schema_enforces_tenant_and_credential_relations(tmp_path: Path) -> None:
    database_path = tmp_path / "tenant-models.db"
    db.configure(database_path)

    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO tenants (id, kind, name, created_at) VALUES (?, 'personal', ?, ?)",
            ("tenant_001", "Tenant", "2026-08-13T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO users (id, login, password_hash, role, created_at, updated_at) VALUES (?, ?, ?, 'user', ?, ?)",
            ("usr_001", "user", "hash", "2026-08-13T00:00:00Z", "2026-08-13T00:00:00Z"),
        )
        conn.execute(
            """
            INSERT INTO tenant_models
                (id, tenant_id, display_name, protocol, base_url, model_name,
                 created_by, created_at, updated_at)
            VALUES (?, ?, ?, 'anthropic-messages', ?, ?, ?, ?, ?)
            """,
            (
                "model_001",
                "tenant_001",
                "Tenant Claude",
                "https://api.example.test",
                "claude-test",
                "usr_001",
                "2026-08-13T00:00:00Z",
                "2026-08-13T00:00:00Z",
            ),
        )
        conn.execute(
            "INSERT INTO model_credentials (id, tenant_id, model_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("cred_001", "tenant_001", "model_001", "2026-08-13T00:00:00Z", "2026-08-13T00:00:00Z"),
        )
        conn.execute(
            """
            INSERT INTO model_credential_versions
                (id, credential_id, tenant_id, version_number, algorithm, key_version,
                 nonce, ciphertext, credential_fingerprint, created_at)
            VALUES (?, ?, ?, 1, 'AES-256-GCM', ?, ?, ?, ?, ?)
            """,
            (
                "credv_001",
                "cred_001",
                "tenant_001",
                "key-v1",
                b"012345678901",
                b"ciphertext",
                "fingerprint",
                "2026-08-13T00:00:00Z",
            ),
        )
        conn.execute(
            "UPDATE model_credentials SET active_version_id = ? WHERE id = ?",
            ("credv_001", "cred_001"),
        )
        model = conn.execute(
            """
            SELECT protocol, auth_header, custom_headers_json,
                   models_endpoint, inference_endpoint
            FROM tenant_models WHERE id = ?
            """,
            ("model_001",),
        ).fetchone()
        assert dict(model) == {
            "protocol": "anthropic-messages",
            "auth_header": "authorization-bearer",
            "custom_headers_json": "[]",
            "models_endpoint": None,
            "inference_endpoint": None,
        }
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO tenant_models
                    (id, tenant_id, display_name, protocol, base_url,
                     created_by, created_at, updated_at)
                VALUES (?, ?, ?, 'anthropic', ?, ?, ?, ?)
                """,
                (
                    "model_legacy_protocol",
                    "tenant_001",
                    "Legacy Protocol",
                    "https://api.example.test",
                    "usr_001",
                    "2026-08-13T00:00:00Z",
                    "2026-08-13T00:00:00Z",
                ),
            )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tenant_models (id, tenant_id, display_name, protocol, base_url, created_by, created_at, updated_at) VALUES (?, ?, ?, 'openai-responses', ?, ?, ?, ?)",
                (
                    "model_bad",
                    "tenant_missing",
                    "Missing Tenant Model",
                    "https://api.example.test",
                    "usr_001",
                    "2026-08-13T00:00:00Z",
                    "2026-08-13T00:00:00Z",
                ),
            )


def test_get_conn_commits_successful_transaction(tmp_path: Path) -> None:
    database_path = tmp_path / "commit.db"
    db.configure(database_path)

    with db.get_conn() as conn:
        conn.execute("INSERT INTO counters (name, value) VALUES (?, ?)", ("commit-test", 1))

    with db.get_conn() as conn:
        assert conn.execute("SELECT value FROM counters WHERE name = ?", ("commit-test",)).fetchone()["value"] == 1


def test_get_conn_rolls_back_failed_transaction(tmp_path: Path) -> None:
    database_path = tmp_path / "rollback.db"
    db.configure(database_path)

    with pytest.raises(RuntimeError, match="rollback"):
        with db.get_conn() as conn:
            conn.execute("INSERT INTO counters (name, value) VALUES (?, ?)", ("rollback-test", 1))
            raise RuntimeError("rollback")

    with db.get_conn() as conn:
        assert conn.execute("SELECT value FROM counters WHERE name = ?", ("rollback-test",)).fetchone() is None


def test_foreign_keys_are_enabled_for_every_connection(tmp_path: Path) -> None:
    database_path = tmp_path / "foreign-keys.db"
    db.configure(database_path)

    with db.get_conn() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO facts (id, project_id, description, creator, created_at) VALUES (?, ?, ?, ?, ?)",
                ("f001", "missing", "orphan", "test", "2026-08-12T00:00:00Z"),
            )


def test_recursive_triggers_are_enabled_for_every_connection(tmp_path: Path) -> None:
    database_path = tmp_path / "recursive-triggers.db"
    db.configure(database_path)

    for _ in range(2):
        with db.get_conn() as conn:
            assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 1


def test_configure_is_idempotent_and_keeps_first_configuration(tmp_path: Path) -> None:
    first_path = tmp_path / "first.db"
    second_path = tmp_path / "second.db"

    db.configure(first_path)
    db.configure(second_path)

    assert first_path.is_file()
    assert not second_path.exists()


def test_qmark_adapter_preserves_quoted_question_marks() -> None:
    sql = "SELECT ?, '?' AS literal, \"?\" AS identifier, 'it''s ?' AS escaped"

    assert db._adapt_qmark_sql(sql) == (
        "SELECT %s, '?' AS literal, \"?\" AS identifier, 'it''s ?' AS escaped"
    )


def test_qmark_adapter_preserves_comments_and_dollar_quotes() -> None:
    sql = "SELECT ? -- why?\n/* ? */ SELECT $tag$?$tag$, ?"

    assert db._adapt_qmark_sql(sql) == (
        "SELECT %s -- why?\n/* ? */ SELECT $tag$?$tag$, %s"
    )


def test_sqlite_baseline_rolls_back_when_normalization_fails(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "atomic.db"

    def fail_normalization(conn) -> None:
        raise RuntimeError("normalization failed")

    monkeypatch.setattr(db, "_ensure_schema_migrations", fail_normalization)
    with pytest.raises(RuntimeError, match="normalization failed"):
        db.configure(database_path)

    conn = sqlite3.connect(database_path)
    try:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'settings'"
        ).fetchone()
        assert table is None
    finally:
        conn.close()


def test_configure_rejects_changed_applied_migration(tmp_path: Path) -> None:
    database_path = tmp_path / "changed-migration.db"
    db.configure(database_path)
    db._reset_for_tests()

    conn = sqlite3.connect(database_path)
    try:
        conn.execute("UPDATE schema_migrations SET checksum = 'changed' WHERE version = 1")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="does not match"):
        db.configure(database_path)
    assert db._config is None


def test_dc9acc6_sqlite_v1_database_upgrades_to_current(tmp_path: Path) -> None:
    database_path = tmp_path / "dc9acc6-v1.db"
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(migrations.SQLITE_V1_SCHEMA)
        migrations.ensure_registry(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
            (
                1,
                "current-sqlite-baseline",
                "487a28b3bdc801b24ab0976e6a5c551c95fe26b0b439b117e861fff380c51f17",
                "2026-08-12T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    db.configure(database_path)

    with db.get_conn() as upgraded:
        rows = upgraded.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [(row["version"], row["name"]) for row in rows] == [
            (1, "current-sqlite-baseline"),
            (2, "identity-personal-tenants"),
            (3, "tenant-models-encrypted-credentials"),
            (4, "tenant-workers-project-bindings"),
            (5, "tenant-model-display-names"),
            (6, "tenant-model-protocol-config"),
            (7, "remove-digest-worker"),
            (8, "tenant-model-display-name-required"),
            (9, "default-copy-assessment-v1"),
            (10, "assessment-attempts"),
            (11, "blocker-cooldown"),
            (12, "surface-manifest"),
            (13, "minimum-coverage"),
            (14, "independent-vulnerability-verifier"),
            (15, "assessment-report-finalization"),
            (16, "default-copy-policy-v2"),
            (17, "assessment-report-generation-claims"),
            (18, "default-copy-v2-scenario"),
            (19, "optional-project-resources-v1"),
            (20, "retire-legacy-assessment-scenarios"),
            (21, "immutable-advisory-libraries-v1"),
            (22, "advisory-library-bindings-v1"),
            (23, "user-password-lifecycle-v1"),
            (24, "scenario-creation-templates-v1"),
            (25, "manual-versioned-assessment-reports-v1"),
            (26, "capability-based-report-execution-v1"),
            (27, "safe-project-resource-imports-v1"),
            (28, "project-resource-batch-expiry-index-v1"),
            (29, "default-copy-v2-display-label-v1"),
            (30, "ai-agent-security-scenario-v1"),
            (31, "injection-payload-records-v1"),
            (32, "canonical-vulnerability-consistency-v1"),
            (33, "verifier-schedule-key-consistency-v1"),
            (34, "assessment-progress-and-pi-workers-v1"),
            (35, "project-execution-lifecycle-v1"),
            (36, "execution-event-ledger-v1"),
            (37, "assessment-report-finish-receipts-v1"),
            (38, "assessment-report-claim-digests-v1"),
            (39, "assessment-charge-compensations-v1"),
            (40, "project-authorization-epochs-v1"),
            (41, "task-run-candidate-store-v1"),
            (42, "goal-driven-execution-v1"),
            (43, "assessment-runtime-retry-limit-v1"),
            (44, "project-container-pause-v1"),
            (45, "lease-elapsed-clock-v1"),
            (46, "goal-driven-default-scenario-v1"),
        ]
        assert rows[0]["checksum"] == migrations.SQLITE_BASELINE.checksum
        assert upgraded.execute("SELECT id FROM tenants WHERE id = 'tenant_legacy'").fetchone()
        assert upgraded.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'digest_worker'"
        ).fetchone() is None
        assert upgraded.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'assessment_attempts'"
        ).fetchone() is not None
        task_run_columns = {
            row["name"]
            for row in upgraded.execute("PRAGMA table_info(task_runs)").fetchall()
        }
        assert "assessment_attempt_id" in task_run_columns
        assert upgraded.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_task_runs_assessment_attempt'"
        ).fetchone() is not None
        report_columns = {
            row["name"]
            for row in upgraded.execute(
                "PRAGMA table_info(assessment_report_finalizations)"
            ).fetchall()
        }
        assert "generation_started_at" in report_columns


def test_sqlite_v6_preserves_model_credentials_and_worker_references() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.executescript(migrations.SQLITE_V1_SCHEMA)
        for migration in migrations.SQLITE_MIGRATIONS[1:5]:
            migration.apply(conn)
        conn.execute(
            "INSERT INTO tenants (id, kind, name, created_at) VALUES ('tenant_001', 'personal', 'Tenant', '2026-08-13T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO users (id, login, password_hash, role, created_at, updated_at) VALUES ('usr_001', 'user', 'hash', 'user', '2026-08-13T00:00:00Z', '2026-08-13T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO projects (id, tenant_id, title, created_at) VALUES ('proj_001', 'tenant_001', 'Project', '2026-08-13T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO worker_templates (id, tenant_id, name, owner, created_at) VALUES ('wt_001', 'tenant_001', 'Template', 'owner', '2026-08-13T00:00:00Z')"
        )
        conn.execute(
            """
            INSERT INTO tenant_models
                (id, tenant_id, display_name, protocol, base_url, model_name,
                 enabled, revision, created_by, created_at, updated_at)
            VALUES
                ('model_anthropic', 'tenant_001', 'Anthropic', 'anthropic',
                 'https://anthropic.example', 'claude-test', 1, 7, 'usr_001',
                 '2026-08-13T00:00:00Z', '2026-08-13T00:00:00Z'),
                ('model_openai', 'tenant_001', 'OpenAI', 'openai',
                 'https://openai.example', 'gpt-test', 1, 3, 'usr_001',
                 '2026-08-13T00:00:00Z', '2026-08-13T00:00:00Z')
            """
        )
        conn.execute(
            "INSERT INTO model_credentials (id, tenant_id, model_id, created_at, updated_at) VALUES ('cred_001', 'tenant_001', 'model_anthropic', '2026-08-13T00:00:00Z', '2026-08-13T00:00:00Z')"
        )
        conn.execute(
            """
            INSERT INTO model_credential_versions
                (id, credential_id, tenant_id, version_number, algorithm, key_version,
                 nonce, ciphertext, credential_fingerprint, created_at)
            VALUES ('credv_001', 'cred_001', 'tenant_001', 1, 'AES-256-GCM',
                    'key-v1', X'000102', X'030405', 'fingerprint',
                    '2026-08-13T00:00:00Z')
            """
        )
        conn.execute(
            "UPDATE model_credentials SET active_version_id = 'credv_001' WHERE id = 'cred_001'"
        )
        conn.execute(
            """
            INSERT INTO tenant_workers
                (id, tenant_id, display_name, model_id, created_by, created_at, updated_at)
            VALUES ('worker_001', 'tenant_001', 'Worker', 'model_anthropic',
                    'usr_001', '2026-08-13T00:00:00Z', '2026-08-13T00:00:00Z')
            """
        )
        conn.execute(
            "INSERT INTO project_tenant_workers (project_id, tenant_id, tenant_worker_id, tenant_worker_revision, position, created_at) VALUES ('proj_001', 'tenant_001', 'worker_001', 1, 0, '2026-08-13T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO worker_template_tenant_workers (template_id, tenant_id, tenant_worker_id, position, created_at) VALUES ('wt_001', 'tenant_001', 'worker_001', 0, '2026-08-13T00:00:00Z')"
        )
        conn.execute(
            """
            INSERT INTO model_operation_tests
                (id, tenant_id, model_id, kind, status, model_revision, created_at)
            VALUES ('op_001', 'tenant_001', 'model_anthropic', 'discovery',
                    'completed', 7, '2026-08-13T00:00:00Z')
            """
        )

        migrations.SQLITE_TENANT_MODEL_PROTOCOL_CONFIG.apply(conn)

        models = conn.execute(
            "SELECT id, protocol, auth_header, custom_headers_json, models_endpoint, inference_endpoint, revision FROM tenant_models ORDER BY id"
        ).fetchall()
        assert [dict(row) for row in models] == [
            {
                "id": "model_anthropic",
                "protocol": "anthropic-messages",
                "auth_header": "x-api-key",
                "custom_headers_json": "[]",
                "models_endpoint": None,
                "inference_endpoint": None,
                "revision": 7,
            },
            {
                "id": "model_openai",
                "protocol": "openai-responses",
                "auth_header": "authorization-bearer",
                "custom_headers_json": "[]",
                "models_endpoint": None,
                "inference_endpoint": None,
                "revision": 3,
            },
        ]
        assert dict(
            conn.execute(
                "SELECT model_id, active_version_id FROM model_credentials WHERE id = 'cred_001'"
            ).fetchone()
        ) == {"model_id": "model_anthropic", "active_version_id": "credv_001"}
        assert conn.execute(
            "SELECT model_id FROM tenant_workers WHERE id = 'worker_001'"
        ).fetchone()["model_id"] == "model_anthropic"
        assert conn.execute(
            "SELECT tenant_worker_id FROM project_tenant_workers WHERE project_id = 'proj_001'"
        ).fetchone()["tenant_worker_id"] == "worker_001"
        assert conn.execute(
            "SELECT tenant_worker_id FROM worker_template_tenant_workers WHERE template_id = 'wt_001'"
        ).fetchone()["tenant_worker_id"] == "worker_001"
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'model_operation_tests'"
        ).fetchone() is None
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_dc9acc6_postgres_v1_registry_upgrades_to_current_definition() -> None:
    class Cursor:
        def __init__(self, *, row=None, rows=()) -> None:
            self.row = row
            self.rows = list(rows)

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class RegistryConnection:
        def __init__(self) -> None:
            self.rows = {
                1: {
                    "name": "current-postgres-baseline",
                    "checksum": "88453cf001766dd0d841f8da83a75a15d24a3cf538dd36b6a31966a0302be46a",
                }
            }
            self.model_rows = [
                {
                    "id": "model_late",
                    "tenant_id": "tenant_a",
                    "model_name": "claude-test",
                    "created_at": "2026-08-13T00:00:02Z",
                },
                {
                    "id": "model_other_tenant",
                    "tenant_id": "tenant_b",
                    "model_name": "claude-test",
                    "created_at": "2026-08-13T00:00:01Z",
                },
                {
                    "id": "model_early",
                    "tenant_id": "tenant_a",
                    "model_name": "claude-test",
                    "created_at": "2026-08-13T00:00:01Z",
                },
                {
                    "id": "model_blank",
                    "tenant_id": "tenant_a",
                    "model_name": " ",
                    "created_at": "2026-08-13T00:00:03Z",
                },
            ]
            self.scripts: list[str] = []
            self.statements: list[str] = []

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT name, checksum"):
                return Cursor(row=self.rows.get(params[0]))
            if (
                normalized.startswith("ALTER TABLE tenant_models")
                or normalized.startswith("ALTER TABLE task_runs")
                or normalized.startswith("ALTER TABLE users")
                or normalized.startswith("ALTER TABLE projects ADD COLUMN execution_generation")
            ):
                self.statements.append(normalized)
                return Cursor()
            if normalized.startswith("SELECT id, tenant_id, model_name, created_at"):
                return Cursor(rows=self.model_rows)
            if normalized.startswith("SELECT finalization.project_id"):
                return Cursor(rows=[])
            if normalized.startswith(
                "SELECT project_id, claim_token, claim_sha256 "
                "FROM assessment_report_finalizations"
            ):
                return Cursor(rows=[])
            if normalized.startswith(
                "SELECT COUNT(*) AS value FROM assessment_report_finalizations"
            ):
                return Cursor(row={"value": 0})
            if normalized.startswith(
                "SELECT id FROM projects WHERE assessment_protocol = "
            ):
                return Cursor(rows=[])
            if normalized == "SELECT id FROM projects ORDER BY id FOR UPDATE":
                return Cursor(rows=[])
            if normalized.startswith(
                "SELECT * FROM projects WHERE assessment_protocol = "
            ):
                return Cursor(rows=[])
            if normalized.startswith(
                "SELECT aa.id AS assessment_attempt_id"
            ):
                return Cursor(rows=[])
            if normalized.startswith("UPDATE tenant_models SET display_name"):
                display_name, model_id = params
                next(row for row in self.model_rows if row["id"] == model_id)[
                    "display_name"
                ] = display_name
                return Cursor()
            if normalized.startswith("CREATE UNIQUE INDEX") or normalized.startswith(
                ("CREATE INDEX IF NOT EXISTS idx_task_runs_assessment_attempt", "CREATE INDEX idx_task_runs_pause")
            ):
                self.statements.append(normalized)
                return Cursor()
            if normalized == "DROP TABLE digest_worker;":
                self.statements.append(normalized)
                return Cursor()
            if normalized.startswith("INSERT INTO schema_migrations"):
                version, name, checksum, _applied_at = params
                self.rows[version] = {"name": name, "checksum": checksum}
                return Cursor()
            raise AssertionError(sql)

        def executescript(self, sql: str) -> None:
            self.scripts.append(sql)

    conn = RegistryConnection()
    for migration in migrations.POSTGRES_MIGRATIONS:
        migrations.verify_or_apply(conn, migration, lambda _target: None)

    assert conn.rows[1] == {
        "name": "current-postgres-baseline",
        "checksum": migrations.POSTGRES_BASELINE.checksum,
    }
    assert conn.rows[2] == {
        "name": "identity-personal-tenants",
        "checksum": migrations.POSTGRES_IDENTITY_TENANT.checksum,
    }
    assert conn.rows[3] == {
        "name": "tenant-models-encrypted-credentials",
        "checksum": migrations.POSTGRES_TENANT_MODELS.checksum,
    }
    assert conn.rows[4] == {
        "name": "tenant-workers-project-bindings",
        "checksum": migrations.POSTGRES_TENANT_WORKERS.checksum,
    }
    assert conn.rows[5] == {
        "name": "tenant-model-display-names",
        "checksum": migrations.POSTGRES_TENANT_MODEL_DISPLAY_NAMES.checksum,
    }
    assert conn.rows[6] == {
        "name": "tenant-model-protocol-config",
        "checksum": migrations.POSTGRES_TENANT_MODEL_PROTOCOL_CONFIG.checksum,
    }
    assert conn.rows[7] == {
        "name": "remove-digest-worker",
        "checksum": migrations.POSTGRES_DIGEST_WORKER_REMOVAL.checksum,
    }
    assert conn.rows[8] == {
        "name": "tenant-model-display-name-required",
        "checksum": migrations.POSTGRES_TENANT_MODEL_DISPLAY_NAME_REQUIRED.checksum,
    }
    assert conn.rows[9] == {
        "name": "default-copy-assessment-v1",
        "checksum": migrations.POSTGRES_DEFAULT_COPY_ASSESSMENT.checksum,
    }
    assert conn.rows[10] == {
        "name": "assessment-attempts",
        "checksum": migrations.POSTGRES_ASSESSMENT_ATTEMPTS.checksum,
    }
    assert conn.rows[11] == {
        "name": "blocker-cooldown",
        "checksum": migrations.POSTGRES_BLOCKER_COOLDOWN.checksum,
    }
    assert conn.rows[12] == {
        "name": "surface-manifest",
        "checksum": migrations.POSTGRES_SURFACE_MANIFEST.checksum,
    }
    assert conn.rows[13] == {
        "name": "minimum-coverage",
        "checksum": migrations.POSTGRES_MINIMUM_COVERAGE.checksum,
    }
    assert conn.rows[14] == {
        "name": "independent-vulnerability-verifier",
        "checksum": migrations.POSTGRES_INDEPENDENT_VERIFIER.checksum,
    }
    assert conn.rows[15] == {
        "name": "assessment-report-finalization",
        "checksum": migrations.POSTGRES_ASSESSMENT_REPORT_FINALIZATION.checksum,
    }
    assert conn.rows[16] == {
        "name": "default-copy-policy-v2",
        "checksum": migrations.POSTGRES_DEFAULT_COPY_POLICY.checksum,
    }
    assert conn.rows[17] == {
        "name": "assessment-report-generation-claims",
        "checksum": migrations.POSTGRES_ASSESSMENT_REPORT_TOKENS.checksum,
    }
    assert conn.rows[18] == {
        "name": "default-copy-v2-scenario",
        "checksum": migrations.POSTGRES_DEFAULT_COPY_V2_SCENARIO.checksum,
    }
    assert conn.rows[19] == {
        "name": "optional-project-resources-v1",
        "checksum": migrations.POSTGRES_OPTIONAL_PROJECT_RESOURCES.checksum,
    }
    assert conn.rows[20] == {
        "name": "retire-legacy-assessment-scenarios",
        "checksum": migrations.POSTGRES_RETIRED_ASSESSMENT_SCENARIOS.checksum,
    }
    assert conn.rows[21] == {
        "name": "immutable-advisory-libraries-v1",
        "checksum": migrations.POSTGRES_ADVISORY_LIBRARIES.checksum,
    }
    assert conn.rows[22] == {
        "name": "advisory-library-bindings-v1",
        "checksum": migrations.POSTGRES_ADVISORY_LIBRARY_BINDINGS.checksum,
    }
    assert conn.rows[23] == {
        "name": "user-password-lifecycle-v1",
        "checksum": migrations.POSTGRES_USER_PASSWORD_LIFECYCLE.checksum,
    }
    assert conn.rows[24] == {
        "name": "scenario-creation-templates-v1",
        "checksum": migrations.POSTGRES_SCENARIO_CREATION_TEMPLATES.checksum,
    }
    assert conn.rows[25] == {
        "name": "manual-versioned-assessment-reports-v1",
        "checksum": migrations.POSTGRES_MANUAL_REPORTS.checksum,
    }
    assert conn.rows[26] == {
        "name": "capability-based-report-execution-v1",
        "checksum": migrations.POSTGRES_REPORT_EXECUTION_PLANS.checksum,
    }
    assert conn.rows[27] == {
        "name": "safe-project-resource-imports-v1",
        "checksum": migrations.POSTGRES_PROJECT_RESOURCE_IMPORTS.checksum,
    }
    assert conn.rows[28] == {
        "name": "project-resource-batch-expiry-index-v1",
        "checksum": migrations.POSTGRES_PROJECT_RESOURCE_BATCH_EXPIRY_INDEX.checksum,
    }
    assert conn.rows[29] == {
        "name": "default-copy-v2-display-label-v1",
        "checksum": migrations.POSTGRES_DEFAULT_COPY_V2_DISPLAY_LABEL.checksum,
    }
    assert conn.rows[30] == {
        "name": "ai-agent-security-scenario-v1",
        "checksum": migrations.POSTGRES_AI_AGENT_SECURITY_SCENARIO.checksum,
    }
    assert conn.rows[31] == {
        "name": "injection-payload-records-v1",
        "checksum": migrations.POSTGRES_INJECTION_PAYLOAD_RECORDS.checksum,
    }
    assert conn.rows[32] == {
        "name": "canonical-vulnerability-consistency-v1",
        "checksum": migrations.POSTGRES_CANONICAL_VULNERABILITY_CONSISTENCY.checksum,
    }
    assert conn.rows[33] == {
        "name": "verifier-schedule-key-consistency-v1",
        "checksum": migrations.POSTGRES_VERIFIER_SCHEDULE_KEY_CONSISTENCY.checksum,
    }
    assert conn.rows[34] == {
        "name": "assessment-progress-and-pi-workers-v1",
        "checksum": migrations.POSTGRES_ASSESSMENT_PROGRESS.checksum,
    }
    assert conn.rows[35] == {
        "name": "project-execution-lifecycle-v1",
        "checksum": migrations.PROJECT_EXECUTION_LIFECYCLE.checksum,
    }
    assert conn.rows[36] == {
        "name": "execution-event-ledger-v1",
        "checksum": migrations.POSTGRES_EXECUTION_EVENT_LEDGER.checksum,
    }
    assert conn.rows[37] == {
        "name": "assessment-report-finish-receipts-v1",
        "checksum": migrations.POSTGRES_ASSESSMENT_REPORT_FINISH_RECEIPTS.checksum,
    }
    assert conn.rows[38] == {
        "name": "assessment-report-claim-digests-v1",
        "checksum": migrations.POSTGRES_ASSESSMENT_REPORT_CLAIM_DIGEST.checksum,
    }
    assert conn.rows[39] == {
        "name": "assessment-charge-compensations-v1",
        "checksum": migrations.POSTGRES_ASSESSMENT_CHARGE_COMPENSATIONS.checksum,
    }
    assert conn.rows[40] == {
        "name": "project-authorization-epochs-v1",
        "checksum": migrations.POSTGRES_PROJECT_AUTHORIZATION.checksum,
    }
    assert conn.rows[41] == {
        "name": "task-run-candidate-store-v1",
        "checksum": migrations.POSTGRES_TASK_RUN_CANDIDATE_STORE.checksum,
    }
    assert conn.rows[42] == {
        "name": "goal-driven-execution-v1",
        "checksum": migrations.POSTGRES_GOAL_DRIVEN_EXECUTION.checksum,
    }
    assert {row["id"]: row["display_name"] for row in conn.model_rows} == {
        "model_late": "claude-test (2)",
        "model_other_tenant": "claude-test",
        "model_early": "claude-test",
        "model_blank": "Model",
    }
    assert conn.statements == [
        "ALTER TABLE tenant_models ADD COLUMN display_name TEXT NOT NULL DEFAULT ''",
        "CREATE UNIQUE INDEX ux_tenant_models_tenant_display_name ON tenant_models(tenant_id, display_name)",
        "DROP TABLE digest_worker;",
        "ALTER TABLE tenant_models ALTER COLUMN display_name DROP DEFAULT;",
        "ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS assessment_attempt_id TEXT",
        "CREATE INDEX IF NOT EXISTS idx_task_runs_assessment_attempt ON task_runs(project_id, assessment_attempt_id, started_at)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0, 1));",
        "ALTER TABLE projects ADD COLUMN execution_generation INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE task_runs ADD COLUMN execution_generation INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE task_runs ADD COLUMN pause_requested INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE task_runs ADD COLUMN pause_interrupted INTEGER NOT NULL DEFAULT 0",
        "CREATE INDEX idx_task_runs_pause ON task_runs(project_id, pause_requested)",
    ]
    assert conn.scripts == [
        migrations.POSTGRES_IDENTITY_TENANT_V2,
        migrations.POSTGRES_TENANT_MODELS_V3,
        migrations.POSTGRES_TENANT_WORKERS_V4,
        migrations.POSTGRES_TENANT_MODEL_PROTOCOL_CONFIG_V6,
        migrations.POSTGRES_DEFAULT_COPY_ASSESSMENT_V9,
        migrations.ASSESSMENT_ATTEMPTS_V10,
        migrations.POSTGRES_BLOCKER_COOLDOWN_V11,
        migrations.POSTGRES_SURFACE_MANIFEST_V12,
        migrations.POSTGRES_MINIMUM_COVERAGE_V13,
        migrations.POSTGRES_INDEPENDENT_VERIFIER_V14,
        migrations.POSTGRES_ASSESSMENT_REPORT_FINALIZATION_V15,
        migrations.POSTGRES_DEFAULT_COPY_POLICY_V16,
        migrations.POSTGRES_ASSESSMENT_REPORT_TOKENS_V17,
        migrations.DEFAULT_COPY_V2_SCENARIO_V18,
        migrations.POSTGRES_OPTIONAL_PROJECT_RESOURCES_V19,
        migrations.RETIRED_ASSESSMENT_SCENARIOS_V20,
        migrations.POSTGRES_ADVISORY_LIBRARIES_V21,
        migrations.ADVISORY_LIBRARY_BINDINGS_V22,
        migrations.POSTGRES_SCENARIO_CREATION_TEMPLATES_V24,
        migrations.POSTGRES_MANUAL_REPORTS_V25,
        migrations.POSTGRES_REPORT_EXECUTION_PLANS_V26,
        migrations.POSTGRES_PROJECT_RESOURCE_IMPORTS_V27,
        migrations.PROJECT_RESOURCE_BATCH_EXPIRY_INDEX_V28,
        migrations.DEFAULT_COPY_V2_DISPLAY_LABEL_V29,
        migrations.AI_AGENT_SECURITY_SCENARIO_V30,
        migrations.INJECTION_PAYLOAD_RECORDS_V31,
        migrations.POSTGRES_CANONICAL_VULNERABILITY_CONSISTENCY_V32,
        migrations.POSTGRES_VERIFIER_SCHEDULE_KEY_CONSISTENCY_V33,
        migrations.POSTGRES_ASSESSMENT_PROGRESS_V34,
        migrations.POSTGRES_EXECUTION_EVENT_LEDGER_V36,
        migrations.POSTGRES_ASSESSMENT_REPORT_FINISH_RECEIPTS_V37,
        migrations.POSTGRES_ASSESSMENT_REPORT_CLAIM_DIGEST_V38,
        migrations.POSTGRES_ASSESSMENT_CHARGE_COMPENSATIONS_V39,
        migrations.POSTGRES_PROJECT_AUTHORIZATION_V40,
        migrations.POSTGRES_TASK_RUN_CANDIDATE_STORE_V41,
        migrations.POSTGRES_GOAL_DRIVEN_EXECUTION_V42,
    ]
    scripts = "\n".join(conn.scripts)
    assert "ALTER TABLE projects ADD COLUMN IF NOT EXISTS scenario_snapshot_json" in scripts
    assert "CREATE TABLE IF NOT EXISTS vulnerability_observations" in scripts
    assert "CREATE TABLE IF NOT EXISTS assessment_attempts" in scripts
    assert "CREATE TABLE IF NOT EXISTS surface_manifest_entries" in scripts
    assert "CREATE TABLE IF NOT EXISTS coverage_scope_dispositions" in scripts
    assert "CREATE TABLE IF NOT EXISTS vulnerability_verifications" in scripts
    assert "CREATE TABLE IF NOT EXISTS assessment_report_finalizations" in scripts
    assert "CREATE TABLE IF NOT EXISTS assessment_charge_compensations" in scripts
    assert "CREATE TABLE IF NOT EXISTS report_templates" in scripts
    assert "CREATE TABLE IF NOT EXISTS report_template_version_profiles" in scripts
    assert "report_execution_plan_sha256" in scripts
    assert "report_prompt_digest_sha256" in scripts
    assert "DROP TABLE model_operation_tests" in scripts
    assert "WHEN 'anthropic' THEN 'anthropic-messages'" in scripts
    assert "WHEN 'openai' THEN 'openai-responses'" in scripts
    assert "CREATE TABLE users" not in migrations.POSTGRES_V1_SCHEMA
    assert "tenant_id" not in migrations.POSTGRES_V1_SCHEMA


@pytest.mark.skipif(
    not os.environ.get("CAIRN_TEST_POSTGRES_URL"),
    reason="CAIRN_TEST_POSTGRES_URL is not configured",
)
def test_postgres_connection_and_baseline(tmp_path: Path) -> None:
    database_url = os.environ["CAIRN_TEST_POSTGRES_URL"]
    db.configure(database_url=database_url, artifact_path=tmp_path / "artifacts")

    with db.get_conn() as conn:
        conn.execute("DELETE FROM counters WHERE name = ?", ("postgres-test",))
        cursor = conn.execute(
            "INSERT INTO counters (name, value) VALUES (?, ?)",
            ("postgres-test", 1),
        )
        assert cursor.rowcount == 1
        row = conn.execute(
            "SELECT name, value FROM counters WHERE name = ?",
            ("postgres-test",),
        ).fetchone()
        assert row == {"name": "postgres-test", "value": 1}
        migration = conn.execute(
            "SELECT name FROM schema_migrations WHERE version = ?",
            (1,),
        ).fetchone()
        assert migration["name"] == "current-postgres-baseline"
        settings = conn.execute(
            "SELECT intent_timeout, reason_timeout FROM settings WHERE id = ?",
            (1,),
        ).fetchone()
        assert settings == {"intent_timeout": 60, "reason_timeout": 180}
        conn.execute("DELETE FROM counters WHERE name = ?", ("postgres-test",))
