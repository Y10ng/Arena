"""flow 契约、零 Artifact 解析与仅材料交付重试的直接检查。"""

import base64
from contextlib import contextmanager
import gzip
from hashlib import sha256
import io
import json
import zlib

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from cairn.evidence_contracts import EvidenceItem, WorkerEvidenceItem, evidence_refs, has_evidence
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.tasks.artifact_records import resolve_declared_evidence_items
from cairn.dispatcher.tasks.common_base import ProcessArtifactRecord
from cairn.dispatcher.tasks.conclude_writer import retry_graph_write
from cairn.dispatcher.tasks.write_outcomes import classify_write_response


def test_flow_contract_and_artifact_resolution():
    value = {"id": "interaction", "kind": "http", "source": "capture", "flow_id": "flow-1",
             "excerpts": [{"side": "response", "pointer": "utf8-bytes=0:4", "representation": "http-evidence-v1",
                           "revision": 2, "sha256": "a" * 64}]}
    for model in (EvidenceItem, WorkerEvidenceItem):
        parsed = model.model_validate(value)
        Draft202012Validator(model.model_json_schema()).validate(value)
        assert has_evidence([parsed], current=True)
        assert evidence_refs([parsed]) == []
        assert not has_evidence([parsed.model_copy(update={"source": "historical"})], current=True)
        for invalid in (
            {**value, "kind": "artifact"}, {**value, "source": "manual"},
            {**value, "response": {"artifact_id": "a1"}},
            {**value, "excerpts": [{"side": "response", "pointer": "utf8-bytes=0:4"}]},
        ):
            with pytest.raises(ValidationError):
                model.model_validate(invalid)
            assert list(Draft202012Validator(model.model_json_schema()).iter_errors(invalid))
    resolved = resolve_declared_evidence_items([WorkerEvidenceItem.model_validate(value)], ProcessArtifactRecord(refs=[]), require_current_run=True)
    assert resolved[0].model_dump(exclude_defaults=True) == EvidenceItem.model_validate(value).model_dump(exclude_defaults=True)
    with pytest.raises(ValidationError):
        EvidenceItem.model_validate({**value, "response_text": "HTTP/1.1 200 OK"})
    assert EvidenceItem.model_validate({"id": "legacy", "kind": "http", "source": "historical", "response_text": "原文"}).response_text == "原文"


def test_flow_delivery_wait_retries_only_original_write(monkeypatch):
    sleeps, calls = [], []
    monkeypatch.setattr("cairn.dispatcher.tasks.conclude_writer.time.sleep", sleeps.append)
    pending = ApiResult(status_code=409, data={"detail": "traffic_evidence_pending"}, text="")
    success = ApiResult(status_code=200, data={"fact": {"id": "f1"}}, text="")
    def write():
        calls.append(True)
        return pending if len(calls) < 4 else success
    assert retry_graph_write(write).ok
    assert len(calls) == 4 and sleeps == [0.5, 1.0, 2.0]
    calls.clear()
    def exhausted():
        calls.append(True)
        return pending
    result = retry_graph_write(exhausted)
    assert len(calls) == 12
    assert classify_write_response(result).error_type == "business_acceptance"
    missing = ApiResult(status_code=404, data={"detail": "traffic_flow_not_found"}, text="")
    assert not classify_write_response(missing).retry_same_write


def readable_message(encoding="identity", media="text/plain; charset=utf-8"):
    def b64(value):
        return base64.b64encode(value.encode()).decode()
    return {"http_version": "HTTP/1.1", "status_code": 200, "reason_base64": b64("OK"),
            "headers_base64": [[b64("Content-Type"), b64(media)], [b64("Content-Encoding"), b64(encoding)]]}


@pytest.mark.parametrize("encoding,encode", [
    ("identity", lambda value: value), ("gzip", gzip.compress), ("deflate", zlib.compress),
    ("deflate", lambda value: zlib.compress(value)[2:-4]),
    ("gzip, identity, deflate", lambda value: zlib.compress(gzip.compress(value))),
    ("gzip", lambda value: gzip.compress(value[:5]) + gzip.compress(value[5:])),
])
def test_readable_content_codings(encoding, encode):
    from cairn.server.services.flow_evidence import _readable_body
    body = '合成小正文，不能被压缩占位替代。'.encode()
    result = _readable_body(io.BytesIO(encode(body)), readable_message(encoding))
    assert result["decode_status"] == "complete"
    assert result["body_text"].encode() == body
    assert result["decoded_body_bytes"] == len(body)


