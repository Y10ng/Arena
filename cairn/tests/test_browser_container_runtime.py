from __future__ import annotations

import json
from pathlib import Path
import re
import threading
from types import SimpleNamespace

from docker.errors import NotFound
import pytest

from cairn.dispatcher.config import ContainerConfig
from cairn.dispatcher.runtime.browser_session_cleanup import CLI_VERSION, CORE_VERSION
from cairn.dispatcher.runtime.containers import (
    ContainerManager,
    ContainerOwnershipError,
    container_instance_id,
)


INSTANCE_ID = container_instance_id("browser-runtime-test", "https://server.example.test")
CONTAINER_NAME = f"cairn-pi-dispatch-{INSTANCE_ID[:16]}-proj_a"


def test_image_playwright_cli_is_pinned_to_supported_cleanup_version() -> None:
    dockerfile = (Path(__file__).resolve().parents[2] / "container/Dockerfile").read_text(encoding="utf-8")
    versions = re.findall(r"npm install -g @playwright/cli@([^\s]+)", dockerfile)
    assert versions == [CLI_VERSION] == ["0.1.15"]
    # 公共npm的CLI 0.1.15元数据精确依赖此core；静态门禁不替代镜像内验收。
    assert CORE_VERSION == "1.62.0-alpha-2026-06-29"


class FakeDockerAPI:
    def __init__(self):
        self.executions = {}
        self.commands = []
        self.helper_status = "closed"

    def exec_create(self, container_id, command, **kwargs):
        exec_id = f"exec_{len(self.commands)}"
        self.commands.append((container_id, command, kwargs))
        self.executions[exec_id] = {
            "Running": False, "ExitCode": 0, "ContainerID": container_id,
        }
        return {"Id": exec_id}

    def exec_start(self, exec_id, **kwargs):
        _, command, _ = self.commands[int(exec_id.split("_")[-1])]
        payload = (
            {"status": "ok", "context": {"uid": 1000, "cache_root": "/home/kali/.cache"}}
            if command[-1] == "probe" else {"status": self.helper_status}
        )
        yield json.dumps(payload).encode(), None

    def exec_inspect(self, exec_id):
        return self.executions[exec_id]


class FakeDocker:
    def __init__(self):
        self.api = FakeDockerAPI()
        self.container = SimpleNamespace(
            id="original-container", name=CONTAINER_NAME, status="running", client=self,
            attrs={"Config": {"Env": [], "User": "kali", "Labels": {
                "cairn.pi.instance_id": INSTANCE_ID,
                "cairn.engine": "pi",
                "cairn.project_id": "proj_a",
            }}},
        )
        self.containers = SimpleNamespace(get=self.get)

    def get(self, identity):
        if identity not in (CONTAINER_NAME, self.container.id):
            raise NotFound("missing container")
        return self.container


@pytest.fixture
def runtime(monkeypatch):
    docker = FakeDocker()
    monkeypatch.setattr("cairn.dispatcher.runtime.containers.docker.from_env", lambda *, timeout: docker)
    config = ContainerConfig(
        image="worker:test", network_mode="none", cap_add=[], completed_action="remove",
    )
    return ContainerManager(config, instance_id=INSTANCE_ID), docker


def test_legacy_and_explicit_sessions_leave_worker_environment_untouched(runtime):
    manager, docker = runtime
    env = {"PATH": "/usr/local/bin:/usr/bin", "API_KEY": "not-for-cleanup"}
    result, session = manager.prepare_browser_session(
        CONTAINER_NAME, env, project_id="proj_a", task_run_id="run_legacy",
    )
    assert result == env and session is None
    manager.configure_browser_sessions("task")
    configured = {**env, "PLAYWRIGHT_CLI_SESSION": "shared-browser"}
    result, session = manager.prepare_browser_session(
        CONTAINER_NAME, configured, project_id="proj_a", task_run_id="run_shared",
    )
    assert result == configured and session is None
    assert env == {"PATH": "/usr/local/bin:/usr/bin", "API_KEY": "not-for-cleanup"}
    assert docker.api.commands == []


