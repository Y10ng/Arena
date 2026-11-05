"""受控整理结果的真实字节选段与消费路径；不调用模型或业务数据库。"""

from copy import deepcopy
from hashlib import sha256
import json

import pytest

from cairn.dispatcher.tasks import report
from cairn.dispatcher.worker_protocol.submission import ReportData, build_submit_result_schema
from cairn.report_readable import readable_finding_context, validate_report_readable
from cairn.report_template_contract import render_report_template


TEMPLATE = """{{#findings}}
# {{ finding.title }}

## 漏洞说明
{{ finding.description }}

## 复现步骤及关键数据包
{{ finding.recorded_steps }}

{{ finding.key_evidence }}

## 修复建议
{{ finding.remediation }}

{{ finding.limitations }}
{{/findings}}"""


def sample():
    request = "GET /records/other HTTP/1.1\r\nHost: fixture.invalid\r\n\r\n"
    response = "HTTP/1.1 200 OK\r\n\r\n" + "irrelevant-prefix\n" * 8000 + "尾部关键结果：跨用户记录\n"
    trace = "supporting trace does not prove a complete test"
    raw = {"request": request, "response": response, "trace": trace, "other": "HTTP/1.1 403 Forbidden\n\n反证"}
    artifacts = [{"id": key, "project_id": "p1", "kind": "file", "size": len(value.encode()), "sha256": sha256(value.encode()).hexdigest()}
                 for key, value in raw.items()]
    item = {"id": "interaction", "kind": "http", "role": "primary", "source": "capture", "title": "记录读取",
            "request": {"artifact_id": "request", "pointer": "free-form-handle"}, "response": {"artifact_id": "response"}}
    packet = {
        "version": 2, "project": {"id": "p1", "title": "受控项目", "status": "stopped"},
        "report_scope": {"vulnerability_id": "v1", "observation_id": "o1"},
        "assessment_summary": {"execution": {}, "coverage": {"tested": [], "blocked": [], "untested": []}},
        "vulnerabilities": [{"id": "v1", "title": "记录读取缺少授权", "status": "open", "severity": "high", "target": "/records",
                              "record_evidence_items": [], "dispositions": [], "source_fact_id": "f1", "repro_steps": "旧步骤不作为本次说明"}],
        "observations": [
            {"id": "o1", "vulnerability_id": "v1", "evidence_items": [item, {"id": "trace", "kind": "artifact", "role": "supporting", "source": "capture", "artifact": {"artifact_id": "trace"}}]},
        ],
        "observation_statuses": [{"id": "o1", "vulnerability_id": "v1", "verification_status": "confirmed"}],
        "artifacts": artifacts, "artifact_contents": {key: {"text": value} for key, value in raw.items()},
        "facts": [{"id": "f1", "description": "Fact日志材料墙不进入新说明"}],
    }
    tail_start = response.encode().index("尾部关键结果".encode())
    refs = [{"side": "request", "artifact_id": "request", "sha256": artifacts[0]["sha256"], "byte_start": 0, "byte_end": len(request.encode())},
            {"side": "response", "artifact_id": "response", "sha256": artifacts[1]["sha256"], "byte_start": tail_start, "byte_end": len(response.encode())}]
    finding = {"vulnerability_id": "v1", "observation_id": "o1", "description": "记录读取未校验对象归属。\n\n已有响应支持**跨用户读取**，范围受本次材料限制。",
               "steps": [{"operation": "以现有身份读取另一个用户的记录。", "observed": "响应尾部出现跨用户记录。",
                          "evidence": [{"evidence_item_id": "interaction", "selections": refs}]}],
               "remediation": "- 在服务端校验对象与当前身份的归属。\n- 修复后重测正常与跨用户对照；尚未执行。",
               "limitations": "另一次观察未复现，不能声称所有条件均成立。"}
    data = {"schema_version": 4, "executive_summary": "仅整理已有记录。", "overall_remediation": "优先校验对象归属。", "findings": [finding], "limitations": "未进行新增测试。"}
    return packet, data


