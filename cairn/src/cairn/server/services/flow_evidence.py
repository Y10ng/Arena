"""终态 flow 的唯一证据投影与输出边界；结构化原记录和正文原件均不改写。"""

from __future__ import annotations

import base64
import codecs
from hashlib import sha256
import json
import io
import re
import zlib

from fastapi import HTTPException

from cairn.server.db import get_conn
from cairn.server.schemas.traffic import TrafficReadMode
from .audit_artifacts import _artifact_disk_path_from_row

REPRESENTATION = "http-evidence-v1"
PROJECTION_BYTES = 65536
READABLE_BODY_BYTES = 10 * 1024 * 1024
AUTO_READABLE_BODY_BYTES = 256 * 1024
SIDES = ("request", "response")


def _display_bytes(raw: bytes) -> str:
    # v1：有效 UTF-8 保留；反斜杠、控制字符和无效字节显式转义，禁止头值伪造新行。
    result = []
    for char in raw.decode("utf-8", "surrogateescape"):
        code = ord(char)
        if char == "\\":
            result.append("\\\\")
        elif 0xDC80 <= code <= 0xDCFF:
            result.append(f"\\x{code - 0xDC00:02x}")
        elif code < 32 or 127 <= code < 160:
            result.append(f"\\x{code:02x}")
        else:
            result.append(char)
    return "".join(result)


def projection_header(message: dict, side: str) -> bytes:
    """v1 固定 CRLF、原头顺序及重复头；不是 HTTP 原始字节流。"""
    def encoded(key):
        return _display_bytes(base64.b64decode(message[key], validate=True))

    version = _display_bytes(message["http_version"].encode("utf-8"))
    if side == "request":
        line = f"{_display_bytes(message['method'].encode('utf-8'))} {encoded('target_base64')} {version}"
    else:
        line = f"{version} {message['status_code']} {encoded('reason_base64')}"
    lines = [line]
    for name, value in message["headers_base64"]:
        lines.append(f"{_display_bytes(base64.b64decode(name))}: {_display_bytes(base64.b64decode(value))}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")


def _body_mode(message: dict, *, decoded: bool = False) -> str:
    for name, value in message["headers_base64"]:
        name, value = base64.b64decode(name).lower(), base64.b64decode(value).lower().strip()
        if not decoded and name == b"content-encoding" and value not in {b"", b"identity"}:
            return "compressed"
    for name, value in message["headers_base64"]:
        if base64.b64decode(name).lower() == b"content-type":
            content_type = base64.b64decode(value).lower()
            media = content_type.split(b";", 1)[0].strip()
            charset = re.search(rb"(?:^|;)\s*charset\s*=\s*[\"']?([^;\s\"']+)", content_type)
            if charset and charset[1] not in {b"utf-8", b"utf8", b"us-ascii", b"ascii"}:
                return "non-utf8"
            if media.startswith((b"image/", b"audio/", b"video/", b"font/")) or media in {
                b"application/octet-stream", b"application/pdf", b"application/zip", b"application/gzip",
            }:
                return "binary"
    return "utf8"


def _placeholder(mode: str, size: int, digest: str) -> bytes:
    # 占位文字不代表正文，不允许选段把它当成目标实际返回值。
    return f"[body omitted: {mode}; size={size}; sha256={digest}; read original artifact]".encode("ascii")


def _artifact(conn, project_id: str, artifact_id: str):
    row = conn.execute("SELECT * FROM artifacts WHERE project_id = ? AND id = ?", (project_id, artifact_id)).fetchone()
    if row is None:
        raise HTTPException(409, "traffic_evidence_pending")
    return row


