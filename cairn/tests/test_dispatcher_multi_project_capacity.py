from __future__ import annotations

from types import MethodType, SimpleNamespace

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
from cairn.dispatcher.models import ReasonCheckpoint
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.scheduler.common import PendingTaskRunFinish
from cairn.dispatcher.scheduler.dispatch_cycle import DispatchCycleMixin
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server.models import ProjectSummary


def _summary(project_id: str, *, status: str = "active") -> ProjectSummary:
    return ProjectSummary(
        id=project_id,
        title=project_id,
        status=status,
        created_at="2026-01-01T00:00:00Z",
        scenario_key="default",
        fact_count=2,
        intent_count=0,
        working_intent_count=0,
        unclaimed_intent_count=0,
        hint_count=0,
    )


def _execution_view(**overrides) -> dict:
    return {
        "version": 2, "executable": True, "execution_contract_version": 1,
        "execution_engine": "pi", "enabled": False, "phase": "baseline",
        **overrides,
    }


def _loop(
    *,
    max_workers: int,
    max_running_projects: int,
    worker_count: int,
    workers_enabled: bool = True,
) -> DispatcherLoop:
    loop = object.__new__(DispatcherLoop)
    loop._log_state = {}
    loop.project_cursor = 0
    loop.runtime_project_ids = set()
    loop.futures = {}
    loop.report_finish_outbox = SimpleNamespace(
        entries=lambda: [],
        has_project=lambda _project_id: False,
    )
    loop.candidate_capture_outbox = object()
    loop.created_containers = set()
    loop.worker_cooldown_until = {}
    loop.worker_rejected_until = {}
    loop._inactive_cleanup_done = {}
    loop._cleanup_pending = set()
    loop.container_manager = SimpleNamespace(
        container_name=lambda project_id: f"container-{project_id}"
    )
    loop._probe_worker_readiness = MethodType(
        lambda self, project_id, task_type, worker, container_runtime, generation: True,
        loop,
    )
    loop.client = SimpleNamespace(
        get_assessment_report_finalization=lambda project_id: ApiResult(
            status_code=404,
            data=None,
        )
    )
    loop.config = DispatchConfig(
        server="http://127.0.0.1:8000",
        runtime=RuntimeConfig(
            max_workers=max_workers,
            max_running_projects=max_running_projects,
            max_project_workers=1,
            interval=1,
            healthcheck_timeout=1,
            prompt_group="default-copy-v2",
        ),
        tasks=TasksConfig(
            bootstrap=BootstrapTaskConfig(timeout=1, conclude_timeout=1),
            reason=ReasonTaskConfig(timeout=1, max_intents=1),
            explore=ExploreTaskConfig(timeout=1, conclude_timeout=1),
        ),
        container=ContainerConfig(
            image="test:latest",
            network_mode="none",
            completed_action="stop",
        ),
        workers=[
            WorkerConfig(
                id=f"worker-{index + 1}",
                engine="pi",
                task_types=["bootstrap", "reason", "explore"],
                concurrency=1,
                priority=index,
                enabled=workers_enabled,
            )
            for index in range(worker_count)
        ],
    )

    def fake_dispatch(self, summary):
        running = self._worker_counts()
        worker = next(
            (
                item
                for item in self.config.workers
                if running.get(item.name, 0) < (item.max_running or 0)
            ),
            None,
        )
        if worker is None or self._project_running_task_count(summary.id) >= self.config.runtime.max_project_workers:
            return False
        key = object()
        self.futures[key] = SimpleNamespace(
            project_id=summary.id,
            worker_name=worker.name,
            task_type="bootstrap",
            intent_id=None,
        )
        self.runtime_project_ids.add(summary.id)
        self.created_containers.add(f"cairn-dispatch-{summary.id}")
        return True

    loop._try_dispatch_project = MethodType(fake_dispatch, loop)
    return loop


def test_no_static_workers_still_attempts_project_dispatch() -> None:
    loop = _loop(max_workers=2, max_running_projects=2, worker_count=0)
    attempted: list[str] = []
    loop._try_dispatch_project = lambda summary: attempted.append(summary.id) or False

    loop._dispatch_available([_summary("p1")])

    assert attempted == ["p1"]
    assert loop.futures == {}
    assert loop.runtime_project_ids == set()


def test_disabled_static_workers_still_attempt_project_dispatch() -> None:
    loop = _loop(
        max_workers=2,
        max_running_projects=2,
        worker_count=1,
        workers_enabled=False,
    )
    attempted: list[str] = []
    loop._try_dispatch_project = lambda summary: attempted.append(summary.id) or False

    loop._dispatch_available([_summary("p1")])

    assert attempted == ["p1"]
    assert loop.futures == {}
    assert loop.runtime_project_ids == set()


