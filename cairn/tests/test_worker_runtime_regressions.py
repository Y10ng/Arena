from __future__ import annotations

import io
import json
import tarfile
from types import SimpleNamespace

import pytest

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.runtime.container_archives import text_file_archive
from cairn.dispatcher.scheduler.common import WorkerSelection
from cairn.dispatcher.scheduler.task_run_finish import TaskRunFinishMixin
from cairn.dispatcher.scheduler.worker_runtime import WorkerRuntimeMixin
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.runtime.readiness import run_readiness_check
from cairn.dispatcher.tasks import hook_records, payload_records, process_runner
from cairn.dispatcher.tasks.common_base import did_oom, did_timeout
from cairn.dispatcher.workers.base import (
    DriverResult,
    REPORT_READONLY_INPUT_ROOT,
    WorkerInputFile,
)


class FakeExecContainer:
    def __init__(self) -> None:
        self.id = "container-1"
        self.name = "worker"
        self.exec_calls: list[tuple[object, dict[str, object]]] = []
        self.archives: list[tuple[str, bytes]] = []

    def exec_run(self, command, **kwargs):
        self.exec_calls.append((command, kwargs))
        if command == ["id", "-u", "kali"]:
            return SimpleNamespace(exit_code=0, output=b"1000\n")
        if command == ["id", "-g", "kali"]:
            return SimpleNamespace(exit_code=0, output=b"1001\n")
        if command in (["id", "-u", "root"], ["id", "-g", "root"]):
            return SimpleNamespace(exit_code=0, output=b"0\n")
        return SimpleNamespace(exit_code=0, output=b"")

    def put_archive(self, path: str, archive: bytes) -> bool:
        self.archives.append((path, archive))
        return True


