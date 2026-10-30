"""控制私有读取的绑定、范围与 socket 生命周期，不启动目标执行或模型。"""

import json
import socket
from types import SimpleNamespace

import pytest

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.traffic_reader import TrafficReadBinding, TrafficReader


def record(flow_id="flow-1", run="run-1", project="project-1"):
    return {
        "id": flow_id, "project_id": project,
        "scope": {"id": "scope-1", "project_id": project, "task_run_id": run, "generation": 0},
        "revision": 3, "state": "partial", "source_state": "partial", "sync": {"pending_sides": []},
        "request": {"method": "GET", "host": "synthetic.invalid", "headers_base64": [["private", "header"]]},
        "response": None,
    }


class Client:
    def __init__(self):
        self.calls = []
        self.records = {"flow-1": record(), "old": record("old", "run-old"), "foreign": record("foreign", project="other")}

    def list_http_flows(self, project, **params):
        self.calls.append(("list", project, params))
        return ApiResult(200, {"items": [self.records["flow-1"]], "next_cursor": 1, "has_more": True})

    def read_http_flow(self, project, flow):
        self.calls.append(("metadata", project, flow))
        return ApiResult(200, self.records[flow])

    def read_http_flow_projection(self, project, flow, side, **params):
        self.calls.append(("projection", project, flow, side, params))
        return ApiResult(200, {"flow_id": flow, "project_id": project, "scope": self.records[flow]["scope"],
                               "representation": "http-evidence-v1", "side": side, "offset": params["offset"],
                               "text": "原文", "sha256": "a" * 64, "revision": 3})


def test_bound_reader_defaults_and_explicit_history():
    client = Client()
    binding = TrafficReadBinding(client, "project-1", "run-1")
    page = binding.dispatch({"method": "list_http_flows", "params": {}})
    assert client.calls == [("list", "project-1", {"task_run_id": "run-1", "cursor": 0, "limit": 20})]
    assert page["items"][0]["flow_id"] == "flow-1"
    assert "private" not in json.dumps(page)
    assert "headers_base64" not in json.dumps(page)
    binding.dispatch({"method": "list_http_flows", "params": {"historical": True, "task_run_id": "run-old", "cursor": 2, "limit": 1}})
    assert client.calls[-1][2]["task_run_id"] == "run-old"
    with pytest.raises(ValueError, match="traffic_historical_required"):
        binding.dispatch({"method": "read_http_flow", "params": {"flow_id": "old", "side": "request"}})
    assert not any(call[0] == "projection" for call in client.calls)
    result = binding.dispatch({"method": "read_http_flow", "params": {"flow_id": "old", "historical": True, "side": "request", "offset": 7, "limit": 6}})
    assert result["scope"]["task_run_id"] == "run-old"
    assert result["offset"] == 7
    with pytest.raises(ValueError, match="traffic_project_mismatch"):
        binding.dispatch({"method": "read_http_flow", "params": {"flow_id": "foreign", "historical": True}})


def test_receiving_body_preview_is_not_a_stable_projection():
    client = Client()
    client.records["flow-1"]["state"] = "receiving"
    calls = []
    client.read_http_flow_body_preview = lambda project, flow, side, **params: (
        calls.append((project, flow, side, params)) or ApiResult(200, {
            "content_base64": "AP8=", "encoding": "base64", "offset": 7,
            "accepted_bytes": 9, "sealed": False, "source_sealed": False,
        })
    )
    result = TrafficReadBinding(client, "project-1", "run-1").dispatch({
        "method": "read_http_flow", "params": {"flow_id": "flow-1", "side": "response", "offset": 7, "limit": 2},
    })
    assert calls == [("project-1", "flow-1", "response", {"offset": 7, "limit": 2})]
    assert result["preview"] and not result["excerpt_eligible"]
    assert result["representation"] == "body-preview-base64" and result["content_base64"] == "AP8="
    assert result["state"] == "receiving" and not result["sealed"]
    assert "pointer" not in result and "sha256" not in result
    assert not any(call[0] == "projection" for call in client.calls)


def test_bound_reader_rejects_proxy_upload_identity_and_unbounded_ranges():
    client = Client()
    binding = TrafficReadBinding(client, "project-1", "run-1")
    invalid = [
        ("GET", {"url": "http://synthetic.invalid"}),
        ("sync_traffic_flow", {}),
        ("list_http_flows", {"project_id": "other"}),
        ("list_http_flows", {"limit": 51}),
        ("list_http_flows", {"cursor": -1}),
        ("list_http_flows", {"historical": "true"}),
        ("list_http_flows", {"task_run_id": "run-old"}),
        ("read_http_flow", {"flow_id": "../scopes/status"}),
        ("read_http_flow", {"flow_id": "flow-1", "limit": 32769}),
        ("read_http_flow", {"flow_id": "flow-1", "offset": True}),
        ("read_http_flow", {"flow_id": "flow-1", "side": "body"}),
    ]
    for method, params in invalid:
        with pytest.raises(ValueError):
            binding.dispatch({"method": method, "params": params})
    assert client.calls == []


