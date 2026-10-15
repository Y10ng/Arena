"""SQLite 初始化与轻量迁移层，负责本地状态库和 artifact 根目录。"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from json import dumps
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Generator
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse

import psycopg
from psycopg.rows import dict_row

from cairn.paths import resolve_source_relative_path

DEFAULT_DB = Path.home() / ".local" / "share" / "cairn" / "cairn.db"
DEFAULT_DATABASE_URL_ENV = "CAIRN_DATABASE_URL"
DEFAULT_ARTIFACT_ROOT_ENV = "CAIRN_ARTIFACT_ROOT"

from cairn.server.db_defaults import (
    DEFAULT_KNOWLEDGE_ROOT,
    DEFAULT_REPORT_TEMPLATES,
    DEFAULT_SCENARIOS,
    FACT_PROVENANCE_BACKFILL_COUNTER,
    KNOWLEDGE_BASE_ENV_VAR,
)
from cairn.server.db_schema import FRESH_SCHEMA as SCHEMA
from cairn.server.migrations import (
    POSTGRES_MIGRATIONS,
    SQLITE_MIGRATIONS,
    ensure_registry,
    verify_or_apply,
)

@dataclass(frozen=True)
class DatabaseConfig:
    backend: str
    database_url: str
    sqlite_path: Path | None
    artifact_path: Path


_config: DatabaseConfig | None = None
_configuring: DatabaseConfig | None = None
_configure_lock = Lock()


class SQLiteConnection(sqlite3.Connection):
    """托管连接保存文件副作用的事务回调，不改变普通 SQLite 事务模式。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.after_commit: list[Callable[[], None]] = []
        self.after_rollback: list[Callable[[], None]] = []
        self.callbacks_pending = False


class PostgresConnection:
    """保留现有 conn.execute 调用形状，并在边界适配 psycopg 参数与映射行。"""

    def __init__(self, connection: psycopg.Connection) -> None:
        self._connection = connection
        self.after_commit: list[Callable[[], None]] = []
        self.after_rollback: list[Callable[[], None]] = []
        self.callbacks_pending = False

    def execute(self, sql: str, params: Any = None):
        adapted = _adapt_qmark_sql(sql)
        adapted = adapted.replace("julianday(", "cairn_julianday(").replace("MAX(0,", "GREATEST(0,")
        if params is None:
            return self._connection.execute(adapted)
        return self._connection.execute(adapted, params)

    def executescript(self, sql: str) -> None:
        self._connection.execute(sql, prepare=False)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


def _adapt_qmark_sql(sql: str) -> str:
    """只替换 SQL 代码中的 qmark，跳过字面量、注释与 dollar quote。"""
    result: list[str] = []
    index = 0
    state: str | None = None
    dollar_tag: str | None = None
    while index < len(sql):
        if state == "line_comment":
            char = sql[index]
            result.append(char)
            index += 1
            if char == "\n":
                state = None
            continue
        if state == "block_comment":
            if sql.startswith("*/", index):
                result.append("*/")
                index += 2
                state = None
            else:
                result.append(sql[index])
                index += 1
            continue
        if state == "dollar_quote":
            assert dollar_tag is not None
            if sql.startswith(dollar_tag, index):
                result.append(dollar_tag)
                index += len(dollar_tag)
                state = None
                dollar_tag = None
            else:
                result.append(sql[index])
                index += 1
            continue
        if state in {"single_quote", "double_quote"}:
            quote = "'" if state == "single_quote" else '"'
            char = sql[index]
            result.append(char)
            index += 1
            if char == quote:
                if index < len(sql) and sql[index] == quote:
                    result.append(sql[index])
                    index += 1
                else:
                    state = None
            continue

        if sql.startswith("--", index):
            result.append("--")
            index += 2
            state = "line_comment"
        elif sql.startswith("/*", index):
            result.append("/*")
            index += 2
            state = "block_comment"
        elif sql[index] in {"'", '"'}:
            result.append(sql[index])
            state = "single_quote" if sql[index] == "'" else "double_quote"
            index += 1
        elif sql[index] == "$":
            tag_end = sql.find("$", index + 1)
            candidate = sql[index : tag_end + 1] if tag_end >= 0 else ""
            if candidate and (candidate == "$$" or candidate[1:-1].replace("_", "a").isalnum()):
                result.append(candidate)
                index += len(candidate)
                state = "dollar_quote"
                dollar_tag = candidate
            else:
                result.append("$")
                index += 1
        elif sql[index] == "?":
            result.append("%s")
            index += 1
        else:
            result.append(sql[index])
            index += 1
    return "".join(result)


def configure(
    path: Path | None = None,
    *,
    database_url: str | None = None,
    artifact_path: Path | None = None,
) -> None:
    global _config, _configuring
    with _configure_lock:
        if _config is not None:
            return
        candidate = _resolve_config(
            path=path,
            database_url=database_url,
            artifact_path=artifact_path,
        )
        if candidate.backend == "sqlite":
            assert candidate.sqlite_path is not None
            candidate.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        candidate.artifact_path.mkdir(parents=True, exist_ok=True)
        _configuring = candidate
        try:
            with get_conn() as conn:
                run_migrations(conn, candidate.backend, initialize_runtime=True)
            _config = candidate
        finally:
            _configuring = None


