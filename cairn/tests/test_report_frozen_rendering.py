"""冻结报告仅增加解释与建议，不改 canonical 证据或复核结论。"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.tasks import report


def _finding(vulnerability_id: str) -> dict[str, str]:
    return {
        "vulnerability_id": vulnerability_id,
        "impact": "需要结合授权业务范围评估影响。",
        "root_cause": "推断为对象授权缺口，缺少源码证据。",
        "repro_steps": "依据证据包的已有身份与对象操作复现；本次报告未执行。",
        "remediation": "在服务端检查对象归属与操作权限。",
        "regression_steps": "修复后使用正常身份和不同对象重测，记录正常对照。",
        "limitations": "只有黑盒证据，未检查源码。",
    }


def _data(*ids: str) -> dict[str, object]:
    return {"executive_summary": "建议优先处理权限边界。", "findings": [_finding(value) for value in ids], "limitations": "未执行修复后回归。", "artifacts": []}


def _packet(*, empty: bool = False) -> dict[str, object]:
    packet = {
        "version": 2,
        "project": {"id": "p1", "title": "冻结评估", "status": "stopped"},
        "assessment_summary": {
            "project_status": "stopped",
            "stop_reason": "explore_budget_exhausted",
            "execution": {
                "phase": "finalizing", "minimum_coverage_enabled": True,
                "budget": 5, "attempts_started": 5, "verification_reserve": 2,
                "verification_attempts_started": 1, "remaining_total": 0,
                "remaining_discovery": 0, "deadline": "2026-09-01T00:00:00Z",
                "depth_review_fact_id": "review-fact", "accepted_input_digest": "review-digest",
            },
            "coverage": {
                "tested": [{"slot_key": "c-tested", "result": "tested_no_finding", "reason": "", "fact_id": "f-tested"}],
                "blocked": [{"slot_key": "c-blocked", "result": "temporarily_blocked", "reason": "缺少第二身份", "fact_id": None}],
                "untested": [{"slot_key": "c-open", "result": None, "reason": "预算耗尽", "fact_id": None}],
            },
            "selected_deep": [{"id": "deep-1", "slot_key": "c-open", "depth_review_fact_id": "review-fact", "to_fact_id": None, "conclusion": "abandoned", "conclusion_reason": "explore_budget_exhausted"}],
            "calls_by_task_type": [{"task_type": "bootstrap", "execution_count": 1}, {"task_type": "reason", "execution_count": 2}, {"task_type": "report", "execution_count": 1}],
        },
        "targets": [{"id": "t1", "type": "url", "value": "https://target.invalid"}],
        "vulnerabilities": [
            {"id": "v1", "title": "相同标题", "severity": "high", "target": "/objects", "status": "open", "source_fact_id": "f1", "repro_request": "original request", "repro_response": "original response", "repro_steps": "original steps"},
            {"id": "v2", "title": "相同标题", "severity": "medium", "target": "/flow", "status": "open", "source_fact_id": "f2"},
        ],
        "observations": [
            {"id": "o1", "vulnerability_id": "v1", "source_fact_id": "f1", "task_run_id": "discovery-1", "packet_artifact_id": "a1"},
            {"id": "o2", "vulnerability_id": "v1", "source_fact_id": "f1", "task_run_id": "discovery-2", "packet_artifact_id": "a1"},
            {"id": "o3", "vulnerability_id": "v2", "source_fact_id": "f2", "task_run_id": "discovery-3", "packet_artifact_id": "a1"},
        ],
        "observation_statuses": [
            {"id": "o1", "vulnerability_id": "v1", "verification_status": "conflicting", "verifications": [{"id": "verify-yes", "outcome": "confirmed", "verifier_task_run_id": "vr1"}, {"id": "verify-no", "outcome": "not_reproduced", "verifier_task_run_id": "vr2"}], "intents": []},
            {"id": "o2", "vulnerability_id": "v1", "verification_status": "run_failed", "verifications": [], "intents": [{"id": "vi-failed", "conclusion": "failed", "conclusion_reason": "worker_timeout", "to_fact_id": None}]},
            {"id": "o3", "vulnerability_id": "v2", "verification_status": "not_run", "verifications": [], "intents": [{"id": "vi-no-budget", "conclusion": "abandoned", "conclusion_reason": "explore_budget_exhausted", "to_fact_id": None}]},
        ],
        "coverage_cells": [{"slot_key": "c-tested", "surface_key": "surface-1", "test_family": "authorization"}],
        "artifacts": [{"id": "a1", "kind": "file", "task_run_id": "discovery-1", "uri": "artifact://p1/a1"}],
        "evidence_links": [{"fact_id": "f1", "artifact_id": "a1", "pointer": "/evidence/request", "verifier_run_id": None}],
        "verifications": [{"id": "verify-yes", "observation_id": "o1", "verifier_task_run_id": "vr1", "evidence": [{"artifact_id": "a1", "pointer": "/evidence/control"}]}],
        "chain_candidates": [{"id": "chain-1"}],
    }
    if empty:
        for key in ("vulnerabilities", "observations", "observation_statuses", "verifications", "chain_candidates"):
            packet[key] = []
    return packet


def _goal_packet() -> dict[str, object]:
    packet = _packet(empty=True)
    packet["project"].update(assessment_protocol="goal-driven-v1")
    packet["policy"] = {"protocol": "goal-driven-v1"}
    packet["assessment_summary"]["execution"] = {
        "budget": 8,
        "attempts_started": 3,
        "verification_reserve": 2,
        "verification_attempts_started": 0,
        "remaining_total": 5,
        "remaining_discovery": 3,
        "deadline": None,
        "authorization_expires_at": None,
    }
    packet["assessment_summary"]["coverage"] = {
        "tested": [{
            "intent_id": "intent-tested",
            "description": "验证对象归属边界",
            "priority": 2,
            "result": "tested_no_finding",
            "reason": "",
            "fact_id": "fact-tested",
            "hypothesis": "对象读取必须检查归属",
            "observations": ["非所有者请求返回 403"],
            "limitations": ["仅验证了一个对象"],
            "attempted_strategies": ["更换对象 ID"],
            "evidence_refs": [{"artifact_id": "artifact-tested", "pointer": "/response/status"}],
        }],
        "blocked": [{
            "intent_id": "intent-blocked",
            "description": "比较第二身份",
            "priority": 1,
            "result": "temporarily_blocked",
            "reason": "缺少已授权第二身份",
            "fact_id": "fact-blocked",
            "hypothesis": "第二身份可能读取导出",
            "observations": [],
            "limitations": ["缺少第二身份", "未执行身份对照"],
            "attempted_strategies": ["检查可用测试身份"],
            "evidence_refs": [{"artifact_id": "artifact-blocked", "pointer": "/setup/accounts"}],
        }],
        "untested": [{
            "intent_id": "intent-open",
            "description": "检查导出权限",
            "priority": 0,
            "result": "abandoned",
            "reason": "user_stopped",
            "fact_id": None,
        }],
    }
    packet["coverage_cells"] = []
    return packet


class _ArtifactOnlyClient:
    def __init__(self, status_code: int = 201) -> None:
        self.status_code = status_code
        self.artifacts: list[dict[str, object]] = []

    def create_artifact(self, project_id: str, **kwargs) -> ApiResult:
        assert project_id == "p1"
        self.artifacts.append(kwargs)
        digest = sha256(kwargs["content"].encode("utf-8")).hexdigest()
        return ApiResult(
            status_code=self.status_code,
            data={"id": "report-artifact", "sha256": digest},
        )

    def update_vulnerability(self, *args, **kwargs):
        pytest.fail("报告不可更新 canonical 漏洞")

    def get_project(self, *args, **kwargs):
        pytest.fail("渲染不可重新读取实时项目状态")

    def list_vulnerabilities(self, *args, **kwargs):
        pytest.fail("渲染不可重新读取实时漏洞")


def test_packet_read_rejects_old_or_cross_project_input_before_freezing() -> None:
    class Client:
        packet = _packet()

        def get_assessment_report_packet(self, project_id: str) -> ApiResult:
            return ApiResult(status_code=200, data=self.packet)

    client = Client()
    assert report._collect_report_packet_json(client, "p1") is not None
    assert report._collect_report_packet_json(client, "another-project") is None
    client.packet["version"] = 1
    assert report._collect_report_packet_json(client, "p1") is None


def test_packet_context_keeps_distinct_ids_with_identical_titles() -> None:
    vulns_json, ids = report._packet_vulnerability_context(json.dumps(_packet()))
    assert ids == {"v1": "相同标题", "v2": "相同标题"}
    assert [item["id"] for item in json.loads(vulns_json)] == ["v1", "v2"]


@pytest.mark.parametrize(("ids", "error"), [
    (("v1",), "report_vulnerability_missing:v2"),
    (("v1", "v1", "v2"), "report_vulnerability_duplicate:v1"),
    (("v1", "v2", "unknown"), "report_vulnerability_unknown:unknown"),
    (("V1", "v2"), "report_vulnerability_unknown:V1"),
    (("相同标题", "v2"), "report_vulnerability_unknown:相同标题"),
])
def test_findings_require_exact_snapshot_id_set(ids, error) -> None:
    assert report._report_completeness_error(_data(*ids), id_to_title={"v1": "相同标题", "v2": "相同标题"}) == error
    client = _ArtifactOnlyClient()
    assert report._write_report_results(
        client,
        "p1",
        "report-run",
        data=_data(*ids),
        report_packet_json=json.dumps(_packet()),
    ) == "request_contract"
    assert client.artifacts == []


@pytest.mark.parametrize("field", ["title", "repro_request", "repro_response", "verification_status", "evidence_refs", "severity"])
def test_model_cannot_submit_fact_or_canonical_update_fields(field: str) -> None:
    data = _data("v1", "v2")
    data["findings"][0][field] = "fabricated"
    assert report._report_completeness_error(data, id_to_title={"v1": "", "v2": ""}) == "report_data_invalid"
    client = _ArtifactOnlyClient()
    assert report._write_report_results(
        client,
        "p1",
        "report-run",
        data=data,
        report_packet_json=json.dumps(_packet()),
    ) == "request_contract"
    assert not client.artifacts


@pytest.mark.parametrize("data", [
    {"vulnerability_updates": [], "report_markdown": "# old report"},
    {"executive_summary": "text", "findings": [{"title": "相同标题"}]},
    {"executive_summary": "text", "findings": [] , "vulnerability_updates": []},
])
def test_old_output_is_not_an_execution_compatibility_path(data) -> None:
    assert report._report_completeness_error(data, id_to_title={}) == "report_data_invalid"


def test_completeness_is_not_length_or_keyword_fact_validation() -> None:
    data = _data("v1")
    data["executive_summary"] = "短说明"
    assert report._report_completeness_error(data, id_to_title={"v1": "完全不同的标题"}) is None
    data["executive_summary"] = "执行摘要 范围 覆盖 漏洞 修复 剩余风险 证据 v1 " * 200
    data["findings"] = []
    assert report._report_completeness_error(data, id_to_title={"v1": ""}) == "report_vulnerability_missing:v1"


def test_whitespace_summary_is_not_a_report() -> None:
    data = _data()
    data["executive_summary"] = " \n "
    assert report._report_completeness_error(data, id_to_title={}) is not None


def test_render_preserves_counts_verifications_coverage_budget_and_evidence() -> None:
    packet = _packet()
    before = deepcopy(packet)
    data = _data("v2", "v1")
    markdown = report._render_report_markdown(data, report_packet_json=json.dumps(packet))
    assert packet == before
    assert markdown.startswith("# 安全评估部分报告")
    assert "Canonical 漏洞记录数：2；Observation 数：3" in markdown
    assert "| high | 1 |" in markdown
    assert "| medium | 1 |" in markdown
    assert "| o1 | v1 | conflicting | f1 | discovery-1 | a1 |" in markdown
    assert "| o1 | verify-yes | confirmed | vr1 |" in markdown
    assert "| o1 | verify-no | not_reproduced | vr2 |" in markdown
    assert "| o2 | v1 | run_failed |" in markdown
    assert "| o3 | v2 | not_run |" in markdown
    assert "| vi-no-budget | abandoned | explore_budget_exhausted |" in markdown
    assert "| vi-failed | failed | worker_timeout |" in markdown
    assert "### 已测（1）" in markdown
    assert "### 阻塞（1）" in markdown
    assert "### 未测/未完成（1）" in markdown
    assert "缺少第二身份" in markdown
    assert "c-open" in markdown and "deep-1" in markdown
    assert "| budget | 5 |" in markdown
    assert "| remaining_total | 0 |" in markdown
    assert "| verification_reserve | 2 |" in markdown
    assert "| report | 1 |" in markdown
    assert "| f1 | a1 | /evidence/request |" in markdown
    assert "| verify-yes | o1 | a1 | /evidence/control | vr1 |" in markdown
    assert "候选关联不等于已复现的利用链" in markdown
    assert "修复建议（未实施）" in markdown
    assert "修复后回归方法（未执行）" in markdown
    assert markdown.index("### 漏洞 v1") < markdown.index("### 漏洞 v2")


def test_goal_driven_render_uses_business_work_without_cell_or_depth_review() -> None:
    packet = _goal_packet()
    markdown = report._render_report_markdown(
        _data(),
        report_packet_json=json.dumps(packet),
    )

    assert "目标驱动工作按已接受的 ExperimentRecord 和 Intent 结论汇总" in markdown
    assert "| intent-tested | 验证对象归属边界 | 2 | tested_no_finding |" in markdown
    assert "| intent-blocked | 比较第二身份 | 1 | temporarily_blocked | 缺少已授权第二身份 |" in markdown
    assert "| intent-open | 检查导出权限 | 0 | abandoned | user_stopped |" in markdown
    assert "已选择的深测义务" not in markdown
    assert "depth_review_fact_id" not in markdown
    assert "| Cell |" not in markdown
    assert "工作结果与未完成事项" in markdown
    assert "已接受实验详情" in markdown
    assert "非所有者请求返回 403" in markdown
    assert "仅验证了一个对象" in markdown
    assert "更换对象 ID" in markdown
    assert "artifact-tested /response/status" in markdown
    assert "缺少第二身份; 未执行身份对照" in markdown
    assert json.loads(report._packet_coverage_json(json.dumps(packet))) == packet[
        "assessment_summary"
    ]["coverage"]


@pytest.mark.parametrize("status", ["confirmed", "not_reproduced", "environment_changed", "insufficient_evidence", "conflicting", "run_failed", "not_run", "pending", "not_scheduled"])
def test_every_observation_status_is_preserved_verbatim(status: str) -> None:
    packet = _packet()
    packet["observation_statuses"][0]["verification_status"] = status
    markdown = report._render_report_markdown(_data("v1", "v2"), report_packet_json=json.dumps(packet))
    assert f"| o1 | v1 | {status} |" in markdown


def test_stopped_zero_findings_produces_partial_report_without_model_no_finding_phrase() -> None:
    client = _ArtifactOnlyClient()
    identity = report._write_report_results(
        client,
        "p1",
        "report-run",
        data=_data(),
        report_packet_json=json.dumps(_packet(empty=True)),
    )
    assert isinstance(identity, report.ReportArtifactIdentity)
    assert identity.artifact_id == "report-artifact"
    markdown = client.artifacts[0]["content"]
    assert "# 安全评估部分报告" in markdown
    assert "项目已停止（stopped）" in markdown
    assert "未发现已确认漏洞不等于证明目标不存在漏洞" in markdown
    assert "explore_budget_exhausted" in markdown
    assert "缺少第二身份" in markdown


def test_unstructured_missing_coverage_is_not_claimed_as_complete() -> None:
    packet = _packet(empty=True)
    packet["assessment_summary"]["execution"]["minimum_coverage_enabled"] = False
    packet["assessment_summary"]["coverage"] = {"tested": [], "blocked": [], "untested": []}
    markdown = report._render_report_markdown(_data(), report_packet_json=json.dumps(packet))
    assert "缺失数据不能解释为零工作或完整覆盖" in markdown


def test_report_artifact_keeps_generation_and_snapshot_metadata_without_canonical_writes() -> None:
    client = _ArtifactOnlyClient()
    packet_json = json.dumps(_packet())
    snapshot_digest = sha256(packet_json.encode()).hexdigest()
    metadata = {
        "report_generation_token": "generation-2", "report_template_key": "default", "report_template_version": 3,
        "report_template_digest_sha256": "template-digest", "report_policy_version": 1,
        "report_execution_plan_sha256": "plan-digest", "report_prompt_digest_sha256": "prompt-digest",
        "completeness_rule_version": 1, "input_snapshot_artifact_id": "snapshot-1", "input_snapshot_sha256": snapshot_digest,
    }
    identity = report._write_report_results(
        client,
        "p1",
        "report-run",
        data=_data("v1", "v2"),
        report_packet_json=packet_json,
        **metadata,
    )
    assert isinstance(identity, report.ReportArtifactIdentity)
    assert identity.artifact_id == "report-artifact"
    assert len(client.artifacts) == 1
    artifact = client.artifacts[0]
    assert artifact["filename"] == report.REPORT_ARTIFACT_FILENAME
    assert artifact["kind"] == "report"
    assert artifact["task_run_id"] == "report-run"
    assert artifact["metadata"] == {"source": "report_task", "format": "markdown", **metadata}
    assert "original request" not in artifact["content"]
    assert "original response" not in artifact["content"]
    assert json.loads(packet_json)["vulnerabilities"][0]["repro_steps"] == "original steps"


def test_failed_artifact_write_is_not_a_success() -> None:
    client = _ArtifactOnlyClient(status_code=503)
    assert report._write_report_results(
        client,
        "p1",
        "report-run",
        data=_data(),
        report_packet_json=json.dumps(_packet(empty=True)),
    ) == "server_write_failed"


@pytest.mark.parametrize("mutation", ["old_version", "wrong_project", "missing_summary", "missing_coverage", "missing_vulnerabilities", "duplicate_vulnerability", "missing_observation_status", "unknown_observation_status", "wrong_observation_vulnerability"])
def test_corrupt_or_cross_project_packet_cannot_become_zero_finding_report(mutation: str) -> None:
    packet = _packet()
    if mutation == "old_version":
        packet["version"] = 1
    elif mutation == "wrong_project":
        packet["project"]["id"] = "another-project"
    elif mutation == "missing_summary":
        packet["assessment_summary"] = {}
    elif mutation == "missing_coverage":
        packet["assessment_summary"]["coverage"].pop("untested")
    elif mutation == "missing_vulnerabilities":
        packet.pop("vulnerabilities")
    elif mutation == "duplicate_vulnerability":
        packet["vulnerabilities"].append(packet["vulnerabilities"][0])
    elif mutation == "missing_observation_status":
        packet["observation_statuses"].pop()
    elif mutation == "unknown_observation_status":
        packet["observation_statuses"][0]["verification_status"] = "fixed"
    else:
        packet["observation_statuses"][0]["vulnerability_id"] = "v2"
    client = _ArtifactOnlyClient()
    assert report._write_report_results(
        client,
        "p1",
        "report-run",
        data=_data("v1", "v2"),
        report_packet_json=json.dumps(packet),
    ) == "request_contract"
    assert not client.artifacts


@pytest.mark.parametrize("group", ["default-report", "default-durable-report-v1", "default-copy-v2", "goal-driven-v1"])
def test_report_prompt_only_requests_narrow_explanatory_result(group: str) -> None:
    prompt = (Path(report.__file__).parents[1] / "prompts" / group / "report.md").read_text()
    assert "submit_result" in prompt
    assert "executive_summary" in prompt and "regression_steps" in prompt
    assert "vulnerability_updates" not in prompt
    assert "report_markdown" not in prompt
    assert "Return one raw JSON" not in prompt
    assert "{report_packet_json}" in prompt
    if group == "goal-driven-v1":
        assert "Coverage 的 required/optional" not in prompt
        assert "不把选中的 deep 工作略去" not in prompt
        assert "ExperimentRecord" in prompt


def test_frozen_plan_rejects_old_execution_without_translation() -> None:
    prompt = "只解释冻结证据"
    prompt_digest = sha256(prompt.encode()).hexdigest()
    plan = {
        "version": 2, "execution_contract_version": 1, "execution_engine": "pi",
        "report_policy_version": 1, "packet_version": 2, "output_contract_version": 2,
        "completeness_rule_version": 1, "report_prompt_name": "report.md",
        "report_prompt_group": "default-durable-report-v1", "report_prompt_digest_sha256": prompt_digest,
    }

    def check() -> str | None:
        canonical = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return report._frozen_execution_error(canonical, plan, sha256(canonical.encode()).hexdigest(), prompt, prompt_digest)

    assert check() is None
    plan["version"] = 1
    assert check() == "report_execution_plan_unsupported:version"
    plan["version"] = 2
    plan["execution_engine"] = "claudecode"
    assert check() == "report_execution_plan_unsupported:execution_engine"
