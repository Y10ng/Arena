from __future__ import annotations

from pathlib import Path
import stat
import threading
from types import SimpleNamespace

import pytest
from requests.exceptions import ReadTimeout

from cairn.dispatcher.runtime.process import (
    PROCESS_OUTPUT_PREVIEW_BYTES,
    ManagedProcess,
    ProcessResult,
)
from cairn.dispatcher.runtime.temporary_files import RUNTIME_TEMP_DIR_ENV


class FakeStream:
    def __init__(self, chunks: list[object]):
        self._chunks = iter(chunks)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._chunks)

    def close(self) -> None:
        self.closed = True


class BlockingStream:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.released = threading.Event()
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        self.started.set()
        self.released.wait(timeout=2)
        raise StopIteration

    def close(self) -> None:
        self.closed = True
        self.released.set()


class FakeAPI:
    def __init__(self, stream: FakeStream | BlockingStream, *, exit_code: int = 0):
        self.stream = stream
        self.exit_code = exit_code
        self.created: dict[str, object] | None = None
        self.started: dict[str, object] | None = None
        self.controls = []

    def exec_create(self, container_id, command, **kwargs):
        if kwargs.get("stdout") is False:
            self.controls.append({"container_id": container_id, "command": command})
            return {"Id": f"control-{len(self.controls)}"}
        self.created = {"container_id": container_id, "command": command, **kwargs}
        return {"Id": "exec-1"}

    def exec_start(self, exec_id, **kwargs):
        if exec_id.startswith("control-"):
            from cairn.dispatcher.runtime.exec_control import EXEC_TERMINATOR
            assert kwargs == {"detach": True}
            command = self.controls[int(exec_id.split("-")[1]) - 1]["command"]
            if command[2] == EXEC_TERMINATOR:
                self.running = False
            return None
        self.started = {"exec_id": exec_id, **kwargs}
        return self.stream

    def exec_inspect(self, _exec_id):
        return {"Running": False, "ExitCode": 0 if _exec_id.startswith("control-") else self.exit_code}


class TimeoutAPI(FakeAPI):
    def __init__(self, stream, *, exit_code=137):
        super().__init__(stream, exit_code=exit_code)
        # socket EOF 与容器进程退出是独立事实，不能由 close 伪造终态。
        self.running = True

    def exec_inspect(self, _exec_id):
        if _exec_id.startswith("control-"):
            return {"Running": False, "ExitCode": 0}
        if self.running:
            return {"Running": True, "Pid": 123, "ExitCode": None}
        return {"Running": False, "ExitCode": 137}


class FakeContainer:
    def __init__(self, api: FakeAPI):
        self.id = "container-1"
        self.name = "worker"
        self.client = SimpleNamespace(api=api)
        self.kill_commands: list[list[str]] = []

    def exec_run(self, command, **_kwargs):
        self.kill_commands.append(command)
        if isinstance(self.client.api, TimeoutAPI):
            self.client.api.running = False
        return SimpleNamespace(exit_code=0)


@pytest.mark.parametrize("terminal", [
    {"Running": True, "ExitCode": 0},
    {"Running": False, "ExitCode": None},
    {"ExitCode": 0},
    {"Running": False, "ExitCode": "0"},
    {"Running": False, "ExitCode": -1},
    {"Running": False, "ExitCode": False},
])
@pytest.mark.parametrize("timed_out", [False, True])
def test_stream_eof_without_docker_terminal_proof_never_completes(monkeypatch, terminal, timed_out):
    api = FakeAPI(FakeStream([(b"complete runner envelope", None)]))
    monkeypatch.setattr(api, "exec_inspect", lambda _: terminal)
    monkeypatch.setattr("cairn.dispatcher.runtime.process.EXEC_KILL_JOIN_TIMEOUT_SECONDS", 0)
    process = ManagedProcess(FakeContainer(api), ["fixture"], {})
    process._timed_out = timed_out
    process.start()
    with process.communicate(timeout=1) as result:
        assert result.returncode == (137 if timed_out else 1)
        assert result.transport_error is True
        assert process.execution_completed is False
        assert result.read_stdout() == "complete runner envelope"