@pytest.mark.parametrize("extra", [0, 1])
@pytest.mark.parametrize("encoding,encode", [("identity", lambda value: value), ("gzip", gzip.compress)])
def test_readable_ten_mib_body_boundary(extra, encoding, encode):
    from cairn.server.services.flow_evidence import _readable_body, READABLE_BODY_BYTES, PROJECTION_BYTES
    source = b"x" * (READABLE_BODY_BYTES + extra)
    result = _readable_body(io.BytesIO(encode(source)), readable_message(encoding))
    assert result["decode_status"] == ("too-large" if extra else "complete")
    assert len(result["body_text"].encode()) == READABLE_BODY_BYTES
    assert result["decoded_body_bytes"] == READABLE_BODY_BYTES + extra
    assert PROJECTION_BYTES == 65536


@pytest.mark.parametrize("encoding,body,media,status", [
    ("gzip", b"broken", "text/plain", "invalid-encoding"),
    ("gzip", gzip.compress(b"valid")[:-1], "text/plain", "invalid-encoding"),
    ("gzip", gzip.compress(b"valid") + b"junk", "text/plain", "invalid-encoding"),
    ("deflate", zlib.compress(b"valid")[:-1], "text/plain", "invalid-encoding"),
    ("br", b"opaque", "text/plain", "unsupported-encoding"),
    ("gzip", gzip.compress(b"text"), "application/octet-stream", "non-text"),
])
def test_readable_failures_are_not_size_limits(encoding, body, media, status):
    from cairn.server.services.flow_evidence import _readable_body
    result = _readable_body(io.BytesIO(body), readable_message(encoding, media))
    assert result["decode_status"] == status
    assert result["body_text"] == ""


@pytest.mark.parametrize("encoding,encode", [("identity", lambda value: value), ("gzip", gzip.compress)])
def test_readable_escapes_only_derived_text_and_bounds_expansion(encoding, encode, monkeypatch):
    from cairn.server.services import flow_evidence as evidence
    original = '有效正文\\xFF'.encode() + b'\xff\x00\t\n' + '结尾'.encode()
    result = evidence._readable_body(io.BytesIO(encode(original)), readable_message(encoding))
    assert result["decode_status"] == "complete" and result["text_escaped"]
    assert result["body_text"] == '有效正文\\\\xFF\\xff\\u0000\t\n结尾'
    assert not result["display_truncated"]
    assert result["decoded_body_bytes"] == len(original)
    monkeypatch.setattr(evidence, "READABLE_BODY_BYTES", 32)
    expanded = evidence._readable_body(io.BytesIO(encode(b'prefix' + b'\x00' * 12)), readable_message(encoding))
    assert expanded["decode_status"] == "complete"
    assert expanded["text_escaped"] and expanded["display_truncated"]
    assert expanded["body_text"] == 'prefix' + '\\u0000' * 4
    empty = evidence._readable_body(io.BytesIO(encode(b'')), readable_message(encoding))
    assert empty["decode_status"] == "complete" and empty["body_text"] == ''
    assert not empty["text_escaped"]


