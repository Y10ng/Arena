"""真实调度入口与 heartbeat 快照，不启动 Docker 或模型。"""
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.scheduler.loop import DispatcherBootstrap, DispatcherLoop
from cairn.dispatcher.tasks.report_finish_outbox import ReportFinishOutbox
from test_dispatcher_multi_project_capacity import _loop, _summary
from test_pi_container_ownership import FakeDocker, dispatch_config
from test_report_finish_outbox import _entry as _report_finish_entry


@pytest.fixture
def startup_loop(monkeypatch, tmp_path):
    docker = FakeDocker()
    monkeypatch.setattr("cairn.dispatcher.runtime.containers.docker.from_env", lambda *, timeout: docker)
    monkeypatch.setenv("CAIRN_RUNTIME_TEMP_DIR", str(tmp_path / "runtime"))
    config = dispatch_config(dispatcher_id="synthetic-display")
    client = SimpleNamespace(
        base_url=config.server, close=lambda: None,
        dispatcher_heartbeat=Mock(return_value=ApiResult(
            status_code=200, data={"dispatcher_id": "dispatcher-test"},
        )),
    )
    loop = DispatcherLoop(
        DispatcherBootstrap(config_path=tmp_path / "synthetic-config.json", hard_cap=8),
        config=config, client=client,
    )
    assert loop.dispatcher_id is None and loop.container_manager is None
    yield loop
    loop.close()


@pytest.mark.parametrize(
    "equivalent_url",
    [
        " HTTPS://SERVER.EXAMPLE.TEST:443/api/ ",
        "https://server.example.test/a/../api/",
        "https://server.example.test/%61pi/",
    ],
)
def test_equivalent_server_urls_share_report_finish_outbox_namespace(
    startup_loop,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    equivalent_url: str,
):
    monkeypatch.setenv("CAIRN_DISPATCHER_TOKEN", "synthetic-token")
    equivalent_client = SimpleNamespace(
        base_url=equivalent_url,
        close=Mock(),
    )
    equivalent_config = startup_loop.config.model_copy(
        update={"server": equivalent_url}
    )
    equivalent = DispatcherLoop(
        DispatcherBootstrap(
            config_path=tmp_path / "synthetic-config.json",
            hard_cap=8,
        ),
        config=equivalent_config,
        client=equivalent_client,
    )
    try:
        assert (
            equivalent.report_finish_outbox.root
            == startup_loop.report_finish_outbox.root
        )
    finally:
        equivalent.close()


