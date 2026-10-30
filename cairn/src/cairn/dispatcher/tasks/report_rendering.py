"""将已有漏洞与同来源材料填入模板；冻结合集和单条读取共用材料排版。"""

from __future__ import annotations

from hashlib import sha256
import re

from cairn.report_template_contract import render_report_template
from cairn.report_readable import ReportFinding, readable_finding_context
from cairn.dispatcher.worker_protocol.submission import ReportData


def _fenced(text: str, language: str = "text") -> str:
    length = max(3, max((len(item[0]) + 1 for item in re.finditer(r"`+", text)), default=3))
    fence = "`" * length
    return f"{fence}{language}\n{text}\n{fence}"


def render_evidence_rows(sources: list[dict], artifacts: list[dict], contents: dict, project_id: str,
                         flow_evidence: dict | None = None, flow_contents: dict | None = None) -> list[dict]:
    """每个条目仅消费自己的显式两侧；不按顺序猜测配对或步骤关联。"""
    by_id = {item["id"]: item for item in artifacts}

    def original(ref: dict | None) -> str:
        if ref is None:
            return "本条未记录此侧材料。"
        artifact_id = ref["artifact_id"]
        if artifact_id not in contents:
            raise ValueError("report_evidence_frozen_content_missing")
        item = contents[artifact_id]
        artifact = by_id.get(artifact_id)
        if artifact is not None and artifact["project_id"] != project_id:
            raise ValueError("report_evidence_artifact_owner_mismatch")
        if item.get("unavailable"):
            return item["unavailable"]
        if artifact is None:
            raise ValueError("report_evidence_artifact_missing")
        text = item["text"]
        content = text.encode("utf-8")
        if len(content) != artifact["size"] or sha256(content).hexdigest() != artifact["sha256"]:
            raise ValueError("report_evidence_digest_mismatch")
        # 指针不是一份可还原报文；保留完整附件及明确身份，不丢弃已允许交付的正文。
        if ref.get("pointer"):
            return "此材料引用了原件内的位置，以下为完整原件，不是独立还原的报文。\n\n" + text
        return text

    rows = []
    for source in sources:
        for item in source["evidence_items"]:
            if item.get("flow_id"):
                from cairn.report_readable import ReportSelection, selected_original

                flow = (flow_evidence or {}).get(item["flow_id"])
                if flow is None or flow["project_id"] != project_id:
                    raise ValueError("report_evidence_flow_missing")
                sides, blocks = {}, [f"Flow {flow['flow_id']} · revision {flow['revision']} · {flow['state']} · 结构化重建，非原始字节流"]
                packet = {"project": {"id": project_id}, "flow_evidence": flow_evidence, "flow_contents": flow_contents}
                for side, label in (("request", "请求"), ("response", "响应")):
                    projection = flow["projections"][side]
                    content = (flow_contents or {}).get(item["flow_id"], {}).get(side, {})
                    parts = []
                    if projection:
                        parts.append(f"{flow['representation']} · SHA-256 {projection['sha256']} · Body Artifact {projection['body_artifact_id']}")
                        ranges = [content] if "text" in content else content.get("selections", [])
                        for value in ranges:
                            # 二进制占位只说明缺口，不以目标正文或报告证明呈现。
                            end = value["next_offset"] if projection["body_display"] == "utf8" else min(value["next_offset"], projection["header_size"])
                            if end <= value["offset"]:
                                continue
                            text, _ = selected_original(ReportSelection(
                                side=side, flow_id=flow["flow_id"], representation=flow["representation"],
                                revision=flow["revision"], sha256=projection["sha256"], byte_start=value["offset"], byte_end=end,
                            ), packet)
                            parts.append(f"utf8-bytes={value['offset']}:{end}\n\n" + _fenced(text, "http"))
                        if projection["body_display"] != "utf8":
                            parts.append("非文本或压缩正文未内嵌；请单独授权读取原件。")
                    if content.get("unavailable"):
                        parts.append(content["unavailable"])
                    sides[side] = "\n\n".join(parts) or "本交互未记录此侧材料。"
                    blocks.extend([f"**{label}**", sides[side]])
                rows.append({"evidence.id": item["id"], "evidence.title": item.get("title") or "HTTP 交互",
                             "evidence.observation_id": source["id"], "evidence.source": item["source"],
                             "evidence.association": blocks[0], "evidence.request": sides["request"],
                             "evidence.response": sides["response"], "evidence.artifact": "正文原件独立授权读取。",
                             "evidence.content": "\n\n".join(blocks)})
                continue
            sides = {}
            for slot, inline in (("request", "request_text"), ("response", "response_text"), ("artifact", "text")):
                parts = []
                if item.get(slot) is not None:
                    parts.append(original(item[slot]))
                if item.get(inline) is not None:
                    parts.append(item[inline])
                sides[slot] = "\n\n".join(parts) if parts else "本条未记录此侧材料。"
            identity = {"manual": "手工记录材料", "historical": "历史内联／引用材料", "capture": "已记录交互材料"}[item["source"]]
            if any(item.get(slot) is not None for slot in ("request_text", "response_text", "text")):
                identity += "（内联文本，不是采集原件）"
            blocks = [source.get("label", "") + identity]
            if item["kind"] == "http":
                for slot, inline, label in (("request", "request_text", "请求"), ("response", "response_text", "响应")):
                    if item.get(slot) is not None:
                        blocks.extend([f"**{label}**", _fenced(original(item[slot]), "http")])
                    if item.get(inline) is not None:
                        blocks.extend([f"**{label}（内联文本）**", _fenced(item[inline], "http")])
                    if item.get(slot) is None and item.get(inline) is None:
                        blocks.append(f"本条未记录{label}。")
            else:
                if item.get("artifact") is not None:
                    blocks.append(_fenced(original(item["artifact"])))
                if item.get("text") is not None:
                    blocks.append(_fenced(item["text"]))
            rows.append({
                "evidence.id": item["id"],
                "evidence.title": item.get("title") or ("HTTP 交互" if item["kind"] == "http" else "相关材料"),
                "evidence.observation_id": source["id"],
                "evidence.source": item["source"],
                "evidence.association": identity + "；未记录步骤关联。",
                "evidence.request": sides["request"],
                "evidence.response": sides["response"],
                "evidence.artifact": sides["artifact"],
                "evidence.content": "\n\n".join(blocks),
            })
    return rows


