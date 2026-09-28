from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
import sqlite3
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.dispatcher.tasks.common import ProcessArtifactRecord, UnresolvedArtifactEvidenceError
from cairn.dispatcher.tasks.reason_context import build_reason_prompt_context
from cairn.dispatcher.tasks.reason_writes import write_reason_decision
from cairn.dispatcher.tasks import conclude_writer, reason_writes
from cairn.server import db
from cairn.server.routers import intents as intents_router
from cairn.server.schemas.projects import CreateIntentRequest, DepthReviewRequest
from cairn.server.services.structured_intent_bindings import (
    issue_structured_intent_binding,
)
from cairn.server.services.time_utils import utcnow
from test_web_assessment_phases import seed_project


DIGEST = "b" * 64


@pytest.fixture(autouse=True)
def graph_write_sleeps(monkeypatch):
    sleeps = []
    monkeypatch.setattr(conclude_writer.time, "sleep", sleeps.append)
    return sleeps


class FakeContainerManager:
    def __init__(self):
        self.files = {}

    def write_text_file(self, container_name, path, content):
        self.files[(container_name, path)] = content


class FakeCoverageClient:
    def __init__(self, slots):
        self.slots = slots

    def list_coverage_slots(self, project_id):
        assert project_id == "p1"
        return ApiResult(200, self.slots)

    def list_scenarios(self):
        raise AssertionError("Reason must not read mutable Scenario features")


def execution_view(**updates):
    return {
        "version": 2,
        "enabled": True,
        "executable": True,
        "execution_contract_version": 1,
        "execution_engine": "pi",
        "contract_version": 2,
        "structured": True,
        "surface_manifest_enabled": True,
        "minimum_coverage_enabled": True,
        "independent_verifier_enabled": True,
        "phase": "depth_review",
        "depth_review_fact_id": None,
        "review_input_digest": DIGEST,
        "verification_reserve": 2,
        "budget": 10,
        "deadline": "2026-10-01T00:00:00Z",
        "attempts_started": 6,
        "verification_attempts_started": 1,
        "remaining_total": 4,
        "remaining_discovery": 3,
        "current_tasks": [],
        **updates,
    }


def open_intent(intent_id, **updates):
    return SimpleNamespace(
        **{
            "id": intent_id,
            "from_": ["f1"],
            "description": "Check authorization boundary",
            "worker": None,
            "to": None,
            "concluded_at": None,
            "assessment_role": "discovery",
            "target_observation_id": None,
            "slot_key": "cell-authorization",
            "strategy_key": "cross-account",
            "depth_review_fact_id": None,
            "depends_on": None,
            **updates,
        }
    )


def project_with_execution(execution, intents=()):
    return SimpleNamespace(
        project=SimpleNamespace(
            id="p1",
            scenario_key="mutable-scenario",
            assessment_protocol="old-value-must-not-control-execution",
            scenario_snapshot={"features": {"surface_manifest_version": 0}},
            assessment_execution=execution,
        ),
        facts=[SimpleNamespace(id="f1"), SimpleNamespace(id="goal")],
        intents=list(intents),
    )


def build_context(
    project,
    *,
    slots=(),
    max_intents=5,
    admission_context=None,
):
    manager = FakeContainerManager()
    context = build_reason_prompt_context(
        SimpleNamespace(tasks=SimpleNamespace(reason=SimpleNamespace(max_intents=max_intents))),
        manager,
        "container-p1",
        project,
        "facts: [large-history-body-not-in-short-context]",
        client=FakeCoverageClient(list(slots)),
        admission_context=admission_context,
    )
    return context, manager


def test_reason_context_binds_frozen_execution_and_terminal_depth_cells():
    frozen = execution_view()
    context, manager = build_context(
        project_with_execution(frozen),
        slots=[{
            "slot_key": "terminal-cell",
            "status": "completed",
            "surface_key": "/orders",
            "test_family": "authorization",
            "resulting_fact_id": "f1",
            "result": "tested_no_finding",
        }],
    )
    assert context.binding == {
        "max_intents": 3,
        "allowed_fact_ids": ["f1"],
        "review_input_digest": DIGEST,
    }
    assert context.replacements["max_intents"] == "3"
    situation = json.loads(context.replacements["assessment_execution"])
    assert situation == {**frozen, "selected_depth_obligations": []}
    assert "terminal-cell" in context.replacements["coverage_slots"]
    assert "eligible_cells" in context.replacements["coverage_slots"]
    assert "discovered_surfaces" in context.replacements["surface_discovery_contract"]
    assert "large-history-body" not in context.replacements["graph_yaml"]
    assert len(manager.files) == 1
    (container_name, path), content = next(iter(manager.files.items()))
    assert container_name == "container-p1"
    assert path in context.replacements["graph_yaml"]
    assert content == "facts: [large-history-body-not-in-short-context]"
    assert "read the entire file" not in context.replacements["graph_yaml"].lower()
    assert frozen == execution_view()


