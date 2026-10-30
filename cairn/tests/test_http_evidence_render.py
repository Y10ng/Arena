"""离线展示源的合成边界检查，不读取业务库、不访问目标。"""
import asyncio
import base64
from copy import deepcopy
from hashlib import sha256
import json
import struct
import zlib

import pytest
from fastapi import HTTPException

from cairn.server.schemas.artifacts import CreateArtifactRequest, CreateRawArtifactRequest
from cairn.server.services import audit_artifacts, flow_evidence, http_evidence_render as images, verification_requests


@pytest.fixture
def source(tmp_path, monkeypatch):
    raw = b"synthetic safe body"
    path = tmp_path / "source.txt"
    path.write_bytes(raw)
    artifact = {"id": "body", "project_id": "synthetic", "task_run_id": "run", "kind": "file",
                "metadata_json": "{}", "sha256": sha256(raw).hexdigest(), "size": len(raw)}
    b64 = lambda value: base64.b64encode(value).decode()
    message = {"http_version": "HTTP/1.1", "status_code": 200, "reason_base64": b64(b"OK"), "headers_base64": []}
    text = flow_evidence.projection_header(message, "response") + raw
    projection = {"sha256": sha256(text).hexdigest(), "size": len(text), "body_artifact_id": "body", "body_display": "utf8"}
    snapshot = {"project_id": "synthetic", "flow_id": "flow", "scope": {"task_run_id": "run"}, "revision": 2,
                "representation": "http-evidence-v1", "record_sha256": "a" * 64, "state": "partial",
                "record": {"request": None, "response": message}, "projections": {"request": None, "response": projection}}
    class Connection:
        def execute(self, *_):
            return self
        def fetchall(self):
            return []
    conn = Connection()
    monkeypatch.setattr(images, "resolve_flow_evidence", lambda *_: snapshot)
    monkeypatch.setattr(images, "_artifact", lambda *_: artifact)
    monkeypatch.setattr(images, "_artifact_disk_path_from_row", lambda _: path)
    monkeypatch.setattr(verification_requests, "_artifact_disk_path_from_row", lambda _: path)
    monkeypatch.setattr(verification_requests, "_platform_secrets", lambda *_: [b"synthetic-platform-secret"])
    reads = []
    def read(_conn, _snapshot, _side, offset=0, limit=65536):
        reads.append((offset, limit))
        return {"text": text[offset:offset + limit].decode(), "next_offset": min(len(text), offset + limit)}
    monkeypatch.setattr(images, "read_frozen_flow_projection", read)
    return conn, snapshot, artifact, path, reads


def test_source_approval_is_before_text_and_preserves_identity(source):
    conn, snapshot, artifact, path, reads = source
    before = path.read_bytes()
    value = images.prepare_render_input(conn, "synthetic", {"kind": "flow", "flow_id": "flow"}, task_run_id="run")
    assert value["request_text"] is None
    assert value["source"]["revision"] == 2
    assert value["source"]["selections"]["response"]["sha256"] == snapshot["projections"]["response"]["sha256"]
    assert path.read_bytes() == before and sha256(before).hexdigest() == artifact["sha256"]
    assert len(reads) == 1


@pytest.mark.parametrize("blocked", ["header", "body", "platform"])
def test_sensitive_source_cannot_be_laundered_into_png(source, blocked):
    conn, snapshot, _, path, reads = source
    if blocked == "header":
        snapshot["record"]["response"]["headers_base64"] = [[base64.b64encode(b"Set-Cookie").decode(), base64.b64encode(b"synthetic=value").decode()]]
    else:
        path.write_bytes(b"Authorization: Bearer synthetic-test-only" if blocked == "body" else b"synthetic-platform-secret")
    with pytest.raises(HTTPException) as error:
        images.prepare_render_input(conn, "synthetic", {"kind": "flow", "flow_id": "flow"}, task_run_id="run")
    assert error.value.status_code == 403
    assert reads == []


def test_freeze_reuses_source_identity_but_rechecks_content(source):
    conn, _, artifact, path, reads = source
    checks = {}
    items = [{"id": "item", "flow_id": "flow"}]
    assert "flow:flow" in images.freeze_render_sources(conn, "synthetic", items, source_checks=checks)
    assert checks == {(artifact["project_id"], artifact["sha256"], artifact["size"]): False}
    # 来源身份判断可复用；不能因第一次允许交付而跳过后续原件秘密检查。
    reads.clear()
    path.write_bytes(b"synthetic-platform-secret")
    assert images.freeze_render_sources(conn, "synthetic", items, source_checks=checks) == {}
    assert reads == []


