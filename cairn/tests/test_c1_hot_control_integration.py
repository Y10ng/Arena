"""C1 受控全链：真实 HTTP/私有 PostgreSQL/生产控制器/Docker，不调用模型或目标。"""

from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import os
from pathlib import Path
import shlex
import socket
import threading
import time
from types import SimpleNamespace

import docker
import pytest

from traffic_api_fixture import traffic_api, postgres_instance, empty_postgres_url  # noqa: F401
from cairn.dispatcher.config import ContainerConfig
from cairn.dispatcher.models import RunningTask
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.container_options import ContainerRuntimeOptions
from cairn.dispatcher.runtime.containers import ContainerManager, container_instance_id
from cairn.dispatcher.runtime.materials import MaterialsError, ProjectMaterials
from cairn.dispatcher.runtime.traffic import ProjectTrafficRuntime
from cairn.dispatcher.scheduler.execution_control import ExecutionControlObserver

pytestmark = pytest.mark.skipif(
    os.environ.get("CAIRN_C1_INTEGRATION") != "1" or os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1",
    reason="仅显式启用一次性 PostgreSQL 和隔离 Docker",
)

WRITER = r'''
import os, pathlib, subprocess, sys, time
root=pathlib.Path(sys.argv[1]); layer=pathlib.Path(sys.argv[2])
root.mkdir(parents=True, exist_ok=True)
(root/'parent.pid').write_text(str(os.getpid()))
child="import os,pathlib,time; p=pathlib.Path(%r); p.with_suffix('.pid').write_text(str(os.getpid())); f=p.open('ab',buffering=0); [(f.write(b'child\\x00\\xff\\n'),time.sleep(.05)) for _ in range(6000)]" % str(root/'child.log')
subprocess.Popen([sys.executable,'-c',child], start_new_session=True)
with (root/'parent.log').open('ab',buffering=0) as a, layer.open('ab',buffering=0) as b:
 for _ in range(6000):
  a.write(b'parent\x00\xff\n'); b.write(b'layer\x00\xff\n'); time.sleep(.05)
'''


def wait_for(check, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError("受控条件未在期限内成立")


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=20)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(20)
        # pytest 临时根可能超过 AF_UNIX 地址长度；仍连接原目录中的同一个 socket。
        descriptor = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            self.sock.connect(f"/proc/self/fd/{descriptor}/{self.path.name}")
        finally:
            os.close(descriptor)


def execute_scope(capability, directory, result, timeout_seconds=6):
    connection = UnixHTTP(directory / "execute.sock")
    try:
        body = dict(scope_id=capability["scope_id"], task_run_id=capability["task_run_id"],
                    command=shlex.join(["python3", "-u", "-c", WRITER,
                                       "/home/kali/workspace/test", "/tmp/c1-test-layer"]),
                    cwd="/home/kali/workspace", timeout_seconds=timeout_seconds)
        connection.request("POST", "/execute", json.dumps(body), {"Content-Type": "application/json"})
        response = connection.getresponse()
        result["status"] = response.status
        result["body"] = response.read()
    except (OSError, http.client.HTTPException) as exc:
        result["connection_closed"] = type(exc).__name__
    finally:
        connection.close()


