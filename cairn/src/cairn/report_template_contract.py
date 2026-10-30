"""报告模板的有限语法；保存、受理和渲染共享同一规则。"""

from __future__ import annotations

from dataclasses import dataclass
import html
import re
from collections.abc import Mapping

from cairn.report_readable import ReportMarkdown


_FIELDS = {
    "": {"report_title", "executive_summary", "overall_remediation", "report_boundary", "limitations", "finding_count"},
    "findings": {"finding." + key for key in (
        "id", "title", "severity", "status", "target", "verification_status", "description",
        "recorded_steps", "impact", "root_cause", "remediation", "regression_steps", "limitations", "evidence_note", "key_evidence",
        "basic_information", "step_evidence",
    )},
    "finding.steps": {"step.number", "step.explanation", "step.evidence_status"},
    "finding.evidence": {"evidence." + key for key in (
        "id", "title", "observation_id", "source", "request", "response", "artifact", "association", "content",
    )},
    "artifacts": {"artifact.id", "artifact.kind", "artifact.sha256"},
}
_COLLECTIONS = {"": {"findings", "artifacts"}, "findings": {"finding.steps", "finding.evidence"}}
_RAW_FIELDS = {"evidence.request", "evidence.response", "evidence.artifact"}
_TOKEN = re.compile(r"\{\{\s*([#/][a-z.]+|[a-z_][a-z_0-9]*(?:\.[a-z_][a-z_0-9]*)?)\s*\}\}")
_FENCE = re.compile(r"(?m)^(`{3,}|~{3,})([^\n]*)\n(.*?)\n\1[ \t]*(?=\n|$)", re.DOTALL)


@dataclass(frozen=True)
class TemplateNode:
    kind: str
    value: str
    children: tuple[TemplateNode, ...] = ()


def parse_report_template(markdown: str) -> tuple[TemplateNode, ...]:
    """只接受固定字段和两个实际重复层级，不执行表达式或静默保留未知槽位。"""
    if not markdown.strip():
        raise ValueError("模板正文不能为空。")
    nodes: list[TemplateNode] = []
    stack: list[tuple[str, list[TemplateNode]]] = [("", nodes)]
    fields: set[str] = set()
    loops: set[str] = set()
    cursor = 0
    for match in _TOKEN.finditer(markdown):
        literal = markdown[cursor:match.start()]
        if "{{" in literal or "}}" in literal:
            raise ValueError("模板槽位格式不合法；仅支持 {{ 字段 }} 和 {{#集合}}…{{/集合}}。")
        stack[-1][1].append(TemplateNode("text", literal))
        token = match[1]
        scope = stack[-1][0]
        if token.startswith("#"):
            name = token[1:]
            if name not in _COLLECTIONS.get(scope, set()):
                raise ValueError(f"此处不支持重复区：{name}。")
            loops.add(name)
            stack.append((name, []))
        elif token.startswith("/"):
            name = token[1:]
            if len(stack) == 1 or name != scope:
                raise ValueError(f"重复区结束标记不匹配：{name}。")
            _, children = stack.pop()
            stack[-1][1].append(TemplateNode("loop", name, tuple(children)))
        else:
            allowed = set().union(*(_FIELDS[item[0]] for item in stack))
            if token not in allowed:
                raise ValueError(f"未知或不属于当前重复区的字段：{token}。")
            fields.add(token)
            stack[-1][1].append(TemplateNode("field", token))
        cursor = match.end()
    ending = markdown[cursor:]
    if "{{" in ending or "}}" in ending:
        raise ValueError("模板槽位格式不合法。")
    stack[-1][1].append(TemplateNode("text", ending))
    if len(stack) != 1:
        raise ValueError(f"重复区未闭合：{stack[-1][0]}。")
    required = {"finding.title", "finding.description", "finding.recorded_steps", "finding.remediation"}
    if "finding.step_evidence" in fields:
        required.remove("finding.recorded_steps")
    missing = sorted(required - fields)
    if missing:
        raise ValueError("模板缺少必要槽位：" + "、".join(missing) + "。")
    if "findings" not in loops:
        raise ValueError("模板需要逐漏洞 findings 重复区。")
    raw_in_fences: set[int] = set()
    for fence in _FENCE.finditer(markdown):
        matches = list(_TOKEN.finditer(fence[3]))
        if matches:
            # 一个原件独占围栏；不能将字段、循环或两个交互混成一个包。
            if len(matches) != 1 or matches[0][1] not in _RAW_FIELDS or fence[3].strip() != matches[0][0]:
                raise ValueError("代码围栏内只能放一个独占一行的原件槽位。")
            raw_in_fences.add(fence.start(3) + matches[0].start())
    for match in _TOKEN.finditer(markdown):
        if match[1] in _RAW_FIELDS and match.start() not in raw_in_fences:
            raise ValueError(f"原件槽位 {match[1]} 必须独占代码围栏中的一行。")
    # 先去除完整围栏，再拒绝未闭合围栏及可吞掉动态正文的 HTML 容器。
    outside = _FENCE.sub("", markdown)
    if re.search(r"(?m)^\s*(?:`{3,}|~{3,})", outside):
        raise ValueError("模板代码围栏未闭合或缩进不受支持。")
    if re.search(r"<!--|<\s*/?\s*(?:script|style|pre|textarea|div|details)\b", outside, re.I):
        raise ValueError("模板不支持会隐藏正文的 HTML 容器或注释。")
    return tuple(nodes)


