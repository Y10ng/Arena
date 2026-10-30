"""已采集材料的可选离线图片：源准出先于像素，关系只存 Artifact.metadata。"""
from __future__ import annotations

from hashlib import sha256
import json
import struct
import zlib

from fastapi import HTTPException
from pydantic import ValidationError

from cairn.evidence_contracts import EvidenceExcerpt

from cairn.server.db import get_conn
from cairn.server.schemas.artifacts import CreateRawArtifactRequest
from .audit_artifacts import _artifact_disk_path_from_row, publish_raw_artifact, stage_raw_artifact
from .flow_evidence import (
    PROJECTION_BYTES, SIDES, _artifact, flow_metadata_allowed,
    read_frozen_flow_projection, resolve_flow_evidence, validate_flow_excerpt,
)

RENDERER = "http-evidence-png-v1"
METADATA_KEY = "http_evidence_render"
MAX_PAGES = 4
MAX_PNG_BYTES = 2 * 1024 * 1024


def is_optional_http_image(artifact) -> bool:
    metadata = artifact.get("metadata") if isinstance(artifact, dict) else None
    if metadata is None:
        metadata = json.loads(artifact["metadata_json"] or "{}")
    marker = metadata.get(METADATA_KEY)
    return (artifact["kind"] == "screenshot" and isinstance(marker, dict)
            and marker.get("version") == 1 and marker.get("renderer") == RENDERER
            and marker.get("optional") is True)


def prepare_render_input(conn, project_id: str, source: dict, *, task_run_id: str | None = None,
                         historical: bool = False, source_checks: dict | None = None) -> dict:
    """仅接受身份与选段，不接收路径、URL、HTML 或调用方提供的报文。"""
    from .verification_requests import _artifact_deliverable

    if not isinstance(source, dict):
        raise HTTPException(422, "http_image_source_invalid")
    if source.get("kind") == "flow":
        if set(source) - {"kind", "flow_id", "excerpts"}:
            raise HTTPException(422, "http_image_source_invalid")
        snapshot = resolve_flow_evidence(conn, project_id, source.get("flow_id"))
        if task_run_id is not None and not historical and snapshot["scope"]["task_run_id"] != task_run_id:
            raise HTTPException(403, "http_image_source_run_mismatch")
        if not flow_metadata_allowed(conn, snapshot, public=True):
            raise HTTPException(403, "http_image_source_not_deliverable")
        excerpts = source.get("excerpts", [])
        if not isinstance(excerpts, list) or len(excerpts) > 2:
            raise HTTPException(422, "http_image_one_excerpt_per_side")
        selected = {}
        for excerpt in excerpts:
            if not isinstance(excerpt, dict) or excerpt.get("side") not in SIDES or excerpt["side"] in selected:
                raise HTTPException(422, "http_image_one_excerpt_per_side")
            try:
                parsed = EvidenceExcerpt.model_validate(excerpt)
            except ValidationError:
                raise HTTPException(422, "http_image_source_excerpt_invalid") from None
            validate_flow_excerpt(conn, snapshot, parsed)
            selected[excerpt["side"]] = excerpt
        identity = {key: snapshot[key] for key in ("project_id", "flow_id", "revision", "representation", "record_sha256", "state")}
        identity.update(kind="flow", task_run_id=snapshot["scope"]["task_run_id"], selections={})
        texts = {}
        for side in SIDES:
            projection = snapshot["projections"][side]
            if projection is None:
                texts[side] = None
                continue
            # 检查完整源而不是只检查选段，避免借裁剪绕过来源输出边界。
            if not _artifact_deliverable(conn, _artifact(conn, project_id, projection["body_artifact_id"]), source_checks=source_checks):
                raise HTTPException(403, "http_image_source_not_deliverable")
            start, end = 0, projection["size"]
            if side in selected:
                start, end = map(int, selected[side]["pointer"].removeprefix("utf8-bytes=").split(":"))
            if end - start > PROJECTION_BYTES:
                raise HTTPException(422, "http_image_source_excerpt_required")
            value = read_frozen_flow_projection(conn, snapshot, side, start, max(1, end - start))
            if value["next_offset"] != end:
                raise HTTPException(422, "http_image_source_utf8_boundary")
            texts[side] = value["text"]
            identity["selections"][side] = {"sha256": projection["sha256"], "offset": start, "length": end - start,
                                               "size": projection["size"], "body_display": projection["body_display"]}
        return {"source": identity, "request_text": texts["request"], "response_text": texts["response"],
                "notice": "HTTP 结构化重建，非原始网络帧；缺侧与选段按来源保留。"}
    if source.get("kind") != "artifact" or set(source) - {"kind", "artifact_id", "sha256", "side", "offset", "length"}:
        raise HTTPException(422, "http_image_source_invalid")
    artifact = _artifact(conn, project_id, source.get("artifact_id"))
    if is_optional_http_image(artifact) or not _artifact_deliverable(conn, artifact, source_checks=source_checks):
        raise HTTPException(403, "http_image_source_not_deliverable")
    if task_run_id is not None and not historical and artifact["task_run_id"] != task_run_id:
        raise HTTPException(403, "http_image_source_run_mismatch")
    if source.get("sha256") != artifact["sha256"] or source.get("side") not in SIDES:
        raise HTTPException(422, "http_image_source_identity_mismatch")
    start, length = source.get("offset", 0), source.get("length", artifact["size"])
    if type(start) is not int or type(length) is not int or not 0 <= start < artifact["size"] or not 1 <= length <= PROJECTION_BYTES or start + length > artifact["size"]:
        raise HTTPException(422, "http_image_source_excerpt_required")
    path = _artifact_disk_path_from_row(artifact)
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(PROJECTION_BYTES):
            digest.update(chunk)
        if stream.tell() != artifact["size"] or digest.hexdigest() != artifact["sha256"]:
            raise HTTPException(409, "http_image_source_changed")
        stream.seek(start)
        raw = stream.read(length)
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise HTTPException(422, "http_image_source_not_utf8") from None
    return {"source": {"kind": "artifact", "project_id": project_id, "task_run_id": artifact["task_run_id"],
                       "artifact_id": artifact["id"], "sha256": artifact["sha256"], "side": source["side"],
                       "offset": start, "length": length, "size": artifact["size"], "representation": "raw-utf8"},
            "request_text": text if source["side"] == "request" else None,
            "response_text": text if source["side"] == "response" else None,
            "notice": "已采集 HTTP 原件的 UTF-8 文本表示；未提供的另一侧不作推测。"}


