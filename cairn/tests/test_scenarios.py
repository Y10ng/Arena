from __future__ import annotations

from contextlib import contextmanager
from json import dumps
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

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
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server import db
from cairn.server.models import CreateProjectRequest, Scenario
from cairn.server.services.assessment_policy import policy_for_scenario, scenario_snapshot
ROOT = Path(__file__).resolve().parents[2]


from cairn.server.services import list_scenarios, replace_scenarios, resolve_scenario_key
from cairn.server.services.scenarios import validate_scenario_features


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    db.run_migrations(conn, "sqlite", initialize_runtime=False)
    return conn


def insert_dispatcher_status(
    conn: sqlite3.Connection,
    *,
    worker_names: list[str] | None = None,
    images: list[str] | None = None,
    caps: list[str] | None = None,
) -> None:
    names = worker_names or ["mock_worker"]
    conn.execute(
        """
        INSERT INTO dispatcher_status
            (id, status, worker_pool_json, container_images_json, cap_baseline_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "dispatcher-test",
            "running",
            dumps([
                {
                    "worker_name": name,
                    "worker_type": "mock",
                    "task_types": ["bootstrap", "reason", "explore"],
                    "priority": 0,
                    "max_running": 1,
                    "running_count": 0,
                }
                for name in names
            ]),
            dumps(images or []),
            dumps(caps) if caps is not None else None,
            "2026-01-01T00:00:00Z",
        ),
    )


def insert_scenario_row(conn: sqlite3.Connection, *, key: str, prompt_group: str) -> None:
    conn.execute(
        """
        INSERT INTO scenarios
            (key, label, is_default, profile_id, worker_ids_json,
             image, prompt_group, knowledge,
             cap_add_json, features_json, enabled_workers_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            key,
            key,
            0,
            "default",
            dumps([]),
            "",
            prompt_group,
            "knowledge_base",
            dumps([]),
            dumps({}),
            dumps([]),
        ),
    )


def test_default_scenarios_are_seeded() -> None:
    conn = make_conn()
    try:
        raw_features = conn.execute("SELECT features_json FROM scenarios").fetchone()["features_json"]
        scenarios = list_scenarios(conn)
        assert conn.execute("SELECT features_json FROM scenarios").fetchone()["features_json"] == raw_features
        assert len(scenarios) == 1
        scenario = scenarios[0]
        assert scenario.key == scenario.prompt_group == "goal-driven-v1"
        assert scenario.label == "目标驱动探索"
        assert scenario.is_default is True
        assert scenario.profile_id == "default"
        assert scenario.knowledge is None
        assert scenario.features == {"sandbox": True}
        assert replace_scenarios(conn, scenarios) == scenarios
    finally:
        conn.close()


def test_scenario_configuration_freezes_goal_driven_policy_without_storing_protocol() -> None:
    from cairn.server.services.prompts import create_prompt_group
    conn = make_conn()
    try:
        create_prompt_group(conn, "custom-goal")
        scenario = Scenario(
            key="custom", label="自定义", profile_id="restricted",
            prompt_group="custom-goal",
            features={"sandbox": True, "report_prompt_group": "custom-goal"},
        )
        saved = replace_scenarios(conn, [scenario])[0]
        snapshot = scenario_snapshot(saved)
        policy = policy_for_scenario(saved, created_at="2026-09-28T00:00:00Z")
        assert snapshot["prompt_group"] == "custom-goal"
        assert snapshot["features"]["report_prompt_group"] == "custom-goal"
        assert snapshot["features"]["assessment_protocol"] == "goal-driven-v1"
        assert snapshot["features"]["execution_engine"] == "pi"
        assert snapshot["features"]["report_policy_version"] == "1"
        assert saved.features == scenario.features
        assert "assessment_protocol" not in saved.features
        assert policy.goal_driven and policy.independent_verifier_enabled
        assert policy.explore_deadline is None
        assert "exploration_limit" not in snapshot["features"]
        assert "verification_reserve" not in snapshot["features"]
    finally:
        conn.close()


def test_resolve_scenario_key_falls_back_to_default() -> None:
    conn = make_conn()
    try:
        assert resolve_scenario_key(conn, None) == "goal-driven-v1"
    finally:
        conn.close()


