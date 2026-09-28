"""显式指定本地镜像才执行；只建本测试归属、无外网容器，不触及真实目标/provider。"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
import uuid

import pytest

from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.process import ManagedProcess


@pytest.fixture
def isolated_runtime():
    image = os.environ.get("CAIRN_TEST_RUNTIME_IMAGE")
    if not image:
        pytest.skip("set CAIRN_TEST_RUNTIME_IMAGE to an existing local image")
    import docker
    client = docker.from_env(timeout=5)
    client.images.get(image)
    container = client.containers.run(
        image, ["sleep", "120"], detach=True, init=True, network_mode="none",
        name=f"cairn-runtime-cancel-test-{uuid.uuid4().hex[:12]}",
        labels={"cairn.test.owner": "runtime-cancel-noble-sparking-cray"},
    )
    manager = object.__new__(ContainerManager)
    manager._require_container = lambda _name: container
    try:
        yield client, container, manager
    finally:
        container.remove(force=True)
        client.close()


def test_legacy_process_group_proof_cannot_claim_descendant_drain(tmp_path):
    from cairn.dispatcher.runtime.exec_control import EXEC_VERIFY_EXIT

    path = tmp_path / "legacy-exec.json"
    proof = {
        "pid": 1000000000, "start": "1", "parent": 1, "parent_start": "1",
        "drained": True, "returncode": 0,
    }
    path.write_text(json.dumps(proof), encoding="utf-8")
    check = subprocess.run(
        [sys.executable, "-c", EXEC_VERIFY_EXIT, str(path), "0"],
        capture_output=True, timeout=3, check=False,
    )
    assert check.returncode == 2


def wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.03)
    assert predicate(), "bounded local runtime observation did not converge"


def exists(container, path):
    return container.exec_run(["test", "-e", path]).exit_code == 0


def markers(container):
    return container.exec_run(["find", "/tmp", "-maxdepth", "1", "-name", "cairn-exec-*"]).output.decode().strip()


@pytest.mark.parametrize("ignore_term", [False, True])
def test_real_cancel_drains_timeout_group_and_local_provider_connection(isolated_runtime, ignore_term):
    client, container, manager = isolated_runtime
    # 这是容器内纯 TCP 假 provider，只观察连接断开，不读凭据、不调用模型。
    provider = """
import socket
from pathlib import Path
s=socket.socket();s.bind(('127.0.0.1',18765));s.listen(1)
Path('/tmp/provider-ready').touch()
c,_=s.accept()
while c.recv(1024): pass
Path('/tmp/provider-closed').touch()
c.close();s.close()
"""
    container.exec_run(["python3", "-c", provider], detach=True)
    wait_until(lambda: exists(container, "/tmp/provider-ready"))
    command = """