def freeze_render_sources(conn, project_id: str, evidence_items: list[dict], *,
                          source_checks: dict | None = None) -> dict:
    """仅在冻结时调用；报告运行器只读结果，不重新解析 live flow。"""
    result = {}
    for item in evidence_items:
        sources = []
        if item.get("flow_id"):
            sources.append((f"flow:{item['flow_id']}", {"kind": "flow", "flow_id": item["flow_id"], "excerpts": item.get("excerpts", [])}))
        else:
            for side in SIDES:
                ref = item.get(side)
                if isinstance(ref, dict) and ref.get("artifact_id"):
                    try:
                        artifact = _artifact(conn, project_id, ref["artifact_id"])
                    except HTTPException:
                        continue
                    sources.append((f"artifact:{artifact['id']}:{side}", {"kind": "artifact", "artifact_id": artifact["id"],
                                                                         "sha256": artifact["sha256"], "side": side}))
        for key, source in sources:
            try:
                value = prepare_render_input(conn, project_id, source, historical=True, source_checks=source_checks)
            except (HTTPException, OSError):
                # 可选图没有源准出就不进入冻结索引，正文及原件仍由其自身策略处理。
                continue
            if key in result and result[key] != value:
                continue
            result[key] = value
    return result


def resolve_render_input(conn, project_id: str, task_run_id: str, source: dict, *, historical: bool = False) -> dict:
    if source.get("kind") != "frozen":
        return prepare_render_input(conn, project_id, source, task_run_id=task_run_id, historical=historical)
    from .assessment_reports import _finalization_row, _validate_snapshot_artifact
    if set(source) != {"kind", "snapshot_artifact_id", "source_key"}:
        raise HTTPException(422, "http_image_source_invalid")
    run = conn.execute("SELECT * FROM task_runs WHERE project_id = ? AND id = ?", (project_id, task_run_id)).fetchone()
    if run is None or run["task_type"] != "report":
        raise HTTPException(403, "http_image_source_run_mismatch")
    claim = json.loads(run["runtime_context_json"]).get("report_claim", {})
    if not claim.get("generation_token"):
        raise HTTPException(403, "http_image_frozen_binding_missing")
    finalization = _finalization_row(conn, project_id, generation_token=claim["generation_token"])
    if source["snapshot_artifact_id"] != finalization["input_snapshot_artifact_id"]:
        raise HTTPException(409, "http_image_frozen_binding_mismatch")
    artifact = _validate_snapshot_artifact(conn, finalization, run, artifact_id=source["snapshot_artifact_id"],
                                           digest_sha256=finalization["input_snapshot_sha256"])
    snapshot = json.loads(_artifact_disk_path_from_row(artifact).read_bytes())
    value = snapshot["report_packet"].get("http_evidence_render_sources", {}).get(source["source_key"])
    if not isinstance(value, dict) or value.get("source", {}).get("project_id") != project_id:
        raise HTTPException(404, "http_image_frozen_source_missing")
    return value