def _resolve_config(
    *,
    path: Path | None,
    database_url: str | None,
    artifact_path: Path | None,
) -> DatabaseConfig:
    explicit_url = (database_url or "").strip()
    if path is not None and explicit_url:
        raise ValueError("path and database_url cannot be configured together")
    # 显式 SQLite 路径优先于环境变量，保留既有 deploy 脚本的覆盖能力。
    configured_url = explicit_url or (
        "" if path is not None else os.environ.get(DEFAULT_DATABASE_URL_ENV, "").strip()
    )
    if configured_url:
        parsed = urlparse(configured_url)
        if parsed.scheme not in {"sqlite", "postgres", "postgresql"}:
            raise ValueError(f"Unsupported database backend: {parsed.scheme}")
        if parsed.scheme != "sqlite":
            return DatabaseConfig(
                backend="postgresql",
                database_url=configured_url,
                sqlite_path=None,
                artifact_path=_resolve_artifact_path(artifact_path, DEFAULT_DB.parent / "artifacts"),
            )
        sqlite_path = _sqlite_path_from_url(parsed)
    else:
        sqlite_path = (path or DEFAULT_DB).expanduser().resolve()
        configured_url = f"sqlite:///{sqlite_path}"
    return DatabaseConfig(
        backend="sqlite",
        database_url=configured_url,
        sqlite_path=sqlite_path,
        artifact_path=_resolve_artifact_path(artifact_path, sqlite_path.parent / "artifacts"),
    )


def _resolve_artifact_path(configured: Path | None, default: Path) -> Path:
    env_value = os.environ.get(DEFAULT_ARTIFACT_ROOT_ENV, "").strip()
    candidate = configured or (Path(env_value) if env_value else default)
    return candidate.expanduser().resolve()


def _sqlite_path_from_url(parsed) -> Path:
    if parsed.netloc not in {"", "localhost"}:
        raise ValueError("SQLite database URL must reference a local path")
    raw_path = unquote(parsed.path)
    if not raw_path:
        raise ValueError("SQLite database URL is missing a database path")
    return Path(raw_path).expanduser().resolve()


def redact_database_url(database_url: str) -> str:
    """隐藏数据库 URL 中的口令及 query 凭据，同时保留可诊断目标。"""
    parsed = urlparse(database_url)
    netloc = parsed.netloc
    if parsed.password is not None:
        username = quote(unquote(parsed.username or ""), safe="")
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = f"{username}:***@{hostname}"
        if parsed.port is not None:
            netloc += f":{parsed.port}"
    sensitive = {"password", "passfile", "sslpassword", "sslkey", "sslcert"}
    query = urlencode(
        [(key, "***" if key.lower() in sensitive else value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)]
    )
    return parsed._replace(netloc=netloc, query=query).geturl()


def artifact_root() -> Path:
    assert _config is not None
    return _config.artifact_path


def database_config() -> DatabaseConfig:
    assert _config is not None
    return _config


def _reset_for_tests() -> None:
    global _config, _configuring
    _config = None
    _configuring = None


def _prepare_legacy_sqlite_baseline(conn: sqlite3.Connection) -> None:
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'settings'"
    ).fetchone()
    if table is None:
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "id" not in columns:
        # 新基线会写显式单例 id，因此先让旧 rowid 表具备兼容列。
        conn.execute("ALTER TABLE settings ADD COLUMN id INTEGER NOT NULL DEFAULT 1")


