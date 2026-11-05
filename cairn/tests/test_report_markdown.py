"""自由正文只接受当前保存身份与冻结来源，证据在指定位置插入。"""
from copy import deepcopy

import pytest

from cairn.report_markdown import assemble_markdown_segments
from cairn.report_segments import REPORT_SEGMENT_PROTOCOL


def material():
    binding = {"project_id": "p1", "generation_token": "current", "task_run_id": "r1"}
    evidence = {"id": "e1", "kind": "http", "source": "historical",
                "response_text": "HTTP/1.1 200 OK\r\n\r\n合成原文"}
    # Packet 的漏洞投影是所选 Observation；记录原件单独保留，不是同一个集合。
    packet = {"project": {"id": "p1"},
              "vulnerabilities": [{"id": "v1", "observation_id": "o1",
                                   "evidence_items": [deepcopy(evidence)], "record_evidence_items": []}],
              "observations": [{"id": "o1", "vulnerability_id": "v1", "evidence_items": [evidence]}]}
    record = {"protocol": REPORT_SEGMENT_PROTOCOL, "binding": deepcopy(binding), "key": "one",
              "content": {"kind": "markdown", "markdown": "# 自由标题\n\np1/v1\n\n[证据](evidence://o1/e1)",
                          "sources": [{"vulnerability_id": "v1", "observation_ids": ["o1"]}]}}
    return [{"artifact_handle": "saved-one"}], {"saved-one": record}, binding, packet


def test_free_markdown_inserts_only_same_source_frozen_evidence_and_images():
    refs, records, binding, packet = material()
    packet["display_images"] = [{"vulnerability_id": "v1", "observation_id": "o1", "evidence_item_id": "e1",
                                  "caption": "已有同源图片", "artifact_id": "a1"},
                                 {"vulnerability_id": "v2", "observation_id": "o2", "evidence_item_id": "e1",
                                  "caption": "其他来源", "artifact_id": "a2"}]
    rendered = assemble_markdown_segments(refs, records, binding=binding, packet=packet)
    assert rendered["markdown"].startswith("# 自由标题\n\np1/v1")
    assert "HTTP/1.1 200 OK\r\n\r\n合成原文" in rendered["markdown"]
    assert "arena-image:a1" in rendered["markdown"] and "arena-image:a2" not in rendered["markdown"]
    assert rendered["source_observation_ids"] == ["o1"]


@pytest.mark.parametrize("change,code", [
    ("missing", "report_segment_identity_mismatch"),
    ("generation", "report_segment_identity_mismatch"),
    ("project", "report_segment_identity_mismatch"),
    ("source", "report_segment_vulnerability_unknown"),
    ("observation", "report_segment_observation_mismatch"),
    ("coverage", "report_markdown_sources_incomplete"),
    ("evidence", "report_markdown_evidence_unknown"),
    ("raw", "report_markdown_unverified_evidence"),
    ("image", "report_markdown_unverified_evidence"),
    ("number", "report_markdown_number_missing"),
])
def test_rejects_unbound_or_unverified_markdown(change, code):
    refs, records, binding, packet = material()
    record = records["saved-one"]
    content = record["content"]
    if change == "missing": records.clear()
    elif change == "generation": record["binding"]["generation_token"] = "old"
    elif change == "project": record["binding"]["project_id"] = "other"
    elif change == "source": content["sources"][0]["vulnerability_id"] = "other"
    elif change == "observation": packet["observations"][0]["vulnerability_id"] = "other"
    elif change == "coverage":
        content["sources"] = []
        content["markdown"] = "p1/v1"
    elif change == "evidence": content["markdown"] = "p1/v1 [证据](evidence://o1/unknown)"
    elif change == "raw": content["markdown"] += "\nHTTP/1.1 200 OK\n伪造"
    elif change == "image": content["markdown"] += "\n![伪造](https://invalid.test/image.png)"
    elif change == "number": content["markdown"] = "无编号"
    with pytest.raises(ValueError, match=code):
        assemble_markdown_segments(refs, records, binding=binding, packet=packet)