def test_cancel_broadcast_does_not_wait_for_another_exec_kill(monkeypatch):
    from cairn.dispatcher.runtime.cancellation import TaskCancellation

    entered, release = threading.Event(), threading.Event()
    first_stream, second_stream = BlockingStream(), BlockingStream()
    first = ManagedProcess(FakeContainer(TimeoutAPI(first_stream)), ["sleep", "10"], {})
    second = ManagedProcess(FakeContainer(TimeoutAPI(second_stream)), ["sleep", "10"], {})
    original = first._signal_execution
    calls = []

    def slow_kill():
        calls.append(True)
        entered.set()
        assert release.wait(3)
        original()

    monkeypatch.setattr(first, "_signal_execution", slow_kill)
    tokens = [TaskCancellation(), TaskCancellation()]
    for token, process in zip(tokens, [first, second]):
        process.start()
        token.attach_process(process)
    assert first_stream.started.wait(1) and second_stream.started.wait(1)
    broadcast_done = threading.Event()

    def broadcast():
        for token in tokens:
            token.cancel("paused")
            token.cancel("deleted")
        broadcast_done.set()

    broadcaster = threading.Thread(target=broadcast, daemon=True)
    broadcaster.start()
    try:
        assert entered.wait(1)
        assert broadcast_done.wait(.25), "one slow kill blocked the control broadcast"
        assert first_stream.closed, "stream close was queued behind the blocked kill"
        with second.communicate(timeout=.5) as result:
            assert result.cancelled and result.cancel_reason == "paused"
            assert second.execution_completed
        assert len(calls) == 1
    finally:
        release.set()
        broadcaster.join(3)
        first.communicate(timeout=1).cleanup()
        second.communicate(timeout=1).cleanup()


def test_cancel_keeps_response_descriptor_until_reader_leaves_poll(monkeypatch):
    entered, release, signalled, response_closed = (threading.Event() for _ in range(4))

    class HeldReaderStream(FakeStream):
        def __init__(self):
            super().__init__([])
            self._response = SimpleNamespace(close=response_closed.set)

        def __next__(self):
            entered.set()
            assert release.wait(3)
            raise StopIteration

    stream = HeldReaderStream()
    process = ManagedProcess(FakeContainer(TimeoutAPI(stream)), ["fixture"], {})
    original = process._signal_execution

    def signal():
        result = original()
        signalled.set()
        return result

    monkeypatch.setattr(process, "_signal_execution", signal)
    process.start()
    try:
        assert entered.wait(1)
        process.cancel("paused")
        assert signalled.wait(1)
        assert stream.closed
        # reader 尚在 poll 时不能回收 response fd，否则可能复用为另一次 Docker 请求。
        assert not response_closed.is_set()
    finally:
        release.set()
        with process.communicate(timeout=2) as result:
            assert result.cancelled and process.execution_completed
    assert response_closed.is_set()


def test_exec_control_proof_is_durable_and_verification_is_non_destructive() -> None:
    from cairn.dispatcher.runtime.exec_control import EXEC_SUPERVISOR, EXEC_VERIFY_EXIT

    assert "f.flush()" in EXEC_SUPERVISOR
    assert "os.fsync(f.fileno())" in EXEC_SUPERVISOR
    assert "os.fsync(directory)" in EXEC_SUPERVISOR
    assert "os.unlink" not in EXEC_VERIFY_EXIT


