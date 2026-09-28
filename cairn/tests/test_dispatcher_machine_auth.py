from __future__ import annotations

import logging
import threading

import pytest

from cairn.dispatcher.protocol.client import ApiResult, CairnClient


class FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.mounted: list[str] = []

    def mount(self, prefix: str, _adapter) -> None:
        self.mounted.append(prefix)

    def close(self) -> None:
        pass


def test_client_session_reads_machine_token_once_and_keeps_it_out_of_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sessions: list[FakeSession] = []
    monkeypatch.setenv("CAIRN_DISPATCHER_TOKEN", "dispatcher-machine-secret")
    monkeypatch.setattr(
        "cairn.dispatcher.protocol.client.requests.Session",
        lambda: sessions.append(FakeSession()) or sessions[-1],
    )

    client = CairnClient("http://server.example")
    with caplog.at_level(logging.DEBUG):
        session = client._session()

    assert session.headers["Authorization"] == "Bearer dispatcher-machine-secret"
    assert "dispatcher-machine-secret" not in caplog.text
    assert "dispatcher-machine-secret" not in repr(client)


def test_client_requires_machine_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAIRN_DISPATCHER_TOKEN", raising=False)
    with pytest.raises(ValueError, match="CAIRN_DISPATCHER_TOKEN"):
        CairnClient("http://server.example")


def test_task_run_client_allows_metadata_only_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CairnClient("http://server.example", token="synthetic-test-only")
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs):
        captured.update(method=method, path=path, **kwargs)
        return ApiResult(status_code=200, data={"status": "running"})

    monkeypatch.setattr(client, "_request_json", fake_request)

    response = client.update_task_run(
        "p",
        "tr-1",
        runtime_context={"execution_lifecycle": {"task_finished": True}},
    )

    assert response.ok
    assert captured == {
        "method": "PATCH",
        "path": "/projects/p/task-runs/tr-1",
        "json": {
            "status": None,
            "session_id": None,
            "exit_code": None,
            "error_type": None,
            "error_message": None,
            "runtime_context": {
                "execution_lifecycle": {"task_finished": True}
            },
        },
    }
    client.close()


def test_client_lists_artifacts_with_encoded_task_run_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CairnClient("http://server.example", token="synthetic-test-only")
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs):
        captured.update(method=method, path=path, **kwargs)
        return ApiResult(status_code=200, data=[])

    monkeypatch.setattr(client, "_request_json", fake_request)

    response = client.list_artifacts(
        "project-1",
        task_run_id="run /?#special",
    )

    assert response.ok
    assert captured == {
        "method": "GET",
        "path": "/projects/project-1/artifacts?task_run_id=run%20%2F%3F%23special",
    }
    client.close()


def test_client_heartbeat_includes_task_run_for_renewal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAIRN_DISPATCHER_TOKEN", "dispatcher-machine-secret")
    client = CairnClient("http://server.example")
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs):
        captured.update(method=method, path=path, **kwargs)
        return "result"

    monkeypatch.setattr(client, "_request_json", fake_request)

    result = client.heartbeat(
        "proj_001", "i001", "worker-1", task_run_id="tr001"
    )

    assert result == "result"
    assert captured == {
        "method": "POST",
        "path": "/projects/proj_001/intents/i001/heartbeat",
        "json": {"worker": "worker-1", "task_run_id": "tr001"},
    }


def test_client_posts_injection_payload_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAIRN_DISPATCHER_TOKEN", "dispatcher-machine-secret")
    client = CairnClient("http://server.example")
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs):
        captured.update(method=method, path=path, **kwargs)
        return "result"

    monkeypatch.setattr(client, "_request_json", fake_request)
    operations = [
        {
            "operation": "begin",
            "capture_id": "cap-1",
            "task_run_id": "tr001",
            "phase": "explore_execute",
            "injection_type": "直接注入",
            "payload_text": "payload",
            "original_format": "明文",
        }
    ]

    result = client.apply_injection_payload_operations("proj_001", operations)

    assert result == "result"
    assert captured == {
        "method": "POST",
        "path": "/projects/proj_001/injection-payloads/operations",
        "json": {"operations": operations},
    }


def test_client_starts_explore_attempt_with_atomic_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAIRN_DISPATCHER_TOKEN", "dispatcher-machine-secret")
    client = CairnClient("http://server.example")
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs):
        captured.update(method=method, path=path, **kwargs)
        return "result"

    monkeypatch.setattr(client, "_request_json", fake_request)
    result = client.start_explore_attempt(
        "proj_001",
        "i001",
        "worker-1",
        worker_type="claudecode",
        prompt_group="default-copy-v2",
        execution_profile="default",
        runtime_context={"source": "test"},
        expected_execution_generation=7,
    )

    assert result == "result"
    assert captured["method"] == "POST"
    assert captured["path"] == (
        "/projects/proj_001/intents/i001/explore-attempts"
    )
    assert captured["json"] == {
        "intent_id": "i001",
        "task_type": "explore",
        "worker_name": "worker-1",
        "worker_type": "claudecode",
        "status": "running",
        "session_id": None,
        "prompt_group": "default-copy-v2",
        "execution_profile": "default",
        "runtime_context": {"source": "test"},
        "expected_execution_generation": 7,
    }