def _projection(conn, project_id: str, message: dict, side: str, body: dict) -> dict:
    artifact = _artifact(conn, project_id, body["artifact_id"])
    if artifact["sha256"] != body["sha256"] or artifact["size"] != body["size"]:
        raise HTTPException(409, "traffic_evidence_pending")
    header = projection_header(message, side)
    digest, body_digest = sha256(header), sha256()
    mode, size = _body_mode(message), 0
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        with _artifact_disk_path_from_row(artifact).open("rb") as source:
            while chunk := source.read(PROJECTION_BYTES):
                body_digest.update(chunk)
                digest.update(chunk)
                size += len(chunk)
                if mode == "utf8":
                    try:
                        text = decoder.decode(chunk)
                    except UnicodeDecodeError:
                        mode = "non-utf8"
                    else:
                        if any(ord(char) < 32 and char not in "\r\n\t" or 127 <= ord(char) < 160 for char in text):
                            mode = "binary"
            if mode == "utf8":
                try:
                    decoder.decode(b"", final=True)
                except UnicodeDecodeError:
                    mode = "non-utf8"
    except (OSError, HTTPException):
        raise HTTPException(409, "traffic_evidence_pending") from None
    if size != body["size"] or body_digest.hexdigest() != body["sha256"]:
        raise HTTPException(409, "traffic_evidence_pending")
    if mode != "utf8":
        projected = header + _placeholder(mode, size, body["sha256"])
        digest, length = sha256(projected), len(projected)
    else:
        length = len(header) + size
    return {"side": side, "representation": REPRESENTATION, "sha256": digest.hexdigest(), "size": length,
            "header_size": len(header), "body_artifact_id": body["artifact_id"], "body_sha256": body["sha256"],
            "body_size": size, "body_display": mode}


def resolve_flow_evidence(conn, project_id: str, flow_id: str) -> dict:
    """只冻结已终结且实际存在侧全部可读取的 flow，不等待无关 scope。"""
    row = conn.execute("SELECT * FROM http_flows WHERE project_id = ? AND id = ?", (project_id, flow_id)).fetchone()
    if row is None:
        raise HTTPException(404, "traffic_flow_not_found")
    record = json.loads(row["record_json"])
    if row["state"] not in {"complete", "partial", "failed"} or record["state"] == "receiving":
        raise HTTPException(409, "traffic_evidence_pending")
    bodies = {body["side"]: body for body in conn.execute(
        "SELECT * FROM http_flow_bodies WHERE project_id = ? AND flow_id = ?", (project_id, flow_id),
    ).fetchall()}
    projections = {}
    for side in SIDES:
        message = record[side]
        if message is None:
            projections[side] = None
            continue
        body = bodies.get(side)
        if (not message["body_sealed"] or body is None or body["revision"] != message["body_revision"]
                or body["size"] != message["body_bytes"]):
            raise HTTPException(409, "traffic_evidence_pending")
        projections[side] = _projection(conn, project_id, message, side, body)
    return {"flow_id": flow_id, "project_id": project_id, "scope": record["scope"], "revision": record["revision"],
            "state": record["state"], "representation": REPRESENTATION, "record_sha256": row["record_sha256"],
            "record": record, "projections": projections}


def read_frozen_flow_projection(conn, snapshot: dict, side: str, offset: int = 0, limit: int = PROJECTION_BYTES) -> dict:
    """只用冻结元信息与精确 Artifact；绝不查询 live flow。偏移是 UTF-8 字节。"""
    if side not in SIDES or offset < 0 or not 1 <= limit <= PROJECTION_BYTES:
        raise HTTPException(422, "traffic_projection_range_invalid")
    projection = snapshot["projections"][side]
    if projection is None:
        raise HTTPException(404, "traffic_side_absent")
    if offset > projection["size"]:
        raise HTTPException(422, "traffic_projection_range_invalid")
    message = snapshot["record"][side]
    body = {"artifact_id": projection["body_artifact_id"], "sha256": projection["body_sha256"], "size": projection["body_size"]}
    if _projection(conn, snapshot["project_id"], message, side, body) != projection:
        raise HTTPException(409, "traffic_projection_identity_mismatch")
    header = projection_header(message, side)
    end = min(offset + limit, projection["size"])
    raw = header[offset:end]
    if end > len(header):
        start = max(offset, len(header)) - len(header)
        count = end - max(offset, len(header))
        if projection["body_display"] == "utf8":
            artifact = _artifact(conn, snapshot["project_id"], projection["body_artifact_id"])
            with _artifact_disk_path_from_row(artifact).open("rb") as source:
                source.seek(start)
                raw += source.read(count)
        else:
            raw += _placeholder(projection["body_display"], projection["body_size"], projection["body_sha256"])[start:start + count]
    try:
        # 允许页尾落在多字节字符中间，下一页从完整字符开始；页首必须是字符边界。
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        text = decoder.decode(raw, final=end == projection["size"])
        pending = decoder.getstate()[0]
        next_offset = end - len(pending)
    except UnicodeDecodeError:
        raise HTTPException(422, "evidence_excerpt_utf8_boundary") from None
    if next_offset == offset and offset < projection["size"]:
        raise HTTPException(422, "traffic_projection_limit_too_small")
    return {**{key: snapshot[key] for key in ("flow_id", "project_id", "scope", "revision", "state")},
            **projection, "offset": offset, "next_offset": next_offset, "text": text,
            "range_sha256": sha256(text.encode("utf-8")).hexdigest(),
            "has_more": next_offset < projection["size"]}


