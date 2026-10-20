from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from cairn.dispatcher.prompt_resources import validate_report_prompt_resources
from cairn.server.schemas.scenarios import Scenario
from cairn.server.services.assessment_policy import frozen_execution_features, scenario_snapshot
from cairn.server.services import assessment_reports, project_records
from cairn.server.services.report_policy import (
    freeze_report_execution_plan,
    parse_report_execution_plan,
    report_policy_from_project_row,
)


def _row(snapshot: object, *, status: str = "completed") -> dict[str, str]:
    return {
        "id": "synthetic-project", "status": status, "assessment_protocol": "goal-driven-v1",
        "scenario_snapshot_json": json.dumps(snapshot, ensure_ascii=False),
    }


def _snapshot(*, features: dict[str, object] | None = None) -> dict[str, object]:
    snapshot = scenario_snapshot(Scenario(
        key="custom-exploration", label="受控探索", prompt_group="custom-exploration",
        profile_id="restricted-profile", knowledge="web-security", features={"sandbox": True},
    ))
    if features:
        snapshot["features"].update(features)
    return snapshot


@pytest.mark.parametrize("retired_features", [
    {},
    {"report_policy_version": "0"},
    {"report_policy_version": "1"},
    {"report_policy_version": "1", "report_prompt_group": "old-custom-report"},
])
def test_new_and_old_projects_use_one_platform_policy_without_rewriting_snapshot(retired_features) -> None:
    row = _row(_snapshot(features=retired_features))
    original = deepcopy(row)
    policy = report_policy_from_project_row(row)
    assert policy.supported is True
    assert policy.report_prompt_group == "goal-driven-v1"
    assert policy.execution_profile_id == "restricted-profile"
    assert policy.knowledge_key == "web-security"
    assert policy.source == "platform-v1"
    assert row == original


def test_new_project_execution_snapshot_does_not_create_report_opt_in_facts() -> None:
    scenario = Scenario(key="custom", label="自定义", features={"sandbox": True})
    features = frozen_execution_features(scenario)
    assert "report_policy_version" not in features
    assert "report_prompt_group" not in features
    assert scenario.features == {"sandbox": True}
    assert features["assessment_protocol"] == "goal-driven-v1"
    assert features["execution_engine"] == "pi"


def test_platform_report_policy_freezes_only_pi_packet_contract() -> None:
    validate_report_prompt_resources("goal-driven-v1")
    plan, prompt_markdown = freeze_report_execution_plan(report_policy_from_project_row(_row(_snapshot())))
    assert plan.report_prompt_group == "goal-driven-v1"
    assert plan.version == 2
    assert plan.packet_version == 2
    assert plan.output_contract_version == 3
    assert plan.execution_engine == "pi"
    assert plan.execution_contract_version == 1
    assert "{report_packet_json}" in prompt_markdown
    assert "submit_result" in prompt_markdown
    assert parse_report_execution_plan(plan.canonical_json()) == plan


@pytest.mark.parametrize(("field", "value"), [
    ("version", 1), ("packet_version", 1), ("output_contract_version", 2),
    ("execution_engine", "claudecode"), ("execution_contract_version", None),
])
def test_old_unsupported_frozen_plan_is_rejected_not_upgraded(field: str, value: object) -> None:
    plan, _ = freeze_report_execution_plan(report_policy_from_project_row(_row(_snapshot())))
    payload = json.loads(plan.canonical_json())
    payload[field] = value
    with pytest.raises(ValueError, match="report_execution_contract_unsupported"):
        parse_report_execution_plan(json.dumps(payload))
    assert payload[field] == value


def test_accepted_generation_keeps_its_frozen_custom_prompt_plan() -> None:
    policy = report_policy_from_project_row(_row(_snapshot()))
    plan, _ = freeze_report_execution_plan(policy)
    frozen = replace(plan, report_prompt_group="old-custom-report", source="explicit-v1")
    assert parse_report_execution_plan(frozen.canonical_json()) == frozen
    assert report_policy_from_project_row(_row(_snapshot())).report_prompt_group != frozen.report_prompt_group


@pytest.mark.parametrize("row, reason", [
    ({}, "missing_or_invalid_scenario_snapshot"),
    ({"scenario_snapshot_json": "{"}, "missing_or_invalid_scenario_snapshot"),
    (_row({**_snapshot(), "profile_id": ""}), "incomplete_scenario_snapshot"),
    (_row({**_snapshot(), "features": []}), "invalid_scenario_features"),
    (_row(_snapshot(features={"execution_engine": "unsupported"})), "execution_contract_unsupported"),
    ({**_row(_snapshot()), "assessment_protocol": "external-verification"}, "assessment_contract_unsupported"),
])
def test_invalid_snapshots_and_nonordinary_contracts_remain_unsupported(row, reason) -> None:
    original = deepcopy(row)
    policy = report_policy_from_project_row(row)
    assert policy.supported is False
    assert policy.reason == reason
    assert row == original


