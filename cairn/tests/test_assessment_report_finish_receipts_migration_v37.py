"""v37 报告完成收据迁移与新库 schema 回归。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cairn.server import db, migrations


@pytest.fixture(autouse=True)
def reset_database_configuration():
    db._reset_for_tests()
    yield
    db._reset_for_tests()


def _sqlite_connection(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _insert_project_and_report_run(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    task_run_id: str,
) -> None:
    conn.execute(
        """
        INSERT INTO projects (
            id, title, status, created_at, tenant_id, execution_generation
        ) VALUES (?, ?, 'active', '2026-09-18T00:00:00Z',
                  'tenant_legacy', 1)
        """,
        (project_id, project_id),
    )
    conn.execute(
        """
        INSERT INTO task_runs (
            id, project_id, task_type, worker_name, worker_type, status,
            runtime_context_json, execution_generation
        ) VALUES (?, ?, 'report', 'worker-a', 'pi', 'failed', '{}', 1)
        """,
        (task_run_id, project_id),
    )


def _insert_receipt(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    task_run_id: str,
) -> None:
    conn.execute(
        """
        INSERT INTO assessment_report_finish_receipts (
            project_id, task_run_id, protocol, generation_token,
            request_sha256, request_json, response_json,
            artifact_id, artifact_sha256,
            input_snapshot_artifact_id, input_snapshot_sha256,
            accepted_at
        ) VALUES (?, ?, 'cairn-assessment-report-finish-v1', 'generation-1',
                  ?, '{}', '{}', NULL, NULL, NULL, NULL,
                  '2026-09-18T00:01:00Z')
        """,
        (project_id, task_run_id, "a" * 64),
    )


def test_v37_upgrades_v36_without_inventing_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _sqlite_connection(tmp_path / "assessment-report-receipts-v37.db")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                db,
                "SQLITE_MIGRATIONS",
                tuple(
                    migration
                    for migration in migrations.SQLITE_MIGRATIONS
                    if migration.version <= 36
                ),
            )
            db.run_migrations(conn, "sqlite")
            conn.commit()

        _insert_project_and_report_run(
            conn,
            project_id="p1",
            task_run_id="tr1",
        )
        prefix = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM schema_migrations WHERE version <= 36 "
                "ORDER BY version"
            ).fetchall()
        ]
        conn.commit()

        db.run_migrations(conn, "sqlite")
        conn.commit()

        assert prefix == [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM schema_migrations WHERE version <= 36 "
                "ORDER BY version"
            ).fetchall()
        ]
        migration_v37 = conn.execute(
            "SELECT version, name, checksum FROM schema_migrations "
            "WHERE version = 37"
        ).fetchone()
        assert dict(migration_v37) == {
            "version": 37,
            "name": "assessment-report-finish-receipts-v1",
            "checksum": (
                migrations.SQLITE_ASSESSMENT_REPORT_FINISH_RECEIPTS.checksum
            ),
        }
        assert conn.execute(
            "SELECT COUNT(*) AS count "
            "FROM assessment_report_finish_receipts"
        ).fetchone()["count"] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

        registry = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        db.run_migrations(conn, "sqlite")
        conn.commit()
        assert registry == [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
    finally:
        conn.close()


def test_sqlite_v37_receipts_are_constrained_immutable_and_cascading(
    tmp_path: Path,
) -> None:
    conn = _sqlite_connection(tmp_path / "assessment-report-receipts.db")
    try:
        db.run_migrations(conn, "sqlite")
        _insert_project_and_report_run(
            conn,
            project_id="p1",
            task_run_id="tr1",
        )
        _insert_project_and_report_run(
            conn,
            project_id="p2",
            task_run_id="tr2",
        )
        _insert_receipt(conn, project_id="p1", task_run_id="tr1")
        _insert_receipt(conn, project_id="p2", task_run_id="tr2")
        conn.commit()

        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(assessment_report_finish_receipts)"
            ).fetchall()
        }
        assert columns == {
            "project_id",
            "task_run_id",
            "protocol",
            "generation_token",
            "request_sha256",
            "request_json",
            "response_json",
            "artifact_id",
            "artifact_sha256",
            "input_snapshot_artifact_id",
            "input_snapshot_sha256",
            "accepted_at",
        }
        indexes = {
            row["name"]
            for row in conn.execute(
                "PRAGMA index_list(assessment_report_finish_receipts)"
            ).fetchall()
        }
        assert "idx_assessment_report_finish_receipts_generation" in indexes

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE assessment_report_finish_receipts "
                "SET response_json = '{\"changed\":true}' "
                "WHERE project_id = 'p1' AND task_run_id = 'tr1'"
            )
        conn.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO assessment_report_finish_receipts (
                    project_id, task_run_id, protocol, generation_token,
                    request_sha256, request_json, response_json,
                    artifact_id, artifact_sha256,
                    input_snapshot_artifact_id, input_snapshot_sha256,
                    accepted_at
                ) VALUES ('p1', 'tr1', 'unknown', 'generation-1',
                          ?, '{}', '{}', NULL, NULL, NULL, NULL,
                          '2026-09-18T00:02:00Z')
                """,
                ("b" * 64,),
            )
        conn.rollback()

        conn.execute("DELETE FROM task_runs WHERE id = 'tr1'")
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM assessment_report_finish_receipts "
            "WHERE project_id = 'p1'"
        ).fetchone()["count"] == 0

        conn.execute("DELETE FROM projects WHERE id = 'p2'")
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM assessment_report_finish_receipts"
        ).fetchone()["count"] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_fresh_sqlite_schema_contains_v37_without_changing_v1() -> None:
    assert migrations.SQLITE_V1_SCHEMA is migrations.SCHEMA
    assert migrations.SQLITE_BASELINE.checksum == (
        "487a28b3bdc801b24ab0976e6a5c551c95fe26b0b439b117e861fff380c51f17"
    )

    conn = _sqlite_connection(":memory:")
    try:
        conn.executescript(db.SCHEMA)
        table = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' "
            "AND name = 'assessment_report_finish_receipts'"
        ).fetchone()
        trigger = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'trigger' "
            "AND name = 'trg_assessment_report_finish_receipts_no_update'"
        ).fetchone()
    finally:
        conn.close()

    assert table is not None
    assert trigger is not None
    assert "assessment_report_finish_receipts" not in migrations.SQLITE_V1_SCHEMA
    assert "assessment_report_finish_receipts" in db.SCHEMA
    assert migrations.SQLITE_MIGRATIONS[-1].version == 41
    assert migrations.POSTGRES_MIGRATIONS[-1].version == 41


def test_postgres_v37_contract_preserves_receipt_identity_and_immutability() -> None:
    sql = " ".join(
        migrations.POSTGRES_ASSESSMENT_REPORT_FINISH_RECEIPTS_V37.split()
    )

    assert "PRIMARY KEY (project_id, task_run_id)" in sql
    assert (
        "FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE"
        in sql
    )
    assert (
        "FOREIGN KEY (task_run_id, project_id) "
        "REFERENCES task_runs(id, project_id) ON DELETE CASCADE"
        in sql
    )
    assert "protocol = 'cairn-assessment-report-finish-v1'" in sql
    assert "BEFORE UPDATE ON assessment_report_finish_receipts" in sql
    assert "BEFORE DELETE ON assessment_report_finish_receipts" not in sql
    assert (
        migrations.POSTGRES_ASSESSMENT_REPORT_FINISH_RECEIPTS.checksum
        != migrations.SQLITE_ASSESSMENT_REPORT_FINISH_RECEIPTS.checksum
    )