def _ensure_schema_migrations(conn: sqlite3.Connection, *, initialize_runtime: bool = True) -> None:
    # 迁移函数保持幂等：每次启动都可运行，缺什么补什么。
    for table_name in ("infra_runtime", "infra_container", "infra_workers", "infra_common_env"):
        conn.execute(f"DROP TABLE IF EXISTS {table_name}")

    settings_columns = {row["name"] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "id" not in settings_columns:
        # 历史 SQLite 表以隐式 rowid 标识单例；显式 id 让运行时 SQL 可跨数据库复用。
        conn.execute("ALTER TABLE settings ADD COLUMN id INTEGER NOT NULL DEFAULT 1")
    # 旧库 intent_timeout/reason_timeout 默认 15s 太短，多 explore 并发写争锁时心跳易误杀。
    # 迁移到 60s，给心跳足够余量。
    current_timeout = conn.execute("SELECT intent_timeout, reason_timeout FROM settings WHERE id = 1").fetchone()
    if current_timeout is not None and current_timeout["intent_timeout"] <= 15:
        conn.execute("UPDATE settings SET intent_timeout = 60, reason_timeout = 60 WHERE id = 1")
    # Phase 4+ follow-up：reason lease 从 60s 放大到 180s，减少 reason 跑长任务时心跳写失败
    # 导致的 heartbeat_lost 误杀（proj_018 tr052 这类）。保留用户主动调大（>60）的值。
    if current_timeout is not None and "reason_timeout" in current_timeout.keys() and current_timeout["reason_timeout"] <= 60:
        conn.execute("UPDATE settings SET reason_timeout = 180 WHERE id = 1")

    # Phase 4 收益递减阈值列（旧库幂等补加）。
    if "coverage_idle_rounds" not in settings_columns:
        conn.execute("ALTER TABLE settings ADD COLUMN coverage_idle_rounds INTEGER NOT NULL DEFAULT 3")

    project_columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
    if "scenario_key" not in project_columns:
        conn.execute("ALTER TABLE projects ADD COLUMN scenario_key TEXT NOT NULL DEFAULT 'default'")
    if "preferred_workers_json" not in project_columns:
        # preferred_workers_json 存储项目指定的 worker name 数组，空数组表示不限。
        conn.execute("ALTER TABLE projects ADD COLUMN preferred_workers_json TEXT NOT NULL DEFAULT '[]'")
    if "group_id" not in project_columns:
        # group_id 把项目归入项目组；FK ON DELETE SET NULL 在建表语句中定义，迁移仅补列。
        conn.execute("ALTER TABLE projects ADD COLUMN group_id TEXT REFERENCES project_groups(id) ON DELETE SET NULL")
    # Phase 4 收敛状态列（仅 coverage_gate 场景读，加性迁移）。
    if "reason_idle_streak" not in project_columns:
        conn.execute("ALTER TABLE projects ADD COLUMN reason_idle_streak INTEGER NOT NULL DEFAULT 0")
    if "reason_last_fact_count" not in project_columns:
        conn.execute("ALTER TABLE projects ADD COLUMN reason_last_fact_count INTEGER NOT NULL DEFAULT 0")
    if "explore_budget" not in project_columns:
        conn.execute("ALTER TABLE projects ADD COLUMN explore_budget INTEGER")
    if "explore_deadline" not in project_columns:
        conn.execute("ALTER TABLE projects ADD COLUMN explore_deadline TEXT")
    if "scenario_snapshot_json" not in project_columns:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN scenario_snapshot_json TEXT NOT NULL DEFAULT '{}'"
        )
    if "assessment_protocol" not in project_columns:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN assessment_protocol TEXT NOT NULL DEFAULT ''"
        )
    if "vulnerability_fingerprint_version" not in project_columns:
        conn.execute("ALTER TABLE projects ADD COLUMN vulnerability_fingerprint_version INTEGER")
    if "explore_attempts_started" not in project_columns:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN explore_attempts_started INTEGER NOT NULL DEFAULT 0"
        )
    for column in (
        "coverage_checkpoint_surface_count",
        "coverage_checkpoint_cell_count",
        "coverage_checkpoint_terminal_count",
        "coverage_checkpoint_required_count",
        "coverage_checkpoint_optional_count",
        "coverage_checkpoint_required_terminal_count",
    ):
        if column not in project_columns:
            conn.execute(
                f"ALTER TABLE projects ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
            )
    if "coverage_stop_reason" not in project_columns:
        conn.execute("ALTER TABLE projects ADD COLUMN coverage_stop_reason TEXT")

    # project_groups 表在 SCHEMA 中定义；旧库迁移时幂等补建。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_groups (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            owner TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("INSERT INTO counters (name, value) VALUES ('project_group', 0) ON CONFLICT DO NOTHING")

    # worker_templates 表在 SCHEMA 中定义；旧库迁移时幂等补建。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS worker_templates (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            owner TEXT NOT NULL,
            worker_names_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("INSERT INTO counters (name, value) VALUES ('worker_template', 0) ON CONFLICT DO NOTHING")

    scenario_columns = {row["name"] for row in conn.execute("PRAGMA table_info(scenarios)").fetchall()}
    if scenario_columns:
        if "profile_id" not in scenario_columns:
            conn.execute("ALTER TABLE scenarios ADD COLUMN profile_id TEXT NOT NULL DEFAULT ''")
        if "worker_ids_json" not in scenario_columns:
            conn.execute("ALTER TABLE scenarios ADD COLUMN worker_ids_json TEXT NOT NULL DEFAULT '[]'")
        if "sort_order" not in scenario_columns:
            conn.execute("ALTER TABLE scenarios ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE scenarios SET sort_order = rowid")
        if "origin_template" not in scenario_columns:
            conn.execute("ALTER TABLE scenarios ADD COLUMN origin_template TEXT")
        if "goal_template" not in scenario_columns:
            conn.execute("ALTER TABLE scenarios ADD COLUMN goal_template TEXT")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_resources (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('note', 'script', 'file')),
            scope TEXT NOT NULL DEFAULT 'project' CHECK (scope = 'project'),
            path TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
            sha256 TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            deactivated_at TEXT,
            PRIMARY KEY (id, project_id),
            UNIQUE (project_id, path),
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_project_resources_project_active
        ON project_resources(project_id, active, created_at, id)
        """
    )

    # 旧库默认场景标签从"默认自治探索"改为"默认探索"，幂等只更新旧值。
    conn.execute(
        "UPDATE scenarios SET label = '默认探索' WHERE key = 'default' AND label = '默认自治探索'"
    )
    if initialize_runtime:
        _ensure_knowledge_root()

    fact_columns = {row["name"] for row in conn.execute("PRAGMA table_info(facts)").fetchall()}
    if "task_run_id" not in fact_columns:
        conn.execute("ALTER TABLE facts ADD COLUMN task_run_id TEXT")
    if "creator" not in fact_columns:
        conn.execute("ALTER TABLE facts ADD COLUMN creator TEXT")
    if "created_at" not in fact_columns:
        conn.execute("ALTER TABLE facts ADD COLUMN created_at TEXT")

    intent_source_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(intent_sources)").fetchall()
    }
    if intent_source_columns and "source_order" not in intent_source_columns:
        conn.execute("ALTER TABLE intent_sources ADD COLUMN source_order INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE intent_sources SET source_order = rowid")

    intent_columns = {row["name"] for row in conn.execute("PRAGMA table_info(intents)").fetchall()}
    if "task_run_id" not in intent_columns:
        conn.execute("ALTER TABLE intents ADD COLUMN task_run_id TEXT")
    if "worker_history_json" not in intent_columns:
        conn.execute("ALTER TABLE intents ADD COLUMN worker_history_json TEXT NOT NULL DEFAULT '[]'")
    if "conclusion" not in intent_columns:
        # conclusion 收敛方式：NULL=未收敛(open)；completed/abandoned/failed。
        conn.execute("ALTER TABLE intents ADD COLUMN conclusion TEXT")
        # 旧库回填：已收敛且有结论 fact 视为 completed；已收敛但无 fact（旧 complete 强制收尾）视为 abandoned。
        conn.execute(
            "UPDATE intents SET conclusion = 'completed' "
            "WHERE concluded_at IS NOT NULL AND to_fact_id IS NOT NULL AND conclusion IS NULL"
        )
        conn.execute(
            "UPDATE intents SET conclusion = 'abandoned' "
            "WHERE concluded_at IS NOT NULL AND to_fact_id IS NULL AND conclusion IS NULL"
        )
    if "depends_on" not in intent_columns:
        # depends_on: 执行顺序约束，引用同项目另一 intent id；NULL=无依赖。
        conn.execute("ALTER TABLE intents ADD COLUMN depends_on TEXT")
    if "slot_key" not in intent_columns:
        # slot_key: pentest 场景把 intent 绑定到 coverage_slots 槽位；其他场景始终 NULL。
        conn.execute("ALTER TABLE intents ADD COLUMN slot_key TEXT")
    if "strategy_key" not in intent_columns:
        conn.execute(
            "ALTER TABLE intents ADD COLUMN strategy_key TEXT NOT NULL DEFAULT ''"
        )

    # coverage_slots 表在 SCHEMA 中定义；旧库迁移时幂等补建（仅 pentest 场景读写）。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS coverage_slots (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            slot_key TEXT NOT NULL,
            kind TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            source_intent_id TEXT,
            assigned_intent_id TEXT,
            resulting_fact_id TEXT,
            verified_run_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (project_id, slot_key)
        )
        """
    )
    coverage_columns = {row["name"] for row in conn.execute("PRAGMA table_info(coverage_slots)").fetchall()}
    if "tier" not in coverage_columns:
        # Phase 4 分层：core(bootstrap 首轮)/extended(过程发现)；旧 slot 默认 extended。
        conn.execute("ALTER TABLE coverage_slots ADD COLUMN tier TEXT NOT NULL DEFAULT 'extended'")
    coverage_column_defs = {
        "surface_key": "TEXT",
        "surface_kind": "TEXT NOT NULL DEFAULT ''",
        "test_family": "TEXT NOT NULL DEFAULT ''",
        "applicability_basis": "TEXT NOT NULL DEFAULT ''",
        "completion_criteria": "TEXT NOT NULL DEFAULT ''",
        "result": "TEXT",
        "skip_reason": "TEXT NOT NULL DEFAULT ''",
        "blocker_fingerprint": "TEXT",
        "blocker_strategy_key": "TEXT NOT NULL DEFAULT ''",
        "blocker_cooldown_until": "TEXT",
        "surface_id": "TEXT",
        "coverage_requirement": "TEXT NOT NULL DEFAULT ''",
        "coverage_rule_id": "TEXT NOT NULL DEFAULT ''",
    }
    for column, definition in coverage_column_defs.items():
        if column not in coverage_columns:
            conn.execute(f"ALTER TABLE coverage_slots ADD COLUMN {column} {definition}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_coverage_slots_project_status ON coverage_slots (project_id, status)"
    )
    conn.execute("DROP INDEX IF EXISTS ux_coverage_slots_project_surface_family")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_coverage_slots_project_surface_kind_family "
        "ON coverage_slots(project_id, surface_kind, surface_key, test_family) "
        "WHERE surface_key IS NOT NULL AND surface_key != '' AND test_family != ''"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_coverage_slots_project_surface_id_family "
        "ON coverage_slots(project_id, surface_id, test_family) "
        "WHERE surface_id IS NOT NULL AND surface_id != '' AND test_family != ''"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_coverage_slots_project_requirement_status "
        "ON coverage_slots(project_id, coverage_requirement, status)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS surface_manifest_entries (
            entry_id TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            surface_id TEXT NOT NULL,
            surface_kind TEXT NOT NULL,
            surface_key TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            traits_json TEXT NOT NULL DEFAULT '{}',
            evidence_json TEXT NOT NULL DEFAULT '[]',
            source_intent_id TEXT,
            source_fact_id TEXT,
            source_task_run_id TEXT,
            observation_fingerprint TEXT NOT NULL,
            manifest_version INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'observed',
            created_at TEXT NOT NULL,
            PRIMARY KEY (entry_id, project_id),
            UNIQUE (project_id, observation_fingerprint)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_surface_manifest_project_surface "
        "ON surface_manifest_entries(project_id, surface_id, created_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS coverage_scope_dispositions (
            disposition_id TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            disposition_kind TEXT NOT NULL,
            target_snapshot_digest TEXT NOT NULL,
            target_ids_json TEXT NOT NULL DEFAULT '[]',
            reason TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '[]',
            source_intent_id TEXT NOT NULL,
            source_fact_id TEXT NOT NULL,
            source_task_run_id TEXT NOT NULL,
            observation_fingerprint TEXT NOT NULL,
            disposition_version INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'observed',
            created_at TEXT NOT NULL,
            PRIMARY KEY (disposition_id, project_id),
            UNIQUE (project_id, observation_fingerprint)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_coverage_scope_dispositions_project_created "
        "ON coverage_scope_dispositions(project_id, created_at)"
    )

    # vulnerabilities 表在 SCHEMA 中定义；旧库迁移时幂等补建（所有场景通用，自动确认+人工独立创建）。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vulnerabilities (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            severity TEXT NOT NULL,
            vuln_type TEXT NOT NULL DEFAULT '',
            target TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0.5,
            status TEXT NOT NULL DEFAULT 'new',
            source_fact_id TEXT,
            packet_artifact_id TEXT,
            remediation TEXT NOT NULL DEFAULT '',
            discovered_by_run_id TEXT,
            discovered_by_worker TEXT,
            slot_key TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (id, project_id),
            FOREIGN KEY (source_fact_id, project_id) REFERENCES facts(id, project_id) ON DELETE SET NULL,
            FOREIGN KEY (packet_artifact_id, project_id) REFERENCES artifacts(id, project_id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_vulnerabilities_project_status ON vulnerabilities (project_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_vulnerabilities_project_severity ON vulnerabilities (project_id, severity)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_vulnerabilities_fact ON vulnerabilities (project_id, source_fact_id)"
    )
    conn.execute("INSERT INTO counters (name, value) VALUES ('vulnerability', 0) ON CONFLICT DO NOTHING")

    # repro_request/repro_response：worker 内联返回的原始 HTTP 请求/响应包，旧库幂等补加。
    vuln_columns = {row["name"] for row in conn.execute("PRAGMA table_info(vulnerabilities)").fetchall()}
    if "repro_request" not in vuln_columns:
        conn.execute("ALTER TABLE vulnerabilities ADD COLUMN repro_request TEXT NOT NULL DEFAULT ''")
    if "repro_response" not in vuln_columns:
        conn.execute("ALTER TABLE vulnerabilities ADD COLUMN repro_response TEXT NOT NULL DEFAULT ''")
    # repro_steps：报告阶段生成的编号复现步骤（markdown），旧库幂等补加。
    if "repro_steps" not in vuln_columns:
        conn.execute("ALTER TABLE vulnerabilities ADD COLUMN repro_steps TEXT NOT NULL DEFAULT ''")
    if "fingerprint" not in vuln_columns:
        conn.execute("ALTER TABLE vulnerabilities ADD COLUMN fingerprint TEXT")
    if "identity_json" not in vuln_columns:
        conn.execute(
            "ALTER TABLE vulnerabilities ADD COLUMN identity_json TEXT NOT NULL DEFAULT '{}'"
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_vulnerabilities_project_fingerprint "
        "ON vulnerabilities(project_id, fingerprint) WHERE fingerprint IS NOT NULL"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vulnerability_observations (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            vulnerability_id TEXT NOT NULL,
            source_fact_id TEXT,
            task_run_id TEXT,
            worker TEXT,
            slot_key TEXT,
            packet_artifact_id TEXT,
            identity_json TEXT NOT NULL DEFAULT '{}',
            severity TEXT NOT NULL,
            confidence REAL NOT NULL,
            repro_request TEXT NOT NULL DEFAULT '',
            repro_response TEXT NOT NULL DEFAULT '',
            observed_at TEXT NOT NULL,
            FOREIGN KEY (vulnerability_id, project_id)
                REFERENCES vulnerabilities(id, project_id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_vulnerability_observations_vulnerability "
        "ON vulnerability_observations(project_id, vulnerability_id, observed_at)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_vulnerability_observations_id_project "
        "ON vulnerability_observations(id, project_id)"
    )

    intent_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(intents)").fetchall()
    }
    if "assessment_role" not in intent_columns:
        conn.execute(
            "ALTER TABLE intents ADD COLUMN assessment_role "
            "TEXT NOT NULL DEFAULT 'discovery'"
        )
    if "target_observation_id" not in intent_columns:
        conn.execute("ALTER TABLE intents ADD COLUMN target_observation_id TEXT")
    if "verifier_schedule_key" not in intent_columns:
        conn.execute("ALTER TABLE intents ADD COLUMN verifier_schedule_key TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_intents_verifier_observation "
        "ON intents(project_id, target_observation_id, created_at) "
        "WHERE assessment_role = 'verifier' AND target_observation_id IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_intents_verifier_schedule "
        "ON intents(project_id, verifier_schedule_key) "
        "WHERE assessment_role = 'verifier' AND verifier_schedule_key IS NOT NULL"
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_intents_verifier_schedule_key_insert
        BEFORE INSERT ON intents
        FOR EACH ROW
        WHEN NEW.assessment_role = 'verifier'
         AND NEW.target_observation_id IS NOT NULL
         AND NEW.verifier_schedule_key IS NOT NEW.target_observation_id
        BEGIN
            SELECT RAISE(ABORT, 'verifier_schedule_key must match target_observation_id');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_intents_verifier_schedule_key_update
        BEFORE UPDATE OF assessment_role, target_observation_id, verifier_schedule_key ON intents
        FOR EACH ROW
        WHEN NEW.assessment_role = 'verifier'
         AND NEW.target_observation_id IS NOT NULL
         AND NEW.verifier_schedule_key IS NOT NEW.target_observation_id
        BEGIN
            SELECT RAISE(ABORT, 'verifier_schedule_key must match target_observation_id');
        END
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vulnerability_verifications (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            vulnerability_id TEXT NOT NULL,
            observation_id TEXT NOT NULL,
            intent_id TEXT NOT NULL,
            fact_id TEXT NOT NULL,
            verifier_task_run_id TEXT NOT NULL,
            verifier_worker TEXT NOT NULL,
            discovery_task_run_id TEXT NOT NULL,
            discovery_worker TEXT,
            independence_level TEXT NOT NULL,
            outcome TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '[]',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY (id, project_id),
            UNIQUE (project_id, intent_id),
            FOREIGN KEY (vulnerability_id, project_id)
                REFERENCES vulnerabilities(id, project_id) ON DELETE CASCADE,
            FOREIGN KEY (observation_id, project_id)
                REFERENCES vulnerability_observations(id, project_id) ON DELETE CASCADE,
            FOREIGN KEY (intent_id, project_id)
                REFERENCES intents(id, project_id) ON DELETE CASCADE,
            FOREIGN KEY (fact_id, project_id)
                REFERENCES facts(id, project_id) ON DELETE CASCADE,
            FOREIGN KEY (verifier_task_run_id, project_id)
                REFERENCES task_runs(id, project_id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_vulnerability_verifications_observation "
        "ON vulnerability_verifications(project_id, observation_id, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_vulnerability_verifications_vulnerability "
        "ON vulnerability_verifications(project_id, vulnerability_id, outcome, created_at)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS assessment_report_finalizations (
            project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            version INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            claimed_by TEXT,
            lease_expires_at TEXT,
            next_attempt_at TEXT,
            artifact_id TEXT,
            last_error TEXT NOT NULL DEFAULT '',
            generation_started_at TEXT NOT NULL,
            generation_token TEXT NOT NULL,
            claim_token TEXT,
            claim_sha256 TEXT CHECK (
                claim_sha256 IS NULL OR length(claim_sha256) = 64
            ),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (artifact_id, project_id)
                REFERENCES artifacts(id, project_id) ON DELETE SET NULL
        )
        """
    )
    finalization_columns = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(assessment_report_finalizations)"
        ).fetchall()
    }
    if "generation_token" not in finalization_columns:
        conn.execute(
            "ALTER TABLE assessment_report_finalizations "
            "ADD COLUMN generation_token TEXT"
        )
    if "claim_token" not in finalization_columns:
        conn.execute(
            "ALTER TABLE assessment_report_finalizations ADD COLUMN claim_token TEXT"
        )
    if "claim_sha256" not in finalization_columns:
        conn.execute(
            "ALTER TABLE assessment_report_finalizations "
            "ADD COLUMN claim_sha256 TEXT CHECK ("
            "claim_sha256 IS NULL OR length(claim_sha256) = 64)"
        )
    for column, definition in {
        "requested_at": "TEXT",
        "requested_by": "TEXT",
        "template_key": "TEXT",
        "template_version": "INTEGER",
        "template_digest_sha256": "TEXT",
        "template_markdown": "TEXT",
        "input_snapshot_artifact_id": "TEXT",
        "input_snapshot_sha256": "TEXT",
        "report_policy_version": "INTEGER",
        "report_execution_plan_json": "TEXT",
        "report_execution_plan_sha256": "TEXT",
        "report_prompt_digest_sha256": "TEXT",
        "report_prompt_markdown": "TEXT",
    }.items():
        if column not in finalization_columns:
            conn.execute(
                f"ALTER TABLE assessment_report_finalizations ADD COLUMN {column} {definition}"
            )
    conn.execute(
        """
        UPDATE assessment_report_finalizations
        SET generation_token = 'legacy:' || project_id || ':' || generation_started_at
        WHERE generation_token IS NULL OR generation_token = ''
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_assessment_report_finalizations_dispatch "
        "ON assessment_report_finalizations(status, next_attempt_at, lease_expires_at, updated_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS report_templates (
            key TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            archived_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS report_template_versions (
            template_key TEXT NOT NULL REFERENCES report_templates(key) ON DELETE RESTRICT,
            version INTEGER NOT NULL CHECK (version > 0),
            digest_sha256 TEXT NOT NULL,
            markdown_content TEXT NOT NULL,
            message TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (template_key, version)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_report_template_versions_created "
        "ON report_template_versions(template_key, version DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS report_template_version_profiles (
            template_key TEXT NOT NULL,
            template_version INTEGER NOT NULL,
            profile_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (template_key, template_version, profile_id),
            FOREIGN KEY (template_key, template_version)
                REFERENCES report_template_versions(template_key, version) ON DELETE RESTRICT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_report_template_version_profiles_profile "
        "ON report_template_version_profiles(profile_id, template_key, template_version)"
    )
    for template in DEFAULT_REPORT_TEMPLATES:
        content = str(template["markdown"]).strip()
        created_at = "2026-08-28T00:00:00Z"
        conn.execute(
            """
            INSERT INTO report_templates (key, name, description, created_at)
            VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING
            """,
            (
                template["key"],
                template["name"],
                template["description"],
                created_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO report_template_versions (
                template_key, version, digest_sha256, markdown_content,
                message, created_by, created_at
            ) VALUES (?, 1, ?, ?, '内置初始版本', 'system', ?)
            ON CONFLICT DO NOTHING
            """,
            (
                template["key"],
                sha256(content.encode("utf-8")).hexdigest(),
                content,
                created_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO report_template_version_profiles (
                template_key, template_version, profile_id, created_at
            ) VALUES (?, 1, 'assessment-report-packet-v1', ?)
            ON CONFLICT DO NOTHING
            """,
            (template["key"], created_at),
        )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS assessment_attempts (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            intent_id TEXT NOT NULL,
            slot_key TEXT,
            assessment_role TEXT NOT NULL DEFAULT 'discovery',
            status TEXT NOT NULL DEFAULT 'active',
            charged INTEGER NOT NULL DEFAULT 1,
            execution_count INTEGER NOT NULL DEFAULT 0,
            max_runtime_retries INTEGER NOT NULL DEFAULT 2,
            strategy_key TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            ended_at TEXT,
            outcome TEXT,
            PRIMARY KEY (id, project_id),
            FOREIGN KEY (intent_id, project_id)
                REFERENCES intents(id, project_id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_assessment_attempts_project_intent "
        "ON assessment_attempts(project_id, intent_id, status, started_at)"
    )

    hint_columns = {row["name"] for row in conn.execute("PRAGMA table_info(hints)").fetchall()}
    if "fact_id" not in hint_columns:
        # fact_id 把提示绑定到具体 fact 节点，空表示项目级提示；FK 在建表语句中定义，迁移仅补列。
        conn.execute("ALTER TABLE hints ADD COLUMN fact_id TEXT")
    if "intent_id" not in hint_columns:
        # intent_id 把提示绑定到具体 intent 线索（探索方向），比 fact 更精确地引导思考过程。
        conn.execute("ALTER TABLE hints ADD COLUMN intent_id TEXT")

    task_run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(task_runs)").fetchall()}
    if "runtime_context_json" not in task_run_columns:
        conn.execute("ALTER TABLE task_runs ADD COLUMN runtime_context_json TEXT NOT NULL DEFAULT '{}'")
    if "assessment_attempt_id" not in task_run_columns:
        conn.execute("ALTER TABLE task_runs ADD COLUMN assessment_attempt_id TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_runs_assessment_attempt "
        "ON task_runs(project_id, assessment_attempt_id, started_at)"
    )

    artifact_columns = {row["name"] for row in conn.execute("PRAGMA table_info(artifacts)").fetchall()}
    if artifact_columns and "category" not in artifact_columns:
        conn.execute("ALTER TABLE artifacts ADD COLUMN category TEXT NOT NULL DEFAULT 'evidence'")
        # 历史 artifact 没有 category，按 kind 回填成证据/交付物两个大类。
        conn.execute(
            """
            UPDATE artifacts
            SET category = CASE
                WHEN kind IN ('report', 'screenshot', 'patch', 'file') THEN 'deliverable'
                ELSE 'evidence'
            END
            """
        )

    dispatcher_columns = {row["name"] for row in conn.execute("PRAGMA table_info(dispatcher_status)").fetchall()}
    if dispatcher_columns:
        if "started_at" not in dispatcher_columns:
            conn.execute("ALTER TABLE dispatcher_status ADD COLUMN started_at TEXT")
        if "host" not in dispatcher_columns:
            conn.execute("ALTER TABLE dispatcher_status ADD COLUMN host TEXT")
        if "config_path" not in dispatcher_columns:
            conn.execute("ALTER TABLE dispatcher_status ADD COLUMN config_path TEXT")
        if "config_mtime" not in dispatcher_columns:
            conn.execute("ALTER TABLE dispatcher_status ADD COLUMN config_mtime INTEGER")
        if "config_sha256" not in dispatcher_columns:
            conn.execute("ALTER TABLE dispatcher_status ADD COLUMN config_sha256 TEXT")
        if "runtime_json" not in dispatcher_columns:
            conn.execute("ALTER TABLE dispatcher_status ADD COLUMN runtime_json TEXT NOT NULL DEFAULT '{}'")
        if "container_json" not in dispatcher_columns:
            conn.execute("ALTER TABLE dispatcher_status ADD COLUMN container_json TEXT NOT NULL DEFAULT '{}'")
        if "server_reachable_path" not in dispatcher_columns:
            conn.execute("ALTER TABLE dispatcher_status ADD COLUMN server_reachable_path TEXT")
        if "worker_cooldowns_json" not in dispatcher_columns:
            conn.execute("ALTER TABLE dispatcher_status ADD COLUMN worker_cooldowns_json TEXT NOT NULL DEFAULT '[]'")
        if "worker_rejections_json" not in dispatcher_columns:
            conn.execute("ALTER TABLE dispatcher_status ADD COLUMN worker_rejections_json TEXT NOT NULL DEFAULT '[]'")
        if "worker_pool_json" not in dispatcher_columns:
            conn.execute("ALTER TABLE dispatcher_status ADD COLUMN worker_pool_json TEXT NOT NULL DEFAULT '[]'")
        if "container_images_json" not in dispatcher_columns:
            conn.execute("ALTER TABLE dispatcher_status ADD COLUMN container_images_json TEXT NOT NULL DEFAULT '[]'")
        if "cap_baseline_json" not in dispatcher_columns:
            conn.execute("ALTER TABLE dispatcher_status ADD COLUMN cap_baseline_json TEXT")
        if "declared_inventory_json" not in dispatcher_columns:
            conn.execute("ALTER TABLE dispatcher_status ADD COLUMN declared_inventory_json TEXT NOT NULL DEFAULT '{}'")

    # 兼容仅使用 SQLite schema/bootstrap 的隔离测试，不推断或回填历史评估阶段。
    for table, columns in (
        ("projects", {"assessment_progress_json": "TEXT", "verification_attempts_started": "INTEGER NOT NULL DEFAULT 0"}),
        ("intents", {"depth_review_fact_id": "TEXT", "conclusion_reason": "TEXT"}),
        ("facts", {"assessment_record_json": "TEXT"}),
    ):
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, definition in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    if not _migration_counter_is_set(conn, FACT_PROVENANCE_BACKFILL_COUNTER):
        # provenance 回填只跑一次，避免人工修正后的历史记录被再次覆盖。
        _backfill_fact_provenance(conn)
        _mark_migration_counter(conn, FACT_PROVENANCE_BACKFILL_COUNTER)


def _initialize_postgres_defaults(conn) -> None:
    # 保留历史初始化的幂等补列，默认场景统一在完整迁移后写入。
    conn.execute("ALTER TABLE scenarios ADD COLUMN IF NOT EXISTS origin_template TEXT")
    conn.execute("ALTER TABLE scenarios ADD COLUMN IF NOT EXISTS goal_template TEXT")


def _seed_default_scenarios(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT COUNT(*) AS count FROM scenarios").fetchone()
    if row is not None and row["count"] > 0:
        return
    # 只有空表才灌默认场景，用户已有配置时绝不追加或覆盖。
    for sort_order, scenario in enumerate(DEFAULT_SCENARIOS):
        conn.execute(
            """
            INSERT INTO scenarios
                (key, label, is_default, profile_id, worker_ids_json,
                 image, prompt_group, knowledge, origin_template, goal_template,
                 cap_add_json, features_json, enabled_workers_json, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scenario["key"],
                scenario["label"],
                1 if scenario["is_default"] else 0,
                scenario["profile_id"],
                dumps(scenario.get("worker_ids", []), ensure_ascii=False),
                scenario["image"],
                scenario["prompt_group"],
                scenario["knowledge"],
                scenario.get("origin_template"),
                scenario.get("goal_template"),
                dumps(scenario["cap_add"], ensure_ascii=False),
                dumps(scenario["features"], ensure_ascii=False),
                dumps(scenario.get("enabled_workers", []), ensure_ascii=False),
                sort_order,
            ),
        )


def _ensure_knowledge_root() -> None:
    # 只确保资料根目录可用，不再生成绑定退役 Prompt 的占位知识集，也不修改已有资料。
    _default_knowledge_root().mkdir(parents=True, exist_ok=True)


def _default_knowledge_root() -> Path:
    configured = os.environ.get(KNOWLEDGE_BASE_ENV_VAR, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return resolve_source_relative_path(DEFAULT_KNOWLEDGE_ROOT, anchor=Path(__file__))


def _migration_counter_is_set(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT value FROM counters WHERE name = ?", (name,)).fetchone()
    return bool(row and row["value"] == 1)


def _mark_migration_counter(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        """
        INSERT INTO counters (name, value) VALUES (?, 1)
        ON CONFLICT(name) DO UPDATE SET value = excluded.value
        """,
        (name,),
    )


def _backfill_fact_provenance(conn: sqlite3.Connection) -> None:
    # created_at 优先取收敛 intent 的 concluded_at，缺失时退回项目创建时间。
    conn.execute(
        """
        UPDATE facts
        SET created_at = COALESCE(
                (
                    SELECT i.concluded_at
                    FROM intents i
                    WHERE i.project_id = facts.project_id
                      AND i.to_fact_id = facts.id
                      AND i.concluded_at IS NOT NULL
                    ORDER BY i.concluded_at DESC, i.id DESC
                    LIMIT 1
                ),
                (
                    SELECT p.created_at
                    FROM projects p
                    WHERE p.id = facts.project_id
                ),
                '1970-01-01T00:00:00Z'
            )
        WHERE created_at IS NULL OR created_at = ''
        """
    )
    conn.execute(
        """
        UPDATE facts
        SET creator = COALESCE(
                (
                    SELECT COALESCE(i.worker, i.creator)
                    FROM intents i
                    WHERE i.project_id = facts.project_id
                      AND i.to_fact_id = facts.id
                    ORDER BY COALESCE(i.concluded_at, i.created_at) DESC, i.id DESC
                    LIMIT 1
                ),
                'system'
            )
        WHERE creator IS NULL OR creator = ''
        """
    )
    conn.execute(
        """
        UPDATE facts
        SET task_run_id = (
            SELECT tr.id
            FROM intents i
            JOIN task_runs tr
              ON tr.project_id = i.project_id
             AND tr.intent_id = i.id
            WHERE i.project_id = facts.project_id
              AND i.to_fact_id = facts.id
            ORDER BY COALESCE(tr.ended_at, tr.started_at, tr.id) DESC, tr.id DESC
            LIMIT 1
        )
        WHERE task_run_id IS NULL
          AND EXISTS (
              SELECT 1
              FROM sqlite_master
              WHERE type = 'table' AND name = 'task_runs'
          )
        """
    )


def open_connection(config: DatabaseConfig) -> Any:
    """打开非全局原始连接；调用方负责事务与关闭。"""
    if config.backend == "sqlite":
        assert config.sqlite_path is not None
        conn = sqlite3.connect(str(config.sqlite_path), timeout=30, factory=SQLiteConnection)
        conn.row_factory = sqlite3.Row
        # WAL 让本地 UI 读写和 dispatcher 写入更少互相阻塞；其余约束依赖每连接开启。
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        # REPLACE 的隐式删除也必须经过不可变表的删除触发器。
        conn.execute("PRAGMA recursive_triggers=ON")
        # busy_timeout：多 explore 同时超时回收时并发写会争锁，让写等锁而非直接返回 SQLITE_BUSY/500，
        # 避免心跳写因瞬态锁争抢失败导致 heartbeat_lost 误杀。
        conn.execute("PRAGMA busy_timeout=10000")
        return conn
    return PostgresConnection(psycopg.connect(config.database_url, row_factory=dict_row))


def run_migrations(conn: Any, backend: str, *, initialize_runtime: bool = False) -> None:
    """在调用方事务中串行迁移；运行期可另外初始化知识目录。"""
    if backend == "sqlite":
        conn.execute("BEGIN IMMEDIATE")
        ensure_registry(conn)
        _prepare_legacy_sqlite_baseline(conn)
        normalize = _ensure_schema_migrations if initialize_runtime else (
            lambda target: _ensure_schema_migrations(target, initialize_runtime=False)
        )
        for migration in SQLITE_MIGRATIONS:
            verify_or_apply(
                conn,
                migration,
                normalize if migration.version == 1 else (lambda _target: None),
            )
        _seed_default_scenarios(conn)
        return
    conn.execute("SELECT pg_advisory_xact_lock(?)", (0x434149524E,))
    ensure_registry(conn)
    for migration in POSTGRES_MIGRATIONS:
        verify_or_apply(
            conn,
            migration,
            _initialize_postgres_defaults if migration.version == 1 else (lambda _target: None),
        )
    _seed_default_scenarios(conn)
    if initialize_runtime:
        _ensure_knowledge_root()


def register_after_commit(conn, callback: Callable[[], None]) -> None:
    """仅在托管外层事务成功提交后执行无参数回调。"""
    if not isinstance(conn, (SQLiteConnection, PostgresConnection)) or not conn.callbacks_pending:
        raise RuntimeError("Transaction callbacks require a managed connection")
    conn.after_commit.append(callback)


def register_after_rollback(conn, callback: Callable[[], None]) -> None:
    """外层事务失败时清理本次新副本，不删除已提交的输入。"""
    if not isinstance(conn, (SQLiteConnection, PostgresConnection)) or not conn.callbacks_pending:
        raise RuntimeError("Transaction callbacks require a managed connection")
    conn.after_rollback.append(callback)


def _run_transaction_callbacks(conn, *, committed: bool) -> None:
    callbacks = conn.after_commit if committed else conn.after_rollback
    conn.callbacks_pending = False
    errors: list[Exception] = []
    try:
        # 一个清理失败不能阻止其他本次副本的清理；错误仍向调用方暴露。
        for callback in callbacks:
            try:
                callback()
            except Exception as exc:
                errors.append(exc)
    finally:
        conn.after_commit.clear()
        conn.after_rollback.clear()
    if errors:
        raise ExceptionGroup("Transaction callbacks failed", errors)


@contextmanager
def _managed_connection(*, schedule: bool = False) -> Generator[Any, None, None]:
    active_config = _config or _configuring
    assert active_config is not None
    conn = open_connection(active_config)
    conn.callbacks_pending = True
    try:
        try:
            if schedule and active_config.backend == "sqlite":
                # 计划写事务在读配置前取得写锁，避免 deferred 读快照升级造成并发触发失败。
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            _run_transaction_callbacks(conn, committed=False)
            raise
        else:
            # 提交后清理失败不能再走 rollback 回调，以免删除已绑定资料。
            _run_transaction_callbacks(conn, committed=True)
    finally:
        conn.close()


@contextmanager
def get_conn() -> Generator[Any, None, None]:
    with _managed_connection() as conn:
        yield conn


@contextmanager
def schedule_transaction() -> Generator[Any, None, None]:
    """计划创建、管理和消费共用的写事务；PostgreSQL 行锁由服务按锁序取得。"""
    with _managed_connection(schedule=True) as conn:
        yield conn
