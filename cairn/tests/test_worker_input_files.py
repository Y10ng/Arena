from __future__ import annotations

import json
from pathlib import Path

import pytest

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks import process_runner
from cairn.dispatcher.tasks.candidate_capture_outbox import CandidateCaptureOutbox
from cairn.dispatcher.tasks.candidate_intake import CandidateIntakeSummary
from cairn.dispatcher.tasks.process_runner import run_worker_process
from cairn.dispatcher.workers.base import (
    DriverResult,
    REPORT_READONLY_INPUT_ROOT,
    WorkerCandidateSpool,
    WorkerCheckpointSpool,
    WorkerInputFile,
)


class _TrackingResult(ProcessResult):
    def __init__(self) -> None:
        super().__init__(returncode=0, stdout="ok", stderr="")
        self.cleanup_calls = 0

    def cleanup(self) -> None:
        self.cleanup_calls += 1
        super().cleanup()


class _Process:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.result = _TrackingResult()
        self.execution_identity = ("container-id", "exec-id")
        self.execution_control_path = "/tmp/cairn-exec-0123456789abcdef0123456789abcdef.json"
        self.execution_start_attempted = True
        self.execution_started = True
        self.execution_completed = True
        self.on_execution_prepared = None
        self.on_execution_starting = None
        self.control_retained = False

    def retain_execution_control(self) -> None:
        self.control_retained = True

    def start(self) -> None:
        if self.on_execution_prepared is not None:
            self.on_execution_prepared(
                *self.execution_identity,
                self.execution_control_path,
            )
        if self.on_execution_starting is not None:
            self.on_execution_starting()

    def communicate(self, timeout=None) -> ProcessResult:
        if self.fail:
            raise RuntimeError("communicate failed")
        return self.result


class _ContainerManager:
    def __init__(self, *, fail: bool = False):
        self.process = _Process(fail=fail)
        self.writes: list[tuple[str, str, str, str, int]] = []
        self.removes: list[tuple[str, str, str]] = []
        self.control_removes: list[dict[str, str]] = []
        self.archive_roots: list[str | None] = []
        self.readonly_prepares: list[tuple[str, str]] = []
        self.readonly_removes: list[tuple[str, str, tuple[str, ...]]] = []
        self.argv: list[str] | None = None

    def docker_daemon_id(self) -> str:
        return "daemon-1"

    def write_text_file(
        self,
        container_name: str,
        path: str,
        content: str,
        *,
        owner: str,
        mode: int,
        archive_root: str | None = None,
    ) -> None:
        self.writes.append((container_name, path, content, owner, mode))
        self.archive_roots.append(archive_root)

    def remove_file(self, container_name: str, path: str, *, allowed_root: str) -> None:
        self.removes.append((container_name, path, allowed_root))

    def cleanup_candidate_execution_control(self, **kwargs: str) -> None:
        self.control_removes.append(kwargs)

    def prepare_readonly_input_directory(self, container_name: str, path: str) -> None:
        self.readonly_prepares.append((container_name, path))

    def remove_readonly_input_directory(
        self,
        container_name: str,
        path: str,
        *,
        filenames: tuple[str, ...],
    ) -> None:
        self.readonly_removes.append((container_name, path, filenames))

    def build_exec_process(self, container_name, env, argv, *, timeout_seconds):
        self.argv = list(argv)
        return self.process


def _worker() -> WorkerConfig:
    return WorkerConfig(
        name="pi",
        type="pi",
        task_types=["bootstrap"],
        max_running=1,
        priority=0,
        env={},
    )


@pytest.mark.parametrize("argv", [[], [True], ["worker", 1]])
def test_driver_result_rejects_invalid_argv_before_launch(argv: list[object]) -> None:
    with pytest.raises(ValueError, match="non-empty string argv"):
        DriverResult(argv=argv)  # type: ignore[arg-type]


def test_run_worker_rejects_boolean_timeout_before_cancellation_or_side_effects() -> None:
    manager = _ContainerManager()
    cancellation = type(
        "Cancelled",
        (),
        {"is_cancelled": True, "reason": "cancelled-before-start"},
    )()

    with pytest.raises(ValueError, match="positive integer timeout"):
        run_worker_process(
            manager,
            "worker-container",
            _worker(),
            ["worker-wrapper"],
            phase="bootstrap",
            timeout_seconds=True,  # type: ignore[arg-type]
            cancellation=cancellation,
        )

    assert manager.writes == []
    assert manager.argv is None


