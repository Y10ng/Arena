"""Goal-driven Reason 使用稳定输入摘要和显式业务上下文。"""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from cairn.dispatcher.contracts import validate_reason_payload
from cairn.dispatcher.prompt_resources import (
    read_prompt_resource,
    validate_prompt_resources,
)
from cairn.dispatcher.tasks.reason_context import (
    build_reason_prompt_context,
    goal_driven_decision_input_digest,
)


class FakeContainerManager:
    def __init__(self):
        self.files = {}

    def write_text_file(self, container_name, path, content):
        self.files[(container_name, path)] = content


class ForbiddenCoverageClient:
    def list_coverage_slots(self, project_id):
        raise AssertionError(f"goal-driven project queried Coverage Cells: {project_id}")


def fact(identity, description, created, task_run_id=None):
    return SimpleNamespace(
        id=identity,
        description=description,
        creator="worker",
        task_run_id=task_run_id,
        created_at=created,
    )


def intent(identity, created, *, priority=1, worker=None, conclusion=None):
    return SimpleNamespace(
        id=identity,
        from_=["f1"],
        to=None,
        description=f"intent {identity}",
        creator="reasoner",
        task_run_id="reason-run",
        worker=worker,
        last_heartbeat_at="2026-09-21T02:00:00Z",
        created_at=created,
        concluded_at=None if conclusion is None else "2026-09-21T03:00:00Z",
        conclusion=conclusion,
        conclusion_reason="done" if conclusion else None,
        depends_on=None,
        slot_key=None,
        strategy_key="",
        assessment_role="discovery",
        priority=priority,
        target_observation_id=None,
        depth_review_fact_id=None,
    )


def goal_project():
    execution = {
        "version": 2,
        "executable": True,
        "execution_contract_version": 1,
        "execution_engine": "pi",
        "goal_driven": True,
        "enabled": True,
        "surface_manifest_enabled": True,
        "independent_verifier_enabled": True,
        "outcome_contract_version": 3,
        # 历史字段即使意外存在，也不能重新启用旧深测路径。
        "phase": "depth_review",
    }
    meta = SimpleNamespace(
        id="p1",
        scenario_key="default",
        assessment_protocol="goal-driven-v1",
        assessment_execution=execution,
        execution_generation=4,
        vulnerability_fingerprint_version=1,
        explore_deadline="2026-09-22T00:00:00Z",
        authorization_expires_at=None,
        explore_attempts_started=3,
        verification_attempts_started=2,
        reason=None,
    )
    return SimpleNamespace(
        project=meta,
        facts=[
            fact("origin", "authorized engagement", "2026-09-21T00:00:00Z"),
            fact("goal", "verify access boundaries", "2026-09-21T00:00:01Z"),
            fact("f1", "login is reachable", "2026-09-21T01:00:00Z", "bootstrap-run"),
        ],
        intents=[
            intent("i-high", "2026-09-21T01:10:00Z", priority=2),
            intent("i-running", "2026-09-21T01:11:00Z", worker="worker-a"),
            intent("i-closed", "2026-09-21T01:12:00Z", conclusion="completed"),
        ],
        hints=[SimpleNamespace(
            id="h1",
            content="check tenant separation",
            creator="operator",
            fact_id="f1",
            intent_id=None,
            created_at="2026-09-21T01:05:00Z",
        )],
        targets=[
            SimpleNamespace(
                id="t1", value="https://example.test", type="url", status="seed",
                source_fact_id="origin", discovered_by_run_id=None,
                created_at="2026-09-21T00:00:00Z",
            ),
            SimpleNamespace(
                id="t2", value="https://candidate.test", type="url", status="discovered",
                source_fact_id="f1", discovered_by_run_id="bootstrap-run",
                created_at="2026-09-21T01:00:00Z",
            ),
        ],
    )


def test_goal_driven_reason_context_binds_digest_priority_goal_and_scope():
    project = goal_project()
    manager = FakeContainerManager()
    context = build_reason_prompt_context(
        SimpleNamespace(tasks=SimpleNamespace(reason=SimpleNamespace(max_intents=4))),
        manager,
        "container-p1",
        project,
        "facts: []",
        client=ForbiddenCoverageClient(),
    )

    assert context.binding["allowed_fact_ids"] == ["origin", "f1"]
    assert context.binding["allowed_intent_ids"] == ["i-high", "i-running", "i-closed"]
    assert context.binding["max_intents"] == 4
    assert len(context.binding["decision_input_digest"]) == 64
    assert "review_input_digest" not in context.binding
    assert "structured_intent_binding" not in context.binding
    assert context.open_intents[0]["priority"] == 2
    assert context.replacements["goal"] == "verify access boundaries"
    assert "https://example.test" in context.replacements["authorized_targets"]
    assert "https://candidate.test" not in context.replacements["authorized_targets"]
    assessment = json.loads(context.replacements["assessment_execution"])
    assert "selected_depth_obligations" not in assessment
    decision = json.loads(context.replacements["decision_context"])
    assert decision["attempt_statistics"] == {
        "explore_attempts_started": 3,
        "verification_attempts_started": 2,
    }
    assert "logical_budget" not in decision
    assert decision["execution_cutoff"] == project.project.explore_deadline
    assert decision["concurrency"] == {
        "open_intents": 2,
        "claimed_open_intents": 1,
        "unclaimed_open_intents": 1,
    }
    assert manager.files