def test_create_project_persists_requested_scenario(monkeypatch: pytest.MonkeyPatch) -> None:
    from cairn.server.routers import projects as project_router

    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    try:
        monkeypatch.setattr(project_router, "get_conn", fake_get_conn)
        principal = project_router.RequestPrincipal(
            kind="user",
            subject_id="usr_test",
            tenant_id="tenant_legacy",
            role="user",
        )
        monkeypatch.setattr(
            project_router,
            "create_project_record",
            lambda body, conn, tenant_id, creator: type(
                "ProjectResult",
                (),
                {
                    "project": type(
                        "ProjectMetaResult",
                        (),
                        {"id": "proj_test", "scenario_key": "default"},
                    )()
                },
            )(),
        )
        project = project_router.create_project(
            CreateProjectRequest(
                title="Scenario project",
                origin="https://example.com",
                goal="Audit the target",
                scenario="default",
                tenant_worker_ids=["twr_test"],
                group_id="grp_test",
                authorization_expires_at=None,
            ),
            principal,
        )

        assert project.project.id == "proj_test"
        assert project.project.scenario_key == "default"
    finally:
        conn.close()


def test_replace_scenarios_protects_referenced_keys() -> None:
    conn = make_conn()
    try:
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, scenario_key) VALUES (?, ?, ?, ?, ?)",
            ("proj_001", "Referenced", "active", "2026-01-01T00:00:00Z", "goal-driven-v1"),
        )

        with pytest.raises(HTTPException) as exc:
            replace_scenarios(
                conn,
                [
                    Scenario(
                        key="custom",
                        label="Custom",
                        is_default=True,
                        prompt_group="goal-driven-v1",
                        knowledge="knowledge_base",
                    )
                ],
            )

        assert exc.value.status_code == 409
        assert "goal-driven-v1" in str(exc.value.detail)
    finally:
        conn.close()


def test_replace_scenarios_keeps_one_default() -> None:
    conn = make_conn()
    try:
        scenarios = replace_scenarios(
            conn,
            [
                Scenario(
                    key="alpha",
                    label="Alpha",
                    prompt_group="goal-driven-v1",
                    knowledge="knowledge_base",
                ),
                Scenario(
                    key="beta",
                    label="Beta",
                    is_default=True,
                    prompt_group="goal-driven-v1",
                    knowledge="knowledge_base",
                ),
            ],
        )
    finally:
        conn.close()

    assert [scenario.key for scenario in scenarios] == ["beta", "alpha"]
    assert [scenario.is_default for scenario in scenarios] == [True, False]


def test_replace_scenarios_ignores_legacy_knowledge_after_v19() -> None:
    conn = make_conn()
    try:
        scenarios = replace_scenarios(
            conn,
            [
                Scenario(
                    key="standalone",
                    label="Standalone",
                    is_default=True,
                    prompt_group="goal-driven-v1",
                    knowledge="knowledge_base",
                )
            ],
        )

        assert scenarios[0].knowledge is None
        row = conn.execute(
            "SELECT knowledge FROM scenarios WHERE key = 'standalone'"
        ).fetchone()
        assert row["knowledge"] is None
    finally:
        conn.close()


def test_replace_scenarios_round_trips_creation_templates_without_normalizing() -> None:
    conn = make_conn()
    origin_template = "  第一行\n\n第二行  \n"
    goal_template = "目标一\n  目标二\n"
    try:
        scenarios = replace_scenarios(
            conn,
            [
                Scenario(
                    key="custom",
                    label="Custom",
                    is_default=True,
                    prompt_group="goal-driven-v1",
                    origin_template=origin_template,
                    goal_template=goal_template,
                ),
                Scenario(
                    key="empty",
                    label="Empty",
                    prompt_group="goal-driven-v1",
                    origin_template="",
                    goal_template="",
                ),
            ],
        )

        rows = conn.execute(
            "SELECT key, origin_template, goal_template FROM scenarios ORDER BY key"
        ).fetchall()
    finally:
        conn.close()

    by_key = {scenario.key: scenario for scenario in scenarios}
    assert by_key["custom"].origin_template == origin_template
    assert by_key["custom"].goal_template == goal_template
    assert by_key["empty"].origin_template is None
    assert by_key["empty"].goal_template is None
    assert [dict(row) for row in rows] == [
        {"key": "custom", "origin_template": origin_template, "goal_template": goal_template},
        {"key": "empty", "origin_template": None, "goal_template": None},
    ]


def test_replace_scenarios_preserves_templates_omitted_by_older_clients() -> None:
    conn = make_conn()
    try:
        replace_scenarios(
            conn,
            [
                Scenario(
                    key="custom",
                    label="Custom",
                    is_default=True,
                    prompt_group="goal-driven-v1",
                    origin_template="origin template",
                    goal_template="goal template",
                )
            ],
        )

        scenarios = replace_scenarios(
            conn,
            [
                Scenario.model_validate(
                    {
                        "key": "custom",
                        "label": "Updated label",
                        "is_default": True,
                        "prompt_group": "goal-driven-v1",
                    }
                )
            ],
        )
    finally:
        conn.close()

    assert scenarios[0].label == "Updated label"
    assert scenarios[0].origin_template == "origin template"
    assert scenarios[0].goal_template == "goal template"


