"""显式 opt-in 的 PostgreSQL 初始化回归；只使用本测试新建的本地容器和空库。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import time
import uuid

from click.testing import CliRunner
import docker
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest
from fastapi import HTTPException

from cairn.cli import main
from cairn.dispatcher.worker_protocol.submission import build_submit_result_schema
from cairn.server import db, migrations
from cairn.server.auth import (
    RequestPrincipal,
    create_machine_principal,
    hash_secret,
    verify_password,
)
from cairn.server.schemas.candidates import (
    CandidateFrameManifest,
    CandidateOutcomeManifest,
    canonical_json_bytes,
    canonical_value_sha256,
)
from cairn.server.services.candidate_store import (
    capture_candidate,
    capture_candidate_outcome,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1",
    reason="requires explicit disposable local PostgreSQL integration opt-in",
)


@pytest.fixture(scope="module")
def postgres_instance():
    # 不采用外部测试 URL 或 Docker host，也不拉取镜像，避免误连既有数据库。
    client = docker.DockerClient(base_url="unix:///var/run/docker.sock", timeout=10)
    container = None
    password = secrets.token_hex(32)
    try:
        image = client.images.get("postgres:16-alpine")
        container = client.containers.create(
            image.id,
            name="cairn-postgres-validation-" + uuid.uuid4().hex,
            environment={
                "POSTGRES_USER": "bootstrap_test",
                "POSTGRES_PASSWORD": password,
                "POSTGRES_DB": "postgres",
            },
            ports={"5432/tcp": ("127.0.0.1", None)},
            labels={"cairn.test": "postgres-bootstrap"},
        )
        container.start()
        container.reload()
        binding = container.attrs["NetworkSettings"]["Ports"]["5432/tcp"][0]
        assert binding["HostIp"] == "127.0.0.1"
        connection_params = {
            "host": "127.0.0.1",
            "port": int(binding["HostPort"]),
            "user": "bootstrap_test",
            "password": password,
            "dbname": "postgres",
            "connect_timeout": 2,
        }
        deadline = time.monotonic() + 60
        while True:
            try:
                with psycopg.connect(**connection_params) as conn:
                    conn.execute("SELECT 1")
                break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    pytest.fail("disposable PostgreSQL did not become ready within 60 seconds")
                time.sleep(0.2)
        yield connection_params
    finally:
        try:
            if container is not None:
                # 仅清理本 fixture 创建的容器及其匿名数据卷。
                container.remove(force=True, v=True)
        finally:
            client.close()


@pytest.fixture
def empty_postgres_url(postgres_instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    name = "bootstrap_" + uuid.uuid4().hex
    params = postgres_instance
    db._reset_for_tests()
    with psycopg.connect(**params, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
        try:
            url = (
                f"postgresql://{params['user']}:{params['password']}"
                f"@127.0.0.1:{params['port']}/{name}"
            )
            monkeypatch.setenv(db.DEFAULT_DATABASE_URL_ENV, url)
            monkeypatch.setenv(db.DEFAULT_ARTIFACT_ROOT_ENV, str(tmp_path / "artifacts"))
            with psycopg.connect(**{**params, "dbname": name}) as conn:
                assert conn.execute(
                    "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname = 'public'"
                ).fetchone() == (0,)
            yield url
        finally:
            db._reset_for_tests()
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def test_configure_initializes_empty_postgres_with_scenario_templates(
    empty_postgres_url: str, tmp_path: Path,
) -> None:
    db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")

    with db.get_conn() as conn:
        columns = conn.execute(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'scenarios'
              AND column_name IN ('origin_template', 'goal_template')
            ORDER BY column_name
            """
        ).fetchall()
        assert columns == [
            {"column_name": "goal_template", "data_type": "text", "is_nullable": "YES"},
            {"column_name": "origin_template", "data_type": "text", "is_nullable": "YES"},
        ]
        scenario = conn.execute(
            "SELECT origin_template, goal_template FROM scenarios WHERE key = ?",
            ("default-copy-v2",),
        ).fetchone()
        assert scenario is not None
        assert scenario["origin_template"]
        assert scenario["goal_template"]
        applied = conn.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert applied == [
            {"version": migration.version, "name": migration.name, "checksum": migration.checksum}
            for migration in migrations.POSTGRES_MIGRATIONS
        ]
        assert any(row["version"] == 24 for row in applied)
        assert applied[-1]["version"] == migrations.POSTGRES_MIGRATIONS[-1].version