def test_long_tail_selection_is_exact_and_original_sha_unchanged():
    packet, data = sample()
    original = deepcopy(packet)
    result = validate_report_readable(data, packet)
    context = readable_finding_context(result.findings[0], packet)
    markdown = render_report_template(TEMPLATE, {"findings": [{"finding.title": "受控漏洞", **context}]})
    assert "尾部关键结果：跨用户记录" in markdown
    assert "GET /records/other HTTP/1.1\r\n" in markdown
    assert "irrelevant-prefix" not in markdown and "supporting trace" not in markdown
    assert "Fact日志材料墙" not in markdown and "旧步骤" not in markdown
    assert "关键节选；其余内容省略" in markdown
    assert "**跨用户读取**" in markdown and "\n\n已有响应" in markdown
    assert "另一次观察未复现" in markdown
    assert all(token not in markdown for token in ("来源定位", "Observation o1", "Evidence interaction", "Artifact request", "utf8-bytes=", "SHA-256", "登录 Arena"))
    assert packet == original
    for artifact in packet["artifacts"]:
        raw = packet["artifact_contents"][artifact["id"]]["text"].encode()
        assert len(raw) == artifact["size"] and sha256(raw).hexdigest() == artifact["sha256"]


@pytest.mark.parametrize("mutation,error", [
    ("observation", "scope_mismatch"), ("cross_side", "interaction_mismatch"),
    ("cross_item", "evidence_observation_mismatch"), ("digest", "digest_mismatch"),
    ("source_digest", "digest_mismatch"), ("owner", "owner_mismatch"),
    ("bounds", "out_of_bounds"), ("utf8_start", "utf8_boundary"), ("utf8_end", "utf8_boundary"),
])
def test_rejects_invalid_provenance_and_ranges(mutation, error):
    packet, data = sample()
    finding = data["findings"][0]
    evidence = finding["steps"][0]["evidence"][0]
    selection = evidence["selections"][1]
    if mutation == "observation":
        finding["observation_id"] = "o2"
    elif mutation == "cross_side":
        selection.update(artifact_id="other", sha256=packet["artifacts"][3]["sha256"])
    elif mutation == "cross_item":
        evidence["evidence_item_id"] = "other-interaction"
    elif mutation == "digest":
        selection["sha256"] = "0" * 64
    elif mutation == "source_digest":
        packet["artifact_contents"]["response"]["text"] += "changed"
    elif mutation == "owner":
        packet["artifacts"][1]["project_id"] = "p2"
    elif mutation == "bounds":
        selection["byte_end"] += 1
    elif mutation == "utf8_start":
        selection["byte_start"] += 1
    elif mutation == "utf8_end":
        selection["byte_end"] -= 2
    with pytest.raises(ValueError, match=error):
        validate_report_readable(data, packet)


@pytest.mark.parametrize("body,encoding,mode", [
    ("正文：中文结果".encode(), None, "utf8"), (b"\x00\x01\xff", None, "non-utf8"),
    (b"binary\x00value", None, "binary"), (b"compressed bytes", b"gzip", "compressed"),
])
def test_flow_projection_is_bounded_stable_and_preserves_original(tmp_path, monkeypatch, body, encoding, mode):
    import base64
    from fastapi import HTTPException
    from cairn.server.services import flow_evidence as flow

    path = tmp_path / "body.bin"
    path.write_bytes(body)
    artifact = {"id": "body", "sha256": sha256(body).hexdigest(), "size": len(body)}
    monkeypatch.setattr(flow, "_artifact", lambda *args: artifact)
    monkeypatch.setattr(flow, "_artifact_disk_path_from_row", lambda row: path)
    encode = lambda value: base64.b64encode(value).decode("ascii")
    headers = [[encode(b"X-Repeat"), encode(value)] for value in (b"one", "二".encode())]
    if encoding:
        headers.append([encode(b"Content-Encoding"), encode(encoding)])
    message = {"http_version": "HTTP/2.0", "status_code": 200, "reason_base64": encode(b"OK"), "headers_base64": headers}
    projection = flow._projection(None, "p1", message, "response", {"artifact_id": "body", **artifact})
    snapshot = {"flow_id": "flow1", "project_id": "p1", "scope": {"task_run_id": "r1"}, "revision": 3,
                "state": "partial", "representation": flow.REPRESENTATION, "record": {"response": message},
                "projections": {"request": None, "response": projection}}
    assert projection["body_display"] == mode
    whole = flow.read_frozen_flow_projection(None, snapshot, "response")
    chunks, offset = [], 0
    while offset < whole["size"]:
        part = flow.read_frozen_flow_projection(None, snapshot, "response", offset, 7)
        chunks.append(part["text"])
        assert part["offset"] == offset and part["next_offset"] > offset
        offset = part["next_offset"]
    assert "".join(chunks) == whole["text"]
    assert "X-Repeat: one\r\nX-Repeat: 二\r\n" in whole["text"]
    assert sha256(whole["text"].encode()).hexdigest() == projection["sha256"]
    assert path.read_bytes() == body
    excerpt = {"side": "response", "representation": flow.REPRESENTATION, "revision": 3,
               "sha256": projection["sha256"], "pointer": f"utf8-bytes=0:{whole['size']}"}
    if mode == "utf8":
        flow.validate_flow_excerpt(None, snapshot, excerpt)
        bad = {**excerpt, "pointer": f"utf8-bytes={projection['header_size'] + 1}:{whole['size']}"}
        with pytest.raises(HTTPException) as error:
            flow.validate_flow_excerpt(None, snapshot, bad)
        assert error.value.detail == "evidence_excerpt_utf8_boundary"
    else:
        with pytest.raises(HTTPException) as error:
            flow.validate_flow_excerpt(None, snapshot, excerpt)
        assert error.value.detail == "evidence_excerpt_body_not_text"
    with pytest.raises(HTTPException) as error:
        flow.read_frozen_flow_projection(None, snapshot, "request")
    assert error.value.status_code == 404