def test_goal_driven_decision_digest_ignores_lease_noise_but_tracks_business_input():
    project = goal_project()
    baseline = goal_driven_decision_input_digest(project)

    lease_change = deepcopy(project)
    lease_change.intents[0].worker = "another-worker"
    lease_change.intents[0].last_heartbeat_at = "2026-09-21T05:00:00Z"
    lease_change.project.reason = SimpleNamespace(worker="reasoner")
    assert goal_driven_decision_input_digest(lease_change) == baseline

    historical_budget_change = deepcopy(project)
    historical_budget_change.project.explore_budget = 1
    historical_budget_change.project.assessment_execution.update(
        budget=1, verification_reserve=8, remaining_discovery=0,
    )
    assert goal_driven_decision_input_digest(historical_budget_change) == baseline

    cutoff_change = deepcopy(project)
    cutoff_change.project.explore_deadline = None
    assert goal_driven_decision_input_digest(cutoff_change) != baseline

    business_change = deepcopy(project)
    business_change.intents[0].priority = 0
    assert goal_driven_decision_input_digest(business_change) != baseline


@pytest.mark.parametrize(("budget", "reserve"), [(40, 99), ("invalid", "invalid")])
def test_goal_driven_policy_ignores_invalid_historical_limits(budget, reserve):
    from cairn.server.services.assessment_policy import policy_from_project_row

    policy = policy_from_project_row({
        "assessment_protocol": "goal-driven-v1",
        "explore_budget": budget,
        "scenario_snapshot_json": json.dumps({"features": {
            "assessment_contract_version": 2, "verifier_contract_version": 1,
            "verification_reserve": reserve,
        }}),
    })
    assert policy.goal_driven
    assert policy.contract_version == 2
    assert policy.logical_attempts_enabled
    assert policy.explore_deadline is None


def test_goal_driven_reason_accepts_surfaces_and_prioritized_intents_together():
    surface = {
        "surface_kind": "host",
        "surface_key": "api.example.test",
        "applicable_tests": [],
    }
    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "reason_result",
        "data": {
            "discovered_surfaces": [surface],
            "intents": [
                {
                    "from": ["f1"],
                    "description": "验证新发现 API 的身份边界",
                    "priority": 2,
                },
                {
                    "from": ["f1"],
                    "description": "记录补充观察",
                },
            ],
        },
    }

    kind, intents, abandon = validate_reason_payload(
        payload,
        open_intents_empty=True,
        max_intents=4,
        surface_manifest=True,
        goal_driven=True,
    )

    assert kind == "intents"
    assert intents[0]["priority"] == 2
    assert intents[1]["priority"] == 1
    assert abandon == []


@pytest.mark.parametrize(
    ("dependency", "valid"),
    [
        ("i-closed", True), ("@1", True),
        ("i-missing", False), ("semantic-name", False),
        ("@0", False), ("@2", False), ("@99", False),
    ],
)
def test_goal_driven_reason_checks_frozen_dependency_before_writes(dependency, valid):
    payload = {
        "protocol": "cairn-assessment-v2", "accepted": True, "kind": "reason_result",
        "data": {"intents": [
            {"from": ["f1"], "description": "First"},
            {"from": ["f1"], "description": "Second", "depends_on": dependency},
        ]},
    }
    kwargs = dict(open_intents_empty=True, max_intents=4, goal_driven=True,
                  allowed_intent_ids=["i-closed"])
    if valid:
        assert validate_reason_payload(payload, **kwargs)[0] == "intents"
    else:
        with pytest.raises(ValueError, match=r"intent\[1\].depends_on is invalid"):
            validate_reason_payload(payload, **kwargs)


