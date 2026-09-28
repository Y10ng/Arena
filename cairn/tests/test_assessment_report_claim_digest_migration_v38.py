"""v38 报告 claim digest 迁移与明文清理回归。"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import sqlite3

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


def _migrate_through_v37(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    *,
    retain_normalized_digest_column: bool = False,
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(
            db,
            "SQLITE_MIGRATIONS",
            tuple(
                migration
                for migration in migrations.SQLITE_MIGRATIONS
                if migration.version <= 37
            ),
        )
        db.run_migrations(conn, "sqlite")
        conn.commit()
    if not retain_normalized_digest_column:
        # 当前 v1 normalization 会补齐运行期列；删除它以精确还原已部署的 v37 形状。
        conn.execute(
            "ALTER TABLE assessment_report_finalizations DROP COLUMN claim_sha256"
        )
        conn.commit()


def _insert_project(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute(
        """
        INSERT INTO projects (
            id, title, status, created_at, tenant_id, execution_generation
        ) VALUES (?, ?, 'completed', '2026-09-18T00:00:00Z',
                  'tenant_legacy', 1)
        """,
        (project_id, project_id),
    )


def _insert_finalization(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    status: str,
    claim_token: str | None,
    claimed_by: str | None,
    lease_expires_at: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO assessment_report_finalizations (
            project_id, status, attempt_count, claimed_by, lease_expires_at,
            last_error, generation_started_at, generation_token, claim_token,
            created_at, updated_at
        ) VALUES (?, ?, 3, ?, ?, '', '2026-09-18T00:00:00Z',
                  ?, ?, '2026-09-18T00:00:00Z', '2026-09-18T00:01:00Z')
        """,
        (
            project_id,
            status,
            claimed_by,
            lease_expires_at,
            f"generation-{project_id}",
            claim_token,
        ),
    )