def test_frozen_flow_report_selection_and_readonly_input_never_require_live_flow():
    packet, data = sample()
    raw = "HTTP/1.1 200 OK\r\nX-Repeat: one\r\nX-Repeat: 二\r\n\r\n冻结结果".encode()
    digest = sha256(raw).hexdigest()
    projection = {"side": "response", "representation": "http-evidence-v1", "sha256": digest, "size": len(raw),
                  "header_size": raw.index(b"\r\n\r\n") + 4, "body_display": "utf8", "body_artifact_id": "body", "body_sha256": "b" * 64}
    frozen = {"flow_id": "flow1", "project_id": "p1", "revision": 4, "state": "partial", "representation": "http-evidence-v1",
              "record": {"sensitive_structured_metadata": "not in working index"}, "projections": {"request": None, "response": projection}}
    packet["flow_evidence"] = {"flow1": frozen}
    packet["flow_contents"] = {"flow1": {"request": {"unavailable": "本交互未记录此侧材料。"}, "response": {
        **projection, "revision": 4, "offset": 0, "next_offset": len(raw), "range_sha256": digest, "text": raw.decode(),
    }}}
    item = packet["observations"][0]["evidence_items"][0]
    item.pop("request")
    item.pop("response")
    item["flow_id"] = "flow1"
    selection = {"side": "response", "flow_id": "flow1", "representation": "http-evidence-v1", "revision": 4,
                 "sha256": digest, "byte_start": 0, "byte_end": len(raw)}
    data["findings"][0]["steps"][0]["evidence"][0]["selections"] = [selection]
    context = readable_finding_context(data["findings"][0], packet)
    assert "冻结结果" in context["finding.key_evidence"] and "Flow flow1" not in context["finding.key_evidence"]
    assert "结构化重建" in context["finding.key_evidence"]
    assert all(token not in context["finding.key_evidence"] for token in (digest, "revision", "utf8-bytes=", "http-evidence-v1"))
    _, _, files = report._readable_input_references(json.dumps(packet), "frozen graph")
    working = json.loads(files[0].content)
    assert "flow_contents" not in working and "冻结结果" not in files[0].content
    assert "record" not in working["flow_evidence"]["flow1"]
    full_index = json.loads(next(file.content for file in files if file.path == working["frozen_materials_path"]))
    assert "record" not in full_index["flow_evidence"]["flow1"]
    assert packet["flow_evidence"]["flow1"]["record"] == frozen["record"]
    path = working["flow_reads"]["flow1"]["response"]["ranges"][0]["path"]
    assert next(file.content for file in files if file.path == path) == raw.decode()
    packet["flow_contents"]["flow1"]["response"]["text"] = raw.decode().replace("冻结", "改变")
    with pytest.raises(ValueError, match="digest_mismatch"):
        validate_report_readable(data, packet)