def read_flow_projection(project_id: str, flow_id: str, side: str, offset: int = 0, limit: int = PROJECTION_BYTES) -> dict:
    with get_conn() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return read_frozen_flow_projection(conn, resolve_flow_evidence(conn, project_id, flow_id), side, offset, limit)


class _AutomaticReadDeferred(Exception):
    """自动预算耗尽是等待显式读取，不是压缩内容损坏。"""


def _decode_chunks(chunks, encoding: str, *, output_limit: int | None = None):
    """每层按需产出有界块；逆编码链不物化中间全文，gzip 支持连续 member。"""
    if encoding == "identity":
        yield from chunks
        return
    decoder = None
    prefix = b""
    produced = 0
    for chunk in chunks:
        if decoder is None:
            prefix += chunk
            if len(prefix) < 2:
                continue
            chunk, prefix = prefix, b""
            # HTTP deflate 同时接受 zlib 包装与 raw DEFLATE，不以解码失败掩盖损坏。
            wrapped = chunk[0] & 15 == 8 and chunk[0] >> 4 <= 7 and int.from_bytes(chunk[:2], "big") % 31 == 0
            decoder = zlib.decompressobj(31 if encoding == "gzip" else 15 if wrapped else -15)
        while chunk:
            if decoder.eof:
                if encoding != "gzip":
                    raise zlib.error("trailing_deflate_data")
                decoder = zlib.decompressobj(31)
            limit = PROJECTION_BYTES if output_limit is None else min(PROJECTION_BYTES, output_limit + 1 - produced)
            output = decoder.decompress(chunk, limit)
            chunk = decoder.unused_data if decoder.eof else decoder.unconsumed_tail
            produced += len(output)
            if output_limit is not None and produced > output_limit:
                # 限制每一层膨胀，不能靠后续编码层缩小结果绕过自动探测预算。
                raise _AutomaticReadDeferred
            if output:
                yield output
    if decoder is None or not decoder.eof:
        raise zlib.error("incomplete_encoded_body")


def _readable_text(raw: bytes, *, complete: bool, limit: int) -> dict:
    """异常字节显式转义；反斜杠同时转义以区分字面文本，显示预算独立于解码预算。"""
    decoder = codecs.getincrementaldecoder("utf-8")("surrogateescape")
    text = decoder.decode(raw, final=complete)
    escaped = bool(re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\udc80-\udcff]", text))
    if not escaped:
        return {"body_text": text, "text_escaped": False, "display_truncated": False}
    output = io.StringIO()
    remaining = limit

    def append(value: str, *, token: bool = False) -> bool:
        nonlocal remaining
        encoded = value.encode("utf-8")
        if len(encoded) > remaining:
            if not token:
                # 显示前缀不切断 UTF-8 字符或转义 token。
                prefix = codecs.getincrementaldecoder("utf-8")().decode(encoded[:remaining], final=False)
                output.write(prefix)
            return False
        output.write(value)
        remaining -= len(encoded)
        return True

    position = 0
    for match in re.finditer(r"[\\\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\udc80-\udcff]", text):
        char = match[0]
        code = ord(char)
        token = "\\\\" if char == "\\" else f"\\x{code - 0xDC00:02x}" if code >= 0xDC80 else f"\\u{code:04x}"
        if not append(text[position:match.start()]) or not append(token, token=True):
            return {"body_text": output.getvalue(), "text_escaped": True, "display_truncated": True}
        position = match.end()
    truncated = not append(text[position:])
    return {"body_text": output.getvalue(), "text_escaped": True, "display_truncated": truncated}


