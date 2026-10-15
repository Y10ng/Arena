"""冻结执行门、复核优先及持久阶段触发，不启动线程或容器。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cairn.dispatcher.models import ReasonCheckpoint
from cairn.dispatcher.scheduler.dispatch_cycle import DispatchCycleMixin
from cairn.server.models import Intent


def execution_view(**changes):
    return {
        "version": 2, "executable": True, "execution_contract_version": 1,
        "execution_engine": "pi", "enabled": True, "phase": "baseline",
        **changes,
    }


def intent(
    identity,
    *,
    role="discovery",
    deep=False,
    created="2026-09-01",
    priority=1,
    **changes,
):
    item = Intent(
        id=identity, **{"from": ["f1"]}, description=identity, creator="reason",
        created_at=created, assessment_role=role,
        depth_review_fact_id="f-review" if deep else None, **changes,
    )
    # priority 字段由 goal-driven 迁移提供；显式赋值也让本测试保持聚焦于调度顺序。
    object.__setattr__(item, "priority", priority)
    return item


def setup_cycle(*intents, view=None, reason=None, protocol="default-copy-v1"):
    project = SimpleNamespace(
        project=SimpleNamespace(
            id="p1", status="active", reason=reason, execution_generation=0,
            authorization_block_reason=None,
            assessment_protocol=protocol,
            assessment_execution=execution_view() if view is None else view,
            coverage_checkpoint_cell_count=1, coverage_stop_reason=None,
        ),
        facts=[SimpleNamespace(id=item) for item in ("origin", "goal", "f1")],
        intents=list(intents), hints=[],
    )
    cycle = DispatchCycleMixin()
    cycle._candidate_project_blocked = lambda _project_id: False
    cycle.futures = {}
    cycle.report_finish_outbox = SimpleNamespace(has_project=lambda _project_id: False)
    cycle._cleanup_pending = set()
    cycle.config = SimpleNamespace(runtime=SimpleNamespace(max_project_workers=4))
    cycle.container_manager = SimpleNamespace(container_name=lambda _: "owned-p1")
    cycle.client = SimpleNamespace(
        get_project=Mock(return_value=project), export_project=Mock(return_value="graph"),
    )
    cycle._log_changed = Mock()
    cycle._dispatch_initial_project = Mock(return_value=True)
    cycle._dispatch_explore = Mock(return_value=True)
    cycle._dispatch_reason = Mock(return_value=True)
    cycle._select_worker = Mock()
    cycle.reason_checkpoints = {"p1": ReasonCheckpoint(
        fact_count=3, hint_count=0, open_intent_count=len(intents),
        coverage_checkpoint_cell_count=1,
    )}
    return cycle, project


def dispatch(cycle):
    return cycle._try_dispatch_project(SimpleNamespace(id="p1"))


@pytest.mark.parametrize("view", [
    {}, execution_view(version=1), execution_view(executable=False),
    execution_view(execution_contract_version=0), execution_view(execution_engine="claudecode"),
])
def test_unsupported_snapshot_is_rejected_before_bootstrap_or_worker_selection(view):
    cycle, project = setup_cycle(view=view)
    project.facts = project.facts[:2]
    assert dispatch(cycle) is False
    assert cycle._try_dispatch_completed_report("p1") is False
    cycle._dispatch_initial_project.assert_not_called()
    cycle._dispatch_explore.assert_not_called()
    cycle._dispatch_reason.assert_not_called()
    cycle._select_worker.assert_not_called()
    cycle.client.export_project.assert_not_called()


@pytest.mark.parametrize("block_reason", [
    "project_authorization_scope_upgrade_required",
    "project_authorization_scope_changed",
    "project_authorization_expired",
    "project_reauthorization_required",
])
def test_fresh_authorization_blocks_admission_each_round_and_clears_without_cache(block_reason):
    from types import MethodType
    from cairn.dispatcher.protocol.client import ApiResult
    from test_dispatcher_multi_project_capacity import _loop, _summary

    _, project = setup_cycle(intent("explore"))
    project.project.authorization_block_reason = block_reason
    project.project.execution_generation = 7
    project.project.tenant_workers = []
    loop = _loop(max_workers=1, max_running_projects=1, worker_count=0)
    loop._try_dispatch_project = MethodType(DispatchCycleMixin._try_dispatch_project, loop)
    loop.client.get_project = Mock(return_value=project)
    loop.client.export_project = Mock(return_value="graph")
    loop.client.list_task_runs = Mock(return_value=ApiResult(status_code=200, data=[]))
    loop.client.start_explore_attempt = Mock(return_value=ApiResult(
        status_code=409, data={"detail": block_reason},
    ))
    worker = SimpleNamespace(id="worker", name="worker", type="pi")
    loop._scenario_for_project = Mock(return_value=SimpleNamespace(key="default", prompt_group="default"))
    loop._container_runtime_options = Mock(return_value={})
    loop._select_worker = Mock(return_value=SimpleNamespace(worker=worker))
    loop._materialize_selected_worker = Mock(return_value=(worker, {}, None))
    loop._probe_worker_readiness = Mock(return_value=True)
    loop._task_run_runtime_context = Mock(return_value={})
    summary = _summary("p1")
    # 摘要故意保留不同代次和空阻断原因，只有本轮详情可决定准入。
    assert summary.execution_generation != project.project.execution_generation
    assert summary.authorization_block_reason is None
    for _ in range(3):
        loop._dispatch_available([summary])
        assert loop._project_decisions["p1"]["code"] == block_reason
        assert loop._project_decisions["p1"]["execution_generation"] == 7
    assert loop.client.get_project.call_count == 3
    loop.client.export_project.assert_not_called()
    loop.client.list_task_runs.assert_not_called()
    loop._select_worker.assert_not_called()
    loop._probe_worker_readiness.assert_not_called()
    loop.client.start_explore_attempt.assert_not_called()

    project.project.authorization_block_reason = None
    loop._dispatch_available([summary])
    loop.client.start_explore_attempt.assert_called_once()
    assert loop._project_decisions["p1"]["code"] == block_reason
    # 详情与准入之间的 409 只供观测；下一轮仍读取 Server 并重新评估。
    loop.client.start_explore_attempt.return_value = ApiResult(status_code=503)
    loop._dispatch_available([summary])
    assert loop.client.get_project.call_count == 5
    assert loop.client.start_explore_attempt.call_count == 2
    assert loop._project_decisions["p1"]["code"] == "admission_failed"
    assert loop.futures == {}


@pytest.mark.parametrize("phase", ["baseline", "depth_review", "deep", "finalizing"])
def test_oldest_verifier_precedes_discovery_deep_and_reason(phase):
    cycle, _ = setup_cycle(
        intent("discovery", created="2026-09-16"),
        intent("deep", deep=True, created="2026-09-15"),
        intent("verifier-new", role="verifier", created="2026-09-14"),
        intent("verifier-old", role="verifier", created="2026-09-13"),
        view=execution_view(phase=phase),
    )
    assert dispatch(cycle) is True
    assert cycle._dispatch_explore.call_args.args[2].id == "verifier-old"
    cycle._dispatch_reason.assert_not_called()


def test_selected_deep_precedes_newer_discovery():
    cycle, _ = setup_cycle(
        intent("discovery", created="2026-09-16"),
        intent("selected", deep=True, created="2026-09-12"),
        view=execution_view(phase="deep"),
    )
    assert dispatch(cycle) is True
    assert cycle._dispatch_explore.call_args.args[2].id == "selected"


def test_goal_driven_verifier_precedes_higher_priority_discovery():
    cycle, _ = setup_cycle(
        intent("discovery", priority=2, created="2026-09-12"),
        intent("verifier", role="verifier", priority=0, created="2026-09-16"),
        view=execution_view(goal_driven=True, phase=None),
        protocol="goal-driven-v1",
    )

    assert dispatch(cycle) is True
    assert cycle._dispatch_explore.call_args.args[2].id == "verifier"


def test_goal_driven_discovery_uses_priority_then_fifo():
    cycle, _ = setup_cycle(
        intent("low", priority=0, created="2026-09-10"),
        intent("high-new", priority=2, created="2026-09-12"),
        intent("high-old-b", priority=2, created="2026-09-11"),
        intent("high-old-a", priority=2, created="2026-09-11"),
        view=execution_view(goal_driven=True, phase=None),
        protocol="goal-driven-v1",
    )

    assert dispatch(cycle) is True
    assert cycle._dispatch_explore.call_args.args[2].id == "high-old-a"


def test_goal_driven_dependency_blocks_until_predecessor_is_concluded():
    blocked = intent("blocked", priority=2, depends_on="predecessor")
    predecessor = intent("predecessor", priority=0, created="2026-09-10")
    ready = intent("ready", priority=1, created="2026-09-12")
    cycle, _ = setup_cycle(
        blocked,
        predecessor,
        ready,
        view=execution_view(goal_driven=True, phase=None),
        protocol="goal-driven-v1",
    )

    assert dispatch(cycle) is True
    assert cycle._dispatch_explore.call_args.args[2].id == "ready"

    concluded = intent(
        "predecessor",
        priority=0,
        created="2026-09-10",
        concluded_at="2026-09-13",
        conclusion="completed",
    )
    cycle, _ = setup_cycle(
        blocked,
        concluded,
        view=execution_view(goal_driven=True, phase=None),
        protocol="goal-driven-v1",
    )
    assert dispatch(cycle) is True
    assert cycle._dispatch_explore.call_args.args[2].id == "blocked"


def test_goal_driven_never_routes_to_depth_review():
    cycle, _ = setup_cycle(
        intent("ordinary", deep=True),
        view=execution_view(goal_driven=True, phase="depth_review"),
        protocol="goal-driven-v1",
    )

    assert dispatch(cycle) is True
    assert cycle._dispatch_explore.call_args.args[2].id == "ordinary"
    cycle._dispatch_reason.assert_not_called()


@pytest.mark.parametrize("phase", ["depth_review", "finalizing"])
def test_durable_phase_triggers_reason_without_new_facts(phase):
    cycle, project = setup_cycle(view=execution_view(phase=phase))
    assert cycle._reason_trigger(project) == phase
    assert dispatch(cycle) is True
    assert cycle._dispatch_reason.call_args.args[2] == phase


def test_all_failed_reason_batch_triggers_next_reason_without_graph_changes():
    cycle, project = setup_cycle(
        intent("failed-a", concluded_at="2026-09-19T01:00:00Z"),
        intent("failed-b", concluded_at="2026-09-19T01:00:01Z"),
        intent("failed-c", concluded_at="2026-09-19T01:00:02Z"),
    )
    cycle.reason_checkpoints["p1"] = ReasonCheckpoint(
        fact_count=3,
        hint_count=0,
        open_intent_count=0,
        coverage_checkpoint_cell_count=1,
        settled_intent_count=0,
        execution_generation=0,
    )

    assert cycle._reason_trigger(project) == "settled_intents:0->3"


def test_execution_generation_change_invalidates_reason_checkpoint():
    cycle, project = setup_cycle()
    project.project.execution_generation = 2
    cycle.reason_checkpoints["p1"] = ReasonCheckpoint(
        fact_count=3,
        hint_count=0,
        open_intent_count=0,
        coverage_checkpoint_cell_count=1,
        settled_intent_count=0,
        execution_generation=1,
    )

    assert cycle._reason_trigger(project) == "execution_generation:1->2"


def test_goal_driven_reason_ignores_historical_coverage_checkpoint_changes():
    cycle, project = setup_cycle(
        view=execution_view(goal_driven=True, phase="finalizing"),
        protocol="goal-driven-v1",
    )
    project.project.coverage_checkpoint_cell_count = 9
    project.project.coverage_stop_reason = "legacy-stop"

    assert cycle._reason_trigger(project) is None


def test_depth_review_lease_prevents_duplicate_reason():
    cycle, _ = setup_cycle(view=execution_view(phase="depth_review"), reason=SimpleNamespace(worker="other"))
    assert dispatch(cycle) is False
    cycle._dispatch_reason.assert_not_called()


def test_runtime_retry_admission_stays_with_server_beyond_legacy_attempt_count():
    cycle, _ = setup_cycle(
        intent("retry"), view=execution_view(attempts_started=47),
    )
    assert dispatch(cycle) is True
    assert cycle._dispatch_explore.call_args.args[2].id == "retry"


def test_closed_or_locally_running_verifier_does_not_starve_ready_work():
    cycle, _ = setup_cycle(
        intent("closed", role="verifier", concluded_at="2026-09-16"),
        intent("running", role="verifier"), intent("ready", deep=True),
    )
    cycle.futures[object()] = SimpleNamespace(project_id="p1", task_type="explore", intent_id="running")
    assert dispatch(cycle) is True
    assert cycle._dispatch_explore.call_args.args[2].id == "ready"
