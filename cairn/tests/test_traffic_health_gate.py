"""真实所有者连接的准入、失联和观测语义；不复制执行器判定。"""

from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

from docker.errors import DockerException
import pytest

from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.traffic import OWNER_PROTOCOL, ProjectTrafficRuntime
from cairn.dispatcher.runtime.traffic_execution import TaskTrafficExecution, TrafficExecutionBridge

SCOPE = "scope"
EXECUTOR = Path(__file__).resolve().parents[2] / "container/task-executor/server.py"


def build_runtime(root: Path):
    client = Mock()
    containers = {name: Mock(status="running") for name in ("collector-id", "executor-id")}
    client.containers.get.side_effect = containers.__getitem__
    runtime = ProjectTrafficRuntime(
        client, project_id="synthetic-owner", root=root,
        gateway_image="gateway", executor_image="executor",
    )
    (runtime.execution / SCOPE).mkdir(parents=True)
    (runtime.capture / SCOPE).mkdir(parents=True)
    runtime.manifest = {
        "scopes": {SCOPE: {
            "entry_closed": False, "task_run_id": "run", "container_id": "executor-id",
            "id": SCOPE, "generation": 0, "address": "127.0.0.1",
            "test_stopped": False, "collector_stopped": False,
        }},
        "resources": {
            f"collector:{SCOPE}": {"kind": "container", "id": "collector-id", "removed": False, "labels": {}},
            f"executor:{SCOPE}": {"kind": "container", "id": "executor-id", "removed": False, "labels": {}},
        },
    }
    runtime._load_scopes()
    return runtime, containers


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "等待本地状态超时"
        time.sleep(0.02)


@pytest.fixture
def executor():
    # 正式实例根目录包含 64 位身份；必须覆盖长路径，不能只在短临时目录验证准入。
    with tempfile.TemporaryDirectory(prefix="owner-") as directory:
        runtime, containers = build_runtime(Path(directory) / ("dispatcher-traffic-v1-" + "a" * 64) / "project")
        path = runtime.execution / SCOPE / "execute.sock"
        clock = runtime.root / "wall-clock"
        clock.write_text(str(time.time()))
        script = """
import os, runpy, sys
from pathlib import Path
service = runpy.run_path(sys.argv[1])
service['time'].time = lambda: float(Path(sys.argv[3]).read_text())
directory_fd = os.open(Path(sys.argv[2]).parent, os.O_RDONLY | os.O_DIRECTORY)
try:
    with service['Server'](f'/proc/self/fd/{directory_fd}/execute.sock', service['Handler']) as server:
        server.serve_forever()
finally:
    os.close(directory_fd)
"""
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(EXECUTOR), str(path), str(clock)],
            env={**os.environ, "CAIRN_EXECUTION_SCOPE": SCOPE, "CAIRN_EXECUTION_TASK_RUN": "run"},
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            wait_for(lambda: path.exists() or process.poll() is not None)
            assert process.poll() is None, process.stderr.read().decode()
            yield runtime, containers, process, clock
        finally:
            for owner in runtime._owners.values():
                owner.close()
            runtime._owners.clear()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stderr.close()