def test_scenario_snapshot_excludes_creation_templates() -> None:
    scenario = Scenario(
        key="custom",
        label="Custom",
        prompt_group="goal-driven-v1",
        origin_template="创建时 origin",
        goal_template="创建时 goal",
    )

    snapshot = scenario_snapshot(scenario)

    assert "origin_template" not in snapshot
    assert "goal_template" not in snapshot


@pytest.mark.parametrize(("field", "value"), [
    ("assessment_protocol", "goal-driven-v1"),
    ("explore_budget", 40), ("exploration_limit", "balanced"), ("verification_reserve", 8),
])
def test_validate_scenario_features_rejects_frozen_protocol_and_removed_limit_fields(field, value) -> None:
    with pytest.raises(HTTPException, match="Unsupported features"):
        validate_scenario_features([
            Scenario(key="custom", label="自定义", features={field: value})
        ])


def test_replace_scenarios_rejects_report_only_group_for_exploration() -> None:
    from cairn.server.services.prompts import prompts_root
    from cairn.dispatcher.prompt_resources import read_prompt_resource
    conn = make_conn()
    try:
        path = prompts_root() / "report-only"
        path.mkdir()
        (path / "report.md").write_text(
            read_prompt_resource("goal-driven-v1", "report.md"), encoding="utf-8"
        )
        with pytest.raises(HTTPException, match="do not support exploration"):
            replace_scenarios(conn, [Scenario(key="custom", label="自定义", prompt_group="report-only")])
    finally:
        conn.close()


def test_replace_scenarios_rejects_new_compatibility_group_for_exploration() -> None:
    conn = make_conn()
    try:
        with pytest.raises(HTTPException) as exc:
            replace_scenarios(
                conn,
                [
                    Scenario(
                        key="new-legacy-exploration",
                        label="New Legacy Exploration",
                        prompt_group="default-report",
                    )
                ],
            )

        assert exc.value.status_code == 422
        assert "Unknown prompt groups" in str(exc.value.detail)
    finally:
        conn.close()


def test_replace_scenarios_accepts_independent_report_prompt_role() -> None:
    from cairn.server.services.prompts import create_prompt_group
    conn = make_conn()
    try:
        create_prompt_group(conn, "custom-report")
        scenarios = replace_scenarios(conn, [Scenario(
            key="split-prompt-roles", label="自定义报告", profile_id="default",
            features={"report_prompt_group": "custom-report"},
        )])
        assert scenarios[0].prompt_group == "goal-driven-v1"
        assert scenario_snapshot(scenarios[0])["features"]["report_prompt_group"] == "custom-report"
    finally:
        conn.close()


def test_replace_scenarios_rejects_unknown_prompt_group() -> None:
    conn = make_conn()
    try:
        with pytest.raises(HTTPException) as exc:
            replace_scenarios(
                conn,
                [
                    Scenario(
                        key="bad",
                        label="Bad",
                        prompt_group="missing_group",
                        knowledge="knowledge_base",
                    )
                ],
            )

        assert exc.value.status_code == 422
        assert "missing_group" in str(exc.value.detail)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "prompt_group",
    ["code_audit", "default-copy", "pentest", "logic-vuln"],
)
def test_replace_scenarios_rejects_new_legacy_prompt_group_reference(
    prompt_group: str,
) -> None:
    conn = make_conn()
    try:
        with pytest.raises(HTTPException) as exc:
            replace_scenarios(
                conn,
                [
                    Scenario(
                        key="new-legacy",
                        label="New Legacy",
                        prompt_group=prompt_group,
                        knowledge="knowledge_base",
                    )
                ],
            )

        assert exc.value.status_code == 422
        assert prompt_group in str(exc.value.detail)
    finally:
        conn.close()


def test_replace_scenarios_ignores_legacy_worker_fields() -> None:
    conn = make_conn()
    try:
        scenarios = replace_scenarios(
            conn,
            [
                Scenario.model_validate(
                    {
                        "key": "offline",
                        "label": "Offline",
                        "prompt_group": "goal-driven-v1",
                        "knowledge": "knowledge_base",
                        "worker_ids": ["future-worker"],
                        "enabled_workers": ["future-worker"],
                    }
                )
            ],
        )

        row = conn.execute("SELECT worker_ids_json, enabled_workers_json FROM scenarios WHERE key = ?", ("offline",)).fetchone()
        assert scenarios[0].key == "offline"
        assert not hasattr(scenarios[0], "enabled_workers")
        assert row["worker_ids_json"] == "[]"
        assert row["enabled_workers_json"] == "[]"
    finally:
        conn.close()


