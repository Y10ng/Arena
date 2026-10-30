"""收集本次报告已落盘的段；失败材料与最终成品走不同的 Artifact 身份。"""

from __future__ import annotations

from hashlib import sha256
import json

from cairn.report_segments import (
    REPORT_MAX_SEGMENT_MANIFEST_BYTES, REPORT_MAX_SEGMENT_BYTES, REPORT_MAX_SEGMENTS,
    REPORT_MAX_SEGMENT_TOTAL_BYTES, REPORT_SEGMENT_MANIFEST_PROTOCOL,
    REPORT_SEGMENT_PROTOCOL, REPORT_SEGMENT_ADAPTER,
)
from cairn.dispatcher.worker_protocol.submission import ArtifactDeclaration
from cairn.dispatcher.tasks.artifact_records import _open_container_artifact


def capture_report_segments(client, container_manager, container_name: str, *, manifest_path: str,
                            binding: dict) -> tuple[dict[str, dict], list[dict]]:
    """即使最终提交截断也保留已保存段；没有最终引用时这些材料绝不成为报告。"""
    with _open_container_artifact(container_manager, container_name, manifest_path) as source:
        raw = source.read(REPORT_MAX_SEGMENT_MANIFEST_BYTES + 1)
    if len(raw) > REPORT_MAX_SEGMENT_MANIFEST_BYTES:
        raise ValueError("report_segment_manifest_too_large")
    if not raw:
        return {}, []
    manifest = json.loads(raw)
    if (not isinstance(manifest, dict) or set(manifest) != {"protocol", "binding", "artifacts"}
            or manifest["protocol"] != REPORT_SEGMENT_MANIFEST_PROTOCOL or manifest["binding"] != binding
            or not isinstance(manifest["artifacts"], list) or len(manifest["artifacts"]) > REPORT_MAX_SEGMENTS):
        raise ValueError("report_segment_manifest_invalid")
    declarations = [ArtifactDeclaration.model_validate(item) for item in manifest["artifacts"]]
    if sum(item.size_bytes for item in declarations) > REPORT_MAX_SEGMENT_TOTAL_BYTES:
        raise ValueError("report_segment_total_too_large")
    records: dict[str, dict] = {}
    for declaration in declarations:
        if (declaration.kind != "file" or declaration.handle in records
                or declaration.size_bytes > REPORT_MAX_SEGMENT_BYTES
                or declaration.path != "/home/kali/workspace/.cairn/artifacts/registered/" + declaration.handle):
            raise ValueError("report_segment_artifact_invalid")
        with _open_container_artifact(container_manager, container_name, declaration.path) as source:
            content = source.read(declaration.size_bytes + 1)
        if len(content) != declaration.size_bytes or sha256(content).hexdigest() != declaration.sha256:
            raise ValueError("report_segment_digest_mismatch")
        record = json.loads(content)
        if (not isinstance(record, dict) or set(record) != {"protocol", "binding", "key", "content"}
                or record["protocol"] != REPORT_SEGMENT_PROTOCOL or record["binding"] != binding):
            raise ValueError("report_segment_identity_mismatch")
        REPORT_SEGMENT_ADAPTER.validate_python(record["content"])
        response = client.create_artifact(
            binding["project_id"], task_run_id=binding["task_run_id"], kind="file", content=content,
            filename="report-segment-" + declaration.handle + ".json",
            metadata={"source": "report_segment", "phase": "report_execute", **binding,
                      "registered_handle": declaration.handle, "segment_key": record["key"],
                      "registered_sha256": declaration.sha256},
        )
        if (not response.ok or not isinstance(response.data, dict)
                or not isinstance(response.data.get("id"), str) or response.data.get("sha256") != declaration.sha256):
            raise ValueError("report_segment_capture_failed")
        records[declaration.handle] = record
    return records, [item.model_dump() for item in declarations]
