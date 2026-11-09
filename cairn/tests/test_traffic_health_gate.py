"""执行许可刷新与健康判定解耦：gate 只表达宿主存活且在看管，不依赖检查全部通过。"""

from __future__ import annotations

from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.traffic import ProjectTrafficRuntime
from cairn.dispatcher.runtime.traffic_execution import TaskTrafficExecution, TrafficExecutionBridge

SCOPE = "synthetic-scope"


def executor_capture_ready(gate: Path) -> bool:
    # 复刻 container/task-executor/server.py capture_ready 的判定，直接读宿主侧许可文件。
    try:
        timestamp = float(gate.read_text())
    except (OSError, ValueError):
        return False
    return 0 <= time.time() - timestamp <= 3


def build_runtime(tmp_path: Path, container_states: dict[str, str]):
    client = Mock()
    client.containers.get = lambda container_id: Mock(status=container_states[container_id])
    runtime = ProjectTrafficRuntime(
        client, project_id="synthetic-gate", root=tmp_path / "runtime",
        gateway_image="gateway", executor_image="executor",
    )
    scope_directory = runtime.execution / SCOPE
    scope_directory.mkdir(parents=True)
    (runtime.capture / SCOPE).mkdir(parents=True)
    runtime.manifest = {
        "scopes": {SCOPE: {"entry_closed": False, "task_run_id": "run", "container_id": "executor-id"}},
        "resources": {
            f"collector:{SCOPE}": {"kind": "container", "id": "collector-id", "removed": False, "labels": {}},
            f"executor:{SCOPE}": {"kind": "container", "id": "executor-id", "removed": False, "labels": {}},
        },
    }
    return runtime, client


def test_slow_check_still_advances_gate_before_container_reads(tmp_path):
    runtime, client = build_runtime(tmp_path, {"collector-id": "running", "executor-id": "running"})
    gate = runtime.execution / SCOPE / "traffic-ready"
    seeded = time.time() - 2.5
    gate.write_text(str(seeded))
    (runtime.capture / SCOPE / "heartbeat").write_text(str(time.time()))

    # 注入 >3s 的 Docker 状态读取延迟：release 前容器状态检查一直阻塞。
    release = threading.Event()
    stubs = {"collector-id": Mock(status="running"), "executor-id": Mock(status="running")}

    def slow_get(container_id):
        release.wait(5)
        return stubs[container_id]

    client.containers.get = slow_get

    outcome = {}

    def probe():
        outcome["code"] = runtime.check_scope(SCOPE)

    worker = threading.Thread(target=probe, daemon=True)
    worker.start()
    refreshed = None
    deadline = time.time() + 2
    while time.time() < deadline:
        try:
            value = float(gate.read_text())
        except (OSError, ValueError):
            value = None
        if value is not None and value > seeded:
            refreshed = value
            break
        time.sleep(0.02)
    # 旧实现把刷新放在容器状态读取之后：阻塞期间不可能出现新时间戳。
    assert refreshed is not None
    assert release.is_set() is False
    # 慢检查期间执行器侧读到的许可是刚写入的新值，不会过期触发自杀保护。
    assert executor_capture_ready(gate) is True
    release.set()
    worker.join(5)
    assert outcome["code"] is None

    # 每次 check_scope 都推进时间戳，包括检查失败（heartbeat 冻结）的调用。
    before = float(gate.read_text())
    (runtime.capture / SCOPE / "heartbeat").write_text(str(time.time() - 10))
    assert runtime.check_scope(SCOPE) == "traffic_capture_stalled"
    assert float(gate.read_text()) > before
    # 对照保留：heartbeat 冻结仍在一个检查内产出 stalled 取消码。
    assert runtime.check_scope(SCOPE) == "traffic_capture_stalled"


def test_executor_container_stop_cancels_within_one_watch_cycle(tmp_path, monkeypatch):
    runtime, _ = build_runtime(tmp_path, {"collector-id": "running", "executor-id": "exited"})
    (runtime.capture / SCOPE / "heartbeat").write_text(str(time.time()))
    gate = runtime.execution / SCOPE / "traffic-ready"
    gate.write_text(str(time.time()))

    stopped = []
    monkeypatch.setattr(
        TaskTrafficExecution, "stop",
        lambda self, reason="task_finished": stopped.append(reason) or {},
    )
    bridge = TrafficExecutionBridge(
        Mock(), None, root=tmp_path / "bridge", dispatcher_id="synthetic",
        config=SimpleNamespace(image="executor", traffic_gateway_image="gateway"),
    )
    cancellation = TaskCancellation(generation=0)
    execution = TaskTrafficExecution(runtime, SCOPE, cancellation)
    cancellation.register_resource(execution)
    with bridge._lock:
        bridge._tasks[("synthetic-gate", "run")] = execution
    watcher = threading.Thread(target=bridge._watch, daemon=True)
    watcher.start()
    try:
        deadline = time.time() + 2
        while time.time() < deadline and not cancellation.is_cancelled:
            time.sleep(0.02)
        assert cancellation.is_cancelled
        assert execution.failure == "traffic_executor_stopped"
        assert stopped == ["traffic_executor_stopped"]
    finally:
        bridge._stop.set()
        watcher.join(2)


def test_entry_closed_scope_never_refreshes_gate(tmp_path):
    runtime, _ = build_runtime(tmp_path, {"collector-id": "running", "executor-id": "running"})
    gate = runtime.execution / SCOPE / "traffic-ready"
    seeded = str(time.time() - 1)
    gate.write_text(seeded)
    runtime.manifest["scopes"][SCOPE]["entry_closed"] = True
    assert runtime.check_scope(SCOPE) == "traffic_scope_closed"
    assert gate.read_text() == seeded