@pytest.mark.parametrize("traffic_api", [1], indirect=True, ids=["one-producer"])
@pytest.mark.parametrize("finish_frozen", [False, True, "restart", "resume-only"])
def test_http_hot_pause_same_scene_and_complete_stop(traffic_api, tmp_path, finish_frozen):
    api = traffic_api
    run = api.task_runs[0]
    dispatcher_id = run["runtime_context"]["dispatcher_id"]
    beat = api.machine.dispatcher_heartbeat(dispatcher_id=dispatcher_id, status="running", config_hash="c1-fixture",
        running_task_count=1, active_project_count=1,
        runtime={"runtime_capabilities": ["project_hot_pause_v1", "project_runtime_v1"]})
    assert beat.ok
    config = ContainerConfig(image="arena-worker:c1-candidate", traffic_gateway_image="arena-traffic-gateway:local",
                             network_mode="none", completed_action="remove")
    manager = ContainerManager(config, instance_id=container_instance_id(dispatcher_id, api.base_url))
    manager.configure_traffic_execution(api.machine, root=tmp_path / "runtime", dispatcher_id=dispatcher_id, isolated=True)
    project = api.machine.get_project(api.project_id).project
    options = ContainerRuntimeOptions.build(image=config.image, network_mode="none", dns=[], cap_add=[],
        features=project.scenario_snapshot["features"], scenario_key=project.scenario_key)
    token = TaskCancellation(generation=project.execution_generation)
    pool = ThreadPoolExecutor(max_workers=1)
    loop = SimpleNamespace(client=api.machine, container_manager=manager, dispatcher_id=dispatcher_id, futures={})
    observer = ExecutionControlObserver(loop)
    report = control = None
    thread = None
    timings = []
    try:
        def register(daemon):
            result = api.machine.register_container_activation(project.id, project.execution_generation, daemon)
            assert result.ok, result.text
            return result.data["activation_token"]

        def finish_activation(activation_token, identity):
            result = api.machine.finish_container_activation(project.id, activation_token, identity)
            assert result.ok, result.text

        name = manager.activate_for_readiness(project.id, options, register=register, finish=finish_activation,
            abort=lambda *_args: pytest.fail("成功创建不能 abort"))
        control = manager._client.containers.get(name)
        fixture_files = control.exec_run(["sh", "-c",
            "mkdir -p /home/kali/workspace/material-proof; "
            "printf 'bind-proof' > /home/kali/workspace/material-proof/original; "
            "ln /home/kali/workspace/material-proof/original /home/kali/workspace/material-proof/hardlink; "
            "ln -s /tmp/c1-control-layer /tmp/c1-layer-link; ln -s /tmp /tmp/c1-layer-parent"])
        assert fixture_files.exit_code == 0
        materials = ProjectMaterials(manager._client)
        capability = manager.prepare_task_execution(project, run["id"], token, options)
        runtime = manager.traffic_execution._runtimes[project.id]
        test = manager._client.containers.get(runtime.manifest["scopes"][capability["scope_id"]]["container_id"])
        update = api.machine.update_task_run(project.id, run["id"], status="running", session_id="c1-same-session", runtime_context={
            "container_id": control.id, "container_name": control.name,
            "execution_container_ids": [control.id],
            "execution_lifecycle": {"generation": token.generation, "process_started": True,
                                    "termination_confirmed": False, "cleanup_confirmed": False},
        })
        assert update.ok, update.text
        report_name = manager.ensure_report_running(project.id, "c1-independent-report", options)
        report = manager._client.containers.get(report_name)
        report.exec_run(["sh", "-c", "while :; do printf x >> /tmp/c1-report; sleep .05; done"], detach=True)
        process = manager.build_exec_process(name, {}, ["python3", "-u", "-c", WRITER,
            "/home/kali/workspace/control", "/tmp/c1-control-layer"], timeout_seconds=6)
        token.register_execution(process)
        token.attach_process(process)
        process.start()
        future = pool.submit(process.communicate, 6)
        loop.futures[future] = RunningTask(project.id, "explore", run["worker_name"], "pi", token, options,
                                         task_run_id=run["id"], execution_generation=token.generation)
        scope_result = {}
        thread = threading.Thread(target=execute_scope, args=(capability, runtime.execution / capability["scope_id"], scope_result), daemon=True)
        thread.start()
        def writers_ready():
            if future.done():
                raise AssertionError(f"受控宿主进程提前退出: {future.result()}")
            if not thread.is_alive():
                raise AssertionError(f"受控工具连接提前退出: {scope_result}")
            return (runtime.workspace / "control/parent.log").exists() and (runtime.workspace / "test/child.log").exists()
        wait_for(writers_ready)
        identity = process.execution_identity
        pids = {path: path.read_bytes() for path in runtime.workspace.glob("*/*.pid")}
        observer.start()

        def action(action, physical):
            started = time.monotonic()
            response = api.user.post(f"{api.base_url}/projects/{project.id}/{action}", timeout=10)
            accepted = time.monotonic() - started
            assert response.status_code == 200, response.text
            assert response.json()["execution_control"]["state"] == "requested"
            def observed_control():
                result = api.user.get(f"{api.base_url}/projects/{project.id}/runtime?schema_version=2", timeout=10)
                assert result.status_code == 200, result.text
                snapshot = result.json()
                view = snapshot["execution_control"]
                assert view["request_id"] == response.json()["execution_control"]["request_id"]
                if view.get("state") in {"failed", "lost"} and physical != view["state"]:
                    pytest.fail(f"控制首次拒绝: {view['request_id']} {view['state']} {view.get('error')}")
                if view.get("state") != physical:
                    return None
                if physical == "paused":
                    assert snapshot["actions"]["resume"]["allowed"] is True
                return view

            value = wait_for(observed_control)
            elapsed = time.monotonic() - started
            timings.append({"action": action, "accepted_seconds": accepted, "physical_seconds": elapsed})
            assert value["request_id"] == response.json()["execution_control"]["request_id"]
            if physical == "lost":
                assert value["confirmed_at"] is None and value["error"] == "execution_scene_lost"
                receipt = value["receipts"][dispatcher_id]
                assert receipt["state"] == "lost" and receipt["confirmed_at"]
            else:
                assert value["confirmed_at"]
            return value

        action("pause", "paused")
        frozen = {path: path.read_bytes() for path in runtime.workspace.glob("*/*.log")}
        assert len(frozen) == 4
        report_before = manager.read_file(report.id, "/tmp/c1-report")
        # 跨越命令和宿主预算；Server 的长租约时钟另由定向 PostgreSQL 测试覆盖。
        time.sleep(7)
        assert all(path.read_bytes() == value for path, value in frozen.items())
        assert not future.done() and thread.is_alive()
        control.reload(); test.reload(); report.reload()
        assert control.status == test.status == "paused" and report.status == "running"
        assert len(manager.read_file(report.id, "/tmp/c1-report")) > len(report_before)
        layer_before = {control.id: manager.read_file(control.id, "/tmp/c1-control-layer")}
        assert {item["container_id"] for item in materials.containers(project.id)} >= {control.id, test.id, report.id}
        assert materials.list(project.id, control.id)["state"] == "paused"
        assert materials.list(project.id, control.id, "/tmp/c1-control-layer")["entries"][0]["size"] == len(layer_before[control.id])
        saved = tmp_path / "paused-layer.bin"
        saved_info = materials.save(project.id, control.id, "/tmp/c1-control-layer", saved)
        assert saved_info["state"] == "paused" and saved_info["hardlink_check"] == "tar_only"
        assert saved.read_bytes() == layer_before[control.id]
        for rejected in ("/home/kali/.cairn/pi/config.json", "/tmp/c1-layer-link",
                         "/tmp/c1-layer-parent/c1-control-layer", "/home/kali/workspace/material-proof/hardlink"):
            blocked = tmp_path / "blocked-material.bin"
            with pytest.raises(MaterialsError):
                materials.save(project.id, control.id, rejected, blocked)
            assert not blocked.exists()
        with pytest.raises(MaterialsError, match="materials_output_exists"):
            materials.save(project.id, control.id, "/tmp/c1-control-layer", saved)
        assert saved.read_bytes() == layer_before[control.id]
        control.reload()
        assert control.status == "paused" and control.id == identity[0]
        # 测试容器的所有权由其 manifest 核验，不冒充 pi 控制容器。
        stream, _ = test.get_archive("/tmp/c1-test-layer")
        assert b"".join(stream)
        if finish_frozen in (False, "resume-only"):
            action("resume", "running")
            wait_for(lambda: all(path.stat().st_size > len(value) for path, value in frozen.items()))
            assert process.execution_identity == identity and control.id == identity[0]
            assert all(path.read_bytes() == value for path, value in pids.items())
            assert not future.done()
            current = api.machine.get_project(project.id).project
            assert current.execution_generation == token.generation
            runs = api.machine.list_task_runs(project.id)
            assert sorted(item["id"] for item in runs.data) == sorted(item["id"] for item in api.task_runs)
            resumed_run = next(item for item in runs.data if item["id"] == run["id"])
            assert resumed_run["status"] == "running"
            assert resumed_run["session_id"] == "c1-same-session"
            assert resumed_run["execution_generation"] == token.generation
            assert not future.done() and thread.is_alive()
            if finish_frozen == "resume-only":
                # 反馈接缝只验收原地恢复；结束的取消/超时竞争仍保留在原测试分支。
                return
        if finish_frozen == "restart":
            from cairn.dispatcher.runtime.pause_clock import ExecutionDetached
            from cairn.dispatcher.scheduler.loop import DispatcherLoop
            from cairn.dispatcher.scheduler.task_lifecycle import TaskLifecycleMixin

            # 使用生产 close/reap；没有待重放收尾，不能把宿主退出写成业务结束。
            loop.execution_control_observer = observer
            loop.executor = pool
            loop.agent_executor = ThreadPoolExecutor(max_workers=1)
            loop.cleanup_executor = ThreadPoolExecutor(max_workers=1)
            loop.pending_task_run_finishes = {}
            loop._pending_task_run_finishes = lambda: set()
            loop._task_run_finish_futures = {}
            loop._reap_futures = lambda: TaskLifecycleMixin._reap_futures(loop)
            loop._refresh_candidate_recovery = lambda: None
            loop._flush_pending_task_run_finishes = lambda **_kwargs: None
            loop._flush_pending_report_finishes = lambda: None
            loop.report_finish_outbox = SimpleNamespace(entries=lambda: [])
            original_pid = control.attrs["State"]["Pid"]
            closed_at = time.monotonic()
            DispatcherLoop.close(loop)
            assert time.monotonic() - closed_at < 12
            with pytest.raises(ExecutionDetached):
                future.result()
            assert not loop.futures
            inspector = docker.from_env(timeout=5)
            try:
                still_frozen = inspector.containers.get(control.id)
                assert still_frozen.status == "paused" and still_frozen.attrs["State"]["Pid"] == original_pid
            finally:
                inspector.close()
            unchanged = api.machine.list_task_runs(project.id)
            assert unchanged.ok
            assert next(item for item in unchanged.data if item["id"] == run["id"])["status"] == "running"
            assert all(path.read_bytes() == value for path, value in frozen.items())
            manager = ContainerManager(config, instance_id=container_instance_id(dispatcher_id, api.base_url))
            manager.configure_traffic_execution(api.machine, root=tmp_path / "runtime", dispatcher_id=dispatcher_id, isolated=True)
            manager.traffic_execution.recover()
            loop.container_manager = manager
            materials = ProjectMaterials(manager._client)
            observer = ExecutionControlObserver(loop)
            observer.start()
            # 原模型流连接不在新进程中，明确 lost；禁止重建、重认领或先解冻。
            action("resume", "lost")
            still_frozen = manager._client.containers.get(control.id)
            assert still_frozen.status == "paused"
        snapshot = {path: path.read_bytes() for path in runtime.workspace.glob("*/*.log")}
        action("finish", "stopped")
        control.reload(); test.reload(); report.reload()
        assert control.status == test.status == "exited" and report.status == "running"
        assert control.attrs["State"]["Pid"] == test.attrs["State"]["Pid"] == 0
        if finish_frozen != "restart":
            result = future.result(timeout=10)
            token.finish_execution(process)
            assert result.cancelled and token.execution_lifecycle(task_finished=True)["termination_confirmed"] is True
        else:
            assert process.execution_completed is False
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert all(path.read_bytes().startswith(value) for path, value in snapshot.items())
        final_layer = manager.read_file(control.id, "/tmp/c1-control-layer")
        assert final_layer.startswith(layer_before[control.id])
        ended = tmp_path / "stopped-layer.bin"
        assert materials.save(project.id, control.id, "/tmp/c1-control-layer", ended)["state"] == "exited"
        assert ended.read_bytes() == final_layer
        control.reload()
        assert control.status == "exited" and control.id == identity[0]
        assert api.machine.get_project(project.id).project.execution_generation == token.generation + 1
        assert api.conn.execute("SELECT COUNT(*) AS n FROM assessment_report_finalizations WHERE project_id=?", (project.id,)).fetchone()["n"] == 0
    finally:
        print("C1_CONTROL_TIMINGS", json.dumps(timings), flush=True)
        if not token.detached:
            token.cancel("fixture_closed")
        if observer._thread.is_alive():
            observer.close()
        try:
            manager.cleanup_finishing(project.id, container_ids=[control.id] if control is not None else [])
            if report is not None:
                manager.stop_owned_report(project.id, report.id)
            if manager.traffic_execution is not None:
                manager.traffic_execution.destroy_project(project.id)
            for container in (control, report):
                if container is not None:
                    container.reload()
                    assert container.status in {"exited", "dead", "created"}
                    container.remove()
        finally:
            pool.shutdown(wait=True)
            manager.close()