class _ReportRequestConnection:
    """仅替代报告受理的存储边界，不运行数据库、模型或真实项目操作。"""

    def __init__(self):
        self.finalization = None
        self.writes = 0

    def execute(self, sql, parameters):
        if "INSERT INTO assessment_report_finalizations" in sql:
            names = (
                "project_id", "version", "generation_started_at", "generation_token",
                "requested_at", "requested_by", "template_key", "template_version",
                "template_digest_sha256", "template_markdown", "template_profiles_json",
                "report_policy_version", "report_execution_plan_json", "report_execution_plan_sha256",
                "report_prompt_digest_sha256", "report_prompt_markdown", "created_at", "updated_at",
            )
            self.finalization = dict(zip(names, parameters, strict=True))
            self.finalization.update(status="pending", attempt_count=0, last_error="")
            self.writes += 1
        else:
            assert "SELECT * FROM assessment_report_finalizations" in sql
        return SimpleNamespace(fetchone=lambda: self.finalization)


def _request_dependencies(monkeypatch, project):
    monkeypatch.setattr(assessment_reports, "_lock_report_project", lambda _conn, _id: project)
    monkeypatch.setattr(project_records, "project_pause_unconfirmed", lambda _conn, _id: False)
    monkeypatch.setattr(assessment_reports, "assessment_has_running_work", lambda _conn, _id: False)
    markdown = "# 受控模板\n"
    monkeypatch.setattr(assessment_reports, "require_report_template_profile", lambda *_args: SimpleNamespace(
        key="synthetic-template", digest_sha256=sha256(markdown.encode()).hexdigest(),
        markdown_content=markdown, compatibility_profiles=["assessment-report-packet-v1"],
    ))


@pytest.mark.parametrize("retired_features", [{}, {"report_policy_version": "0"}])
def test_new_and_old_snapshot_can_explicitly_request_a_new_generation(monkeypatch, retired_features) -> None:
    project = _row(_snapshot(features=retired_features))
    original = deepcopy(project)
    _request_dependencies(monkeypatch, project)
    conn = _ReportRequestConnection()
    result = assessment_reports.request_assessment_report_generation(
        conn, project["id"], template_key="synthetic-template", requested_by="synthetic-user",
    )
    assert result.status == "pending"
    assert result.requested_at is not None
    assert result.requested_by == "synthetic-user"
    assert result.template_key == "synthetic-template"
    assert conn.writes == 1
    plan = parse_report_execution_plan(conn.finalization["report_execution_plan_json"])
    assert plan.report_prompt_group == "goal-driven-v1"
    assert plan.source == "platform-v1"
    assert project == original


def test_reaccepted_pending_generation_never_reloads_platform_plan_or_template(monkeypatch) -> None:
    project = _row(_snapshot())
    _request_dependencies(monkeypatch, project)
    conn = _ReportRequestConnection()
    first = assessment_reports.request_assessment_report_generation(
        conn, project["id"], template_key="synthetic-template", requested_by="synthetic-user",
    )
    plan = parse_report_execution_plan(conn.finalization["report_execution_plan_json"])
    frozen = replace(plan, report_prompt_group="accepted-custom-report", source="explicit-v1")
    conn.finalization["report_execution_plan_json"] = frozen.canonical_json()
    conn.finalization["report_execution_plan_sha256"] = frozen.digest_sha256()
    before = deepcopy(conn.finalization)

    def unexpected_reload(*_args):
        raise AssertionError("已受理 generation 不应重新冻结实时配置")

    monkeypatch.setattr(assessment_reports, "freeze_report_execution_plan", unexpected_reload)
    monkeypatch.setattr(assessment_reports, "require_report_template_profile", unexpected_reload)
    second = assessment_reports.request_assessment_report_generation(
        conn, project["id"], template_key="synthetic-template", requested_by="synthetic-user",
    )
    assert second.generation_token == first.generation_token
    assert conn.finalization == before
    assert conn.writes == 1


def test_default_report_capability_does_not_bypass_active_project_restriction(monkeypatch) -> None:
    project = _row(_snapshot(), status="active")
    _request_dependencies(monkeypatch, project)
    conn = _ReportRequestConnection()
    with pytest.raises(HTTPException) as rejected:
        assessment_reports.request_assessment_report_generation(
            conn, project["id"], template_key="synthetic-template", requested_by="synthetic-user",
        )
    assert rejected.value.detail == "assessment_report_project_active"
    assert conn.writes == 0