@pytest.mark.parametrize("communicate_fails", [False, True])
def test_worker_input_files_are_private_and_always_removed(communicate_fails: bool) -> None:
    manager = _ContainerManager(fail=communicate_fails)
    invocation = DriverResult(
        argv=["worker-wrapper"],
        input_files=(
            WorkerInputFile(
                path="/tmp/cairn-prompts/pi/request.json",
                content="full prompt",
            ),
        ),
    )

    if communicate_fails:
        with pytest.raises(RuntimeError, match="communicate failed"):
            run_worker_process(
                manager,
                "worker-container",
                _worker(),
                invocation,
                phase="bootstrap",
                timeout_seconds=30,
            )
    else:
        result = run_worker_process(
            manager,
            "worker-container",
            _worker(),
            invocation,
            phase="bootstrap",
            timeout_seconds=30,
        )
        result.cleanup()

    assert manager.argv == ["worker-wrapper"]
    assert manager.writes == [
        (
            "worker-container",
            "/tmp/cairn-prompts/pi/request.json",
            "full prompt",
            "kali",
            0o600,
        )
    ]
    assert manager.removes == [
        (
            "worker-container",
            "/tmp/cairn-prompts/pi/request.json",
            "/tmp/cairn-prompts",
        ),
    ]


def _readonly_input(filename: str, content: str = "payload") -> WorkerInputFile:
    directory = f"{REPORT_READONLY_INPUT_ROOT}/{'a' * 32}"
    return WorkerInputFile(
        path=f"{directory}/{filename}",
        content=content,
        owner="root",
        mode=0o444,
        allowed_root=REPORT_READONLY_INPUT_ROOT,
        readonly_directory=directory,
    )


def test_readonly_worker_inputs_share_one_preparation_and_scoped_cleanup() -> None:
    manager = _ContainerManager()
    first = _readonly_input("report-packet.json", "packet")
    second = _readonly_input("graph.yaml", "graph")

    result = run_worker_process(
        manager,
        "worker-container",
        _worker(),
        DriverResult(
            argv=["worker-wrapper"],
            input_files=(first, second),
        ),
        phase="report_execute",
        timeout_seconds=30,
    )
    result.cleanup()

    assert manager.readonly_prepares == [
        ("worker-container", first.readonly_directory),
    ]
    assert manager.archive_roots == [first.readonly_directory, first.readonly_directory]
    assert manager.removes == []
    assert manager.readonly_removes == [
        (
            "worker-container",
            first.readonly_directory,
            ("report-packet.json", "graph.yaml"),
        ),
    ]
    assert [(write[3], write[4]) for write in manager.writes] == [
        ("root", 0o444),
        ("root", 0o444),
    ]


@pytest.mark.parametrize("failure_point", ["write", "build", "communicate"])
def test_readonly_worker_inputs_cleanup_partial_process_failures(
    failure_point: str,
) -> None:
    manager = _ContainerManager(fail=failure_point == "communicate")
    first = _readonly_input("report-packet.json", "packet")
    second = _readonly_input("graph.yaml", "graph")
    original_write = manager.write_text_file

    if failure_point == "write":
        def fail_second_write(*args, **kwargs) -> None:
            original_write(*args, **kwargs)
            if args[1].endswith("graph.yaml"):
                raise KeyboardInterrupt("write interrupted")

        manager.write_text_file = fail_second_write
    elif failure_point == "build":
        def fail_build(*_args, **_kwargs):
            raise KeyboardInterrupt("build interrupted")

        manager.build_exec_process = fail_build

    expected = KeyboardInterrupt if failure_point in {"write", "build"} else RuntimeError
    with pytest.raises(expected):
        run_worker_process(
            manager,
            "worker-container",
            _worker(),
            DriverResult(
                argv=["worker-wrapper"],
                input_files=(first, second),
            ),
            phase="report_execute",
            timeout_seconds=30,
        )

    assert manager.readonly_removes == [
        (
            "worker-container",
            first.readonly_directory,
            ("report-packet.json", "graph.yaml"),
        ),
    ]


