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


V3_TEMPLATE = "# {{ report_title }}\n\n## 漏洞成果汇总\n\n## 漏洞详情\n\n## 整体修复建议\n\n## 附录：证据索引\n"


def _layout(*ids: str) -> list[dict[str, str]]:
    return [
        {"type": "markdown", "markdown": "# 安全评估部分报告\n\n## 漏洞成果汇总"},
        *({"type": "fact", "ref": ref} for ref in ("scope", "counts", "work_results", "execution_status")),
        {"type": "markdown", "markdown": "## 漏洞详情"},
        *({"type": "fact", "ref": f"finding:{value}"} for value in ids),
        {"type": "markdown", "markdown": "## 整体修复建议\n\n建议优先恢复服务端权限边界，修复后按正常对照回归。"},
        {"type": "fact", "ref": "limitations"},
        {"type": "markdown", "markdown": "## 附录：证据索引"},
        {"type": "fact", "ref": "evidence_index"},
    ]


def _data(*ids: str) -> dict[str, object]:
    return {
        "schema_version": 2,
        "executive_summary": "建议优先处理权限边界。",
        "findings": [_finding(value) for value in ids],
        "layout": _layout(*ids),
        "limitations": "未执行修复后回归。",
        "artifacts": [],
    }


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
    packet["assessment_summary"]["stop_reason"] = "user_stopped"
    packet["assessment_summary"]["execution"] = {
        "attempts_started": 47,
        "verification_attempts_started": 0,
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
        report_template_markdown=V3_TEMPLATE,
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
        report_template_markdown=V3_TEMPLATE,
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
    markdown = report._render_report_markdown(data, report_packet_json=json.dumps(packet), report_template_markdown=V3_TEMPLATE)
    assert packet == before
    assert markdown.startswith("# 安全评估部分报告")
    assert "Canonical 漏洞记录数：2；Observation 数：3" in markdown
    assert "| high | 1 |" in markdown
    assert "| medium | 1 |" in markdown
    assert "| o1 | v1 | conflicting | f1 | discovery-1 | 历史冻结 packet Artifact: a1 |" in markdown
    assert "| o1 | verify-yes | confirmed | vr1 |" in markdown
    assert "| o1 | verify-no | not_reproduced | vr2 |" in markdown
    assert "| o2 | v1 | run_failed |" in markdown
    assert "| o3 | v2 | not_run |" in markdown
    assert "| vi-no-budget | abandoned | explore_budget_exhausted |" in markdown
    assert "| vi-failed | failed | worker_timeout |" in markdown
    assert "#### 已测（1）" in markdown
    assert "#### 阻塞（1）" in markdown
    assert "#### 未测/未完成（1）" in markdown
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
    assert markdown.index("### 漏洞 v2") < markdown.index("### 漏洞 v1")


def test_goal_driven_render_uses_business_work_without_cell_or_depth_review() -> None:
    packet = _goal_packet()
    markdown = report._render_report_markdown(
        _data(),
        report_packet_json=json.dumps(packet),
        report_template_markdown=V3_TEMPLATE,
    )

    assert "目标驱动工作按已接受的 ExperimentRecord 和 Intent 结论汇总" in markdown
    assert "| intent-tested | 验证对象归属边界 | 2 | tested_no_finding |" in markdown
    assert "| intent-blocked | 比较第二身份 | 1 | temporarily_blocked | 缺少已授权第二身份 |" in markdown
    assert "| intent-open | 检查导出权限 | 0 | abandoned | user_stopped |" in markdown
    assert "已选择的深测义务" not in markdown
    assert "depth_review_fact_id" not in markdown
    assert "| Cell |" not in markdown
    assert "工作结果与未完成事项" in markdown
    assert "执行停止时间与累计计量" in markdown
    assert "| deadline | 不限 |" in markdown
    assert "| attempts_started | 47 |" in markdown
    assert "| budget |" not in markdown
    assert "verification_reserve" not in markdown
    assert "remaining_discovery" not in markdown
    assert "已接受实验详情" in markdown
    assert "非所有者请求返回 403" in markdown
    assert "仅验证了一个对象" in markdown
    assert "更换对象 ID" in markdown
    assert "artifact-tested /response/status" in markdown
    assert "缺少第二身份; 未执行身份对照" in markdown
    assert json.loads(report._packet_coverage_json(json.dumps(packet))) == packet[
        "assessment_summary"
    ]["coverage"]


def test_historical_goal_driven_packet_keeps_frozen_limits_without_reinterpreting_policy() -> None:
    packet = _goal_packet()
    packet["assessment_summary"]["execution"].update(
        budget=8, verification_reserve=2, remaining_total=5, remaining_discovery=3,
    )
    before = deepcopy(packet)
    markdown = report._render_report_markdown(_data(), report_packet_json=json.dumps(packet), report_template_markdown=V3_TEMPLATE)
    assert packet == before
    assert "历史额度与执行计量（冻结原件）" in markdown
    assert "| budget | 8 |" in markdown
    assert "| verification_reserve | 2 |" in markdown
    assert "| remaining_discovery | 3 |" in markdown
    assert "不是当前目标驱动的有效额度政策" in markdown


@pytest.mark.parametrize("status", ["confirmed", "not_reproduced", "environment_changed", "insufficient_evidence", "conflicting", "run_failed", "not_run", "pending", "not_scheduled"])
def test_every_observation_status_is_preserved_verbatim(status: str) -> None:
    packet = _packet()
    packet["observation_statuses"][0]["verification_status"] = status
    markdown = report._render_report_markdown(_data("v1", "v2"), report_packet_json=json.dumps(packet), report_template_markdown=V3_TEMPLATE)
    assert f"| o1 | v1 | {status} |" in markdown


def test_stopped_zero_findings_produces_partial_report_without_model_no_finding_phrase() -> None:
    client = _ArtifactOnlyClient()
    identity = report._write_report_results(
        client,
        "p1",
        "report-run",
        data=_data(),
        report_packet_json=json.dumps(_packet(empty=True)),
        report_template_markdown=V3_TEMPLATE,
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
    markdown = report._render_report_markdown(_data(), report_packet_json=json.dumps(packet), report_template_markdown=V3_TEMPLATE)
    assert "缺失数据不能解释为零工作或完整覆盖" in markdown


def test_report_artifact_keeps_generation_and_snapshot_metadata_without_canonical_writes() -> None:
    client = _ArtifactOnlyClient()
    packet_json = json.dumps(_packet())
    snapshot_digest = sha256(packet_json.encode()).hexdigest()
    metadata = {
        "report_generation_token": "generation-2", "report_template_key": "default",
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
        report_template_markdown=V3_TEMPLATE,
        **metadata,
    )
    assert isinstance(identity, report.ReportArtifactIdentity)
    assert identity.artifact_id == "report-artifact"
    assert len(client.artifacts) == 1
    artifact = client.artifacts[0]
    assert artifact["filename"] == report.REPORT_ARTIFACT_FILENAME
    assert artifact["kind"] == "report"
    assert artifact["task_run_id"] == "report-run"
    assert artifact["metadata"] == {
        "source": "report_task", "format": "markdown", "packet_version": 2,
        "output_contract_version": 3, "report_data_schema_version": 2, **metadata,
    }
    assert "report_template_version" not in artifact["metadata"]
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
        report_template_markdown=V3_TEMPLATE,
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
        report_template_markdown=V3_TEMPLATE,
    ) == "request_contract"
    assert not client.artifacts


def test_report_prompt_requests_explanations_and_single_layout() -> None:
    prompt = (Path(report.__file__).parents[1] / "prompts" / "goal-driven-v1" / "report.md").read_text()
    assert "submit_result" in prompt
    assert "executive_summary" in prompt and "regression_steps" in prompt
    assert "vulnerability_updates" not in prompt
    assert "report_markdown" not in prompt
    assert "Return one raw JSON" not in prompt
    assert "{report_packet_json}" in prompt
    assert "Coverage 的 required/optional" not in prompt
    assert "不把选中的 deep 工作略去" not in prompt
    assert "ExperimentRecord" in prompt
    assert "schema_version: 2" in prompt
    assert "layout" in prompt and "finding:<" in prompt


def test_frozen_plan_rejects_old_execution_without_translation() -> None:
    prompt = "只解释冻结证据"
    prompt_digest = sha256(prompt.encode()).hexdigest()
    plan = {
        "version": 2, "execution_contract_version": 1, "execution_engine": "pi",
        "report_policy_version": 1, "packet_version": 2, "output_contract_version": 3,
        "completeness_rule_version": 1, "report_prompt_name": "report.md",
        "report_prompt_group": "default-durable-report-v1", "report_prompt_digest_sha256": prompt_digest,
    }

    def check() -> str | None:
        canonical = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return report._frozen_execution_error(canonical, plan, sha256(canonical.encode()).hexdigest(), prompt, prompt_digest)

    assert check() is None
    plan["output_contract_version"] = 2
    assert check() == "report_execution_plan_unsupported:output_contract_version"
    plan["output_contract_version"] = 3
    plan["version"] = 1
    assert check() == "report_execution_plan_unsupported:version"
    plan["version"] = 2
    plan["execution_engine"] = "claudecode"
    assert check() == "report_execution_plan_unsupported:execution_engine"


def test_same_packet_two_templates_change_real_artifact_structure_not_facts() -> None:
    packet = _goal_packet()
    original = _packet()
    for key in ("vulnerabilities", "observations", "observation_statuses", "verifications", "chain_candidates"):
        packet[key] = original[key]
    before = deepcopy(packet)
    first = _data("v1", "v2")
    second = deepcopy(first)
    second["layout"] = [
        {"type": "markdown", "markdown": "# 冻结材料交付\n\n## 事实及发现"},
        *({"type": "fact", "ref": item["ref"]} for item in first["layout"] if item["type"] == "fact" and item["ref"] not in {"limitations", "evidence_index"}),
        {"type": "markdown", "markdown": "## 修复路线与附件\n\n先修复边界，再记录回归证据。"},
        {"type": "fact", "ref": "limitations"},
        {"type": "fact", "ref": "evidence_index"},
    ]
    alternative = "# 冻结材料交付\n\n## 事实及发现\n\n## 修复路线与附件\n"
    outputs = []
    for data, template in ((first, V3_TEMPLATE), (second, alternative)):
        client = _ArtifactOnlyClient()
        identity = report._write_report_results(
            client, "p1", "report-run", data=data,
            report_packet_json=json.dumps(packet), report_template_markdown=template,
        )
        assert isinstance(identity, report.ReportArtifactIdentity)
        markdown = client.artifacts[0]["content"]
        assert report._report_main_headings(markdown) == report._report_main_headings(template.replace("{{ report_title }}", "安全评估部分报告"))
        for text in (
            "Canonical 漏洞记录数：2；Observation 数：3", "conflicting", "run_failed", "not_run",
            "非所有者请求返回 403", "仅验证了一个对象", "缺少第二身份; 未执行身份对照",
            "a1", "/evidence/request", "漏洞 v1", "漏洞 v2", "未执行修复后回归。",
        ):
            assert text in markdown
        assert "## 1." not in markdown and "## 10." not in markdown
        facts = markdown
        for item in data["layout"]:
            if item["type"] == "markdown":
                facts = facts.replace(item["markdown"], "")
        outputs.append((markdown, facts.strip()))
    assert packet == before
    assert outputs[0][0] != outputs[1][0]
    assert outputs[0][1].replace("\n", "") == outputs[1][1].replace("\n", "")
    assert [title for level, title in report._report_main_headings(outputs[0][0]) if level == 2] == [
        "漏洞成果汇总", "漏洞详情", "整体修复建议", "附录：证据索引",
    ]


@pytest.mark.parametrize(("mutation", "error"), [
    ("unknown", "report_layout_ref_unknown:unknown"),
    ("unknown_finding", "report_layout_ref_unknown:finding:unknown"),
    ("duplicate", "report_layout_ref_duplicate:scope"),
    ("duplicate_finding", "report_layout_ref_duplicate:finding:v1"),
    ("missing", "report_layout_ref_missing:counts"),
    ("missing_finding", "report_layout_ref_missing:finding:v2"),
])
def test_invalid_layout_references_fail_without_old_report_fallback(mutation, error) -> None:
    data = _data("v1", "v2")
    if mutation in {"unknown", "unknown_finding", "duplicate", "duplicate_finding"}:
        ref = {"unknown": "unknown", "unknown_finding": "finding:unknown", "duplicate": "scope", "duplicate_finding": "finding:v1"}[mutation]
        data["layout"].append({"type": "fact", "ref": ref})
    else:
        ref = "counts" if mutation == "missing" else "finding:v2"
        data["layout"] = [item for item in data["layout"] if item.get("ref") != ref]
    assert report._report_completeness_error(data, id_to_title={"v1": "", "v2": ""}) == error
    with pytest.raises(ValueError, match=error):
        report._render_report_markdown(data, report_packet_json=json.dumps(_packet()), report_template_markdown=V3_TEMPLATE)
    client = _ArtifactOnlyClient()
    assert report._write_report_results(
        client, "p1", "report-run", data=data, report_packet_json=json.dumps(_packet()), report_template_markdown=V3_TEMPLATE,
    ) == "request_contract"
    assert not client.artifacts


@pytest.mark.parametrize("replacement", ["## 改名", "## 漏洞详情\n\n## 漏洞成果汇总", "## 漏洞成果汇总\n\n## 额外章节"])
def test_fixed_template_heading_name_count_and_order_are_required(replacement) -> None:
    data = _data("v1", "v2")
    data["layout"][0]["markdown"] = "# 安全评估部分报告\n\n" + replacement
    with pytest.raises(ValueError, match="report_template_heading_mismatch"):
        report._render_report_markdown(data, report_packet_json=json.dumps(_packet()), report_template_markdown=V3_TEMPLATE)


@pytest.mark.parametrize("title", ["{{ report_title }}", "{{report_title}}", "安全评估 {{ report_title }}"])
def test_template_title_placeholder_must_be_expanded(title: str) -> None:
    data = _data("v1", "v2")
    data["layout"][0]["markdown"] = f"# {title}\n\n## 漏洞成果汇总"
    with pytest.raises(ValueError, match="report_template_heading_mismatch"):
        report._render_report_markdown(data, report_packet_json=json.dumps(_packet()), report_template_markdown=V3_TEMPLATE)


@pytest.mark.parametrize("placement", ["layout", "repro_steps"])
@pytest.mark.parametrize("fragment", [
    "1. ```http\n   GET / HTTP/1.1\n   ```",
    "10. ```http\n    GET / HTTP/1.1\n    ```",
    "1.     ```http\n       GET / HTTP/1.1",
    "<!-- 已闭合 --> <!-- 也已闭合 --> ## 同行文本不是主章节",
])
def test_closed_markdown_fragments_are_preserved(placement: str, fragment: str) -> None:
    data = _data("v1", "v2")
    if placement == "layout":
        data["layout"].insert(-1, {"type": "markdown", "markdown": fragment})
    else:
        data["findings"][0]["repro_steps"] = fragment
    markdown = report._render_report_markdown(
        data, report_packet_json=json.dumps(_packet()), report_template_markdown=V3_TEMPLATE,
    )
    assert fragment in markdown
    assert markdown.index(fragment) < markdown.index("### 证据索引（冻结快照）")


def test_heading_extractor_ignores_code_and_html_but_supports_setext() -> None:
    markdown = """标题
===

章节
---

```markdown
# 示例不是标题
## 代码内章节
```

~~~~
## 另一个代码示例
~~~~

    ## 缩进代码

<!--
## 注释不是标题
-->

<pre>
## HTML 内不是标题
</pre>

## 真实章节 ##
"""
    assert report._report_main_headings(markdown, require_closed=True) == [(1, "标题"), (2, "章节"), (2, "真实章节")]
    data = _data("v1", "v2")
    data["layout"].append({"type": "markdown", "markdown": "```markdown\n## 不是额外主章节\n```\n\n    # 也不是标题\n\n行内代码 `<!--` 只表示语法示例。"})
    assert report._render_report_markdown(data, report_packet_json=json.dumps(_packet()), report_template_markdown=V3_TEMPLATE)


@pytest.mark.parametrize("placement", ["layout", "repro_steps"])
@pytest.mark.parametrize("fragment", [
    "```markdown", "~~~", "<!-- 未闭合", "说明 <!-- 未闭合", "<pre>未闭合",
    "1. ```http\n   GET / HTTP/1.1", "<!-- 已闭合 --> <!--",
])
def test_fragment_cannot_hide_following_facts_in_unclosed_block(placement: str, fragment: str) -> None:
    data = _data("v1", "v2")
    if placement == "layout":
        data["layout"].insert(-1, {"type": "markdown", "markdown": fragment})
    else:
        data["findings"][0]["repro_steps"] = fragment
    with pytest.raises(ValueError, match="report_layout_unclosed_block"):
        report._render_report_markdown(data, report_packet_json=json.dumps(_packet()), report_template_markdown=V3_TEMPLATE)