def test_replace_scenarios_does_not_validate_workers_when_dispatcher_pool_exists() -> None:
    conn = make_conn()
    try:
        insert_dispatcher_status(conn, worker_names=["worker-a"])

        scenarios = replace_scenarios(
            conn,
            [
                Scenario.model_validate(
                    {
                        "key": "bad-worker",
                        "label": "Bad worker",
                        "prompt_group": "goal-driven-v1",
                        "knowledge": "knowledge_base",
                        "worker_ids": ["worker-a", "missing-worker"],
                    }
                )
            ],
        )

        assert scenarios[0].key == "bad-worker"
    finally:
        conn.close()


def test_scenario_catalog_exposes_only_project_creation_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    from cairn.server.routers import scenarios as scenarios_router

    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    try:
        conn.execute("DELETE FROM scenarios")
        conn.execute(
            """
            INSERT INTO scenarios
                (key, label, is_default, profile_id, image, prompt_group, knowledge,
                 origin_template, goal_template, cap_add_json, features_json)
            VALUES ('safe-default', 'Safe default', 1, 'private-profile', 'private-image',
                    'private-prompt', 'private-knowledge', 'safe origin', 'safe goal',
                    '[\"NET_ADMIN\"]', '{\"secret\":true}')
            """
        )
        monkeypatch.setattr(scenarios_router, "get_conn", fake_get_conn)

        payload = [item.model_dump() for item in scenarios_router.get_scenario_catalog()]

        assert payload == [
            {
                "key": "safe-default",
                "label": "Safe default",
                "is_default": True,
                "origin_template": "safe origin",
                "goal_template": "safe goal",
            }
        ]
        assert not ({"profile_id", "image", "prompt_group", "knowledge", "cap_add", "features"} & payload[0].keys())
    finally:
        conn.close()


def test_app_exposes_scenarios_routes() -> None:
    from cairn.server.app import app

    paths = app.openapi()["paths"]
    assert "/scenario-catalog" in paths
    assert "/scenarios" in paths
    assert "/scenarios/validate" in paths
    assert "/scenarios/options" in paths
    assert "/digest-worker" not in paths


def test_scenario_validate_route_returns_project_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    from cairn.server.routers import scenarios as scenario_router

    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    try:
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, scenario_key) VALUES (?, ?, ?, ?, ?)",
            ("proj_001", "Referenced", "active", "2026-01-01T00:00:00Z", "goal-driven-v1"),
        )
        monkeypatch.setattr(scenario_router, "get_conn", fake_get_conn)

        result = scenario_router.validate_scenarios(list_scenarios(conn))

        assert result.valid is True
        assert result.scenario_count == 1
        assert result.project_counts == {"goal-driven-v1": 1}
    finally:
        conn.close()


def test_scenario_options_include_dispatcher_report_images(monkeypatch: pytest.MonkeyPatch) -> None:
    from cairn.server.routers import scenarios as scenario_router

    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    try:
        insert_dispatcher_status(
            conn,
            worker_names=["mock_worker"],
            images=["cairn/default:latest", "cairn/web:latest"],
            caps=["NET_RAW"],
        )
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, scenario_key) VALUES (?, ?, ?, ?, ?)",
            ("proj_001", "Web", "active", "2026-01-01T00:00:00Z", "goal-driven-v1"),
        )
        monkeypatch.setattr(scenario_router, "get_conn", fake_get_conn)

        options = scenario_router.get_scenario_options()

        assert not hasattr(options, "workers")
        assert "cairn/default:latest" in options.images
        assert "cairn/web:latest" in options.images
        assert options.project_counts == {"goal-driven-v1": 1}
    finally:
        conn.close()


def test_dispatcher_container_runtime_uses_allowed_image_and_cap_baseline() -> None:
    loop = _dispatcher_loop_for_scenario_policy()
    scenario = Scenario(
        key="web",
        label="Web",
        image="cairn/web:latest",
        prompt_group="goal-driven-v1",
        knowledge="knowledge_base",
        cap_add=["NET_RAW", "SYS_ADMIN"],
        features={"sandbox": True},
    )

    runtime = loop._container_runtime_options(scenario)
    context = loop._scenario_container_runtime_context(scenario, runtime)

    assert runtime.image == "cairn/web:latest"
    assert runtime.cap_add == ("NET_RAW",)
    assert context["rejected"] == {"image": None, "cap_add": ["SYS_ADMIN"]}


