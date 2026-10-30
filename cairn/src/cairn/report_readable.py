"""报告整理的唯一结构化契约；解释与精确原件选段分开保存。"""

from __future__ import annotations

from hashlib import sha256
import html
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator


Text = Annotated[str, Field(min_length=1, pattern=r"\S")]
READABLE_REPORT_FILENAME = "vulnerability-readable-report.json"
REPORT_DATA_SCHEMA_VERSION = 4


class ReadableModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ReportSelection(ReadableModel):
    side: Literal["request", "response", "artifact"]
    artifact_id: Text | None = None
    flow_id: Text | None = None
    representation: Literal["http-evidence-v1"] | None = None
    revision: int | None = Field(default=None, gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    byte_start: int = Field(ge=0)
    byte_end: int = Field(gt=0)

    @model_serializer(mode="wrap")
    def serialize_source(self, handler):
        # 新 flow 分支不向旧 Artifact 成果增加空的身份字段。
        return {key: value for key, value in handler(self).items() if value is not None}

    @model_validator(mode="after")
    def validate_range(self) -> ReportSelection:
        if (self.flow_id is None) == (self.artifact_id is None):
            raise ValueError("report_selection_source_invalid")
        if self.flow_id is not None:
            if self.side == "artifact" or self.representation is None or self.revision is None:
                raise ValueError("report_selection_projection_required")
        elif self.representation is not None or self.revision is not None:
            raise ValueError("report_selection_projection_forbidden")
        if self.byte_end <= self.byte_start:
            raise ValueError("report_selection_range_invalid")
        return self


class ReportEvidence(ReadableModel):
    evidence_item_id: Text
    selections: list[ReportSelection]


class ReportStep(ReadableModel):
    operation: Text
    observed: Text
    evidence: list[ReportEvidence]


class ReportFinding(ReadableModel):
    vulnerability_id: Text
    observation_id: Text | None
    description: Text
    steps: list[ReportStep]
    remediation: Text
    limitations: str = ""


class ReportReadableData(ReadableModel):
    schema_version: Literal[4]
    executive_summary: Annotated[str, Field(min_length=1, max_length=20000, pattern=r"\S")]
    overall_remediation: Text
    findings: list[ReportFinding]
    limitations: str = ""


def recorded_step_contents(steps: list[dict]) -> list[dict]:
    """单漏洞直出与总报告复用相同原步骤，不从正文推测操作或参数。"""
    return [{"operation": step["action"], "observed": step["observation"],
             "evidence_item_ids": list(step["evidence_item_ids"])} for step in steps]


def recorded_evidence_selections(item: dict, artifacts: dict[str, dict], flows: dict | None = None,
                                 flow_contents: dict | None = None) -> list[dict]:
    """已有节选优先，短原件完整呈现；长原件不凭空制造关键范围。"""
    if item.get("flow_id"):
        flow = (flows or {}).get(item["flow_id"])
        if flow is None:
            raise ValueError("report_readable_flow_unknown")
        selections = []
        for side, projection in flow["projections"].items():
            if projection is None:
                continue
            available = (flow_contents or {}).get(item["flow_id"], {}).get(side, {})
            if "text" not in available and not available.get("selections"):
                continue
            excerpts = [value for value in item.get("excerpts", []) if value["side"] == side]
            ranges = [tuple(map(int, value["pointer"].removeprefix("utf8-bytes=").split(":"))) for value in excerpts]
            if not excerpts and projection["body_display"] == "utf8" and 0 < projection["size"] <= 8192:
                ranges = [(0, projection["size"])]
            for start, end in ranges:
                selection = {"side": side, "flow_id": item["flow_id"], "representation": flow["representation"],
                             "revision": flow["revision"], "sha256": projection["sha256"],
                             "byte_start": start, "byte_end": end}
                try:
                    _selected_flow_original(ReportSelection.model_validate(selection), {
                        "project": {"id": flow["project_id"]}, "flow_evidence": flows, "flow_contents": flow_contents,
                    })
                except ValueError as exc:
                    if str(exc) != "report_readable_flow_range_unavailable":
                        raise
                    continue
                selections.append(selection)
        return selections
    selections = []
    for side in ("request", "response", "artifact"):
        reference = item.get(side)
        if reference is None:
            continue
        artifact = artifacts.get(reference["artifact_id"])
        if artifact is None:
            raise ValueError("report_segment_artifact_unknown")
        excerpts = [excerpt for excerpt in item.get("excerpts", []) if excerpt["side"] == side]
        ranges = []
        for excerpt in excerpts:
            match = re.fullmatch(r"utf8-bytes=([0-9]+):([0-9]+)", excerpt["pointer"])
            if match is None:
                raise ValueError("report_readable_excerpt_invalid")
            ranges.append(tuple(map(int, match.groups())))
        if not excerpts and 0 < artifact["size"] <= 8192:
            ranges.append((0, artifact["size"]))
        for start, end in ranges:
            selections.append(ReportSelection.model_validate({
                "side": side, "artifact_id": artifact["id"], "sha256": artifact["sha256"],
                "byte_start": start, "byte_end": end,
            }).model_dump())
    return selections


def _finding_material(finding: ReportFinding, packet: dict) -> dict[str, dict]:
    vulnerabilities = {item["id"]: item for item in packet["vulnerabilities"]}
    vulnerability = vulnerabilities.get(finding.vulnerability_id)
    if vulnerability is None:
        raise ValueError("report_readable_vulnerability_mismatch")
    scope = packet.get("report_scope")
    if scope is not None and (
        finding.vulnerability_id != scope["vulnerability_id"]
        or finding.observation_id != scope.get("observation_id")
    ):
        raise ValueError("report_readable_scope_mismatch")
    if finding.observation_id is None:
        # 无 Observation 的人工记录只消费自己的材料，不能借用其他发现补侧。
        items = vulnerability.get("record_evidence_items", vulnerability.get("evidence_items", []))
    else:
        observation = next((item for item in packet["observations"] if item["id"] == finding.observation_id), None)
        if observation is None or observation["vulnerability_id"] != finding.vulnerability_id:
            raise ValueError("report_readable_observation_mismatch")
        items = observation["evidence_items"]
    result = {item["id"]: item for item in items}
    if len(result) != len(items):
        raise ValueError("report_readable_evidence_duplicate")
    return result


def _selected_flow_original(selection: ReportSelection, packet: dict) -> tuple[str, bool]:
    flow = packet.get("flow_evidence", {}).get(selection.flow_id)
    if flow is None or flow["project_id"] != packet["project"]["id"]:
        raise ValueError("report_readable_flow_owner_mismatch")
    projection = flow["projections"].get(selection.side)
    if (projection is None or selection.representation != flow["representation"]
            or selection.revision != flow["revision"] or selection.sha256 != projection["sha256"]):
        raise ValueError("report_readable_projection_mismatch")
    if selection.byte_end > projection["size"]:
        raise ValueError("report_readable_range_out_of_bounds")
    if projection["body_display"] != "utf8" and selection.byte_end > projection["header_size"]:
        raise ValueError("report_readable_body_not_text")
    content = packet.get("flow_contents", {}).get(selection.flow_id, {}).get(selection.side, {})
    ranges = [content] if "text" in content else content.get("selections", [])
    for value in ranges:
        if not value["offset"] <= selection.byte_start < selection.byte_end <= value["next_offset"]:
            continue
        raw = value["text"].encode("utf-8")
        if (len(raw) != value["next_offset"] - value["offset"] or sha256(raw).hexdigest() != value["range_sha256"]
                or value["sha256"] != projection["sha256"]
                or value["revision"] != flow["revision"] or value["representation"] != flow["representation"]):
            raise ValueError("report_readable_digest_mismatch")
        if value["offset"] == 0 and value["next_offset"] == projection["size"] and sha256(raw).hexdigest() != projection["sha256"]:
            raise ValueError("report_readable_digest_mismatch")
        start, end = selection.byte_start - value["offset"], selection.byte_end - value["offset"]
        try:
            text = raw[start:end].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("report_readable_utf8_boundary") from exc
        return text, selection.byte_start == 0 and selection.byte_end == projection["size"]
    raise ValueError("report_readable_flow_range_unavailable")


def selected_original(selection: ReportSelection, packet: dict) -> tuple[str, bool]:
    """从已冻结原件取半开字节区间，不解释旧 pointer，也不回退全文。"""
    if selection.flow_id is not None:
        return _selected_flow_original(selection, packet)
    artifact = next((item for item in packet.get("artifacts", []) if item["id"] == selection.artifact_id), None)
    if artifact is None or artifact["project_id"] != packet["project"]["id"]:
        raise ValueError("report_readable_artifact_owner_mismatch")
    content = packet.get("artifact_contents", {}).get(selection.artifact_id)
    if not isinstance(content, dict) or not isinstance(content.get("text"), str):
        raise ValueError("report_readable_artifact_unavailable")
    raw = content["text"].encode("utf-8")
    digest = sha256(raw).hexdigest()
    if len(raw) != artifact["size"] or digest != artifact["sha256"] or digest != selection.sha256:
        raise ValueError("report_readable_digest_mismatch")
    if selection.byte_end > len(raw):
        raise ValueError("report_readable_range_out_of_bounds")
    try:
        # 前缀也需可解码，排除起点落在多字节字符内部的空洞选段。
        raw[:selection.byte_start].decode("utf-8")
        text = raw[selection.byte_start:selection.byte_end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("report_readable_utf8_boundary") from exc
    return text, selection.byte_start == 0 and selection.byte_end == len(raw)


def validate_readable_finding(finding: ReportFinding | dict, packet: dict) -> ReportFinding:
    finding = ReportFinding.model_validate(finding)
    items = _finding_material(finding, packet)
    for step in finding.steps:
        for evidence in step.evidence:
            item = items.get(evidence.evidence_item_id)
            if item is None:
                raise ValueError("report_readable_evidence_observation_mismatch")
            for selection in evidence.selections:
                if selection.flow_id is not None:
                    if item.get("flow_id") != selection.flow_id:
                        raise ValueError("report_readable_interaction_mismatch")
                else:
                    reference = item.get(selection.side)
                    if not isinstance(reference, dict) or reference.get("artifact_id") != selection.artifact_id:
                        raise ValueError("report_readable_interaction_mismatch")
                selected_original(selection, packet)
    return finding


def validate_report_readable(data: dict | bytes | str, packet: dict) -> ReportReadableData:
    """供 Dispatcher 输出与 Server finish/读取共用；不接受模型提供原始包正文。"""
    if isinstance(data, (bytes, str)):
        data = json.loads(data)
    # artifacts 是 Harness 交付字段，不属于持久化的可读成果。
    if isinstance(data, dict) and data.get("artifacts") == []:
        data = {key: value for key, value in data.items() if key != "artifacts"}
    result = ReportReadableData.model_validate(data)
    expected = {item["id"] for item in packet["vulnerabilities"]}
    ids = [item.vulnerability_id for item in result.findings]
    if len(ids) != len(set(ids)) or set(ids) != expected:
        raise ValueError("report_readable_findings_mismatch")
    for finding in result.findings:
        validate_readable_finding(finding, packet)
    return result


class ReportMarkdown(str):
    """仅由程序生成或经过有限 Markdown 处理的交付片段。"""


def readable_markdown(value: str) -> ReportMarkdown:
    """保留解释的列表、表格和代码；链接、图片及 HTML 仍只由程序解引用。"""
    def inline(text: str) -> str:
        # 行内代码按字面交给 Markdown 引擎；其余方括号不允许组成链接或图片。
        parts, cursor = [], 0
        for match in re.finditer(r"(`+)(.+?)\1(?!`)", text):
            parts.append(plain(text[cursor:match.start()]))
            parts.append(match[0])
            cursor = match.end()
        parts.append(plain(text[cursor:]))
        return "".join(parts)

    def plain(text: str) -> str:
        return html.escape(text, quote=False).replace("[", "&#91;").replace("]", "&#93;")

    lines = []
    fence = None
    for line in value.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence is not None:
            lines.append(line)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
        elif marker and (marker[1][0] != "`" or "`" not in marker[2]):
            fence = marker[1]
            lines.append(line)
        elif line.startswith(("    ", "\t")):
            lines.append(line)
        else:
            # 模板拥有章节结构；解释字段可排版，但不能覆盖外层章节。
            lines.append(re.sub(r"^( {0,3})(#{1,6})(?=\s)", r"\1\\\2", inline(line)))
    if fence is not None:
        # 未闭合的解释代码不能吞掉程序随后追加的证据、限制和修复建议。
        lines.append(fence)
    return ReportMarkdown("\n".join(lines))


def _fenced(text: str, language: str) -> str:
    fence = "`" * max(3, max((len(item[0]) + 1 for item in re.finditer(r"`+", text)), default=3))
    return f"{fence}{language}\n{text}\n{fence}"


def readable_finding_context(finding: ReportFinding | dict, packet: dict) -> dict:
    """详情与所有导出共用同一说明、真实节选及可独立定位的证据身份。"""
    finding = validate_readable_finding(finding, packet)
    items = _finding_material(finding, packet)
    artifacts = {item["id"]: item for item in packet.get("artifacts", [])}
    steps = []
    evidence_blocks = []
    step_rows = []
    for index, step in enumerate(finding.steps, 1):
        operation = readable_markdown(step.operation)
        observed = readable_markdown(step.observed)
        steps.append(f"{index}. {operation}\n\n   观察：{observed}")
        step_rows.append({"step.number": index, "step.explanation": ReportMarkdown(f"{operation}\n\n观察：{observed}"),
                          "step.evidence_status": "根据已有材料整理；不是新增测试。"})
        for evidence in step.evidence:
            item = items[evidence.evidence_item_id]
            blocks = [f"**步骤 {index} · {readable_markdown(item.get('title') or '相关交互')}**",
                      f"项目 {readable_markdown(packet['project']['id'])} / 漏洞 {readable_markdown(finding.vulnerability_id)} / "
                      f"Observation {readable_markdown(finding.observation_id or '无')} / Evidence {readable_markdown(item['id'])}"]
            selected_sides = {selection.side for selection in evidence.selections}
            for selection in evidence.selections:
                text, complete = selected_original(selection, packet)
                label = {"request": "请求", "response": "响应", "artifact": "材料"}[selection.side]
                identity = (f"Flow {readable_markdown(selection.flow_id)} · revision {selection.revision} · "
                            f"{selection.representation} · SHA-256 {selection.sha256} · 结构化重建，非原始字节流"
                            if selection.flow_id else f"Artifact {readable_markdown(selection.artifact_id)}")
                blocks.extend([f"{label}（{'完整投影' if complete and selection.flow_id else '完整原件' if complete else '关键节选；其余内容省略'} · "
                               f"{identity} · utf8-bytes={selection.byte_start}:{selection.byte_end}）",
                               _fenced(text, "http" if item["kind"] == "http" else "text")])
            for side, label in (("request", "请求"), ("response", "响应"), ("artifact", "材料")):
                if side in selected_sides:
                    continue
                reference = item.get(side)
                if item.get("flow_id") and side != "artifact":
                    flow = packet.get("flow_evidence", {}).get(item["flow_id"])
                    projection = flow["projections"].get(side) if flow else None
                    content = packet.get("flow_contents", {}).get(item["flow_id"], {}).get(side, {})
                    note = content.get("unavailable") or "未选择投影内容，不能据此补全交互。"
                    identity = f"Flow {readable_markdown(item['flow_id'])}"
                    if projection:
                        identity += f" · revision {flow['revision']} · {flow['representation']} · SHA-256 {projection['sha256']}"
                    blocks.append(f"{label} · {identity}（结构化重建，非原始字节流）：{note}")
                elif reference is not None:
                    artifact_id = reference["artifact_id"]
                    artifact = artifacts.get(artifact_id)
                    note = "原件未记录关键选段，未截取内容充当证明。" if artifact and artifact["size"] > 8192 else "未选择原件内容，不能据此补全交互。"
                    blocks.append(f"{label} · Artifact {readable_markdown(artifact_id)}：{note}")
                elif item["kind"] == "http" and side != "artifact":
                    blocks.append(f"本交互未记录{label}材料。")
            for image in packet.get("display_images", []):
                source_key = image.get("source_key")
                if source_key:
                    if item.get("flow_id"):
                        matches = source_key == f"flow:{item['flow_id']}"
                    else:
                        matches = any(source_key == f"artifact:{item[side]['artifact_id']}:{side}"
                                      for side in ("request", "response") if item.get(side))
                else:
                    matches = (image.get("vulnerability_id") == finding.vulnerability_id
                               and image.get("observation_id") == finding.observation_id
                               and image.get("evidence_item_id") == item["id"])
                if matches:
                    blocks.append(image.get("unavailable_reason") or
                                  f"![{image['caption']}](arena-image:{image['artifact_id']})")
            evidence_blocks.append("\n\n".join(blocks))
    return {
        "finding.description": readable_markdown(finding.description),
        "finding.recorded_steps": ReportMarkdown("\n\n".join(steps) or "现有材料不足以整理真实操作步骤。"),
        "finding.remediation": readable_markdown(finding.remediation),
        "finding.limitations": readable_markdown(finding.limitations or "结论仅适用于已记录条件；修复建议不代表已修复。"),
        "finding.steps": step_rows,
        "finding.key_evidence": ReportMarkdown("\n\n".join(evidence_blocks) or "未选择可交付的关键证据；不能据此补全未记录的交互。"),
    }
