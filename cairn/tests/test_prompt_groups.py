from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3

import pytest
from fastapi import HTTPException

from cairn.dispatcher.config import validate_prompt_resources
from cairn.dispatcher.prompting import load_prompt
from cairn.dispatcher.prompt_resources import (
    PROMPT_ALLOWED_FILES,
    PROMPT_REQUIRED_FILES,
    RETIRED_PROMPT_GROUP_KEYS,
    available_builtin_prompt_groups,
    read_prompt_resource,
    validate_exploration_prompt_resources,
    validate_prompt_group_contents,
    validate_report_prompt_resources,
)
from cairn.server import db
from cairn.server.models import Scenario
from cairn.server.services import (
    available_prompt_groups,
    create_prompt_group,
    delete_prompt_group,
    list_prompt_groups,
    prompts_root,
    read_prompt_file,
    repair_prompt_group,
    replace_scenarios,
    restore_prompt_group,
    validate_prompt_group,
    write_prompt_file,
)


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    db.run_migrations(conn, "sqlite", initialize_runtime=False)
    return conn


def test_list_prompt_groups_includes_builtins() -> None:
    conn = make_conn()
    try:
        groups = {group.key: group for group in list_prompt_groups(conn)}
        assert set(groups) == {"goal-driven-v1"}
        group = groups["goal-driven-v1"]
        assert group.builtin is True
        assert group.managed is False
        assert group.capabilities == ["durable-report", "exploration"]
        assert group.visibility == "visible"
        assert group.valid is True
        assert group.file_count == 9
    finally:
        conn.close()


@pytest.mark.parametrize("group", sorted(available_builtin_prompt_groups()))
def test_builtin_prompts_use_sdk_tools_without_wire_examples(group: str) -> None:
    validate_prompt_resources(group, builtin_only=True)
    for name in sorted(PROMPT_ALLOWED_FILES - {"_output_lang.md"}):
        try:
            content = read_prompt_resource(group, name, builtin_only=True)
        except FileNotFoundError:
            continue
        assert "submit_result" in content
        assert "register_artifact" in content
        assert '"protocol"' not in content
        assert '"kind":"' not in content
        assert "Return one raw JSON" not in content
        assert "Return only one raw JSON" not in content
        assert "Copy `slot_key`" not in content
        assert "declared in `data.artifacts`" not in content


def test_retired_managed_prompt_groups_stay_hidden(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("CAIRN_PROMPTS_ROOT", str(tmp_path))
    for key in ("default-copy", "pentest", "logic-vuln", "mock"):
        (tmp_path / key).mkdir()

    conn = make_conn()
    try:
        assert {"default-copy", "pentest", "logic-vuln"}.isdisjoint(
            available_prompt_groups()
        )
        assert {"default-copy", "pentest", "logic-vuln"}.isdisjoint(
            group.key for group in list_prompt_groups(conn)
        )
    finally:
        conn.close()


@pytest.mark.parametrize("key", ["default-copy", "pentest", "logic-vuln", "mock"])
def test_create_prompt_group_rejects_retired_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    key: str,
) -> None:
    monkeypatch.setenv("CAIRN_PROMPTS_ROOT", str(tmp_path))
    conn = make_conn()
    try:
        with pytest.raises(HTTPException) as exc:
            create_prompt_group(conn, key)

        assert exc.value.status_code == 422
        assert not (tmp_path / key).exists()
    finally:
        conn.close()


@pytest.mark.parametrize("group", ["goal-driven-v1", "custom-goal"])
@pytest.mark.parametrize("token", ["{goal}", "{decision_context}", "{decision_input_digest}"])
def test_custom_exploration_keeps_goal_driven_contract(group: str, token: str) -> None:
    contents = {
        name: read_prompt_resource("goal-driven-v1", name, builtin_only=True)
        for name in PROMPT_REQUIRED_FILES
    }
    contents["reason.md"] = contents["reason.md"].replace(token, "")
    with pytest.raises(ValueError, match="missing placeholders"):
        validate_prompt_group_contents(contents, prompt_group=group)


