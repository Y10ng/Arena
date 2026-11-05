"""自由 Markdown 写作合同：只校验来源与保存身份，局部插入已冻结证据。"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field

from cairn.report_readable import ReadableModel, Text
from cairn.vulnerability_number import vulnerability_number

REPORT_MARKDOWN_SCHEMA_VERSION = 6


class MarkdownSource(ReadableModel):
    vulnerability_id: Text
    observation_ids: list[Text] = Field(default_factory=list)


class MarkdownSegment(ReadableModel):
    kind: Literal["markdown"]
    markdown: Text
    sources: list[MarkdownSource]


_EVIDENCE = re.compile(r"\[证据\]\(evidence://(?:(record)/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\)")
_HTTP = re.compile(r"(?im)^\s*(?:HTTP/\d(?:\.\d)?\s+\d{3}|(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|CONNECT|TRACE)\s+\S+\s+HTTP/\d)")
_CONTAINER = re.compile(r"^ {0,3}(?:> ?|(?:[-+*]|\d{1,9}[.)])[ \t]+)")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _has_unverified_evidence(markdown: str) -> bool:
    """按 Markdown 块容器检查正文；只读检查，不改写待交付原文。"""
    fence = None
    for line in markdown.expandtabs(4).splitlines():
        # 引用与列表可以任意嵌套；容器标记不是报文内容的一部分。
        line = line.lstrip()
        while match := _CONTAINER.match(line):
            line = line[match.end():].lstrip()
        marker = _FENCE.match(line)
        if fence is None and marker:
            delimiter, info = marker.groups()
            if info.strip().lower().split(maxsplit=1)[:1] in (["http"], ["http-request"], ["http-response"]):
                return True
            fence = delimiter
        elif fence is not None and marker:
            delimiter, info = marker.groups()
            if delimiter[0] == fence[0] and len(delimiter) >= len(fence) and not info.strip():
                fence = None
        # 围栏与缩进代码同样可能包含 HTTP，不能因语言标签不同而跳过。
        if _HTTP.search(line):
            return True
    return bool(re.search(r"!\[[^\]]*\]\(", markdown))


def assemble_markdown_segments(references: list[dict], records: dict[str, dict], *, binding: dict, packet: dict) -> dict:
    from cairn.report_segments import REPORT_SEGMENT_PROTOCOL, SegmentReference
    from cairn.dispatcher.tasks.report_rendering import render_evidence_rows

    handles = [SegmentReference.model_validate(item).artifact_handle for item in references]
    if not handles or len(handles) != len(set(handles)):
        raise ValueError("report_segment_reference_duplicate_or_empty")
    vulnerabilities = {item["id"]: item for item in packet["vulnerabilities"]}
    observations = {item["id"]: item for item in packet["observations"]}
    project_id = packet["project"]["id"]
    used_vulnerabilities, used_observations, keys = set(), set(), set()
    blocks = []
    for handle in handles:
        record = records.get(handle)
        if (record is None or set(record) != {"protocol", "binding", "key", "content"}
                or record["protocol"] != REPORT_SEGMENT_PROTOCOL or record["binding"] != binding):
            raise ValueError("report_segment_identity_mismatch")
        if not isinstance(record["key"], str) or not record["key"] or record["key"] in keys:
            raise ValueError("report_segment_key_duplicate")
        keys.add(record["key"])
        segment = MarkdownSegment.model_validate(record["content"])
        local_observations, local_vulnerabilities = set(), set()
        for source in segment.sources:
            if source.vulnerability_id not in vulnerabilities:
                raise ValueError("report_segment_vulnerability_unknown")
            used_vulnerabilities.add(source.vulnerability_id)
            local_vulnerabilities.add(source.vulnerability_id)
            for observation_id in source.observation_ids:
                observation = observations.get(observation_id)
                if observation is None or observation["vulnerability_id"] != source.vulnerability_id:
                    raise ValueError("report_segment_observation_mismatch")
                local_observations.add(observation_id)
                used_observations.add(observation_id)
        # 自由正文不能提交看似采集原件的 HTTP；原文仅从已核验的冻结来源插入。
        if _has_unverified_evidence(segment.markdown):
            raise ValueError("report_markdown_unverified_evidence")

        def insert_evidence(match: re.Match) -> str:
            record_level, source_id, evidence_id = match.groups()
            allowed = local_vulnerabilities if record_level else local_observations
            if source_id not in allowed:
                raise ValueError("report_markdown_evidence_source_mismatch")
            source = (vulnerabilities if record_level else observations)[source_id]
            evidence_items = source["record_evidence_items" if record_level else "evidence_items"]
            evidence = next((item for item in evidence_items if item["id"] == evidence_id), None)
            if evidence is None:
                raise ValueError("report_markdown_evidence_unknown")
            rows = render_evidence_rows(
                [{**source, "evidence_items": [evidence]}], packet.get("artifacts", []),
                packet.get("artifact_contents", {}), project_id,
                packet.get("flow_evidence"), packet.get("flow_contents"),
            )
            blocks = [rows[0]["evidence.content"]]
            source_keys = ({f"flow:{evidence['flow_id']}"} if evidence.get("flow_id") else
                           {f"artifact:{evidence[side]['artifact_id']}:{side}" for side in ("request", "response") if evidence.get(side)})
            for image in packet.get("display_images", []):
                same_item = (image.get("vulnerability_id") == (source_id if record_level else source["vulnerability_id"])
                             and image.get("observation_id") == (None if record_level else source_id)
                             and image.get("evidence_item_id") == evidence_id)
                if image.get("source_key") in source_keys or (not image.get("source_key") and same_item):
                    blocks.append(image.get("unavailable_reason") or
                                  f"![{image['caption']}](arena-image:{image['artifact_id']})")
            return "\n\n".join(blocks)

        markdown = _EVIDENCE.sub(insert_evidence, segment.markdown)
        if "evidence://" in markdown:
            raise ValueError("report_markdown_evidence_reference_invalid")
        blocks.append(markdown)
    if used_vulnerabilities != vulnerabilities.keys() or used_observations != observations.keys():
        raise ValueError("report_markdown_sources_incomplete")
    markdown = "\n\n".join(blocks) + "\n"
    for identity in vulnerabilities:
        if vulnerability_number(project_id, identity) not in markdown:
            raise ValueError("report_markdown_number_missing")
    return {"schema_version": REPORT_MARKDOWN_SCHEMA_VERSION, "markdown": markdown,
            "segment_refs": references, "source_vulnerability_ids": sorted(used_vulnerabilities),
            "source_observation_ids": sorted(used_observations)}