def validate_png(content: bytes) -> None:
    """校验固定尺寸 PNG 的完整块与有界像素流，不把仅有魔数的文件发布成图片。"""
    def invalid():
        raise HTTPException(422, "http_image_png_invalid")
    if not 33 <= len(content) <= MAX_PNG_BYTES or content[:8] != b"\x89PNG\r\n\x1a\n":
        invalid()
    position, pixels, channels, finished = 8, bytearray(), 0, False
    while position + 12 <= len(content):
        size = struct.unpack(">I", content[position:position + 4])[0]
        kind = content[position + 4:position + 8]
        end = position + 8 + size
        if end + 4 > len(content):
            invalid()
        data = content[position + 8:end]
        if zlib.crc32(kind + data) != struct.unpack(">I", content[end:end + 4])[0]:
            invalid()
        if kind == b"IHDR":
            if position != 8 or size != 13:
                invalid()
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
            if (width, height, depth, compression, filtering, interlace) != (1440, 1000, 8, 0, 0, 0) or color not in {2, 6}:
                invalid()
            channels = 3 if color == 2 else 4
        elif not channels:
            invalid()
        elif kind == b"IDAT":
            pixels.extend(data)
        elif kind == b"IEND":
            if size or end + 4 != len(content):
                invalid()
            finished = True
            break
        elif kind not in {b"sRGB", b"pHYs", b"gAMA", b"cHRM"}:
            invalid()
        position = end + 4
    if not finished or not pixels:
        invalid()
    expected = (1440 * channels + 1) * 1000
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(pixels, expected + 1)
    except zlib.error:
        invalid()
    if len(raw) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        invalid()
    if any(raw[offset] > 4 for offset in range(0, len(raw), 1440 * channels + 1)):
        invalid()


async def publish_rendered_image(project_id: str, task_run_id: str, render_input: dict, *,
                                 page: int, page_count: int, truncated: bool, content: bytes):
    """调用方必须传服务端准出结果或本次冻结索引，不接受模型自报 metadata。"""
    if render_input["source"]["project_id"] != project_id or type(page) is not int or type(page_count) is not int or not 1 <= page <= page_count <= MAX_PAGES or type(truncated) is not bool:
        raise HTTPException(422, "http_image_identity_invalid")
    validate_png(content)
    metadata = {METADATA_KEY: {"version": 1, "renderer": RENDERER, "optional": True,
                              "source": render_input["source"], "page": page, "page_count": page_count,
                              "truncated": truncated}}
    async def chunks():
        yield content
    async with stage_raw_artifact(project_id, task_run_id, chunks(), expected_size=len(content)) as upload:
        with get_conn() as conn:
            return publish_raw_artifact(conn, project_id, CreateRawArtifactRequest(
                task_run_id=task_run_id, kind="screenshot", category="deliverable",
                filename=f"http-evidence-{upload.digest}.png", metadata_json=json.dumps(metadata)), upload)