def test_outbox_namespace_uses_config_server_when_injected_client_is_replaced(
    startup_loop,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CAIRN_DISPATCHER_TOKEN", "synthetic-token")
    mismatched_client = SimpleNamespace(
        base_url="http://wrong-server.invalid:8000",
        close=Mock(),
    )
    replacement = DispatcherLoop(
        DispatcherBootstrap(
            config_path=tmp_path / "synthetic-config.json",
            hard_cap=8,
        ),
        config=startup_loop.config,
        client=mismatched_client,
    )
    try:
        assert replacement.client.base_url == startup_loop.config.server.rstrip("/")
        assert (
            replacement.report_finish_outbox.root
            == startup_loop.report_finish_outbox.root
        )
        mismatched_client.close.assert_called_once_with()
    finally:
        replacement.close()


def test_noncanonical_legacy_namespace_migrates_before_scheduler_start(
    startup_loop,
    tmp_path,
):
    raw_server = "HTTPS://SERVER.EXAMPLE.TEST:443/api"
    legacy_namespace = sha256(raw_server.encode("utf-8")).hexdigest()
    legacy_root = (
        tmp_path
        / "data"
        / f"dispatcher-report-finish-outbox-v1-{legacy_namespace}"
    )
    entry = _report_finish_entry()
    ReportFinishOutbox(legacy_root).persist(entry)
    legacy_client = SimpleNamespace(base_url=raw_server, close=Mock())
    legacy_config = startup_loop.config.model_copy(
        update={"server": raw_server}
    )
    migrated = DispatcherLoop(
        DispatcherBootstrap(
            config_path=tmp_path / "synthetic-config.json",
            hard_cap=8,
        ),
        config=legacy_config,
        client=legacy_client,
    )
    try:
        assert migrated.report_finish_outbox.entries() == [entry]
        assert not list(legacy_root.glob("*.json"))
        migrated.report_finish_outbox.remove(entry)
    finally:
        migrated.close()
    assert ReportFinishOutbox(legacy_root).entries() == []


def test_report_finish_outbox_is_namespaced_by_server_base_url(
    startup_loop,
    tmp_path,
):
    other_client = SimpleNamespace(
        base_url="http://other-server.invalid:8000",
        close=Mock(),
    )
    other_config = startup_loop.config.model_copy(
        update={"server": other_client.base_url}
    )
    other = DispatcherLoop(
        DispatcherBootstrap(
            config_path=tmp_path / "synthetic-config.json",
            hard_cap=8,
        ),
        config=other_config,
        client=other_client,
    )
    try:
        assert other.report_finish_outbox.root != startup_loop.report_finish_outbox.root
        assert (
            other.report_finish_outbox.root.parent
            == startup_loop.report_finish_outbox.root.parent
        )
    finally:
        other.close()


def test_global_capacity_is_observed_and_next_round_does_not_refresh_old_decision():
    loop = _loop(max_workers=1, max_running_projects=1, worker_count=1)
    loop.futures[object()] = SimpleNamespace(project_id="other", worker_name="w")
    summary = _summary("p1")
    loop._dispatch_available([summary])

    runtime = loop._dispatcher_runtime_summary()
    assert runtime["runtime_capabilities"] == ["project_pause_v1", "project_runtime_v1"]
    decision = runtime["project_decisions"][0]
    assert decision["project_id"] == "p1"
    assert decision["execution_generation"] == 0
    assert decision["code"] == "global_capacity"
    assert decision["observed_at"]

    loop.futures.clear()
    loop._dispatch_available([])
    assert loop._dispatcher_runtime_summary()["project_decisions"] == []


def test_no_binding_is_observed_without_creating_a_task_run():
    loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
    loop._project_generations = {"p1": 7}
    selection = loop._select_worker("p1", "bootstrap", SimpleNamespace(key="default"), [])
    assert selection.worker is None
    assert loop.futures == {}
    decision = loop._dispatcher_runtime_summary()["project_decisions"][0]
    assert decision["code"] == "no_candidates"
    assert decision["task_type"] == "bootstrap"
    assert decision["execution_generation"] == 7


@pytest.mark.parametrize("status_code", [200, 503])
def test_close_attempts_all_pending_finishes_once_without_unbounded_retry(status_code):
    from concurrent.futures import ThreadPoolExecutor
    from cairn.dispatcher.protocol.client import ApiResult

    loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
    loop.executor = ThreadPoolExecutor(max_workers=1)
    loop.cleanup_executor = ThreadPoolExecutor(max_workers=2)
    loop._finish_runtime_context = lambda *_args: {}
    loop.container_manager.close = Mock()
    loop.client.close = Mock()
    loop.client.update_task_run = Mock(return_value=ApiResult(status_code=status_code))
    for index in range(10):
        loop._finish_task_run("project", f"run-{index}", "failed")

    DispatcherLoop.close(loop)

    calls = loop.client.update_task_run.call_args_list
    assert len(calls) == 10
    assert {call.args for call in calls} == {("project", f"run-{index}") for index in range(10)}
    assert not loop._task_run_finish_futures
    assert len(loop.pending_task_run_finishes) == (0 if status_code == 200 else 10)
    loop.container_manager.close.assert_called_once_with()
    loop.client.close.assert_called_once_with()


def test_control_observation_continues_after_finish_probe_has_entered():
    import threading
    from concurrent.futures import Future, ThreadPoolExecutor
    from cairn.dispatcher.protocol.client import ApiResult
    from cairn.dispatcher.runtime.cancellation import TaskCancellation
    from cairn.dispatcher.runtime.process import ProcessResult

    loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
    entered, release, queued, cancelled = (threading.Event() for _ in range(4))

    def communicate(**_kwargs):
        entered.set()
        assert release.wait(3)
        return ProcessResult(0, "fixture-version", "")

    loop.container_manager.build_exec_process = lambda *_a, **_kw: SimpleNamespace(start=lambda: None, communicate=communicate)
    loop._finish_runtime_context = lambda *_a: {"python_version": loop._probe_version("exact-container", ["python3", "--version"])}
    loop.client.update_task_run = Mock(return_value=ApiResult(status_code=200))
    loop.client.list_projects = lambda: [_summary("p1", status="paused")]
    token = TaskCancellation()
    token.attach_process(SimpleNamespace(cancel=lambda _reason: cancelled.set()))
    loop.futures[Future()] = SimpleNamespace(project_id="p1", task_type="reason", worker_name="w", cancellation=token)
    loop._settings_checked = loop._startup_healthchecks_checked = loop._startup_reconcile_checked = True
    loop.reason_checkpoints = {}
    for method in ("_ensure_config_loaded", "run_startup_healthchecks", "_refresh_config", "_reap_cleanup_futures",
                   "_dispatch_available", "_queue_container_cleanups", "_record_dispatcher_heartbeat", "close"):
        setattr(loop, method, lambda *_a, **_kw: None)

    def finish():
        loop._finish_task_run("old-project", "old-run", "failed")
        queued.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        loop.cleanup_executor = executor
        thread = threading.Thread(target=finish, daemon=True)
        thread.start()
        try:
            assert queued.wait(.5), "finish blocked the scheduler before control observation"
            loop._flush_pending_task_run_finishes()
            assert entered.wait(1)
            loop.run(once=True)
            assert cancelled.is_set(), "an already-entered probe blocked a later pause"
        finally:
            release.set()
            thread.join(3)


def test_startup_reports_periodic_starting_while_selfcheck_is_running(startup_loop):
    import threading

    loop = startup_loop
    loop._sleep_interval = lambda: .02
    periodic = threading.Event()
    observed = []

    def heartbeat(**kw):
        observed.append(kw["status"])
        if observed.count("starting") >= 3:
            periodic.set()
        return ApiResult(status_code=200, data={"dispatcher_id": "dispatcher-test"})

    loop.client.dispatcher_heartbeat.side_effect = heartbeat
    loop._run_startup_healthchecks = lambda **_kw: periodic.wait(1)
    loop.run_startup_healthchecks()
    assert periodic.is_set(), "startup heartbeat went stale during the self-check"
    assert observed[-1] == "idle"


def test_startup_heartbeat_precedes_healthcheck_and_failure_is_safe(startup_loop):
    loop = startup_loop
    observed = []

    def heartbeat(**kw):
        observed.append((kw, dict(loop._startup_check)))
        return ApiResult(status_code=200, data={"dispatcher_id": "dispatcher-test"})

    loop.client.dispatcher_heartbeat.side_effect = heartbeat

    def check(**_kw):
        assert observed[0][0]["status"] == "starting"
        raise RuntimeError("secret-canary /host/private/path")

    loop._run_startup_healthchecks = check
    with pytest.raises(RuntimeError):
        loop.run_startup_healthchecks()
    assert observed[-1][0]["status"] == "error"
    assert observed[-1][1]["code"] == "startup_healthcheck_failed"
    assert "secret-canary" not in str(observed)
    assert observed[0][0]["loop_finished_at"] is None
    assert observed[0][0]["loop_duration_ms"] is None


@pytest.mark.parametrize("reason, expected", [
    ("model_disabled", "model_disabled"), ("credential_missing", "credential_missing"),
    ("binding_stale", "binding_stale"), ("secret-canary", "worker_disabled"),
])
def test_binding_unavailability_uses_safe_selection_classification(reason, expected):
    loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
    binding = SimpleNamespace(id="worker", engine="pi", task_types=["bootstrap"],
                              max_running=1, priority=0, enabled=True,
                              currently_available=False, unavailable_reason=reason)
    assert loop._select_worker("p1", "bootstrap", SimpleNamespace(key="s"), [binding]).worker is None
    decision = loop._dispatcher_runtime_summary()["project_decisions"][0]
    assert decision["code"] == expected
    assert "secret-canary" not in str(decision)


def test_heartbeat_payload_is_bounded_and_does_not_refresh_observed_at():
    from pathlib import Path
    import time
    from cairn.dispatcher.protocol.client import ApiResult

    loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
    loop.dispatcher_id = "dispatcher-test"
    loop.started_at = "2026-01-01T00:00:00Z"
    loop._config_path = Path("/synthetic/config")
    loop._config_mtime_ns = None
    loop._config_sha256 = "synthetic-hash"
    loop._last_config_hash = "synthetic-hash"
    loop.client.dispatcher_heartbeat = Mock(return_value=ApiResult(
        status_code=200, data={"dispatcher_id": "dispatcher-test"},
    ))
    summaries = [_summary(f"p{index}") for index in range(300)]
    loop.futures[object()] = SimpleNamespace(project_id="other", worker_name="w")
    loop._dispatch_available(summaries)
    recovery_observed_at = "2026-01-01T00:00:01Z"
    loop._candidate_recovery_observations = {
        f"capture-{index}": {
            "project_id": f"p{index}",
            "task_run_id": f"run-{index}",
            "status": "unknown",
            "reason": "docker_daemon_unavailable",
            "observed_at": recovery_observed_at,
        }
        for index in range(300)
    }
    summary = loop._dispatcher_runtime_summary()
    original = [dict(item) for item in summary["project_decisions"]]
    original_recovery = [dict(item) for item in summary["candidate_recovery"]]
    for _ in range(2):
        loop._record_dispatcher_heartbeat(status="idle", loop_started_at="2026-01-01T00:00:00Z",
                                          loop_started=time.perf_counter(), summaries=summaries)
        payload = loop.client.dispatcher_heartbeat.call_args.kwargs
        assert payload["runtime"]["project_decisions"] == original
        assert payload["runtime"]["candidate_recovery"] == original_recovery
        assert len(original) == 256
        assert len(original_recovery) == 256
        assert all(
            item["observed_at"] == recovery_observed_at
            for item in original_recovery
        )
        assert all(set(item) == {"project_id", "execution_generation", "observed_at", "code"} for item in original)


@pytest.mark.parametrize("kind, expected", [("busy", "worker_capacity"), ("cooldown", "worker_cooldown"), ("engine", "engine_unsupported")])
def test_existing_selection_blocked_categories_are_observed(kind, expected):
    import time
    loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
    binding = SimpleNamespace(id="worker", engine="pi", task_types=["bootstrap"],
                              max_running=1, priority=0, enabled=True, currently_available=True)
    if kind == "busy":
        loop.futures[object()] = SimpleNamespace(worker_name="worker")
    elif kind == "cooldown":
        loop.worker_cooldown_until["worker"] = (time.time() + 120, "provider_auth")
    else:
        binding.engine = None
    selection = loop._select_worker("p1", "bootstrap", SimpleNamespace(key="s"), [binding])
    assert selection.worker is None
    decision = loop._dispatcher_runtime_summary()["project_decisions"][0]
    assert decision["code"] == expected
    assert ("retry_at" in decision) is (kind == "cooldown")