def test_reason_depth_context_keeps_a_lower_configured_intent_limit():
    context, _ = build_context(project_with_execution(execution_view()), max_intents=1)
    assert context.binding["max_intents"] == 1
    assert context.replacements["max_intents"] == "1"


def test_reason_context_projects_explicit_roles_and_selected_obligations():
    intents = [
        open_intent("verifier", assessment_role="verifier", target_observation_id="obs1"),
        open_intent("deep", depth_review_fact_id="f-review", depends_on="verifier"),
        open_intent("not-selected", strategy_key="deep-but-no-review-reference"),
        open_intent("closed", depth_review_fact_id="f-review", concluded_at="now"),
        open_intent("connected", to="f2"),
    ]
    context, _ = build_context(project_with_execution(
        execution_view(phase="deep", review_input_digest=None, depth_review_fact_id="f-review"),
        intents,
    ))
    assert [item["id"] for item in context.open_intents] == ["verifier", "deep", "not-selected"]
    assert context.open_intents[0]["assessment_role"] == "verifier"
    assert context.open_intents[0]["target_observation_id"] == "obs1"
    deep = context.open_intents[1]
    assert deep["slot_key"] == "cell-authorization"
    assert deep["strategy_key"] == "cross-account"
    assert deep["depth_review_fact_id"] == "f-review"
    assert deep["depends_on"] == "verifier"
    assert json.loads(context.replacements["assessment_execution"])["selected_depth_obligations"] == [deep]
    assert context.binding["max_intents"] == 5
    assert "review_input_digest" not in context.binding


@pytest.mark.parametrize("verifier", [True, False])
def test_reason_context_uses_resolved_flags_without_protocol_name_guessing(verifier):
    project = project_with_execution(execution_view(
        structured=False, enabled=False, phase=None, review_input_digest=None,
        independent_verifier_enabled=verifier, surface_manifest_enabled=False,
    ))
    project.project.assessment_protocol = "default-copy-v1"
    context, _ = build_context(project, slots=[{"slot_key": "cell1", "status": "pending"}])
    assert context.replacements["coverage_slots"].startswith(f"verifier={'on' if verifier else 'off'}")
    assert "eligible_cells" not in context.replacements["coverage_slots"]


@pytest.mark.parametrize("execution", [
    None, {}, execution_view(version=1), execution_view(executable=False),
    execution_view(execution_engine="legacy"), execution_view(execution_contract_version=0),
])
def test_reason_context_rejects_historical_execution_without_writing_files(execution):
    manager = FakeContainerManager()
    with pytest.raises(ValueError, match="assessment_execution"):
        build_reason_prompt_context(
            SimpleNamespace(tasks=SimpleNamespace(reason=SimpleNamespace(max_intents=3))),
            manager, "container-p1", project_with_execution(execution), "facts: []",
        )
    assert manager.files == {}


@pytest.mark.parametrize("digest", [None, "", "stale-not-a-digest", "A" * 64])
def test_reason_depth_context_requires_server_review_digest(digest):
    with pytest.raises(ValueError, match="review_input_digest"):
        build_context(project_with_execution(execution_view(review_input_digest=digest)))


class FakeDepthClient(CairnClient):
    def __init__(self, response=None):
        super().__init__("http://fake-server.invalid", token="test-only-token")
        self.calls = []
        self.response = response or ApiResult(200, {
            "fact": {"id": "f-review"}, "intents": [{"id": "i-deep"}],
        })

    def _request_json(self, method, path, json=None):
        self.calls.append((method, path, deepcopy(json)))
        if path.endswith("/reason/depth-review"):
            DepthReviewRequest.model_validate(json)
            return self.response
        if path.endswith("/agent-events"):
            return ApiResult(200, {})
        raise AssertionError(f"Unexpected non-atomic write: {method} {path}")


def depth_decision():
    return {
        "input_digest": DIGEST,
        "description": "Compare access using a second account",
        "evidence_refs": [{"artifact_handle": "baseline", "pointer": "request"}],
        "selected": [{
            "from": ["f1"],
            "description": "Check cross-account order access",
            "slot_key": "cell-authorization",
            "strategy_key": "cross-account",
            "hypothesis": "Order access does not check ownership",
            "baseline": "Owner can read order",
            "variants": ["Other account reads same order"],
            "controls": ["Other account reads own order"],
            "evidence_refs": [
                {"artifact_handle": "baseline", "pointer": "response", "verifier_run_id": "run-evidence"},
                {"artifact_id": "a7", "pointer": "existing-observation"},
            ],
        }],
    }


def write_depth(client, decision, *, task_run_id="reason-run", declared=None, **updates):
    return write_reason_decision(
        client, "p1", SimpleNamespace(name="reason-worker"), task_run_id,
        kind="depth_review", data=decision,
        payload={"data": {"depth_review": decision, "rationale": "Prioritize authorization"}},
        evidence=ProcessArtifactRecord(refs=[]),
        declared_artifacts=declared or ProcessArtifactRecord(
            refs=[], artifact_ids_by_handle={"baseline": "a42"},
        ),
        execute_ms=10, total_ms=20, **updates,
    )