def test_dispatcher_container_runtime_rejects_unlisted_image() -> None:
    loop = _dispatcher_loop_for_scenario_policy()
    scenario = Scenario(
        key="custom",
        label="Custom",
        image="cairn/untrusted:latest",
        prompt_group="goal-driven-v1",
        knowledge="knowledge_base",
        cap_add=["NET_RAW"],
        features={},
    )

    runtime = loop._container_runtime_options(scenario)
    context = loop._scenario_container_runtime_context(scenario, runtime)

    assert runtime.image == "cairn/default:latest"
    assert context["requested"]["image"] == "cairn/untrusted:latest"
    assert context["effective"]["image"] == "cairn/default:latest"
    assert context["rejected"]["image"] == "cairn/untrusted:latest"


def test_dispatcher_worker_selection_fails_closed_without_project_binding() -> None:
    loop = _dispatcher_loop_for_scenario_policy()
    loop.futures = {}
    loop.worker_cooldown_until = {}
    loop.worker_rejected_until = {}
    scenario = Scenario(
        key="custom",
        label="Custom",
        prompt_group="goal-driven-v1",
        knowledge="knowledge_base",
    )

    selection = loop._select_worker("project-1", "reason", scenario)

    assert selection.worker is None
    assert selection.candidate_order == []


def _dispatcher_loop_for_scenario_policy() -> DispatcherLoop:
    loop = object.__new__(DispatcherLoop)
    loop._log_state = {}
    loop.config = DispatchConfig(
        server="http://127.0.0.1:8000",
        runtime=RuntimeConfig(
            max_workers=1,
            max_running_projects=1,
            max_project_workers=1,
            interval=1,
            healthcheck_timeout=1,
            prompt_group="goal-driven-v1",
        ),
        tasks=TasksConfig(
            bootstrap=BootstrapTaskConfig(timeout=1, conclude_timeout=1),
            reason=ReasonTaskConfig(timeout=1, max_intents=1),
            explore=ExploreTaskConfig(timeout=1, conclude_timeout=1),
        ),
        container=ContainerConfig(
            image="cairn/default:latest",
            allowed_images=["cairn/web:latest"],
            network_mode="none",
            completed_action="stop",
            cap_add=["NET_RAW"],
        ),
        workers=[
            WorkerConfig(
                name="fixture-pi",
                engine="pi",
                task_types=["bootstrap", "reason", "explore"],
                max_running=1,
                priority=1,
            )
        ],
    )
    return loop


def test_project_scenario_snapshot_takes_precedence_over_catalog() -> None:
    loop = _dispatcher_loop_for_scenario_policy()
    frozen = Scenario(
        key="default-copy-v2",
        label="Frozen default-copy",
        profile_id="default",
        prompt_group="default-copy-v2",
        knowledge="pentest",
        cap_add=["NET_RAW"],
        features={"assessment_protocol": "default-copy-v1"},
    )
    loop.client = SimpleNamespace(
        list_scenarios=lambda: (_ for _ in ()).throw(AssertionError("catalog must not be read"))
    )
    project = SimpleNamespace(
        project=SimpleNamespace(
            id="proj_snapshot",
            scenario_key="default-copy-v2",
            scenario_snapshot=frozen.model_dump(),
        )
    )

    assert loop._scenario_for_project(project) == frozen


def test_legacy_project_without_snapshot_uses_live_catalog() -> None:
    loop = _dispatcher_loop_for_scenario_policy()
    catalog = Scenario(
        key="legacy-custom",
        label="Live catalog",
        profile_id="default",
        prompt_group="goal-driven-v1",
        knowledge="knowledge_base",
    )
    loop.client = SimpleNamespace(list_scenarios=lambda: [catalog])
    project = SimpleNamespace(
        project=SimpleNamespace(
            id="proj_legacy",
            scenario_key="legacy-custom",
            scenario_snapshot={},
        )
    )

    assert loop._scenario_for_project(project) == catalog


# ---- P1: 分支生命周期 + 完成硬门 ----

from contextlib import contextmanager as _ctx_cm


@_ctx_cm
def _yield_conn(conn):
    yield conn