def archive_members(data: bytes) -> list[tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        return archive.getmembers()


def test_positive_oom_evidence_takes_precedence_over_exit_137_timeout() -> None:
    oom = ProcessResult(
        returncode=137,
        stdout="",
        stderr="",
        oom_killed=True,
        oom_evidence_source="cgroup_v2_memory_events",
    )
    ambiguous = ProcessResult(returncode=137, stdout="", stderr="")
    cancelled = ProcessResult(
        returncode=137,
        stdout="",
        stderr="",
        cancelled=True,
        oom_killed=True,
    )

    assert did_oom(oom) is True
    assert did_timeout(oom) is False
    assert did_oom(ambiguous) is False
    assert did_timeout(ambiguous) is True
    assert did_oom(cancelled) is False
    assert did_timeout(cancelled) is False


def test_container_manager_detects_only_new_oom_kill_counter() -> None:
    class FakeContainer:
        attrs = {"State": {"OOMKilled": False}}

        def reload(self) -> None:
            return None

    manager = object.__new__(ContainerManager)
    counts = iter([4, 5])
    manager.oom_kill_count = lambda _name: next(counts)
    manager._require_container = lambda _name: FakeContainer()

    before = manager.oom_kill_count("worker")
    assert manager.detect_oom_kill("worker", before_count=before) == (
        True,
        "cgroup_v2_memory_events",
    )


def test_container_oom_counter_is_not_attributed_to_successful_exec() -> None:
    class FakeManager:
        def detect_oom_kill(self, _name: str, *, before_count: int | None):
            return True, "cgroup_v2_memory_events"

    result = ProcessResult(returncode=0, stdout="ok", stderr="")

    process_runner._apply_oom_evidence(
        FakeManager(),
        "worker",
        result,
        before_count=4,
    )

    assert result.oom_killed is False
    assert result.oom_evidence_source is None


def test_readiness_collects_positive_oom_evidence() -> None:
    process_result = ProcessResult(returncode=137, stdout="", stderr="")

    class OomProcess:
        def start(self) -> None:
            return None

        def communicate(self, timeout=None) -> ProcessResult:
            return process_result

        def cleanup_output(self) -> None:
            process_result.cleanup()

    class OomManager:
        def oom_kill_count(self, _name: str) -> int:
            return 4

        def detect_oom_kill(self, _name: str, *, before_count: int | None):
            assert before_count == 4
            return True, "cgroup_v2_memory_events"

        def build_exec_process(self, *args, **kwargs):
            return OomProcess()

    outcome = run_readiness_check(
        OomManager(),
        "worker",
        timeout_seconds=30,
    )

    assert outcome.ok is False
    assert outcome.error_type == "readiness_oom"
    assert process_result._cleaned is True


def test_text_file_archive_applies_owner_to_all_members() -> None:
    archive_path, data = text_file_archive(
        "/home/kali/workspace/evidence/packet.json",
        "{}",
        uid=1000,
        gid=1001,
    )

    members = archive_members(data)
    assert archive_path == "/home"
    assert [member.name for member in members] == [
        "kali",
        "kali/workspace",
        "kali/workspace/evidence",
        "kali/workspace/evidence/packet.json",
    ]
    assert {(member.uid, member.gid) for member in members} == {(1000, 1001)}
    assert [member.mode for member in members[:-1]] == [0o755, 0o755, 0o755]
    assert members[-1].mode == 0o644


def test_text_file_archive_anchor_contains_only_root_owned_leaf() -> None:
    directory = f"{REPORT_READONLY_INPUT_ROOT}/{'a' * 32}"
    archive_path, data = text_file_archive(
        f"{directory}/report-packet.json",
        "{}",
        uid=0,
        gid=0,
        mode=0o444,
        archive_root=directory,
    )

    members = archive_members(data)
    assert archive_path == directory
    assert [member.name for member in members] == ["report-packet.json"]
    assert (members[0].uid, members[0].gid, members[0].mode) == (0, 0, 0o444)

    with pytest.raises(ValueError, match="direct parent"):
        text_file_archive(
            f"{directory}/report-packet.json",
            "{}",
            archive_root=REPORT_READONLY_INPUT_ROOT,
        )


def test_text_file_archive_keeps_root_default_and_rejects_invalid_owner() -> None:
    _archive_path, data = text_file_archive("/tmp/example.txt", "payload")
    assert {(member.uid, member.gid) for member in archive_members(data)} == {(0, 0)}

    with pytest.raises(ValueError):
        text_file_archive("/tmp/example.txt", "payload", uid=-1)
    with pytest.raises(ValueError):
        text_file_archive("relative.txt", "payload")
    with pytest.raises(ValueError):
        text_file_archive("/tmp/../example.txt", "payload")
    with pytest.raises(ValueError):
        text_file_archive("/tmp/./example.txt", "payload")


def test_write_text_file_resolves_owner() -> None:
    container = FakeExecContainer()
    manager = object.__new__(ContainerManager)
    manager._container_user_ids_cache = {}
    manager._require_container = lambda _name: container

    manager.write_text_file("worker", "/home/kali/workspace/file.txt", "payload", owner="kali")

    assert container.exec_calls[:2] == [
        (["id", "-u", "kali"], {"demux": False}),
        (["id", "-g", "kali"], {"demux": False}),
    ]
    assert {(member.uid, member.gid) for member in archive_members(container.archives[0][1])} == {(1000, 1001)}


def test_write_text_file_anchors_root_owned_readonly_leaf() -> None:
    container = FakeExecContainer()
    manager = object.__new__(ContainerManager)
    manager._container_user_ids_cache = {}
    manager._require_container = lambda _name: container
    directory = f"{REPORT_READONLY_INPUT_ROOT}/{'a' * 32}"

    manager.write_text_file(
        "worker",
        f"{directory}/report-packet.json",
        "{}",
        owner="root",
        mode=0o444,
        archive_root=directory,
    )

    archive_path, data = container.archives[0]
    members = archive_members(data)
    assert archive_path == directory
    assert [member.name for member in members] == ["report-packet.json"]
    assert (members[0].uid, members[0].gid, members[0].mode) == (0, 0, 0o444)


def test_readonly_input_directory_uses_root_scoped_lifecycle() -> None:
    container = FakeExecContainer()
    manager = object.__new__(ContainerManager)
    manager._container_user_ids_cache = {}
    manager._require_container = lambda _name: container
    directory = f"{REPORT_READONLY_INPUT_ROOT}/{'a' * 32}"

    manager.prepare_readonly_input_directory("worker", directory)
    manager.remove_readonly_input_directory(
        "worker",
        directory,
        filenames=("report-packet.json", "graph.yaml"),
    )

    prepare_command, prepare_options = container.exec_calls[0]
    cleanup_command, cleanup_options = container.exec_calls[1]
    assert prepare_command[:2] == ["python3", "-c"]
    assert prepare_command[3:] == [directory]
    assert "/opt/cairn/readonly-inputs" in prepare_command[2]
    assert "info.st_uid != 0" in prepare_command[2]
    assert "stat.S_IMODE(info.st_mode) & 0o022" in prepare_command[2]
    assert prepare_options == {"user": "0:0", "demux": False}
    assert cleanup_command[3:] == [directory, "report-packet.json", "graph.yaml"]
    assert "/opt/cairn/readonly-inputs" in cleanup_command[2]
    assert cleanup_options == {"user": "0:0", "demux": False}


@pytest.mark.parametrize("exit_code", [None, False, "0", 0.0])
@pytest.mark.parametrize(
    "operation",
    ["resolve_owner", "prepare_readonly", "remove_readonly", "remove_file"],
)
def test_container_helpers_reject_non_integer_zero_exit_codes(
    exit_code: object,
    operation: str,
) -> None:
    container = FakeExecContainer()
    container.exec_run = lambda *_args, **_kwargs: SimpleNamespace(
        exit_code=exit_code,
        output=b"1000\n",
    )
    manager = object.__new__(ContainerManager)
    manager._container_user_ids_cache = {}
    manager._require_container = lambda _name: container
    directory = f"{REPORT_READONLY_INPUT_ROOT}/{'a' * 32}"

    with pytest.raises(RuntimeError):
        if operation == "resolve_owner":
            manager._container_user_ids(container, "kali")
        elif operation == "prepare_readonly":
            manager.prepare_readonly_input_directory("worker", directory)
        elif operation == "remove_readonly":
            manager.remove_readonly_input_directory(
                "worker",
                directory,
                filenames=("report-packet.json",),
            )
        else:
            manager.remove_file(
                "worker",
                "/tmp/cairn-prompts/request.json",
                allowed_root="/tmp/cairn-prompts",
            )


@pytest.mark.parametrize("exit_code", [False, "0", 0.0])
def test_oom_counter_rejects_non_integer_zero_exit_code(exit_code: object) -> None:
    container = FakeExecContainer()
    container.exec_run = lambda *_args, **_kwargs: SimpleNamespace(
        exit_code=exit_code,
        output=b"oom_kill 7\n",
    )
    manager = object.__new__(ContainerManager)
    manager._require_container = lambda _name: container

    assert manager.oom_kill_count("worker") is None


@pytest.mark.parametrize(
    ("directory", "filenames"),
    [
        (REPORT_READONLY_INPUT_ROOT, ("report-packet.json",)),
        (f"{REPORT_READONLY_INPUT_ROOT}/../{'a' * 32}", ("report-packet.json",)),
        (f"{REPORT_READONLY_INPUT_ROOT}/{'a' * 31}", ("report-packet.json",)),
        (f"{REPORT_READONLY_INPUT_ROOT}/{'a' * 32}/nested", ("report-packet.json",)),
        (f"{REPORT_READONLY_INPUT_ROOT}/{'a' * 32}", ("../report-packet.json",)),
        (f"{REPORT_READONLY_INPUT_ROOT}/{'a' * 32}", ()),
    ],
)
def test_readonly_input_directory_rejects_uncontrolled_cleanup(
    directory: str,
    filenames: tuple[str, ...],
) -> None:
    manager = object.__new__(ContainerManager)
    manager._require_container = lambda _name: pytest.fail("container must not be accessed")

    with pytest.raises(ValueError):
        manager.remove_readonly_input_directory(
            "worker",
            directory,
            filenames=filenames,
        )


def test_pi_request_files_use_private_mode_and_kali_owner() -> None:
    writes = []
    manager = SimpleNamespace(write_text_file=lambda *args, **kwargs: writes.append((args, kwargs)))
    request = WorkerInputFile(path="/tmp/cairn-prompts/fixture.json", content="fixture request")
    written = process_runner._write_worker_input_files(manager, "worker", (request,))
    assert written == [request]
    assert writes == [(("worker", request.path, request.content), {"owner": "kali", "mode": 0o600})]
    assert not hasattr(hook_records, "prepare_claude_hook_log")


def test_http_legacy_env_never_injects_pi_ca_overrides() -> None:
    worker = WorkerConfig(
        name="fixture-pi",
        type="pi",
        task_types=["bootstrap"],
        max_running=1,
        priority=0,
        env={
            "ANTHROPIC_MODEL": "test-model",
            "ANTHROPIC_BASE_URL": "http://model.test:8317",
            "ANTHROPIC_AUTH_TOKEN": "test-token",
        },
    )

    assert process_runner._worker_process_env(worker) == worker.env
    assert "NODE_EXTRA_CA_CERTS" not in process_runner._worker_process_env(worker)


def test_https_legacy_env_never_injects_pi_ca_overrides() -> None:
    worker = WorkerConfig(
        name="fixture-pi",
        type="pi",
        task_types=["bootstrap"],
        max_running=1,
        priority=0,
        env={
            "ANTHROPIC_MODEL": "test-model",
            "ANTHROPIC_BASE_URL": "https://model.test",
            "ANTHROPIC_AUTH_TOKEN": "test-token",
        },
    )

    assert process_runner._worker_process_env(worker) == worker.env
    assert "NODE_EXTRA_CA_CERTS" not in process_runner._worker_process_env(worker)


@pytest.mark.parametrize("auth_header", ["authorization-bearer", "x-api-key"])
@pytest.mark.parametrize("protocol", ["anthropic-messages", "openai-chat-completions", "openai-responses"])
def test_materialize_worker_preserves_model_request_semantics(auth_header: str, protocol: str) -> None:
    binding = SimpleNamespace(
        worker_id="worker-1",
        model_id="model-1",
        task_types=["bootstrap"],
        max_running=1,
        priority=10,
        model_name="model-test",
        base_url="https://model.test/gateway",
        inference_endpoint="https://model.test/gateway/v1/messages",
        auth_header=auth_header,
        custom_headers=[
            SimpleNamespace(name="X-Tenant", value="tenant-a"),
            SimpleNamespace(name="Authorization", value="custom-auth"),
            SimpleNamespace(name="x-api-key", value="custom-key"),
        ],
        api_key="active-secret",
        worker_revision=2,
        model_revision=3,
        credential_version_id="tmcv-1",
        credential_version=4,
        credential_fingerprint="fingerprint",
        engine="pi",
        protocol=protocol,
    )
    runtime = object.__new__(WorkerRuntimeMixin)
    runtime.client = SimpleNamespace(resolve_execution_binding=lambda *_args: binding)
    candidate = WorkerConfig(
        name="worker-1",
        type="pi",
        task_types=["bootstrap"],
        max_running=1,
        priority=10,
        env={
            "ANTHROPIC_MODEL": "pending",
            "ANTHROPIC_BASE_URL": "https://pending.invalid",
            "ANTHROPIC_AUTH_TOKEN": "pending",
        },
    )

    materialized = runtime._materialize_worker("proj-1", candidate, "bootstrap")

    assert materialized is not None
    worker, _provenance, circuit_key = materialized
    assert worker.env == {}
    assert worker.model_binding is not None
    assert worker.model_binding.api_key == "active-secret"
    assert worker.model_binding.protocol == protocol
    assert worker.model_binding.auth_header == auth_header
    assert worker.model_binding.base_url == "https://model.test/gateway"
    assert worker.model_binding.inference_endpoint == "https://model.test/gateway/v1/messages"
    assert [header.model_dump() for header in worker.model_binding.custom_headers] == [
        {"name": "X-Tenant", "value": "tenant-a"},
        {"name": "Authorization", "value": "custom-auth"},
        {"name": "x-api-key", "value": "custom-key"},
    ]
    assert "active-secret" not in repr(worker)
    assert circuit_key.model_id == "model-1"
    assert circuit_key.credential_version_id == "tmcv-1"
    assert "active-secret" not in repr(circuit_key)
    assert "tenant-a" not in repr(circuit_key)


def test_materialize_selected_worker_skips_all_workers_on_open_shared_binding() -> None:
    def project_binding(worker_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=worker_id,
            engine="pi",
            task_types=["explore"],
            max_running=1,
            priority=10,
            enabled=True,
            currently_available=True,
        )

    def execution_binding(
        worker_id: str,
        *,
        credential_version_id: str,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            worker_id=worker_id,
            model_id="model-1",
            task_types=["explore"],
            max_running=1,
            priority=10,
            model_name="model-test",
            base_url="https://model.test",
            inference_endpoint="https://model.test/v1/messages",
            auth_header="x-api-key",
            custom_headers=[],
            api_key=f"secret-{worker_id}",
            worker_revision=1,
            model_revision=3,
            credential_version_id=credential_version_id,
            credential_version=4,
            credential_fingerprint=f"fingerprint-{worker_id}",
            engine="pi",
            protocol="anthropic-messages",
        )

    project_workers = [
        project_binding("worker-a"),
        project_binding("worker-b"),
        project_binding("worker-c"),
    ]
    resolved = {
        "worker-a": execution_binding(
            "worker-a", credential_version_id="tmcv-shared"
        ),
        "worker-b": execution_binding(
            "worker-b", credential_version_id="tmcv-shared"
        ),
        "worker-c": execution_binding(
            "worker-c", credential_version_id="tmcv-other"
        ),
    }
    runtime = object.__new__(WorkerRuntimeMixin)
    runtime.client = SimpleNamespace(
        resolve_execution_binding=lambda _project, worker_id, _task: resolved[
            worker_id
        ]
    )
    runtime.futures = {}
    runtime.binding_auth_circuit_attempts = {}
    shared_key = runtime._binding_circuit_key(resolved["worker-a"])
    assert shared_key == runtime._binding_circuit_key(resolved["worker-b"])
    runtime.binding_auth_circuit_until = {shared_key: 10**20}
    runtime._log_changed = lambda *args, **kwargs: None
    runtime._clear_log_state = lambda key: None
    selection = WorkerSelection(
        worker=runtime._project_worker_config(project_workers[0]),
        candidate_order=[
            {"id": "worker-a"},
            {"id": "worker-b"},
            {"id": "worker-c"},
        ],
        blocked_busy=[],
        blocked_cooldown=[],
        blocked_rejected=[],
        blocked_task_type=[],
        blocked_disabled=[],
    )

    materialized = runtime._materialize_selected_worker(
        "proj-1", "explore", selection, project_workers
    )

    assert materialized is not None
    worker, _provenance, selected_key = materialized
    assert worker.id == "worker-c"
    assert selected_key.credential_version_id == "tmcv-other"
    assert [item["id"] for item in selection.blocked_cooldown] == [
        "worker-a",
        "worker-b",
    ]
    assert all(
        item["reason"] == "binding_auth_circuit"
        for item in selection.blocked_cooldown
    )


class FakeProcess:
    def __init__(self) -> None:
        self.started = False

    def start(self) -> None:
        self.started = True

    def communicate(self, timeout=None) -> ProcessResult:
        return ProcessResult(returncode=0, stdout="ok", stderr="")


class FakeProcessManager:
    def __init__(self) -> None:
        self.env: dict[str, str] | None = None
        self.process = FakeProcess()

    def build_exec_process(self, container_name, env, argv, *, timeout_seconds):
        self.env = dict(env)
        return self.process


def test_worker_process_uses_configured_environment_without_capture(monkeypatch) -> None:
    manager = FakeProcessManager()
    worker = WorkerConfig(
        name="fixture-pi",
        type="pi",
        task_types=["bootstrap"],
        max_running=1,
        priority=0,
        env={"CUSTOM": "value"},
    )
    monkeypatch.setattr(process_runner, "record_pi_tool_events", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "record_command_started", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "record_command_finished", lambda *_args, **_kwargs: None)

    result = process_runner.run_worker_process(
        manager,
        "worker",
        worker,
        DriverResult(["cairn-agent", "--request", "/tmp/cairn-prompts/fixture.json"]),
        phase="bootstrap_execute",
        timeout_seconds=30,
        client=SimpleNamespace(create_agent_event=lambda *_args, **_kwargs: SimpleNamespace(ok=True)),
        project_id="proj_011",
        task_run_id="tr001",
    )

    assert result.returncode == 0
    assert manager.process.started is True
    assert manager.env == {"CUSTOM": "value"}


def test_worker_process_exposes_payload_recorder_only_when_enabled(monkeypatch) -> None:
    manager = FakeProcessManager()
    worker = WorkerConfig(
        name="fixture-pi",
        type="pi",
        task_types=["explore"],
        max_running=1,
        priority=0,
        env={
            "PATH": "/custom/bin",
            "ANTHROPIC_MODEL": "test",
            "ANTHROPIC_BASE_URL": "https://model.test",
            "ANTHROPIC_AUTH_TOKEN": "test-token",
        },
    )
    payload_log = payload_records.PayloadOperationLog(
        path="/tmp/cairn-payloads/proj/tr/explore.jsonl",
        phase="explore_execute",
    )
    monkeypatch.setattr(process_runner, "record_pi_tool_events", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "prepare_payload_recorder", lambda *_args, **_kwargs: payload_log)
    monkeypatch.setattr(process_runner, "record_payload_operations", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "record_command_started", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "record_command_finished", lambda *_args, **_kwargs: None)

    result = process_runner.run_worker_process(
        manager,
        "worker",
        worker,
        DriverResult(["cairn-agent", "--request", "/tmp/cairn-prompts/fixture.json"]),
        phase="explore_execute",
        timeout_seconds=30,
        client=SimpleNamespace(create_agent_event=lambda *_args, **_kwargs: SimpleNamespace(ok=True)),
        project_id="proj_011",
        task_run_id="tr001",
        payload_recorder_enabled=True,
    )

    assert result.returncode == 0
    assert manager.env is not None
    assert manager.env["CAIRN_PAYLOAD_LOG"] == payload_log.path
    assert manager.env["PATH"] == (
        f"{payload_records.PAYLOAD_RECORDER_BIN_ROOT}:/custom/bin"
    )
    assert "CAIRN_DISPATCHER_TOKEN" not in manager.env


def test_runtime_version_probe_reads_complete_output_and_cleans_file(tmp_path) -> None:
    stdout_path = tmp_path / "version.stdout"
    stdout_path.write_text("runtime-tool " + "x" * 500 + "\n", encoding="utf-8")
    result = ProcessResult(
        returncode=0,
        stdout="bounded preview with wrong value",
        stderr="",
        _stdout_path=str(stdout_path),
    )

    class VersionProcess:
        def start(self) -> None:
            return None

        def communicate(self, timeout=None) -> ProcessResult:
            return result

    runtime = object.__new__(TaskRunFinishMixin)
    runtime.container_manager = SimpleNamespace(
        build_exec_process=lambda *_args, **_kwargs: VersionProcess()
    )

    version = runtime._probe_version("worker", ["runtime-tool", "--version"])

    assert version.startswith("runtime-tool ")
    assert version.endswith("...")
    assert len(version) == 160
    assert not stdout_path.exists()


def test_runtime_version_probe_cleans_output_when_parsing_fails(monkeypatch, tmp_path) -> None:
    stdout_path = tmp_path / "failed-version.stdout"
    stdout_path.write_text("runtime-tool 1.2.3\n", encoding="utf-8")
    result = ProcessResult(
        returncode=0,
        stdout="runtime-tool 1.2.3",
        stderr="",
        _stdout_path=str(stdout_path),
    )

    class VersionProcess:
        def start(self) -> None:
            return None

        def communicate(self, timeout=None) -> ProcessResult:
            return result

    runtime = object.__new__(TaskRunFinishMixin)
    runtime.container_manager = SimpleNamespace(
        build_exec_process=lambda *_args, **_kwargs: VersionProcess()
    )
    monkeypatch.setattr(
        TaskRunFinishMixin,
        "_version_from_probe_result",
        staticmethod(lambda _result: (_ for _ in ()).throw(ValueError("bad version"))),
    )

    assert runtime._probe_version("worker", ["runtime-tool", "--version"]) == "unknown"
    assert not stdout_path.exists()


@pytest.mark.parametrize("returncode", [0, 1])
@pytest.mark.parametrize(("code", "expected"), [
    ("provider_auth", "provider_auth"),
    ("provider_rate_limit", "provider_rate_limit"),
    ("provider_error", "provider_error"),
    ("session_corrupt", "session_error"),
    ("invalid_result_schema", "request_contract"),
    ("deadline_exceeded", "worker_timeout"),
])
def test_historical_healthcheck_envelope_decodes_without_diagnostic_leak(
    tmp_path,
    returncode: int,
    code: str,
    expected: str,
) -> None:
    session = "pi:07b53667-a6ab-44be-badb-e3ed6b347331"
    raw = json.dumps({
        "protocol": "cairn-agent-result-v1", "engine": "pi", "project_id": "project-fixture",
        "task_run_id": "run-fixture", "phase": "healthcheck", "session": session,
        "status": "failed", "result": None, "submission_id": None,
        "usage": {"fixture_padding": "x" * 70_000},
        "error": {"code": code, "message": "private-fixture-provider-body"},
    })
    stdout_path = tmp_path / "healthcheck.stdout"
    stdout_path.write_text(raw, encoding="utf-8")
    result = ProcessResult(
        returncode,
        "bounded preview without terminal code",
        "http_status=401\nauth_401_confirmed=1",
        _stdout_path=str(stdout_path),
    )
    try:
        failure = process_runner.worker_process_failure(
            result,
            phase="healthcheck",
            project_id="project-fixture",
            task_run_id="run-fixture",
            session=session,
        )
        assert failure is not None
        assert failure.details()["error_type"] == expected
        assert "private-fixture-provider-body" not in json.dumps(
            failure.details()
        )
        assert "http_status" not in json.dumps(failure.details())
        assert stdout_path.read_text(encoding="utf-8") == raw
    finally:
        result.cleanup()
    assert not stdout_path.exists()