def test_pre_cancelled_execution_does_not_materialize_readonly_inputs() -> None:
    manager = _ContainerManager()
    result = run_worker_process(
        manager,
        "worker-container",
        _worker(),
        DriverResult(
            argv=["worker-wrapper"],
            input_files=(_readonly_input("report-packet.json"),),
        ),
        phase="report_execute",
        timeout_seconds=30,
        cancellation=type(
            "Cancelled",
            (),
            {"is_cancelled": True, "reason": "cancelled-before-start"},
        )(),
    )

    assert result.cancelled is True
    assert manager.writes == []
    assert manager.readonly_prepares == []
    assert manager.readonly_removes == []


def test_pre_cancelled_candidate_still_requires_complete_delivery_identity() -> None:
    manager = _ContainerManager()
    invocation = DriverResult(
        argv=["worker-wrapper"],
        session="pi:87654321-4321-4321-8321-cba987654321",
        candidate_spool=_candidate_descriptor(),
    )

    with pytest.raises(RuntimeError, match="candidate capture identity"):
        run_worker_process(
            manager,
            "worker-container",
            _worker(),
            invocation,
            phase="explore_execute",
            timeout_seconds=30,
            cancellation=type(
                "Cancelled",
                (),
                {"is_cancelled": True, "reason": "cancelled-before-start"},
            )(),
        )

    assert manager.writes == []
    assert manager.argv is None


def test_cleanup_failure_does_not_skip_remaining_input_resources() -> None:
    manager = _ContainerManager()
    ordinary = WorkerInputFile(
        path="/tmp/cairn-prompts/request.json",
        content="request",
    )
    readonly = _readonly_input("report-packet.json")

    def fail_ordinary_cleanup(*_args, **_kwargs) -> None:
        raise KeyboardInterrupt("cleanup interrupted")

    manager.remove_file = fail_ordinary_cleanup
    process_runner._remove_worker_input_files(
        manager,
        "worker-container",
        [readonly, ordinary],
    )

    assert manager.readonly_removes == [
        (
            "worker-container",
            readonly.readonly_directory,
            ("report-packet.json",),
        ),
    ]


def test_report_readonly_reference_builder_defers_materialization() -> None:
    packet_reference, graph_reference, files = (
        process_runner.build_report_readonly_input_references(
            '{"version":2}',
            "facts: []",
        )
    )

    packet, graph = files
    assert packet.readonly_directory == graph.readonly_directory
    assert packet.path == f"{packet.readonly_directory}/report-packet.json"
    assert graph.path == f"{graph.readonly_directory}/graph.yaml"
    assert packet.content == '{"version":2}'
    assert graph.content == "facts: []"
    assert packet.path in packet_reference
    assert "next_offset" in packet_reference
    assert graph.path in graph_reference
    assert "register_artifact" in packet_reference
    assert "register_artifact" in graph_reference


def test_worker_input_descriptor_rejects_uncontrolled_ordinary_values() -> None:
    valid: dict[str, object] = {
        "path": "/tmp/cairn-prompts/nested/request.json",
        "content": "request",
        "owner": "kali",
        "mode": 0o600,
        "allowed_root": "/tmp/cairn-prompts",
    }
    invalid = [
        {**valid, "path": "relative/request.json"},
        {**valid, "path": "/tmp/cairn-prompts/../request.json"},
        {**valid, "path": "/tmp/cairn-prompts/./request.json"},
        {**valid, "path": "/tmp/cairn-prompts"},
        {**valid, "path": "/tmp/other/request.json"},
        {**valid, "path": True},
        {**valid, "content": 1},
        {**valid, "owner": 1},
        {**valid, "mode": True},
        {**valid, "mode": "0600"},
        {**valid, "allowed_root": 1},
        {**valid, "allowed_root": "tmp/cairn-prompts"},
        {**valid, "allowed_root": "/tmp/../tmp/cairn-prompts"},
        {**valid, "readonly_directory": 1},
    ]

    assert WorkerInputFile(**valid).path == valid["path"]  # type: ignore[arg-type]
    for values in invalid:
        with pytest.raises(ValueError, match="worker input descriptor"):
            WorkerInputFile(**values)  # type: ignore[arg-type]


