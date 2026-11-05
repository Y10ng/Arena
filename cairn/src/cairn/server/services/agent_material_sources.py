"""会话交付的有界来源投影；不是读取账本，也不证明模型实际使用了材料。"""

from hashlib import sha256
import json

from .time_utils import utcnow


SOURCE_METHODS = {"read_conversation_attachment", "_read_conversation_attachment_bytes",
                  "read_research_context", "read_vulnerability", "read_conversation_research", "_read_conversation_research_bytes"}


def projection_version(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def describe_source(method, params, data):
    """只对本次实际准出的投影取摘要；读取时间不参与材料版本。"""
    if data.get("availability") == "unavailable" or data.get("readable") is False:
        return None
    projection = {key: value for key, value in data.items() if key != "read_at"}
    reference = {"method": method, "params": params, "expected_version": projection_version(projection)}
    if method in {"read_conversation_attachment", "_read_conversation_attachment_bytes"}:
        label = "会话附件 · " + data["attachment"]["filename"]
        if method.startswith("_"):
            scope = "完整附件字节；不证明已解析内容"
        else:
            start = data["offset"]
            scope = f"Unicode 字符 [{start}, {start + len(data['content'])})；仅此范围"
    elif method in {"read_conversation_research", "_read_conversation_research_bytes"} and (
            "artifact" in data or data.get("source") == "artifacts"):
        label = "会话文件材料 · " + data["conversation_id"]
        if "items" in data:
            scope = f"offset={params.get('offset', 0)} 起的 {len(data['items'])} 项索引；不含正文"
        else:
            label += " · " + data["artifact"]["title"]
            if method.startswith("_"):
                scope = "完整原件字节；不证明已解析内容或漏洞成立"
            else:
                start = data["offset"]
                scope = f"原件字节 [{start}, {start + data['bytes_read']}) 的文本视图；仅此范围"
    elif method == "read_conversation_research":
        label = "会话 HTTP 材料 · " + data["conversation_id"]
        if "items" in data:
            scope = f"cursor={params.get('cursor', 0)} 后的 {len(data['items'])} 项索引；不含正文"
        else:
            label += " · " + data["flow_id"]
            start = data["offset"]
            scope = f"{data['side']} 头部投影与采集正文原字节 [{start}, {start + data['bytes_read']}) 的文本视图；仅此范围"
            if not data["sealed"] or not data["body_complete"]:
                scope += "；正文尚未完整封口"
    elif method == "read_vulnerability":
        label = "漏洞 · " + data["vulnerability_id"]
        scope = "所选 Observation 的可读详情与证据限制：" + (data["observation_id"] or "漏洞记录")
    else:
        label = "项目材料 · " + params["project_id"] + " · " + params["source"]
        if "items" in data:
            start = params.get("offset", 0)
            scope = f"索引条目 [{start}, {start + len(data['items'])})；仅索引，不含材料正文"
        else:
            label += " · " + params["item_id"]
            start = data["offset"]
            length = len(data["content"].encode()) if data["offset_unit"] == "utf8_bytes" else len(data["content"])
            unit = "UTF-8 字节" if data["offset_unit"] == "utf8_bytes" else "Unicode 字符"
            scope = f"{unit} [{start}, {start + length})；仅此范围"
            if params.get("source") == "http_flows":
                scope += "；" + params.get("side", "request") + " 结构化投影，非原始网络字节"
    readable = data.get("readable")
    limitations = readable.get("limitations_markdown", "") if isinstance(readable, dict) else ""
    return {"reference": reference, "label": label, "scope": scope, "limitations": limitations,
            "version": reference["expected_version"],
            "material_version": {key: data[key] for key in ("sha256", "revision", "representation") if key in data}}


def verified_source(description):
    return {**description, "verified_at": utcnow()}


def public_sources(metadata):
    # 历史 metadata 不回写、不补造核验；旧 sources/read_at 只能作为未核验声明展示。
    return {"verified": metadata.get("verified_sources", []),
            "declared": metadata.get("source_notes", metadata.get("sources", [])),
            "declared_read_at": metadata.get("read_at"),
            "limitations": metadata.get("limitations", [])}
