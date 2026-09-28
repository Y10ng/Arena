from __future__ import annotations

from contextlib import contextmanager
import sqlite3

import pytest
from fastapi import HTTPException

from cairn.server import db
from cairn.server.services import audit_artifacts


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(db.SCHEMA)
    db._ensure_schema_migrations(conn)
    return conn


def seed_project_fact(
    conn: sqlite3.Connection,
    project_id: str,
    fact_id: str,
) -> None:
    conn.execute(
        "INSERT INTO projects (id, title, status, created_at) VALUES (?, ?, 'active', ?)",
        (project_id, project_id, "2026-08-16T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO facts (id, project_id, description, creator, created_at) "
        "VALUES (?, ?, 'fact', 'system', ?)",
        (fact_id, project_id, "2026-08-16T00:00:00Z"),
    )


def test_list_fact_evidence_returns_empty_for_existing_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(audit_artifacts, "get_conn", fake_get_conn)
    try:
        seed_project_fact(conn, "p1", "f1")

        assert audit_artifacts.list_fact_evidence_records("p1", "f1") == []
    finally:
        conn.close()


def test_list_fact_evidence_returns_bound_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(audit_artifacts, "get_conn", fake_get_conn)
    try:
        seed_project_fact(conn, "p1", "f1")
        conn.execute(
            "INSERT INTO artifacts "
            "(id, project_id, kind, category, uri, sha256, size, created_at) "
            "VALUES ('a1', 'p1', 'raw_output', 'evidence', 'artifact://a1', "
            "'digest', 4, '2026-08-16T00:01:00Z')"
        )
        conn.execute(
            "INSERT INTO fact_evidence "
            "(project_id, fact_id, artifact_id, pointer, created_at) "
            "VALUES ('p1', 'f1', 'a1', 'stdout', '2026-08-16T00:02:00Z')"
        )

        evidence = audit_artifacts.list_fact_evidence_records("p1", "f1")

        assert len(evidence) == 1
        assert evidence[0].artifact_id == "a1"
        assert evidence[0].pointer == "stdout"
        assert evidence[0].artifact_exists is True
        assert evidence[0].evidence_valid is True
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("project_id", "fact_id"),
    [("p1", "missing"), ("p2", "f1")],
)
def test_list_fact_evidence_rejects_missing_or_cross_project_fact(
    monkeypatch: pytest.MonkeyPatch,
    project_id: str,
    fact_id: str,
) -> None:
    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(audit_artifacts, "get_conn", fake_get_conn)
    try:
        seed_project_fact(conn, "p1", "f1")
        if project_id == "p2":
            conn.execute(
                "INSERT INTO projects (id, title, status, created_at) "
                "VALUES ('p2', 'p2', 'active', '2026-08-16T00:00:00Z')"
            )

        with pytest.raises(HTTPException) as exc_info:
            audit_artifacts.list_fact_evidence_records(project_id, fact_id)

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == f"Fact {fact_id} not found"
    finally:
        conn.close()
