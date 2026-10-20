"""数据库状态检查与 PostgreSQL 迁移。"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from urllib.parse import quote

import psycopg
from psycopg.rows import dict_row

from cairn.server import db
from cairn.server.artifact_paths import UnsafeArtifactPath, resolve_artifact_path

APPLICATION_TABLES = (
    "settings",
    "users",
    "tenants",
    "tenant_memberships",
    "auth_sessions",
    "machine_principals",
    "security_audit_events",
    "tenant_models",
    "model_credentials",
    "model_credential_versions",
    "tenant_workers",
    "scenarios",
    "counters",
    "worker_templates",
    "projects",
    "project_tenant_workers",
    "worker_template_tenant_workers",
    "scoped_counters",
    "facts",
    "intents",
    "intent_sources",
    "hints",
    "task_runs",
    "targets",
    "agent_events",
    "artifacts",
    "project_resources",
    "project_resource_batches",
    "project_resource_batch_members",
    "fact_evidence",
    "coverage_slots",
    "surface_manifest_entries",
    "coverage_scope_dispositions",
    "assessment_charge_compensations",
    "project_authorization_grants",
    "vulnerabilities",
    "vulnerability_observations",
    "dispatcher_status",
    "dispatcher_connectivity_tests",
)


class DatabaseAdminError(RuntimeError):
    """数据库运维拒绝执行或校验失败。"""


@dataclass(frozen=True)
class AdminResult:
    operation: str
    backend: str
    database_url: str
    dry_run: bool
    classification: str
    migration_versions: tuple[int, ...]
    counts: dict[str, int]
    artifacts_validated: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def status(
    *,
    database_url: str | None = None,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
) -> AdminResult:
    """只读检查数据库；SQLite 不存在时直接报错，绝不创建文件或 schema。"""
    config = _admin_config(
        database_url=database_url,
        db_path=db_path,
        artifact_root=artifact_root,
    )
    with _readonly_connection(config) as conn:
        tables = _table_names(conn, config.backend)
        versions = _migration_versions(conn, tables)
        counts = {
            table: _count(conn, table)
            for table in APPLICATION_TABLES
            if table in tables
        }
        classification = _status_classification(tables, counts)
        artifacts_validated = 0
        if artifact_root is not None and "artifacts" in tables:
            rows = _fetch_rows(conn, "artifacts")
            _validate_artifacts(rows, config.artifact_path)
            artifacts_validated = len(rows)
    return AdminResult(
        operation="status",
        backend=config.backend,
        database_url=db.redact_database_url(config.database_url),
        dry_run=False,
        classification=classification,
        migration_versions=versions,
        counts=counts,
        artifacts_validated=artifacts_validated,
    )


def migrate(*, database_url: str, dry_run: bool = False) -> AdminResult:
    """在同一事务和 advisory lock 下执行 PostgreSQL 迁移。"""
    config = _admin_config(database_url=database_url, db_path=None, artifact_root=None)
    if config.backend != "postgresql":
        raise DatabaseAdminError("migrate requires a PostgreSQL database URL")
    conn = db.open_connection(config)
    try:
        before = _table_names(conn, config.backend)
        db.run_migrations(conn, config.backend)
        after = _table_names(conn, config.backend)
        versions = _migration_versions(conn, after)
        counts = {
            table: _count(conn, table)
            for table in APPLICATION_TABLES
            if table in after
        }
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return AdminResult(
        operation="migrate",
        backend=config.backend,
        database_url=db.redact_database_url(config.database_url),
        dry_run=dry_run,
        classification="uninitialized" if not before else "migrated",
        migration_versions=versions,
        counts=counts,
    )


def _admin_config(
    *,
    database_url: str | None,
    db_path: Path | None,
    artifact_root: Path | None,
) -> db.DatabaseConfig:
    try:
        return db._resolve_config(
            path=db_path,
            database_url=database_url,
            artifact_path=artifact_root,
        )
    except ValueError as exc:
        raise DatabaseAdminError(str(exc)) from exc


@contextmanager
def _readonly_connection(config: db.DatabaseConfig) -> Iterator[Any]:
    if config.backend == "sqlite":
        assert config.sqlite_path is not None
        if not config.sqlite_path.is_file():
            raise DatabaseAdminError(
                f"SQLite database does not exist: {config.sqlite_path}"
            )
        uri = f"file:{quote(str(config.sqlite_path), safe='/')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
    else:
        conn = db.PostgresConnection(
            psycopg.connect(config.database_url, row_factory=dict_row)
        )
        conn.execute("SET TRANSACTION READ ONLY")
    try:
        yield conn
        conn.rollback()
    finally:
        conn.close()


def _validate_artifacts(rows: list[dict[str, Any]], root: Path) -> None:
    for row in rows:
        try:
            path = resolve_artifact_path(root, row)
        except UnsafeArtifactPath as exc:
            raise DatabaseAdminError(
                f"Unsafe artifact URI for {row['project_id']}/{row['id']}"
            ) from exc
        if not path.is_file():
            raise DatabaseAdminError(
                f"Artifact file is missing: {row['project_id']}/{row['id']}"
            )
        stat = path.stat()
        if stat.st_size != row["size"]:
            raise DatabaseAdminError(
                f"Artifact size mismatch: {row['project_id']}/{row['id']}"
            )
        digest = sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != row["sha256"]:
            raise DatabaseAdminError(
                f"Artifact SHA-256 mismatch: {row['project_id']}/{row['id']}"
            )


def _table_names(conn: Any, backend: str) -> set[str]:
    if backend == "sqlite":
        return {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            if not row["name"].startswith("sqlite_")
        }
    return {
        row["table_name"]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema()"
        ).fetchall()
    }


def _migration_versions(conn: Any, tables: set[str]) -> tuple[int, ...]:
    if "schema_migrations" not in tables:
        return ()
    return tuple(
        row["version"]
        for row in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )
    )


def _status_classification(tables: set[str], counts: dict[str, int]) -> str:
    if not tables.intersection(set(APPLICATION_TABLES) | {"schema_migrations"}):
        return "uninitialized"
    if set(APPLICATION_TABLES).issubset(tables):
        return "initialized" if any(counts.values()) else "empty-schema"
    return "partial"


def _count(conn: Any, table: str) -> int:
    return conn.execute(f'SELECT COUNT(*) AS count FROM "{table}"').fetchone()[
        "count"
    ]


def _fetch_rows(conn: Any, table: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(f'SELECT * FROM "{table}"').fetchall()
    ]
