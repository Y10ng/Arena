from __future__ import annotations

from types import SimpleNamespace

from docker.errors import DockerException
import pytest

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.runtime.readiness import ReadinessResult
from cairn.dispatcher.runtime.containers import ContainerRuntimeMismatchError
from cairn.dispatcher.scheduler.worker_runtime import WorkerRuntimeMixin


def _runtime() -> WorkerRuntimeMixin:
    runtime = object.__new__(WorkerRuntimeMixin)
    runtime.config = SimpleNamespace(
        runtime=SimpleNamespace(healthcheck_timeout=7),
    )
    def activate(project_id, options, *, register, finish):
        token = register("daemon-test")
        try:
            return runtime.container_manager.ensure_running(project_id, options)
        finally:
            finish(token, f"id-{project_id}")

    runtime.container_manager = SimpleNamespace(
        ensure_running=lambda project_id, options: f"container-{project_id}",
        activate_for_readiness=activate,
    )
    runtime.client = SimpleNamespace(
        register_container_activation=lambda _project, _generation, _daemon: SimpleNamespace(
            ok=True, status_code=200, data={"activation_token": "fixture-token"},
        ),
        finish_container_activation=lambda _project, _token, _id: SimpleNamespace(ok=True),
    )
    runtime.worker_cooldown_until = {}
    runtime.worker_cooldown_attempts = {}
    runtime._project_generations = {"p1": 0}
    runtime._project_decisions = {}
    runtime._decision_log_state = {}
    return runtime


def _worker() -> WorkerConfig:
    return WorkerConfig(
        id="worker-1",
        engine="pi",
        task_types=["explore"],
        concurrency=1,
        priority=0,
    )


def test_readiness_failure_opens_only_readiness_worker_cooldown(monkeypatch) -> None:
    from cairn.dispatcher.scheduler import worker_runtime

    runtime = _runtime()
    runtime.worker_cooldown_attempts[("worker-1", "provider_error")] = 2
    monkeypatch.setattr(
        worker_runtime,
        "run_readiness_check",
        lambda *args, **kwargs: ReadinessResult(
            ok=False,
            error_type="readiness_timeout",
            returncode=124,
            duration_ms=7000,
            capabilities=frozenset(),
        ),
    )

    assert runtime._probe_worker_readiness(
        "p1",
        "explore",
        _worker(),
        SimpleNamespace(),
        0,
    ) is False

    cooldown_until, error_type = runtime.worker_cooldown_until["worker-1"]
    assert cooldown_until > 0
    assert error_type == "readiness_timeout"
    assert runtime.worker_cooldown_attempts == {
        ("worker-1", "provider_error"): 2,
        ("worker-1", "readiness_timeout"): 1,
    }
    assert runtime._project_decisions["p1"]["code"] == "readiness_timeout"


def test_container_transport_failure_is_readiness_cooldown(monkeypatch) -> None:
    from cairn.dispatcher.scheduler import worker_runtime

    runtime = _runtime()

    def fail_container_start(_project_id, _options):
        raise DockerException("daemon unavailable")

    def unexpected_readiness(*_args, **_kwargs):
        raise AssertionError("container failure must not start readiness")

    runtime.container_manager.ensure_running = fail_container_start
    monkeypatch.setattr(
        worker_runtime,
        "run_readiness_check",
        unexpected_readiness,
    )

    assert runtime._probe_worker_readiness(
        "p1",
        "explore",
        _worker(),
        SimpleNamespace(),
        0,
    ) is False

    assert runtime.worker_cooldown_until["worker-1"][1] == "readiness_transport"
    assert runtime.worker_cooldown_attempts == {
        ("worker-1", "readiness_transport"): 1,
    }
    assert runtime._project_decisions["p1"]["code"] == "readiness_transport"


def test_unresolved_legacy_inventory_blocks_readiness_without_worker_cooldown(monkeypatch) -> None:
    from cairn.dispatcher.scheduler import worker_runtime

    runtime = _runtime()
    attempts = []

    def register(_project, generation, _daemon):
        attempts.append(generation)
        return SimpleNamespace(
            ok=False, status_code=409, data={"detail": "container_inventory_unresolved"},
        )

    runtime.client.register_container_activation = register
    monkeypatch.setattr(
        worker_runtime, "run_readiness_check",
        lambda *_args, **_kwargs: pytest.fail("未知旧现场不能执行 Worker 探针"),
    )
    for _ in range(2):
        assert runtime._probe_worker_readiness("p1", "explore", _worker(), SimpleNamespace(), 0) is False
    assert attempts == [0]
    assert runtime.worker_cooldown_until == {}
    assert runtime._project_decisions["p1"]["code"] == "container_inventory_unresolved"


def test_preserved_container_mismatch_blocks_project_without_retrying_worker(monkeypatch) -> None:
    from cairn.dispatcher.scheduler import worker_runtime

    runtime = _runtime()
    attempted = []

    def mismatch(_project_id, options):
        attempted.append(options.runtime_signature)
        if options.runtime_signature == "changed":
            raise ContainerRuntimeMismatchError("preserved workspace")
        return "container-p1"

    runtime.container_manager.ensure_running = mismatch
    monkeypatch.setattr(
        worker_runtime, "run_readiness_check",
        lambda *_args, **_kwargs: ReadinessResult(
            ok=True, error_type=None, returncode=0, duration_ms=2,
            capabilities=frozenset(),
        ),
    )
    changed = SimpleNamespace(runtime_signature="changed")
    for _ in range(2):
        assert runtime._probe_worker_readiness("p1", "explore", _worker(), changed, 0) is False
    assert attempted == ["changed"]
    assert runtime.worker_cooldown_until == {}
    assert runtime._project_decisions["p1"]["code"] == "container_runtime_mismatch"

    corrected = SimpleNamespace(runtime_signature="original")
    assert runtime._probe_worker_readiness("p1", "explore", _worker(), corrected, 0) is True
    assert attempted == ["changed", "original"]


def test_readiness_success_clears_no_provider_failure_history(monkeypatch) -> None:
    from cairn.dispatcher.scheduler import worker_runtime

    runtime = _runtime()
    runtime.worker_cooldown_until["worker-1"] = (
        10**20,
        "readiness_invalid",
    )
    runtime.worker_cooldown_attempts.update(
        {
            ("worker-1", "provider_error"): 2,
            ("worker-1", "readiness_invalid"): 3,
        }
    )
    monkeypatch.setattr(
        worker_runtime,
        "run_readiness_check",
        lambda *args, **kwargs: ReadinessResult(
            ok=True,
            error_type=None,
            returncode=0,
            duration_ms=12,
            capabilities=frozenset({"execution-checkpoint-spool-v1"}),
        ),
    )

    assert runtime._probe_worker_readiness(
        "p1",
        "explore",
        _worker(),
        SimpleNamespace(),
        0,
    ) is True

    assert "worker-1" not in runtime.worker_cooldown_until
    assert runtime.worker_cooldown_attempts == {
        ("worker-1", "provider_error"): 2,
    }