def test_model_schema_rejects_raw_http_and_only_allows_explanation_and_selections():
    _, data = sample()
    ReportData.model_validate(data)
    schema = build_submit_result_schema("report_execute")
    assert schema["properties"]["data"]["properties"]["schema_version"]["const"] == 4
    data["findings"][0]["steps"][0]["evidence"][0]["request_text"] = "GET /invented HTTP/1.1"
    with pytest.raises(ValueError, match="extra_forbidden"):
        ReportData.model_validate(data)


def test_single_output_is_structured_json_and_repeated_reading_does_not_run_model():
    packet, data = sample()
    prepared = report._prepare_report_output("p1", data=data, report_packet_json=json.dumps(packet), report_template_markdown=TEMPLATE)
    assert isinstance(prepared, tuple)
    content, metadata = prepared
    assert metadata["format"] == "json" and metadata["report_data_schema_version"] == 4
    assert json.loads(content) == data
    for _ in range(2):
        result = validate_report_readable(content.encode(), packet)
        assert "尾部关键结果" in readable_finding_context(result.findings[0], packet)["finding.key_evidence"]


def test_single_scope_does_not_require_whole_project_summary_or_statuses():
    packet, data = sample()
    packet["assessment_summary"] = {}
    packet["observation_statuses"] = []
    assert isinstance(report._prepare_report_output("p1", data=data, report_packet_json=json.dumps(packet), report_template_markdown=TEMPLATE), tuple)
    packet["observations"].append({"id": "another", "vulnerability_id": "v1", "evidence_items": []})
    assert report._prepare_report_output("p1", data=data, report_packet_json=json.dumps(packet), report_template_markdown=TEMPLATE) == "request_contract"


def test_structured_output_uses_exact_durable_artifact_filename_and_replay():
    from cairn.dispatcher.protocol.client import ApiResult
    from cairn.dispatcher.tasks.report_finish_outbox import pending_report_execution, pending_report_output

    packet, data = sample()
    execution = pending_report_execution(project_id="p1", authenticated_dispatcher_id="d1", worker="w1", claim_sha256="c" * 64,
                                         generation_token="g1", task_run_id="r1", input_snapshot_artifact_id="s1", input_snapshot_sha256="d" * 64)
    content, metadata = report._prepare_report_output("p1", data=data, report_packet_json=json.dumps(packet), report_template_markdown=TEMPLATE,
                                                     report_generation_token="g1", input_snapshot_artifact_id="s1", input_snapshot_sha256="d" * 64)
    output = pending_report_output(execution, content=content, metadata=metadata)
    assert output.filename == "vulnerability-readable-report.json"
    assert output.metadata.format == "json"
    requests = []

    class Client:
        def create_artifact(self, project_id, **kwargs):
            requests.append((project_id, kwargs))
            return ApiResult(status_code=201, data={"id": "accepted", "sha256": output.artifact_sha256})

    assert output.replay(Client()).ok
    assert output.replay(Client()).ok
    assert requests[0] == requests[1]
    assert requests[0][1]["content"] == content
    assert requests[0][1]["filename"] == "vulnerability-readable-report.json"


def test_overall_consumes_model_explanation_and_freezes_accepted_content():
    packet, data = sample()
    packet["report_scope"] = None
    generated = report._render_report_markdown(data, report_packet_json=json.dumps(packet), report_template_markdown=TEMPLATE)
    assert "记录读取未校验对象归属" in generated and "尾部关键结果" in generated
    assert "Fact日志材料墙" not in generated
    packet["accepted_readable_findings"] = deepcopy(data["findings"])
    model_data = {**data, "findings": []}
    assert report._render_report_markdown(model_data, report_packet_json=json.dumps(packet), report_template_markdown=TEMPLATE) == generated
    with pytest.raises(ValueError, match="vulnerability_unknown"):
        report._render_report_markdown(data, report_packet_json=json.dumps(packet), report_template_markdown=TEMPLATE)