def test_readonly_worker_input_descriptor_rejects_uncontrolled_paths() -> None:
    directory = f"{REPORT_READONLY_INPUT_ROOT}/{'a' * 32}"
    valid = {
        "path": f"{directory}/report-packet.json",
        "content": "packet",
        "owner": "root",
        "mode": 0o444,
        "allowed_root": REPORT_READONLY_INPUT_ROOT,
        "readonly_directory": directory,
    }
    invalid = [
        {**valid, "path": f"{directory}/../report-packet.json"},
        {**valid, "path": f"{directory}/./report-packet.json"},
        {**valid, "path": f"{directory}/nested/report-packet.json"},
        {**valid, "readonly_directory": REPORT_READONLY_INPUT_ROOT},
        {
            **valid,
            "readonly_directory": f"{REPORT_READONLY_INPUT_ROOT}/./{'a' * 32}",
        },
        {**valid, "readonly_directory": "/tmp/" + "a" * 32},
        {**valid, "readonly_directory": f"{REPORT_READONLY_INPUT_ROOT}/ABC"},
        {**valid, "owner": "kali"},
        {**valid, "mode": 0o600},
        {**valid, "allowed_root": "/home/kali/workspace"},
    ]

    assert WorkerInputFile(**valid).readonly_directory == directory
    for values in invalid:
        with pytest.raises(ValueError, match="invalid readonly worker input descriptor"):
            WorkerInputFile(**values)


def test_worker_result_is_cleaned_when_post_process_audit_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _ContainerManager()

    def fail_command_audit(*_args, **_kwargs) -> None:
        raise RuntimeError("audit failed")

    monkeypatch.setattr(process_runner, "record_command_finished", fail_command_audit)

    with pytest.raises(RuntimeError, match="audit failed"):
        run_worker_process(
            manager,
            "worker-container",
            _worker(),
            ["worker"],
            phase="bootstrap",
            timeout_seconds=30,
        )

    assert manager.process.result.cleanup_calls == 1


def test_candidate_spool_descriptor_binds_path_capture_and_hard_limits() -> None:
    capture_id = "12345678-1234-4234-8234-123456789abc"
    descriptor = WorkerCandidateSpool(
        path=f"/tmp/cairn-prompts/pi-candidates-{capture_id}.bin",
        capture_id=capture_id,
        max_candidates=1,
        max_candidate_bytes=1024,
        max_manifest_bytes=512,
        max_spool_bytes=4096,
    )
    assert descriptor.protocol == "cairn-candidate-spool-v1"

    invalid_values = [
        {"path": "relative.bin", "capture_id": capture_id},
        {"path": f"/tmp/pi-candidates-{capture_id}.bin", "capture_id": capture_id},
        {"path": f"/tmp/cairn-prompts/pi-candidates-{capture_id}.json", "capture_id": capture_id},
        {"path": f"/tmp/cairn-prompts/pi-candidates-{capture_id}.bin", "capture_id": capture_id.upper()},
        {"path": f"/tmp/cairn-prompts/pi-candidates-{capture_id}.bin", "capture_id": "not-a-uuid"},
        {"path": f"/tmp/cairn-prompts/pi-candidates-{capture_id}.bin", "capture_id": capture_id, "protocol": "v2"},
        {"path": f"/tmp/cairn-prompts/pi-candidates-{capture_id}.bin", "capture_id": capture_id, "max_candidates": 17},
        {"path": f"/tmp/cairn-prompts/pi-candidates-{capture_id}.bin", "capture_id": capture_id, "max_candidate_bytes": 2 * 1024 * 1024 + 1},
        {"path": f"/tmp/cairn-prompts/pi-candidates-{capture_id}.bin", "capture_id": capture_id, "max_manifest_bytes": 4097},
        {"path": f"/tmp/cairn-prompts/pi-candidates-{capture_id}.bin", "capture_id": capture_id, "max_spool_bytes": 34 * 1024 * 1024 + 1},
    ]
    for values in invalid_values:
        with pytest.raises(ValueError):
            WorkerCandidateSpool(**values)