def test_execution_prepared_callback_binds_exact_control_before_exec_start() -> None:
    api = FakeAPI(FakeStream([]))
    process = ManagedProcess(FakeContainer(api), ["fixture"], {})
    observed: list[tuple[str, str, str]] = []

    def prepared(container_id: str, exec_id: str, control_path: str) -> None:
        assert api.started is None
        observed.append((container_id, exec_id, control_path))

    start_attempts: list[str] = []

    def starting() -> None:
        assert observed
        assert api.started is None
        start_attempts.append("durable")

    process.retain_execution_control()
    process.on_execution_prepared = prepared
    process.on_execution_starting = starting
    process.start()
    with process.communicate(timeout=1):
        pass

    assert observed == [
        ("container-1", "exec-1", process.execution_control_path)
    ]
    assert start_attempts == ["durable"]


def test_execution_prepared_callback_failure_prevents_exec_start() -> None:
    api = FakeAPI(FakeStream([]))
    process = ManagedProcess(FakeContainer(api), ["must-not-run"], {})
    process.retain_execution_control()

    def reject_binding(*_args: str) -> None:
        raise RuntimeError("durable binding failed")

    process.on_execution_prepared = reject_binding

    with pytest.raises(RuntimeError, match="durable binding failed"):
        process.start()

    assert api.created is not None
    assert api.started is None
    assert process.execution_started is False


def test_execution_starting_callback_failure_prevents_exec_start() -> None:
    api = FakeAPI(FakeStream([]))
    process = ManagedProcess(FakeContainer(api), ["must-not-run"], {})
    process.retain_execution_control()

    def reject_start() -> None:
        raise RuntimeError("durable start boundary failed")

    process.on_execution_starting = reject_start
    process.start()
    with process.communicate(timeout=1) as result:
        assert result.transport_error is True
        assert "durable start boundary failed" in result.read_stderr()

    assert api.created is not None
    assert api.started is None
    assert process.execution_started is False


def test_malformed_control_create_cannot_strand_communicate_waiter() -> None:
    class MalformedControlAPI(FakeAPI):
        def exec_create(self, container_id, command, **kwargs):
            if kwargs.get("stdout") is False:
                return {}
            return super().exec_create(container_id, command, **kwargs)

    process = ManagedProcess(
        FakeContainer(MalformedControlAPI(FakeStream([]))),
        ["fixture"],
        {},
    )
    process.start()
    finished = threading.Event()
    results: list[ProcessResult] = []

    def communicate() -> None:
        try:
            results.append(process.communicate(timeout=1))
        finally:
            finished.set()

    waiter = threading.Thread(target=communicate, daemon=True)
    waiter.start()
    try:
        assert finished.wait(1), "malformed Docker control response stranded communicate()"
    finally:
        if not finished.is_set():
            process._done.set()
        waiter.join(1)
        for result in results:
            result.cleanup()

    assert len(results) == 1
    assert results[0].transport_error is True
    assert process.execution_completed is False


def test_blocked_terminator_cannot_extend_communicate_without_bound(monkeypatch) -> None:
    stream = BlockingStream()
    process = ManagedProcess(
        FakeContainer(TimeoutAPI(stream)),
        ["sleep", "10"],
        {},
    )
    terminate_entered = threading.Event()
    release_terminate = threading.Event()
    finished = threading.Event()
    results: list[ProcessResult] = []

    def blocked_terminate() -> None:
        terminate_entered.set()
        release_terminate.wait(timeout=3)

    monkeypatch.setattr(process, "_terminate", blocked_terminate)
    monkeypatch.setattr(
        "cairn.dispatcher.runtime.process.EXEC_KILL_JOIN_TIMEOUT_SECONDS",
        0.05,
    )
    process.start()
    assert stream.started.wait(1)

    def communicate() -> None:
        try:
            results.append(process.communicate(timeout=0))
        finally:
            finished.set()

    waiter = threading.Thread(target=communicate, daemon=True)
    waiter.start()
    try:
        assert terminate_entered.wait(1)
        assert finished.wait(0.5), "blocked terminator stranded communicate()"
    finally:
        release_terminate.set()
        waiter.join(1)
        for result in results:
            result.cleanup()

    assert len(results) == 1
    assert results[0].transport_error is True
    assert process.execution_completed is False