def _setup_project_with_intent(conn, intent_id="i001", worker=None):
    """建一个 active 项目 + origin/goal/f001 + 一个 open intent（可指定 worker）。"""
    conn.execute(
        "INSERT INTO projects (id, title, status, created_at, scenario_key) "
        "VALUES ('proj_001', 't', 'active', '2026-01-01T00:00:00Z', 'default')"
    )
    conn.execute(
        "INSERT INTO facts (id, project_id, description, creator, created_at) VALUES "
        "('origin', 'proj_001', 'o', 'system', '2026-01-01T00:00:00Z'),"
        "('goal', 'proj_001', 'g', 'system', '2026-01-01T00:00:00Z'),"
        "('f001', 'proj_001', 'finding', 'system', '2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, "
        "last_heartbeat_at, created_at, concluded_at) "
        "VALUES (?, 'proj_001', NULL, 'test dir', 'w', ?, ?, '2026-01-01T00:00:00Z', NULL)",
        (intent_id, worker, '2026-01-01T00:00:00Z' if worker else None),
    )
    conn.execute(
        "INSERT INTO intent_sources (intent_id, project_id, fact_id) "
        "VALUES (?, 'proj_001', 'f001')",
        (intent_id,),
    )


def test_abandon_unclaimed_intent() -> None:
    """abandon 一个未认领的 open intent → conclusion='abandoned'，可继续 complete。"""
    from cairn.server.services import projects as projects_service

    conn = make_conn()
    try:
        _setup_project_with_intent(conn, "i001", worker=None)
        result = projects_service.abandon_intent_record(conn, "proj_001", "i001", "w")
        assert result.conclusion == "abandoned"
        assert result.concluded_at is not None
        assert result.to is None
    finally:
        conn.close()


def test_abandon_claimed_intent_rejected() -> None:
    """运行中（worker 非空）的 intent 不能 abandon → 409。"""
    from cairn.server.services import projects as projects_service

    conn = make_conn()
    try:
        _setup_project_with_intent(conn, "i001", worker="explorer")
        with pytest.raises(HTTPException) as exc:
            projects_service.abandon_intent_record(conn, "proj_001", "i001", "w")
        assert exc.value.status_code == 409
    finally:
        conn.close()


def test_complete_blocked_by_open_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    """具备执行资格的项目仍不能绕过 open intent 完成硬门。"""
    from cairn.server.services import project_completion
    from cairn.server.models import CompleteRequest

    conn = make_conn()
    try:
        # 此处测试非结构化执行项目的完成门，不让执行资格错误掩盖开放义务。
        conn.execute(
            "UPDATE scenarios SET features_json = '{\"complete_gate\":true}' "
            "WHERE key = 'default'"
        )
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, scenario_key) "
            "VALUES ('proj_001', 't', 'active', '2026-01-01T00:00:00Z', 'default')"
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description, creator, created_at) VALUES "
            "('origin', 'proj_001', 'o', 'system', '2026-01-01T00:00:00Z'),"
            "('goal', 'proj_001', 'g', 'system', '2026-01-01T00:00:00Z'),"
            "('f001', 'proj_001', 'finding', 'system', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, "
            "last_heartbeat_at, created_at, concluded_at) "
            "VALUES ('i001', 'proj_001', NULL, 'test dir', 'w', NULL, NULL, '2026-01-01T00:00:00Z', NULL)"
        )
        conn.execute(
            "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES ('i001', 'proj_001', 'f001')"
        )
        conn.execute(
            "UPDATE projects SET scenario_snapshot_json = ? WHERE id = 'proj_001'",
            (dumps({"features": {"execution_contract_version": 1, "execution_engine": "pi"}}),),
        )
        monkeypatch.setattr(project_completion, "get_conn", lambda: _yield_conn(conn))
        with pytest.raises(HTTPException) as exc:
            project_completion.complete_project_record(
                "proj_001", CompleteRequest(from_=["f001"], description="done", worker="w")
            )
        assert exc.value.status_code == 409
        assert "Resolve open intents before complete" in exc.value.detail
        assert conn.execute("SELECT status FROM projects WHERE id = 'proj_001'").fetchone()[0] == "active"
    finally:
        conn.close()


def test_reason_contract_abandon_parsed() -> None:
    """contracts.py 正确解析 data.abandon。"""
    from cairn.dispatcher.contracts import validate_reason_payload

    kind, data, abandon = validate_reason_payload(
        {"protocol":"cairn-assessment-v2","accepted": True, "kind": "reason_result", "data": {"rationale": "x", "abandon": ["i001", "i002"], "intents": [{"from": ["f001"], "description": "new"}]}},
        open_intents_empty=False,
        max_intents=2,
    )
    assert kind == "intents"
    assert abandon == ["i001", "i002"]

    with pytest.raises(ValueError, match="completion cannot discard obligations in the same decision"):
        validate_reason_payload(
            {"protocol":"cairn-assessment-v2","accepted": True, "kind": "reason_result", "data": {"rationale": "x", "abandon": ["i001"], "complete": {"from": ["f001"], "description": "done"}}},
            open_intents_empty=False,
            max_intents=2,
        )

    kind, data, abandon = validate_reason_payload(
        {"protocol":"cairn-assessment-v2","accepted": True, "kind": "reason_result", "data": {"rationale": "x", "abandon": ["i001"]}},
        open_intents_empty=False,
        max_intents=2,
    )
    assert kind == "noop"
    assert abandon == ["i001"]