def test_checkpoint_spool_descriptor_rejects_paths_protocols_and_invalid_limits() -> None:
    descriptor = WorkerCheckpointSpool(
        path="/tmp/cairn-prompts/pi-checkpoints-test.jsonl",
        max_events=1,
        max_event_bytes=1024,
        max_spool_bytes=4096,
    )
    assert descriptor.protocol == "cairn-execution-event-v1"

    invalid_values = [
        {"path": "relative.jsonl"},
        {"path": "/tmp/pi-checkpoints-test.jsonl"},
        {"path": "/tmp/cairn-prompts/nested/pi-checkpoints-test.jsonl"},
        {"path": "/tmp/cairn-prompts/pi-checkpoints-test.txt"},
        {"path": "/tmp/cairn-prompts/pi-checkpoints-test.jsonl", "protocol": "v2"},
        {"path": "/tmp/cairn-prompts/pi-checkpoints-test.jsonl", "max_events": 0},
        {"path": "/tmp/cairn-prompts/pi-checkpoints-test.jsonl", "max_events": True},
        {"path": "/tmp/cairn-prompts/pi-checkpoints-test.jsonl", "max_events": 1.5},
        {"path": "/tmp/cairn-prompts/pi-checkpoints-test.jsonl", "max_event_bytes": 16385},
        {"path": "/tmp/cairn-prompts/pi-checkpoints-test.jsonl", "max_spool_bytes": 524289},
    ]
    for values in invalid_values:
        with pytest.raises(ValueError):
            WorkerCheckpointSpool(**values)


