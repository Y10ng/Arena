from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from cairn.dispatcher.runtime.browser_sessions import BrowserSessions
from cairn.dispatcher.runtime.process import ManagedProcess, ProcessResult
from cairn.dispatcher.tasks.process_runner import run_worker_process


class DockerAPI:
    def __init__(self):
        self.created = []
        self.controls = []

    def exec_create(self, container_id, command, **kwargs):
        if kwargs.get("stdout") is False:
            self.controls.append((container_id, command, kwargs))
            return {"Id": f"control-{len(self.controls)}"}
        self.created.append((container_id, command, kwargs))
        return {"Id": f"exec-{len(self.created)}"}

    def exec_start(self, exec_id, **kwargs):
        return iter([(b"worker output", b"")])

    def exec_inspect(self, exec_id):
        return {"Running": False, "ExitCode": 0}


def docker_container(api):
    return SimpleNamespace(id="container-1", name="worker", client=SimpleNamespace(api=api))


def test_managed_process_exposes_actual_execution_identity_after_start():
    api = DockerAPI()
    container = docker_container(api)
    process = ManagedProcess(container, ["worker"], {})
    assert process.execution_identity is None

    process.start()
    result = process.communicate(timeout=5)
    try:
        assert result.read_stdout() == "worker output"
        assert process.execution_identity == ("container-1", "exec-1")
        container.id = "replacement-container"
        assert process.execution_identity == ("container-1", "exec-1")
    finally:
        result.cleanup()


def test_execution_completed_requires_normal_eof_and_finished_reader():
    inspect_entered = threading.Event()
    finish_inspect = threading.Event()

    class SlowInspectAPI(DockerAPI):
        def exec_inspect(self, exec_id):
            inspect_entered.set()
            assert finish_inspect.wait(5)
            return super().exec_inspect(exec_id)

    process = ManagedProcess(docker_container(SlowInspectAPI()), ["worker"], {})
    assert process.execution_completed is False
    process.start()
    try:
        assert inspect_entered.wait(5)
        assert process.execution_completed is False
    finally:
        finish_inspect.set()
        result = process.communicate(timeout=5)
        result.cleanup()
    assert process.execution_completed is True


class ProcessManager:
    def __init__(self):
        self.api = DockerAPI()
        self.container = docker_container(self.api)
        self.browser_sessions = BrowserSessions()
        self.prepared = []
        self.handles = []

    def prepare_browser_session(self, container_name, env, *, project_id, task_run_id):
        self.prepared.append(dict(env))
        session = self.browser_sessions.prepare(
            project_id, task_run_id, self.container.id, env={}, context={}
        )
        self.handles.append(session)
        return {**env, "PLAYWRIGHT_CLI_SESSION": session.name}, session

    def write_text_file(self, *args, **kwargs):
        pass

    def build_exec_process(self, container_name, env, command, *, timeout_seconds):
        return ManagedProcess(self.container, command, env)


def run_phase(manager, phase, *, worker=None, payload_recorder_enabled=False):
    return run_worker_process(
        manager, "worker", worker or SimpleNamespace(name="mock", type="mock", env={}),
        ["worker", phase], phase=phase, timeout_seconds=30,
        project_id="project-1", task_run_id="run-1",
        payload_recorder_enabled=payload_recorder_enabled,
    )


def test_browser_prepare_keeps_payload_path_and_worker_env_across_phases():
    manager = ProcessManager()
    original_env = {"PATH": "/worker/bin:/usr/bin", "WORKER_TOKEN": "private-worker-value"}
    worker = SimpleNamespace(name="mock", type="mock", env=dict(original_env))

    for phase in ("explore_execute", "explore_conclude"):
        with run_phase(manager, phase, worker=worker, payload_recorder_enabled=True) as result:
            assert result.read_stdout() == "worker output"

    assert len(manager.prepared) == 2
    expected_path = "/home/kali/workspace/.cairn/bin:/worker/bin:/usr/bin"
    assert all(env["PATH"] == expected_path for env in manager.prepared)
    assert all(env["CAIRN_PAYLOAD_LOG"].startswith("/tmp/cairn-payloads/project-1/run-1/") for env in manager.prepared)
    execute_env, conclude_env = [call[2]["environment"] for call in manager.api.created]
    assert execute_env["PLAYWRIGHT_CLI_SESSION"] == conclude_env["PLAYWRIGHT_CLI_SESSION"]
    assert execute_env["PATH"] == conclude_env["PATH"] == expected_path
    assert worker.env == original_env
    assert manager.browser_sessions.claim_ready() == []