def test_optional_report_images_publish_or_fail_without_changing_original_packet(monkeypatch):
    from contextlib import contextmanager
    from io import BytesIO
    from types import SimpleNamespace
    from cairn.dispatcher.tasks import report_images

    packet, data = sample()
    packet["report_scope"] = None
    packet["http_evidence_render_sources"] = {"artifact:request:request": {
        "source": {"kind": "artifact", "project_id": "p1", "artifact_id": "request"},
        "request_text": packet["artifact_contents"]["request"]["text"], "response_text": None, "notice": "受控来源",
    }}
    before = deepcopy(packet)
    png = b"non-sensitive-controlled-png-placeholder"
    manifest_path = "/tmp/cairn-prompts/pi-http-evidence-test.json"
    image_path = "/tmp/cairn-prompts/http-evidence-test/1.png"
    manifest = {"version": 1, "images": [{"source_key": "artifact:request:request", "path": image_path,
        "size": len(png), "sha256": sha256(png).hexdigest(), "page": 1, "page_count": 1, "truncated": False}]}
    @contextmanager
    def opening(*args):
        yield BytesIO(json.dumps(manifest).encode() if args[-1] == manifest_path else png)
    monkeypatch.setattr(report_images, "_open_container_artifact", opening)
    manager = SimpleNamespace(remove_file=lambda *args, **kwargs: None)
    for succeeds in (True, False):
        client = SimpleNamespace(publish_http_evidence_image=lambda *args: SimpleNamespace(
            ok=succeeds, data={"id": "image_one", "sha256": sha256(png).hexdigest()} if succeeds else None))
        images = report_images.capture_report_images(client, manager, "test", manifest_path=manifest_path,
            project_id="p1", task_run_id="run1", snapshot_artifact_id="snapshot1", packet=packet)
        generated = report._prepare_report_output("p1", data=data, report_packet_json=json.dumps(packet),
            report_template_markdown=TEMPLATE, display_images=images)
        assert isinstance(generated, tuple)
        markdown = generated[0]
        assert "尾部关键结果" in markdown and "修复建议" in markdown
        assert ("arena-image:image_one" in markdown) is succeeds
        if not succeeds:
            assert "图片未能生成或发布" in markdown
        assert packet == before
    manifest.update(images=[], unavailable=True)
    images = report_images.capture_report_images(client, manager, "test", manifest_path=manifest_path,
        project_id="p1", task_run_id="run1", snapshot_artifact_id="snapshot1", packet=packet)
    generated = report._prepare_report_output("p1", data=data, report_packet_json=json.dumps(packet),
        report_template_markdown=TEMPLATE, display_images=images)
    assert isinstance(generated, tuple)
    assert "图片未能生成或发布" in generated[0] and "arena-image:" not in generated[0]
    assert "尾部关键结果" in generated[0] and packet == before


def test_model_input_index_does_not_inline_full_originals_and_exposes_tail_addressing():
    packet, _ = sample()
    _, _, files = report._readable_input_references(json.dumps(packet), "frozen-graph")
    index = json.loads(files[0].content)
    assert "artifact_contents" not in index and "irrelevant-prefix" not in files[0].content
    response = index["artifact_reads"]["response"]
    original = next(item for item in files if item.path == response["path"])
    assert original.content == packet["artifact_contents"]["response"]["text"]
    assert response["size_bytes"] > 32768 and "尾部关键结果" in original.content
    assert all(item.mode == 0o444 and item.owner == "root" for item in files)


def test_model_markdown_preserves_tables_lists_and_code_without_links_or_html():
    packet, data = sample()
    data["findings"][0]["description"] = "第一段。\n\n**有限强调**\n- 正常列表\n  - 子项\n\n| 条件 | 结论 |\n| --- | --- |\n| 已测 | 限于样本 |\n\n# 伪标题\n```text\n示例逻辑：检查身份 < 资源归属\n```\n<script>fake</script>\n[伪链接](https://fixture.invalid)\n![外部图片](https://fixture.invalid/image)"
    context = readable_finding_context(data["findings"][0], packet)
    description = context["finding.description"]
    assert "\n\n**有限强调**\n- 正常列表\n  - 子项" in description
    assert "| --- | --- |" in description and "\n```text\n示例逻辑：检查身份 < 资源归属\n```" in description
    assert "\n# 伪标题" not in description
    assert "<script>" not in description and "[伪链接](" not in description and "![外部图片](" not in description


