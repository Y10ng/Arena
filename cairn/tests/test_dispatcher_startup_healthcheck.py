from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cairn.dispatcher.config import (
    BootstrapTaskConfig,
    ContainerConfig,
    DispatchConfig,
    ExploreTaskConfig,
    ReasonTaskConfig,
    RuntimeConfig,
    TasksConfig,
    WorkerConfig,
)
from cairn.dispatcher.runtime import readiness, startup_healthcheck
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.scheduler import config_runtime


class DummyContainerManager:
    def __init__(self) -> None:
        self.created = 0
        self.removed: list[str] = []

    def create_startup_container(self) -> str:
        self.created += 1
        return "startup-container"

    def remove_container(self, name: str, *, force: bool) -> None:
        assert force is True
        self.removed.append(name)

    def oom_kill_count(self, _name: str) -> int | None:
        return None

    def detect_oom_kill(
        self,
        _name: str,
        *,
        before_count: int | None,
    ) -> tuple[bool, str | None]:
        assert before_count is None
        return False, None


def make_config(workers: list[WorkerConfig]) -> DispatchConfig:
    return DispatchConfig(
        server="http://127.0.0.1:8000",
        runtime=RuntimeConfig(
            max_workers=4,
            max_running_projects=1,
            max_project_workers=1,
            interval=1,
            healthcheck_timeout=1,
            prompt_group="default",
        ),
        tasks=TasksConfig(
            bootstrap=BootstrapTaskConfig(timeout=1, conclude_timeout=1),
            reason=ReasonTaskConfig(timeout=1, max_intents=1),
            explore=ExploreTaskConfig(timeout=1, conclude_timeout=1),
        ),
        container=ContainerConfig(image="test:latest", network_mode="none", completed_action="stop"),
        workers=workers,
    )


def pi_worker(name: str, *, enabled: bool) -> WorkerConfig:
    return WorkerConfig(
        id=name,
        engine="pi",
        task_types=["bootstrap", "reason", "explore"],
        concurrency=1,
        priority=0,
        enabled=enabled,
        env={},
    )


@pytest.mark.parametrize("engine", ["claudecode", "codex", "mock"])
def test_disabled_workers_still_reject_unsupported_worker_types(engine) -> None:
    with pytest.raises(ValueError):
        WorkerConfig(
            id="draft-legacy", engine=engine, task_types=["reason"],
            concurrency=1, priority=0, enabled=False,
        )


def test_startup_healthcheck_skips_container_without_enabled_workers() -> None:
    manager = DummyContainerManager()
    config = make_config([pi_worker("draft-worker", enabled=False)])

    assert startup_healthcheck.run_startup_healthchecks(config, manager) == []
    assert manager.created == 0
    assert manager.removed == []


def test_startup_healthcheck_only_runs_enabled_workers(monkeypatch) -> None:
    manager = DummyContainerManager()
    checked: list[str] = []

    def fake_healthcheck(_manager, _container_name, worker, _timeout):
        checked.append(worker.name)
        return startup_healthcheck.StartupHealthcheckResult(
            worker_name=worker.name,
            ok=True,
            returncode=0,
            duration_ms=1,
            http_status="200",
            response_preview="ok",
            stderr_preview="",
            command="mock-healthcheck",
        )

    monkeypatch.setattr(startup_healthcheck, "_run_worker_healthcheck", fake_healthcheck)
    config = make_config(
        [
            pi_worker("draft-worker", enabled=False),
            pi_worker("enabled-worker", enabled=True),
        ]
    )

    results = startup_healthcheck.run_startup_healthchecks(config, manager)

    assert checked == ["enabled-worker"]
    assert [result.worker_name for result in results] == ["enabled-worker"]
    assert manager.created == 1
    assert manager.removed == ["startup-container"]


class ProbeProcess:
    def __init__(
        self,
        result: ProcessResult,
        *,
        start_error: Exception | None = None,
        communicate_error: Exception | None = None,
        cleanup_error: Exception | None = None,
    ) -> None:
        self.result = result
        self.start_error = start_error
        self.communicate_error = communicate_error
        self.cleanup_error = cleanup_error
        self.communicate_timeouts: list[int] = []
        self.cleanup_calls = 0

    def start(self) -> None:
        if self.start_error is not None:
            raise self.start_error

    def communicate(self, *, timeout: int) -> ProcessResult:
        self.communicate_timeouts.append(timeout)
        if self.communicate_error is not None:
            raise self.communicate_error
        return self.result

    def cleanup_output(self) -> None:
        self.cleanup_calls += 1
        self.result.cleanup()
        if self.cleanup_error is not None:
            raise self.cleanup_error