def test_readable_endpoint_keeps_original_and_v1_identity(tmp_path, monkeypatch):
    from cairn.server.services import flow_evidence as evidence
    original = gzip.compress(b'{"synthetic":true}')
    path = tmp_path / "synthetic.gz"
    path.write_bytes(original)
    digest = sha256(original).hexdigest()
    body = {"artifact_id": "body", "sha256": digest, "size": len(original), "revision": 1}
    message = {**readable_message("gzip", "application/json"), "body_sealed": True,
               "body_revision": 1, "body_bytes": len(original)}
    record = {"revision": 1, "state": "complete", "response": message}
    row = {"state": "complete", "record_json": json.dumps(record)}
    class Connection:
        def execute(self, query, params=()):
            self.value = body if "http_flow_bodies" in query else row
            return self
        def fetchone(self):
            return self.value
    @contextmanager
    def connection():
        yield Connection()
    monkeypatch.setattr(evidence, "get_conn", connection)
    monkeypatch.setattr(evidence, "_artifact", lambda *args: body)
    monkeypatch.setattr(evidence, "_artifact_disk_path_from_row", lambda _: path)
    before = evidence._projection(None, "project", message, "response", body)
    snapshot = {"project_id": "project", "flow_id": "flow", "scope": {}, "revision": 1,
                "state": "complete", "record": record, "projections": {"response": before}}
    frozen = evidence.read_frozen_flow_projection(None, snapshot, "response")
    result = evidence.read_flow_readable("project", "flow", "response")
    assert result["body_text"] == '{"synthetic":true}'
    assert result["header_text"].encode() == evidence.projection_header(message, "response")
    assert "Content-Encoding: gzip" in result["header_text"]
    assert result["representation"] == "http-readable" and result["excerpt_eligible"] is False
    assert not {"sha256", "size", "offset", "pointer"}.intersection(result)
    assert evidence.read_frozen_flow_projection(None, snapshot, "response") == frozen
    assert evidence._projection(None, "project", message, "response", body) == before
    assert before["body_display"] == "compressed" and path.read_bytes() == original
    evidence.validate_flow_excerpt(None, snapshot, {"side": "response", "representation": "http-evidence-v1",
        "revision": 1, "sha256": before["sha256"], "pointer": "utf8-bytes=0:4"})
    row["state"] = "receiving"
    assert evidence.read_flow_readable("project", "flow", "response")["decode_status"] == "pending"


@pytest.mark.parametrize("encoding,encode", [
    ("gzip", gzip.compress), ("deflate", zlib.compress),
    ("gzip, deflate", lambda value: zlib.compress(gzip.compress(value))),
])
@pytest.mark.parametrize("extra", [0, 1])
def test_readable_auto_decoding_budget(encoding, encode, extra):
    from cairn.server.services import flow_evidence as evidence

    size = evidence.AUTO_READABLE_BODY_BYTES + extra
    result = evidence._readable_body(io.BytesIO(encode(b"x" * size)), readable_message(encoding), mode="auto")
    assert result["read_mode"] == "auto" and result["body_limit"] == 262144
    assert result["decode_status"] == ("deferred" if extra else "complete")
    assert result["deferred_reason"] == ("decoded-size" if extra else None)
    assert result["body_text"] == ("" if extra else "x" * size)
    assert not result["display_truncated"]