def test_accepted_read_api_only_delivers_same_readable_fields_not_internal_packet(monkeypatch):
    from cairn.server.services import assessment_reports, vulnerability_reports

    packet, data = sample()
    accepted = {"artifact_id": "r1", "generation_token": "g1", "input_sha256": "a" * 64, "stale": False,
                "data": data, "report_packet": packet}
    monkeypatch.setattr(assessment_reports, "get_accepted_readable_report", lambda *args: deepcopy(accepted))
    first = vulnerability_reports.read_accepted_vulnerability_report(None, "p1", "v1", "o1")
    second = vulnerability_reports.read_accepted_vulnerability_report(None, "p1", "v1", "o1")
    assert first == second
    assert set(first) == {"artifact_id", "generation_token", "input_sha256", "stale", "readable"}
    assert first["readable"]["observation_id"] == "o1"
    assert "尾部关键结果" in first["readable"]["key_evidence_markdown"]
    assert "irrelevant-prefix" not in json.dumps(first, ensure_ascii=False)


@pytest.mark.parametrize("preconditions,expected", [
    (["无特殊条件"], "无特殊条件"),
    (["需普通用户登录，并持有自己的对象编号"], "需普通用户登录，并持有自己的对象编号"),
    ([], "未记录"),
])
def test_report_conditions_use_recorded_fact_without_inference(preconditions, expected):
    packet, data = sample()
    packet["observations"][0]["source_fact_id"] = "f1"
    packet["facts"][0]["assessment_record"] = {"preconditions": preconditions}
    original = deepcopy(packet)
    context = readable_finding_context(data["findings"][0], packet)
    assert context["finding.description"].endswith("**成立条件：**" + expected)
    assert "另一次观察未复现" not in context["finding.description"]
    assert packet == original


def test_sensitive_selected_original_is_rejected_without_rewriting_source():
    packet, data = sample()
    text = "Authorization: Bearer synthetic-only-fixture-value"
    packet["artifact_contents"]["request"]["text"] = text
    artifact = packet["artifacts"][0]
    artifact.update(size=len(text.encode()), sha256=sha256(text.encode()).hexdigest())
    selection = data["findings"][0]["steps"][0]["evidence"][0]["selections"][0]
    selection.update(sha256=artifact["sha256"], byte_end=artifact["size"])
    assert report._prepare_report_output("p1", data=data, report_packet_json=json.dumps(packet), report_template_markdown=TEMPLATE) == "policy_denied"
    assert packet["artifact_contents"]["request"]["text"] == text


def test_explicit_original_slot_still_contains_whole_original():
    packet, data = sample()
    packet["report_scope"] = None
    template = TEMPLATE + "{{#findings}}{{#finding.evidence}}\n{{ evidence.content }}\n{{/finding.evidence}}{{/findings}}"
    rendered = report._render_report_markdown(data, report_packet_json=json.dumps(packet), report_template_markdown=template)
    assert "irrelevant-prefix" in rendered and "supporting trace" in rendered


def _recorded_steps_sample():
    packet, data = sample()
    packet["report_scope"] = None
    observation = packet["observations"][0]
    observation.update(source_fact_id="f1", remediation="对每次对象读取校验当前主体的资源归属。", reproduction_steps=[
        {"action": "从已授权对照响应取对象编号 42，保留同一用户甲身份。", "observation": "已有对照提供后续请求的对象编号。", "evidence_item_ids": ["interaction"]},
        {"action": "沿用步骤一的对象编号 42 和同一身份读取对象。", "observation": "记录读取结果和未展开的完整研究材料。", "evidence_item_ids": ["interaction", "trace"]},
    ])
    packet["facts"][0]["description"] = "已登录用户读取其他用户对象；尚未验证写入权限。"
    observation["evidence_items"][0]["excerpts"] = [{
        "side": "response", "pointer": "utf8-bytes=" + ":".join(str(data["findings"][0]["steps"][0]["evidence"][0]["selections"][1][key]) for key in ("byte_start", "byte_end")),
    }]
    long_trace = "研究旁支原件，不将开头截作关键证明。\n" * 600
    packet["artifact_contents"]["trace"]["text"] = long_trace
    packet["artifacts"][2].update(size=len(long_trace.encode()), sha256=sha256(long_trace.encode()).hexdigest())
    packet["observations"].append({"id": "o2", "vulnerability_id": "v1", "evidence_items": [],
                                   "source_fact_id": "f1", "remediation": "另一次发现的建议，不借用其步骤。"})
    binding = {"project_id": "p1", "task_run_id": "r1", "generation_token": "g1"}
    contents = [
        {"kind": "executive_summary", "text": "项目综合：优先修复读取授权，写入风险仍未知。"},
        {"kind": "overall_remediation", "text": "集中核查同类对象访问的服务端授权边界。"},
        {"kind": "finding", "vulnerability_id": "v1", "observation_id": "o1",
         "reuse_source": ["description", "steps", "remediation"], "limitations": "只依据本次冻结发现，未进行新增测试。"},
    ]
    records = {f"h{index}": {"protocol": "cairn-report-segment-v1", "binding": binding, "key": f"s{index}", "content": content}
               for index, content in enumerate(contents)}
    return packet, records, binding