def test_depth_review_atomically_submits_resolved_evidence_without_abandon():
    client = FakeDepthClient()
    decision = depth_decision()
    original = deepcopy(decision)
    assert write_depth(client, decision, abandon=["must-not-abandon"], surface_manifest=True) == "success"
    method, path, body = client.calls[0]
    assert (method, path) == ("POST", "/projects/p1/reason/depth-review")
    assert body["worker"] == "reason-worker"
    assert body["task_run_id"] == "reason-run"
    assert body["input_digest"] == DIGEST
    assert body["evidence_refs"] == [{"artifact_id": "a42", "pointer": "request"}]
    assert body["selected"][0]["evidence_refs"] == [
        {"artifact_id": "a42", "pointer": "response", "verifier_run_id": "run-evidence"},
        {"artifact_id": "a7", "pointer": "existing-observation"},
    ]
    assert "artifact_handle" not in json.dumps(body)
    assert decision == original
    assert len(client.calls) == 3
    audit = client.calls[-1][2]["payload"]
    assert audit["outcome"] == "depth_review"
    assert audit["write_status"] == "success"
    assert audit["created_intent_ids"] == ["i-deep"]
    assert audit["completed_fact_id"] == "f-review"


def test_zero_selected_depth_review_is_successful_accepted_decision():
    client = FakeDepthClient(ApiResult(200, {"fact": {"id": "f-review"}, "intents": []}))
    decision = depth_decision()
    decision["selected"] = []
    assert write_depth(client, decision) == "success"
    assert client.calls[0][2]["selected"] == []
    assert client.calls[-1][2]["payload"]["write_status"] == "success"
    assert client.calls[-1][2]["payload"]["created_intent_ids"] == []


@pytest.mark.parametrize("status,expected", [
    (0, "server_write_failed"),
    (403, "policy_denied"),
    (409, "business_acceptance"),
    (422, "business_acceptance"),
    (500, "server_write_failed"),
])
def test_depth_review_rejection_is_failed_not_success_or_skipped(status, expected):
    client = FakeDepthClient(ApiResult(status, text="depth_review_input_stale"))
    assert write_depth(client, depth_decision()) == expected
    writes = [call for call in client.calls if call[1].endswith("/reason/depth-review")]
    assert len(writes) == (3 if status in {0, 500} else 1)
    assert all(call == writes[0] for call in writes)
    assert client.calls[-2][2]["payload"]["status"] == "failed"
    audit = client.calls[-1][2]["payload"]
    assert audit["write_status"] == "failed"
    assert audit["created_intent_ids"] == []
    assert "completed_fact_id" not in audit


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("handle", ["unknown", "a7"])
def test_unresolved_depth_evidence_prevents_all_server_writes(nested, handle):
    client = FakeDepthClient()
    decision = depth_decision()
    section = decision["selected"][0] if nested else decision
    section["evidence_refs"] = [{"artifact_handle": handle}]
    with pytest.raises(UnresolvedArtifactEvidenceError, match="unresolved artifact handle"):
        write_depth(client, decision)
    assert client.calls == []


def test_depth_review_explicit_artifact_id_is_not_reinterpreted_as_handle():
    client = FakeDepthClient()
    declared = ProcessArtifactRecord(refs=[], artifact_ids_by_handle={"baseline": "a42", "a7": "a99"})
    assert write_depth(client, depth_decision(), declared=declared) == "success"
    assert client.calls[0][2]["selected"][0]["evidence_refs"][1]["artifact_id"] == "a7"


def test_depth_review_without_task_run_cannot_submit():
    client = FakeDepthClient()
    assert write_depth(client, depth_decision(), task_run_id=None) == "request_contract"
    assert client.calls == []


def test_depth_review_uses_shared_decision_schema_before_server_writes():
    client = FakeDepthClient()
    decision = depth_decision()
    decision["selected"] *= 4
    with pytest.raises(ValidationError):
        write_depth(client, decision)
    assert client.calls == []


class FakeCompleteClient(FakeDepthClient):
    def _request_json(self, method, path, json=None):
        if path.endswith("/complete"):
            self.calls.append((method, path, deepcopy(json)))
            return self.response
        return super()._request_json(method, path, json=json)