class ProbeContainerManager(DummyContainerManager):
    def __init__(self, process: ProbeProcess) -> None:
        super().__init__()
        self.process = process
        self.commands: list[tuple[str, dict[str, str], list[str], int]] = []
        self.fail_at: str | None = None
        self.oom_evidence = (False, None)

    def oom_kill_count(self, _name: str) -> int | None:
        if self.fail_at == "oom_kill_count":
            raise RuntimeError("oom count unavailable")
        return 7

    def build_exec_process(
        self,
        name: str,
        env: dict[str, str],
        command: list[str],
        *,
        timeout_seconds: int,
    ) -> ProbeProcess:
        if self.fail_at == "build_exec_process":
            raise RuntimeError("exec unavailable")
        self.commands.append((name, env, command, timeout_seconds))
        return self.process

    def detect_oom_kill(
        self,
        _name: str,
        *,
        before_count: int | None,
    ) -> tuple[bool, str | None]:
        assert before_count == 7
        if self.fail_at == "detect_oom_kill":
            raise RuntimeError("oom detection unavailable")
        return self.oom_evidence


def valid_self_check(
    capabilities: object = (
        "candidate-spool-v1",
        "execution-checkpoint-spool-v1",
    ),
) -> str:
    return json.dumps(
        {
            "status": "ok",
            "engine": "pi",
            "offline": True,
            "capabilities": list(capabilities) if isinstance(capabilities, tuple) else capabilities,
        }
    )


def test_readiness_public_contract_runs_only_offline_self_check_and_cleans_output(
    tmp_path,
) -> None:
    stdout_path = tmp_path / "readiness.stdout"
    stderr_path = tmp_path / "readiness.stderr"
    stdout_path.write_text(valid_self_check(), encoding="utf-8")
    stderr_path.write_text("private runtime detail", encoding="utf-8")
    process_result = ProcessResult(
        0,
        "bounded preview",
        "bounded stderr",
        _stdout_path=str(stdout_path),
        _stderr_path=str(stderr_path),
    )
    process = ProbeProcess(process_result)
    manager = ProbeContainerManager(process)

    result = readiness.run_readiness_check(
        manager,
        "startup-container",
        timeout_seconds=3,
    )

    assert readiness.READINESS_ERROR_TYPES == frozenset(
        {
            "readiness_timeout",
            "readiness_transport",
            "readiness_oom",
            "readiness_invalid",
            "readiness_process",
        }
    )
    assert result.ok is True
    assert result.error_type is None
    assert result.returncode == 0
    assert result.duration_ms >= 0
    assert result.capabilities == frozenset(
        {"candidate-spool-v1", "execution-checkpoint-spool-v1"}
    )
    assert manager.commands == [
        ("startup-container", {}, ["cairn-agent", "--self-check"], 3)
    ]
    assert process.communicate_timeouts == [
        3 + readiness.READINESS_COMMUNICATE_GRACE_SECONDS
    ]
    assert process.cleanup_calls == 1
    assert not stdout_path.exists()
    assert not stderr_path.exists()


@pytest.mark.parametrize(
    "stdout",
    [
        "not-json",
        "{}",
        '{"status":"ok","engine":"pi","offline":true}',
        valid_self_check("execution-checkpoint-spool-v1"),
        valid_self_check([" leading-space"]),
        valid_self_check(["duplicate", "duplicate"]),
        valid_self_check(["contains space"]),
        valid_self_check(["x" * 129]),
        valid_self_check([f"capability-{index}" for index in range(65)]),
        '{"status":"ok","status":"bad","engine":"pi","offline":true,"capabilities":[]}',
        '{"status":"ok","engine":"other","offline":true,"capabilities":[]}',
        '{"status":"ok","engine":"pi","offline":1,"capabilities":[]}',
    ],
)
def test_readiness_strictly_rejects_invalid_self_check_output(stdout: str) -> None:
    process_result = ProcessResult(0, stdout, "")
    manager = ProbeContainerManager(ProbeProcess(process_result))

    result = readiness.run_readiness_check(
        manager,
        "startup-container",
        timeout_seconds=1,
    )

    assert result.ok is False
    assert result.error_type == "readiness_invalid"
    assert result.capabilities == frozenset()
    assert process_result._cleaned is True