def test_worker_phases_register_actual_execs_without_finishing_browser_session():
    manager = ProcessManager()
    phases = ("reason_execute", "bootstrap", "bootstrap_conclude", "explore_execute", "explore_conclude", "report_execute")
    for phase in phases:
        with run_phase(manager, phase) as result:
            assert result.returncode == 0
        manager.browser_sessions.terminal_confirmed("project-1", "run-1")
        assert manager.browser_sessions.claim_ready() == []

    session = manager.handles[0]
    assert all(handle == session for handle in manager.handles)
    assert manager.browser_sessions.execution_ids(session) == (
        "exec-1", "exec-2", "exec-3", "exec-4", "exec-5", "exec-6",
    )


def test_completed_real_process_proves_all_execs_before_cleanup():
    manager = ProcessManager()
    for phase in ("explore_execute", "explore_conclude"):
        with run_phase(manager, phase):
            pass
    session = manager.handles[0]
    manager.browser_sessions.terminal_confirmed("project-1", "run-1")
    assert manager.browser_sessions.claim_ready() == []
    manager.browser_sessions.local_finished("project-1", "run-1")
    assert manager.browser_sessions.claim_ready() == [session]
    assert manager.browser_sessions.cleanup_allowed(session) is True


def test_late_exec_start_timeout_permanently_fences_even_after_normal_completion(monkeypatch):
    start_entered = threading.Event()
    release_start = threading.Event()

    class SlowStartAPI(DockerAPI):
        def exec_start(self, exec_id, **kwargs):
            start_entered.set()
            assert release_start.wait(5)
            return super().exec_start(exec_id, **kwargs)

    class ImmediateTimeoutProcess(ManagedProcess):
        def communicate(self, timeout):
            # 只缩短等待预算，保留真实 reader、kill、abandon 与结果处理路径。
            assert start_entered.wait(5)
            return super().communicate(timeout=0)

    manager = ProcessManager()
    manager.api = SlowStartAPI()
    manager.container = docker_container(manager.api)
    process = ImmediateTimeoutProcess(manager.container, ["worker"], {})
    monkeypatch.setattr(manager, "build_exec_process", lambda *args, **kwargs: process)
    monkeypatch.setattr("cairn.dispatcher.runtime.process.EXEC_KILL_JOIN_TIMEOUT_SECONDS", 0)
    try:
        with run_phase(manager, "explore_execute") as result:
            assert result.timed_out is True
            assert result.returncode == 137
            assert process.execution_completed is False
            assert manager.api.exec_inspect("exec-1")["Running"] is False
        session = manager.handles[0]
        manager.browser_sessions.local_finished("project-1", "run-1")
        manager.browser_sessions.terminal_confirmed("project-1", "run-1")
        assert manager.browser_sessions.claim_ready() == [session]
        assert manager.browser_sessions.cleanup_allowed(session) is False
        assert manager.browser_sessions.execution_ids(session) is None
    finally:
        release_start.set()
        process._reader.join(timeout=5)
    assert not process._reader.is_alive()
    assert process.execution_completed is False
    manager.browser_sessions.finish_exec(session, process.execution_identity, completed=True)
    assert manager.browser_sessions.cleanup_allowed(session) is False


@pytest.mark.parametrize("failure_stage", ("build", "create", "output_file"))
def test_failed_process_start_records_known_identity_or_fences_unknown_exec(failure_stage, monkeypatch):
    manager = ProcessManager()

    def fail(*args, **kwargs):
        raise OSError("start failed")

    if failure_stage == "build":
        monkeypatch.setattr(manager, "build_exec_process", fail)
    elif failure_stage == "create":
        monkeypatch.setattr(manager.api, "exec_create", fail)
    else:
        monkeypatch.setattr("cairn.dispatcher.runtime.process.tempfile.mkstemp", fail)

    with pytest.raises(OSError, match="start failed"):
        run_phase(manager, "explore_execute")

    session = manager.handles[0]
    assert bool(manager.api.created) is (failure_stage == "output_file")
    assert manager.browser_sessions.execution_ids(session) is None
    manager.browser_sessions.local_finished("project-1", "run-1")
    manager.browser_sessions.terminal_confirmed("project-1", "run-1")
    assert manager.browser_sessions.claim_ready() == [session]
    assert manager.browser_sessions.cleanup_allowed(session) is False


