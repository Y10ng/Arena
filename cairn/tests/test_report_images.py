"""在自建 PostgreSQL 中验证成品图片消费，不读取业务材料或调用模型。"""

from hashlib import sha256
import json
import struct
import zlib

from fastapi import HTTPException
import pytest

from cairn.server.services import assessment_reports, audit_artifacts
from cairn.server.services.http_evidence_render import METADATA_KEY, RENDERER
from cairn.server.services.report_images import report_display_image_content, report_display_images
from test_report_inventory_postgres import _artifact, _receipt, inventory_conn
from test_report_templates import defaults_v60_conn, private_postgres


_NOW = "2026-10-28T00:00:00Z"


def _png() -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1440, 1000, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\x00" + b"\xff" * (1440 * 3)) * 1000)) + chunk(b"IEND", b""))


def test_report_images_require_receipt_frozen_identity_and_preserve_required_hash_errors(inventory_conn, tmp_path, monkeypatch):
    conn = inventory_conn
    # 仅替换文件根目录；真实 SQL、路径准出及完整性校验全部保留。
    monkeypatch.setattr(assessment_reports, "artifact_root", lambda: tmp_path)
    monkeypatch.setattr(audit_artifacts, "artifact_root", lambda: tmp_path)
    accepted = _artifact(conn, "accepted")
    previous = _artifact(conn, "previous")
    foreign = _artifact(conn, "foreign", project_id="p2")
    png = _png()
    source = {"kind": "flow", "project_id": "p1", "flow_id": "synthetic-flow", "revision": 3,
              "representation": "http-exchange-v1", "record_sha256": "a" * 64,
              "task_run_id": previous["task_run_id"], "state": "partial", "selections": {}}

    def insert_artifact(identifier, run_id, content, *, kind="screenshot", metadata=None, project_id="p1"):
        suffix = ".png" if kind == "screenshot" else ".json"
        filename = identifier + suffix
        path = tmp_path / "projects" / project_id / "runs" / run_id / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        digest = sha256(content).hexdigest()
        conn.execute(
            """INSERT INTO artifacts
               (id, project_id, task_run_id, kind, category, uri, sha256, size, created_at, metadata_json)
               VALUES (?, ?, ?, ?, 'deliverable', ?, ?, ?, ?, ?)""",
            (identifier, project_id, run_id, kind,
             f"artifact://projects/{project_id}/runs/{run_id}/{filename}", digest, len(content), _NOW,
             json.dumps(metadata or {})),
        )
        return path, digest

    def image(identifier, run_id, *, bound_source=None, project_id="p1"):
        return insert_artifact(identifier, run_id, png, project_id=project_id, metadata={METADATA_KEY: {
            "version": 1, "renderer": RENDERER, "optional": True,
            "source": source if bound_source is None else bound_source,
            "page": 1, "page_count": 1, "truncated": False,
        }})

    frozen_path, frozen_digest = image("frozen-image", previous["task_run_id"])
    generated_path, _ = image("generated-image", accepted["task_run_id"])
    image("other-run", previous["task_run_id"])
    image("wrong-source", accepted["task_run_id"], bound_source=source | {"revision": 4})
    image("unreferenced", accepted["task_run_id"])
    image("foreign-image", foreign["task_run_id"], project_id="p2")
    packet = {"project": {"id": "p1"}, "display_images": [{
        "artifact_id": "frozen-image", "sha256": frozen_digest,
        "caption": "合成冻结图", "content_type": "image/png", "evidence_item_id": "exchange",
    }], "http_evidence_render_sources": {"flow:synthetic-flow": {"source": source}}}
    snapshot_content = json.dumps({"report_packet": packet}).encode()
    snapshot_path, snapshot_digest = insert_artifact(accepted["input_snapshot_artifact_id"],
                                                    accepted["task_run_id"], snapshot_content, kind="file")
    identifiers = ["frozen-image", "generated-image", "other-run", "wrong-source", "foreign-image", "missing-image"]
    report_content = ("# 合成成品\n\n" + "\n".join(f"![合成图片](arena-image:{item})" for item in identifiers)
                      + "\n![重复引用](arena-image:generated-image)\n").encode()
    report = conn.execute("SELECT * FROM artifacts WHERE id = ?", (accepted["artifact_id"],)).fetchone()
    report_path = audit_artifacts._artifact_disk_path_from_row(report)
    report_path.write_bytes(report_content)
    report_digest = sha256(report_content).hexdigest()
    conn.execute("UPDATE artifacts SET size = ?, sha256 = ? WHERE id = ?",
                 (len(report_content), report_digest, accepted["artifact_id"]))
    accepted.update(artifact_sha256=report_digest, input_snapshot_sha256=snapshot_digest)

    # 没有 receipt 的历史成品不按现场来源补配图片。
    assert report_display_images(conn, "p1", accepted["artifact_id"]) == []
    _receipt(conn, accepted)
    images = report_display_images(conn, "p1", accepted["artifact_id"])
    assert [item["artifact_id"] for item in images] == identifiers
    by_id = {item["artifact_id"]: item for item in images}
    for identifier in identifiers[:2]:
        assert "unavailable_reason" not in by_id[identifier]
        assert report_display_image_content(conn, "p1", accepted["artifact_id"], identifier) == png
    assert by_id["frozen-image"]["sha256"] == frozen_digest
    for identifier in identifiers[2:]:
        assert "正文与原始证据仍可阅读" in by_id[identifier]["unavailable_reason"]
    for identifier in [*identifiers[2:], "unreferenced"]:
        with pytest.raises(HTTPException) as rejected:
            report_display_image_content(conn, "p1", accepted["artifact_id"], identifier)
        assert (rejected.value.status_code, rejected.value.detail) == (404, "report_image_unavailable")

    conn.execute("UPDATE artifacts SET sha256 = ? WHERE id = 'frozen-image'", ("f" * 64,))
    assert report_display_images(conn, "p1", accepted["artifact_id"])[0]["unavailable_reason"]
    conn.execute("UPDATE artifacts SET sha256 = ? WHERE id = 'frozen-image'", (frozen_digest,))
    frozen_path.unlink()
    images = report_display_images(conn, "p1", accepted["artifact_id"])
    assert images[0]["unavailable_reason"] and "unavailable_reason" not in images[1]
    assert report_path.read_bytes() == report_content and snapshot_path.read_bytes() == snapshot_content
    frozen_path.write_bytes(png)
    # 保持长度及 PNG 头不变，只改变内容，以实际触发图片 hash 检查。
    generated_path.write_bytes(png[:-1] + bytes([png[-1] ^ 1]))
    assert report_display_images(conn, "p1", accepted["artifact_id"])[1]["unavailable_reason"]
    generated_path.write_bytes(png)

    def require_source_error(artifact_id=accepted["artifact_id"]):
        for read in (lambda: report_display_images(conn, "p1", artifact_id),
                     lambda: report_display_image_content(conn, "p1", artifact_id, "generated-image")):
            with pytest.raises(HTTPException) as invalid:
                read()
            assert (invalid.value.status_code, invalid.value.detail) == (409, "report_image_source_unavailable")

    # receipt 中的报告 hash、run 与冻结 snapshot hash 均不可被现场版本替代。
    for artifact_id, column, altered, original in (
        (accepted["artifact_id"], "sha256", "f" * 64, report_digest),
        (accepted["artifact_id"], "task_run_id", previous["task_run_id"], accepted["task_run_id"]),
        (accepted["input_snapshot_artifact_id"], "sha256", "f" * 64, snapshot_digest),
    ):
        conn.execute(f"UPDATE artifacts SET {column} = ? WHERE id = ?", (altered, artifact_id))
        require_source_error()
        conn.execute(f"UPDATE artifacts SET {column} = ? WHERE id = ?", (original, artifact_id))

    def additional_report(identifier):
        value = _artifact(conn, identifier)
        row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (identifier,)).fetchone()
        path = audit_artifacts._artifact_disk_path_from_row(row)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(report_content)
        conn.execute("UPDATE artifacts SET sha256 = ?, size = ? WHERE id = ?",
                     (report_digest, len(report_content), identifier))
        value["artifact_sha256"] = report_digest
        return value

    missing_snapshot = additional_report("missing-snapshot-report")
    _receipt(conn, missing_snapshot)
    require_source_error(missing_snapshot["artifact_id"])

    for path, original in ((report_path, report_content), (snapshot_path, snapshot_content)):
        path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        require_source_error()
        path.write_bytes(original)

    # 即使文件与 receipt 的 hash 一致，快照也必须绑定本项目。
    foreign_packet = packet | {"project": {"id": "p2"}}
    foreign_content = json.dumps({"report_packet": foreign_packet}).encode()
    foreign_snapshot = additional_report("foreign-snapshot-report")
    _, foreign_digest = insert_artifact(foreign_snapshot["input_snapshot_artifact_id"],
                                       foreign_snapshot["task_run_id"], foreign_content, kind="file")
    foreign_snapshot["input_snapshot_sha256"] = foreign_digest
    _receipt(conn, foreign_snapshot)
    require_source_error(foreign_snapshot["artifact_id"])