def _text(value: object) -> str:
    """槽位只贡献文字，不能从说明内容插入标题、链接、围栏或表格结构。"""
    text = html.escape(str(value if value not in (None, "") else "未记录"), quote=False)
    text = re.sub(r"([\\`*_{}\[\]()#+.!|>~=\-])", r"\\\1", text)
    text = re.sub(r"(?m)^[ \t]+", lambda match: "".join(f"&#{ord(char)};" for char in match[0]), text)
    return re.sub(r"\r\n|\r|\n", "  \n", text)


def supports_single_vulnerability(markdown: str) -> bool:
    """单条导出没有整体模型解释或全项目索引，不用虚构值填满这些字段。"""
    try:
        nodes = parse_report_template(markdown)
    except ValueError:
        return False

    def supported(items: tuple[TemplateNode, ...]) -> bool:
        return all(
            node.value not in {"executive_summary", "overall_remediation", "report_boundary", "limitations", "artifacts"}
            and supported(node.children)
            for node in items if node.kind != "text"
        )

    return supported(nodes)


def requires_single_vulnerability(markdown: str) -> bool:
    """专用槽位不属于冻结项目报告合同，不能误绑定为项目报告模板。"""
    return any(match[1] in {"finding.basic_information", "finding.step_evidence"} for match in _TOKEN.finditer(markdown))


def render_report_template(markdown: str, context: Mapping[str, object]) -> str:
    nodes = parse_report_template(markdown)

    def expand(items: tuple[TemplateNode, ...], values: Mapping[str, object]) -> str:
        parts: list[str] = []
        for node in items:
            if node.kind == "text":
                parts.append(node.value)
            elif node.kind == "field":
                # 原件延后到整个代码围栏处理，避免原件内容被解析成模板语法。
                parts.append(
                    "{{ " + node.value + " }}"
                    if node.value in _RAW_FIELDS or node.value == "evidence.content"
                    else str(values[node.value]) if isinstance(values[node.value], ReportMarkdown)
                    else _text(values[node.value])
                )
            else:
                rows = values[node.value]
                if not rows:
                    parts.append({
                        "findings": "冻结快照没有漏洞记录；不代表目标不存在漏洞。\n",
                        "finding.steps": "未补充逐步骤解释；以已记录复现材料为准。\n",
                        "finding.evidence": "",
                        "artifacts": "未记录证据索引。\n",
                    }[node.value])
                else:
                    parts.extend(expand(node.children, {**values, **row}) for row in rows)
        fragment = "".join(parts)
        # 在证据所属上下文内一次替换，引用中的模板字符始终作为原文保留。
        def raw_block(match: re.Match[str]) -> str:
            if match[0] == "{{ evidence.content }}":
                return str(values["evidence.content"])
            slot = _TOKEN.fullmatch(match[3].strip())
            if slot is None or slot[1] not in _RAW_FIELDS:
                return match[0]
            text = str(values[slot[1]])
            marker = match[1][0]
            length = max(len(match[1]), max((len(m[0]) + 1 for m in re.finditer(re.escape(marker) + "+", text)), default=3))
            fence = marker * length
            return fence + match[2] + "\n" + text + "\n" + fence
        if "evidence.id" in values:
            # 原件与受控材料同轮插入，不再解释原文中恰好相同的模板字符。
            fragment = re.sub(
                _FENCE.pattern + r"|\{\{ evidence\.content \}\}",
                raw_block,
                fragment,
                flags=_FENCE.flags,
            )
        return fragment

    return expand(nodes, context)