@pytest.mark.parametrize(
    ("process_result", "oom_evidence", "expected"),
    [
        (ProcessResult(0, valid_self_check([]), "", cancelled=True), (False, None), "readiness_process"),
        (ProcessResult(137, "", "", timed_out=True), (True, "cgroup_v2_memory_events"), "readiness_oom"),
        (ProcessResult(137, "", ""), (False, None), "readiness_timeout"),
        (ProcessResult(124, "", ""), (False, None), "readiness_timeout"),
        (ProcessResult(0, valid_self_check([]), "", transport_error=True), (False, None), "readiness_transport"),
        (ProcessResult(2, "", ""), (False, None), "readiness_process"),
    ],
)
def test_readiness_classifies_process_facts_with_oom_precedence(
    process_result: ProcessResult,
    oom_evidence: tuple[bool, str | None],
    expected: str,
) -> None:
    manager = ProbeContainerManager(ProbeProcess(process_result))
    manager.oom_evidence = oom_evidence

    result = readiness.run_readiness_check(
        manager,
        "startup-container",
        timeout_seconds=1,
    )

    assert result.ok is False
    assert result.error_type == expected
    assert process_result._cleaned is True


@pytest.mark.parametrize(
    "failure_stage",
    [
        "oom_kill_count",
        "build_exec_process",
        "start",
        "communicate",
        "detect_oom_kill",
    ],
)
def test_readiness_classifies_runtime_failures_and_cleans_owned_output(
    failure_stage: str,
    tmp_path,
) -> None:
    stdout_path = tmp_path / f"{failure_stage}.stdout"
    stdout_path.write_text(valid_self_check(), encoding="utf-8")
    returncode = 137 if failure_stage == "detect_oom_kill" else 0
    process_result = ProcessResult(
        returncode,
        "preview",
        "",
        _stdout_path=str(stdout_path),
    )
    process = ProbeProcess(
        process_result,
        start_error=(RuntimeError("start failed") if failure_stage == "start" else None),
        communicate_error=(
            RuntimeError("communicate failed")
            if failure_stage == "communicate"
            else None
        ),
    )
    manager = ProbeContainerManager(process)
    if failure_stage in {
        "oom_kill_count",
        "build_exec_process",
        "detect_oom_kill",
    }:
        manager.fail_at = failure_stage

    result = readiness.run_readiness_check(
        manager,
        "startup-container",
        timeout_seconds=1,
    )

    assert result.ok is False
    assert result.error_type == "readiness_transport"
    if failure_stage in {"start", "communicate", "detect_oom_kill"}:
        assert process.cleanup_calls == 1
        assert not stdout_path.exists()
    else:
        assert process.cleanup_calls == 0


@pytest.mark.parametrize("helper", ["_process_error_type", "_parse_self_check"])
def test_readiness_cleans_result_when_classification_or_parser_raises(
    monkeypatch: pytest.MonkeyPatch,
    helper: str,
    tmp_path,
) -> None:
    stdout_path = tmp_path / f"{helper}.stdout"
    stdout_path.write_text(valid_self_check([]), encoding="utf-8")
    process_result = ProcessResult(
        0,
        "preview",
        "",
        _stdout_path=str(stdout_path),
    )
    process = ProbeProcess(process_result)
    manager = ProbeContainerManager(process)
    monkeypatch.setattr(
        readiness,
        helper,
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("failed")),
    )

    with pytest.raises(RuntimeError, match="failed"):
        readiness.run_readiness_check(
            manager,
            "startup-container",
            timeout_seconds=1,
        )

    assert not stdout_path.exists()
    assert process.cleanup_calls == 1