@pytest.mark.parametrize("status,expected", [
    (200, "success"),
    (403, "policy_denied"),
    (409, "business_acceptance"),
    (500, "server_write_failed"),
])
def test_complete_requires_server_acceptance_for_success(status, expected):
    client = FakeCompleteClient(ApiResult(status, text="completion_gate_rejected"))
    complete = {"from": ["f1"], "description": "All obligations have been addressed"}
    outcome = write_reason_decision(
        client, "p1", SimpleNamespace(name="reason-worker"), "reason-run",
        kind="complete", data=complete, payload={"data": {"complete": complete}},
        evidence=ProcessArtifactRecord(refs=[]),
        declared_artifacts=ProcessArtifactRecord(refs=[]),
        execute_ms=10, total_ms=20,
    )
    accepted = status == 200
    assert outcome == expected
    assert len(client.calls) == (5 if status == 500 else 3)
    writes = [call for call in client.calls if call[1].endswith("/complete")]
    assert all(call == writes[0] for call in writes)
    assert client.calls[0][:2] == ("POST", "/projects/p1/complete")
    protocol_audit = client.calls[-2][2]["payload"]
    decision_audit = client.calls[-1][2]["payload"]
    assert protocol_audit["status"] == ("success" if accepted else "failed")
    assert decision_audit["write_status"] == ("success" if accepted else "failed")
    assert decision_audit["outcome"] == "complete"
    if accepted:
        assert decision_audit["completed_fact_id"] == "goal"
    else:
        assert "fact_id" not in protocol_audit
        assert "completed_fact_id" not in decision_audit
        assert decision_audit["error_message"] == "complete_not_accepted"


@pytest.mark.parametrize("status", [0, 408, 429, 500, 503, 599])
def test_graph_write_transient_retry_is_bounded(status, graph_write_sleeps):
    calls = []
    response = ApiResult(status, text="synthetic transient failure")

    def operation():
        calls.append(True)
        return response

    assert conclude_writer.retry_graph_write(operation) is response
    assert len(calls) == 3
    assert graph_write_sleeps == [0.5, 1.0]


@pytest.mark.parametrize("status", [200, 400, 403, 404, 409, 422, 600])
def test_graph_write_does_not_retry_acceptance_or_permanent_rejection(status, graph_write_sleeps):
    calls = []
    response = ApiResult(status)

    def operation():
        calls.append(True)
        return response

    assert conclude_writer.retry_graph_write(operation) is response
    assert len(calls) == 1
    assert graph_write_sleeps == []


class FakeIntentClient(FakeDepthClient):
    def __init__(self, responses):
        super().__init__()
        self.responses = iter(responses)

    def _request_json(self, method, path, json=None):
        if path.endswith("/intents"):
            CreateIntentRequest.model_validate(json)
            self.calls.append((method, path, deepcopy(json)))
            return next(self.responses)
        return super()._request_json(method, path, json=json)


def intent_decisions():
    return [
        {
            "from": ["origin"],
            "description": "Establish baseline",
            "slot_key": "cell-0",
            "strategy_key": "authorization.owner-baseline-v1",
        },
        {
            "from": ["origin"],
            "description": "Compare authorized state",
            "slot_key": "cell-1",
            "strategy_key": "authorization.owner-vs-other-v1",
            "depends_on": "@1",
        },
    ]


def write_intents(client, intents=None, *, task_run_id="reason-run", project_id="p1"):
    decisions = intents if intents is not None else intent_decisions()
    return write_reason_decision(
        client, project_id, SimpleNamespace(name="reasoner"), task_run_id,
        kind="intents", data=decisions, payload={"data": {"intents": decisions}},
        evidence=ProcessArtifactRecord(refs=[]), declared_artifacts=ProcessArtifactRecord(refs=[]),
        execute_ms=10, total_ms=20,
    )


def test_reason_intent_retry_preserves_entry_identity_and_dependency(graph_write_sleeps):
    client = FakeIntentClient([
        ApiResult(0), ApiResult(201, {"id": "i1"}), ApiResult(201, {"id": "i2"}),
    ])
    assert write_intents(client) == "success"
    writes = [call[2] for call in client.calls if call[1].endswith("/intents")]
    assert writes[0] == writes[1]
    assert [body["submission_index"] for body in writes] == [0, 0, 1]
    assert {body["task_run_id"] for body in writes} == {"reason-run"}
    assert writes[2]["depends_on"] == "i1"
    assert graph_write_sleeps == [0.5]
    assert client.calls[-1][2]["payload"]["created_intent_ids"] == ["i1", "i2"]


@pytest.mark.parametrize("status,expected", [
    (403, "policy_denied"),
    (409, "business_acceptance"),
    (422, "business_acceptance"),
    (500, "server_write_failed"),
])
@pytest.mark.parametrize("accepted_prefix", [False, True])
def test_reason_intent_rejection_never_reports_success_or_skips(status, expected, accepted_prefix):
    responses = [ApiResult(201, {"id": "i1"})] if accepted_prefix else []
    responses += [ApiResult(status, text="synthetic rejection")] * (3 if status == 500 else 1)
    client = FakeIntentClient(responses)
    assert write_intents(client) == expected
    writes = [call[2] for call in client.calls if call[1].endswith("/intents")]
    assert len(writes) == int(accepted_prefix) + (3 if status == 500 else 1)
    assert writes[-1]["submission_index"] == int(accepted_prefix)
    protocol = client.calls[-2][2]["payload"]
    assert protocol["status"] == "failed"
    decision = client.calls[-1][2]["payload"]
    assert decision["write_status"] == ("partial" if accepted_prefix else "failed")
    assert decision["created_intent_ids"] == (["i1"] if accepted_prefix else [])