def test_goal_driven_exploration_requires_verifier_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("CAIRN_PROMPTS_ROOT", str(tmp_path))
    conn = make_conn()
    try:
        create_prompt_group(conn, "goal-driven-v1")
        (tmp_path / "goal-driven-v1" / "verifier.md").unlink()

        with pytest.raises(ValueError, match="missing resource: verifier.md"):
            validate_exploration_prompt_resources("goal-driven-v1")

        group = next(
            item for item in list_prompt_groups(conn) if item.key == "goal-driven-v1"
        )
        assert "exploration" not in group.capabilities
        assert group.capabilities == ["durable-report"]
    finally:
        conn.close()


def test_create_prompt_group_clones_default_and_allows_scenario_reference() -> None:
    conn = make_conn()
    try:
        created = create_prompt_group(conn, "playbooks")

        assert created.key == "playbooks"
        assert created.managed is True
        assert created.file_count >= len(PROMPT_REQUIRED_FILES)
        assert "playbooks" in available_prompt_groups()
        validate_prompt_resources("playbooks")

        scenarios = replace_scenarios(
            conn,
            [
                Scenario(
                    key="custom",
                    label="Custom",
                    prompt_group="playbooks",
                    knowledge="knowledge_base",
                )
            ],
        )
        assert scenarios[0].prompt_group == "playbooks"
    finally:
        conn.close()


def test_custom_report_group_infers_report_capability_from_contract() -> None:
    conn = make_conn()
    try:
        path = prompts_root() / "custom-report"
        path.mkdir()
        (path / "report.md").write_text(
            read_prompt_resource("goal-driven-v1", "report.md"), encoding="utf-8"
        )
        group = next(item for item in list_prompt_groups(conn) if item.key == "custom-report")
        assert group.capabilities == ["durable-report"]
        validate_report_prompt_resources("custom-report")
        with pytest.raises(ValueError, match="missing resource: reason.md"):
            validate_exploration_prompt_resources("custom-report")
    finally:
        conn.close()


@pytest.mark.parametrize("group", sorted(RETIRED_PROMPT_GROUP_KEYS))
def test_retired_builtin_resources_are_removed(group: str) -> None:
    with pytest.raises(FileNotFoundError):
        read_prompt_resource(group, "reason.md", builtin_only=True)
    with pytest.raises(ValueError, match="retired"):
        load_prompt(group, "reason.md")


def test_managed_prompt_group_overrides_builtin_and_restore_falls_back() -> None:
    conn = make_conn()
    try:
        create_prompt_group(conn, "goal-driven-v1")
        original = read_prompt_file("goal-driven-v1", "reason.md").content
        custom = original + "\nMANAGED OVERRIDE\n"
        saved = write_prompt_file("goal-driven-v1", "reason.md", custom)
        groups = {group.key: group for group in list_prompt_groups(conn)}
        assert "MANAGED OVERRIDE" in saved.content
        assert groups["goal-driven-v1"].builtin is True
        assert groups["goal-driven-v1"].managed is True
        assert groups["goal-driven-v1"].overrides_builtin is True
        assert "MANAGED OVERRIDE" in load_prompt("goal-driven-v1", "reason.md")
        with pytest.raises(HTTPException, match="must be restored"):
            delete_prompt_group(conn, "goal-driven-v1")
        restore_prompt_group(conn, "goal-driven-v1")
        assert "MANAGED OVERRIDE" not in load_prompt("goal-driven-v1", "reason.md")
    finally:
        conn.close()