@pytest.mark.parametrize("priority", [-1, 3, True, "2"])
def test_goal_driven_reason_rejects_invalid_priority(priority):
    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "reason_result",
        "data": {
            "intents": [{
                "from": ["f1"],
                "description": "验证边界",
                "priority": priority,
            }],
        },
    }

    with pytest.raises(ValueError, match="priority must be 0, 1, or 2"):
        validate_reason_payload(
            payload,
            open_intents_empty=True,
            max_intents=4,
            surface_manifest=True,
            goal_driven=True,
        )


def test_goal_driven_builtin_prompts_are_executable_without_old_control_model():
    validate_prompt_resources("goal-driven-v1", builtin_only=True)
    bootstrap = read_prompt_resource("goal-driven-v1", "bootstrap.md", builtin_only=True)
    reason = read_prompt_resource("goal-driven-v1", "reason.md", builtin_only=True)
    explore = read_prompt_resource("goal-driven-v1", "explore.md", builtin_only=True)
    report = read_prompt_resource("goal-driven-v1", "report.md", builtin_only=True)

    assert "does not complete the project" in bootstrap
    assert "large minified bundles or very long single-line scripts" in bootstrap
    assert "leave deep script analysis to Reason/Explore" in bootstrap
    assert "`priority`" in reason
    assert "only a suggestion" in reason
    assert "Surfaces and source-linked Intents together" in reason
    assert "single `data.assessment_record`" in explore
    assert "Do not submit `coverage_result`" in explore
    assert "zero vulnerabilities" in report or "零漏洞" in report
    for content in (bootstrap, reason, explore, report):
        assert "submit_result" in content
        assert "register_artifact" in content


@pytest.mark.parametrize("origin_source", [True, False])
def test_research_contexts_share_persisted_scope_and_unchanged_origin(origin_source):
    from cairn.dispatcher.prompting import render_prompt
    from cairn.dispatcher.tasks.bootstrap_context import bootstrap_prompt_replacements
    from cairn.dispatcher.tasks.explore_context import (
        explore_prompt_replacements,
        verifier_prompt_replacements,
    )
    from cairn.project_authorization_contract import (
        build_scope_descriptor_v3,
        build_scope_descriptor_v4,
        target_scope_v3,
    )

    project = goal_project()
    project.project.tenant_id = "tenant-context"
    origin = "  仅研究提供的资料；{goal} 是资料中的字面片段。\n保留排除项与原始空白。  "
    project.facts[0].description = origin
    if origin_source:
        project.targets = []
        descriptor = build_scope_descriptor_v4(
            project_id=project.project.id,
            tenant_id=project.project.tenant_id,
            origin_fact_id="origin",
            origin=origin,
        )
    else:
        descriptor = build_scope_descriptor_v3(
            project_id=project.project.id,
            tenant_id=project.project.tenant_id,
            target_digest=target_scope_v3([vars(target) for target in project.targets])[0],
            target_ids=["t1"],
        )
    project.project.authorization_scope_descriptor = descriptor
    manager = FakeContainerManager()
    replacements = {
        "bootstrap": bootstrap_prompt_replacements(project),
        "reason": build_reason_prompt_context(
            SimpleNamespace(tasks=SimpleNamespace(reason=SimpleNamespace(max_intents=4))),
            manager, "container-p1", project, "facts: []",
        ).replacements,
        "explore": explore_prompt_replacements(
            manager, "container-p1", "facts: []", project.intents[0],
            phase="explore_execute", project=project,
        ),
    }
    verifier_intent = deepcopy(project.intents[0])
    verifier_intent.target_observation_id = "observation-1"
    replacements["verifier"] = verifier_prompt_replacements(
        verifier_intent,
        SimpleNamespace(neutral_projection={"version": 1, "observation_id": "observation-1"}),
        project=project,
    )
    for phase, context in replacements.items():
        assert context["origin"] == origin
        scope = json.loads(context["authorization_scope"])
        assert scope["scope_descriptor"] == descriptor
        assert ("Origin is the authoritative scope source" in scope["instructions"]) == origin_source
        assert ("legacy or explicit Target authorization" in scope["instructions"]) != origin_source
        target_index = json.loads(context["authorized_targets"])
        assert [item["id"] for item in target_index] == ([] if origin_source else ["t1"])
        names = [f"{phase}.md"]
        if phase != "reason":
            names.append(f"{phase}_conclude.md")
        for name in names:
            rendered = render_prompt(
                read_prompt_resource("goal-driven-v1", name, builtin_only=True), context,
            )
            assert origin in rendered
            assert context["authorization_scope"] in rendered
            assert "{authorization_scope}" not in rendered
    if origin_source:
        project.facts[0].description += "改写范围"
        with pytest.raises(ValueError, match="does not match its grant"):
            bootstrap_prompt_replacements(project)
