from __future__ import annotations

from contextlib import contextmanager
import sqlite3

import pytest
from fastapi import HTTPException

from cairn.server import db
from cairn.server.models import Scenario
from cairn.server.services import (
    available_knowledge_set_keys,
    create_knowledge_set,
    delete_knowledge_file,
    delete_knowledge_set,
    knowledge_root,
    list_knowledge_files,
    list_knowledge_sets,
    list_scenarios,
    read_knowledge_file,
    replace_scenarios,
    write_knowledge_file,
)


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    db.run_migrations(conn, "sqlite", initialize_runtime=True)
    return conn


def test_default_seed_does_not_create_retired_knowledge_sets() -> None:
    conn = make_conn()
    try:
        assert {scenario.knowledge for scenario in list_scenarios(conn)} == {None}
        assert available_knowledge_set_keys() == {"knowledge_base"}
        assert not (knowledge_root() / "logic-vuln").exists()
        assert not (knowledge_root() / "pentest").exists()
    finally:
        conn.close()


def test_knowledge_initialization_preserves_existing_user_content() -> None:
    path = knowledge_root() / "pentest" / "README.md"
    path.parent.mkdir(parents=True)
    path.write_text("用户已有资料\n", encoding="utf-8")
    conn = make_conn()
    try:
        assert path.read_text(encoding="utf-8") == "用户已有资料\n"
    finally:
        conn.close()


def test_list_knowledge_sets_includes_builtin_on_empty_root() -> None:
    conn = make_conn()
    try:
        for child in knowledge_root().iterdir():
            if child.is_dir():
                for item in child.rglob("*"):
                    if item.is_file():
                        item.unlink()
                child.rmdir()

        sets = list_knowledge_sets(conn)

        assert [item.key for item in sets] == ["knowledge_base"]
        assert sets[0].builtin is True
    finally:
        conn.close()


def test_builtin_knowledge_base_does_not_list_child_sets() -> None:
    conn = make_conn()
    try:
        write_knowledge_file("knowledge_base", "README.md", "# Root\n")
        create_knowledge_set(conn, "playbooks")
        write_knowledge_file("playbooks", "README.md", "# Playbooks\n")

        root_files = list_knowledge_files("knowledge_base")
        playbook_files = list_knowledge_files("playbooks")
        sets = {item.key: item for item in list_knowledge_sets(conn)}

        assert [file.path for file in root_files] == ["README.md"]
        assert [file.path for file in playbook_files] == ["README.md"]
        assert sets["knowledge_base"].file_count == 1
        assert sets["playbooks"].file_count == 1
    finally:
        conn.close()


def test_builtin_knowledge_base_rejects_child_set_file_paths() -> None:
    conn = make_conn()
    try:
        with pytest.raises(HTTPException) as exc:
            read_knowledge_file("knowledge_base", "web_standards/README.md")

        assert exc.value.status_code == 422
    finally:
        conn.close()


def test_knowledge_set_and_file_crud() -> None:
    conn = make_conn()
    try:
        created = create_knowledge_set(conn, "playbooks")
        assert created.key == "playbooks"

        saved = write_knowledge_file(
            "playbooks",
            "web/login.md",
            "---\ntitle: Login checks\n---\n\nCheck redirects.\n",
        )
        assert saved.path == "web/login.md"
        assert saved.content.endswith("Check redirects.\n")

        files = list_knowledge_files("playbooks")
        assert [file.path for file in files] == ["web/login.md"]

        read = read_knowledge_file("playbooks", "web/login.md")
        assert read.content == saved.content

        delete_knowledge_file("playbooks", "web/login.md")
        delete_knowledge_file("playbooks", "web/login.md")
        assert list_knowledge_files("playbooks") == []

        delete_knowledge_set(conn, "playbooks")
        delete_knowledge_set(conn, "playbooks")
        assert "playbooks" not in available_knowledge_set_keys()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "name",
    [
        "../escape.md",
        "/absolute.md",
        "nested/../escape.md",
        "nested//file.md",
        "script.py",
    ],
)
def test_knowledge_file_paths_reject_traversal_and_unsupported_extensions(name: str) -> None:
    conn = make_conn()
    try:
        create_knowledge_set(conn, "safe")

        with pytest.raises(HTTPException) as exc:
            write_knowledge_file("safe", name, "nope")

        assert exc.value.status_code == 422
        assert not (knowledge_root().parent / "escape.md").exists()
    finally:
        conn.close()


def test_delete_knowledge_set_is_not_blocked_by_legacy_scenario_payload() -> None:
    conn = make_conn()
    try:
        create_knowledge_set(conn, "playbooks")
        scenarios = replace_scenarios(
            conn,
            [
                Scenario(
                    key="custom",
                    label="Custom",
                    prompt_group="goal-driven-v1",
                    knowledge="playbooks",
                )
            ],
        )

        assert scenarios[0].knowledge is None
        delete_knowledge_set(conn, "playbooks")
        assert "playbooks" not in available_knowledge_set_keys()
    finally:
        conn.close()


def test_replace_scenarios_ignores_unknown_legacy_knowledge_set() -> None:
    conn = make_conn()
    try:
        scenarios = replace_scenarios(
            conn,
            [
                Scenario(
                    key="bad-knowledge",
                    label="Bad knowledge",
                    prompt_group="goal-driven-v1",
                    knowledge="missing_set",
                )
            ],
        )

        assert scenarios[0].knowledge is None
    finally:
        conn.close()


def test_scenario_options_do_not_expose_knowledge_sets(monkeypatch: pytest.MonkeyPatch) -> None:
    from cairn.server.routers import scenarios as scenario_router

    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    try:
        create_knowledge_set(conn, "playbooks")
        monkeypatch.setattr(scenario_router, "get_conn", fake_get_conn)

        options = scenario_router.get_scenario_options()

        assert options.knowledge == []
    finally:
        conn.close()


def test_app_exposes_knowledge_routes() -> None:
    from cairn.server.app import app

    paths = app.openapi()["paths"]
    assert "/knowledge" in paths
    assert "/knowledge/{key}" in paths
    assert "/knowledge/{key}/files" in paths
    assert "/knowledge/{key}/files/{name}" in paths
