"""Tenant Worker durable binding 的 fail-closed 选择测试。"""

from __future__ import annotations

from types import MethodType, SimpleNamespace

from cairn.dispatcher.config import (
    BootstrapTaskConfig,
    ContainerConfig,
    DispatchConfig,
    ExploreTaskConfig,
    ReasonTaskConfig,
    RuntimeConfig,
    TasksConfig,
)
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server.models import ProjectSummary, Scenario


def _loop() -> DispatcherLoop:
    loop = object.__new__(DispatcherLoop)
    loop._log_state = {}
    loop.futures = {}
    loop.worker_cooldown_until = {}
    loop.worker_rejected_until = {}
    loop.config = DispatchConfig(
        server="http://127.0.0.1:8000",
        runtime=RuntimeConfig(
            max_workers=4,
            max_running_projects=1,
            max_project_workers=4,
            interval=1,
            healthcheck_timeout=1,
            prompt_group="default",
        ),
        tasks=TasksConfig(
            bootstrap=BootstrapTaskConfig(timeout=1, conclude_timeout=1),
            reason=ReasonTaskConfig(timeout=1, max_intents=1),
            explore=ExploreTaskConfig(timeout=1, conclude_timeout=1),
        ),
        container=ContainerConfig(
            image="cairn/default:latest",
            network_mode="none",
            completed_action="stop",
            cap_add=["NET_RAW"],
        ),
        workers=[],
    )
    return loop


def _scenario() -> Scenario:
    return Scenario(key="default", label="Default", prompt_group="default", knowledge="knowledge_base")


def _binding(
    worker_id: str,
    priority: int,
    *,
    available: bool = True,
    task_types: list[str] | None = None,
):
    return SimpleNamespace(
        id=worker_id,
        engine="pi",
        task_types=task_types or ["reason"],
        max_running=1,
        priority=priority,
        enabled=True,
        currently_available=available,
    )


def test_empty_static_pool_reaches_durable_project_dispatch() -> None:
    loop = _loop()
    loop.project_cursor = 0
    loop.runtime_project_ids = set()
    loop.created_containers = set()
    loop._cleanup_pending = set()
    attempted: list[str] = []

    def fake_dispatch(self, summary):
        attempted.append(summary.id)
        return False

    loop._try_dispatch_project = MethodType(fake_dispatch, loop)
    loop._dispatch_available(
        [
            ProjectSummary(
                id="p1",
                title="Project",
                status="active",
                created_at="2026-08-14T00:00:00Z",
                scenario_key="default",
                fact_count=2,
                intent_count=0,
                working_intent_count=0,
                unclaimed_intent_count=0,
                hint_count=0,
            )
        ]
    )

    assert attempted == ["p1"]


def test_bound_workers_select_by_stable_id_and_priority() -> None:
    loop = _loop()
    selection = loop._select_worker(
        "p1",
        "reason",
        _scenario(),
        project_workers=[_binding("twr_1", 1), _binding("twr_2", 2)],
    )
    assert selection.worker is not None
    assert selection.worker.id == "twr_1"
    assert selection.worker.name == "twr_1"


def test_verifier_prefers_worker_other_than_discovery_worker() -> None:
    loop = _loop()
    loop.client = SimpleNamespace(
        get_observation_verifier_target=lambda project_id, observation_id: SimpleNamespace(
            discovery_worker="twr_discovery"
        )
    )
    intent = SimpleNamespace(
        id="i-verifier",
        assessment_role="verifier",
        target_observation_id="vo1",
        worker_history=[],
    )

    tried = loop._explore_tried_workers("p1", intent)
    selection = loop._select_worker(
        "p1",
        "explore",
        _scenario(),
        project_workers=[
            _binding("twr_discovery", 1, task_types=["explore"]),
            _binding("twr_independent", 1, task_types=["explore"]),
        ],
        tried_workers=tried,
    )

    assert tried == {"twr_discovery"}
    assert selection.worker is not None
    assert selection.worker.name == "twr_independent"


def test_single_worker_verifier_falls_back_to_discovery_worker() -> None:
    loop = _loop()
    loop.client = SimpleNamespace(
        get_observation_verifier_target=lambda project_id, observation_id: SimpleNamespace(
            discovery_worker="twr_only"
        )
    )
    intent = SimpleNamespace(
        id="i-verifier",
        assessment_role="verifier",
        target_observation_id="vo1",
        worker_history=[],
    )

    selection = loop._select_worker(
        "p1",
        "explore",
        _scenario(),
        project_workers=[_binding("twr_only", 1, task_types=["explore"])],
        tried_workers=loop._explore_tried_workers("p1", intent),
    )

    assert selection.worker is not None
    assert selection.worker.name == "twr_only"


def test_empty_binding_fails_closed_without_global_fallback() -> None:
    loop = _loop()
    selection = loop._select_worker("p1", "reason", _scenario(), project_workers=[])
    assert selection.worker is None
    assert selection.candidate_order == []


def test_unavailable_binding_is_not_a_candidate() -> None:
    loop = _loop()
    selection = loop._select_worker(
        "p1",
        "reason",
        _scenario(),
        project_workers=[_binding("twr_disabled", 1, available=False)],
    )
    assert selection.worker is None
    assert [item["id"] for item in selection.blocked_disabled] == ["twr_disabled"]
