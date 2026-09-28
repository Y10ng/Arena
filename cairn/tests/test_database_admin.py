from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

from click.testing import CliRunner
import pytest

from cairn.cli import main
from cairn.server import database_admin, db
from cairn.server.artifact_paths import UnsafeArtifactPath, resolve_artifact_path


@pytest.fixture(autouse=True)
def reset_database_configuration():
    db._reset_for_tests()
    yield
    db._reset_for_tests()


def _configured_source(tmp_path: Path) -> tuple[Path, Path]:
    database_path = tmp_path / "source.db"
    artifact_root = tmp_path / "artifacts"
    db.configure(database_path, artifact_path=artifact_root)
    db._reset_for_tests()
    return database_path, artifact_root


def test_status_missing_sqlite_is_read_only_and_non_creating(tmp_path: Path) -> None:
    database_path = tmp_path / "missing" / "source.db"

    with pytest.raises(database_admin.DatabaseAdminError, match="does not exist"):
        database_admin.status(db_path=database_path)

    assert not database_path.exists()
    assert not database_path.parent.exists()


def test_status_reads_current_sqlite_without_mutating_file(tmp_path: Path) -> None:
    database_path, _ = _configured_source(tmp_path)
    before = database_path.stat().st_mtime_ns

    result = database_admin.status(db_path=database_path)

    assert result.backend == "sqlite"
    assert result.classification == "initialized"
    assert result.migration_versions == (
        1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
        21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37,
        38, 39, 40, 41,
    )
    assert result.counts["scenarios"] == 3
    assert result.counts["project_resources"] == 0
    assert result.counts["assessment_charge_compensations"] == 0
    assert result.counts["project_authorization_grants"] == 0
    assert database_path.stat().st_mtime_ns == before


def test_status_cli_redacts_postgres_password(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = database_admin.AdminResult(
        operation="status",
        backend="postgresql",
        database_url="postgresql://user:***@db.example/cairn",
        dry_run=False,
        classification="initialized",
        migration_versions=(1,),
        counts={},
    )
    monkeypatch.setattr(database_admin, "status", lambda **kwargs: expected)

    result = CliRunner().invoke(
        main,
        ["db", "status", "--database-url", "postgresql://user:secret@db.example/cairn"],
    )

    assert result.exit_code == 0
    assert "secret" not in result.output
    assert "***" in result.output


def test_status_explicit_db_path_overrides_database_url_env(tmp_path: Path) -> None:
    database_path, _ = _configured_source(tmp_path)

    result = CliRunner().invoke(
        main,
        ["db", "status", "--db-path", str(database_path)],
        env={db.DEFAULT_DATABASE_URL_ENV: "postgresql://ignored:secret@invalid/cairn"},
    )

    assert result.exit_code == 0
    assert json.loads(result.output)["backend"] == "sqlite"
    assert "secret" not in result.output


def test_cli_reports_missing_sqlite_without_creating_it(tmp_path: Path) -> None:
    database_path = tmp_path / "missing.db"

    result = CliRunner().invoke(main, ["db", "status", "--db-path", str(database_path)])

    assert result.exit_code != 0
    assert "does not exist" in result.output
    assert not database_path.exists()


def test_database_cli_does_not_expose_sqlite_import() -> None:
    result = CliRunner().invoke(main, ["db", "--help"])

    assert result.exit_code == 0
    assert "import-sqlite" not in result.output


def test_artifact_validation_checks_path_size_and_digest(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    content = b"evidence"
    path = root / "projects" / "proj_001" / "runs" / "unscoped" / "a001-log.txt"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    row = {
        "id": "a001",
        "project_id": "proj_001",
        "task_run_id": None,
        "uri": "artifact://projects/proj_001/runs/unscoped/a001-log.txt",
        "size": len(content),
        "sha256": sha256(content).hexdigest(),
    }

    database_admin._validate_artifacts([row], root)

    row["sha256"] = "0" * 64
    with pytest.raises(database_admin.DatabaseAdminError, match="SHA-256"):
        database_admin._validate_artifacts([row], root)


def test_shared_artifact_resolver_rejects_unsafe_segments_and_symlink_escape(
    tmp_path: Path,
) -> None:
    for project_id, task_run_id in (("../outside", None), ("proj_001", "../outside")):
        with pytest.raises(UnsafeArtifactPath):
            resolve_artifact_path(
                tmp_path,
                {
                    "project_id": project_id,
                    "task_run_id": task_run_id,
                    "uri": f"artifact://projects/{project_id}/runs/{task_run_id or 'unscoped'}/file",
                },
            )
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "proj_001").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UnsafeArtifactPath):
        resolve_artifact_path(
            tmp_path,
            {
                "project_id": "proj_001",
                "task_run_id": None,
                "uri": "artifact://projects/proj_001/runs/unscoped/file",
            },
        )


def test_shared_artifact_resolver_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(UnsafeArtifactPath):
        resolve_artifact_path(
            tmp_path,
            {
                "project_id": "proj_001",
                "task_run_id": None,
                "uri": "artifact://projects/proj_001/runs/unscoped/../secret",
            },
        )


def test_database_url_redaction_handles_encoded_credentials() -> None:
    value = db.redact_database_url(
        "postgresql://user:p%40ss@db.example:5432/cairn?sslmode=require&passfile=%2Fsecret&password=query"
    )

    assert value == (
        "postgresql://user:***@db.example:5432/cairn?"
        "sslmode=require&passfile=%2A%2A%2A&password=%2A%2A%2A"
    )
    assert "p%40ss" not in value
    assert "secret" not in value