def test_record_level_evidence_without_observation():
    refs, records, binding, packet = material()
    finding = packet["vulnerabilities"][0]
    finding["observation_id"] = None
    finding["record_evidence_items"] = packet["observations"].pop()["evidence_items"]
    finding["evidence_items"] = deepcopy(finding["record_evidence_items"])
    records["saved-one"]["content"] = {"kind": "markdown", "markdown": "p1/v1\n[证据](evidence://record/v1/e1)",
        "sources": [{"vulnerability_id": "v1", "observation_ids": []}]}
    assert "合成原文" in assemble_markdown_segments(refs, records, binding=binding, packet=packet)["markdown"]


@pytest.mark.parametrize("record_id", ["record-only", "e1"])
def test_record_and_observation_evidence_keep_their_own_packet_sources(record_id):
    refs, records, binding, packet = material()
    packet["vulnerabilities"][0]["record_evidence_items"] = [
        {"id": record_id, "kind": "http", "source": "manual",
         "request_text": "GET /record HTTP/1.1\r\nHost: example.test\r\n\r\n"}]
    records["saved-one"]["content"]["markdown"] = (
        f"# 自由布局 p1/v1\n\n记录材料\n\n[证据](evidence://record/v1/{record_id})"
        "\n\n观察材料\n\n[证据](evidence://o1/e1)")
    body = assemble_markdown_segments(refs, records, binding=binding, packet=packet)["markdown"]
    record_body, observation_body = body.split("观察材料")
    assert "GET /record HTTP/1.1\r\nHost: example.test\r\n\r\n" in record_body
    assert "合成原文" not in record_body
    assert "合成原文" in observation_body and "/record" not in observation_body
    assert body.count("GET /record") == body.count("合成原文") == 1


@pytest.mark.parametrize("raw", [
    "> GET /invented HTTP/1.1\n> Host: example.test\n> HTTP/1.1 200 OK",
    "> > GET /invented HTTP/1.1",
    "- GET /invented HTTP/1.1",
    "> 1. GET /invented HTTP/1.1",
    "1. 普通段落\n\n    > GET /invented HTTP/1.1",
    "- > ~~~text\n  > HTTP/1.1 200 OK\n  > ~~~",
    "> ```HTTP\n> invented\n> ```",
    "    HTTP/1.1 200 OK",
])
def test_markdown_containers_cannot_bypass_http_provenance(raw):
    refs, records, binding, packet = material()
    records["saved-one"]["content"]["markdown"] += "\n\n" + raw
    with pytest.raises(ValueError, match="report_markdown_unverified_evidence"):
        assemble_markdown_segments(refs, records, binding=binding, packet=packet)


def test_record_reference_does_not_fall_back_to_selected_observation():
    refs, records, binding, packet = material()
    records["saved-one"]["content"]["markdown"] = "p1/v1\n[证据](evidence://record/v1/e1)"
    with pytest.raises(ValueError, match="report_markdown_evidence_unknown"):
        assemble_markdown_segments(refs, records, binding=binding, packet=packet)


def test_free_markdown_containers_and_frozen_evidence_remain_deliverable():
    refs, records, binding, packet = material()
    records["saved-one"]["content"]["markdown"] = (
        "# 任意标题 p1/v1\n\n> 引用说明，不是原始报文。\n\n"
        "- 自由列表\n\n| 栏目 | 内容 |\n| --- | --- |\n| 方法 | GET |\n\n"
        "```python\nprint('示例代码')\n```\n\n[证据](evidence://o1/e1)")
    body = assemble_markdown_segments(refs, records, binding=binding, packet=packet)["markdown"]
    assert "> 引用说明" in body and "print('示例代码')" in body
    assert "HTTP/1.1 200 OK\r\n\r\n合成原文" in body