@pytest.mark.parametrize("extra", [0, 1])
def test_readable_auto_escape_budget(extra):
    from cairn.server.services import flow_evidence as evidence

    # 无效字节每个展示为四字节 token；精确预算允许自动读取，再多一个则不传前缀。
    original = b"\xff" * (evidence.AUTO_READABLE_BODY_BYTES // 4 + extra)
    result = evidence._readable_body(io.BytesIO(original), readable_message(), mode="auto")
    assert result["decode_status"] == ("deferred" if extra else "complete")
    assert result["deferred_reason"] == ("display-size" if extra else None)
    assert result["body_text"] == ("" if extra else "\\xff" * len(original))
    assert result["text_escaped"] and not result["display_truncated"]
    manual = evidence._readable_body(io.BytesIO(original), readable_message())
    assert manual["decode_status"] == "complete" and manual["read_mode"] == "manual"
    assert manual["body_text"] == "\\xff" * len(original)


@pytest.fixture
def readable_flow(tmp_path, monkeypatch):
    from cairn.server.services import flow_evidence as evidence

    path = tmp_path / "synthetic-body"
    body = {"artifact_id": "body", "revision": 1}
    message = {**readable_message(), "body_sealed": True, "body_revision": 1}
    record = {"revision": 1, "state": "complete", "response": message}
    row = {"state": "complete"}
    calls = []

    class Connection:
        def execute(self, query, params=()):
            calls.append((query, params))
            self.value = body if "http_flow_bodies" in query else {**row, "record_json": json.dumps(record)}
            return self

        def fetchone(self):
            return self.value

    @contextmanager
    def connection():
        yield Connection()

    def store(raw, encoding="identity"):
        path.write_bytes(raw)
        body.update(size=len(raw), sha256=sha256(raw).hexdigest())
        message.update(readable_message(encoding), body_bytes=len(raw))

    monkeypatch.setattr(evidence, "get_conn", connection)
    monkeypatch.setattr(evidence, "_artifact", lambda *args: body)
    monkeypatch.setattr(evidence, "_artifact_disk_path_from_row", lambda _: path)
    store(b"synthetic")
    return store, message, record, row, calls


def test_readable_auto_large_raw_and_pending_never_open_or_project(readable_flow, monkeypatch):
    from cairn.server.services import flow_evidence as evidence

    store, message, record, row, calls = readable_flow
    store(b"x" * (evidence.AUTO_READABLE_BODY_BYTES + 1))

    def forbidden(*args):
        pytest.fail("自动大包或未同步正文不应扫描原件或投影")

    monkeypatch.setattr(evidence, "_artifact", forbidden)
    monkeypatch.setattr(evidence, "_projection", forbidden)
    monkeypatch.setattr(evidence, "_artifact_disk_path_from_row", forbidden)
    result = evidence.read_flow_readable("project", "flow", "response", mode="auto")
    assert result["decode_status"] == "deferred" and result["deferred_reason"] == "raw-size"
    assert result["body_text"] == "" and result["decoded_body_bytes"] is None
    assert result["body_bytes"] == message["body_bytes"]
    assert result["header_text"].encode() == evidence.projection_header(message, "response")
    assert calls[-1][1] == ("project", "flow", "response")
    row["state"] = "receiving"
    pending = evidence.read_flow_readable("project", "flow", "response", mode="auto")
    assert pending["decode_status"] == "pending" and pending["body_text"] == ""
    assert pending["deferred_reason"] is None


@pytest.mark.parametrize("original,encoding,status,reason", [
    (b"small", "identity", "complete", None),
    (gzip.compress(b"x" * (262144 + 1)), "gzip", "deferred", "decoded-size"),
    (b"\x00" * 50000, "identity", "deferred", "display-size"),
    (b"broken", "gzip", "invalid-encoding", None),
])
def test_readable_auto_route_and_manual_default(readable_flow, monkeypatch, original, encoding, status, reason):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from cairn.server.auth import require_tenant_project
    from cairn.server.routers.traffic import project_router
    from cairn.server.services import flow_evidence as evidence

    store, *_ = readable_flow
    store(original, encoding)
    projections = []
    original_projection = evidence._projection

    def projection(*args):
        projections.append(True)
        return original_projection(*args)

    monkeypatch.setattr(evidence, "_projection", projection)
    app = FastAPI()
    app.include_router(project_router)
    app.dependency_overrides[require_tenant_project] = lambda: None
    with TestClient(app) as client:
        url = "/projects/project/traffic/flow/readable/response"
        response = client.get(url, params={"mode": "auto"})
        assert response.status_code == 200
        result = response.json()
        assert result["decode_status"] == status and result["deferred_reason"] == reason
        assert result["body_text"] == ("small" if status == "complete" else "")
        assert result["body_limit"] == 262144 and not projections
        assert response.headers["Cache-Control"] == "private, no-store"
        assert client.get(url, params={"mode": "invalid"}).status_code == 422
        manual = client.get(url).json()
        assert manual["read_mode"] == "manual" and manual["body_limit"] == 10 * 1024 * 1024
        assert manual["decode_status"] == ("invalid-encoding" if status == "invalid-encoding" else "complete")
        assert len(projections) == 1


def test_flow_detail_headers_do_not_read_body(monkeypatch):
    from cairn.server.services import traffic
    from cairn.server.services.flow_evidence import projection_header

    message = {**readable_message(), "method": "GET", "target_base64": "Lw=="}
    view = {"request": message, "response": None, "sync": {"bodies": {
        "request": {"accepted_bytes": 4, "captured_bytes": 9}, "response": {"status": "absent"},
    }}}

    class Connection:
        def execute(self, *args):
            return self

        def fetchall(self):
            return []

    @contextmanager
    def connection():
        yield Connection()

    monkeypatch.setattr(traffic, "get_conn", connection)
    monkeypatch.setattr(traffic, "get_project_or_404", lambda *args: None)
    monkeypatch.setattr(traffic, "_flow", lambda *args: {})
    monkeypatch.setattr(traffic, "_view", lambda *args: view)
    monkeypatch.setattr(traffic, "traffic_request_address", lambda *args: {})
    result = traffic.read_traffic_record("project", "flow")
    assert result["header_text"] == {"request": projection_header(message, "request").decode(), "response": None}
    assert result["sync"]["bodies"]["request"] == {"accepted_bytes": 4, "captured_bytes": 9}