def test_readiness_without_local_future_can_pause_resume_and_finish(traffic_api, tmp_path):
    api = traffic_api
    dispatcher_id = api.task_runs[0]["runtime_context"]["dispatcher_id"]
    assert api.machine.dispatcher_heartbeat(dispatcher_id=dispatcher_id, status="idle", config_hash="c1-readiness",
        running_task_count=0, active_project_count=0,
        runtime={"runtime_capabilities": ["project_hot_pause_v1", "project_runtime_v1"]}).ok
    created = api.user.post(f"{api.base_url}/projects", timeout=10, json={
        "title": "C1 无 Future 的隔离 readiness", "origin": "本机隔离容器", "goal": "验证控制归属，不启动研究",
        "scenario": "goal-driven-v1", "authorization_expires_at": None,
    })
    assert created.status_code == 201, created.text
    project = api.machine.get_project(created.json()["project"]["id"]).project
    config = ContainerConfig(image="arena-worker:c1-candidate", network_mode="none", completed_action="stop")
    manager = ContainerManager(config, instance_id=container_instance_id(dispatcher_id, api.base_url))
    manager.configure_traffic_execution(api.machine, root=tmp_path / "readiness", dispatcher_id=dispatcher_id, isolated=True)
    options = ContainerRuntimeOptions.build(image=config.image, network_mode="none", dns=[], cap_add=[],
        features=project.scenario_snapshot["features"], scenario_key=project.scenario_key)
    loop = SimpleNamespace(client=api.machine, container_manager=manager, dispatcher_id=dispatcher_id, futures={})
    observer = ExecutionControlObserver(loop)
    container = None
    try:
        def register(daemon):
            result = api.machine.register_container_activation(project.id, project.execution_generation, daemon)
            assert result.ok, result.text
            return result.data["activation_token"]

        def finish(token, identity):
            assert api.machine.finish_container_activation(project.id, token, identity).ok

        name = manager.activate_for_readiness(project.id, options, register=register, finish=finish,
            abort=lambda *_args: pytest.fail("成功创建不能 abort"))
        container = manager._client.containers.get(name)
        original_id = container.id
        observer.start()
        for action_name, physical in (("pause", "paused"), ("resume", "running"), ("finish", "stopped")):
            result = api.user.post(f"{api.base_url}/projects/{project.id}/{action_name}", timeout=10)
            assert result.status_code == 200, result.text
            receipt = wait_for(lambda: (control if (control := api.machine.get_project(project.id).project.execution_control).get("state") == physical else None))
            assert receipt["resources"] == [{"kind": "research", "container_id": original_id,
                "container_name": name, "state": physical}]
            container.reload()
            assert container.id == original_id and container.status == ("exited" if physical == "stopped" else physical)
        assert api.machine.list_task_runs(project.id).data == []
    finally:
        if observer._thread.is_alive():
            observer.close()
        if container is not None:
            manager._stop_retaining(container)
            container.remove()
        manager.traffic_execution.destroy_project(project.id)
        manager.close()