def test_cross_run_and_forged_excerpt_are_rejected_before_pixels(source):
    conn, snapshot, _, _, reads = source
    with pytest.raises(HTTPException) as error:
        images.prepare_render_input(conn, "synthetic", {"kind": "flow", "flow_id": "flow"}, task_run_id="other")
    assert error.value.status_code == 403
    excerpt = {"side": "response", "pointer": "utf8-bytes=0:4", "representation": "http-evidence-v1", "revision": 99, "sha256": "a" * 64}
    with pytest.raises(HTTPException):
        images.prepare_render_input(conn, "synthetic", {"kind": "flow", "flow_id": "flow", "excerpts": [excerpt]})
    assert reads == []
    good = images.prepare_render_input(conn, "synthetic", {"kind": "flow", "flow_id": "flow"})
    item = {"id": "item", "kind": "http", "flow_id": "flow"}
    assert images._matches(conn, good["source"], item)
    changed = deepcopy(good["source"])
    changed["revision"] += 1
    assert not images._matches(conn, changed, item)
    changed = deepcopy(good["source"])
    changed["selections"]["response"]["sha256"] = "0" * 64
    assert not images._matches(conn, changed, item)
    excerpt.update(revision=2, sha256=snapshot["projections"]["response"]["sha256"])
    assert not images._matches(conn, good["source"], {**item, "excerpts": [excerpt]})


def test_missing_optional_source_does_not_abort_freeze(monkeypatch):
    def missing(*_):
        raise HTTPException(404, "synthetic_missing")
    monkeypatch.setattr(images, "_artifact", missing)
    monkeypatch.setattr(images, "resolve_flow_evidence", missing)
    assert images.freeze_render_sources(None, "synthetic", [
        {"id": "artifact-item", "request": {"artifact_id": "absent"}},
        {"id": "flow-item", "flow_id": "absent"},
    ]) == {}


def test_png_requires_complete_bounded_pixels_not_only_a_header():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    header = chunk(b"IHDR", struct.pack(">IIBBBBB", 1440, 1000, 8, 2, 0, 0, 0))
    pixels = zlib.compress((b"\x00" + b"\xff" * (1440 * 3)) * 1000)
    png = b"\x89PNG\r\n\x1a\n" + header + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")
    images.validate_png(png)
    for invalid in (png[:24], png[:-1], png + b"untrusted", png[:40] + b"changed" + png[47:]):
        with pytest.raises(HTTPException):
            images.validate_png(invalid)


def test_reserved_metadata_cannot_be_registered_by_generic_upload():
    marker = {images.METADATA_KEY: {"version": 1, "renderer": images.RENDERER, "optional": True}}
    with pytest.raises(HTTPException) as error:
        audit_artifacts.create_artifact_record("synthetic", CreateArtifactRequest(kind="screenshot", content="synthetic", metadata=marker))
    assert error.value.detail == "artifact_reserved_metadata"
    async def chunks():
        yield b"synthetic"
    with pytest.raises(HTTPException) as error:
        asyncio.run(audit_artifacts.create_raw_artifact_record("synthetic", CreateRawArtifactRequest(kind="screenshot", metadata_json=json.dumps(marker)), chunks()))
    assert error.value.detail == "artifact_reserved_metadata"
    assert images.is_optional_http_image({"kind": "screenshot", "metadata": marker})
    assert not images.is_optional_http_image({"kind": "screenshot", "metadata": {}})


def test_missing_image_is_local_feedback_not_missing_original(source, monkeypatch):
    conn, _, _, path, _ = source
    value = images.prepare_render_input(conn, "synthetic", {"kind": "flow", "flow_id": "flow"})
    artifact = {"id": "image", "kind": "screenshot", "task_run_id": "run", "size": 24, "sha256": "a" * 64,
                "metadata_json": json.dumps({images.METADATA_KEY: {"version": 1, "renderer": images.RENDERER,
                    "optional": True, "source": value["source"], "page": 1, "page_count": 1}})}
    class Connection:
        def execute(self, query, *_):
            self.rows = [artifact] if "kind = 'screenshot'" in query else []
            return self
        def fetchall(self):
            return self.rows
    monkeypatch.setattr(images, "_artifact_disk_path_from_row", lambda _: path.parent / "missing.png")
    result = images.list_display_images(Connection(), "synthetic", [{"id": "item", "flow_id": "flow"}], task_run_id="run")
    assert len(result) == 1 and result[0]["unavailable_reason"]
    assert images.list_display_images(Connection(), "synthetic", [{"id": "item", "flow_id": "flow"}], task_run_id="other") == []