def test_v38_upgrades_v37_claims_without_changing_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _sqlite_connection(tmp_path / "assessment-report-claims-v38.db")
    active_token = "令牌-café-🔒"
    expired_token = "expired-claim"
    receipt_digest = "a" * 64
    try:
        _migrate_through_v37(conn, monkeypatch)
        for project_id in ("p-active", "p-expired", "p-null"):
            _insert_project(conn, project_id)
        _insert_finalization(
            conn,
            project_id="p-active",
            status="running",
            claim_token=active_token,
            claimed_by="worker-a",
            lease_expires_at="2026-09-18T00:10:00Z",
        )
        _insert_finalization(
            conn,
            project_id="p-expired",
            status="running",
            claim_token=expired_token,
            claimed_by="worker-b",
            lease_expires_at="2026-09-17T23:59:00Z",
        )
        _insert_finalization(
            conn,
            project_id="p-null",
            status="pending",
            claim_token=None,
            claimed_by=None,
            lease_expires_at=None,
        )
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, task_type, worker_name, worker_type, status,
                runtime_context_json, execution_generation
            ) VALUES ('tr-active', 'p-active', 'report', 'worker-a', 'pi',
                      'failed', '{}', 1)
            """
        )
        conn.execute(
            """
            INSERT INTO assessment_report_finish_receipts (
                project_id, task_run_id, protocol, generation_token,
                request_sha256, request_json, response_json,
                artifact_id, artifact_sha256,
                input_snapshot_artifact_id, input_snapshot_sha256,
                accepted_at
            ) VALUES (
                'p-active', 'tr-active',
                'cairn-assessment-report-finish-v1', 'old-generation',
                ?, '{}', '{}', NULL, NULL, NULL, NULL,
                '2026-09-18T00:02:00Z'
            )
            """,
            (receipt_digest,),
        )
        prefix = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM schema_migrations WHERE version <= 37 "
                "ORDER BY version"
            ).fetchall()
        ]
        conn.commit()

        db.run_migrations(conn, "sqlite")
        conn.commit()

        assert prefix == [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM schema_migrations WHERE version <= 37 "
                "ORDER BY version"
            ).fetchall()
        ]
        migration_v38 = conn.execute(
            "SELECT version, name, checksum FROM schema_migrations "
            "WHERE version = 38"
        ).fetchone()
        assert dict(migration_v38) == {
            "version": 38,
            "name": "assessment-report-claim-digests-v1",
            "checksum": migrations.SQLITE_ASSESSMENT_REPORT_CLAIM_DIGEST.checksum,
        }
        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(assessment_report_finalizations)"
            ).fetchall()
        }
        assert {"claim_token", "claim_sha256"} <= columns
        claims = {
            row["project_id"]: dict(row)
            for row in conn.execute(
                "SELECT project_id, status, claimed_by, lease_expires_at, "
                "claim_token, claim_sha256 "
                "FROM assessment_report_finalizations ORDER BY project_id"
            ).fetchall()
        }
        assert claims["p-active"]["claim_token"] is None
        assert claims["p-active"]["claim_sha256"] == sha256(
            active_token.encode("utf-8")
        ).hexdigest()
        assert claims["p-active"]["claimed_by"] == "worker-a"
        assert claims["p-active"]["lease_expires_at"] == "2026-09-18T00:10:00Z"
        assert claims["p-expired"]["claim_token"] is None
        assert claims["p-expired"]["claim_sha256"] == sha256(
            expired_token.encode("utf-8")
        ).hexdigest()
        assert claims["p-null"]["claim_token"] is None
        assert claims["p-null"]["claim_sha256"] is None
        receipt = conn.execute(
            "SELECT request_sha256 FROM assessment_report_finish_receipts "
            "WHERE project_id = 'p-active' AND task_run_id = 'tr-active'"
        ).fetchone()
        assert receipt["request_sha256"] == receipt_digest
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
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM assessment_report_finalizations "
            "WHERE claim_token IS NOT NULL"
        ).fetchone()["count"] == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("claim_token", "claim_sha256", "message"),
    [
        ("legacy-token", "f" * 64, "Conflicting assessment report claim digest"),
        (None, "A" * 64, "Invalid assessment report claim digest"),
    ],
)
def test_v38_rejects_partial_backfill_corruption_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    claim_token: str | None,
    claim_sha256: str,
    message: str,
) -> None:
    conn = _sqlite_connection(tmp_path / f"corrupt-{message.split()[0]}.db")
    try:
        _migrate_through_v37(
            conn,
            monkeypatch,
            retain_normalized_digest_column=True,
        )
        _insert_project(conn, "p1")
        _insert_finalization(
            conn,
            project_id="p1",
            status="running",
            claim_token=claim_token,
            claimed_by="worker-a",
            lease_expires_at="2026-09-18T00:10:00Z",
        )
        conn.execute(
            "UPDATE assessment_report_finalizations SET claim_sha256 = ? "
            "WHERE project_id = 'p1'",
            (claim_sha256,),
        )
        conn.commit()

        with pytest.raises(RuntimeError, match=message):
            db.run_migrations(conn, "sqlite")
        conn.rollback()

        row = conn.execute(
            "SELECT claim_token, claim_sha256 "
            "FROM assessment_report_finalizations WHERE project_id = 'p1'"
        ).fetchone()
        assert row["claim_token"] == claim_token
        assert row["claim_sha256"] == claim_sha256
        assert conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 38"
        ).fetchone() is None
    finally:
        conn.close()


def test_v38_accepts_matching_partial_backfill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _sqlite_connection(tmp_path / "matching-partial-v38.db")
    token = "partially-migrated-token"
    digest = sha256(token.encode("utf-8")).hexdigest()
    try:
        _migrate_through_v37(
            conn,
            monkeypatch,
            retain_normalized_digest_column=True,
        )
        _insert_project(conn, "p1")
        _insert_finalization(
            conn,
            project_id="p1",
            status="running",
            claim_token=token,
            claimed_by="worker-a",
            lease_expires_at="2026-09-18T00:10:00Z",
        )
        conn.execute(
            "UPDATE assessment_report_finalizations SET claim_sha256 = ? "
            "WHERE project_id = 'p1'",
            (digest,),
        )
        conn.commit()

        db.run_migrations(conn, "sqlite")
        conn.commit()

        row = conn.execute(
            "SELECT claim_token, claim_sha256 "
            "FROM assessment_report_finalizations WHERE project_id = 'p1'"
        ).fetchone()
        assert row["claim_token"] is None
        assert row["claim_sha256"] == digest
    finally:
        conn.close()


def test_fresh_sqlite_and_postgres_v38_contracts_preserve_legacy_column() -> None:
    assert migrations.SQLITE_ASSESSMENT_REPORT_TOKENS.checksum == (
        "0f977cc64f398a3ffe39d4d8362bff8dbb2d81353d1f455b7bbdc7b01828109a"
    )
    assert migrations.POSTGRES_ASSESSMENT_REPORT_TOKENS.checksum == (
        "a10f66ef6681cc73101ad45ebb47545593a4264bde53f0295019a563c8ed7418"
    )
    assert migrations.SQLITE_ASSESSMENT_REPORT_FINISH_RECEIPTS.checksum == (
        "3c5e9cb3b8e51ed624cc565152005a06a4101a032c88d5f330a1f9529ed99b7d"
    )
    assert migrations.POSTGRES_ASSESSMENT_REPORT_FINISH_RECEIPTS.checksum == (
        "b50d44a398d2d52026a72d98fc9cfcf7d7311a55d71162c552035c20b49960e6"
    )
    conn = _sqlite_connection(":memory:")
    try:
        db.run_migrations(conn, "sqlite")
        columns = {
            row["name"]: row
            for row in conn.execute(
                "PRAGMA table_info(assessment_report_finalizations)"
            ).fetchall()
        }
    finally:
        conn.close()

    assert "claim_token" in columns
    assert "claim_sha256" in columns
    assert migrations.SQLITE_MIGRATIONS[-1].version == 41
    assert migrations.POSTGRES_MIGRATIONS[-1].version == 41
    postgres_sql = " ".join(
        migrations.POSTGRES_ASSESSMENT_REPORT_CLAIM_DIGEST_V38.split()
    )
    assert "ADD COLUMN IF NOT EXISTS claim_sha256 TEXT" in postgres_sql
    assert "length(claim_sha256) = 64" in postgres_sql
    assert (
        migrations.POSTGRES_ASSESSMENT_REPORT_CLAIM_DIGEST.checksum
        != migrations.SQLITE_ASSESSMENT_REPORT_CLAIM_DIGEST.checksum
    )
