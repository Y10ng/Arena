"""用合成冻结原件验证完整模板排版、证据归属与解释边界，不调用模型。"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json

import pytest

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.tasks import report
from cairn.dispatcher.worker_protocol.submission import build_submit_result_schema
from cairn.report_template_contract import parse_report_template


EVIDENCE_TEMPLATE = """{{#finding.evidence}}
**{{ evidence.title }}**
{{ evidence.association }}

请求
```http
{{ evidence.request }}
```

响应
```http
{{ evidence.response }}
```
{{/finding.evidence}}"""

# 与模板编辑器的新建草稿保持相同四章；预期正文在下方独立逐块给出。
TEMPLATE = """# {{ report_title }}

## 1. 漏洞成果汇总

{{ report_boundary }}

{{ executive_summary }}

漏洞记录数：{{ finding_count }}

{{ limitations }}

## 2. 漏洞详情

{{#findings}}
### {{ finding.title }}

| 漏洞 ID | 等级 | 记录状态 | 验证状态 | 目标 |
| --- | --- | --- | --- | --- |
| {{ finding.id }} | {{ finding.severity }} | {{ finding.status }} | {{ finding.verification_status }} | {{ finding.target }} |

#### 漏洞描述

{{ finding.description }}

{{ finding.impact }}

#### 已记录复现步骤与条件

{{ finding.recorded_steps }}

{{#finding.steps}}
**解释步骤 {{ step.number }}**

{{ step.explanation }}

{{ step.evidence_status }}
{{/finding.steps}}

#### 已记录交互原件

EVIDENCE_PLACEHOLDER

#### 修复建议

{{ finding.remediation }}

回归建议（未执行）：{{ finding.regression_steps }}

{{ finding.limitations }}
{{/findings}}

## 3. 整体修复建议

{{ overall_remediation }}

## 附录：证据索引

| 原件 | 类型 | SHA-256 |
| --- | --- | --- |
{{#artifacts}}
| {{ artifact.id }} | {{ artifact.kind }} | {{ artifact.sha256 }} |
{{/artifacts}}
""".replace("EVIDENCE_PLACEHOLDER", EVIDENCE_TEMPLATE)

RAW = {
    "a1": "GET /one HTTP/1.1\r\nHost: fixture.invalid\r\n\r\n",
    "a2": "HTTP/1.1 200 OK\r\n\r\n第一观察的响应\r\n",
    "a3": "POST /two HTTP/1.1\nHost: fixture.invalid\n\n第二观察的请求",
    "a4": "HTTP/1.1 403 Forbidden\n\n另一漏洞的响应",
}
BOUNDARY = (
    "项目已停止；本报告是冻结快照的部分报告，不代表评估完成或完整覆盖。"
    " 已测记录 1 项，阻塞 1 项，未测或未完成 1 项；未测不能当作安全。"
    " 模型解释、修复建议和回归方法不是新增测试证据，也不证明已修复。"
)
STEP_BOUNDARY = "报告阶段的步骤解释，不是新增执行记录；未记录步骤与原件的明确关联。"
ASSOCIATION = "已记录交互原件（未记录步骤关联）"
MISSING = "本条未记录此侧原件。"


def _finding(vulnerability_id: str) -> dict:
    suffix = "一" if vulnerability_id == "v1" else "二"
    return {
        "vulnerability_id": vulnerability_id,
        "observation_id": None,
        "description": f"可读说明{suffix}；影响仅限已有材料支持的范围。",
        "steps": [
            {"operation": f"解释{suffix}的第一步。", "observed": "已有记录中的观察，不是新执行。", "evidence": []},
            {"operation": f"解释{suffix}的第二步。", "observed": "未记录明确原件选段。", "evidence": []},
        ],
        "remediation": f"修复建议{suffix}；修复后回归尚未执行。",
        "limitations": f"本项解释限制{suffix}。",
    }


def _data(*ids: str) -> dict:
    return {
        "schema_version": 4,
        "executive_summary": "冻结材料的整体解释。",
        "overall_remediation": "整体修复建议，尚未实施。",
        "findings": [_finding(value) for value in ids],
        "limitations": "本报告未执行新的测试或修复回归。",
        "artifacts": [],
    }


def _http(item_id: str, title: str, request: str | None, response: str | None) -> dict:
    return {
        "id": item_id,
        "kind": "http",
        "role": "primary",
        "title": title,
        "source": "capture",
        "request": {"artifact_id": request, "pointer": ""} if request else None,
        "response": {"artifact_id": response, "pointer": ""} if response else None,
    }


def _packet(*, empty: bool = False) -> dict:
    packet = {
        "version": 2,
        "project": {"id": "p1", "title": "合成冻结评估", "status": "stopped"},
        "assessment_summary": {
            "execution": {},
            "coverage": {
                "tested": [{"fact_id": "ft"}],
                "blocked": [{"reason": "缺少第二身份"}],
                "untested": [{"fact_id": None}],
            },
        },
        "vulnerabilities": [
            {
                "id": "v1", "title": "同名漏洞", "severity": "high", "status": "open",
                "target": "/one", "source_fact_id": "f1",
                "repro_steps": "原始步骤一：读取对象\n原始步骤二：比较身份",
                "record_evidence_items": [],
            },
            {
                "id": "v2", "title": "同名漏洞", "severity": "medium", "status": "open",
                "target": "/two", "source_fact_id": "f2", "repro_steps": "第二漏洞的原始条件",
                "record_evidence_items": [],
            },
        ],
        "facts": [
            {"id": "f1", "description": "第一漏洞的来源描述。"},
            {"id": "f2", "description": "第二漏洞的来源描述。"},
        ],
        "observations": [
            {
                "id": "o1", "vulnerability_id": "v1", "task_run_id": "r1",
                "evidence_items": [_http("e1", "第一观察交互", "a1", "a2")],
            },
            {
                "id": "o2", "vulnerability_id": "v1", "task_run_id": "r2",
                "evidence_items": [_http("e2", "第二观察交互", "a3", None)],
            },
            {
                "id": "o3", "vulnerability_id": "v2", "task_run_id": "r3",
                "evidence_items": [_http("e3", "第二观察交互", None, "a4")],
            },
        ],
        "observation_statuses": [
            {"id": "o1", "vulnerability_id": "v1", "verification_status": "conflicting"},
            {"id": "o2", "vulnerability_id": "v1", "verification_status": "pending"},
            {"id": "o3", "vulnerability_id": "v2", "verification_status": "not_run"},
        ],
        "artifacts": [
            {
                "id": artifact_id, "project_id": "p1", "task_run_id": run_id, "kind": "file",
                "size": len(text.encode("utf-8")), "sha256": sha256(text.encode("utf-8")).hexdigest(),
            }
            for (artifact_id, text), run_id in zip(RAW.items(), ("r1", "r1", "r2", "r3"), strict=True)
        ],
        "artifact_contents": {artifact_id: {"text": text} for artifact_id, text in RAW.items()},
    }
    if empty:
        for key in ("vulnerabilities", "facts", "observations", "observation_statuses", "artifacts"):
            packet[key] = []
        packet["artifact_contents"] = {}
    return packet


def _expected_evidence(title: str, request: str, response: str, *, request_fence: str = "```") -> str:
    return (
        f"\n**{title}**\n{ASSOCIATION}\n\n请求\n{request_fence}http\n{request}\n{request_fence}"
        f"\n\n响应\n```http\n{response}\n```\n"
    )


def _expected_finding(suffix: str, *, evidence: str) -> str:
    if suffix == "一":
        identity = "| v1 | high | open | o1: conflicting; o2: pending | /one |"
        recorded = "原始步骤一：读取对象<br>原始步骤二：比较身份"
    else:
        identity = "| v2 | medium | open | o3: not\\_run | /two |"
        recorded = "第二漏洞的原始条件"
    return f"""
### 同名漏洞

| 漏洞 ID | 等级 | 记录状态 | 验证状态 | 目标 |
| --- | --- | --- | --- | --- |
{identity}

#### 漏洞描述

第{suffix}漏洞的来源描述。

影响解释{suffix}。

#### 已记录复现步骤与条件

{recorded}


**解释步骤 1**

解释{suffix}的第一步。

{STEP_BOUNDARY}

**解释步骤 2**

解释{suffix}的第二步。

{STEP_BOUNDARY}


#### 已记录交互原件

{evidence}

#### 修复建议

修复建议{suffix}。

回归建议（未执行）：回归建议{suffix}。

本项解释限制{suffix}。
"""


def _expected_markdown(packet: dict) -> str:
    first_evidence = (
        _expected_evidence("第一观察交互", RAW["a1"], RAW["a2"])
        + _expected_evidence("第二观察交互", RAW["a3"], MISSING)
    )
    first = _expected_finding("一", evidence=first_evidence)
    second = _expected_finding("二", evidence=_expected_evidence("第二观察交互", MISSING, RAW["a4"]))
    artifact_rows = "".join(f"\n| {item['id']} | file | {item['sha256']} |\n" for item in packet["artifacts"])
    return f"""# 合成冻结评估 · 安全评估报告

## 1. 漏洞成果汇总

{BOUNDARY}

冻结材料的整体解释。

漏洞记录数：2

本报告未执行新的测试或修复回归。

## 2. 漏洞详情

{first}{second}

## 3. 整体修复建议

整体修复建议，尚未实施。

## 附录：证据索引

| 原件 | 类型 | SHA-256 |
| --- | --- | --- |
{artifact_rows}
"""


def _render(packet: dict | None = None, data: dict | None = None, *, template: str = TEMPLATE) -> str:
    return report._render_report_markdown(
        data if data is not None else _data("v1", "v2"),
        report_packet_json=json.dumps(packet if packet is not None else _packet(), ensure_ascii=False),
        report_template_markdown=template,
    )


class _ArtifactOnlyClient:
    def __init__(self, status_code: int = 201) -> None:
        self.status_code = status_code
        self.artifacts: list[dict] = []

    def create_artifact(self, project_id: str, **kwargs) -> ApiResult:
        assert project_id == "p1"
        self.artifacts.append(kwargs)
        return ApiResult(
            status_code=self.status_code,
            data={"id": "report-artifact", "sha256": sha256(kwargs["content"].encode("utf-8")).hexdigest()},
        )

    def update_vulnerability(self, *args, **kwargs):
        pytest.fail("报告不可更新 canonical 漏洞")

    def get_project(self, *args, **kwargs):
        pytest.fail("渲染不可重新读取实时项目状态")

    def list_vulnerabilities(self, *args, **kwargs):
        pytest.fail("渲染不可重新读取实时漏洞")


def test_full_template_output_preserves_order_text_and_observation_ownership() -> None:
    packet = _packet()
    before = deepcopy(packet)
    # 模型返回顺序不能取代模板按冻结漏洞顺序展开的职责。
    markdown = _render(packet, _data("v2", "v1"))
    assert markdown == _expected_markdown(packet)
    assert packet == before
    first, second = markdown.split("### 同名漏洞")[1:]
    assert RAW["a1"] in first and RAW["a2"] in first and RAW["a3"] in first
    assert RAW["a4"] not in first and RAW["a1"] not in second
    assert _expected_evidence("第二观察交互", RAW["a3"], MISSING) in first
    assert _expected_evidence("第二观察交互", MISSING, RAW["a4"]) in second
    assert "GET /example" not in markdown and "漏洞一示例" not in markdown


def test_template_controls_inner_labels_lists_and_interaction_headings() -> None:
    packet = _packet()
    original = "#### 漏洞描述\n\n{{ finding.description }}\n\n{{ finding.impact }}"
    replacement = "#### 简述\n\n- {{ finding.impact }}\n- {{ finding.description }}"
    template = TEMPLATE.replace(original, replacement).replace("**{{ evidence.title }}**", "##### 交互：{{ evidence.title }}")
    expected = _expected_markdown(packet)
    for suffix in ("一", "二"):
        expected = expected.replace(
            f"#### 漏洞描述\n\n第{suffix}漏洞的来源描述。\n\n影响解释{suffix}。",
            f"#### 简述\n\n- 影响解释{suffix}。\n- 第{suffix}漏洞的来源描述。",
        )
    for title in ("第一观察交互", "第二观察交互"):
        expected = expected.replace(f"**{title}**", f"##### 交互：{title}")
    assert _render(packet, template=template) == expected


def test_zero_findings_keeps_four_chapters_and_honest_partial_boundary() -> None:
    packet = _packet(empty=True)
    expected = f"""# 合成冻结评估 · 安全评估报告

## 1. 漏洞成果汇总

{BOUNDARY}

冻结材料的整体解释。

漏洞记录数：0

本报告未执行新的测试或修复回归。

## 2. 漏洞详情

冻结快照没有漏洞记录；不代表目标不存在漏洞。


## 3. 整体修复建议

整体修复建议，尚未实施。

## 附录：证据索引

| 原件 | 类型 | SHA-256 |
| --- | --- | --- |
未记录证据索引。

"""
    assert _render(packet, _data()) == expected


def test_model_headings_tables_comments_and_fences_cannot_escape_text_slots() -> None:
    data = _data("v1", "v2")
    payload = "# 伪造标题\n|列|列|\n```http\nGET /fake\n```\n<!--"
    escaped = r"\# 伪造标题<br>\|列\|列\|<br>\`\`\`http<br>GET /fake<br>\`\`\`<br>&lt;\!\-\-"
    data["executive_summary"] = payload
    expected = _expected_markdown(_packet()).replace("冻结材料的整体解释。", escaped)
    assert _render(data=data) == expected
    data["findings"][0]["steps"][0]["explanation"] = payload
    expected = expected.replace("解释一的第一步。", escaped)
    assert _render(data=data) == expected


def test_long_raw_fences_and_template_tokens_preserve_exact_original_text() -> None:
    packet = _packet()
    original = "POST /long HTTP/1.1\r\n\r\n`````\n{{ finding.title }}\n## 原件内标题\n" + "长正文" * 25000
    packet["artifact_contents"]["a3"] = {"text": original}
    artifact = packet["artifacts"][2]
    artifact.update(size=len(original.encode("utf-8")), sha256=sha256(original.encode("utf-8")).hexdigest())
    before = deepcopy(packet)
    expected = _expected_markdown(packet).replace(
        _expected_evidence("第二观察交互", RAW["a3"], MISSING),
        _expected_evidence("第二观察交互", original, MISSING, request_fence="``````"),
    )
    markdown = _render(packet)
    assert markdown == expected
    assert f"``````http\n{original}\n``````" in markdown
    assert packet == before
    assert sha256(original.encode("utf-8")).hexdigest() == artifact["sha256"]


def test_legacy_inline_http_is_not_promoted_to_captured_originals() -> None:
    packet = _packet()
    item = packet["observations"][1]["evidence_items"][0]
    item.update(request_text="GET /legacy HTTP/1.1", response_text="HTTP/1.1 299 Legacy")
    expected = _expected_markdown(packet).replace(
        _expected_evidence("第二观察交互", RAW["a3"], MISSING),
        _expected_evidence("第二观察交互", RAW["a3"], MISSING).replace(
            ASSOCIATION,
            ASSOCIATION + "；历史内联材料不是采集原件，未作为原包内嵌",
        ),
    )
    markdown = _render(packet)
    assert markdown == expected
    assert "GET /legacy" not in markdown and "299 Legacy" not in markdown


def test_unavailable_and_pointer_originals_are_not_filled_from_other_observations() -> None:
    packet = _packet()
    packet["artifact_contents"]["a1"] = {"unavailable": "原件不允许内嵌交付。"}
    packet["observations"][0]["evidence_items"][0]["response"]["pointer"] = "/response"
    expected = (
        _expected_markdown(packet)
        .replace(RAW["a1"], "原件不允许内嵌交付。")
        .replace(RAW["a2"], "此引用只记录了原件内指针，未记录可独立还原的报文；请按证据索引查看原件。")
    )
    assert _render(packet) == expected


@pytest.mark.parametrize(("ids", "error"), [
    (("v1",), "report_vulnerability_missing:v2"),
    (("v1", "v1", "v2"), "report_vulnerability_duplicate:v1"),
    (("v1", "v2", "unknown"), "report_vulnerability_unknown:unknown"),
    (("同名漏洞", "v2"), "report_vulnerability_unknown:同名漏洞"),
])
def test_findings_require_exact_frozen_id_set(ids, error) -> None:
    assert report._report_completeness_error(_data(*ids), id_to_title={"v1": "同名漏洞", "v2": "同名漏洞"}) == error
    with pytest.raises(ValueError, match=error):
        _render(data=_data(*ids))


@pytest.mark.parametrize("field", [
    "title", "repro_request", "repro_response", "verification_status", "evidence_refs", "severity", "repro_steps",
])
def test_model_cannot_submit_originals_fact_updates_or_old_repro_field(field: str) -> None:
    data = _data("v1", "v2")
    data["findings"][0][field] = "fabricated"
    assert report._report_completeness_error(data, id_to_title={"v1": "", "v2": ""}) == "report_data_invalid"


@pytest.mark.parametrize("mutation", ["layout", "old_version", "missing_overall", "step_evidence", "empty_summary"])
def test_old_layout_and_invented_step_associations_are_rejected(mutation: str) -> None:
    data = _data("v1", "v2")
    if mutation == "layout":
        data["layout"] = [{"type": "markdown", "markdown": "# 自定整篇报告"}]
    elif mutation == "old_version":
        data["schema_version"] = 2
    elif mutation == "missing_overall":
        data.pop("overall_remediation")
    elif mutation == "step_evidence":
        data["findings"][0]["steps"][0]["evidence_item_ids"] = ["e1"]
    else:
        data["executive_summary"] = " \n "
    assert report._report_completeness_error(data, id_to_title={"v1": "", "v2": ""}) == "report_data_invalid"


@pytest.mark.parametrize("output_contract_version", [2, 3, 4])
def test_frozen_plan_rejects_old_output_contract_without_translation(output_contract_version: int) -> None:
    prompt = "只解释冻结证据，不执行测试。"
    prompt_digest = sha256(prompt.encode("utf-8")).hexdigest()
    plan = {
        "version": 2, "execution_contract_version": 1, "execution_engine": "pi",
        "report_policy_version": 1, "packet_version": 2,
        "output_contract_version": output_contract_version, "report_data_schema_version": 4, "completeness_rule_version": 1,
        "report_prompt_name": "report.md", "report_prompt_group": "goal-driven-v1",
        "report_prompt_digest_sha256": prompt_digest,
    }
    canonical = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    expected = None if output_contract_version == 4 else "report_execution_plan_unsupported:output_contract_version"
    assert report._frozen_execution_error(
        canonical, plan, sha256(canonical.encode("utf-8")).hexdigest(), prompt, prompt_digest,
    ) == expected


def test_dynamic_submit_schema_exposes_explanations_not_layout_or_raw_packets() -> None:
    schema = build_submit_result_schema("report_execute")
    properties = schema["properties"]["data"]["properties"]
    assert properties["schema_version"]["const"] == 4
    assert "overall_remediation" in properties and "layout" not in properties
    finding = schema["$defs"]["ReportFinding"]["properties"]
    assert "steps" in finding and "repro_steps" not in finding
    assert "repro_request" not in finding and "repro_response" not in finding
    assert set(schema["$defs"]["ReportStep"]["properties"]) == {"operation", "observed", "evidence"}


@pytest.mark.parametrize("template", [
    "# {{ report_title }}\n\n## 漏洞详情\n漏洞一示例\n```http\nGET /example HTTP/1.1\n```\n",
    TEMPLATE.replace("{{ finding.description }}", "固定示例描述"),
    TEMPLATE.replace("{{/finding.evidence}}", "{{/findings}}"),
    TEMPLATE.replace("{{ finding.title }}", "{{ finding.unknown }}"),
    TEMPLATE.replace("```http\n{{ evidence.request }}\n```", "{{ evidence.request }}"),
    TEMPLATE + "\n<!-- 未闭合",
])
def test_invalid_templates_fail_without_copying_examples_or_using_old_layout(template: str) -> None:
    with pytest.raises(ValueError):
        parse_report_template(template)
    client = _ArtifactOnlyClient()
    assert report._write_report_results(
        client, "p1", "report-run", data=_data("v1", "v2"),
        report_packet_json=json.dumps(_packet()), report_template_markdown=template,
    ) == "request_contract"
    assert not client.artifacts


@pytest.mark.parametrize("mutation", ["digest", "size", "owner"])
def test_changed_or_cross_project_artifact_cannot_become_report_evidence(mutation: str) -> None:
    packet = _packet()
    if mutation == "digest":
        packet["artifact_contents"]["a1"]["text"] += " changed"
    elif mutation == "size":
        packet["artifacts"][0]["size"] += 1
    else:
        packet["artifacts"][0]["project_id"] = "other-project"
    client = _ArtifactOnlyClient()
    assert report._write_report_results(
        client, "p1", "report-run", data=_data("v1", "v2"),
        report_packet_json=json.dumps(packet), report_template_markdown=TEMPLATE,
    ) == "request_contract"
    assert not client.artifacts


def test_exact_markdown_and_new_contract_metadata_are_registered_once() -> None:
    packet = _packet()
    packet_json = json.dumps(packet)
    metadata = {
        "report_generation_token": "generation-2", "report_template_key": "standard-pentest",
        "report_template_digest_sha256": sha256(TEMPLATE.encode()).hexdigest(), "report_policy_version": 1,
        "report_execution_plan_sha256": "plan-digest", "report_prompt_digest_sha256": "prompt-digest",
        "completeness_rule_version": 1, "input_snapshot_artifact_id": "snapshot-1",
        "input_snapshot_sha256": sha256(packet_json.encode()).hexdigest(),
    }
    client = _ArtifactOnlyClient()
    identity = report._write_report_results(
        client, "p1", "report-run", data=_data("v1", "v2"),
        report_packet_json=packet_json, report_template_markdown=TEMPLATE, **metadata,
    )
    expected = _expected_markdown(packet)
    assert identity == report.ReportArtifactIdentity("report-artifact", sha256(expected.encode("utf-8")).hexdigest())
    assert client.artifacts == [{
        "task_run_id": "report-run", "kind": "report", "content": expected,
        "filename": report.REPORT_ARTIFACT_FILENAME, "category": "deliverable",
        "metadata": {"source": "report_task", "format": "markdown", "packet_version": 2,
                     "output_contract_version": 4, "report_data_schema_version": 3, **metadata},
    }]


@pytest.mark.parametrize("receipt", ["missing_id", "missing_digest", "wrong_digest"])
def test_artifact_acceptance_requires_identity_and_exact_digest(receipt: str) -> None:
    class InvalidReceiptClient(_ArtifactOnlyClient):
        def create_artifact(self, project_id: str, **kwargs) -> ApiResult:
            accepted = super().create_artifact(project_id, **kwargs)
            returned = dict(accepted.data)
            if receipt == "missing_id":
                returned.pop("id")
            elif receipt == "missing_digest":
                returned.pop("sha256")
            else:
                returned["sha256"] = "0" * 64
            return ApiResult(status_code=201, data=returned)

    client = InvalidReceiptClient()
    result = report._write_report_results(
        client, "p1", "report-run", data=_data(),
        report_packet_json=json.dumps(_packet(empty=True)), report_template_markdown=TEMPLATE,
    )
    assert result == "business_acceptance"
    assert len(client.artifacts) == 1


def test_failed_artifact_write_is_not_report_success() -> None:
    client = _ArtifactOnlyClient(status_code=503)
    assert report._write_report_results(
        client, "p1", "report-run", data=_data(),
        report_packet_json=json.dumps(_packet(empty=True)), report_template_markdown=TEMPLATE,
    ) == "server_write_failed"


@pytest.mark.parametrize("mutation", [
    "old_version", "wrong_project", "missing_summary", "missing_coverage", "missing_contents",
    "missing_referenced_content", "duplicate_vulnerability", "missing_status", "wrong_status_owner",
])
def test_invalid_frozen_packet_cannot_become_zero_finding_success(mutation: str) -> None:
    packet = _packet()
    if mutation == "old_version":
        packet["version"] = 1
    elif mutation == "wrong_project":
        packet["project"]["id"] = "another-project"
    elif mutation == "missing_summary":
        packet.pop("assessment_summary")
    elif mutation == "missing_coverage":
        packet["assessment_summary"]["coverage"].pop("untested")
    elif mutation == "missing_contents":
        packet.pop("artifact_contents")
    elif mutation == "missing_referenced_content":
        packet["artifact_contents"].pop("a1")
    elif mutation == "duplicate_vulnerability":
        packet["vulnerabilities"].append(packet["vulnerabilities"][0])
    elif mutation == "missing_status":
        packet["observation_statuses"].pop()
    else:
        packet["observation_statuses"][0]["vulnerability_id"] = "v2"
    client = _ArtifactOnlyClient()
    assert report._write_report_results(
        client, "p1", "report-run", data=_data("v1", "v2"),
        report_packet_json=json.dumps(packet), report_template_markdown=TEMPLATE,
    ) == "request_contract"
    assert not client.artifacts