def _readable_body(source, message: dict, *, mode: TrafficReadMode = "manual") -> dict:
    limit = AUTO_READABLE_BODY_BYTES if mode == "auto" else READABLE_BODY_BYTES
    encodings = [token.strip().lower().decode("ascii", "replace")
                 for name, value in message["headers_base64"] if base64.b64decode(name).lower() == b"content-encoding"
                 for token in base64.b64decode(value).split(b",") if token.strip()]
    result = {"decode_status": "complete", "decoded": any(value != "identity" for value in encodings),
              "content_encodings": encodings, "body_text": "", "decoded_body_bytes": None,
              "body_limit": limit, "text_escaped": False, "display_truncated": False,
              "read_mode": mode, "deferred_reason": None}
    if any(value not in {"gzip", "deflate", "identity"} for value in encodings):
        return {**result, "decode_status": "unsupported-encoding"}
    if _body_mode(message, decoded=True) != "utf8":
        return {**result, "decode_status": "non-text"}
    chunks = iter(lambda: source.read(PROJECTION_BYTES), b"")
    for encoding in reversed(encodings):
        chunks = _decode_chunks(chunks, encoding, output_limit=limit if mode == "auto" else None)
    raw = bytearray()
    try:
        for chunk in chunks:
            raw.extend(chunk[:limit + 1 - len(raw)])
            if len(raw) > limit:
                if mode == "auto":
                    raise _AutomaticReadDeferred
                result["decode_status"] = "too-large"
                break
    except _AutomaticReadDeferred:
        return {**result, "decode_status": "deferred", "deferred_reason": "decoded-size"}
    except zlib.error:
        return {**result, "decode_status": "invalid-encoding"}
    result["decoded_body_bytes"] = len(raw)
    text = _readable_text(bytes(raw[:limit]), complete=result["decode_status"] == "complete", limit=limit)
    if mode == "auto" and text["display_truncated"]:
        return {**result, "decode_status": "deferred", "deferred_reason": "display-size",
                "text_escaped": text["text_escaped"]}
    return {**result, **text}


def read_frozen_flow_readable(conn, snapshot: dict, side: str) -> dict:
    """在调用者的同一快照内读取派生正文；公共导出边界仍由调用者裁决。"""
    if side not in SIDES:
        raise HTTPException(422, "traffic_side_invalid")
    projection = snapshot["projections"][side]
    if projection is None:
        raise HTTPException(404, "traffic_side_absent")
    message = snapshot["record"][side]
    body = {"artifact_id": projection["body_artifact_id"], "sha256": projection["body_sha256"], "size": projection["body_size"]}
    if _projection(conn, snapshot["project_id"], message, side, body) != projection:
        raise HTTPException(409, "traffic_projection_identity_mismatch")
    artifact = _artifact(conn, snapshot["project_id"], body["artifact_id"])
    try:
        with _artifact_disk_path_from_row(artifact).open("rb") as source:
            readable = _readable_body(source, message)
    except OSError:
        raise HTTPException(409, "traffic_evidence_pending") from None
    return {"project_id": snapshot["project_id"], "flow_id": snapshot["flow_id"], "side": side,
            "revision": snapshot["revision"], "representation": "http-readable", "excerpt_eligible": False,
            "header_text": projection_header(message, side).decode("utf-8"), **readable,
            "body_artifact_id": body["artifact_id"], "body_sha256": body["sha256"]}