@pytest.mark.parametrize("dependency", ["@0", "@2", "@99", ["@1"], "", 42])
def test_reason_invalid_dependency_is_not_dropped_or_guessed(dependency):
    client = FakeIntentClient([])
    decisions = intent_decisions()
    decisions[1]["depends_on"] = dependency
    assert write_intents(client, decisions) == "request_contract"
    assert not any(call[1].endswith("/intents") for call in client.calls)
    assert client.calls[-1][2]["payload"]["error_message"] == "intent_dependency_invalid"


def test_reason_existing_dependency_is_preserved_verbatim():
    client = FakeIntentClient([ApiResult(201, {"id": "i2"})])
    assert write_intents(client, [{"from": ["origin"], "description": "Check state", "depends_on": "existing-i1"}]) == "success"
    assert client.calls[0][2]["depends_on"] == "existing-i1"


@pytest.mark.parametrize("body", [None, {}, {"id": ""}, {"id": 1}])
def test_reason_create_without_accepted_id_fails_before_dependent_write(body):
    client = FakeIntentClient([ApiResult(201, body)])
    assert write_intents(client) == "business_acceptance"
    assert len([call for call in client.calls if call[1].endswith("/intents")]) == 1
    assert client.calls[-1][2]["payload"]["write_status"] == "failed"


def test_reason_intent_submission_requires_task_run():
    client = FakeIntentClient([])
    assert write_intents(client, task_run_id=None) == "request_contract"
    assert client.calls == []


def seed_submission_binding(connection):
    now = utcnow()
    for index in range(4):
        connection.execute(
            """
            INSERT INTO coverage_slots (
                project_id, slot_key, kind, description, status, tier,
                surface_key, surface_kind, test_family, coverage_requirement,
                applicability_basis, completion_criteria, created_at, updated_at
            ) VALUES ('p', ?, 'endpoint', 'synthetic', 'pending', 'core', ?,
                      'endpoint', 'authorization', 'required',
                      'authorized identity boundary', 'compare owner and non-owner', ?, ?)
            """,
            (
                f"cell-{index}",
                f"GET https://example.test/resource/{index}",
                now,
                now,
            ),
        )
    project = connection.execute("SELECT * FROM projects WHERE id='p'").fetchone()
    return now, issue_structured_intent_binding(connection, project, issued_at=now)


@pytest.fixture
def intent_submission_db(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    db.run_migrations(connection, "sqlite")
    seed_project(connection)
    now, binding = seed_submission_binding(connection)
    connection.execute(
        "INSERT INTO task_runs (id,project_id,task_type,worker_name,worker_type,status,started_at,runtime_context_json) VALUES ('reason-run','p','reason','reasoner','pi','running',?,?)",
        (
            now,
            json.dumps(
                {
                    "server_note": "preserved",
                    "structured_intent_binding": binding,
                }
            ),
        ),
    )
    connection.commit()

    @contextmanager
    def get_conn():
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    monkeypatch.setattr(intents_router, "get_conn", get_conn)
    yield connection
    connection.close()


def create_body(**updates):
    submission_index = updates.get("submission_index", 0)
    slot_index = submission_index if type(submission_index) is int and submission_index >= 0 else 0
    return CreateIntentRequest.model_validate({
        "from": ["origin"], "description": "Check authorized boundary", "creator": "reasoner",
        "task_run_id": "reason-run", "submission_index": 0,
        "slot_key": f"cell-{slot_index}", "strategy_key": "authorization.owner-vs-other-v1",
        **updates,
    })


def receipt_context(conn):
    return json.loads(conn.execute("SELECT runtime_context_json FROM task_runs WHERE id='reason-run'").fetchone()[0])


def test_reason_task_run_admission_freezes_server_owned_cell_binding(
    intent_submission_db,
):
    from cairn.server.auth import RequestPrincipal
    from cairn.server.models import CreateTaskRunRequest
    from cairn.server.services.audit_task_runs import insert_task_run_record

    conn = intent_submission_db
    run = insert_task_run_record(
        conn,
        "p",
        CreateTaskRunRequest(
            task_type="reason",
            worker_name="reasoner",
            worker_type="pi",
            status="running",
        ),
        principal=RequestPrincipal(kind="machine", subject_id="reasoner"),
    )

    binding = run.runtime_context["structured_intent_binding"]
    assert binding["assessment_phase"] == "baseline"
    assert [cell["slot_key"] for cell in binding["eligible_cells"]] == [
        "cell-0",
        "cell-1",
        "cell-2",
        "cell-3",
    ]
    assert len(binding["digest"]) == 64


@pytest.mark.parametrize(
    "updates",
    [
        {"slot_key": None},
        {"strategy_key": ""},
        {"slot_key": "outside-frozen-binding"},
    ],
)
def test_structured_reason_rejection_has_no_partial_writes_or_receipt(
    intent_submission_db,
    updates,
):
    conn = intent_submission_db

    with pytest.raises(HTTPException) as denied:
        intents_router.create_intent("p", create_body(**updates))

    assert denied.value.status_code == 422
    assert conn.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM intent_sources").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM coverage_slots WHERE assigned_intent_id IS NOT NULL"
    ).fetchone()[0] == 0
    assert "accepted_intent_submissions" not in receipt_context(conn)


