"""hint 定向测试：fact/intent 锚点、goal 拒绝、悬空引用校验。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from cairn.server import db
from cairn.server.models import CreateHintRequest
from cairn.server.routers import hints as hints_router


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(db.SCHEMA)
    db._ensure_schema_migrations(conn)
    return conn


@contextmanager
def _yield(conn: sqlite3.Connection):
    yield conn


def _seed_project(conn: sqlite3.Connection) -> str:
    """建项目并插入一条从 origin 出发的 intent，返回 project_id 与 intent id。"""
    conn.execute(
        "INSERT INTO projects (id, title, status, created_at, scenario_key, preferred_workers_json) "
        "VALUES ('proj_001', 't', 'active', '2026-01-01', 'default', '[]')"
    )
    conn.execute(
        "INSERT INTO facts (id, project_id, description, creator, created_at) VALUES "
        "('origin', 'proj_001', '起点', 'system', '2026-01-01'),"
        "('goal', 'proj_001', '终点', 'system', '2026-01-01'),"
        "('f001', 'proj_001', '发现端口', 'system', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO intents (id, project_id, description, creator, created_at) "
        "VALUES ('i001', 'proj_001', 'try sqli', 'alice', '2026-01-01')"
    )
    return "proj_001"


def test_goal_fact_rejected_at_schema() -> None:
    # goal 不可作为 hint 锚点，在 schema 层 422 拦截。
    with pytest.raises(ValidationError):
        CreateHintRequest(content="x", creator="alice", fact_id="goal")


def test_hint_with_valid_fact_id(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = make_conn()
    _seed_project(conn)
    monkeypatch.setattr(hints_router, "get_conn", lambda: _yield(conn))
    try:
        h = hints_router.create_hint(
            "proj_001", CreateHintRequest(content="c", creator="alice", fact_id="f001")
        )
        assert h.fact_id == "f001"
        assert h.intent_id is None
    finally:
        conn.close()


def test_hint_with_unknown_fact_id_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = make_conn()
    _seed_project(conn)
    monkeypatch.setattr(hints_router, "get_conn", lambda: _yield(conn))
    try:
        with pytest.raises(HTTPException) as exc:
            hints_router.create_hint(
                "proj_001", CreateHintRequest(content="c", creator="alice", fact_id="f999")
            )
        assert exc.value.status_code == 422
    finally:
        conn.close()


def test_hint_with_valid_intent_id(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = make_conn()
    _seed_project(conn)
    monkeypatch.setattr(hints_router, "get_conn", lambda: _yield(conn))
    try:
        h = hints_router.create_hint(
            "proj_001", CreateHintRequest(content="c", creator="alice", intent_id="i001")
        )
        assert h.intent_id == "i001"
        assert h.fact_id is None
    finally:
        conn.close()


def test_hint_with_unknown_intent_id_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = make_conn()
    _seed_project(conn)
    monkeypatch.setattr(hints_router, "get_conn", lambda: _yield(conn))
    try:
        with pytest.raises(HTTPException) as exc:
            hints_router.create_hint(
                "proj_001", CreateHintRequest(content="c", creator="alice", intent_id="i999")
            )
        assert exc.value.status_code == 422
    finally:
        conn.close()


def test_project_level_hint_still_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = make_conn()
    _seed_project(conn)
    monkeypatch.setattr(hints_router, "get_conn", lambda: _yield(conn))
    try:
        h = hints_router.create_hint(
            "proj_001", CreateHintRequest(content="c", creator="alice")
        )
        assert h.fact_id is None and h.intent_id is None
    finally:
        conn.close()