def read_flow_readable(project_id: str, flow_id: str, side: str, *, mode: TrafficReadMode = "manual") -> dict:
    """自动读取只做小包有界探测；显式阅读与报告沿用原预算，不改变 v1 坐标。"""
    if side not in SIDES:
        raise HTTPException(422, "traffic_side_invalid")
    if mode not in {"auto", "manual"}:
        raise HTTPException(422, "traffic_read_mode_invalid")
    limit = AUTO_READABLE_BODY_BYTES if mode == "auto" else READABLE_BODY_BYTES
    with get_conn() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        row = conn.execute("SELECT * FROM http_flows WHERE project_id = ? AND id = ?", (project_id, flow_id)).fetchone()
        if row is None:
            raise HTTPException(404, "traffic_flow_not_found")
        record = json.loads(row["record_json"])
        message = record[side]
        if message is None:
            raise HTTPException(404, "traffic_side_absent")
        result = {"project_id": project_id, "flow_id": flow_id, "side": side, "revision": record["revision"],
                  "representation": "http-readable", "excerpt_eligible": False,
                  "header_text": projection_header(message, side).decode("utf-8"),
                  "body_text": "", "decode_status": "pending", "decoded": False,
                  "content_encodings": [], "decoded_body_bytes": None, "body_limit": limit,
                  "text_escaped": False, "display_truncated": False,
                  "read_mode": mode, "deferred_reason": None, "body_bytes": message["body_bytes"]}
        body = conn.execute("SELECT * FROM http_flow_bodies WHERE project_id = ? AND flow_id = ? AND side = ?",
                            (project_id, flow_id, side)).fetchone()
        if (row["state"] == "receiving" or record["state"] == "receiving" or not message["body_sealed"] or body is None
                or body["revision"] != message["body_revision"] or body["size"] != message["body_bytes"]):
            return result
        result.update(body_artifact_id=body["artifact_id"], body_sha256=body["sha256"])
        if mode == "auto" and body["size"] > limit:
            # 已知大包不打开原件，也不为派生阅读计算 v1 摘要。
            return {**result, "decode_status": "deferred", "deferred_reason": "raw-size"}
        if mode == "manual":
            # 显式阅读保留原件与投影校验，不把自动预算带入报告导出。
            _projection(conn, project_id, message, side, body)
        artifact = _artifact(conn, project_id, body["artifact_id"])
        if artifact["sha256"] != body["sha256"] or artifact["size"] != body["size"]:
            raise HTTPException(409, "traffic_evidence_pending")
        try:
            with _artifact_disk_path_from_row(artifact).open("rb") as source:
                if mode == "auto":
                    # 即使磁盘内容与元信息不符，也只读取预算加一个探测字节。
                    raw = source.read(limit + 1)
                    if len(raw) > limit:
                        return {**result, "decode_status": "deferred", "deferred_reason": "raw-size"}
                    if len(raw) != body["size"] or sha256(raw).hexdigest() != body["sha256"]:
                        raise HTTPException(409, "traffic_evidence_pending")
                    readable = _readable_body(io.BytesIO(raw), message, mode=mode)
                else:
                    readable = _readable_body(source, message)
        except OSError:
            raise HTTPException(409, "traffic_evidence_pending") from None
        return {**result, **readable}


def read_flow_preview(project_id: str, flow_id: str, side: str, offset: int = 0, limit: int = PROJECTION_BYTES) -> dict:
    """非稳定预览使用同一头部格式化；正文坐标不等于证据投影坐标。"""
    from .traffic import read_traffic_body, read_traffic_record

    if side not in SIDES or offset < 0 or not 1 <= limit <= PROJECTION_BYTES:
        raise HTTPException(422, "traffic_projection_range_invalid")
    flow = read_traffic_record(project_id, flow_id)
    message = flow[side]
    if message is None:
        raise HTTPException(404, "traffic_side_absent")
    body = read_traffic_body(project_id, flow_id, side, preview=True, offset=offset, limit=limit)
    if body["revision"] != message["body_revision"]:
        raise HTTPException(409, "traffic_preview_changed")
    mode = _body_mode(message)
    raw = base64.b64decode(body["content_base64"])
    text = ""
    next_offset = body["next_offset"]
    if mode == "utf8":
        try:
            decoder = codecs.getincrementaldecoder("utf-8")("strict")
            text = decoder.decode(raw, final=body["sealed"] and not body["has_more"])
            next_offset -= len(decoder.getstate()[0])
            if any(ord(char) < 32 and char not in "\r\n\t" or 127 <= ord(char) < 160 for char in text):
                mode, text = "binary", ""
        except UnicodeDecodeError:
            mode, text = "non-utf8", ""
    header = projection_header(message, side)
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    header_text = decoder.decode(header[:PROJECTION_BYTES], final=len(header) <= PROJECTION_BYTES)
    return {
        "project_id": project_id, "flow_id": flow_id, "side": side,
        "revision": flow["revision"], "representation": "http-preview", "stable": False,
        "header_text": header_text if offset == 0 else "", "header_truncated": len(header) > PROJECTION_BYTES,
        "text": text, "body_display": mode, "offset": body["offset"], "next_offset": next_offset,
        "accepted_bytes": body["accepted_bytes"], "captured_bytes": body["captured_bytes"],
        "source_sealed": body["source_sealed"], "sealed": body["sealed"],
        "has_more": next_offset < body["accepted_bytes"],
    }