def test_structured_coverage_kind_shorthand_is_canonicalized() -> None:
    """模型的 kind=coverage 仅在完整 structured Cell 中归一化。"""
    from cairn.dispatcher.contracts import extract_coverage_slots

    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "bootstrap_result",
        "data": {
            "slots": [
                {
                    "kind": "coverage",
                    "surface_kind": "endpoint",
                    "surface_key": "POST https://example.test/login",
                    "test_family": "authentication",
                    "applicability_basis": "公开登录端点接受凭据",
                    "completion_criteria": "比较有效、无效、缺失和畸形凭据",
                }
            ]
        },
    }

    slots = extract_coverage_slots(payload)
    assert slots[0]["kind"] == "endpoint"
    assert slots[0]["surface_kind"] == "endpoint"


@pytest.mark.parametrize(
    "slot",
    [
        {"kind": "coverage", "key": "legacy:x"},
        {
            "kind": "host",
            "surface_kind": "endpoint",
            "surface_key": "POST https://example.test/login",
            "test_family": "authentication",
            "applicability_basis": "公开登录端点接受凭据",
            "completion_criteria": "比较有效和无效凭据",
        },
    ],
)
def test_invalid_coverage_kind_forms_are_rejected(slot: dict[str, object]) -> None:
    from cairn.dispatcher.contracts import extract_coverage_slots

    with pytest.raises(ValueError):
        extract_coverage_slots({"protocol":"cairn-assessment-v2","accepted": True, "data": {"slots": [slot]}})


def test_reason_contract_rejects_slots_alias() -> None:
    from cairn.dispatcher.contracts import validate_reason_payload

    with pytest.raises(ValueError, match="data.discovered_slots"):
        validate_reason_payload(
            {
                "protocol": "cairn-assessment-v2",
                "accepted": True,
                "kind": "reason_result",
                "data": {
                    "slots": [
                        {
                            "kind": "endpoint",
                            "surface_kind": "endpoint",
                            "surface_key": "POST https://example.test/login",
                            "test_family": "authentication",
                            "applicability_basis": "新确认的公开登录端点",
                            "completion_criteria": "比较有效和无效凭据",
                        }
                    ]
                },
            },
            open_intents_empty=True,
            max_intents=2,
        )


def test_reason_structured_discovery_is_registration_only() -> None:
    from cairn.dispatcher.contracts import validate_reason_payload

    discovered_slot = {
        "kind": "endpoint",
        "surface_kind": "endpoint",
        "surface_key": "POST https://example.test/login",
        "test_family": "authentication",
        "applicability_basis": "新确认的公开登录端点",
        "completion_criteria": "比较有效和无效凭据",
    }
    kind, data, abandon = validate_reason_payload(
        {
            "protocol": "cairn-assessment-v2",
            "accepted": True,
            "kind": "reason_result",
            "data": {"rationale": "先登记 Cell", "discovered_slots": [discovered_slot]},
        },
        open_intents_empty=True,
        max_intents=2,
    )
    assert kind == "noop"
    assert data is None
    assert abandon == []

    with pytest.raises(ValueError, match="registration-only"):
        validate_reason_payload(
            {
                "protocol": "cairn-assessment-v2",
                "accepted": True,
                "kind": "reason_result",
                "data": {
                    "discovered_slots": [discovered_slot],
                    "intents": [
                        {
                            "from": ["f001"],
                            "slot_key": "cell:guessed",
                            "description": "unsafe same-round binding",
                        }
                    ],
                },
            },
            open_intents_empty=True,
            max_intents=2,
        )


def test_conclusion_column_migrated() -> None:
    """迁移后 intents 表有 conclusion 列。"""
    conn = make_conn()
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(intents)").fetchall()}
        assert "conclusion" in cols
    finally:
        conn.close()


# ---- P2: depends_on 依赖门控 ----