def test_browser_helper_rejects_boolean_exit_code(
    runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, docker = runtime
    monkeypatch.setattr(
        docker.api,
        "exec_inspect",
        lambda _exec_id: {
            "Running": False,
            "ExitCode": False,
            "ContainerID": docker.container.id,
        },
    )

    assert manager._run_browser_helper(
        docker.container.id,
        {},
        ["probe"],
    ) == {"status": "retry"}


def test_browser_helper_rejects_foreign_container(
    runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, docker = runtime
    monkeypatch.setattr(
        docker.api,
        "exec_inspect",
        lambda _exec_id: {
            "Running": False,
            "ExitCode": 0,
            "ContainerID": "foreign-container",
        },
    )

    assert manager._run_browser_helper(
        docker.container.id,
        {},
        ["probe"],
    ) == {"status": "retry"}


def test_managed_phases_share_exact_session_without_retaining_worker_secrets(runtime):
    manager, docker = runtime
    manager.configure_browser_sessions("task")
    env = {"PATH": "/usr/local/bin:/usr/bin", "HOME": "/home/kali", "API_KEY": "private"}
    first_env, first = manager.prepare_browser_session(
        CONTAINER_NAME, env, project_id="proj_a", task_run_id="run_a",
    )
    assert first is not None
    assert first_env == {**env, "PLAYWRIGHT_CLI_SESSION": first.name}
    assert "PLAYWRIGHT_CLI_SESSION" not in env
    assert "private" not in repr(first)
    assert dict(first.env) == {"PATH": "/usr/local/bin:/usr/bin", "HOME": "/home/kali"}
    second_env, second = manager.prepare_browser_session(
        CONTAINER_NAME, env, project_id="proj_a", task_run_id="run_a",
    )
    assert second == first and second_env == first_env
    _, other = manager.prepare_browser_session(
        CONTAINER_NAME, env, project_id="proj_a", task_run_id="run_b",
    )
    assert other is not None and other.name != first.name
    assert first.container_id == "original-container"
    assert all("API_KEY" not in command[2]["environment"] for command in docker.api.commands)


def test_policy_reload_only_changes_new_task_runs(runtime):
    manager, _ = runtime
    name = CONTAINER_NAME
    assert manager.prepare_browser_session(name, {}, project_id="proj_a", task_run_id="legacy")[1] is None
    manager.configure_browser_sessions("task")
    assert manager.prepare_browser_session(name, {}, project_id="proj_a", task_run_id="legacy")[1] is None
    _, managed = manager.prepare_browser_session(name, {}, project_id="proj_a", task_run_id="managed")
    assert managed is not None
    manager.configure_browser_sessions("legacy")
    assert manager.prepare_browser_session(name, {}, project_id="proj_a", task_run_id="managed")[1] == managed
    assert manager.prepare_browser_session(name, {}, project_id="proj_a", task_run_id="new")[1] is None


@pytest.mark.parametrize("session_value", ["", "shared-browser"])
def test_container_configured_session_is_never_overridden(runtime, session_value):
    manager, docker = runtime
    manager.configure_browser_sessions("task")
    docker.container.attrs["Config"]["Env"] = [f"PLAYWRIGHT_CLI_SESSION={session_value}"]
    env, session = manager.prepare_browser_session(
        CONTAINER_NAME, {}, project_id="proj_a", task_run_id="inherited",
    )
    assert env == {} and session is None
    assert docker.api.commands == []


def ready_session(manager, docker):
    manager.configure_browser_sessions("task")
    _, session = manager.prepare_browser_session(
        CONTAINER_NAME, {}, project_id="proj_a", task_run_id="run_a",
    )
    docker.api.executions["worker"] = {
        "Running": False, "ExitCode": 137, "ContainerID": "original-container",
    }
    manager.browser_sessions.register_exec(session, ("original-container", "worker"))
    manager.browser_sessions.finish_exec(session, ("original-container", "worker"), completed=True)
    return session


def test_cleanup_requires_both_completion_gates_and_actual_stopped_exec(runtime):
    manager, docker = runtime
    session = ready_session(manager, docker)
    assert manager.cleanup_browser_session(session) == "skipped"
    manager.browser_sessions.local_finished("proj_a", "run_a")
    assert manager.cleanup_browser_session(session) == "skipped"
    manager.browser_sessions.terminal_confirmed("proj_a", "run_a")
    assert manager.browser_sessions.claim_ready() == [session]
    docker.api.executions["worker"]["Running"] = True
    assert manager.cleanup_browser_session(session) == "retry"
    assert len(docker.api.commands) == 1
    docker.api.executions["worker"]["Running"] = False
    assert manager.cleanup_browser_session(session) == "closed"
    container_id, command, kwargs = docker.api.commands[-1]
    assert container_id == "original-container"
    assert command[-3:] == ["cleanup", session.name, session.context_json]
    assert kwargs["environment"] == dict(session.env)
    manager.browser_sessions.finish_cleanup(session, "closed")
    assert manager.cleanup_browser_session(session) == "skipped"
    assert len(docker.api.commands) == 2


@pytest.mark.parametrize("change", ["container", "namespace", "explicit", "node_options"])
def test_changed_execution_context_fences_original_scope(runtime, change):
    manager, docker = runtime
    session = ready_session(manager, docker)
    env = {}
    if change == "container":
        docker.container.id = "replacement-container"
    elif change == "namespace":
        env["HOME"] = "/other-home"
    elif change == "explicit":
        env["PLAYWRIGHT_CLI_SESSION"] = "shared"
    else:
        env["NODE_OPTIONS"] = "--require=/different-runtime.js"
    later_env, later = manager.prepare_browser_session(
        CONTAINER_NAME, env, project_id="proj_a", task_run_id="run_a",
    )
    if change == "explicit":
        assert later is None and later_env == env
    else:
        assert later == session and later_env["PLAYWRIGHT_CLI_SESSION"] == session.name
    manager.browser_sessions.local_finished("proj_a", "run_a")
    manager.browser_sessions.terminal_confirmed("proj_a", "run_a")
    manager.browser_sessions.claim_ready()
    assert manager.cleanup_browser_session(session) == "skipped"
    assert not any(command[-3] == "cleanup" for _, command, _ in docker.api.commands)


@pytest.mark.parametrize("state", [None, "exited", "replacement"])
def test_cleanup_never_restarts_or_uses_same_name_replacement(runtime, state):
    manager, docker = runtime
    session = ready_session(manager, docker)
    manager.browser_sessions.local_finished("proj_a", "run_a")
    manager.browser_sessions.terminal_confirmed("proj_a", "run_a")
    manager.browser_sessions.claim_ready()
    if state == "exited":
        docker.container.status = "exited"
    else:
        docker.container.id = "replacement-container"
    assert manager.cleanup_browser_session(session) in {"absent", "skipped"}
    assert len(docker.api.commands) == 1


@pytest.mark.parametrize("caller_pause", [0, 3.1])
def test_unresponsive_docker_helpers_are_bounded_and_do_not_kill_processes(runtime, monkeypatch, caller_pause):
    manager, docker = runtime
    manager.configure_browser_sessions("task")
    monkeypatch.setattr("cairn.dispatcher.runtime.containers.BROWSER_HELPER_TIMEOUT_SECONDS", 0.02, raising=False)
    release = threading.Event()
    entered = threading.Event()

    def blocked_start(*args, **kwargs):
        entered.set()
        release.wait()
        yield b'{"status":"skipped"}', None

    monkeypatch.setattr(docker.api, "exec_start", blocked_start)
    try:
        for index in range(4):
            entered.clear()
            result, session = manager.prepare_browser_session(
                CONTAINER_NAME, {}, project_id="proj_a", task_run_id=f"run_{index}",
            )
            assert result == {} and session is None
            assert entered.wait(5), "helper did not enter the blocked stub"
        # 慢调用方不能让无响应桩自行解阻，否则第五次获准执行并不代表并发上限失效。
        release.wait(caller_pause)
        result, session = manager.prepare_browser_session(
            CONTAINER_NAME, {}, project_id="proj_a", task_run_id="run_4",
        )
        assert result == {} and session is None
        assert len(docker.api.commands) == 4
    finally:
        release.set()


def test_later_phase_keeps_binding_when_probe_is_unavailable(runtime, monkeypatch):
    manager, docker = runtime
    session = ready_session(manager, docker)
    calls_before = len(docker.api.commands)

    def unavailable(*args, **kwargs):
        yield b'{"status":"retry"}', None

    monkeypatch.setattr(docker.api, "exec_start", unavailable)
    env, later = manager.prepare_browser_session(
        CONTAINER_NAME, {}, project_id="proj_a", task_run_id="run_a",
    )
    assert later == session
    assert env["PLAYWRIGHT_CLI_SESSION"] == session.name
    assert manager.browser_sessions.policy_for_task("proj_a", "run_a", "legacy") == "task"
    assert len(docker.api.commands) == calls_before


def test_created_exec_without_completed_stream_never_authorizes_cleanup(runtime):
    manager, docker = runtime
    session = ready_session(manager, docker)
    docker.api.executions["not_started"] = {
        "Running": False, "ExitCode": 0, "ContainerID": "original-container",
    }
    manager.browser_sessions.register_exec(session, ("original-container", "not_started"))
    manager.browser_sessions.local_finished("proj_a", "run_a")
    manager.browser_sessions.terminal_confirmed("proj_a", "run_a")
    manager.browser_sessions.claim_ready()
    assert manager.cleanup_browser_session(session) == "skipped"
    assert len(docker.api.commands) == 1


@pytest.mark.parametrize("change", ["owner", "engine", "name", "project", "missing"])
def test_browser_prepare_and_cleanup_refuse_changed_ownership(runtime, change):
    manager, docker = runtime
    session = ready_session(manager, docker)
    manager.browser_sessions.local_finished("proj_a", "run_a")
    manager.browser_sessions.terminal_confirmed("proj_a", "run_a")
    assert manager.browser_sessions.claim_ready() == [session]
    labels = docker.container.attrs["Config"]["Labels"]
    if change == "owner":
        labels["cairn.pi.instance_id"] = container_instance_id("other", "https://server.example.test")
    elif change == "engine":
        labels["cairn.engine"] = "legacy"
    elif change == "name":
        docker.container.name = "cairn-dispatch-proj_a"
    elif change == "project":
        labels["cairn.project_id"] = "proj_other"
    else:
        labels.clear()
    with pytest.raises(ContainerOwnershipError):
        manager.prepare_browser_session(CONTAINER_NAME, {}, project_id="proj_a", task_run_id="new")
    assert manager.cleanup_browser_session(session) == "skipped"
    assert len(docker.api.commands) == 1
    # helper线程在exec_create之前独立重验，即使调用方已持有旧container id也不能绕过。
    assert manager._run_browser_helper(docker.container.id, {}, ["probe"]) == {"status": "retry"}
    assert len(docker.api.commands) == 1


def test_browser_prepare_refuses_wrong_task_project(runtime):
    manager, docker = runtime
    manager.configure_browser_sessions("task")
    with pytest.raises(ContainerOwnershipError, match="project"):
        manager.prepare_browser_session(CONTAINER_NAME, {}, project_id="proj_other", task_run_id="new")
    assert docker.api.commands == []