def test_max_running_projects_one_keeps_second_project_waiting() -> None:
    loop = _loop(max_workers=2, max_running_projects=1, worker_count=2)
    loop._dispatch_available([_summary("p1"), _summary("p2")])
    assert loop.runtime_project_ids == {"p1"}
    assert {task.project_id for task in loop.futures.values()} == {"p1"}


def test_two_project_admission_and_two_worker_slots_create_distinct_containers() -> None:
    loop = _loop(max_workers=2, max_running_projects=2, worker_count=2)
    loop._dispatch_available([_summary("p1"), _summary("p2")])
    assert loop.runtime_project_ids == {"p1", "p2"}
    assert {task.project_id for task in loop.futures.values()} == {"p1", "p2"}
    assert loop.created_containers == {"cairn-dispatch-p1", "cairn-dispatch-p2"}


def test_single_busy_worker_limits_second_project() -> None:
    loop = _loop(max_workers=2, max_running_projects=2, worker_count=1)
    loop._dispatch_available([_summary("p1"), _summary("p2")])
    assert len(loop.futures) == 1
    assert len(loop.runtime_project_ids) == 1


def test_concluded_intent_does_not_starve_open_intent() -> None:
    loop = _loop(max_workers=2, max_running_projects=1, worker_count=1)
    loop._cleanup_pending = set()
    loop.container_manager = SimpleNamespace(
        container_name=lambda project_id: f"container-{project_id}"
    )
    open_intent = SimpleNamespace(
        id="i-open",
        to=None,
        worker=None,
        concluded_at=None,
        depends_on=None,
        description="open",
        assessment_role="discovery",
        depth_review_fact_id=None,
        creator="reason",
        from_=["origin"],
        created_at="2026-01-01T00:00:00Z",
    )
    concluded_intent = SimpleNamespace(
        id="i-concluded",
        to=None,
        worker=None,
        concluded_at="2026-01-02T00:00:00Z",
        depends_on=None,
        description="abandoned",
        creator="coverage_convergence",
        from_=["origin"],
        created_at="2026-01-02T00:00:00Z",
    )
    project = SimpleNamespace(
        project=SimpleNamespace(
            id="p1",
            status="active",
            reason=None,
            assessment_protocol="",
            assessment_execution=_execution_view(),
            authorization_block_reason=None,
        ),
        facts=[SimpleNamespace(id="origin"), SimpleNamespace(id="goal"), SimpleNamespace(id="f1")],
        intents=[open_intent, concluded_intent],
    )
    loop.client = SimpleNamespace(
        get_project=lambda project_id: project,
        export_project=lambda project_id: "project: p1",
    )
    dispatched: list[str] = []
    loop._dispatch_explore = (
        lambda project, export_yaml, intent: dispatched.append(intent.id) or True
    )

    result = DispatchCycleMixin._try_dispatch_project(loop, _summary("p1"))

    assert result is True
    assert dispatched == ["i-open"]


def test_terminal_bootstrap_intent_is_not_reused() -> None:
    loop = _loop(max_workers=2, max_running_projects=1, worker_count=1)
    terminal = SimpleNamespace(
        id="i-bootstrap-old",
        description="bootstrap",
        creator="dispatcher.bootstrap",
        from_=["origin"],
        to=None,
        concluded_at="2026-01-02T00:00:00Z",
        worker=None,
        created_at="2026-01-01T00:00:00Z",
    )
    project = SimpleNamespace(
        project=SimpleNamespace(id="p1"),
        intents=[terminal],
    )

    assert DispatchCycleMixin._get_bootstrap_intent(loop, project) is None


def test_open_intent_count_excludes_completed_and_abandoned_intents() -> None:
    loop = _loop(max_workers=2, max_running_projects=1, worker_count=1)
    intents = [
        SimpleNamespace(to=None, concluded_at=None, worker=None),
        SimpleNamespace(to=None, concluded_at=None, worker="worker-1"),
        SimpleNamespace(to="f1", concluded_at="2026-01-02T00:00:00Z", worker=None),
        SimpleNamespace(to=None, concluded_at="2026-01-02T00:00:00Z", worker=None),
    ]
    project = SimpleNamespace(intents=intents)

    assert DispatchCycleMixin._project_open_intent_count(loop, project) == 2


