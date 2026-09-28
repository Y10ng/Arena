from __future__ import annotations

from base64 import urlsafe_b64encode

import pytest

from cairn.dispatcher.protocol.client import CairnClient


def _capability(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "required": True,
        "spool_protocol": "cairn-candidate-spool-v1",
        "frame_protocol": "cairn-candidate-frame-v1",
        "receipt_protocol": "cairn-candidate-capture-receipt-v1",
        "manifest_header": "X-Cairn-Candidate-Manifest",
        "manifest_encoding": "base64url-utf8-canonical-json-no-padding",
        "content_type": "application/octet-stream",
        "source_format": "pi-toolcall-delta-utf8-v1",
        "max_candidates_per_task_run": 16,
        "max_raw_bytes": 2 * 1024 * 1024,
        "max_manifest_bytes": 4096,
        "max_spool_bytes": 34 * 1024 * 1024,
        "max_outcomes_per_candidate": 64,
        "max_artifacts_per_candidate": 16,
    }
    value.update(updates)
    return value


def test_candidate_capability_discovery_caches_defensive_fail_closed_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAIRN_DISPATCHER_TOKEN", "dispatcher-machine-secret")
    client = CairnClient("http://server.example")
    calls: list[tuple[str, str]] = []

    def request(method: str, path: str, **_kwargs: object):
        from cairn.dispatcher.protocol.client import ApiResult

        calls.append((method, path))
        return ApiResult(status_code=200, data=_capability())

    monkeypatch.setattr(client, "_request_json", request)
    first = client.get_candidate_capabilities()
    assert first == _capability()
    assert first is not None
    first["max_raw_bytes"] = 1
    assert client.get_candidate_capabilities() == _capability()
    assert calls == [("GET", "/candidates/capabilities")]

    client._candidate_capability_cache = None
    monkeypatch.setattr(
        client,
        "_request_json",
        lambda *_args, **_kwargs: type(
            "Result", (), {"ok": True, "data": _capability(required=False)}
        )(),
    )
    assert client.get_candidate_capabilities() is None


@pytest.mark.parametrize(
    "updates",
    [
        {"required": 1},
        {"required": 1.0},
        {"spool_protocol": "wrong"},
        {"receipt_protocol": "wrong"},
        {"manifest_encoding": "base64"},
        {"max_raw_bytes": True},
        {"max_manifest_bytes": 0},
        {"max_outcomes_per_candidate": "16"},
    ],
)
def test_candidate_capability_rejects_incompatible_contract(
    monkeypatch: pytest.MonkeyPatch,
    updates: dict[str, object],
) -> None:
    monkeypatch.setenv("CAIRN_DISPATCHER_TOKEN", "dispatcher-machine-secret")
    client = CairnClient("http://server.example")
    monkeypatch.setattr(
        client,
        "_request_json",
        lambda *_args, **_kwargs: type(
            "Result", (), {"ok": True, "data": _capability(**updates)}
        )(),
    )
    assert client.get_candidate_capabilities() is None


def test_client_captures_exact_candidate_bytes_with_unpadded_manifest_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAIRN_DISPATCHER_TOKEN", "dispatcher-machine-secret")
    client = CairnClient("http://server.example")
    manifest = b'{"note":"\xe9\x9b\xaa","record_kind":"candidate"}'
    raw = b'{\r\n"accepted": tru, "note": "\xe9\x9b\xaa"\r\n}'
    captured: dict[str, object] = {}

    class Response:
        status_code = 201
        text = ""
        headers = {"content-type": "application/json"}

        @staticmethod
        def json() -> object:
            return {"protocol": "cairn-candidate-capture-receipt-v1"}

    class Session:
        @staticmethod
        def request(method: str, url: str, **kwargs: object) -> Response:
            captured.update(method=method, url=url, **kwargs)
            return Response()

    monkeypatch.setattr(client, "_session", lambda: Session())
    response = client.capture_candidate("project 1", "run/1", manifest, raw)

    assert response.status_code == 201
    assert response.data == {"protocol": "cairn-candidate-capture-receipt-v1"}
    assert captured == {
        "method": "POST",
        "url": "http://server.example/projects/project%201/task-runs/run%2F1/candidates",
        "data": raw,
        "headers": {
            "Content-Type": "application/octet-stream",
            "X-Cairn-Candidate-Manifest": urlsafe_b64encode(manifest)
            .rstrip(b"=")
            .decode("ascii"),
        },
        "timeout": 10.0,
    }