@pytest.mark.parametrize("existing_session", (False, True))
@pytest.mark.parametrize("explicit_session", (None, "caller-session", ""))
def test_browser_prepare_error_preserves_execution_env_and_fences_scope(
    existing_session, explicit_session, monkeypatch, caplog,
):
    manager = ProcessManager()
    if existing_session:
        with run_phase(manager, "explore_execute"):
            pass
    original_env = {"PATH": "/worker/bin", "KEY": "original"}
    if explicit_session is not None:
        original_env["PLAYWRIGHT_CLI_SESSION"] = explicit_session
    worker = SimpleNamespace(name="mock", type="mock", env=dict(original_env))
    observed_identities = []

    class ObservedProcess(ManagedProcess):
        @property
        def execution_identity(self):
            identity = super().execution_identity
            observed_identities.append(identity)
            return identity

    monkeypatch.setattr(
        manager, "build_exec_process",
        lambda container_name, env, command, **kwargs: ObservedProcess(manager.container, command, env),
    )

    def fail_prepare(container_name, env, **kwargs):
        env["KEY"] = "private mutated value"
        raise RuntimeError("private prepare detail")

    monkeypatch.setattr(manager, "prepare_browser_session", fail_prepare)
    with run_phase(manager, "explore_conclude", worker=worker) as result:
        assert result.returncode == 0
        assert result.read_stdout() == "worker output"

    expected_env = dict(original_env)
    if existing_session and explicit_session is None:
        expected_env["PLAYWRIGHT_CLI_SESSION"] = manager.handles[0].name
    assert manager.api.created[-1][2]["environment"] == expected_env
    assert bool(observed_identities) is (existing_session and explicit_session is None)
    assert all(identity == ("container-1", "exec-2") for identity in observed_identities)
    assert worker.env == original_env
    assert manager.browser_sessions.policy_for_task("project-1", "run-1", "task") == (
        "task" if existing_session else "legacy"
    )
    if existing_session:
        assert manager.browser_sessions.execution_ids(manager.handles[0]) is None
    assert "private" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_managed_scope_with_legacy_process_missing_identity_is_fenced(monkeypatch):
    manager = ProcessManager()
    process = SimpleNamespace(
        start=lambda: None,
        communicate=lambda **kwargs: ProcessResult(returncode=0, stdout="original output", stderr=""),
    )
    monkeypatch.setattr(manager, "build_exec_process", lambda *args, **kwargs: process)
    with run_phase(manager, "explore_execute") as result:
        assert result.read_stdout() == "original output"
    assert manager.browser_sessions.execution_ids(manager.handles[0]) is None


@pytest.mark.parametrize("proof", ("missing", "false", "getter_error"))
@pytest.mark.parametrize("communicate_failed", (False, True))
def test_unknown_completion_fences_without_overwriting_result_or_error(
    proof, communicate_failed, monkeypatch, caplog,
):
    manager = ProcessManager()

    class IncompleteProcess:
        execution_identity = ("container-1", "exec-1")

        def start(self):
            pass

        def communicate(self, **kwargs):
            if communicate_failed:
                raise OSError("original communicate failure")
            return ProcessResult(returncode=0, stdout="original output", stderr="")

    class BrokenProofProcess(IncompleteProcess):
        @property
        def execution_completed(self):
            raise RuntimeError("private completion detail")

    process = BrokenProofProcess() if proof == "getter_error" else IncompleteProcess()
    if proof == "false":
        process.execution_completed = False
    monkeypatch.setattr(manager, "build_exec_process", lambda *args, **kwargs: process)
    if communicate_failed:
        with pytest.raises(OSError, match="original communicate failure"):
            run_phase(manager, "explore_execute")
    else:
        with run_phase(manager, "explore_execute") as result:
            assert result.returncode == 0
            assert result.read_stdout() == "original output"
    session = manager.handles[0]
    assert manager.browser_sessions.execution_ids(session) is None
    manager.browser_sessions.local_finished("project-1", "run-1")
    manager.browser_sessions.terminal_confirmed("project-1", "run-1")
    assert manager.browser_sessions.claim_ready() == [session]
    assert manager.browser_sessions.cleanup_allowed(session) is False
    assert "private" not in caplog.text
    if proof == "getter_error":
        assert "RuntimeError" in caplog.text