def test_abandoned_intents_do_not_hide_open_intents_transition() -> None:
    loop = _loop(max_workers=2, max_running_projects=1, worker_count=1)
    loop.reason_checkpoints = {
        "p1": ReasonCheckpoint(
            fact_count=3,
            hint_count=0,
            open_intent_count=1,
        )
    }
    project = SimpleNamespace(
        project=SimpleNamespace(
            id="p1",
            execution_generation=0,
            assessment_protocol="",
            assessment_execution=_execution_view(),
        ),
        facts=[SimpleNamespace(), SimpleNamespace(), SimpleNamespace()],
        hints=[],
        intents=[
            SimpleNamespace(
                to=None,
                concluded_at="2026-01-02T00:00:00Z",
            )
        ],
    )

    assert DispatchCycleMixin._reason_trigger(loop, project) == "open_intents:1->0"


def test_completed_report_dispatches_only_claimable_finalization() -> None:
    loop = _loop(max_workers=3, max_running_projects=1, worker_count=1)
    states = {
        "pending": ApiResult(
            status_code=200,
            data={"status": "pending", "requested_at": "2026-08-29T00:00:00Z"},
        ),
        "failed": ApiResult(
            status_code=200,
            data={"status": "failed", "requested_at": "2026-08-29T00:00:00Z"},
        ),
        "ready": ApiResult(
            status_code=200,
            data={"status": "ready", "requested_at": "2026-08-29T00:00:00Z"},
        ),
        "running": ApiResult(
            status_code=200,
            data={
                "status": "running",
                "requested_at": "2026-08-29T00:00:00Z",
                "lease_expires_at": "2099-01-01T00:00:00Z",
            },
        ),
        "legacy": ApiResult(status_code=404, data=None),
    }
    loop.client = SimpleNamespace(
        get_assessment_report_finalization=lambda project_id: states[project_id]
    )
    dispatched: list[str] = []
    loop._try_dispatch_completed_report = (
        lambda project_id: dispatched.append(project_id) or True
    )

    loop._dispatch_completed_reports(
        [_summary(project_id, status="completed") for project_id in states]
    )

    assert set(dispatched) == {"pending", "failed"}


def test_stopped_report_dispatch_waits_for_ordinary_future_and_cleanup() -> None:
    loop = _loop(max_workers=3, max_running_projects=1, worker_count=1)
    loop.client = SimpleNamespace(
        get_assessment_report_finalization=lambda project_id: ApiResult(
            status_code=200,
            data={"status": "pending", "requested_at": "2026-08-29T00:00:00Z"},
        )
    )
    loop.container_manager = SimpleNamespace(
        container_name=lambda project_id: f"container-{project_id}"
    )
    dispatched: list[str] = []
    loop._try_dispatch_completed_report = (
        lambda project_id: dispatched.append(project_id) or True
    )
    ordinary_future = object()
    loop.futures[ordinary_future] = SimpleNamespace(
        project_id="p1",
        worker_name="worker-1",
        task_type="explore",
        intent_id="intent-1",
    )

    loop._dispatch_completed_reports([_summary("p1", status="stopped")])
    assert dispatched == []

    loop.futures.pop(ordinary_future)
    loop._cleanup_pending.add("container-p1")
    loop._dispatch_completed_reports([_summary("p1", status="stopped")])
    assert dispatched == []

    loop._cleanup_pending.clear()
    loop._dispatch_completed_reports([_summary("p1", status="stopped")])
    assert dispatched == ["p1"]


def test_completed_report_skips_failed_finalization_before_retry_time() -> None:
    loop = _loop(max_workers=2, max_running_projects=1, worker_count=1)
    loop.client = SimpleNamespace(
        get_assessment_report_finalization=lambda project_id: ApiResult(
            status_code=200,
            data={
                "status": "failed",
                "requested_at": "2026-08-29T00:00:00Z",
                "next_attempt_at": "2099-01-01T00:00:00Z",
            },
        )
    )
    dispatched: list[str] = []
    loop._try_dispatch_completed_report = (
        lambda project_id: dispatched.append(project_id) or True
    )

    loop._dispatch_completed_reports([_summary("p1", status="completed")])

    assert dispatched == []


def test_completed_report_dispatches_failed_finalization_when_retry_is_due() -> None:
    loop = _loop(max_workers=2, max_running_projects=1, worker_count=1)
    loop.client = SimpleNamespace(
        get_assessment_report_finalization=lambda project_id: ApiResult(
            status_code=200,
            data={
                "status": "failed",
                "requested_at": "2026-08-29T00:00:00Z",
                "next_attempt_at": "2000-01-01T00:00:00Z",
            },
        )
    )
    dispatched: list[str] = []
    loop._try_dispatch_completed_report = (
        lambda project_id: dispatched.append(project_id) or True
    )

    loop._dispatch_completed_reports([_summary("p1", status="completed")])

    assert dispatched == ["p1"]