def test_reused_steps_keep_frozen_parameters_evidence_ranges_and_long_original_locator():
    from cairn.report_segments import assemble_report_segments

    live, records, binding = _recorded_steps_sample()
    frozen = deepcopy(live)
    original = deepcopy(frozen)
    live["observations"][0]["reproduction_steps"][0]["action"] = "后改的实时步骤不能进入已冻结报告。"
    live["facts"][0]["description"] = "后改的实时说明。"
    data = assemble_report_segments([{"artifact_handle": key} for key in records], records, binding=binding, packet=frozen)
    finding = data["findings"][0]
    assert finding["description"] == frozen["facts"][0]["description"]
    assert [step["operation"] for step in finding["steps"]] == [step["action"] for step in frozen["observations"][0]["reproduction_steps"]]
    assert "对象编号 42" in finding["steps"][1]["operation"]
    assert [item["evidence_item_id"] for item in finding["steps"][1]["evidence"]] == ["interaction", "trace"]
    assert finding["steps"][1]["evidence"][1]["selections"] == []
    context = readable_finding_context(finding, frozen)
    evidence = context["finding.key_evidence"]
    assert "尾部关键结果：跨用户记录" in evidence and "GET /records/other" in evidence
    assert "irrelevant-prefix" not in evidence and "研究旁支原件" not in evidence
    assert "未记录关键选段" in evidence
    excerpt = frozen["observations"][0]["evidence_items"][0]["excerpts"][0]["pointer"]
    assert all(token not in evidence for token in ("Observation o1", "Evidence trace", "Artifact trace", excerpt))
    assert data["executive_summary"] == records["h0"]["content"]["text"]
    assert frozen == original


@pytest.mark.parametrize("mutation,error", [
    ("no_observation", "steps_source_unavailable"),
    ("missing_source", "steps_source_unavailable"),
    ("wrong_evidence", "evidence_observation_mismatch"),
    ("inline_conflict", "source_conflict"),
    ("range", "out_of_bounds"),
])
def test_reused_steps_reject_missing_source_conflicts_and_invalid_original_ranges(mutation, error):
    from cairn.report_segments import assemble_report_segments

    packet, records, binding = _recorded_steps_sample()
    content = records["h2"]["content"]
    if mutation == "no_observation":
        content.update(observation_id=None, reuse_source=["steps"], description="已有人工说明。", remediation="已有人工建议。")
    elif mutation == "missing_source":
        content["observation_id"] = "o2"
    elif mutation == "wrong_evidence":
        packet["observations"][0]["reproduction_steps"][0]["evidence_item_ids"] = ["别次证据"]
    elif mutation == "inline_conflict":
        content["steps"] = []
    elif mutation == "range":
        packet["observations"][0]["evidence_items"][0]["excerpts"][0]["pointer"] = "utf8-bytes=0:999999"
    with pytest.raises(ValueError, match=error):
        assemble_report_segments([{"artifact_handle": key} for key in records], records, binding=binding, packet=packet)


def test_finding_can_rewrite_steps_with_selected_originals_instead_of_reusing_them():
    from cairn.report_segments import assemble_report_segments

    packet, records, binding = _recorded_steps_sample()
    _, existing = sample()
    content = records["h2"]["content"]
    content["reuse_source"] = ["description", "remediation"]
    content["steps"] = existing["findings"][0]["steps"]
    for evidence in content["steps"][0]["evidence"]:
        for selection in evidence["selections"]:
            selection.pop("sha256")
    data = assemble_report_segments([{"artifact_handle": key} for key in records], records, binding=binding, packet=packet)
    assert data["findings"][0]["steps"][0]["operation"] == "以现有身份读取另一个用户的记录。"
    assert len(data["findings"][0]["steps"]) == 1