def test_legacy_managed_prompt_is_readable_but_not_silently_rewritten() -> None:
    root = prompts_root() / "legacy-managed"
    root.mkdir(parents=True)
    for name in PROMPT_REQUIRED_FILES:
        content = read_prompt_resource("goal-driven-v1", name, builtin_only=True)
        content = content.replace("submit_result", "legacy_submit")
        (root / name).write_text(content, encoding="utf-8")
    original = (root / "reason.md").read_text(encoding="utf-8")
    assert read_prompt_resource("legacy-managed", "reason.md") == original
    with pytest.raises(ValueError, match="missing SDK tools: submit_result"):
        load_prompt("legacy-managed", "reason.md")
    assert (root / "reason.md").read_text(encoding="utf-8") == original


@pytest.mark.parametrize("name", ("verifier.md", "verifier_conclude.md", "report.md"))
def test_optional_managed_stage_cannot_bypass_sdk_tool_contract(name: str) -> None:
    root = prompts_root() / "custom-stage"
    root.mkdir(parents=True)
    for required in PROMPT_REQUIRED_FILES:
        (root / required).write_text(
            read_prompt_resource("goal-driven-v1", required, builtin_only=True), encoding="utf-8",
        )
    (root / name).write_text("Return only one raw JSON object.", encoding="utf-8")
    with pytest.raises(ValueError, match="missing SDK tools"):
        load_prompt("custom-stage", name)


def test_write_prompt_file_rejects_missing_required_placeholder() -> None:
    conn = make_conn()
    try:
        create_prompt_group(conn, "playbooks")
        current = read_prompt_file("playbooks", "reason.md").content

        with pytest.raises(HTTPException) as exc:
            write_prompt_file("playbooks", "reason.md", current.replace("{graph_yaml}", "graph"))

        assert exc.value.status_code == 422
        assert "{graph_yaml}" in str(exc.value.detail)
    finally:
        conn.close()


def test_validate_prompt_group_reports_missing_file_for_partial_manual_group() -> None:
    conn = make_conn()
    try:
        partial = prompts_root() / "partial"
        partial.mkdir(parents=True)
        (partial / "reason.md").write_text("{graph_yaml} {fact_ids} {open_intents} {max_intents} {knowledge_base}", encoding="utf-8")

        result = validate_prompt_group("partial")

        assert result.valid is False
        assert "bootstrap.md" in result.missing_files
    finally:
        conn.close()


def test_load_prompt_fails_closed_for_invalid_runtime_group() -> None:
    conn = make_conn()
    try:
        broken = prompts_root() / "broken"
        broken.mkdir(parents=True)
        (broken / "reason.md").write_text("BROKEN {fact_ids}", encoding="utf-8")

        with pytest.raises(
            ValueError,
            match=r"prompt group broken resource reason\.md missing (placeholders|SDK tools)",
        ):
            load_prompt("broken", "reason.md")
    finally:
        conn.close()


def test_clone_copies_all_present_allowed_prompt_files() -> None:
    conn = make_conn()
    try:
        create_prompt_group(conn, "full-copy", "goal-driven-v1")
        copied = {path.name for path in (prompts_root() / "full-copy").iterdir()}
        source = {
            name
            for name in PROMPT_ALLOWED_FILES
            if _builtin_prompt_file_exists("goal-driven-v1", name)
        }
        assert copied == source
        assert {"report.md", "verifier.md", "verifier_conclude.md"} <= copied
    finally:
        conn.close()


def _builtin_prompt_file_exists(group: str, name: str) -> bool:
    try:
        load_prompt(group, name)
    except FileNotFoundError:
        return False
    return True


def test_restore_referenced_managed_override_reveals_builtin() -> None:
    conn = make_conn()
    try:
        create_prompt_group(conn, "goal-driven-v1")
        references = conn.execute(
            "SELECT COUNT(*) FROM scenarios WHERE prompt_group = 'goal-driven-v1'"
        ).fetchone()[0]
        assert references > 0
        restored = restore_prompt_group(conn, "goal-driven-v1")
        assert restored.builtin is True
        assert restored.managed is False
        assert not (prompts_root() / "goal-driven-v1").exists()
    finally:
        conn.close()