def _matches(conn, source: dict, item: dict) -> bool:
    from .verification_requests import _artifact_deliverable
    try:
        if source.get("kind") == "flow":
            if item.get("flow_id") != source.get("flow_id"):
                return False
            snapshot = resolve_flow_evidence(conn, source["project_id"], source["flow_id"])
            if any(source.get(key) != snapshot[key] for key in ("project_id", "flow_id", "revision", "representation", "record_sha256")):
                return False
            if not flow_metadata_allowed(conn, snapshot, public=True):
                return False
            for side in SIDES:
                projection, selected = snapshot["projections"][side], source.get("selections", {}).get(side)
                if projection is None:
                    if selected is not None:
                        return False
                    continue
                if (not selected or selected.get("sha256") != projection["sha256"] or selected.get("size") != projection["size"]
                        or type(selected.get("offset")) is not int or type(selected.get("length")) is not int
                        or not 0 <= selected["offset"] < selected["offset"] + selected["length"] <= projection["size"]
                        or not _artifact_deliverable(conn, _artifact(conn, source["project_id"], projection["body_artifact_id"]))):
                    return False
            for excerpt in item.get("excerpts", []):
                selected = source.get("selections", {}).get(excerpt.get("side"), {})
                if (excerpt.get("revision") != source.get("revision") or excerpt.get("sha256") != selected.get("sha256")
                        or excerpt.get("representation") != source.get("representation")
                        or excerpt.get("pointer") != f"utf8-bytes={selected.get('offset')}:{selected.get('offset', 0) + selected.get('length', 0)}"):
                    return False
            return True
        side = source.get("side")
        if side not in SIDES or not isinstance(item.get(side), dict) or item[side].get("artifact_id") != source.get("artifact_id"):
            return False
        expected = prepare_render_input(conn, source["project_id"], {key: source[key] for key in ("kind", "artifact_id", "sha256", "side", "offset", "length")}, historical=True)
        return source == expected["source"]
    except (HTTPException, OSError):
        return False


def list_display_images(conn, project_id: str, evidence_items: list[dict], *, task_run_id: str | None = None) -> list[dict]:
    images = []
    rows = conn.execute("SELECT * FROM artifacts WHERE project_id = ? AND kind = 'screenshot' ORDER BY created_at, id", (project_id,)).fetchall()
    for artifact in rows:
        if task_run_id is not None and artifact["task_run_id"] != task_run_id:
            continue
        if not is_optional_http_image(artifact):
            continue
        marker = json.loads(artifact["metadata_json"])[METADATA_KEY]
        if marker["source"].get("project_id") != project_id:
            continue
        item = next((item for item in evidence_items if _matches(conn, marker["source"], item)), None)
        if item is None:
            continue
        image = {"artifact_id": artifact["id"], "caption": f"HTTP 数据包展示 · 第 {marker['page']}/{marker['page_count']} 页",
                 "content_type": "image/png", "evidence_item_id": item.get("id")}
        try:
            _read_image_bytes(artifact)
        except (HTTPException, OSError):
            image["unavailable_reason"] = "可选数据包图片不可用，正文与原始证据仍可阅读。"
        images.append(image)
    return images


def _read_image_bytes(artifact) -> bytes:
    with _artifact_disk_path_from_row(artifact).open("rb") as stream:
        content = stream.read(MAX_PNG_BYTES + 1)
    validate_png(content)
    if len(content) != artifact["size"] or sha256(content).hexdigest() != artifact["sha256"]:
        raise HTTPException(409, "http_image_content_changed")
    return content


def read_display_image(conn, project_id: str, artifact_id: str, evidence_items: list[dict], *, task_run_id: str | None = None) -> bytes:
    artifact = _artifact(conn, project_id, artifact_id)
    if not is_optional_http_image(artifact) or (task_run_id is not None and artifact["task_run_id"] != task_run_id):
        raise HTTPException(404, "http_image_not_found")
    source = json.loads(artifact["metadata_json"])[METADATA_KEY]["source"]
    if source.get("project_id") != project_id or not any(_matches(conn, source, item) for item in evidence_items):
        raise HTTPException(404, "http_image_not_found")
    return _read_image_bytes(artifact)