@pytest.mark.parametrize(("control_action", "blocked_at"), [
    ("pause", "create"), ("pause", "start"), ("pause", "readiness"),
    ("finish", "create"), ("finish", "readiness"),
    ("pause", "socket"), ("finish", "socket"),
])
def test_initializing_scope_does_not_block_physical_control(traffic_api, tmp_path, monkeypatch, blocked_at, control_action):
    api = traffic_api
    run, preparing_run = api.task_runs
    dispatcher_id = run["runtime_context"]["dispatcher_id"]
    assert api.machine.dispatcher_heartbeat(dispatcher_id=dispatcher_id, status="running", config_hash="c1.1-init",
        running_task_count=2, active_project_count=1,
        runtime={"runtime_capabilities": ["project_hot_pause_v1", "project_runtime_v1"]}).ok
    config = ContainerConfig(image="arena-worker:c1-candidate", traffic_gateway_image="arena-traffic-gateway:local",
                             network_mode="none", completed_action="stop")
    manager = ContainerManager(config, instance_id=container_instance_id(dispatcher_id, api.base_url))
    manager.configure_traffic_execution(api.machine, root=tmp_path / "initializing", dispatcher_id=dispatcher_id, isolated=True)
    project = api.machine.get_project(api.project_id).project
    options = ContainerRuntimeOptions.build(image=config.image, network_mode="none", dns=[], cap_add=[],
        features=project.scenario_snapshot["features"], scenario_key=project.scenario_key)
    token, preparing_token = (TaskCancellation(generation=project.execution_generation) for _ in range(2))
    entered, release, bound = (threading.Event() for _ in range(3))
    pool = ThreadPoolExecutor(max_workers=2)
    loop = SimpleNamespace(client=api.machine, container_manager=manager, dispatcher_id=dispatcher_id, futures={})
    observer = ExecutionControlObserver(loop)
    control = report = None
    thread = preparation = None
    timings = []
    try:
        name = manager.ensure_running(project.id, options)
        control = manager._client.containers.get(name)
        capability = manager.prepare_task_execution(project, run["id"], token, options)
        runtime = manager.traffic_execution._runtimes[project.id]
        for item, started in ((run, True), (preparing_run, False)):
            assert api.machine.update_task_run(project.id, item["id"], status="running", runtime_context={
                "container_id": control.id, "container_name": name, "execution_container_ids": [control.id],
                "session_id": "c1.1-original-session",
                "execution_lifecycle": {"generation": project.execution_generation, "process_started": started,
                                        "termination_confirmed": False, "cleanup_confirmed": False},
            }).ok
        report = manager._client.containers.get(manager.ensure_report_running(project.id, "c1.1-independent", options))
        process = manager.build_exec_process(name, {}, ["python3", "-u", "-c", WRITER,
            "/home/kali/workspace/control", "/tmp/c1.1-control"], timeout_seconds=60)
        token.register_execution(process)
        token.attach_process(process)
        process.start()
        future = pool.submit(process.communicate, 60)
        loop.futures[future] = RunningTask(project.id, "explore", run["worker_name"], "pi", token, options,
                                         task_run_id=run["id"], execution_generation=token.generation)
        scope_result = {}
        thread = threading.Thread(target=execute_scope,
            args=(capability, runtime.execution / capability["scope_id"], scope_result, 60), daemon=True)
        thread.start()
        wait_for(lambda: (runtime.workspace / "control/parent.log").exists()
                 and (runtime.workspace / "test/child.log").exists())
        identity = process.execution_identity
        pids = {path: path.read_bytes() for path in runtime.workspace.glob("*/*.pid")}
        create = manager._client.api.create_container
        initialize = runtime._initialize_network
        start_container = runtime._start_container
        path_exists = Path.exists
        register = api.machine.register_traffic_scope
        bindings = []

        def blocked_create(*args, **kwargs):
            labels = kwargs.get("labels", {})
            if (blocked_at == "create" and labels.get("io.cairn.traffic.role") == "executor"
                    and labels.get("io.cairn.traffic.task-run") == preparing_run["id"]):
                entered.set()
                assert release.wait(15)
            return create(*args, **kwargs)

        def blocked_initialize(container, command):
            if blocked_at == "readiness" and container.labels.get("io.cairn.traffic.task-run") == preparing_run["id"]:
                entered.set()
                assert release.wait(15)
            return initialize(container, command)

        def blocked_start(container, scope_id=None):
            if (blocked_at == "start" and container.labels.get("io.cairn.traffic.role") == "executor"
                    and container.labels.get("io.cairn.traffic.task-run") == preparing_run["id"]):
                entered.set()
                assert release.wait(15)
            return start_container(container, scope_id)

        def blocked_socket(path):
            state = runtime.manifest["scopes"].get(path.parent.name, {})
            if (blocked_at == "socket" and path.name == "execute.sock"
                    and state.get("task_run_id") == preparing_run["id"] and not entered.is_set()):
                entered.set()
                assert release.wait(15)
                # 强制走一轮生产 readiness 检查，覆盖冻结后不能误清理的窗口。
                return False
            return path_exists(path)

        def registered(project_id, binding):
            result = register(project_id, binding)
            if binding["task_run_id"] == preparing_run["id"]:
                assert result.ok, result.text
                bindings.append(dict(binding))
                bound.set()
            return result

        monkeypatch.setattr(manager._client.api, "create_container", blocked_create)
        monkeypatch.setattr(runtime, "_initialize_network", blocked_initialize)
        monkeypatch.setattr(runtime, "_start_container", blocked_start)
        monkeypatch.setattr(Path, "exists", blocked_socket)
        monkeypatch.setattr(api.machine, "register_traffic_scope", registered)
        preparation = pool.submit(manager.prepare_task_execution, project, preparing_run["id"], preparing_token, options)
        loop.futures[preparation] = RunningTask(project.id, "explore", preparing_run["worker_name"], "pi",
            preparing_token, options, task_run_id=preparing_run["id"], execution_generation=token.generation)
        def reached_block():
            if preparation.done():
                preparation.result()
                raise AssertionError("准备未到阻塞点便已完成")
            return entered.is_set()

        wait_for(reached_block, timeout=10)
        observer.start()

        def action(action_name, physical):
            started = time.monotonic()
            response = api.user.post(f"{api.base_url}/projects/{project.id}/{action_name}", timeout=10)
            accepted = time.monotonic() - started
            assert response.status_code == 200, response.text
            value = wait_for(lambda: (view if (view := api.machine.get_project(project.id).project.execution_control).get("state") == physical else None), timeout=10)
            elapsed = time.monotonic() - started
            timings.append({"action": action_name, "accepted_seconds": accepted, "physical_seconds": elapsed})
            assert value["request_id"] == response.json()["execution_control"]["request_id"]
            assert value["confirmed_at"] and value["resources"]
            return value

        receipt = action(control_action, "paused" if control_action == "pause" else "stopped")
        assert not preparation.done() and not release.is_set()
        frozen = {path: path.read_bytes() for path in runtime.workspace.glob("*/*.log")}
        control.reload(); report.reload()
        assert control.status == ("paused" if control_action == "pause" else "exited")
        assert report.status == "running"
        for state in runtime.manifest["scopes"].values():
            if state.get("container_id"):
                resource = manager._client.containers.get(state["container_id"])
                expected = "created" if blocked_at == "start" and state["task_run_id"] == preparing_run["id"] else ("paused" if control_action == "pause" else "exited")
                assert resource.status == expected
        # 用事件只留一个短写入采样窗口，不机械跨越数十秒。
        assert not release.wait(.15)
        assert all(path.read_bytes() == value for path, value in frozen.items())
        release.set()
        if control_action == "pause":
            assert bound.wait(5)
            pending = manager._client.containers.get(bindings[0]["container_id"])
            assert pending.status == ("paused" if blocked_at in {"readiness", "socket"} else "created")
            assert not preparation.done()
            action("resume", "running")
            result = preparation.result(timeout=10)
            assert result["scope_id"] == bindings[0]["id"]
            wait_for(lambda: all(path.stat().st_size > len(value) for path, value in frozen.items()))
            assert process.execution_identity == identity and all(path.read_bytes() == value for path, value in pids.items())
            assert api.machine.get_project(project.id).project.execution_generation == token.generation
            action("finish", "stopped")
        else:
            with pytest.raises((RuntimeError, docker.errors.DockerException)):
                preparation.result(timeout=10)
        result = future.result(timeout=10)
        assert result.cancelled
        # 既有 TrafficSync 可幂等重传 binding；不能把重传当成新的资源认领。
        assert len({binding["id"] for binding in bindings}) == 1
        assert all(binding == bindings[0] for binding in bindings)
        assert len(runtime.manifest["scopes"]) == 2
        for scope_id, state in runtime.manifest["scopes"].items():
            assert state["termination_confirmed"] is True
            row = api.conn.execute("SELECT * FROM http_capture_scopes WHERE project_id=? AND id=?", (project.id, scope_id)).fetchone()
            assert row["container_id"] == state["container_id"] and row["dispatcher_id"] == dispatcher_id
        for entry in runtime.manifest["resources"].values():
            if entry["kind"] == "container" and not entry["removed"]:
                resource = runtime._resource(entry)
                if resource is not None:
                    assert resource.status in {"created", "exited", "dead"}
                    assert all(resource.labels.get(key) == value for key, value in entry["labels"].items())
        assert len(api.machine.list_task_runs(project.id).data) == 2
        assert api.conn.execute("SELECT COUNT(*) AS n FROM assessment_report_finalizations WHERE project_id=?", (project.id,)).fetchone()["n"] == 0
        print("C1_1_INITIALIZATION_TIMINGS", blocked_at, control_action, json.dumps(timings))
    finally:
        release.set()
        token.cancel("fixture_closed"); preparing_token.cancel("fixture_closed")
        manager.hot_control(project.id, "finish", container_ids=[control.id] if control is not None else [], in_flight=False)
        if observer._thread.is_alive():
            observer.close()
        pool.shutdown(wait=True)
        if thread is not None:
            thread.join(timeout=5)
        if report is not None:
            manager.stop_owned_report(project.id, report.id)
        manager.traffic_execution.destroy_project(project.id)
        for resource in (control, report):
            if resource is not None:
                manager._stop_retaining(resource)
                resource.remove()
        manager.close()