def test_readiness_does_not_hide_output_cleanup_failure() -> None:
    process_result = ProcessResult(0, valid_self_check([]), "")
    process = ProbeProcess(
        process_result,
        cleanup_error=OSError("cleanup failed"),
    )
    manager = ProbeContainerManager(process)

    with pytest.raises(OSError, match="cleanup failed"):
        readiness.run_readiness_check(
            manager,
            "startup-container",
            timeout_seconds=1,
        )

    assert process_result._cleaned is True
    assert process.cleanup_calls == 1


def test_startup_healthcheck_delegates_to_readiness(monkeypatch) -> None:
    observed = []

    def fake_readiness(manager, container_name, *, timeout_seconds):
        observed.append((manager, container_name, timeout_seconds))
        return readiness.ReadinessResult(
            ok=False,
            error_type="readiness_invalid",
            returncode=0,
            duration_ms=12,
            capabilities=frozenset(),
        )

    monkeypatch.setattr(startup_healthcheck, "run_readiness_check", fake_readiness)
    manager = DummyContainerManager()

    summary = startup_healthcheck._run_worker_healthcheck(
        manager,
        "startup-container",
        pi_worker("enabled-worker", enabled=True),
        4,
    )

    assert observed == [(manager, "startup-container", 4)]
    assert summary.ok is False
    assert summary.error_type == "readiness_invalid"
    assert summary.response_preview == "readiness_invalid"
    assert summary.stderr_preview == ""
    assert summary.command == "cairn-agent --self-check"


class _ConfigRuntimeHarness(config_runtime.ConfigRuntimeMixin):
    pass


def _staging_config(mode: str) -> DispatchConfig:
    config = make_config([pi_worker("enabled-worker", enabled=True)])
    return config.model_copy(
        update={
            "runtime": config.runtime.model_copy(
                update={"execution_checkpoints": mode}
            )
        }
    )


def _startup_result(
    *,
    worker_name: str = "enabled-worker",
    ok: bool = True,
    capabilities: frozenset[str] = frozenset({"candidate-spool-v1"}),
) -> startup_healthcheck.StartupHealthcheckResult:
    return startup_healthcheck.StartupHealthcheckResult(
        worker_name=worker_name,
        ok=ok,
        returncode=0 if ok else 1,
        duration_ms=1,
        http_status=None,
        response_preview="ok" if ok else "failed",
        stderr_preview="",
        command="cairn-agent --self-check",
        capabilities=capabilities,
    )


def _server_candidate_capability(**updates: object) -> dict[str, object]:
    capability: dict[str, object] = {
        "required": True,
        "spool_protocol": "cairn-candidate-spool-v1",
        "frame_protocol": "cairn-candidate-frame-v1",
        "receipt_protocol": "cairn-candidate-capture-receipt-v1",
        "manifest_header": "X-Cairn-Candidate-Manifest",
        "manifest_encoding": "base64url-utf8-canonical-json-no-padding",
        "content_type": "application/octet-stream",
        "source_format": "pi-toolcall-delta-utf8-v1",
        "max_candidates_per_task_run": 16,
        "max_raw_bytes": 2 * 1024 * 1024,
        "max_manifest_bytes": 4 * 1024,
        "max_spool_bytes": 34 * 1024 * 1024,
        "max_outcomes_per_candidate": 64,
        "max_artifacts_per_candidate": 16,
    }
    capability.update(updates)
    return capability


def _server_checkpoint_capability(**updates: object) -> dict[str, object]:
    capability: dict[str, object] = {
        "enabled": True,
        "protocol": "cairn-execution-event-v1",
        "max_events": 64,
        "max_event_bytes": 16384,
        "max_spool_bytes": 524288,
    }
    capability.update(updates)
    return capability


def test_candidate_capability_is_required_without_enabled_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _ConfigRuntimeHarness()
    harness.config = make_config([pi_worker("disabled-worker", enabled=False)])
    harness.container_manager = object()
    calls: list[bool] = []
    harness.client = SimpleNamespace(
        get_candidate_capabilities=lambda *, force_refresh=False: (
            calls.append(force_refresh) or _server_candidate_capability()
        )
    )
    monkeypatch.setattr(
        config_runtime,
        "run_startup_healthchecks",
        lambda *_args, **_kwargs: [],
    )

    harness._run_startup_healthchecks(show_commands=False)

    assert calls == [True]
    assert harness._worker_capabilities == {}