def validate_flow_excerpt(conn, snapshot: dict, excerpt) -> None:
    value = excerpt if isinstance(excerpt, dict) else excerpt.model_dump()
    projection = snapshot["projections"].get(value["side"])
    if projection is None:
        raise HTTPException(422, "evidence_excerpt_side_absent")
    if (value.get("representation") != REPRESENTATION or value.get("revision") != snapshot["revision"]
            or value.get("sha256") != projection["sha256"]):
        raise HTTPException(422, "evidence_excerpt_projection_mismatch")
    match = re.fullmatch(r"utf8-bytes=([0-9]+):([0-9]+)", value["pointer"])
    if match is None:
        raise HTTPException(422, "evidence_excerpt_range_out_of_bounds")
    start, end = map(int, match.groups())
    if not 0 <= start < end <= projection["size"]:
        raise HTTPException(422, "evidence_excerpt_range_out_of_bounds")
    if projection["body_display"] != "utf8" and end > projection["header_size"]:
        raise HTTPException(422, "evidence_excerpt_body_not_text")
    # 已在 resolve 阶段流式核验正文；只检查两个边界，避免长选段重复扫描整个正文。
    for boundary in (start, end):
        if boundary < projection["size"]:
            read_frozen_flow_projection(conn, snapshot, value["side"], boundary, min(4, projection["size"] - boundary))


def flow_metadata_allowed(conn, snapshot: dict, *, public: bool) -> bool:
    """头部不经过 Artifact 策略，必须在同一输出边界检查真实解码值与平台秘密。"""
    from .verification_requests import PUBLIC_SECRET_PATTERN, _platform_secrets

    try:
        secrets = _platform_secrets(conn, snapshot["project_id"], snapshot["scope"]["task_run_id"])
    except HTTPException:
        return False
    pieces = [json.dumps(snapshot["record"], ensure_ascii=False).encode("utf-8")]
    for side in SIDES:
        message = snapshot["record"][side]
        if message is None:
            continue
        pieces.append(projection_header(message, side))
        for key, value in message.items():
            if key.endswith("_base64") and isinstance(value, str):
                pieces.append(base64.b64decode(value))
        for name, value in message["headers_base64"]:
            raw_name, raw_value = base64.b64decode(name), base64.b64decode(value)
            if public and raw_name.lower() in {b"authorization", b"proxy-authorization", b"cookie", b"set-cookie", b"x-api-key"}:
                return False
            pieces.append(raw_name + b": " + raw_value)
    raw = b"\n".join(pieces)
    pattern = re.compile(PUBLIC_SECRET_PATTERN.pattern.encode("ascii"), re.IGNORECASE)
    return not (public and pattern.search(raw)) and not any(secret in raw for secret in secrets)


def freeze_report_flows(conn, project_id: str, sources: list[dict], *,
                        source_checks: dict | None = None) -> tuple[dict, dict]:
    """冻结元信息与有界可交付投影；长正文没有选段时只提供身份与缺口。"""
    from .verification_requests import _artifact_deliverable

    if source_checks is None:
        source_checks = {}
    snapshots, contents, items_by_flow = {}, {}, {}
    for source in sources:
        for item in source["evidence_items"]:
            if item.get("flow_id"):
                items_by_flow.setdefault(item["flow_id"], []).append(item)
    for flow_id, items in items_by_flow.items():
        snapshot = resolve_flow_evidence(conn, project_id, flow_id)
        snapshots[flow_id] = snapshot
        contents[flow_id] = {}
        allowed_metadata = flow_metadata_allowed(conn, snapshot, public=True)
        for side in SIDES:
            projection = snapshot["projections"][side]
            if projection is None:
                contents[flow_id][side] = {"unavailable": "本交互未记录此侧材料。"}
                continue
            if not allowed_metadata or not _artifact_deliverable(
                conn, _artifact(conn, project_id, projection["body_artifact_id"]), source_checks=source_checks,
            ):
                contents[flow_id][side] = {"unavailable": "流量头或正文超出允许交付边界；保留身份，权威原件未改写。"}
                continue
            if projection["size"] <= PROJECTION_BYTES:
                contents[flow_id][side] = read_frozen_flow_projection(conn, snapshot, side)
                continue
            ranges = sorted({excerpt["pointer"] for item in items for excerpt in item.get("excerpts", []) if excerpt["side"] == side})
            selections = []
            for pointer in ranges:
                start, end = map(int, pointer.removeprefix("utf8-bytes=").split(":"))
                if end - start > PROJECTION_BYTES:
                    continue
                result = read_frozen_flow_projection(conn, snapshot, side, start, end - start)
                if result["next_offset"] != end:
                    raise HTTPException(422, "evidence_excerpt_utf8_boundary")
                selections.append(result)
            contents[flow_id][side] = {"selections": selections, "unavailable": "长流量仅交付已记录的有界选段；完整原件须单独授权读取。"}
    return snapshots, contents
