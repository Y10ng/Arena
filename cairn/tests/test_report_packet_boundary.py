"""只验证报告 Packet 的失败归属和冻结工作索引，不接触项目或模型。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import requests

from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.dispatcher.tasks.report import (
    ReportPacketReadError,
    _collect_report_packet_json,
    _readable_input_references,
)


@pytest.mark.parametrize("error,code", [
    (requests.ConnectTimeout("private diagnostic"), "connect_timeout"),
    (requests.ReadTimeout("private diagnostic"), "read_timeout"),
    (requests.ConnectionError("private diagnostic"), "transport_error"),
])
def test_packet_transport_distinguishes_failure_without_retry(monkeypatch, error, code):
    client = CairnClient("http://localhost:1", token="synthetic-test-token")
    calls = []

    def request(*args, **kwargs):
        calls.append(kwargs)
        raise error

    monkeypatch.setattr(client, "_session", lambda: SimpleNamespace(request=request))
    response = client.get_assessment_report_packet("p1", generation_token="synthetic-generation")
    assert response.error_code == code
    assert response.text == code
    assert calls == [{"json": None, "timeout": 10.0}]
    monkeypatch.setattr(client, "get_assessment_report_packet", lambda *args, **kwargs: response)
    with pytest.raises(ReportPacketReadError, match=code):
        _collect_report_packet_json(client, "p1")


@pytest.mark.parametrize("response,code", [
    (ApiResult(503, data={"detail": "unavailable"}), "http_503"),
    (ApiResult(200, data={"version": 2}), "report_packet_contract_invalid"),
    (ApiResult(200, data=None), "report_packet_contract_invalid"),
])
def test_packet_http_and_contract_failures_remain_distinct(response, code):
    client = SimpleNamespace(get_assessment_report_packet=lambda *args, **kwargs: response)
    with pytest.raises(ReportPacketReadError, match=code):
        _collect_report_packet_json(client, "p1")


def test_working_packet_preserves_full_frozen_index_and_original_bytes():
    packet = {
        "vulnerabilities": [{"id": "v1", "source_fact_id": "f1"}],
        "observations": [{"id": "o1", "source_fact_id": "f1", "evidence_items": [
            {"id": "e1", "response": {"artifact_id": "a1"}},
        ]}],
        "facts": [{"id": "f1", "description": "必要原文"}, {"id": "f2", "description": "其他材料"}],
        "artifacts": [{"id": "a1"}, {"id": "a2"}],
        "artifact_contents": {"a1": {"text": "响应原件\n尾部反证"}, "a2": {"text": "其他冻结原件"}},
        "task_runs": [{"id": "t1"}], "assessment_attempts": [], "evidence_links": [],
    }
    original = json.dumps(packet, ensure_ascii=False)
    _, _, files = _readable_input_references(original, "frozen: graph")
    working = json.loads(files[0].content)
    full = json.loads(next(item.content for item in files if item.path.endswith("/packet-full.json")))
    assert "task_runs" not in working
    assert working["facts"] == packet["facts"][:1]
    assert working["artifacts"] == packet["artifacts"][:1]
    assert set(working["artifact_reads"]) == {"a1"}
    assert full["task_runs"] == packet["task_runs"]
    assert full["facts"] == packet["facts"]
    assert set(full["artifact_reads"]) == {"a1", "a2"}
    for artifact_id, content in packet["artifact_contents"].items():
        descriptor = next(item for item in files if item.path == full["artifact_reads"][artifact_id]["path"])
        assert descriptor.content.encode() == content["text"].encode()
        assert descriptor.owner == "root" and descriptor.mode == 0o444
    assert json.loads(original) == packet


def test_serve_h11_accepts_fragmented_report_candidate_manifest(monkeypatch):
    import base64

    from click.testing import CliRunner
    import h11

    from cairn import cli
    from cairn.report_segments import REPORT_MAX_MANIFEST_BYTES

    settings = {}
    monkeypatch.setattr(cli.db, "configure", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: settings.update(kwargs))
    result = CliRunner().invoke(cli.main, ["serve"])
    assert result.exit_code == 0
    parser = h11.Connection(
        h11.SERVER, max_incomplete_event_size=settings["h11_max_incomplete_event_size"],
    )
    header = base64.urlsafe_b64encode(b"a" * REPORT_MAX_MANIFEST_BYTES).rstrip(b"=")
    request = b"POST /candidates HTTP/1.1\r\nHost: localhost\r\nX-Cairn-Candidate-Manifest: " + header
    # 模拟真实网络分包；默认 h11 会在完整 header 抵达前按 16 KiB 拒绝。
    for start in range(0, len(request), 8192):
        parser.receive_data(request[start:start + 8192])
        assert parser.next_event() is h11.NEED_DATA
    parser.receive_data(b"\r\nContent-Length: 0\r\n\r\n")
    event = parser.next_event()
    assert isinstance(event, h11.Request)
    assert dict(event.headers)[b"x-cairn-candidate-manifest"] == header