def _seed_v39_compensation_candidate(conn: db.PostgresConnection) -> None:
    now = "2026-09-19T00:00:00Z"
    conn.execute(
        """
        INSERT INTO projects (
            id, title, status, created_at, tenant_id, assessment_protocol,
            scenario_snapshot_json, explore_budget, explore_attempts_started,
            execution_generation
        ) VALUES (
            'migration-project', 'Migration', 'paused', ?, 'tenant_legacy',
            'default-copy-v1', '{}', 40, 1, 3
        )
        """,
        (now,),
    )
    conn.execute(
        "INSERT INTO facts (id, project_id, description, creator, created_at) "
        "VALUES ('origin', 'migration-project', 'authorized origin', 'system', ?)",
        (now,),
    )
    reason_context = json.dumps(
        {
            "accepted_intent_submissions": {
                "0": {"digest": "1" * 64, "intent_id": "intent-1"}
            },
            "execution_generation": 3,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    conn.execute(
        """
        INSERT INTO task_runs (
            id, project_id, task_type, worker_name, worker_type, status,
            started_at, ended_at, runtime_context_json, execution_generation
        ) VALUES (
            'reason-1', 'migration-project', 'reason', 'reason-worker', 'pi',
            'succeeded', ?, ?, ?, 3
        )
        """,
        (now, now, reason_context),
    )
    conn.execute(
        """
        INSERT INTO intents (
            id, project_id, description, creator, task_run_id,
            worker_history_json, created_at, concluded_at, conclusion,
            slot_key, strategy_key, assessment_role, conclusion_reason
        ) VALUES (
            'intent-1', 'migration-project', 'historical obligation',
            'reason-worker', 'reason-1', '[]', ?, ?, 'failed', NULL,
            'authorization.owner-vs-other-v1', 'discovery',
            'server_write_failed'
        )
        """,
        (now, now),
    )
    conn.execute(
        "INSERT INTO intent_sources (intent_id, project_id, fact_id, source_order) "
        "VALUES ('intent-1', 'migration-project', 'origin', 0)"
    )
    conn.execute(
        """
        INSERT INTO assessment_attempts (
            id, project_id, intent_id, slot_key, assessment_role, status,
            charged, execution_count, max_runtime_retries, strategy_key,
            started_at, ended_at, outcome
        ) VALUES (
            'attempt-1', 'migration-project', 'intent-1', NULL, 'discovery',
            'failed', 1, 1, 2, 'authorization.owner-vs-other-v1', ?, ?,
            'server_write_failed'
        )
        """,
        (now, now),
    )
    explore_context = json.dumps(
        {
            "execution_container_ids": [],
            "execution_generation": 3,
            "execution_lifecycle": {
                "generation": 3,
                "process_started": False,
                "termination_confirmed": True,
                "cleanup_confirmed": True,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    conn.execute(
        """
        INSERT INTO task_runs (
            id, project_id, intent_id, task_type, worker_name, worker_type,
            status, started_at, ended_at, error_type, error_message,
            runtime_context_json, assessment_attempt_id,
            execution_generation, pause_requested, pause_interrupted
        ) VALUES (
            'explore-1', 'migration-project', 'intent-1', 'explore',
            'explore-worker', 'pi', 'failed', ?, ?, 'server_write_failed',
            'default-copy intent is missing slot_key', ?, 'attempt-1', 3, 0, 0
        )
        """,
        (now, now, explore_context),
    )


def test_postgres_v39_compensates_only_the_proven_pre_execution_charge(
    empty_postgres_url: str,
    tmp_path: Path,
) -> None:
    with psycopg.connect(empty_postgres_url, row_factory=dict_row) as raw:
        conn = db.PostgresConnection(raw)
        migrations.ensure_registry(conn)
        for migration in migrations.POSTGRES_MIGRATIONS:
            if migration.version > 38:
                break
            migrations.verify_or_apply(conn, migration, lambda _target: None)
        _seed_v39_compensation_candidate(conn)

    db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")
    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT explore_attempts_started FROM projects "
            "WHERE id = 'migration-project'"
        ).fetchone()["explore_attempts_started"] == 0
        attempt = conn.execute(
            "SELECT charged, status, outcome, execution_count "
            "FROM assessment_attempts WHERE project_id = 'migration-project' "
            "AND id = 'attempt-1'"
        ).fetchone()
        assert attempt == {
            "charged": 0,
            "status": "failed",
            "outcome": "server_write_failed",
            "execution_count": 1,
        }
        run = conn.execute(
            "SELECT status, error_type, error_message, session_id "
            "FROM task_runs WHERE project_id = 'migration-project' "
            "AND id = 'explore-1'"
        ).fetchone()
        assert run == {
            "status": "failed",
            "error_type": "server_write_failed",
            "error_message": "default-copy intent is missing slot_key",
            "session_id": None,
        }
        compensation = conn.execute(
            "SELECT reason_code, charge_delta, task_run_ids_json, proof_sha256 "
            "FROM assessment_charge_compensations "
            "WHERE project_id = 'migration-project'"
        ).fetchone()
        assert compensation["reason_code"] == (
            "structured_explore_missing_slot_pre_execution"
        )
        assert compensation["charge_delta"] == -1
        assert compensation["task_run_ids_json"] == '["explore-1"]'
        assert len(compensation["proof_sha256"]) == 64


def test_assessment_charge_compensation_ledger_is_immutable_but_project_deletable(
    empty_postgres_url: str,
    tmp_path: Path,
) -> None:
    db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO projects (
                id, title, status, created_at, tenant_id
            ) VALUES (
                'compensation-project', 'Compensation', 'paused',
                '2026-09-19T00:00:00Z', 'tenant_legacy'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO assessment_charge_compensations (
                project_id, assessment_attempt_id, migration_version,
                predicate_version, reason_code, original_charged,
                charge_delta, project_counter_before, project_counter_after,
                source_reason_task_run_id, task_run_ids_json, proof_sha256,
                created_at
            ) VALUES (
                'compensation-project', 'attempt-1', 39,
                'pre-execution-missing-structured-cell-v1',
                'structured_explore_missing_slot_pre_execution',
                1, -1, 1, 0, 'reason-1', '["explore-1"]', ?,
                '2026-09-19T00:00:01Z'
            )
            """,
            ("a" * 64,),
        )

    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        with db.get_conn() as conn:
            conn.execute(
                "DELETE FROM assessment_charge_compensations "
                "WHERE project_id = 'compensation-project'"
            )

    with db.get_conn() as conn:
        conn.execute("DELETE FROM projects WHERE id = 'compensation-project'")
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM assessment_charge_compensations "
            "WHERE project_id = 'compensation-project'"
        ).fetchone()["count"] == 0


def _seed_v39_authorization_project(conn: db.PostgresConnection) -> None:
    snapshot = json.dumps(
        {
            "key": "default-copy-v2",
            "features": {
                "execution_contract_version": 1,
                "execution_engine": "pi",
                "assessment_protocol": "default-copy-v1",
                "assessment_contract_version": 2,
                "exploration_limit": "compact",
                "verification_reserve": 2,
            },
        }
    )
    conn.execute(
        """
        INSERT INTO projects (
            id, title, status, created_at, tenant_id, scenario_key,
            scenario_snapshot_json, assessment_protocol,
            assessment_progress_json, explore_budget, explore_deadline,
            explore_attempts_started, verification_attempts_started,
            execution_generation
        ) VALUES (
            'authorization-project', 'Authorization', 'paused',
            '2026-09-18T00:00:00Z', 'tenant_legacy', 'default-copy-v2',
            ?, 'default-copy-v1', ?, 16, '2026-09-18T02:00:00Z', 3, 1, 4
        )
        """,
        (snapshot, json.dumps({"version": 1, "phase": "baseline"})),
    )
    conn.execute(
        "INSERT INTO facts (id, project_id, description, creator, created_at) "
        "VALUES ('authorization-origin', 'authorization-project', 'origin', "
        "'system', '2026-09-18T00:00:00Z')"
    )
    conn.execute(
        """
        INSERT INTO targets (
            id, project_id, value, type, status, source_fact_id,
            discovered_by_run_id, created_at
        ) VALUES (
            'authorization-target', 'authorization-project',
            'https://example.test/app', 'url', 'seed',
            'authorization-origin', NULL, '2026-09-18T00:00:00Z'
        )
        """
    )


def test_postgres_v40_backfills_and_protects_authorization_grants(
    empty_postgres_url: str,
    tmp_path: Path,
) -> None:
    with psycopg.connect(empty_postgres_url, row_factory=dict_row) as raw:
        conn = db.PostgresConnection(raw)
        migrations.ensure_registry(conn)
        for migration in migrations.POSTGRES_MIGRATIONS:
            if migration.version > 39:
                break
            migrations.verify_or_apply(conn, migration, lambda _target: None)
        _seed_v39_authorization_project(conn)

    db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")
    with db.get_conn() as conn:
        project = conn.execute(
            "SELECT authorization_epoch, authorization_scope_digest, "
            "authorization_window_seconds FROM projects "
            "WHERE id = 'authorization-project'"
        ).fetchone()
        assert project["authorization_epoch"] == 1
        assert project["authorization_scope_digest"].startswith("authorization-scope:")
        assert project["authorization_window_seconds"] == 7200
        grant = conn.execute(
            "SELECT source, actor_type, actor_id, new_deadline, execution_generation "
            "FROM project_authorization_grants "
            "WHERE project_id = 'authorization-project' AND authorization_epoch = 1"
        ).fetchone()
        assert grant == {
            "source": "legacy_backfill",
            "actor_type": "system",
            "actor_id": "migration-v40",
            "new_deadline": "2026-09-18T02:00:00Z",
            "execution_generation": 4,
        }

    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE project_authorization_grants SET actor_id = 'changed' "
                "WHERE project_id = 'authorization-project'"
            )
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        with db.get_conn() as conn:
            conn.execute(
                "DELETE FROM project_authorization_grants "
                "WHERE project_id = 'authorization-project'"
            )

    with db.get_conn() as conn:
        conn.execute("DELETE FROM projects WHERE id = 'authorization-project'")
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM project_authorization_grants "
            "WHERE project_id = 'authorization-project'"
        ).fetchone()["count"] == 0


def test_postgres_v41_candidate_store_bytea_replay_conflict_and_cascade(
    empty_postgres_url: str,
    tmp_path: Path,
) -> None:
    session_id = "pi:11111111-1111-4111-8111-111111111111"
    result_schema_sha256 = canonical_value_sha256(
        build_submit_result_schema("explore_execute", binding=None)
    )
    delivery = {
        "session_id": session_id,
        "phase": "explore_execute",
        "deadline_at": "2026-09-19T12:00:00Z",
        "container_id": "postgres-candidate-container",
        "model_binding_sha256": "a" * 64,
        "result_schema_sha256": result_schema_sha256,
        "binding": None,
        "inputs": {},
    }
    with psycopg.connect(empty_postgres_url, row_factory=dict_row) as raw_conn:
        conn = db.PostgresConnection(raw_conn)
        migrations.ensure_registry(conn)
        for migration in migrations.POSTGRES_MIGRATIONS:
            if migration.version > 40:
                break
            migrations.verify_or_apply(conn, migration, lambda _target: None)
        conn.execute(
            """
            INSERT INTO projects (
                id, title, status, created_at, tenant_id, execution_generation
            ) VALUES (
                'candidate-pg', 'Candidate PostgreSQL', 'active',
                '2026-09-19T00:00:00Z', 'tenant_legacy', 0
            )
            """
        )
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, task_type, worker_name, worker_type, status,
                session_id, runtime_context_json, execution_generation
            ) VALUES (
                'candidate-pg-run', 'candidate-pg', 'explore', 'worker', 'pi',
                'running', ?, ?, 0
            )
            """,
            (
                session_id,
                json.dumps(
                    {
                        "dispatcher_id": "pending",
                        "execution_delivery": delivery,
                    }
                ),
            ),
        )

    db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")
    machine_id, _token = create_machine_principal(
        "candidate-postgres-dispatcher",
        "candidate-postgres-token",
    )
    with db.get_conn() as conn:
        conn.execute(
            """
            UPDATE task_runs SET runtime_context_json = ?
            WHERE project_id = 'candidate-pg' AND id = 'candidate-pg-run'
            """,
            (
                json.dumps(
                    {
                        "dispatcher_id": machine_id,
                        "execution_delivery": delivery,
                    }
                ),
            ),
        )

    raw_candidate = '{"candidate":"中文\\r\\n"}'.encode("utf-8")
    capture_id = "22222222-2222-4222-8222-222222222222"
    manifest_value = {
        "protocol": "cairn-candidate-frame-v1",
        "record_kind": "candidate",
        "capture_id": capture_id,
        "producer_candidate_key": f"{capture_id}:1:0",
        "parent_producer_candidate_key": None,
        "assistant_turn": 1,
        "content_index": 0,
        "project_id": "candidate-pg",
        "task_run_id": "candidate-pg-run",
        "phase": "explore_execute",
        "session": session_id,
        "binding_sha256": canonical_value_sha256(None),
        "result_schema_sha256": result_schema_sha256,
        "raw_sha256": sha256(raw_candidate).hexdigest(),
        "raw_size_bytes": len(raw_candidate),
        "artifacts": [],
    }
    manifest_bytes = canonical_json_bytes(manifest_value)
    manifest = CandidateFrameManifest.model_validate(manifest_value)
    principal = RequestPrincipal("machine", machine_id)

    def append(_: int):
        return capture_candidate(
            "candidate-pg",
            "candidate-pg-run",
            manifest,
            manifest_bytes,
            raw_candidate,
            principal=principal,
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        receipts = list(executor.map(append, range(4)))
    assert sorted(receipt.disposition for receipt in receipts) == [
        "received",
        "replayed",
        "replayed",
        "replayed",
    ]
    assert len({receipt.candidate_id for receipt in receipts}) == 1
    candidate_id = receipts[0].candidate_id

    with db.get_conn() as conn:
        stored = conn.execute(
            """
            SELECT raw_bytes, raw_size_bytes FROM task_run_candidates
            WHERE project_id = 'candidate-pg' AND id = ?
            """,
            (candidate_id,),
        ).fetchone()
        assert stored["raw_bytes"] == raw_candidate
        assert stored["raw_size_bytes"] == len(raw_candidate)
        data_type = conn.execute(
            """
            SELECT data_type FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'task_run_candidates'
              AND column_name = 'raw_bytes'
            """
        ).fetchone()
        assert data_type["data_type"] == "bytea"

    different_raw = b"different"
    conflict_value = {
        **manifest_value,
        "raw_sha256": sha256(different_raw).hexdigest(),
        "raw_size_bytes": len(different_raw),
    }
    with pytest.raises(HTTPException) as conflict:
        capture_candidate(
            "candidate-pg",
            "candidate-pg-run",
            CandidateFrameManifest.model_validate(conflict_value),
            canonical_json_bytes(conflict_value),
            different_raw,
            principal=principal,
        )
    assert (conflict.value.status_code, conflict.value.detail) == (
        409,
        "candidate_receipt_conflict",
    )

    outcome_value = {
        "protocol": "cairn-candidate-frame-v1",
        "record_kind": "outcome",
        "capture_id": capture_id,
        "producer_candidate_key": f"{capture_id}:1:0",
        "outcome_sequence": 1,
        "code": "submission_invalid",
        "path": "/data",
        "keyword": "required",
        "count": 1,
        "artifact_status": "not_evaluated",
        "binding_status": "not_evaluated",
    }
    outcome_receipt = capture_candidate_outcome(
        "candidate-pg",
        "candidate-pg-run",
        CandidateOutcomeManifest.model_validate(outcome_value),
        canonical_json_bytes(outcome_value),
        b"",
        principal=principal,
    )
    assert outcome_receipt.disposition == "received"

    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE task_run_candidates SET phase = 'report_execute' "
                "WHERE project_id = 'candidate-pg' AND id = ?",
                (candidate_id,),
            )
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        with db.get_conn() as conn:
            conn.execute(
                "DELETE FROM task_run_candidate_outcomes "
                "WHERE project_id = 'candidate-pg'"
            )

    with db.get_conn() as conn:
        conn.execute("DELETE FROM projects WHERE id = 'candidate-pg'")
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM task_run_candidates "
            "WHERE project_id = 'candidate-pg'"
        ).fetchone()["count"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM task_run_candidate_outcomes "
            "WHERE project_id = 'candidate-pg'"
        ).fetchone()["count"] == 0


def test_bootstrap_runtime_initializes_empty_postgres_and_preserves_identity_on_restart(
    empty_postgres_url: str, tmp_path: Path,
) -> None:
    password = "synthetic initial administrator password"
    token = "synthetic-dispatcher-token-" + secrets.token_hex(16)
    password_file = tmp_path / "initial-admin-password"
    with os.fdopen(os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        stream.write(password + "\n")
    args = [
        "auth", "bootstrap-runtime",
        "--admin-login", "Admin",
        "--admin-password-file", str(password_file),
        "--dispatcher-token-env", "TEST_DISPATCHER_TOKEN",
        "--database-url", empty_postgres_url,
    ]
    first = CliRunner().invoke(main, args, env={"TEST_DISPATCHER_TOKEN": token})
    assert first.exit_code == 0, first.output
    first_result = json.loads(first.output)
    assert first_result["admin_created"] is True
    assert first_result["dispatcher_created"] is True
    assert password not in first.output and token not in first.output
    with db.get_conn() as conn:
        scenarios_before = conn.execute("SELECT * FROM scenarios ORDER BY key").fetchall()
        migrations_before = conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()

    # 重置进程内缓存，确保第二次真的重新连接、校验迁移，而非 configure 的提前返回。
    db._reset_for_tests()
    password_file.write_text("unused replacement password\n", encoding="utf-8")
    second = CliRunner().invoke(main, args, env={"TEST_DISPATCHER_TOKEN": token})
    assert second.exit_code == 0, second.output
    assert json.loads(second.output) == {
        **first_result, "admin_created": False, "dispatcher_created": False,
    }
    assert password not in second.output and token not in second.output
    assert "unused replacement password" not in second.output
    with db.get_conn() as conn:
        users = conn.execute("SELECT password_hash FROM users").fetchall()
        principals = conn.execute("SELECT token_hash FROM machine_principals").fetchall()
        assert len(users) == 1
        assert verify_password(users[0]["password_hash"], password)
        assert not verify_password(users[0]["password_hash"], "unused replacement password")
        assert principals == [{"token_hash": hash_secret(token)}]
        assert conn.execute("SELECT * FROM scenarios ORDER BY key").fetchall() == scenarios_before
        assert conn.execute(
            "SELECT * FROM schema_migrations ORDER BY version"
        ).fetchall() == migrations_before


@pytest.mark.parametrize("initial_version", [1, 23])
def test_configure_upgrades_registered_postgres_and_preserves_custom_scenario(
    empty_postgres_url: str, tmp_path: Path, initial_version: int,
) -> None:
    # 用未改动的历史迁移构造已登记的旧库；自定义场景不依赖新版默认场景 seed。
    with psycopg.connect(empty_postgres_url, row_factory=dict_row) as raw:
        conn = db.PostgresConnection(raw)
        migrations.ensure_registry(conn)
        migrations.verify_or_apply(conn, migrations.POSTGRES_BASELINE, lambda _conn: None)
        conn.execute(
            "INSERT INTO scenarios (key, label, prompt_group) VALUES (?, ?, ?)",
            ("custom-scenario", "Custom scenario", "custom-prompts"),
        )
        for migration in migrations.POSTGRES_MIGRATIONS:
            if 1 < migration.version <= initial_version:
                migrations.verify_or_apply(conn, migration, lambda _conn: None)
        registered = conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
        assert registered[-1]["version"] == initial_version
        assert conn.execute(
            """
            SELECT COUNT(*) AS count FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'scenarios'
              AND column_name IN ('origin_template', 'goal_template')
            """
        ).fetchone()["count"] == 0
        custom_before = conn.execute(
            "SELECT * FROM scenarios WHERE key = ?", ("custom-scenario",),
        ).fetchone()

    db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")

    with db.get_conn() as conn:
        applied = conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
        assert applied[:len(registered)] == registered
        assert [row["version"] for row in applied] == [
            migration.version for migration in migrations.POSTGRES_MIGRATIONS
        ]
        assert [row["checksum"] for row in applied] == [
            migration.checksum for migration in migrations.POSTGRES_MIGRATIONS
        ]
        custom_after = conn.execute(
            "SELECT * FROM scenarios WHERE key = ?", ("custom-scenario",),
        ).fetchone()
        # v21 按既有规则清空旧 knowledge 引用；其余自定义字段不得被默认场景覆盖。
        assert {key: custom_after[key] for key in custom_before} == {**custom_before, "knowledge": None}
        assert custom_after["origin_template"] is None
        assert custom_after["goal_template"] is None
        conn.execute(
            "UPDATE scenarios SET origin_template = ?, goal_template = ? WHERE key = ?",
            ("custom origin", "custom goal", "custom-scenario"),
        )
        scenarios_before_restart = conn.execute("SELECT * FROM scenarios ORDER BY key").fetchall()

    db._reset_for_tests()
    db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")

    with db.get_conn() as conn:
        assert conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall() == applied
        assert conn.execute(
            "SELECT * FROM scenarios ORDER BY key"
        ).fetchall() == scenarios_before_restart