@pytest.mark.parametrize(
    "capability",
    [
        None,
        _server_candidate_capability(required=False),
        _server_candidate_capability(spool_protocol="wrong"),
        _server_candidate_capability(max_candidates_per_task_run=15),
        _server_candidate_capability(max_raw_bytes=2 * 1024 * 1024 - 1),
        _server_candidate_capability(max_manifest_bytes=4095),
        _server_candidate_capability(max_spool_bytes=34 * 1024 * 1024 - 1),
        _server_candidate_capability(max_outcomes_per_candidate=63),
        _server_candidate_capability(max_artifacts_per_candidate=15),
        _server_candidate_capability(max_artifacts_per_candidate=True),
    ],
)
def test_candidate_capture_fails_closed_for_unusable_server_capability(
    monkeypatch: pytest.MonkeyPatch,
    capability: dict[str, object] | None,
) -> None:
    harness = _ConfigRuntimeHarness()
    harness.config = _staging_config("off")
    harness.container_manager = object()
    harness.client = SimpleNamespace(
        get_candidate_capabilities=lambda **_kwargs: capability,
    )
    monkeypatch.setattr(
        config_runtime,
        "run_startup_healthchecks",
        lambda *_args, **_kwargs: [_startup_result()],
    )

    with pytest.raises(RuntimeError, match="mandatory Server capability"):
        harness._run_startup_healthchecks(show_commands=False)
    assert not hasattr(harness, "_worker_capabilities")


@pytest.mark.parametrize(
    "results",
    [
        [],
        [_startup_result(ok=False)],
        [_startup_result(capabilities=frozenset())],
        [_startup_result(), _startup_result()],
    ],
    ids=["missing", "failed", "capability-missing", "duplicate"],
)
def test_candidate_capture_requires_one_healthy_result_per_enabled_worker(
    monkeypatch: pytest.MonkeyPatch,
    results: list[startup_healthcheck.StartupHealthcheckResult],
) -> None:
    harness = _ConfigRuntimeHarness()
    harness.config = _staging_config("off")
    harness.container_manager = object()
    harness.client = SimpleNamespace(
        get_candidate_capabilities=lambda **_kwargs: _server_candidate_capability(),
    )
    monkeypatch.setattr(
        config_runtime,
        "run_startup_healthchecks",
        lambda *_args, **_kwargs: results,
    )

    with pytest.raises(RuntimeError, match="runner capability.*enabled-worker"):
        harness._run_startup_healthchecks(show_commands=False)
    assert not hasattr(harness, "_worker_capabilities")


def test_candidate_capture_ignores_disabled_workers_but_rejects_any_failed_enabled_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _ConfigRuntimeHarness()
    harness.config = make_config(
        [
            pi_worker("healthy-worker", enabled=True),
            pi_worker("failed-worker", enabled=True),
            pi_worker("disabled-worker", enabled=False),
        ]
    )
    harness.container_manager = object()
    harness.client = SimpleNamespace(
        get_candidate_capabilities=lambda **_kwargs: _server_candidate_capability(),
    )
    results = [
        _startup_result(worker_name="healthy-worker"),
        _startup_result(worker_name="failed-worker", ok=False),
    ]
    monkeypatch.setattr(
        config_runtime,
        "run_startup_healthchecks",
        lambda *_args, **_kwargs: results,
    )

    with pytest.raises(RuntimeError, match="runner capability.*failed-worker"):
        harness._run_startup_healthchecks(show_commands=False)

    harness.config = make_config(
        [
            pi_worker("healthy-worker", enabled=True),
            pi_worker("disabled-worker", enabled=False),
        ]
    )
    monkeypatch.setattr(
        config_runtime,
        "run_startup_healthchecks",
        lambda *_args, **_kwargs: [results[0]],
    )
    harness._run_startup_healthchecks(show_commands=False)
    assert harness._worker_capabilities == {
        "healthy-worker": frozenset({"candidate-spool-v1"})
    }