def test_intent_creation_receipt_is_atomic_and_replays_original_id(intent_submission_db):
    conn = intent_submission_db
    body = create_body()
    first = intents_router.create_intent("p", body)
    replay = intents_router.create_intent("p", body)
    assert replay == first
    assert conn.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM intent_sources").fetchone()[0] == 1
    assigned = conn.execute(
        "SELECT assigned_intent_id FROM coverage_slots WHERE project_id='p' AND slot_key='cell-0'"
    ).fetchone()
    assert assigned["assigned_intent_id"] == first.id
    context = receipt_context(conn)
    assert context["server_note"] == "preserved"
    receipt = context["accepted_intent_submissions"]["0"]
    assert receipt["intent_id"] == first.id
    assert len(receipt["digest"]) == 64


@pytest.mark.parametrize("change", [
    {"description": "Different test"}, {"from": ["baseline"]}, {"depends_on": "missing"},
    {"strategy_key": "different"}, {"slot_key": "different"}, {"worker": "reasoner"},
])
def test_intent_same_run_index_conflicting_content_is_rejected(intent_submission_db, change):
    first = intents_router.create_intent("p", create_body())
    with pytest.raises(HTTPException) as exc:
        intents_router.create_intent("p", create_body(**change))
    assert (exc.value.status_code, exc.value.detail) == (409, "intent_submission_conflict")
    assert intents_router.create_intent("p", create_body()).id == first.id
    assert intent_submission_db.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 1


@pytest.mark.parametrize("status,phase", [("stopped", "baseline"), ("completed", "finalizing"), ("active", "depth_review")])
def test_intent_receipt_replays_after_project_phase_or_run_closes(intent_submission_db, status, phase):
    conn = intent_submission_db
    first = intents_router.create_intent("p", create_body())
    conn.execute("UPDATE projects SET status=?,assessment_progress_json=? WHERE id='p'", (status, json.dumps({"version": 1, "phase": phase})))
    conn.execute("UPDATE task_runs SET status='succeeded' WHERE id='reason-run'")
    conn.commit()
    assert intents_router.create_intent("p", create_body()).id == first.id
    with pytest.raises(HTTPException) as exc:
        intents_router.create_intent("p", create_body(submission_index=1))
    assert exc.value.status_code == 409
    assert conn.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 1


def test_intent_dependency_is_validated_and_returned(intent_submission_db):
    conn = intent_submission_db
    first = intents_router.create_intent("p", create_body())
    second_body = create_body(submission_index=1, depends_on=first.id)
    second = intents_router.create_intent("p", second_body)
    assert second.depends_on == first.id
    assert intents_router.create_intent("p", second_body).depends_on == first.id
    with pytest.raises(HTTPException) as exc:
        intents_router.create_intent("p", create_body(submission_index=2, depends_on="nonexistent"))
    assert exc.value.status_code == 404
    assert set(receipt_context(conn)["accepted_intent_submissions"]) == {"0", "1"}
    assert conn.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 2


def test_intent_receipt_rolls_back_with_creation_failure(intent_submission_db, monkeypatch):
    conn = intent_submission_db

    def fail_slot(*args):
        raise HTTPException(409, "synthetic slot collision")

    monkeypatch.setattr(intents_router, "assign_slot_intent", fail_slot)
    with pytest.raises(HTTPException, match="synthetic slot collision"):
        intents_router.create_intent("p", create_body(slot_key="cell-0"))
    assert conn.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM intent_sources").fetchone()[0] == 0
    assert "accepted_intent_submissions" not in receipt_context(conn)


@pytest.mark.parametrize("change,detail", [
    ({"creator": "other"}, "intent_submission_run_mismatch"),
    ({"submission_index": None}, "intent_submission_index_required"),
    ({"task_run_id": None}, "intent_submission_requires_reason_task_run"),
])
def test_intent_receipt_rejects_unbound_identity(intent_submission_db, change, detail):
    with pytest.raises(HTTPException) as exc:
        intents_router.create_intent("p", create_body(**change))
    assert exc.value.detail == detail
    assert intent_submission_db.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 0


@pytest.mark.parametrize("index", [-1, True, "0", 1.0])
def test_intent_submission_index_requires_nonnegative_strict_integer(index):
    with pytest.raises(ValidationError):
        create_body(submission_index=index)