@pytest.mark.parametrize(
    ("lease_expires_at", "expected"),
    [
        ("2099-01-01T00:00:00Z", []),
        ("2000-01-01T00:00:00Z", ["p1"]),
    ],
)
def test_completed_report_reclaims_only_expired_running_lease(
    lease_expires_at: str,
    expected: list[str],
) -> None:
    loop = _loop(max_workers=2, max_running_projects=1, worker_count=1)
    loop.client = SimpleNamespace(
        get_assessment_report_finalization=lambda project_id: ApiResult(
            status_code=200,
            data={
                "status": "running",
                "requested_at": "2026-08-29T00:00:00Z",
                "lease_expires_at": lease_expires_at,
            },
        )
    )
    dispatched: list[str] = []
    loop._try_dispatch_completed_report = (
        lambda project_id: dispatched.append(project_id) or True
    )

    loop._dispatch_completed_reports([_summary("p1", status="completed")])

    assert dispatched == expected


def test_completed_report_future_blocks_duplicate_dispatch() -> None:
    loop = _loop(max_workers=2, max_running_projects=1, worker_count=1)
    loop.futures[object()] = SimpleNamespace(
        project_id="p1",
        worker_name="worker-1",
        task_type="report",
        intent_id=None,
    )
    loop.client = SimpleNamespace(
        get_assessment_report_finalization=lambda project_id: ApiResult(
            status_code=200,
            data={"status": "pending"},
        )
    )
    dispatched: list[str] = []
    loop._try_dispatch_completed_report = (
        lambda project_id: dispatched.append(project_id) or True
    )

    loop._dispatch_completed_reports([_summary("p1", status="completed")])

    assert dispatched == []


@pytest.mark.parametrize(
    ("selected_capability", "expected_selection_calls"),
    (
        ("reason", ["reason"]),
        ("bootstrap", ["reason", "bootstrap"]),
    ),
)
def test_completed_report_materializes_report_execution_purpose(
    selected_capability: str,
    expected_selection_calls: list[str],
) -> None:
    loop = _loop(max_workers=1, max_running_projects=1, worker_count=1)
    worker = loop.config.workers[0]
    project_workers = [
        SimpleNamespace(
            id=worker.id,
            task_types=[selected_capability],
            max_running=1,
            priority=0,
            enabled=True,
            currently_available=True,
        )
    ]
    project = SimpleNamespace(
        project=SimpleNamespace(
            id="p1", tenant_workers=project_workers, assessment_execution=_execution_view(),
            authorization_block_reason="project_authorization_expired",
        )
    )
    scenario = SimpleNamespace(
        key="report-test",
        prompt_group="default-copy-v2",
        knowledge=None,
    )
    selection_calls: list[str] = []
    materialization_calls: list[str] = []

    def select_worker(project_id, task_type, selected_scenario, bindings):
        selection_calls.append(task_type)
        available = task_type == selected_capability
        return SimpleNamespace(
            worker=worker if available else None,
            candidate_order=[{"id": worker.id}] if available else [],
        )

    def materialize(project_id, task_type, selection, bindings):
        materialization_calls.append(task_type)
        return worker, {}, None

    loop.client = SimpleNamespace(
        get_project=lambda project_id: project,
        export_project=lambda project_id: "project: p1\n",
        get_assessment_report_finalization=lambda project_id: ApiResult(
            status_code=200,
            data={"project_id": project_id, "status": "pending", "delivery_retry_route": None},
        ),
    )
    loop._scenario_for_project = lambda value: scenario
    loop._container_runtime_options = lambda value, project_id: SimpleNamespace()
    loop._select_worker = select_worker
    loop._materialize_selected_worker = materialize
    loop.container_manager = SimpleNamespace()
    submissions: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def submit(*args, **kwargs):
        submissions.append((args, kwargs))
        return object()

    loop.executor = SimpleNamespace(submit=submit)

    assert loop._try_dispatch_completed_report("p1") is True
    assert selection_calls == expected_selection_calls
    assert materialization_calls == ["report"]
    assert submissions[0][0][5] == "project: p1\n"
    assert next(iter(loop.futures.values())).task_type == "report"