def test_pi_runner_deadline_survives_real_freeze(tmp_path):
    """合成 provider 保持在线，研究 runner 真冻结；不调用平台模型或外部目标。"""
    root = tmp_path / "pi-clock"
    root.mkdir()
    for child in ("execution", "output", "prompts"):
        (root / child).mkdir()
        os.chown(root / child, 1000, 1000)
    provider_script = r'''
import { writeFile } from 'node:fs/promises';
import { fixture, ok } from '/opt/cairn/pi-agent/tests/fixtures.mjs';
const { request, calls } = await fixture({ after: () => {} }, 'openai-chat-completions', [async call => {
  await writeFile('/proof/call-' + calls.length + '.json', JSON.stringify({ time: Date.now(), messages: call.body.messages }));
  await writeFile('/proof/ready', String(calls.length));
  await new Promise(resolve => setTimeout(resolve, 1000));
  return ok;
}]);
request.timeout_seconds = 4;
await writeFile('/proof/request.json', JSON.stringify(request));
'''
    runner_script = r'''
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { runRequest } from '/opt/cairn/pi-agent/dist/runner.js';
const request = JSON.parse(await readFile('/proof/request.json', 'utf8'));
await mkdir(request.cwd, { recursive: true });
await mkdir(request.session_root, { recursive: true });
await writeFile('/proof/identity.json', JSON.stringify({ pid: process.pid, session: request.session }));
const audit = [];
const result = await runRequest(request, { audit: event => audit.push(event) });
await writeFile('/proof/result.json', JSON.stringify({ result, audit, pid: process.pid, session: request.session }));
'''
    client = docker.from_env(timeout=5)
    runtime = ProjectTrafficRuntime(client, project_id="c1-pi-clock", root=root,
        gateway_image="arena-traffic-gateway:local", executor_image="arena-worker:c1-candidate", isolated=True)
    runtime.manifest = {"scopes": {}}
    fixtures = Path(__file__).resolve().parents[2] / "container/pi-agent/tests/fixtures.mjs"
    common_volumes = {str(fixtures): {"bind": "/opt/cairn/pi-agent/tests/fixtures.mjs", "mode": "ro"},
                      str(root / "output"): {"bind": "/proof", "mode": "rw"},
                      str(root / "prompts"): {"bind": "/tmp/cairn-prompts", "mode": "rw"}}
    container = client.containers.run("arena-worker:c1-candidate", ["sleep", "infinity"],
        detach=True, network_mode="none", labels={"io.cairn.c1-test": "pi-pause-clock"},
        volumes={**common_volumes, str(root / "execution"): {"bind": "/run/cairn-exec", "mode": "ro"}})
    provider = None
    try:
        # provider 与 runner 共享隔离的 loopback，但不共享冻结 cgroup，避免同时冻结远端替身。
        provider = client.containers.run("arena-worker:c1-candidate", ["node", "--input-type=module", "-e", provider_script],
            detach=True, network_mode=f"container:{container.id}", labels={"io.cairn.c1-test": "pi-pause-provider"}, volumes=common_volumes)
        wait_for((root / "output/request.json").exists)
        execution = client.api.exec_create(container.id, ["node", "--input-type=module", "-e", runner_script])["Id"]
        client.api.exec_start(execution, detach=True)
        wait_for((root / "output/ready").exists)
        identity = json.loads((root / "output/identity.json").read_text())
        runtime.hot_control(True)
        container.pause()
        time.sleep(5)
        assert not (root / "output/result.json").exists()
        runtime.hot_control(False)
        container.unpause()
        wait_for((root / "output/result.json").exists)
        proof = json.loads((root / "output/result.json").read_text())
        assert proof["result"]["status"] == "succeeded", proof
        assert (root / "output/ready").read_text() == "1"
        assert proof["pid"] == identity["pid"] and proof["session"] == identity["session"]
        wait_for(lambda: client.api.exec_inspect(execution)["Running"] is False)
        assert client.api.exec_inspect(execution)["ExitCode"] == 0
    finally:
        for owned in (provider, container):
            if owned is None:
                continue
            owned.reload()
            if owned.status == "paused":
                owned.kill(signal="SIGKILL")
            elif owned.status not in {"exited", "dead", "created"}:
                owned.stop(timeout=1)
            owned.remove()
        client.close()


@pytest.mark.parametrize("returncode", [0, 7])
def test_normal_or_failed_execution_retains_layer(returncode):
    manager = ContainerManager(ContainerConfig(image="arena-worker:c1-candidate", network_mode="none", completed_action="remove"),
        instance_id=container_instance_id(f"c1-retention-{returncode}-{time.monotonic_ns()}", "http://127.0.0.1/c1-fixture"))
    container = None
    try:
        name = manager.ensure_running("c1-retention")
        container = manager._client.containers.get(name)
        process = manager.build_exec_process(name, {}, ["python3", "-c",
            "import pathlib,sys; pathlib.Path('/tmp/c1-outcome').write_bytes(b'evidence\\x00\\xff'); sys.exit(int(sys.argv[1]))", str(returncode)])
        process.start()
        result = process.communicate(timeout=10)
        assert result.returncode == returncode
        assert manager.cleanup_completed("c1-retention")
        container.reload()
        assert container.status == "exited" and container.attrs["State"]["Pid"] == 0
        assert manager.read_file(container.id, "/tmp/c1-outcome") == b"evidence\x00\xff"
    finally:
        if container is not None:
            manager._stop_retaining(container)
            container.remove()
        manager.close()