def test_reason_lost_create_response_reuses_server_receipt(intent_submission_db):
    class LostResponseClient(FakeDepthClient):
        def __init__(self):
            super().__init__()
            self.lost = False

        def _request_json(self, method, path, json=None):
            if path.endswith("/intents"):
                self.calls.append((method, path, deepcopy(json)))
                accepted = intents_router.create_intent("p", CreateIntentRequest.model_validate(json))
                if not self.lost:
                    self.lost = True
                    return ApiResult(0, text="response lost after commit")
                return ApiResult(201, accepted.model_dump(by_alias=True))
            return super()._request_json(method, path, json=json)

    client = LostResponseClient()
    assert write_intents(client, project_id="p") == "success"
    conn = intent_submission_db
    assert conn.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 2
    receipts = receipt_context(conn)["accepted_intent_submissions"]
    second = conn.execute("SELECT depends_on FROM intents WHERE id=? AND project_id='p'", (receipts["1"]["intent_id"],)).fetchone()
    assert second[0] == receipts["0"]["intent_id"]
    writes = [call for call in client.calls if call[1].endswith("/intents")]
    assert len(writes) == 3
    assert writes[0] == writes[1]


@pytest.mark.parametrize("field,value", [("task_type", "explore"), ("worker_type", "claudecode"), ("status", "failed")])
def test_intent_receipt_rejects_non_reason_legacy_or_terminal_run(intent_submission_db, field, value):
    conn = intent_submission_db
    conn.execute(f"UPDATE task_runs SET {field}=? WHERE id='reason-run'", (value,))
    conn.commit()
    with pytest.raises(HTTPException) as exc:
        intents_router.create_intent("p", create_body())
    assert (exc.value.status_code, exc.value.detail) == (409, "intent_submission_run_mismatch")
    assert conn.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 0


def test_intent_receipt_cannot_cross_project_boundary(intent_submission_db):
    conn = intent_submission_db
    conn.execute(
        "INSERT INTO projects (id,title,status,created_at,scenario_key,assessment_protocol,scenario_snapshot_json,assessment_progress_json,explore_budget,explore_deadline) SELECT 'other',title,status,created_at,scenario_key,assessment_protocol,scenario_snapshot_json,assessment_progress_json,explore_budget,explore_deadline FROM projects WHERE id='p'"
    )
    conn.commit()
    with pytest.raises(HTTPException) as exc:
        intents_router.create_intent("other", create_body())
    assert exc.value.status_code == 404
    assert conn.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 0


def test_reason_complete_requires_task_run_before_retry():
    client = FakeCompleteClient()
    assert write_reason_decision(
        client, "p1", SimpleNamespace(name="reasoner"), None,
        kind="complete", data={"from": ["origin"], "description": "Complete"}, payload={},
        evidence=ProcessArtifactRecord(refs=[]), declared_artifacts=ProcessArtifactRecord(refs=[]),
        execute_ms=1, total_ms=2,
    ) == "request_contract"
    assert client.calls == []


@pytest.mark.parametrize("conflicting", [False, True])
def test_sqlite_concurrent_intent_submission_is_atomic(tmp_path, monkeypatch, conflicting):
    database = tmp_path / "intent-submissions.sqlite"
    setup = sqlite3.connect(database)
    setup.row_factory = sqlite3.Row
    db.run_migrations(setup, "sqlite")
    seed_project(setup)
    now, binding = seed_submission_binding(setup)
    setup.execute(
        "INSERT INTO task_runs (id,project_id,task_type,worker_name,worker_type,status,started_at,runtime_context_json) VALUES ('reason-run','p','reason','reasoner','pi','running',?,?)",
        (
            now,
            json.dumps({"structured_intent_binding": binding}),
        ),
    )
    setup.commit()
    setup.close()

    @contextmanager
    def get_conn():
        conn = sqlite3.connect(database, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    monkeypatch.setattr(intents_router, "get_conn", get_conn)
    barrier = Barrier(2)

    def submit(index):
        body = create_body(description=f"Conflict {index}") if conflicting else create_body()
        barrier.wait(timeout=10)
        try:
            return intents_router.create_intent("p", body).id
        except HTTPException as exc:
            return exc.status_code, exc.detail

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, range(2)))
    if conflicting:
        assert len([result for result in results if isinstance(result, str)]) == 1
        assert (409, "intent_submission_conflict") in results
    else:
        assert isinstance(results[0], str)
        assert results[0] == results[1]
    with get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM intent_sources").fetchone()[0] == 1
        assert len(receipt_context(conn)["accepted_intent_submissions"]) == 1