def test_retained_execution_control_proof_is_repeatably_verified() -> None:
    from cairn.dispatcher.runtime.exec_control import EXEC_CLEANUP, EXEC_VERIFY_EXIT

    api = FakeAPI(FakeStream([]))
    process = ManagedProcess(FakeContainer(api), ["fixture"], {})
    process.retain_execution_control()
    process.start()
    with process.communicate(timeout=1):
        pass

    assert process._resolve_exit_code() == 0
    helpers = [item["command"][2] for item in api.controls]
    assert helpers.count(EXEC_VERIFY_EXIT) == 2
    assert EXEC_CLEANUP not in helpers


def test_non_candidate_execution_control_is_cleaned_after_verification() -> None:
    from cairn.dispatcher.runtime.exec_control import EXEC_CLEANUP, EXEC_VERIFY_EXIT

    api = FakeAPI(FakeStream([]))
    process = ManagedProcess(FakeContainer(api), ["fixture"], {})
    process.start()
    with process.communicate(timeout=1):
        pass

    helpers = [item["command"][2] for item in api.controls]
    assert EXEC_VERIFY_EXIT in helpers
    assert EXEC_CLEANUP in helpers


def test_cancel_before_start_never_launches_exec():
    api = FakeAPI(FakeStream([]))
    process = ManagedProcess(FakeContainer(api), ["must-not-run"], {})
    process.cancel("paused")
    process.start()
    with process.communicate(timeout=1) as result:
        assert result.cancelled and result.cancel_reason == "paused"
        assert api.created is None and api.started is None
        assert process.execution_started is False
        assert process.execution_completed is True