import os,signal,socket,subprocess,time
from pathlib import Path
if %s: signal.signal(signal.SIGTERM, signal.SIG_IGN)
s=socket.create_connection(('127.0.0.1',18765))
child=subprocess.Popen(['sleep','60'])
Path('/tmp/command-pids').write_text(str(os.getpid())+' '+str(child.pid))
time.sleep(60)
""" % ignore_term
    process = manager.build_exec_process(container.name, {}, ["python3", "-c", command], timeout_seconds=60, kill_after_seconds=1)
    token = TaskCancellation(generation=7)
    token.register_execution(process)
    token.attach_process(process)
    process.start()
    wait_until(lambda: exists(container, "/tmp/command-pids"))
    raw = container.exec_run(["cat", "/tmp/command-pids"]).output.decode()
    namespace_pids = [int(pid) for pid in raw.split()]
    identity = process.execution_identity
    assert identity is not None and identity[0] == container.id
    before = client.api.exec_inspect(identity[1])
    assert before["Running"] is True and before["Pid"] not in namespace_pids
    started = time.monotonic()
    assert token.cancel("paused") is True
    assert token.cancel("deleted") is False
    assert time.monotonic() - started < .5
    with process.communicate(timeout=8) as result:
        token.finish_execution(process)
        assert result.cancelled and result.cancel_reason == "paused"
        assert not result.timed_out
        assert client.api.exec_inspect(identity[1])["Running"] is False
        assert process.execution_completed is True
        assert token.execution_container_ids() == [container.id]
        assert token.execution_lifecycle(task_finished=True) == {
            "generation": 7, "process_started": True,
            "termination_confirmed": True, "cleanup_confirmed": False,
        }
        if ignore_term:
            assert result.returncode == 137
    wait_until(lambda: exists(container, "/tmp/provider-closed"))
    for pid in namespace_pids:
        check = container.exec_run(["python3", "-c", "import os,sys;sys.exit(os.path.exists('/proc/'+sys.argv[1]))", str(pid)])
        assert check.exit_code == 0
    assert markers(container) == ""


def test_task_level_stop_drains_only_lost_exec_in_shared_container(isolated_runtime):
    client, container, manager = isolated_runtime
    manager._client = client
    manager.docker_daemon_id = lambda: client.info()["ID"]
    manager._get_container = lambda identity: container if identity == container.id else None

    def assert_owned(actual, *, project_id=None):
        assert actual.id == container.id and project_id in (None, "isolated-test-project")

    manager._assert_owned = assert_owned
    manager._container_state = lambda actual: (actual.reload() or actual.status)
    processes = []
    try:
        for name in ("A", "B"):
            command = (
                "from pathlib import Path\n"
                "import time\n"
                f"Path('/tmp/cairn-test-{name}-ready').touch()\n"
                "while True: time.sleep(.1)\n"
            )
            process = manager.build_exec_process(
                container.name, {}, ["python3", "-c", command], timeout_seconds=60,
            )
            process.retain_execution_control()
            process.start()
            processes.append(process)
        for name in ("A", "B"):
            wait_until(lambda name=name: exists(container, f"/tmp/cairn-test-{name}-ready"))
        a, b = processes
        assert a.execution_identity is not None and b.execution_identity is not None
        assert client.api.exec_inspect(a.execution_identity[1])["Running"] is True
        assert client.api.exec_inspect(b.execution_identity[1])["Running"] is True

        outcome = manager.terminate_candidate_execution(
            docker_daemon_id=manager.docker_daemon_id(),
            container_id=container.id,
            container_name=container.name,
            project_id="isolated-test-project",
            exec_id=a.execution_identity[1],
            control_path=a.execution_control_path,
        )
        assert outcome.status == "stopped" and outcome.reason == "exec_terminal_descendants_drained"
        assert client.api.exec_inspect(a.execution_identity[1])["Running"] is False
        assert client.api.exec_inspect(b.execution_identity[1])["Running"] is True
        assert manager.candidate_execution_status(
            docker_daemon_id=manager.docker_daemon_id(),
            container_id=container.id,
            container_name=container.name,
            project_id="isolated-test-project",
            exec_id=b.execution_identity[1],
            control_path=b.execution_control_path,
        ).status == "running"
        # 历史 PGID-only 凭据不能因为 Docker exec 已退出而被升级为后代停止证明。
        legacy = container.exec_run([
            "python3", "-c",
            "import json,sys; p=sys.argv[1]; d=json.load(open(p)); d.pop('version'); "
            "open(p,'w').write(json.dumps(d))",
            a.execution_control_path,
        ])
        assert legacy.exit_code == 0
        assert manager.candidate_execution_status(
            docker_daemon_id=manager.docker_daemon_id(),
            container_id=container.id,
            container_name=container.name,
            project_id="isolated-test-project",
            exec_id=a.execution_identity[1],
            control_path=a.execution_control_path,
        ).status == "unknown"
    finally:
        for process in processes:
            process.cancel("test_cleanup")
            with process.communicate(timeout=8) as result:
                assert result.cancelled or process.execution_completed


@pytest.mark.parametrize("nested_session", [False, True])
def test_actual_pi_bash_descendants_stop_with_a_and_not_b(isolated_runtime, nested_session):
    client, container, manager = isolated_runtime
    manager._client = client
    manager.docker_daemon_id = lambda: client.info()["ID"]
    manager._get_container = lambda identity: container if identity == container.id else None
    def assert_owned(actual, *, project_id=None):
        assert actual.id == container.id and project_id in (None, "isolated-test-project")

    manager._assert_owned = assert_owned
    manager._container_state = lambda actual: (actual.reload() or actual.status)

    def sdk_process(name, *, nested=False):
        # 真实 pi SDK bash 默认 detached，第二种再从 bash 内启动新的 session。
        writer = (
            "from pathlib import Path\nimport os,time\n"
            f"Path('/tmp/sdk-{name}-tool-pid').write_text(str(os.getpid()))\n"
            "step=0\nwhile True:\n"
            f"    Path('/tmp/sdk-{name}-mark').write_text(str(step))\n"
            "    step+=1\n    time.sleep(.06)\n"
        )
        if nested:
            launcher = (
                "import subprocess,sys\n"
                f"subprocess.Popen([sys.executable,'-c',{writer!r}], "
                "start_new_session=True, stdin=subprocess.DEVNULL, "
                "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            )
            command = f"python3 -c {shlex.quote(launcher)}"
        else:
            command = f"python3 -c {shlex.quote(writer)}"
        js = (
            "const fs=require('node:fs');"
            f"fs.writeFileSync('/tmp/sdk-{name}-runner-pid',String(process.pid));"
            "import('file:///opt/cairn/pi-agent/dist/runner.js').then(async ({createRequestBashToolDefinition})=>{"
            "const tool=createRequestBashToolDefinition('/tmp',40,()=>40);"
            f"await tool.execute('actual-sdk-bash',{{command:{json.dumps(command)}}});"
            "setInterval(()=>{},1000);"
            "}).catch((error)=>{console.error(error);process.exitCode=1});"
        )
        process = manager.build_exec_process(container.name, {}, ["node", "-e", js], timeout_seconds=60)
        process.retain_execution_control()
        process.start()
        return process

    def marker(name):
        raw = container.exec_run(["cat", f"/tmp/sdk-{name}-mark"])
        return int(raw.output.decode()) if raw.exit_code == 0 and raw.output.strip().isdigit() else -1

    processes = []
    try:
        a = sdk_process("A", nested=nested_session)
        b = sdk_process("B")
        processes.extend((a, b))
        wait_until(lambda: marker("A") >= 2 and marker("B") >= 2, timeout=12)
        a_runner = int(container.exec_run(["cat", "/tmp/sdk-A-runner-pid"]).output)
        a_tool = int(container.exec_run(["cat", "/tmp/sdk-A-tool-pid"]).output)
        control = json.loads(container.exec_run(["cat", a.execution_control_path]).output)
        assert control["version"] == 2
        groups = container.exec_run([
            "python3", "-c",
            "import os,sys;print(os.getpgid(int(sys.argv[1])),os.getsid(int(sys.argv[2])))",
            str(control["pid"]), str(a_tool),
        ])
        assert groups.exit_code == 0
        runner_group, tool_session = map(int, groups.output.decode().split())
        assert runner_group != tool_session, "pi SDK bash must have its own session"
        identity = dict(
            docker_daemon_id=manager.docker_daemon_id(), container_id=container.id,
            container_name=container.name, project_id="isolated-test-project",
            exec_id=a.execution_identity[1], control_path=a.execution_control_path,
        )
        assert manager.candidate_execution_status(**identity).status == "running"
        # 只杀 runner Node，不调用其 abort callback；bash 无输出且持续改动 marker。
        container.exec_run(["kill", "-KILL", str(a_runner)])
        wait_until(lambda: manager.candidate_execution_status(**identity).status == "stopped", timeout=12)
        stopped = manager.candidate_execution_status(**identity)
        assert stopped.reason == "exec_terminal_descendants_drained"
        assert client.api.exec_inspect(b.execution_identity[1])["Running"] is True
        a_mark, b_mark = marker("A"), marker("B")
        time.sleep(.4)
        assert marker("A") == a_mark, "stopped A continued to execute after descendant proof"
        assert marker("B") > b_mark, "unrelated B in shared container must continue"
        proof = json.loads(container.exec_run(["cat", a.execution_control_path]).output)
        assert proof["version"] == 2 and proof["drained"] is True
    finally:
        for process in processes:
            process.cancel("test_cleanup")
            with process.communicate(timeout=8) as result:
                assert result.cancelled or process.execution_completed


def test_real_cancel_before_pidfile_prevents_command_start(isolated_runtime, monkeypatch):
    import cairn.dispatcher.runtime.process as module
    _, container, manager = isolated_runtime
    monkeypatch.setattr(module, "EXEC_SUPERVISOR", "import time;time.sleep(.3)\n" + module.EXEC_SUPERVISOR)
    process = manager.build_exec_process(container.name, {}, ["touch", "/tmp/must-not-start"], timeout_seconds=60)
    process.start()
    wait_until(lambda: process.execution_started)
    process.cancel("paused")
    with process.communicate(timeout=8) as result:
        assert result.cancelled and process.execution_completed
    assert not exists(container, "/tmp/must-not-start")
    assert markers(container) == ""


def test_real_natural_exit_racing_cancel_cleans_late_cancel_marker(isolated_runtime, monkeypatch):
    client, container, manager = isolated_runtime
    process = manager.build_exec_process(container.name, {}, ["sh", "-c", "while test ! -e /tmp/finish; do sleep .03; done"], timeout_seconds=60)
    entered, release = threading.Event(), threading.Event()
    signal_execution = process._signal_execution

    def delayed_signal():
        entered.set()
        assert release.wait(8)
        return signal_execution()

    monkeypatch.setattr(process, "_signal_execution", delayed_signal)
    process.start()
    wait_until(lambda: process.execution_started)
    process.cancel("paused")
    assert entered.wait(2)
    try:
        container.exec_run(["touch", "/tmp/finish"])
        wait_until(lambda: client.api.exec_inspect(process.execution_identity[1])["Running"] is False)
    finally:
        release.set()
    with process.communicate(timeout=8) as result:
        assert result.cancelled and process.execution_completed
    assert markers(container) == ""


def test_real_startup_selfcheck_succeeds_without_network_or_provider_env(isolated_runtime):
    from cairn.dispatcher.config import WorkerConfig
    from cairn.dispatcher.runtime.startup_healthcheck import _run_worker_healthcheck

    _, container, manager = isolated_runtime
    result = _run_worker_healthcheck(
        manager, container.name, WorkerConfig(id="offline-fixture", engine="pi", task_types=["reason"], priority=0, concurrency=1),
        timeout_seconds=30,
    )
    assert result.ok
    assert result.command == "cairn-agent --self-check"
    assert markers(container) == ""


def test_real_supervisor_death_does_not_fake_group_exit(isolated_runtime):
    client, container, manager = isolated_runtime
    process = manager.build_exec_process(container.name, {}, ["sleep", "60"], timeout_seconds=60)
    process.start()
    wait_until(lambda: exists(container, process._control_path))
    identity = json.loads(container.exec_run(["cat", process._control_path]).output)
    container.exec_run(["kill", "-KILL", str(identity["parent"])])
    wait_until(lambda: client.api.exec_inspect(process.execution_identity[1])["Running"] is False)
    with process.communicate(timeout=.2) as result:
        assert result.transport_error
        assert process.execution_completed is False
    # supervisor 的 Docker exec 虽然已终态，原命令组仍运行，不能凭终态允许恢复。
    assert container.exec_run(["kill", "-0", str(identity["pid"])]).exit_code == 0