def request(runtime, method, path, payload=None, headers=None):
    connection = http.client.HTTPConnection("localhost", timeout=20)
    connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.sock.settimeout(20)
    directory_fd = os.open(runtime.execution / SCOPE, os.O_RDONLY | os.O_DIRECTORY)
    try:
        connection.sock.connect(f"/proc/self/fd/{directory_fd}/execute.sock")
    finally:
        os.close(directory_fd)
    try:
        connection.request(method, path, body=json.dumps(payload) if payload else None, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def execute(runtime, command="printf owner-ok"):
    status, body = request(runtime, "POST", "/execute", {
        "scope_id": SCOPE, "task_run_id": "run", "command": command,
        "cwd": str(runtime.root), "timeout_seconds": 20,
    })
    assert status == 200, body
    events = [json.loads(line) for line in body.splitlines()]
    assert events[-1] == {"exit_code": 0}, events
    return events


def test_real_executor_requires_one_owner_after_preparation(executor):
    runtime, _, process, _ = executor
    assert request(runtime, "GET", "/health")[0] == 503
    assert request(runtime, "POST", "/execute", {"command": "touch should-not-run"})[0] == 503
    assert not (runtime.root / "should-not-run").exists()
    runtime._admit_scope(SCOPE)
    assert request(runtime, "GET", "/health")[0] == 200
    assert request(runtime, "POST", "/owner", headers={"X-Cairn-Owner-Protocol": OWNER_PROTOCOL})[0] == 409
    execute(runtime)
    execute(runtime)
    assert process.poll() is None
    assert not (runtime.execution / SCOPE / "traffic-ready").exists()


def test_runtime_lock_docker_failure_and_wall_clock_do_not_revoke_owner(executor):
    runtime, _, process, clock = executor
    runtime._admit_scope(SCOPE)
    runtime.client.containers.get.side_effect = DockerException("daemon temporarily unavailable")
    # 模拟容器启动或 manifest 落盘持锁；所有者连接无需访问这把锁或刷新文件。
    with runtime._entry_lock:
        for timestamp in (time.time() - 3600, time.time() + 3600):
            clock.write_text(str(timestamp))
            assert request(runtime, "GET", "/health")[0] == 200
            execute(runtime)
    assert process.poll() is None


def test_owner_process_stall_survives_old_window_then_crash_stops_executor(executor):
    runtime, _, process, _ = executor
    script = """
import json, sys
from pathlib import Path
from cairn.dispatcher.runtime.traffic import ProjectTrafficRuntime
runtime = ProjectTrafficRuntime(None, project_id='synthetic-owner', root=Path(sys.argv[1]), gateway_image='g', executor_image='e')
runtime.manifest = {'scopes': {'scope': {'entry_closed': False}}}
runtime._admit_scope('scope')
print('owned', flush=True)
sys.stdin.read()
"""
    owner = subprocess.Popen(
        [sys.executable, "-c", script, str(runtime.root)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        assert owner.stdout.readline() == b"owned\n"
        owner.send_signal(signal.SIGSTOP)
        # 真正停调度所有者进程，跨过上一轮 10 秒窗口；不是 mock 一个就绪谓词。
        execute(runtime, "sleep 11; printf survived-owner-stall")
        assert process.poll() is None
        owner.kill()
        owner.wait(timeout=5)
        assert process.wait(timeout=5) == 75
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=5)
        for stream in (owner.stdin, owner.stdout, owner.stderr):
            stream.close()


def test_explicit_stop_closes_owner_even_when_docker_confirmation_fails(executor, monkeypatch):
    runtime, _, process, _ = executor
    runtime._admit_scope(SCOPE)
    monkeypatch.setattr(runtime, "_stop_container", Mock(side_effect=DockerException("unavailable")))
    result = runtime.stop_scope(SCOPE, "user_cancelled")
    assert process.wait(timeout=5) == 75
    assert result["termination_confirmed"] is False
    assert result["code"] == "traffic_termination_unknown"
    assert runtime._owners == {}


def test_closed_scope_cannot_acquire_owner(executor):
    runtime, _, _, _ = executor
    runtime.manifest["scopes"][SCOPE]["entry_closed"] = True
    with pytest.raises(RuntimeError, match="traffic_task_cancelled"):
        runtime._admit_scope(SCOPE)
    assert runtime._owners == {}
    assert request(runtime, "GET", "/health")[0] == 503


def test_collector_heartbeat_is_not_a_death_certificate(tmp_path):
    runtime, containers = build_runtime(tmp_path)
    heartbeat = runtime.capture / SCOPE / "heartbeat"
    for content in ("0", str(time.time() + 3600), "not-a-timestamp"):
        heartbeat.write_text(content)
        assert runtime.check_scope(SCOPE) is None
    heartbeat.unlink()
    assert runtime.check_scope(SCOPE) is None
    containers["collector-id"].status = "exited"
    assert runtime.check_scope(SCOPE) == "traffic_collector_stopped"


@contextmanager
def watching(runtime, tmp_path, monkeypatch):
    stopped = []
    monkeypatch.setattr(TaskTrafficExecution, "stop", lambda self, reason: stopped.append(reason) or {})
    bridge = TrafficExecutionBridge(
        Mock(), None, root=tmp_path / "bridge", dispatcher_id="synthetic",
        config=SimpleNamespace(image="executor", traffic_gateway_image="gateway"),
    )
    cancellation = TaskCancellation(generation=0)
    execution = TaskTrafficExecution(runtime, SCOPE, cancellation)
    cancellation.register_resource(execution)
    bridge._tasks[("synthetic-owner", "run")] = execution
    thread = threading.Thread(target=bridge._watch, daemon=True)
    thread.start()
    try:
        yield bridge, execution, cancellation, stopped
    finally:
        bridge._stop.set()
        thread.join(5)


def test_observation_failure_is_visible_recovers_and_only_real_exit_cancels(tmp_path, monkeypatch):
    runtime, containers = build_runtime(tmp_path)
    runtime.client.containers.get.side_effect = DockerException("temporary daemon failure")
    with watching(runtime, tmp_path, monkeypatch) as (bridge, execution, cancellation, stopped):
        wait_for(lambda: execution.health_error is not None)
        status = bridge._scope_status(runtime.manifest["scopes"][SCOPE], execution, {}, {})
        assert status["state"] == "retrying"
        assert status["code"] == "traffic_health_unavailable"
        assert status["failure_code"] is None
        assert not cancellation.is_cancelled
        runtime.client.containers.get.side_effect = containers.__getitem__
        wait_for(lambda: execution.health_error is None)
        assert not cancellation.is_cancelled
        containers["executor-id"].status = "exited"
        wait_for(lambda: bool(stopped))
        assert cancellation.is_cancelled
        assert execution.failure == "traffic_executor_stopped"
        assert stopped == ["traffic_executor_stopped"]


def test_egress_transient_dns_retry_is_bounded(tmp_path, monkeypatch):
    runtime, _ = build_runtime(tmp_path)
    calls = Mock(side_effect=[
        socket.gaierror(socket.EAI_AGAIN, "temporary failure"),
        socket.gaierror(socket.EAI_AGAIN, "temporary failure"),
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 9567))],
    ])
    monkeypatch.setattr(socket, "getaddrinfo", calls)
    pauses = []
    monkeypatch.setattr(time, "sleep", pauses.append)
    monkeypatch.setattr(runtime, "_container", lambda *a, **kw: Mock(id="egress-id"))
    monkeypatch.setattr(runtime, "_start_container", lambda *a, **kw: None)
    ready = runtime.root / "egress" / SCOPE / "ready"
    ready.parent.mkdir(parents=True)
    ready.touch()
    runtime._start_egress(runtime.scopes[0], {"host": "proxy.test", "port": 9567, "mode": "http"})
    assert calls.call_count == 3
    assert pauses == [2, 2]


@pytest.mark.parametrize("code, attempts", [(socket.EAI_AGAIN, 3), (socket.EAI_NONAME, 1)])
def test_egress_dns_failure_has_recovery_basis(tmp_path, monkeypatch, code, attempts):
    runtime, _ = build_runtime(tmp_path)
    lookup = Mock(side_effect=socket.gaierror(code, "resolution failed"))
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    pauses = []
    monkeypatch.setattr(time, "sleep", pauses.append)
    with pytest.raises(RuntimeError, match="traffic_proxy_dns_or_address_failed"):
        runtime._start_egress(runtime.scopes[0], {"host": "proxy.test", "port": 9567, "mode": "http"})
    assert lookup.call_count == attempts
    assert pauses == [2] * (attempts - 1)