def evidence_note(rows: list[dict]) -> str:
    return "以下为相关交互与材料，未记录与具体步骤的对应关系。" if rows else "未记录可交付的相关材料。"


def finding_limitations(status: str, verification_statuses: list[str], explanation: str = "") -> str:
    notes = []
    if status == "dismissed":
        notes.append("此记录已被驳回，不能作为已确认漏洞。")
    if any(value in {"conflicting", "not_reproduced", "rejected"} for value in verification_statuses):
        notes.append("已有未复现、反证或冲突记录，不能据此确认漏洞。")
    elif not verification_statuses or any(value != "confirmed" for value in verification_statuses):
        notes.append("现有材料未全部获得复现确认，结论仍受已记录条件限制。")
    if explanation:
        notes.append(explanation)
    return "\n".join(notes) or "结论仅适用于已记录条件；修复建议不代表已修复。"


def render_frozen_report(data: ReportData, packet: dict, template: str) -> str:
    observations = packet["observations"]
    statuses = {item["id"]: item for item in packet["observation_statuses"]}
    findings = {item.vulnerability_id: item for item in data.findings}
    # 已接受内容属于冻结输入，不能被本次整体模型的重新措辞替换。
    findings.update({item["vulnerability_id"]: ReportFinding.model_validate(item)
                     for item in packet.get("accepted_readable_findings", [])})
    rows = []
    used_artifacts = set()
    for vulnerability in packet["vulnerabilities"]:
        finding = findings[vulnerability["id"]]
        own_observations = [item for item in observations if item["vulnerability_id"] == vulnerability["id"]]
        sources = [
            {**item, "label": f"发现 {index} · " if len(own_observations) > 1 else ""}
            for index, item in enumerate(own_observations, 1)
        ]
        sources.append({"id": "漏洞记录原件（非 Observation）", "label": "漏洞记录 · ", "evidence_items": vulnerability["record_evidence_items"]})
        evidence = (render_evidence_rows(sources, packet.get("artifacts", []), packet["artifact_contents"], packet["project"]["id"],
                                         packet.get("flow_evidence"), packet.get("flow_contents"))
                    if re.search(r"\{\{\s*#finding\.evidence\s*\}\}", template) else [])
        used_artifacts.update(
            item[side]["artifact_id"] for source in sources for item in source["evidence_items"]
            for side in ("request", "response", "artifact") if item.get(side) is not None
        )
        readable = readable_finding_context(finding, packet)
        verification = [statuses[item["id"]]["verification_status"] for item in own_observations]
        rows.append({
            **{"finding." + key: vulnerability.get(key) for key in ("id", "title", "severity", "status", "target")},
            "finding.verification_status": "; ".join(verification) or "未记录验证结论",
            **readable,
            "finding.impact": "实际支持的影响见漏洞说明。",
            "finding.root_cause": "仅以漏洞说明中有依据的解释为准；未记录的根因不能补全。",
            "finding.regression_steps": "修复后的验证建议见修复建议；不代表已执行。",
            "finding.limitations": finding_limitations(vulnerability["status"], verification, "\n".join(filter(None, [finding.limitations, data.limitations]))),
            "finding.evidence_note": evidence_note(evidence),
            "finding.evidence": evidence,
        })
    project = packet["project"]
    coverage = packet["assessment_summary"]["coverage"]
    boundary = (
        "项目已停止；本报告是冻结快照的部分报告，不代表评估完成或完整覆盖。"
        if project.get("status") == "stopped"
        else "本报告仅描述冻结快照；项目状态不代表完整覆盖或没有剩余风险。"
    )
    boundary += (
        f" 已测记录 {len(coverage['tested'])} 项，阻塞 {len(coverage['blocked'])} 项，"
        f"未测或未完成 {len(coverage['untested'])} 项；未测不能当作安全。"
        " 模型解释、修复建议和回归方法不是新增测试证据，也不证明已修复。"
    )
    context = {
        "report_title": str(project.get("title") or project["id"]) + " · 漏洞报告",
        "executive_summary": data.executive_summary,
        "overall_remediation": data.overall_remediation,
        "report_boundary": boundary,
        "limitations": data.limitations or "未补充整体解释限制；冻结材料中的限制仍然适用。",
        "finding_count": len(rows),
        "findings": rows,
        "artifacts": [{
            "artifact.id": item["id"], "artifact.kind": item.get("kind"), "artifact.sha256": item.get("sha256"),
        } for item in packet.get("artifacts", []) if item["id"] in used_artifacts],
    }
    return render_report_template(template, context)
