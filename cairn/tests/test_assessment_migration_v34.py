"""v34 双后端回归；PG 显式 opt-in，只启动自建 Unix socket 临时集群。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import pwd
import sqlite3
import subprocess
import tempfile
from threading import Barrier
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest
from fastapi import HTTPException

from cairn.server import db
from cairn.server.auth import RequestPrincipal
from test_web_assessment_phases import seed_project, seed_intent, start


TEST_DISPATCHER = RequestPrincipal(kind="machine", subject_id="synthetic-dispatcher")


@pytest.fixture(scope="module")
def private_postgres():
    if os.environ.get("CAIRN_TEST_LOCAL_PG") != "1":
        pytest.skip("explicit CAIRN_TEST_LOCAL_PG=1 required for disposable cluster")
    candidates = sorted(Path("/usr/lib/postgresql").glob("*/bin/initdb"))
    if not candidates:
        pytest.skip("local PostgreSQL binaries unavailable")
    binaries = candidates[-1].parent
    with tempfile.TemporaryDirectory(prefix="cairn-v34-private-pg-") as directory:
        root = Path(directory)
        prefix = []
        if os.geteuid() == 0:
            account = pwd.getpwnam("nobody")
            os.chown(root, account.pw_uid, account.pw_gid)
            prefix = ["runuser", "-u", "nobody", "--"]
        data = root / "data"
        def run(*args):
            return subprocess.run([*prefix, *map(str, args)], cwd="/tmp", check=True, capture_output=True, text=True, timeout=60)
        run(binaries / "initdb", "-D", data, "-U", "synthetic", "-A", "trust", "--no-locale", "--encoding=UTF8")
        started = False
        try:
            # 无 TCP listener，不接受外部 DSN，不触碰任何既有服务/容器。
            run(binaries / "pg_ctl", "-D", data, "-l", root / "log", "-o", f"-F -c listen_addresses='' -c unix_socket_directories='{root}'", "-w", "start")
            started = True
            yield {"host": str(root), "user": "synthetic", "dbname": "postgres", "connect_timeout": 3}
        finally:
            if started:
                run(binaries / "pg_ctl", "-D", data, "-m", "immediate", "-w", "stop")


@pytest.fixture(params=["sqlite", "postgres"])
def database(request, tmp_path):
    backend = request.param
    if backend == "sqlite":
        path = tmp_path / "synthetic.sqlite"
        def connect():
            conn = sqlite3.connect(path, timeout=20)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            return conn
        yield backend, connect
        return
    params = request.getfixturevalue("private_postgres")
    name = "synthetic_" + uuid4().hex
    with psycopg.connect(**params, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
        connections = []
        def connect():
            conn = db.PostgresConnection(psycopg.connect(**{**params, "dbname": name}, row_factory=dict_row))
            connections.append(conn)
            return conn
        try:
            yield backend, connect
        finally:
            for conn in connections:
                conn.close()
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def test_forward_migration_preserves_history_bindings_and_immutability(database, monkeypatch):
    backend, connect = database
    conn = connect()
    try:
        registry = "SQLITE_MIGRATIONS" if backend == "sqlite" else "POSTGRES_MIGRATIONS"
        with monkeypatch.context() as patch:
            patch.setattr(db, registry, tuple(item for item in getattr(db, registry) if item.version < 34))
            db.run_migrations(conn, backend)
        conn.execute("INSERT INTO users(id,login,password_hash,role,created_at,updated_at) VALUES ('u','synthetic','not-a-real-secret','user','2000','2000')")
        conn.execute("INSERT INTO projects(id,title,status,created_at,tenant_id) VALUES ('p','synthetic','active','2000','tenant_legacy')")
        conn.execute("INSERT INTO worker_templates(id,name,owner,created_at,tenant_id) VALUES ('wt','synthetic','u','2000','tenant_legacy')")
        conn.execute("INSERT INTO tenant_models(id,tenant_id,display_name,protocol,base_url,model_name,created_by,created_at,updated_at) VALUES ('m','tenant_legacy','synthetic','openai-responses','https://invalid.test','synthetic','u','2000','2000')")
        if backend == "sqlite":
            conn.execute("PRAGMA ignore_check_constraints=ON")
        else:
            conn.execute("ALTER TABLE tenant_workers DROP CONSTRAINT tenant_workers_engine_check")
            conn.execute("ALTER TABLE tenant_workers ADD CONSTRAINT tenant_workers_engine_check CHECK (engine <> '')")
        for position, engine in enumerate(("claudecode", "codex", "historical-unknown")):
            wid = "w" + str(position)
            conn.execute("INSERT INTO tenant_workers(id,tenant_id,display_name,model_id,engine,revision,created_by,created_at,updated_at) VALUES (?,'tenant_legacy',?,'m',?,7,'u','2000','2000')", (wid, wid, engine))
            conn.execute("INSERT INTO project_tenant_workers(project_id,tenant_id,tenant_worker_id,tenant_worker_revision,position,created_at) VALUES ('p','tenant_legacy',?,7,?,'2000')", (wid, position))
            conn.execute("INSERT INTO worker_template_tenant_workers(template_id,tenant_id,tenant_worker_id,position,created_at) VALUES ('wt','tenant_legacy',?,?,'2000')", (wid, position))
        if backend == "sqlite":
            conn.execute("PRAGMA ignore_check_constraints=OFF")
        conn.execute("INSERT INTO facts(id,project_id,description,creator,created_at) VALUES ('f','p','synthetic','u','2000')")
        conn.execute("INSERT INTO intents(id,project_id,description,creator,created_at) VALUES ('i','p','synthetic','u','2000')")
        tables = ("tenant_workers", "project_tenant_workers", "worker_template_tenant_workers", "schema_migrations")
        before = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()] for table in tables}
        conn.commit()
        db.run_migrations(conn, backend)
        conn.commit()
        for table in tables[:-1]:
            assert before[table] == [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]
        after = [dict(row) for row in conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()]
        assert before["schema_migrations"] == [row for row in after if row["version"] < 34]
        assert [row["version"] for row in after if row["version"] >= 34] == [
            34,
            35,
            36,
            37,
            38,
            39,
            40,
            41,
        ]
        assert conn.execute("SELECT assessment_progress_json FROM projects WHERE id='p'").fetchone()["assessment_progress_json"] is None
        assert conn.execute("SELECT assessment_record_json FROM facts WHERE id='f' AND project_id='p'").fetchone()["assessment_record_json"] is None
        conn.execute("INSERT INTO tenant_workers(id,tenant_id,display_name,model_id,created_by,created_at,updated_at) VALUES ('new','tenant_legacy','new','m','u','2000','2000')")
        assert conn.execute("SELECT engine FROM tenant_workers WHERE id='new'").fetchone()["engine"] == "pi"
        conn.execute("UPDATE facts SET assessment_record_json='{}' WHERE project_id='p' AND id='f'")
        conn.commit()
        for query in ("UPDATE facts SET assessment_record_json=NULL WHERE project_id='p' AND id='f'", "UPDATE intents SET depth_review_fact_id='f' WHERE project_id='p' AND id='i'"):
            with pytest.raises((sqlite3.IntegrityError, psycopg.Error)):
                conn.execute(query)
            conn.rollback()
        db.run_migrations(conn, backend)
        conn.commit()
        assert after == [dict(row) for row in conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()]
        if backend == "sqlite":
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


@pytest.mark.parametrize("operation", ["review", "conclude", "complete", "create_intent"])
@pytest.mark.parametrize("conflicting", [False, True])
def test_concurrent_result_receipts_accept_once(database, monkeypatch, operation, conflicting):
    from cairn.server.models import CompleteRequest, ConcludeRequest, CreateIntentRequest, CreateTaskRunRequest
    from cairn.server.routers import intents as intents_router
    from cairn.server.services import project_completion, project_reason
    from cairn.server.services.audit_task_runs import insert_task_run_record
    from cairn.server.services.structured_intent_bindings import (
        issue_structured_intent_binding,
    )
    from test_web_assessment_phases import review_body, seed_cell

    backend, connect = database
    conn = connect()
    try:
        db.run_migrations(conn, backend)
        conn.commit()
        seed_project(conn, phase="depth_review")
        seed_cell(conn)
        body = review_body(conn, selected=False)
        conn.commit()

        @contextmanager
        def isolated_connection():
            transaction = connect()
            try:
                yield transaction
                transaction.commit()
            except Exception:
                transaction.rollback()
                raise
            finally:
                transaction.close()

        for module in (intents_router, project_reason, project_completion):
            monkeypatch.setattr(module, "get_conn", isolated_connection)
        if operation == "review":
            submit = lambda candidate: project_reason.accept_depth_review("p", candidate).fact.id
            conflict_code = "depth_review_already_accepted"
        elif operation == "create_intent":
            conn.execute(
                "UPDATE projects SET assessment_progress_json=? WHERE id='p'",
                (json.dumps({"version": 1, "phase": "baseline"}),),
            )
            conn.execute(
                "UPDATE coverage_slots SET status='pending', result=NULL, "
                "resulting_fact_id=NULL WHERE project_id='p' AND slot_key='cell'"
            )
            project = conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()
            binding = issue_structured_intent_binding(conn, project)
            conn.execute(
                "UPDATE task_runs SET runtime_context_json=? "
                "WHERE project_id='p' AND id='review-run'",
                (json.dumps({"structured_intent_binding": binding}),),
            )
            conn.commit()
            body = CreateIntentRequest(
                **{"from": ["baseline"]},
                creator="reasoner",
                task_run_id="review-run",
                submission_index=0,
                description="Synthetic new hypothesis",
                slot_key="cell",
                strategy_key="synthetic.baseline-v1",
            )
            submit = lambda candidate: intents_router.create_intent("p", candidate).id
            conflict_code = "intent_submission_conflict"
        elif operation == "complete":
            project_reason.accept_depth_review("p", body)
            body = CompleteRequest(**{"from": ["baseline"]}, worker="reasoner", task_run_id="review-run", description="Synthetic completion")
            submit = lambda candidate: project_completion.complete_project_record("p", candidate).id
            conflict_code = "assessment_completion_conflict"
        else:
            snapshot = {"features": {"execution_contract_version": 1, "execution_engine": "pi"}}
            conn.execute("UPDATE projects SET scenario_key='custom',assessment_protocol='',assessment_progress_json=NULL,scenario_snapshot_json=? WHERE id='p'", (json.dumps(snapshot),))
            seed_intent(conn, "bootstrap")
            conn.execute("UPDATE intents SET worker='worker' WHERE id='bootstrap' AND project_id='p'")
            run = insert_task_run_record(conn, "p", CreateTaskRunRequest(
                task_type="bootstrap", intent_id="bootstrap", worker_name="worker", worker_type="pi", status="running",
            ), principal=TEST_DISPATCHER)
            conn.commit()
            body = ConcludeRequest(worker="worker", task_run_id=run.id, description="Synthetic observation")
            submit = lambda candidate: intents_router.conclude("p", "bootstrap", candidate).fact.id
            conflict_code = "assessment_result_conflict"
        barrier = Barrier(4)

        def one(index):
            candidate = body.model_copy(update={"description": f"Synthetic contender {index}"}) if conflicting else body
            barrier.wait(timeout=10)
            try:
                return submit(candidate)
            except HTTPException as exc:
                assert conflicting and exc.status_code == 409 and exc.detail == conflict_code
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            accepted = [item for item in pool.map(one, range(4)) if item is not None]
        assert len(accepted) == (1 if conflicting else 4)
        assert len(set(accepted)) == 1
        # 同内容并发只产生一次业务变更，不靠主键冲突异常假装幂等。
        if operation == "complete":
            assert conn.execute("SELECT COUNT(*) AS n FROM intents WHERE project_id='p' AND to_fact_id='goal'").fetchone()["n"] == 1
        elif operation == "create_intent":
            assert conn.execute("SELECT COUNT(*) AS n FROM intents WHERE project_id='p'").fetchone()["n"] == 1
        else:
            assert conn.execute("SELECT COUNT(*) AS n FROM facts WHERE project_id='p'").fetchone()["n"] == 4
    finally:
        conn.close()


def test_concurrent_admission_enforces_total_and_verifier_reserve(database):
    backend, connect = database
    conn = connect()
    db.run_migrations(conn, backend)
    conn.commit()
    seed_project(conn, budget=5)
    for index in range(6):
        seed_intent(conn, f"d{index}")
    for index in range(3):
        seed_intent(conn, f"v{index}", role="verifier")
    conn.commit()
    def admit(ids):
        barrier = Barrier(len(ids))
        def one(iid):
            transaction = connect()
            try:
                barrier.wait(timeout=10)
                try:
                    start(transaction, iid, worker=iid)
                    transaction.commit()
                    return True
                except HTTPException as exc:
                    transaction.rollback()
                    assert exc.status_code == 409
                    return False
            finally:
                transaction.close()
        with ThreadPoolExecutor(max_workers=len(ids)) as pool:
            return sum(pool.map(one, ids))
    try:
        assert admit([f"d{i}" for i in range(6)]) == 4
        assert admit([f"v{i}" for i in range(3)]) == 1
        row = conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()
        assert row["explore_attempts_started"] == 5
        assert row["verification_attempts_started"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM assessment_attempts WHERE project_id='p'").fetchone()["n"] == 5
    finally:
        conn.close()