def test_create_intent_with_depends_on() -> None:
    """create_intent 带 depends_on → 存储正确。"""
    from cairn.server.services import projects as projects_service

    conn = make_conn()
    try:
        _setup_project_with_intent(conn, "i001", worker=None)
        # 创建第二个 intent 依赖第一个
        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, "
            "last_heartbeat_at, created_at, concluded_at, depends_on) "
            "VALUES ('i002', 'proj_001', NULL, 'step2', 'w', NULL, NULL, "
            "'2026-01-01T00:00:00Z', NULL, 'i001')"
        )
        conn.execute(
            "INSERT INTO intent_sources (intent_id, project_id, fact_id) "
            "VALUES ('i002', 'proj_001', 'f001')"
        )
        # 验证 intent_to_model 正确读 depends_on
        row = conn.execute(
            "SELECT * FROM intents WHERE id = 'i002' AND project_id = 'proj_001'"
        ).fetchone()
        model = projects_service.intent_to_model(conn, row, "proj_001")
        assert model.depends_on == "i001"
    finally:
        conn.close()


def test_claim_blocked_by_unmet_dependency() -> None:
    """claim 依赖未收敛的 intent → 409。"""
    from cairn.server.services import projects as projects_service

    conn = make_conn()
    try:
        _setup_project_with_intent(conn, "i001", worker=None)
        # i002 依赖 i001（i001 未收敛）
        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, "
            "last_heartbeat_at, created_at, concluded_at, depends_on) "
            "VALUES ('i002', 'proj_001', NULL, 'step2', 'w', NULL, NULL, "
            "'2026-01-01T00:00:00Z', NULL, 'i001')"
        )
        conn.execute(
            "INSERT INTO intent_sources (intent_id, project_id, fact_id) "
            "VALUES ('i002', 'proj_001', 'f001')"
        )
        with pytest.raises(HTTPException) as exc:
            projects_service.get_claimable_open_intent_or_404(
                conn, "proj_001", "i002", "explorer"
            )
        assert exc.value.status_code == 409
        assert "unmet dependencies" in str(exc.value.detail).lower()
    finally:
        conn.close()


def test_claim_allowed_after_dependency_concluded() -> None:
    """依赖收敛后 claim → 成功。"""
    from cairn.server.services import projects as projects_service

    conn = make_conn()
    try:
        _setup_project_with_intent(conn, "i001", worker=None)
        # 先收敛 i001
        conn.execute(
            "UPDATE intents SET to_fact_id = 'f001', concluded_at = '2026-01-01T00:01:00Z', "
            "conclusion = 'completed' WHERE id = 'i001' AND project_id = 'proj_001'"
        )
        # i002 依赖 i001（已收敛）
        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, "
            "last_heartbeat_at, created_at, concluded_at, depends_on) "
            "VALUES ('i002', 'proj_001', NULL, 'step2', 'w', NULL, NULL, "
            "'2026-01-01T00:00:00Z', NULL, 'i001')"
        )
        conn.execute(
            "INSERT INTO intent_sources (intent_id, project_id, fact_id) "
            "VALUES ('i002', 'proj_001', 'f001')"
        )
        # claim 应成功
        row = projects_service.get_claimable_open_intent_or_404(
            conn, "proj_001", "i002", "explorer"
        )
        assert row is not None
    finally:
        conn.close()


def test_reason_contract_depends_on_parsed() -> None:
    """contracts.py 解析 intent 的 depends_on 字段。"""
    from cairn.dispatcher.contracts import validate_reason_payload

    kind, data, abandon = validate_reason_payload(
        {
            "protocol": "cairn-assessment-v2",
            "accepted": True,
            "kind": "reason_result",
            "data": {
                "rationale": "chain plan",
                "intents": [
                    {"from": ["f001"], "description": "step1"},
                    {"from": ["f001"], "description": "step2", "depends_on": "@1"},
                ],
            },
        },
        open_intents_empty=False,
        max_intents=2,
    )
    assert kind == "intents"
    assert data[1]["depends_on"] == "@1"


def test_depends_on_column_migrated() -> None:
    """迁移后 intents 表有 depends_on 列。"""
    conn = make_conn()
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(intents)").fetchall()}
        assert "depends_on" in cols
    finally:
        conn.close()


def test_restricted_execution_profile_keeps_resource_limits() -> None:
    from cairn.dispatcher.config import ExecutionProfileConfig
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="requires mem_limit"):
        ExecutionProfileConfig(id="restricted")
    with pytest.raises(ValidationError, match="must equal mem_limit"):
        ExecutionProfileConfig(id="restricted", mem_limit="1536m", memswap_limit="2g", pids_limit=256)
    profile = ExecutionProfileConfig(id="restricted", mem_limit="1536m", memswap_limit="1536m", pids_limit=256)
    assert profile.mem_limit == profile.memswap_limit == 1536 * 1024 * 1024
    assert profile.pids_limit == 256