def test_repair_partial_custom_group_requires_source_and_restores_contract() -> None:
    conn = make_conn()
    try:
        path = prompts_root() / "partial"
        path.mkdir(parents=True)
        (path / "reason.md").write_text("broken", encoding="utf-8")
        with pytest.raises(HTTPException, match="requires clone_from"):
            repair_prompt_group(conn, "partial")

        repaired = repair_prompt_group(conn, "partial", "goal-driven-v1")
        assert repaired.valid is True
        assert validate_prompt_group("partial").valid is True
    finally:
        conn.close()


def test_invalid_managed_goal_never_loads_builtin_fallback() -> None:
    conn = make_conn()
    try:
        managed = prompts_root() / "goal-driven-v1"
        managed.mkdir(parents=True)
        (managed / "reason.md").write_text("BROKEN {fact_ids}", encoding="utf-8")

        with pytest.raises(
            ValueError,
            match=(
                r"prompt group goal-driven-v1 resource reason\.md "
                r"missing (placeholders|SDK tools)"
            ),
        ):
            load_prompt("goal-driven-v1", "reason.md")
    finally:
        conn.close()


def test_write_builtin_prompt_group_requires_clone() -> None:
    conn = make_conn()
    try:
        with pytest.raises(HTTPException) as exc:
            write_prompt_file("goal-driven-v1", "reason.md", "nope")

        assert exc.value.status_code == 409
        assert "cloned" in str(exc.value.detail)
    finally:
        conn.close()


def test_delete_prompt_group_rejects_referenced_group() -> None:
    conn = make_conn()
    try:
        create_prompt_group(conn, "playbooks")
        replace_scenarios(
            conn,
            [
                Scenario(
                    key="custom",
                    label="Custom",
                    prompt_group="playbooks",
                    knowledge="knowledge_base",
                )
            ],
        )

        with pytest.raises(HTTPException) as exc:
            delete_prompt_group(conn, "playbooks")

        assert exc.value.status_code == 409
        assert "custom" in str(exc.value.detail)
    finally:
        conn.close()


def test_report_prompt_reference_blocks_deletion_and_is_deduplicated() -> None:
    conn = make_conn()
    try:
        create_prompt_group(conn, "shared-prompts", "goal-driven-v1")
        conn.execute(
            """
            INSERT INTO scenarios
                (key, label, is_default, profile_id, image, prompt_group, knowledge,
                 cap_add_json, features_json, enabled_workers_json)
            VALUES (?, ?, 0, 'default', '', ?, '', '[]', ?, '[]')
            """,
            (
                "shared-role-scenario",
                "Shared Role Scenario",
                "shared-prompts",
                '{"report_prompt_group":"shared-prompts","report_policy_version":"1"}',
            ),
        )
        conn.execute(
            "UPDATE scenarios SET features_json = '{broken' WHERE key = 'goal-driven-v1'"
        )

        group = {
            item.key: item for item in list_prompt_groups(conn)
        }["shared-prompts"]
        assert group.referenced_by == ["shared-role-scenario"]

        with pytest.raises(HTTPException, match="shared-role-scenario"):
            delete_prompt_group(conn, "shared-prompts")
    finally:
        conn.close()


def test_scenario_options_reflect_real_prompt_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    from cairn.server.routers import scenarios as scenario_router
    conn = make_conn()
    @contextmanager
    def fake_get_conn():
        yield conn
    try:
        create_prompt_group(conn, "custom-goal")
        monkeypatch.setattr(scenario_router, "get_conn", fake_get_conn)
        options = scenario_router.get_scenario_options()
        assert set(options.prompt_groups) == {"goal-driven-v1", "custom-goal"}
    finally:
        conn.close()


def test_app_exposes_prompt_group_routes() -> None:
    from cairn.server.app import app

    paths = app.openapi()["paths"]
    assert "/prompt-groups" in paths
    assert "/prompt-groups/{key}" in paths
    assert "/prompt-groups/{key}/files" in paths
    assert "/prompt-groups/{key}/files/{name}" in paths
    assert "/prompt-groups/{key}/validate" in paths