def test_client_uses_existing_authenticated_session_for_fixed_get_routes(monkeypatch):
    from cairn.dispatcher.protocol.client import CairnClient

    client = CairnClient("http://synthetic.invalid", token="synthetic-dispatcher-token")
    calls = []
    session = client._session()
    monkeypatch.setattr(session, "request", lambda method, url, **kwargs: (
        calls.append((method, url, kwargs)) or SimpleNamespace(status_code=200, headers={"content-type": "application/json"}, json=lambda: {}, text="{}")
    ))
    try:
        assert session.headers["Authorization"] == "Bearer synthetic-dispatcher-token"
        assert client.list_http_flows("p1", task_run_id="tr1", cursor=3, limit=1).ok
        assert client.read_http_flow("p1", "flow-1").ok
        assert client.read_http_flow_projection("p1", "flow-1", "response", offset=4, limit=6).ok
        assert client.read_http_flow_body_preview("p1", "flow-1", "response", offset=7, limit=2).ok
        assert [call[0] for call in calls] == ["GET"] * 4
        assert calls[0][2]["params"] == {"task_run_id": "tr1", "cursor": 3, "limit": 1}
        assert calls[2][1].endswith("/projects/p1/traffic/flow-1/projection/response")
        assert calls[2][2]["params"] == {"offset": 4, "limit": 6}
        assert calls[3][1].endswith("/projects/p1/traffic/flow-1/bodies/response")
        assert calls[3][2]["params"] == {"preview": "true", "offset": 7, "limit": 2}
        assert "synthetic-dispatcher-token" not in json.dumps(calls)
    finally:
        client.close()


def test_driver_binds_private_readonly_delivery_but_never_report_live_reads():
    from cairn.dispatcher.config import PiModelConfig, WorkerConfig
    from cairn.dispatcher.workers.adapters.pi import PiDriver

    worker = WorkerConfig(id="pi-worker", engine="pi", priority=0, concurrency=1, task_types=["explore"], model_binding=PiModelConfig(
        protocol="openai-chat-completions", model_name="synthetic", inference_endpoint="http://synthetic.invalid/infer",
        base_url="http://synthetic.invalid", auth_header="authorization-bearer", api_key="synthetic-model-key",
    ))
    driver = PiDriver()
    invocation = driver.build_conclude(worker, "只读已有材料", driver.prepare_session(), phase="explore_conclude",
        project_id="p1", task_run_id="tr1", timeout_seconds=10, session_root="/home/kali/.cairn/pi")
    request = json.loads(invocation.input_files[0].content)
    capability = request["capabilities"]["http_traffic"]
    assert set(capability) == {"protocol", "project_id", "task_run_id", "socket_path"}
    assert capability["project_id"] == "p1" and capability["task_run_id"] == "tr1"
    assert invocation.traffic_read_socket_path == capability["socket_path"]
    assert "task_execution" not in request["capabilities"]
    report = driver.build_execute(worker, "读取冻结Packet", None, phase="report_execute", project_id="p1", task_run_id="tr-report",
        timeout_seconds=10, session_root="/home/kali/.cairn/pi", binding={"report_segments": {
            "project_id": "p1", "task_run_id": "tr-report", "generation_token": "generation", "input_snapshot_sha256": "a" * 64,
            "report_template_digest_sha256": "b" * 64,
        }})
    assert report.traffic_read_socket_path is None
    assert "http_traffic" not in json.loads(report.input_files[0].content)["capabilities"]


def test_private_socket_roundtrip_and_cleanup(tmp_path):
    # 宿主私有根可以长于 AF_UNIX 地址限制；容器看到的 mount 路径仍固定且短。
    directory = tmp_path / ("private-" + "x" * 100)
    directory.mkdir(mode=0o700)
    binding = TrafficReadBinding(Client(), "project-1", "run-1")
    reader = TrafficReader(directory, "reader.sock", binding)
    try:
        assert reader.path.stat().st_mode & 0o777 == 0o600
        import os
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.connect(f"/proc/self/fd/{fd}/reader.sock")
                connection.sendall(b'{"method":"list_http_flows","params":{}}\n')
                response = json.loads(connection.makefile("rb").readline())
                assert response["ok"] is True
                assert response["data"]["items"][0]["flow_id"] == "flow-1"
        finally:
            os.close(fd)
    finally:
        reader.close()
    assert not reader.path.exists()