def test_checkpoint_mode_off_still_requires_candidate_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _ConfigRuntimeHarness()
    harness.config = _staging_config("off")
    harness.container_manager = object()
    candidate_calls: list[bool] = []
    harness.client = SimpleNamespace(
        get_candidate_capabilities=lambda *, force_refresh=False: (
            candidate_calls.append(force_refresh) or _server_candidate_capability()
        ),
        get_execution_event_capabilities=lambda **_kwargs: pytest.fail(
            "off 模式不能查询 execution event capability"
        ),
    )
    monkeypatch.setattr(
        config_runtime,
        "run_startup_healthchecks",
        lambda *_args, **_kwargs: [_startup_result()],
    )

    harness._run_startup_healthchecks(show_commands=False)

    assert candidate_calls == [True]
    assert harness._worker_capabilities == {
        "enabled-worker": frozenset({"candidate-spool-v1"})
    }
    assert harness._execution_checkpoints_enabled_for(
        pi_worker("enabled-worker", enabled=True)
    ) is False


def test_checkpoint_stage_requires_server_and_runner_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _ConfigRuntimeHarness()
    harness.config = _staging_config("stage")
    harness.container_manager = object()
    calls: list[bool] = []
    capability = _server_checkpoint_capability()
    harness.client = SimpleNamespace(
        get_candidate_capabilities=lambda **_kwargs: _server_candidate_capability(),
        get_execution_event_capabilities=lambda *, force_refresh=False: (
            calls.append(force_refresh) or dict(capability)
        ),
    )
    monkeypatch.setattr(
        config_runtime,
        "run_startup_healthchecks",
        lambda *_args, **_kwargs: [
            _startup_result(
                capabilities=frozenset(
                    {"candidate-spool-v1", "execution-checkpoint-spool-v1"}
                )
            )
        ],
    )
    worker = pi_worker("enabled-worker", enabled=True)

    harness._run_startup_healthchecks(show_commands=False)

    assert calls == [True]
    assert harness._execution_checkpoints_enabled_for(worker) is True
    assert calls == [True, False]


@pytest.mark.parametrize(
    "capability",
    [
        None,
        _server_checkpoint_capability(enabled=False),
        _server_checkpoint_capability(protocol="wrong"),
        _server_checkpoint_capability(max_events=63),
        _server_checkpoint_capability(max_event_bytes=16383),
        _server_checkpoint_capability(max_spool_bytes=524287),
    ],
)
def test_checkpoint_stage_fails_closed_for_unusable_server_capability(
    monkeypatch: pytest.MonkeyPatch,
    capability: dict[str, object] | None,
) -> None:
    harness = _ConfigRuntimeHarness()
    harness.config = _staging_config("stage")
    harness.container_manager = object()
    calls: list[bool] = []
    harness.client = SimpleNamespace(
        get_candidate_capabilities=lambda **_kwargs: _server_candidate_capability(),
        get_execution_event_capabilities=lambda *, force_refresh=False: (
            calls.append(force_refresh) or capability
        ),
    )
    monkeypatch.setattr(
        config_runtime,
        "run_startup_healthchecks",
        lambda *_args, **_kwargs: [
            _startup_result(
                capabilities=frozenset(
                    {"candidate-spool-v1", "execution-checkpoint-spool-v1"}
                )
            )
        ],
    )

    with pytest.raises(RuntimeError, match="compatible enabled Server capability"):
        harness._run_startup_healthchecks(show_commands=False)
    assert calls == [True]


@pytest.mark.parametrize(
    "result",
    [
        _startup_result(),
        _startup_result(ok=False),
    ],
    ids=["capability-missing", "self-check-failed"],
)
def test_checkpoint_stage_fails_closed_without_runner_capability(
    monkeypatch: pytest.MonkeyPatch,
    result: startup_healthcheck.StartupHealthcheckResult,
) -> None:
    harness = _ConfigRuntimeHarness()
    harness.config = _staging_config("stage")
    harness.container_manager = object()
    harness.client = SimpleNamespace(
        get_candidate_capabilities=lambda **_kwargs: _server_candidate_capability(),
        get_execution_event_capabilities=lambda **_kwargs: _server_checkpoint_capability(),
    )
    monkeypatch.setattr(
        config_runtime,
        "run_startup_healthchecks",
        lambda *_args, **_kwargs: [result],
    )

    with pytest.raises(RuntimeError, match="runner capability.*enabled-worker"):
        harness._run_startup_healthchecks(show_commands=False)