@pytest.mark.parametrize("group_verified", [False, True])
def test_stream_error_requires_independent_group_exit_proof(monkeypatch, group_verified):
    manager = ProcessManager()

    def broken_stream(*args, **kwargs):
        yield b"partial output", b""
        raise OSError("stream disconnected")

    monkeypatch.setattr(manager.api, "exec_start", broken_stream)
    monkeypatch.setattr(manager.api, "exec_inspect", lambda identity: {
        "Running": False, "ExitCode": 0 if identity == "exec-1" or group_verified else 2,
    })
    process = ManagedProcess(manager.container, ["worker"], {})
    monkeypatch.setattr(manager, "build_exec_process", lambda *args, **kwargs: process)
    with run_phase(manager, "explore_execute") as result:
        assert result.returncode == 0
        assert result.read_stdout() == "partial output"
        assert result.transport_error is True
    assert process.execution_completed is group_verified
    assert manager.browser_sessions.execution_ids(manager.handles[0]) == (("exec-1",) if group_verified else None)


def test_binding_recovery_getter_failure_does_not_replace_process_result(monkeypatch, caplog):
    class UnavailableRecoveryManager(ProcessManager):
        recovery_reads = 0
        fail_recovery = False

        @property
        def browser_sessions(self):
            if self.fail_recovery:
                self.recovery_reads += 1
                if self.recovery_reads > 1:
                    raise RuntimeError("private recovery detail")
            return self.store

        @browser_sessions.setter
        def browser_sessions(self, store):
            self.store = store

    manager = UnavailableRecoveryManager()
    with run_phase(manager, "explore_execute"):
        pass

    def fail_prepare(*args, **kwargs):
        manager.fail_recovery = True
        raise OSError("private prepare detail")

    monkeypatch.setattr(manager, "prepare_browser_session", fail_prepare)
    with run_phase(manager, "explore_conclude") as result:
        assert result.returncode == 0
        assert result.read_stdout() == "worker output"
    assert manager.store.execution_ids(manager.handles[0]) is None
    assert manager.api.created[-1][2]["environment"] == {}
    assert "binding restore failed" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "private" not in caplog.text


def test_legacy_manager_without_prepare_interface_runs_unchanged():
    manager = ProcessManager()
    manager.prepare_browser_session = None
    with run_phase(manager, "explore_execute") as result:
        assert result.returncode == 0
    assert manager.api.created[0][2]["environment"] == {}
    assert manager.prepared == []


@pytest.mark.parametrize("start_failed", (False, True))
def test_exec_identity_error_never_overwrites_process_result_or_start_error(start_failed, monkeypatch, caplog):
    manager = ProcessManager()

    class UnknownIdentityProcess:
        @property
        def execution_identity(self):
            raise RuntimeError("private identity detail")

        def start(self):
            if start_failed:
                raise OSError("original start failure")

        def communicate(self, **kwargs):
            return ProcessResult(returncode=0, stdout="original output", stderr="")

    monkeypatch.setattr(manager, "build_exec_process", lambda *args, **kwargs: UnknownIdentityProcess())
    if start_failed:
        with pytest.raises(OSError, match="original start failure"):
            run_phase(manager, "explore_execute")
    else:
        with run_phase(manager, "explore_execute") as result:
            assert result.returncode == 0
            assert result.read_stdout() == "original output"

    assert manager.browser_sessions.execution_ids(manager.handles[0]) is None
    assert "private" not in caplog.text
    assert "RuntimeError" in caplog.text