@pytest.mark.parametrize("stage", ["exec_create", "exec_start"])
def test_cancel_during_docker_start_replays_before_or_after_attach(stage, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    api = TimeoutAPI(BlockingStream())
    original = getattr(api, stage)

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(api, stage, delayed)
    process = ManagedProcess(FakeContainer(api), ["fixture"], {})
    starter = threading.Thread(target=process.start)
    starter.start()
    try:
        assert entered.wait(1)
        process.cancel("paused")
        process.cancel("deleted")
    finally:
        release.set()
        starter.join(3)
    with process.communicate(timeout=2) as result:
        assert result.cancelled and result.cancel_reason == "paused"
        assert process.execution_completed
        if stage == "exec_create":
            assert api.started is None and not process.execution_started
        else:
            assert process.execution_started and not api.running


def test_closing_cancelled_stream_is_not_execution_exit(monkeypatch):
    stream = BlockingStream()
    api = TimeoutAPI(stream)
    process = ManagedProcess(FakeContainer(api), ["sleep", "10"], {})
    monkeypatch.setattr(process, "_signal_execution", lambda: None)
    monkeypatch.setattr("cairn.dispatcher.runtime.process.EXEC_KILL_JOIN_TIMEOUT_SECONDS", .05)
    process.start()
    assert stream.started.wait(1)
    process.cancel("paused")
    with process.communicate(timeout=1) as result:
        assert stream.closed
        assert api.running
        assert result.cancelled and result.transport_error
        assert process.execution_completed is False


def test_lost_exec_start_ack_remains_exactly_cancellable() -> None:
    class LostAckAPI(TimeoutAPI):
        def exec_start(self, exec_id, **kwargs):
            if exec_id.startswith("control-"):
                return super().exec_start(exec_id, **kwargs)
            self.started = {"exec_id": exec_id, **kwargs}
            raise ReadTimeout("exec start acknowledgement lost")

    api = LostAckAPI(FakeStream([]))
    process = ManagedProcess(FakeContainer(api), ["fixture"], {})

    process.start()
    assert process.execution_start_attempted is True
    assert process.execution_started is False
    process.cancel("paused")

    with process.communicate(timeout=2) as result:
        assert result.cancelled is True
        assert api.running is False
        assert process.execution_identity == ("container-1", "exec-1")
        from cairn.dispatcher.runtime.exec_control import EXEC_TERMINATOR
        terminators = [
            item for item in api.controls if item["command"][2] == EXEC_TERMINATOR
        ]
        assert len(terminators) == 1
        assert terminators[0]["container_id"] == "container-1"


def test_termination_controller_retries_request_transport_failure(monkeypatch) -> None:
    stream = BlockingStream()
    api = TimeoutAPI(stream)
    process = ManagedProcess(FakeContainer(api), ["sleep", "10"], {})
    original_inspect = api.exec_inspect
    terminator_failures = 0

    def flaky_inspect(exec_id):
        nonlocal terminator_failures
        if (
            exec_id == "exec-1"
            and threading.current_thread() is process._terminator
            and terminator_failures == 0
        ):
            terminator_failures += 1
            raise ReadTimeout("temporary Docker transport timeout")
        return original_inspect(exec_id)

    monkeypatch.setattr(api, "exec_inspect", flaky_inspect)
    process.start()
    assert stream.started.wait(1)
    process.cancel("paused")

    with process.communicate(timeout=2) as result:
        assert result.cancelled is True
        assert process.execution_completed is True
    assert terminator_failures == 1


def test_termination_helper_retries_request_transport_failure() -> None:
    from cairn.dispatcher.runtime.exec_control import EXEC_TERMINATOR

    class FlakyTerminatorAPI(TimeoutAPI):
        def __init__(self, stream) -> None:
            super().__init__(stream)
            self.terminator_failures = 0

        def exec_start(self, exec_id, **kwargs):
            if exec_id.startswith("control-"):
                command = self.controls[int(exec_id.split("-")[1]) - 1]["command"]
                if command[2] == EXEC_TERMINATOR and self.terminator_failures == 0:
                    self.terminator_failures += 1
                    raise ReadTimeout("temporary terminator transport timeout")
            return super().exec_start(exec_id, **kwargs)

    stream = BlockingStream()
    api = FlakyTerminatorAPI(stream)
    process = ManagedProcess(FakeContainer(api), ["sleep", "10"], {})
    process.start()
    assert stream.started.wait(1)

    process.cancel("paused")

    with process.communicate(timeout=2) as result:
        assert result.cancelled is True
        assert process.execution_completed is True
    terminators = [
        item for item in api.controls if item["command"][2] == EXEC_TERMINATOR
    ]
    assert api.terminator_failures == 1
    assert len(terminators) == 2
    assert api.running is False


def test_cleanup_transport_timeout_preserves_known_exit_and_physical_proof() -> None:
    from cairn.dispatcher.runtime.exec_control import EXEC_CLEANUP

    class CleanupTimeoutAPI(FakeAPI):
        def exec_start(self, exec_id, **kwargs):
            if exec_id.startswith("control-"):
                command = self.controls[int(exec_id.split("-")[1]) - 1]["command"]
                if command[2] == EXEC_CLEANUP:
                    raise ReadTimeout("cleanup acknowledgement lost")
            return super().exec_start(exec_id, **kwargs)

    process = ManagedProcess(
        FakeContainer(CleanupTimeoutAPI(FakeStream([]), exit_code=7)),
        ["fixture"],
        {},
    )
    process.start()

    with process.communicate(timeout=1) as result:
        assert result.returncode == 7
        assert process.execution_completed is True


def test_inline_process_result_keeps_legacy_fields_and_stream_api() -> None:
    result = ProcessResult(returncode=0, stdout="前缀 output 后缀", stderr=" \n")
    positional = ProcessResult(0, "out", "err", False, False, None, False, None)

    assert positional.stdout == "out"
    assert result.stdout == "前缀 output 后缀"
    assert result.read_stdout() == result.stdout
    assert result.read_stdout_head(6) == "前缀"
    assert result.read_stdout_tail(6) == "后缀"
    assert result.stdout_size == len(result.stdout.encode("utf-8"))
    assert result.stderr_blank is True

    result.cleanup()
    assert result.read_stdout() == result.stdout


@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
def test_managed_process_spools_complete_output_to_private_file(
    stream_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_temp = tmp_path / "runtime-temp"
    monkeypatch.setenv(RUNTIME_TEMP_DIR_ENV, str(runtime_temp))
    complete = ("首" + "x" * (PROCESS_OUTPUT_PREVIEW_BYTES * 2) + "尾").encode()
    chunk = (complete, None) if stream_name == "stdout" else (None, complete)
    api = FakeAPI(FakeStream([chunk]))
    process = ManagedProcess(FakeContainer(api), ["command"], {"TOKEN": "secret"})

    process.start()
    result = process.communicate(timeout=1)
    assert result.transport_error is False
    assert process.execution_completed is True
    path_value = result._stdout_path if stream_name == "stdout" else result._stderr_path
    assert path_value is not None
    path = Path(path_value)

    assert path.parent == runtime_temp
    assert stat.S_IMODE(runtime_temp.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert getattr(result, f"read_{stream_name}")() == complete.decode()
    assert getattr(result, f"{stream_name}_head")(len("首".encode())) == "首"
    assert getattr(result, f"{stream_name}_tail")(len("尾".encode())) == "尾"
    assert getattr(result, f"{stream_name}_size") == len(complete)
    assert getattr(result, f"{stream_name}_blank") is False
    assert len(getattr(result, stream_name).encode()) <= PROCESS_OUTPUT_PREVIEW_BYTES
    assert "output truncated" in getattr(result, stream_name)
    assert not hasattr(process, "_stdout")

    result.cleanup()
    result.cleanup()
    assert not path.exists()
    with pytest.raises(RuntimeError, match="cleaned up"):
        getattr(result, f"read_{stream_name}")()


def test_blank_check_scans_complete_file_not_only_preview() -> None:
    output = b" " * (PROCESS_OUTPUT_PREVIEW_BYTES + 1) + b"content"
    process = ManagedProcess(FakeContainer(FakeAPI(FakeStream([(output, None)]))), ["command"], {})

    process.start()
    result = process.communicate(timeout=1)
    try:
        assert result.stdout_blank is False
        assert result.stderr_blank is True
    finally:
        result.cleanup()


def test_timeout_closes_stream_joins_reader_and_preserves_result_files() -> None:
    stream = BlockingStream()
    container = FakeContainer(TimeoutAPI(stream, exit_code=137))
    process = ManagedProcess(container, ["sleep", "10"], {})

    process.start()
    assert stream.started.wait(timeout=1)
    result = process.communicate(timeout=0)
    try:
        assert result.timed_out is True
        assert result.returncode == 137
        assert stream.closed is True
        assert process._reader is not None and not process._reader.is_alive()
        from cairn.dispatcher.runtime.exec_control import EXEC_TERMINATOR
        termination = [item for item in container.client.api.controls if item["command"][2] == EXEC_TERMINATOR]
        assert len(termination) == 1
        assert all(item["container_id"] == "container-1" for item in container.client.api.controls)
        assert result.read_stdout() == ""
        assert result.read_stderr() == ""
    finally:
        result.cleanup()


@pytest.mark.parametrize("exit_code", [0, 1])
def test_read_error_is_recorded_in_stderr_file(exit_code: int) -> None:
    class BrokenStream(FakeStream):
        def __next__(self):
            raise RuntimeError("stream failed")

    process = ManagedProcess(FakeContainer(FakeAPI(BrokenStream([]), exit_code=exit_code)), ["command"], {})

    process.start()
    result = process.communicate(timeout=1)
    try:
        assert result.returncode == exit_code
        assert result.transport_error is True
        # 输出不可接受与物理退出独立；这里 inspect 已提供准确终态，允许退出清理。
        assert process.execution_completed is True
        assert result.stderr == "stream failed"
        assert result.read_stderr() == "stream failed"
    finally:
        result.cleanup()