def _execution_event_capability(**updates: object) -> dict[str, object]:
    capability: dict[str, object] = {
        "enabled": True,
        "protocol": "cairn-execution-event-v1",
        "max_events": 64,
        "max_event_bytes": 16384,
        "max_spool_bytes": 524288,
    }
    capability.update(updates)
    return capability


def test_execution_event_capability_cache_has_ttl_force_refresh_and_defensive_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAIRN_DISPATCHER_TOKEN", "dispatcher-machine-secret")
    client = CairnClient("http://server.example")
    clock = [100.0]
    calls: list[tuple[str, str]] = []

    def request(method: str, path: str, **_kwargs: object) -> ApiResult:
        calls.append((method, path))
        return ApiResult(status_code=200, data=_execution_event_capability())

    monkeypatch.setattr("cairn.dispatcher.protocol.client.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(client, "_request_json", request)

    first = client.get_execution_event_capabilities()
    assert first is not None
    first["max_events"] = 1
    assert client.get_execution_event_capabilities() == _execution_event_capability()
    assert calls == [("GET", "/execution-events/capabilities")]

    clock[0] = 130.0
    assert client.get_execution_event_capabilities() == _execution_event_capability()
    assert len(calls) == 2
    assert client.get_execution_event_capabilities(force_refresh=True) == _execution_event_capability()
    assert len(calls) == 3


def test_execution_event_capability_caches_fail_closed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAIRN_DISPATCHER_TOKEN", "dispatcher-machine-secret")
    client = CairnClient("http://server.example")
    calls: list[str] = []

    def request(_method: str, path: str, **_kwargs: object) -> ApiResult:
        calls.append(path)
        return ApiResult(status_code=503, data=_execution_event_capability())

    monkeypatch.setattr(client, "_request_json", request)
    assert client.get_execution_event_capabilities() is None
    assert client.get_execution_event_capabilities() is None
    assert calls == ["/execution-events/capabilities"]


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        _execution_event_capability(enabled=False),
        _execution_event_capability(protocol="wrong"),
        _execution_event_capability(max_events=True),
        _execution_event_capability(max_event_bytes=0),
        _execution_event_capability(max_spool_bytes="524288"),
    ],
)
def test_execution_event_capability_rejects_invalid_shapes(
    monkeypatch: pytest.MonkeyPatch,
    data: object,
) -> None:
    monkeypatch.setenv("CAIRN_DISPATCHER_TOKEN", "dispatcher-machine-secret")
    client = CairnClient("http://server.example")
    monkeypatch.setattr(
        client,
        "_request_json",
        lambda *_args, **_kwargs: ApiResult(status_code=200, data=data),
    )
    assert client.get_execution_event_capabilities() is None


def test_execution_event_capability_serializes_concurrent_first_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAIRN_DISPATCHER_TOKEN", "dispatcher-machine-secret")
    client = CairnClient("http://server.example")
    request_started = threading.Event()
    release_request = threading.Event()
    second_started = threading.Event()
    calls: list[str] = []
    results: list[dict[str, object] | None] = []

    def request(_method: str, path: str, **_kwargs: object) -> ApiResult:
        calls.append(path)
        request_started.set()
        assert release_request.wait(timeout=2)
        return ApiResult(status_code=200, data=_execution_event_capability())

    def lookup(started: threading.Event | None = None) -> None:
        if started is not None:
            started.set()
        results.append(client.get_execution_event_capabilities())

    monkeypatch.setattr(client, "_request_json", request)
    first = threading.Thread(target=lookup)
    second = threading.Thread(target=lookup, args=(second_started,))
    first.start()
    assert request_started.wait(timeout=2)
    second.start()
    assert second_started.wait(timeout=2)
    release_request.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert calls == ["/execution-events/capabilities"]
    assert results == [_execution_event_capability(), _execution_event_capability()]


def test_execution_event_capability_malformed_json_fails_closed_without_body_log(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("CAIRN_DISPATCHER_TOKEN", "dispatcher-machine-secret")
    client = CairnClient("http://server.example")

    class Response:
        status_code = 200
        text = "PRIVATE_CAPABILITY_BODY"
        headers = {"content-type": "application/json; charset=utf-8"}

        @staticmethod
        def json() -> object:
            raise ValueError("PRIVATE_JSON_ERROR")

    class Session:
        @staticmethod
        def request(method: str, url: str, **kwargs: object) -> Response:
            assert (method, url) == (
                "GET",
                "http://server.example/execution-events/capabilities",
            )
            assert kwargs == {"json": None, "timeout": 10.0}
            return Response()

    monkeypatch.setattr(client, "_session", lambda: Session())
    with caplog.at_level(logging.WARNING):
        assert client.get_execution_event_capabilities() is None
    assert "PRIVATE_" not in caplog.text


def test_client_posts_execution_event_to_task_run_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAIRN_DISPATCHER_TOKEN", "dispatcher-machine-secret")
    client = CairnClient("http://server.example")
    captured: dict[str, object] = {}
    event = {
        "protocol": "cairn-execution-event-v1",
        "producer_event_key": "checkpoint-1",
    }

    def request(method: str, path: str, **kwargs: object) -> ApiResult:
        captured.update(method=method, path=path, **kwargs)
        return ApiResult(status_code=201)

    monkeypatch.setattr(client, "_request_json", request)
    response = client.create_execution_event("project-1", "run-1", event)

    assert response.status_code == 201
    assert captured == {
        "method": "POST",
        "path": "/projects/project-1/task-runs/run-1/execution-events",
        "json": event,
    }