def test_completed_report_rejects_empty_export_before_worker_selection() -> None:
    loop = _loop(max_workers=1, max_running_projects=1, worker_count=1)
    project = SimpleNamespace(
        project=SimpleNamespace(
            id="p1",
            assessment_execution=_execution_view(),
        )
    )
    loop.client = SimpleNamespace(
        get_project=lambda project_id: project,
        export_project=lambda project_id: " \n",
    )
    loop._select_worker = lambda *args, **kwargs: pytest.fail(
        "空 graph 不得进入 Worker 选择"
    )

    assert loop._try_dispatch_completed_report("p1") is False
    assert loop.futures == {}
    decision = loop._dispatcher_runtime_summary()["project_decisions"][-1]
    assert decision["code"] == "admission_failed"
    assert decision["task_type"] == "report"


@pytest.mark.parametrize("task_type", ("report", "reason", "bootstrap"))
def test_active_completion_future_preserves_container_and_is_not_cancelled(
    task_type: str,
) -> None:
    loop = _loop(max_workers=2, max_running_projects=1, worker_count=1)
    cancelled: list[str] = []
    task = SimpleNamespace(
        project_id="p1",
        worker_name="worker-1",
        task_type=task_type,
        intent_id=None,
        cancellation=SimpleNamespace(
            cancel=lambda status: cancelled.append(status) or True
        ),
    )
    loop.futures[object()] = task
    loop._cleanup_pending = set()
    cleanup_calls: list[str] = []
    loop.container_manager = SimpleNamespace(
        container_name=lambda project_id: f"container-{project_id}",
        needs_completed_cleanup=lambda project_id: True,
        cleanup_completed=lambda project_id: cleanup_calls.append(project_id),
    )
    loop.cleanup_executor = SimpleNamespace(
        submit=lambda function, project_id: function(project_id)
    )

    summaries = [_summary("p1", status="completed")]
    loop._cancel_inactive_tasks(summaries)
    loop._cleanup_completed_containers(summaries)

    assert cancelled == []
    assert cleanup_calls == []


def test_stopped_report_future_preserves_container_and_is_not_cancelled() -> None:
    loop = _loop(max_workers=2, max_running_projects=1, worker_count=1)
    cancelled: list[str] = []
    loop.futures[object()] = SimpleNamespace(
        project_id="p1",
        worker_name="worker-1",
        task_type="report",
        intent_id=None,
        cancellation=SimpleNamespace(
            cancel=lambda status: cancelled.append(status) or True
        ),
    )
    cleanup_calls: list[str] = []
    loop.container_manager = SimpleNamespace(
        container_name=lambda project_id: f"container-{project_id}",
        needs_stopped_cleanup=lambda project_id: True,
        cleanup_stopped=lambda project_id: cleanup_calls.append(project_id),
    )

    summaries = [_summary("p1", status="stopped")]
    loop._cancel_inactive_tasks(summaries)
    loop._cleanup_stopped_containers(summaries)

    assert cancelled == []
    assert cleanup_calls == []


@pytest.mark.parametrize("status_code,expected", [
    (0, "retry_pending"),
    (503, "retry_pending"),
    (403, "rejected"),
    (409, "rejected"),
    (422, "rejected"),
    (404, "record_gone"),
])
def test_task_run_finish_uses_explicit_delivery_disposition(status_code, expected) -> None:
    loop = _loop(max_workers=1, max_running_projects=1, worker_count=1)
    loop.client = SimpleNamespace(
        update_task_run=lambda *args, **kwargs: ApiResult(
            status_code=status_code,
            data={"detail": "Terminal task run status cannot change"},
        )
    )
    loop._finish_runtime_context = lambda *args, **kwargs: {}
    finish = PendingTaskRunFinish(
        project_id="p1",
        task_run_id="tr1",
        status="succeeded",
        session_id=None,
        exit_code=0,
        error_type=None,
        error_message=None,
        task_type="explore",
        worker_name="worker-1",
        worker_type="mock",
        container_runtime=None,
    )

    assert loop._send_task_run_finish(finish) == expected


def test_worker_and_admission_slots_release() -> None:
    loop = _loop(max_workers=2, max_running_projects=1, worker_count=1)
    loop._dispatch_available([_summary("p1"), _summary("p2")])
    first_project = next(iter(loop.runtime_project_ids))
    other = "p2" if first_project == "p1" else "p1"

    # 任务结束释放 Worker slot，项目退出 active 同时释放 admission 名额。
    loop.futures.clear()
    summaries = [_summary(first_project, status="stopped"), _summary(other)]
    loop._refresh_runtime_projects(summaries)
    loop._dispatch_available(summaries)
    assert loop.runtime_project_ids == {other}
    assert {task.project_id for task in loop.futures.values()} == {other}
