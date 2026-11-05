"""总报告的分段内容契约；产物身份由 Harness 填写，证据摘要只取冻结来源。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from cairn.report_readable import (
    ReadableModel, ReportReadableData, Text, recorded_evidence_selections,
    recorded_step_contents, validate_report_readable,
)


REPORT_SEGMENT_PROTOCOL = "cairn-report-segment-v1"
REPORT_SEGMENT_MANIFEST_PROTOCOL = "cairn-report-segment-manifest-v1"
REPORT_SEGMENT_SUBMISSION_VERSION = 5
REPORT_MAX_SEGMENTS = 256
REPORT_MAX_MANIFEST_BYTES = 128 * 1024
REPORT_MAX_SEGMENT_MANIFEST_BYTES = 1024 * 1024
REPORT_MAX_SEGMENT_BYTES = 1024 * 1024
REPORT_MAX_SEGMENT_TOTAL_BYTES = 32 * 1024 * 1024


class SegmentSelection(ReadableModel):
    side: Literal["request", "response", "artifact"]
    artifact_id: Text | None = None
    flow_id: Text | None = None
    byte_start: int = Field(ge=0)
    byte_end: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_source(self) -> SegmentSelection:
        if (self.artifact_id is None) == (self.flow_id is None) or (self.flow_id is not None and self.side == "artifact"):
            raise ValueError("report_selection_source_invalid")
        if self.byte_end <= self.byte_start:
            raise ValueError("report_selection_range_invalid")
        return self


class SegmentEvidence(ReadableModel):
    evidence_item_id: Text
    selections: list[SegmentSelection] = Field(min_length=1)


class SegmentStep(ReadableModel):
    operation: Text
    observed: Text
    evidence: list[SegmentEvidence]


class SummarySegment(ReadableModel):
    kind: Literal["executive_summary", "overall_remediation", "limitations"]
    text: Text


class FindingSegment(ReadableModel):
    kind: Literal["finding"]
    vulnerability_id: Text
    observation_id: Text | None
    description: str = ""
    steps: list[SegmentStep] = Field(default_factory=list)
    remediation: str = ""
    limitations: str = ""
    reuse_source: list[Literal["description", "steps", "remediation"]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content(self) -> FindingSegment:
        fields = self.model_fields_set & {"description", "steps", "remediation", "limitations"}
        if not fields and not self.reuse_source:
            raise ValueError("report_segment_empty")
        if len(self.reuse_source) != len(set(self.reuse_source)) or fields.intersection(self.reuse_source):
            raise ValueError("report_segment_source_conflict")
        return self


from cairn.report_markdown import MarkdownSegment

# 旧段仅供冻结任务重放；新请求的 Worker schema 只公开 MarkdownSegment。
ReportSegment = Annotated[SummarySegment | FindingSegment | MarkdownSegment, Field(discriminator="kind")]
REPORT_SEGMENT_ADAPTER = TypeAdapter(ReportSegment)
MARKDOWN_SEGMENT_ADAPTER = TypeAdapter(MarkdownSegment)


class SegmentReference(ReadableModel):
    artifact_handle: Text


def _source_field(packet: dict, finding: dict, field: str) -> str | list[dict]:
    vulnerability = next(item for item in packet["vulnerabilities"] if item["id"] == finding["vulnerability_id"])
    if finding["observation_id"] is None:
        source = vulnerability
    else:
        source = next(item for item in packet["observations"] if item["id"] == finding["observation_id"])
    if field == "steps":
        if finding["observation_id"] is None or not isinstance(source.get("reproduction_steps"), list):
            raise ValueError("report_segment_steps_source_unavailable")
        items = {item["id"]: item for item in source["evidence_items"]}
        if len(items) != len(source["evidence_items"]):
            raise ValueError("report_readable_evidence_duplicate")
        artifacts = {item["id"]: item for item in packet.get("artifacts", [])}
        steps = []
        for recorded in recorded_step_contents(source["reproduction_steps"]):
            evidence = []
            for item_id in recorded["evidence_item_ids"]:
                if item_id not in items:
                    raise ValueError("report_readable_evidence_observation_mismatch")
                evidence.append({"evidence_item_id": item_id,
                                 "selections": recorded_evidence_selections(items[item_id], artifacts, packet.get("flow_evidence"), packet.get("flow_contents"))})
            steps.append({"operation": recorded["operation"], "observed": recorded["observed"], "evidence": evidence})
        return steps
    if field == "description":
        fact = next((item for item in packet.get("facts", []) if item["id"] == source.get("source_fact_id")), None)
        value = fact.get("description") if fact is not None else None
    else:
        value = source.get("remediation")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("report_segment_source_unavailable")
    return value


def assemble_report_segments(references: list[dict], records: dict[str, dict], *, binding: dict, packet: dict) -> dict:
    """按显式清单顺序组合完整内容；不补漏项，也不从可变项目猜测来源。"""
    handles = [SegmentReference.model_validate(item).artifact_handle for item in references]
    if len(handles) != len(set(handles)):
        raise ValueError("report_segment_reference_duplicate")
    summaries = {key: [] for key in ("executive_summary", "overall_remediation", "limitations")}
    findings: dict[str, dict] = {}
    written: dict[str, set[str]] = {}
    reused: dict[str, set[str]] = {}
    keys = set()
    vulnerabilities = {item["id"] for item in packet["vulnerabilities"]}
    observations = {item["id"]: item for item in packet["observations"]}
    artifacts = {item["id"]: item for item in packet.get("artifacts", [])}
    for handle in handles:
        record = records.get(handle)
        if (record is None or record.get("protocol") != REPORT_SEGMENT_PROTOCOL
                or record.get("binding") != binding or set(record) != {"protocol", "binding", "key", "content"}):
            raise ValueError("report_segment_identity_mismatch")
        if not isinstance(record["key"], str) or not record["key"] or record["key"] in keys:
            raise ValueError("report_segment_key_duplicate")
        keys.add(record["key"])
        segment = REPORT_SEGMENT_ADAPTER.validate_python(record["content"])
        if isinstance(segment, SummarySegment):
            summaries[segment.kind].append(segment.text)
            continue
        identity = segment.vulnerability_id
        observation_id = segment.observation_id
        if identity not in vulnerabilities:
            raise ValueError("report_segment_vulnerability_unknown")
        if observation_id is not None and (observation_id not in observations or observations[observation_id]["vulnerability_id"] != identity):
            raise ValueError("report_segment_observation_mismatch")
        if identity not in findings:
            findings[identity] = {"vulnerability_id": identity, "observation_id": observation_id,
                                  "description": "", "steps": [], "remediation": "", "limitations": ""}
            written[identity], reused[identity] = set(), set()
        finding = findings[identity]
        if finding["observation_id"] != observation_id:
            raise ValueError("report_segment_observation_conflict")
        values = segment.model_dump(exclude_unset=True)
        for field in segment.reuse_source:
            if field in written[identity]:
                raise ValueError("report_segment_source_conflict")
            finding[field] = _source_field(packet, finding, field)
            written[identity].add(field)
            reused[identity].add(field)
        for field in ("description", "steps", "remediation", "limitations"):
            if field not in values:
                continue
            if field in reused[identity]:
                raise ValueError("report_segment_source_conflict")
            written[identity].add(field)
            if field == "steps":
                for step in values[field]:
                    for evidence in step["evidence"]:
                        for selection in evidence["selections"]:
                            if selection.get("flow_id"):
                                flow = packet.get("flow_evidence", {}).get(selection["flow_id"])
                                original = flow["projections"].get(selection["side"]) if flow else None
                                if original is None:
                                    raise ValueError("report_segment_flow_unknown")
                                selection.update(representation=flow["representation"], revision=flow["revision"])
                            else:
                                original = artifacts.get(selection["artifact_id"])
                                if original is None:
                                    raise ValueError("report_segment_artifact_unknown")
                            selection["sha256"] = original["sha256"]
                    finding[field].append(step)
            elif values[field]:
                finding[field] = "\n\n".join(part for part in (finding[field], values[field]) if part)
    # 同一漏洞可有多段，但最终只有一份明确 Observation 的完整表达。
    accepted = packet.get("accepted_readable_findings", [])
    expected = vulnerabilities - {item["vulnerability_id"] for item in accepted}
    if findings.keys() != expected or any(not {"description", "steps", "remediation"} <= fields for fields in written.values()):
        raise ValueError("report_segment_findings_incomplete")
    result = {"schema_version": 4, **{key: "\n\n".join(parts) for key, parts in summaries.items()},
              "findings": [*findings.values(), *accepted]}
    validated = validate_report_readable(result, packet)
    # 历史只读成果由既有渲染器添加；本次模型清单不能重复提交它们。
    return ReportReadableData.model_validate({**validated.model_dump(), "findings": list(findings.values())}).model_dump()