class FakeAuxiliaryClient(FakeDepthClient):
    def __init__(self, *, abandon_responses=(), auxiliary_response=None):
        super().__init__()
        self.abandon_responses = iter(abandon_responses)
        self.auxiliary_response = auxiliary_response or ApiResult(200, {})
        self.operations = []

    def abandon_intent(self, project_id, intent_id, worker, *, task_run_id=None):
        self.operations.append(("abandon", intent_id))
        return next(self.abandon_responses)

    def register_discovered_surfaces(self, project_id, surfaces, **kwargs):
        self.operations.append(("surfaces", surfaces))
        return self.auxiliary_response

    def upsert_coverage_slots(self, project_id, slots, **kwargs):
        self.operations.append(("slots", slots))
        return self.auxiliary_response

    def create_intent(self, *args, **kwargs):
        self.operations.append(("create", kwargs))
        return ApiResult(201, {"id": "accepted-intent"})

    def complete_with_evidence(self, *args, **kwargs):
        self.operations.append(("complete", kwargs))
        return ApiResult(200, {"id": "accepted-completion"})


def write_with_auxiliary(
    client,
    kind,
    *,
    abandon=None,
    surface_manifest=False,
    priority=1,
):
    item = {
        "from": ["origin"],
        "description": "Authorized follow-up",
        "priority": priority,
    }
    data = [item] if kind == "intents" else item
    return write_reason_decision(
        client, "p1", SimpleNamespace(name="reasoner"), "reason-run",
        kind=kind, data=data, payload={"data": {kind: data}},
        evidence=ProcessArtifactRecord(refs=[]), declared_artifacts=ProcessArtifactRecord(refs=[]),
        execute_ms=10, total_ms=20, abandon=abandon, surface_manifest=surface_manifest,
    )


def test_reason_intent_write_preserves_goal_driven_priority():
    client = FakeAuxiliaryClient()

    assert write_with_auxiliary(client, "intents", priority=2) == "success"

    create = next(operation for operation in client.operations if operation[0] == "create")
    assert create[1]["priority"] == 2


@pytest.mark.parametrize("kind", ["intents", "complete"])
@pytest.mark.parametrize("status,expected", [
    (0, "server_write_failed"),
    (403, "policy_denied"),
    (409, "business_acceptance"),
    (500, "server_write_failed"),
])
@pytest.mark.parametrize("accepted_prefix", [False, True])
def test_reason_failed_abandon_blocks_graph_writes_without_retry(kind, status, expected, accepted_prefix, graph_write_sleeps):
    responses = ([ApiResult(200, {})] if accepted_prefix else []) + [ApiResult(status, text="not accepted")]
    intent_ids = (["already-accepted"] if accepted_prefix else []) + ["rejected-intent"]
    client = FakeAuxiliaryClient(abandon_responses=responses)
    assert write_with_auxiliary(client, kind, abandon=intent_ids) == expected
    assert client.operations == [("abandon", iid) for iid in intent_ids]
    assert graph_write_sleeps == []
    protocol = client.calls[-2][2]["payload"]
    assert protocol["operation"] == "abandon_intent"
    assert protocol["status"] == "failed"
    summary = client.calls[-1][2]["payload"]
    assert summary["outcome"] == "abandon"
    assert summary["write_status"] == ("partial" if accepted_prefix else "failed")
    assert summary["error_message"] == "未接受的 Intent 数量: 1"
    if accepted_prefix:
        assert client.calls[0][2]["payload"]["status"] == "success"


@pytest.mark.parametrize("kind", ["intents", "complete"])
@pytest.mark.parametrize("surface_manifest", [False, True])
@pytest.mark.parametrize("status,expected", [
    (0, "server_write_failed"),
    (403, "policy_denied"),
    (409, "business_acceptance"),
    (500, "server_write_failed"),
])
def test_reason_rejected_surface_or_slot_blocks_graph_writes(monkeypatch, kind, surface_manifest, status, expected, graph_write_sleeps):
    monkeypatch.setattr(reason_writes, "build_discovered_surfaces_for_conclude", lambda *args: [{"surface_key": "synthetic"}])
    monkeypatch.setattr(reason_writes, "extract_coverage_slots", lambda *args, **kwargs: [{"key": "synthetic"}])
    client = FakeAuxiliaryClient(auxiliary_response=ApiResult(status, text="not accepted"))
    assert write_with_auxiliary(client, kind, surface_manifest=surface_manifest) == expected
    expected_calls = 3 if status in {0, 500} else 1
    assert len(client.operations) == expected_calls
    assert {operation[0] for operation in client.operations} == {"surfaces" if surface_manifest else "slots"}
    assert not any(operation[0] in {"create", "complete"} for operation in client.operations)
    assert graph_write_sleeps == ([0.5, 1.0] if expected_calls == 3 else [])
    assert client.calls[-1][2]["payload"]["status"] == "failed"


@pytest.mark.parametrize("kind", ["intents", "complete"])
def test_reason_accepted_abandon_can_continue_to_graph_write(kind):
    client = FakeAuxiliaryClient(abandon_responses=[ApiResult(200, {})])
    assert write_with_auxiliary(client, kind, abandon=["accepted-abandon"]) == "success"
    assert [operation[0] for operation in client.operations] == ["abandon", "create" if kind == "intents" else "complete"]
    abandon_summary = client.calls[1][2]["payload"]
    assert abandon_summary["outcome"] == "abandon"
    assert abandon_summary["write_status"] == "success"
