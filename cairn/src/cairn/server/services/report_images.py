"""只读成品里的展示图片；来源固定在接受时的快照，不补配后来采集的材料。"""
from __future__ import annotations

import json
import re

from fastapi import HTTPException

from .assessment_reports import _artifact_content_bytes, _artifact_row
from .http_evidence_render import METADATA_KEY, _read_image_bytes, is_optional_http_image


def report_display_images(conn, project_id: str, artifact_id: str) -> list[dict]:
    error = "report_image_source_unavailable"
    report = _artifact_row(conn, project_id, artifact_id, error_code=error)
    receipt = conn.execute(
        "SELECT * FROM assessment_report_finish_receipts WHERE project_id = ? AND artifact_id = ?",
        (project_id, artifact_id),
    ).fetchone()
    if receipt is None:
        # 没有冻结图合同的历史成品只读原文，不追补现场图片。
        return []
    if report["sha256"] != receipt["artifact_sha256"] or report["task_run_id"] != receipt["task_run_id"]:
        raise HTTPException(409, error)
    snapshot = _artifact_row(conn, project_id, receipt["input_snapshot_artifact_id"], error_code=error)
    if snapshot["sha256"] != receipt["input_snapshot_sha256"]:
        raise HTTPException(409, error)
    packet = json.loads(_artifact_content_bytes(snapshot, error_code=error))["report_packet"]
    if packet["project"]["id"] != project_id:
        raise HTTPException(409, error)
    text = _artifact_content_bytes(report, error_code=error).decode("utf-8")
    identifiers = list(dict.fromkeys(re.findall(r"!\[[^\]\n]*\]\(arena-image:([A-Za-z0-9_-]+)\)", text)))
    frozen = {image["artifact_id"]: image for image in packet.get("display_images", [])}
    sources = [value["source"] for value in packet.get("http_evidence_render_sources", {}).values()]
    images = []
    for identifier in identifiers:
        image = {"artifact_id": identifier, "caption": "HTTP 数据包展示", "content_type": "image/png"}
        row = conn.execute("SELECT * FROM artifacts WHERE project_id = ? AND id = ?", (project_id, identifier)).fetchone()
        allowed = False
        if row is not None and is_optional_http_image(row):
            marker = json.loads(row["metadata_json"])[METADATA_KEY]
            if identifier in frozen:
                allowed = row["sha256"] == frozen[identifier]["sha256"]
                image.update(frozen[identifier])
            else:
                allowed = row["task_run_id"] == report["task_run_id"] and marker["source"] in sources
            image["caption"] = f"HTTP 数据包展示 · 第 {marker['page']}/{marker['page_count']} 页"
        if allowed:
            try:
                _read_image_bytes(row)
            except (HTTPException, OSError):
                allowed = False
        if not allowed:
            image["unavailable_reason"] = "可选数据包图片不可用，正文与原始证据仍可阅读。"
        images.append(image)
    return images


def report_display_image_content(conn, project_id: str, artifact_id: str, image_id: str) -> bytes:
    image = next((image for image in report_display_images(conn, project_id, artifact_id)
                  if image["artifact_id"] == image_id), None)
    if image is None or image.get("unavailable_reason"):
        raise HTTPException(404, "report_image_unavailable")
    artifact = _artifact_row(conn, project_id, image_id, error_code="report_image_unavailable")
    return _read_image_bytes(artifact)
