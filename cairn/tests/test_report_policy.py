from __future__ import annotations

import json

import pytest

from cairn.dispatcher.prompt_resources import validate_report_prompt_resources
from cairn.server.services.report_policy import (
    freeze_report_execution_plan,
    parse_report_execution_plan,
    report_policy_from_project_row,
)


def _row(snapshot: object) -> dict[str, str]:
    return {"scenario_snapshot_json": json.dumps(snapshot)}


def _snapshot(
    *,
    key: str = "goal-driven-v1",
    prompt_group: str = "goal-driven-v1",
    profile_id: str = "default",
    features: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "key": key,
        "prompt_group": prompt_group,
        "profile_id": profile_id,
        "knowledge": "web-security",
        "features": features if features is not None else {
            "report_policy_version": "1", "report_prompt_group": "goal-driven-v1",
        },
    }


def test_explicit_report_policy_freezes_only_pi_packet_contract() -> None:
    validate_report_prompt_resources("goal-driven-v1")

    explicit_policy = report_policy_from_project_row(
        _row(
            _snapshot(
                features={
                    "report_policy_version": "1",
                    "report_prompt_group": "goal-driven-v1",
                }
            )
        )
    )
    assert explicit_policy.supported is True
    assert explicit_policy.report_prompt_group == "goal-driven-v1"
    assert explicit_policy.source == "explicit-v1"

    plan, prompt_markdown = freeze_report_execution_plan(explicit_policy)
    assert plan.report_prompt_group == "goal-driven-v1"
    assert plan.version == 2
    assert plan.packet_version == 2
    assert plan.execution_engine == "pi"
    assert plan.execution_contract_version == 1
    assert "{report_packet_json}" in prompt_markdown
    assert "submit_result" in prompt_markdown
    assert parse_report_execution_plan(plan.canonical_json()) == plan


@pytest.mark.parametrize(("field", "value"), [
    ("version", 1), ("packet_version", 1),
    ("execution_engine", "claudecode"), ("execution_contract_version", None),
])
def test_old_frozen_plan_is_rejected_not_upgraded(field: str, value: object) -> None:
    plan, _ = freeze_report_execution_plan(report_policy_from_project_row(_row(_snapshot())))
    payload = json.loads(plan.canonical_json())
    payload[field] = value
    with pytest.raises(ValueError, match="report_execution_contract_unsupported"):
        parse_report_execution_plan(json.dumps(payload))
    assert payload[field] == value


def test_future_scenario_can_explicitly_opt_in_without_key_allowlist() -> None:
    policy = report_policy_from_project_row(
        _row(
            _snapshot(
                key="future-scenario",
                prompt_group="future-explore",
                profile_id="future-profile",
                features={
                    "report_policy_version": "1",
                    "report_prompt_group": "future-report",
                },
            )
        )
    )

    assert policy.supported is True
    assert policy.report_prompt_group == "future-report"
    assert policy.execution_profile_id == "future-profile"
    assert policy.source == "explicit-v1"


def test_missing_or_malformed_frozen_snapshot_fails_closed() -> None:
    missing = report_policy_from_project_row({})
    malformed = report_policy_from_project_row({"scenario_snapshot_json": "{"})

    assert missing.supported is False
    assert missing.reason == "missing_or_invalid_scenario_snapshot"
    assert malformed.supported is False
    assert malformed.reason == "missing_or_invalid_scenario_snapshot"


def test_unknown_policy_version_and_incomplete_opt_in_fail_closed() -> None:
    unknown = report_policy_from_project_row(
        _row(_snapshot(features={"report_policy_version": "2"}))
    )
    incomplete = report_policy_from_project_row(
        _row(_snapshot(features={"report_policy_version": "1"}))
    )

    assert unknown.supported is False
    assert unknown.reason == "unsupported_report_policy_version"
    assert incomplete.supported is False
    assert incomplete.reason == "report_prompt_group_missing"


@pytest.mark.parametrize("group", ["default", "default-copy-v2", "default-report"])
def test_removed_builtin_names_do_not_enable_implicit_report_policy(group: str) -> None:
    policy = report_policy_from_project_row(_row(_snapshot(key=group, prompt_group=group, features={})))
    assert policy.supported is False
    assert policy.reason == "report_policy_not_enabled"