@pytest.mark.parametrize(
    "result",
    [
        ProcessResult(0, "", ""),
        ProcessResult(7, "", ""),
        ProcessResult(1, "", "", timed_out=True),
        ProcessResult(1, "", "", cancelled=True),
        ProcessResult(1, "", "", transport_error=True),
        ProcessResult(137, "", "", oom_killed=True),
    ],
    ids=["success", "nonzero", "timeout", "cancelled", "transport", "oom"],
)
def test_checkpoint_intake_precedes_cleanup_and_never_changes_process_outcome(
    monkeypatch: pytest.MonkeyPatch,
    result: ProcessResult,
) -> None:
    manager = _ContainerManager()
    manager.process.result = result
    events: list[str] = []
    original_write = manager.write_text_file
    original_remove = manager.remove_file

    def write(*args, **kwargs) -> None:
        events.append(f"write:{args[1]}")
        original_write(*args, **kwargs)

    def remove(*args, **kwargs) -> None:
        events.append(f"remove:{args[1]}")
        original_remove(*args, **kwargs)

    def intake(_client, _manager, _container, descriptor, **identity):
        events.append("intake")
        assert descriptor.path == "/tmp/cairn-prompts/pi-checkpoints-test.jsonl"
        assert identity == {
            "project_id": "project-1",
            "task_run_id": "run-1",
            "phase": "explore_execute",
        }

    manager.write_text_file = write
    manager.remove_file = remove
    monkeypatch.setattr(process_runner, "intake_worker_checkpoints", intake)
    monkeypatch.setattr(process_runner, "record_command_started", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "record_command_finished", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "worker_process_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "record_pi_tool_events", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "record_payload_operations", lambda *_args, **_kwargs: None)
    descriptor = WorkerCheckpointSpool(
        path="/tmp/cairn-prompts/pi-checkpoints-test.jsonl"
    )
    invocation = DriverResult(
        argv=["worker-wrapper"],
        input_files=(
            WorkerInputFile(path="/tmp/cairn-prompts/request.json", content="request"),
            WorkerInputFile(path=descriptor.path, content=""),
        ),
        checkpoint_spool=descriptor,
    )

    returned = run_worker_process(
        manager,
        "worker-container",
        _worker(),
        invocation,
        phase="explore_execute",
        timeout_seconds=30,
        client=object(),
        project_id="project-1",
        task_run_id="run-1",
    )

    assert returned is result
    assert events == [
        "write:/tmp/cairn-prompts/request.json",
        "write:/tmp/cairn-prompts/pi-checkpoints-test.jsonl",
        "intake",
        "remove:/tmp/cairn-prompts/pi-checkpoints-test.jsonl",
        "remove:/tmp/cairn-prompts/request.json",
    ]


def test_checkpoint_intake_exception_isolated_but_communicate_exception_skips_intake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def intake(*_args, **_kwargs):
        calls.append("intake")
        raise RuntimeError("intake failed")

    monkeypatch.setattr(process_runner, "intake_worker_checkpoints", intake)
    monkeypatch.setattr(process_runner, "record_command_started", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "record_command_finished", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "worker_process_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "record_pi_tool_events", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "record_payload_operations", lambda *_args, **_kwargs: None)
    descriptor = WorkerCheckpointSpool(
        path="/tmp/cairn-prompts/pi-checkpoints-test.jsonl"
    )
    invocation = DriverResult(
        argv=["worker-wrapper"],
        input_files=(WorkerInputFile(path=descriptor.path, content=""),),
        checkpoint_spool=descriptor,
    )

    manager = _ContainerManager()
    returned = run_worker_process(
        manager,
        "worker-container",
        _worker(),
        invocation,
        phase="explore_execute",
        timeout_seconds=30,
        client=object(),
        project_id="project-1",
        task_run_id="run-1",
    )
    assert returned is manager.process.result
    assert calls == ["intake"]

    failing_manager = _ContainerManager(fail=True)
    with pytest.raises(RuntimeError, match="communicate failed"):
        run_worker_process(
            failing_manager,
            "worker-container",
            _worker(),
            invocation,
            phase="explore_execute",
            timeout_seconds=30,
            client=object(),
            project_id="project-1",
            task_run_id="run-1",
        )
    assert calls == ["intake"]
    assert failing_manager.removes == [
        (
            "worker-container",
            "/tmp/cairn-prompts/pi-checkpoints-test.jsonl",
            "/tmp/cairn-prompts",
        )
    ]


def _candidate_descriptor() -> WorkerCandidateSpool:
    capture_id = "12345678-1234-4234-8234-123456789abc"
    return WorkerCandidateSpool(
        path=f"/tmp/cairn-prompts/pi-candidates-{capture_id}.bin",
        capture_id=capture_id,
        binding_sha256="a" * 64,
        result_schema_sha256="b" * 64,
    )


def test_unresolved_candidate_execution_retains_all_inputs_and_skips_checkpoint_intake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _ContainerManager()
    manager.process.execution_completed = False
    manager.process.result = ProcessResult(
        1,
        "",
        "",
        transport_error=True,
    )
    descriptor = _candidate_descriptor()
    checkpoint = WorkerCheckpointSpool(
        path="/tmp/cairn-prompts/pi-checkpoints-test.jsonl"
    )
    readonly = _readonly_input("report-packet.json")
    outbox = CandidateCaptureOutbox(
        tmp_path / "candidate-outbox",
        owner_id="dispatcher-1",
    )
    invocation = DriverResult(
        argv=["worker-wrapper"],
        session="pi:87654321-4321-4321-8321-cba987654321",
        input_files=(
            WorkerInputFile(
                path="/tmp/cairn-prompts/request.json",
                content="request",
            ),
            WorkerInputFile(path=descriptor.path, content=""),
            WorkerInputFile(path=checkpoint.path, content=""),
            readonly,
        ),
        candidate_spool=descriptor,
        checkpoint_spool=checkpoint,
    )
    checkpoint_calls: list[bool] = []
    monkeypatch.setattr(
        process_runner,
        "intake_worker_checkpoints",
        lambda *_args, **_kwargs: checkpoint_calls.append(True),
    )
    monkeypatch.setattr(
        process_runner,
        "record_command_started",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        process_runner,
        "record_command_finished",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        process_runner,
        "worker_process_failure",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        process_runner,
        "record_pi_tool_events",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        process_runner,
        "record_payload_operations",
        lambda *_args, **_kwargs: None,
    )

    result = run_worker_process(
        manager,
        "worker-container",
        _worker(),
        invocation,
        phase="explore_execute",
        timeout_seconds=30,
        client=object(),
        project_id="project-1",
        task_run_id="run-1",
        candidate_outbox=outbox,
    )

    assert result.candidate_capture_integrity_error == "candidate_execution_unresolved"
    assert checkpoint_calls == []
    assert manager.removes == []
    assert manager.readonly_removes == []
    assert len(outbox.fences()) == 1


def test_pre_start_failure_retains_candidate_spool_when_exact_fence_release_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _ContainerManager()
    descriptor = _candidate_descriptor()
    outbox = CandidateCaptureOutbox(
        tmp_path / "candidate-outbox",
        owner_id="dispatcher-1",
    )
    invocation = DriverResult(
        argv=["worker-wrapper"],
        session="pi:87654321-4321-4321-8321-cba987654321",
        input_files=(
            WorkerInputFile(
                path="/tmp/cairn-prompts/request.json",
                content="request",
            ),
            WorkerInputFile(path=descriptor.path, content=""),
        ),
        candidate_spool=descriptor,
    )

    def fail_build(*_args, **_kwargs):
        raise RuntimeError("build failed")

    manager.build_exec_process = fail_build
    monkeypatch.setattr(outbox, "remove_fence", lambda _fence: False)
    monkeypatch.setattr(
        process_runner,
        "record_command_started",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        process_runner,
        "record_command_finished",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(RuntimeError, match="build failed"):
        run_worker_process(
            manager,
            "worker-container",
            _worker(),
            invocation,
            phase="explore_execute",
            timeout_seconds=30,
            client=object(),
            project_id="project-1",
            task_run_id="run-1",
            candidate_outbox=outbox,
        )

    assert len(outbox.fences()) == 1
    assert manager.removes == [
        (
            "worker-container",
            "/tmp/cairn-prompts/request.json",
            "/tmp/cairn-prompts",
        )
    ]


def test_candidate_fence_precedes_start_and_intake_precedes_checkpoint_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _ContainerManager()
    events: list[str] = []
    descriptor = _candidate_descriptor()
    checkpoint = WorkerCheckpointSpool(
        path="/tmp/cairn-prompts/pi-checkpoints-test.jsonl"
    )
    outbox = CandidateCaptureOutbox(
        tmp_path / "candidate-outbox",
        owner_id="dispatcher-1",
    )
    original_start = manager.process.start
    original_remove = manager.remove_file

    def start() -> None:
        events.append("start")
        assert len(outbox.fences()) == 1
        original_start()

    def candidate_intake(*_args, **_kwargs) -> CandidateIntakeSummary:
        events.append("candidate")
        fences = outbox.fences()
        assert len(fences) == 1
        assert fences[0].terminal_confirmed is True
        assert fences[0].control_path == manager.process.execution_control_path
        return CandidateIntakeSummary(host_durable=True, pending=1)

    def checkpoint_intake(*_args, **_kwargs) -> None:
        events.append("checkpoint")

    def remove(*args, **kwargs) -> None:
        events.append(f"remove:{args[1]}")
        original_remove(*args, **kwargs)

    manager.process.start = start
    manager.remove_file = remove
    monkeypatch.setattr(process_runner, "intake_worker_candidates", candidate_intake)
    monkeypatch.setattr(process_runner, "intake_worker_checkpoints", checkpoint_intake)
    monkeypatch.setattr(process_runner, "record_command_started", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "record_command_finished", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "worker_process_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "record_pi_tool_events", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "record_payload_operations", lambda *_args, **_kwargs: None)
    invocation = DriverResult(
        argv=["worker-wrapper"],
        session="pi:87654321-4321-4321-8321-cba987654321",
        input_files=(
            WorkerInputFile(path="/tmp/cairn-prompts/request.json", content="request"),
            WorkerInputFile(path=descriptor.path, content=""),
            WorkerInputFile(path=checkpoint.path, content=""),
        ),
        candidate_spool=descriptor,
        checkpoint_spool=checkpoint,
    )

    result = run_worker_process(
        manager,
        "worker-container",
        _worker(),
        invocation,
        phase="explore_execute",
        timeout_seconds=30,
        client=object(),
        project_id="project-1",
        task_run_id="run-1",
        candidate_outbox=outbox,
    )

    assert result.candidate_host_durable is True
    assert result.candidate_capture_pending is True
    assert result.candidate_capture_integrity_error is None
    assert outbox.fences() == []
    assert events == [
        "start",
        "candidate",
        "checkpoint",
        "remove:/tmp/cairn-prompts/pi-checkpoints-test.jsonl",
        "remove:/tmp/cairn-prompts/request.json",
    ]
    result.cleanup()
    assert events[-1] == f"remove:{descriptor.path}"
    assert manager.control_removes == [
        {
            "container_id": "container-id",
            "container_name": "worker-container",
            "project_id": "project-1",
            "control_path": manager.process.execution_control_path,
        }
    ]


def test_lost_exec_start_ack_still_intakes_candidate_spool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _ContainerManager()
    manager.process.execution_start_attempted = True
    manager.process.execution_started = False
    manager.process.execution_completed = True
    manager.process.result = ProcessResult(1, "", "", transport_error=True)
    descriptor = _candidate_descriptor()
    outbox = CandidateCaptureOutbox(
        tmp_path / "candidate-outbox",
        owner_id="dispatcher-1",
    )
    calls: list[str] = []
    monkeypatch.setattr(
        process_runner,
        "intake_worker_candidates",
        lambda *_args, **_kwargs: (
            calls.append("candidate")
            or CandidateIntakeSummary(host_durable=True)
        ),
    )
    monkeypatch.setattr(process_runner, "record_command_started", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "record_command_finished", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "worker_process_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "record_pi_tool_events", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "record_payload_operations", lambda *_args, **_kwargs: None)

    result = run_worker_process(
        manager,
        "worker-container",
        _worker(),
        DriverResult(
            argv=["worker-wrapper"],
            session="pi:87654321-4321-4321-8321-cba987654321",
            input_files=(WorkerInputFile(path=descriptor.path, content=""),),
            candidate_spool=descriptor,
        ),
        phase="explore_execute",
        timeout_seconds=30,
        client=object(),
        project_id="project-1",
        task_run_id="run-1",
        candidate_outbox=outbox,
    )

    assert calls == ["candidate"]
    assert result.candidate_host_durable is True
    assert outbox.fences() == []
    assert manager.removes == []
    result.cleanup()
    assert manager.removes == [
        ("container-id", descriptor.path, "/tmp/cairn-prompts")
    ]


def test_candidate_receipt_conflict_retains_fence_spool_and_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _ContainerManager()
    descriptor = _candidate_descriptor()
    outbox = CandidateCaptureOutbox(
        tmp_path / "candidate-outbox",
        owner_id="dispatcher-1",
    )
    invocation = DriverResult(
        argv=["worker-wrapper"],
        session="pi:87654321-4321-4321-8321-cba987654321",
        input_files=(WorkerInputFile(path=descriptor.path, content=""),),
        candidate_spool=descriptor,
    )
    monkeypatch.setattr(
        process_runner,
        "intake_worker_candidates",
        lambda *_args, **_kwargs: CandidateIntakeSummary(
            host_durable=True,
            pending=1,
            integrity_error="candidate_receipt_conflict",
        ),
    )
    monkeypatch.setattr(process_runner, "record_command_started", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "record_command_finished", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "worker_process_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "record_pi_tool_events", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "record_payload_operations", lambda *_args, **_kwargs: None)

    result = run_worker_process(
        manager,
        "worker-container",
        _worker(),
        invocation,
        phase="explore_execute",
        timeout_seconds=30,
        client=object(),
        project_id="project-1",
        task_run_id="run-1",
        candidate_outbox=outbox,
    )

    assert result.candidate_capture_integrity_error == "candidate_receipt_conflict"
    fences = outbox.fences()
    assert len(fences) == 1
    assert fences[0].terminal_confirmed is True
    result.cleanup()
    assert manager.removes == []
    assert manager.control_removes == []


def test_candidate_integrity_failure_blocks_only_otherwise_successful_process() -> None:
    common = {
        "phase": "explore_execute",
        "project_id": "project-1",
        "task_run_id": "run-1",
        "session": "pi:87654321-4321-4321-8321-cba987654321",
    }
    physical = ProcessResult(
        7,
        "not-json",
        "",
        candidate_capture_integrity_error="candidate_spool_invalid",
    )
    physical_failure = process_runner.worker_process_failure(physical, **common)
    assert physical_failure is not None
    assert physical_failure.code == "process_exit_nonzero"

    captured = ProcessResult(
        0,
        json.dumps({
            "protocol": "cairn-agent-result-v1",
            "engine": "pi",
            "project_id": common["project_id"],
            "task_run_id": common["task_run_id"],
            "phase": common["phase"],
            "session": common["session"],
            "status": "succeeded",
            "result": {
                "protocol": "cairn-assessment-v2",
                "accepted": True,
                "kind": "explore_result",
                "data": {"fact": {"description": "bounded result"}},
            },
            "submission_id": "a" * 64,
            "usage": {},
            "error": None,
        }),
        "",
        candidate_capture_integrity_error="candidate_spool_invalid",
    )
    capture_failure = process_runner.worker_process_failure(captured, **common)
    assert capture_failure is not None
    assert capture_failure.failure_class == "candidate_integrity"
    assert capture_failure.code == "candidate_spool_invalid"
