"""唯一 pi 信封与 Python 导出工具契约的故障注入。"""

from hashlib import sha256
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cairn.assessment_contracts import AssessmentEvidenceRef, GOAL_DRIVEN_PROTOCOL
from cairn.dispatcher.contracts import (
    validate_bootstrap_execute_payload,
    validate_reason_payload,
    validate_report_payload,
)
from cairn.dispatcher.worker_protocol.submission import (
    PHASE_DATA_MODELS, build_submit_result_schema, exported_contracts,
    validate_healthcheck_payload, validate_phase_data,
)
from cairn.dispatcher.worker_result_gateway import decode_pi_result, worker_failure_error_type

SESSION = "pi:07b53667-a6ab-44be-badb-e3ed6b347331"


def structured_intent_binding(*, empty: bool = False):
    cells = [] if empty else [
        {
            "slot_key": "cell-a",
            "surface_kind": "endpoint",
            "surface_key": "GET /a",
            "test_family": "authorization",
            "applicability_basis": "owner boundary",
            "completion_criteria": "compare owner and non-owner",
            "coverage_requirement": "required",
            "tier": "core",
            "strategy": {"mode": "any"},
        },
        {
            "slot_key": "cell-b",
            "surface_kind": "endpoint",
            "surface_key": "POST /b",
            "test_family": "business_logic",
            "applicability_basis": "state transition",
            "completion_criteria": "compare permitted and denied states",
            "coverage_requirement": "optional",
            "tier": "extended",
            "strategy": {
                "mode": "must_differ",
                "excluded_strategy_key": "blocked.v1",
            },
        },
    ]
    payload = {
        "version": 1,
        "assessment_phase": "baseline",
        "issued_at": "2026-09-19T08:00:00Z",
        "eligible_cells": cells,
    }
    digest = sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {**payload, "digest": digest}


def envelope():
    return {
        "protocol": "cairn-agent-result-v1", "engine": "pi", "project_id": "p1",
        "task_run_id": "tr1", "phase": "healthcheck", "session": SESSION,
        "status": "succeeded", "result": {
            "protocol": "cairn-assessment-v2", "accepted": True,
            "kind": "healthcheck_result", "data": {"status": "ok"},
        }, "submission_id": "a" * 64, "usage": None, "error": None,
    }


def decode(raw):
    return decode_pi_result(raw, phase="healthcheck", project_id="p1", task_run_id="tr1",
                            session=SESSION, validator=validate_healthcheck_payload)


def test_valid_single_program_envelope():
    result = decode(json.dumps(envelope()))
    assert result.succeeded and result.kind == "healthcheck"
    assert result.validated == {"status": "ok"}

    explained = envelope()
    explained["result"]["reason"] = "Local evidence merits follow-up"
    received = decode(json.dumps(explained))
    assert received.succeeded
    assert received.payload["reason"] == explained["result"]["reason"]
    explained["result"]["reason"] = ""
    assert not decode(json.dumps(explained)).succeeded


@pytest.mark.parametrize("raw", ["", "hello", "```json\n{}\n```", "{}{}", "[]", "null", "NaN", '{"protocol":1,"protocol":2}', '{"x":'])
def test_transport_noise_is_not_repaired(raw):
    assert not decode(raw).succeeded


@pytest.mark.parametrize("field,value", [
    ("protocol", "v0"), ("engine", "claudecode"), ("project_id", "p2"),
    ("task_run_id", "tr2"), ("phase", "report_execute"), ("session", "old-cli-id"),
    ("status", "running"), ("result", "{}"), ("submission_id", "not-a-digest"),
    ("error", {"code": "provider_auth"}), ("usage", 0),
])
def test_identity_or_terminal_conflict_fails(field, value):
    item = envelope()
    item[field] = value
    assert not decode(json.dumps(item)).succeeded


def test_unknown_or_missing_envelope_fields_fail():
    item = envelope()
    item["debug"] = "secret"
    assert not decode(json.dumps(item)).succeeded
    item = envelope()
    del item["usage"]
    assert not decode(json.dumps(item)).succeeded


def test_result_shape_does_not_echo_input():
    item = envelope()
    item["result"]["data"]["status"] = "secret-value"
    result = decode(json.dumps(item))
    assert result.failure.code == "phase_shape_invalid"
    assert "secret-value" not in result.failure.message


def test_oversized_result_rejected():
    assert decode(" " * (2 * 1024 * 1024 + 1)).failure.code == "result_size_invalid"


@pytest.mark.parametrize("code,error_type", [
    ("provider_auth", "provider_auth"), ("provider_rate_limit", "provider_rate_limit"),
    ("provider_error", "provider_error"), ("session_corrupt", "session_error"),
    ("result_schema", "result_schema"), ("cancelled", "cancelled"),
    ("deadline_exceeded", "worker_timeout"), ("runner_error", "transport_error"),
    ("artifact_unknown", "artifact_error"), ("artifact_changed", "artifact_error"),
    ("artifact_invalid", "artifact_error"), ("artifact_unreadable", "artifact_error"),
    ("artifact_permission", "policy_denied"), ("tool_permission", "policy_denied"),
    ("submission_injected_too_large", "request_contract"),
    ("submission_conflict", "result_schema"),
])
def test_failure_code_classification_and_redaction(code, error_type):
    item = envelope()
    item.update(status="failed", result=None, submission_id=None,
                error={"code": code, "message": "private-credential-content"})
    result = decode(json.dumps(item))
    assert worker_failure_error_type(result.failure) == error_type
    from cairn.server.models import UpdateTaskRunRequest
    assert UpdateTaskRunRequest(status="failed", error_type=error_type).error_type == error_type
    assert "private-credential-content" not in result.failure.message
    assert not result.failure.retryable
    from cairn.dispatcher.worker_protocol.submission import SUBMISSION_FAILURE_CODES
    from cairn.server.services.runtime_context import sanitize_task_run_runtime_context
    if code in SUBMISSION_FAILURE_CODES:
        stored = sanitize_task_run_runtime_context({"worker_failure": result.failure.details()})["worker_failure"]
        assert stored["code"] == code
        assert stored["error_type"] == error_type
        assert "private-credential-content" not in json.dumps(stored)


@pytest.mark.parametrize("code", [
    "candidate_admission_rejected",
    "candidate_manifest_invalid",
    "candidate_outcome_invalid",
    "candidate_spool_conflict",
    "candidate_spool_invalid",
    "candidate_spool_limit",
    "candidate_spool_unavailable",
])
def test_candidate_capture_failure_keeps_specific_code_and_safe_classification(code):
    item = envelope()
    item.update(status="failed", result=None, submission_id=None, error={
        "code": code, "message": "private-candidate-content",
    })

    failure = decode(json.dumps(item)).failure

    assert failure.code == code
    assert failure.failure_class == "candidate_integrity"
    assert worker_failure_error_type(failure) == "candidate_integrity"
    assert failure.origin == "pi_runner"
    assert failure.message == f"Pi 执行失败：{code}"
    assert "private-candidate-content" not in json.dumps(failure.details())


def test_unknown_pi_failure_code_remains_fail_closed():
    item = envelope()
    item.update(status="failed", result=None, submission_id=None, error={
        "code": "future_private_failure", "message": "private-provider-content",
    })

    failure = decode(json.dumps(item)).failure

    assert failure.code == "runner_error"
    assert worker_failure_error_type(failure) == "transport_error"
    assert failure.origin == "dispatcher_validation"
    assert "future_private_failure" not in json.dumps(failure.details())
    assert "private-provider-content" not in json.dumps(failure.details())


@pytest.mark.parametrize("path", [
    "binding.review_input_digest",
    "binding.structured_intent_binding",
    "private-token",
    "binding.private-token",
    None,
])
def test_request_failure_retains_only_fixed_contract_path(path):
    item = envelope()
    item.update(status="failed", result=None, submission_id=None, error={
        "code": "invalid_request", "message": "private-provider-body", "path": path,
    })
    failure = decode(json.dumps(item)).failure
    assert worker_failure_error_type(failure) == "request_contract"
    assert failure.origin == "pi_request"
    assert failure.phase == "healthcheck"
    assert failure.path == (
        path
        if path in {
            "binding.review_input_digest",
            "binding.structured_intent_binding",
        }
        else None
    )
    assert "private-" not in json.dumps(failure.details())
    assert "invalid_request" in failure.message


@pytest.mark.parametrize("code", ["submission_missing", "submission_invalid"])
def test_model_submission_diagnostics_cross_gateway_without_retry_authority(code):
    from cairn.server.models import UpdateTaskRunRequest
    from cairn.server.services.runtime_context import sanitize_task_run_runtime_context

    item = envelope()
    item.update(status="failed", result=None, submission_id=None, error={
        "code": code, "message": "PRIVATE_PROVIDER_BODY", "retryable": True,
        "submission_attempts": 3, "repair_rounds": 2, "max_repair_rounds": 2,
        "issue": {"keyword": "const", "path": "/data/status", "value": "PRIVATE_VALUE"},
    })
    failure = decode(json.dumps(item)).failure
    assert failure.code == code
    assert worker_failure_error_type(failure) == code
    assert not failure.retryable
    assert failure.details()["issue"] == {"keyword": "const", "path": "/data/status"}
    assert failure.details()["submission_attempts"] == 3
    assert failure.details()["repair_rounds"] == 2
    assert failure.details()["max_repair_rounds"] == 2
    wire = UpdateTaskRunRequest(status="failed", error_type=code, runtime_context={"worker_failure": failure.details()})
    safe = sanitize_task_run_runtime_context(wire.runtime_context)["worker_failure"]
    assert safe["issue"] == failure.details()["issue"]
    assert "PRIVATE" not in json.dumps(safe)


@pytest.mark.parametrize("issue", [
    {"keyword": "type", "path": "/data/PRIVATE_KEY"},
    {"keyword": "PRIVATE_KEY", "path": "/data/status"},
    {"keyword": "type", "path": "/data/status/PRIVATE_KEY"},
])
def test_gateway_rechecks_untrusted_submission_metadata(issue):
    item = envelope()
    item.update(status="failed", result=None, submission_id=None, error={
        "code": "submission_invalid", "message": "PRIVATE_BODY", "issue": issue,
        "submission_attempts": True, "repair_rounds": 10, "max_repair_rounds": "PRIVATE_KEY",
    })
    failure = decode(json.dumps(item)).failure
    assert failure.code == "submission_invalid"
    assert "issue" not in failure.details()
    assert "PRIVATE" not in json.dumps(failure.details())
    assert "submission_attempts" not in failure.details()
    assert "repair_rounds" not in failure.details()


def test_validation_failure_event_uses_safe_gateway_state_not_raw_exception():
    from unittest.mock import Mock
    from cairn.dispatcher.protocol.client import ApiResult
    from cairn.dispatcher.tasks.validation_records import record_validation_result

    item = envelope()
    item.update(status="failed", result=None, submission_id=None, error={
        "code": "submission_invalid", "message": "PRIVATE_BODY",
        "submission_attempts": 3, "repair_rounds": 2, "max_repair_rounds": 2,
        "issue": {"keyword": "const", "path": "/data/status"},
    })
    failure = decode(json.dumps(item)).failure
    client = Mock()
    client.create_agent_event.return_value = ApiResult(status_code=200)
    record_validation_result(client, "p1", "tr1", phase="healthcheck", status="invalid",
                             failure=failure, error_message="PRIVATE_RAW_EXCEPTION")
    event = client.create_agent_event.call_args.kwargs["payload"]
    assert event["failure"]["code"] == "submission_invalid"
    assert event["failure"]["submission_attempts"] == 3
    assert "PRIVATE" not in json.dumps(event)


def test_failed_candidate_cannot_become_success():
    item = envelope()
    item.update(status="failed", error={"code": "runner_error", "message": "lost stream"})
    assert decode(json.dumps(item)).failure.code == "result_terminal_conflict"


def test_rejection_is_not_a_healthy_result():
    item = envelope()
    item["result"] = {"protocol": "cairn-assessment-v2", "accepted": False, "reason": "cannot run"}
    assert decode(json.dumps(item)).kind == "rejected"


def test_bootstrap_intake_rejects_scope_disposition_with_discovered_surfaces():
    conflicting = {
        "fact": {"description": "mapped"},
        "complete": {"description": "done"},
        "discovered_surfaces": [{
            "surface_kind": "endpoint",
            "surface_key": "GET /fixture",
        }],
        "coverage_scope_disposition": {
            "disposition_kind": "no_reachable_surfaces",
            "target_ids": ["target-1"],
            "reason": "none reachable",
            "evidence_refs": [{"artifact_id": "a1"}],
        },
    }

    for phase in ("bootstrap_execute", "bootstrap_conclude"):
        with pytest.raises(ValidationError):
            validate_phase_data(phase, conflicting)
        zero_surface = {**conflicting, "discovered_surfaces": []}
        assert validate_phase_data(phase, zero_surface)[
            "coverage_scope_disposition"
        ]["disposition_kind"] == "no_reachable_surfaces"
        discovered = dict(conflicting)
        discovered.pop("coverage_scope_disposition")
        assert len(validate_phase_data(phase, discovered)["discovered_surfaces"]) == 1
        data_schema = build_submit_result_schema(phase)["properties"]["data"]
        assert {
            "if": {"required": ["coverage_scope_disposition"]},
            "then": {
                "properties": {
                    "discovered_surfaces": {"maxItems": 0},
                },
            },
        } in data_schema["allOf"]


def test_goal_driven_bootstrap_decode_accepts_fact_without_complete():
    item = envelope()
    item.update(phase="bootstrap_execute")
    item["result"] = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "bootstrap_result",
        "data": {"fact": {"description": "initial target context"}},
    }

    historical = decode_pi_result(
        json.dumps(item),
        phase="bootstrap_execute",
        project_id="p1",
        task_run_id="tr1",
        session=SESSION,
        validator=validate_bootstrap_execute_payload,
    )
    assert historical.failure is not None
    assert historical.failure.code == "phase_shape_invalid"

    goal_driven = decode_pi_result(
        json.dumps(item),
        phase="bootstrap_execute",
        project_id="p1",
        task_run_id="tr1",
        session=SESSION,
        assessment_protocol=GOAL_DRIVEN_PROTOCOL,
        validator=lambda payload: validate_bootstrap_execute_payload(
            payload,
            goal_driven=True,
        ),
    )
    assert goal_driven.failure is None
    assert goal_driven.kind == "fact"
    assert goal_driven.validated == {
        "fact_description": "initial target context",
        "slots": [],
        "discovered_surfaces": [],
        "coverage_scope_disposition": None,
    }


@pytest.mark.parametrize("action", [
    {"intents": [{"from": ["f1"], "description": "new work"}]},
    {"abandon": ["i1"]},
    {"discovered_surfaces": [{"surface_kind": "path", "surface_key": "/fixture"}]},
])
def test_reason_completion_rejects_nonempty_actions_in_python_and_dispatcher(action):
    from cairn.dispatcher.contracts import PhaseContractError

    data = {"complete": {"from": ["f1"], "description": "done"}, **action}
    with pytest.raises(ValidationError):
        validate_phase_data("reason_execute", data)
    with pytest.raises(PhaseContractError):
        validate_reason_payload({"protocol": "cairn-assessment-v2", "accepted": True,
                                 "kind": "reason_result", "data": data},
                                open_intents_empty=True, max_intents=3, goal_driven=True,
                                surface_manifest=True)


def test_every_executable_phase_schema_and_export_are_current():
    assert len(PHASE_DATA_MODELS) == 9
    assert "healthcheck" not in PHASE_DATA_MODELS
    with pytest.raises(ValueError, match="unsupported result phase"):
        build_submit_result_schema("healthcheck")
    exported = Path(__file__).resolve().parents[2] / "container/pi-agent/contracts/submission-v1.json"
    contracts = exported_contracts()
    assert "healthcheck" not in contracts["phase_result_kinds"]
    assert "healthcheck" not in contracts["submit_result_schemas"]
    assert json.loads(exported.read_text()) == contracts
    for phase in PHASE_DATA_MODELS:
        schema = build_submit_result_schema(phase)
        assert schema["additionalProperties"] is False
        assert "protocol" not in schema["properties"]
        assert "artifacts" not in schema["properties"]["data"]["properties"]


def test_runtime_identity_is_hidden_not_model_echoed():
    schema = build_submit_result_schema("explore_execute", {
        "slot_key": "s1", "surface_key": "surface1", "test_family": "authorization",
    })
    coverage = schema["$defs"]["CoverageResult"]
    assert not {"slot_key", "surface_key", "test_family"} & coverage["properties"].keys()
    assert "coverage_result" in schema["properties"]["data"]["required"]
    schema = build_submit_result_schema("reason_execute", {
        "allowed_fact_ids": ["f1"], "max_intents": 2, "review_input_digest": "a" * 64,
    })
    assert schema["$defs"]["IntentDecision"]["properties"]["from"]["items"]["enum"] == ["f1"]
    assert "input_digest" not in schema["$defs"]["DepthReviewDecision"]["properties"]


def test_goal_driven_explore_schema_requires_authoritative_evidence():
    schema = build_submit_result_schema(
        "explore_execute",
        {"assessment_protocol": GOAL_DRIVEN_PROTOCOL},
    )
    experiment = schema["$defs"]["ExperimentRecord"]

    assert "evidence_refs" in experiment["required"]
    assert experiment["properties"]["evidence_refs"]["minItems"] == 1


def test_structured_reason_schema_binds_slots_and_per_cell_strategy_constraints():
    schema = build_submit_result_schema(
        "reason_execute",
        {
            "allowed_fact_ids": ["f1"],
            "max_intents": 5,
            "structured_intent_binding": structured_intent_binding(),
        },
    )
    decision = schema["$defs"]["IntentDecision"]
    assert {"slot_key", "strategy_key"} <= set(decision["required"])
    assert decision["properties"]["slot_key"]["enum"] == [
        "cell-a",
        "cell-b",
    ]
    assert decision["properties"]["strategy_key"] == {
        "type": "string",
        "pattern": "^[a-z0-9][a-z0-9._:-]{0,127}$",
    }
    assert decision["allOf"] == [
        {
            "if": {
                "properties": {"slot_key": {"const": "cell-b"}},
                "required": ["slot_key"],
            },
            "then": {
                "properties": {
                    "strategy_key": {
                        "not": {"const": "blocked.v1"}
                    }
                }
            },
        }
    ]
    intents = next(
        variant
        for variant in schema["properties"]["data"]["properties"][
            "intents"
        ]["anyOf"]
        if variant.get("type") == "array"
    )
    assert intents["maxItems"] == 2


def test_structured_reason_schema_with_no_eligible_cells_allows_no_intents():
    schema = build_submit_result_schema(
        "reason_execute",
        {
            "max_intents": 5,
            "structured_intent_binding": structured_intent_binding(
                empty=True
            ),
        },
    )
    intents = next(
        variant
        for variant in schema["properties"]["data"]["properties"][
            "intents"
        ]["anyOf"]
        if variant.get("type") == "array"
    )
    assert intents["maxItems"] == 0
    assert schema["$defs"]["IntentDecision"]["properties"][
        "slot_key"
    ] == {"type": "string", "not": {}}


def reason_payload(intents):
    return {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "reason_result",
        "data": {"intents": intents},
    }


def test_structured_reason_semantics_allow_same_strategy_on_distinct_cells():
    payload = reason_payload(
        [
            {
                "from": ["f1"],
                "description": "Check A",
                "slot_key": "cell-a",
                "strategy_key": "shared.v1",
            },
            {
                "from": ["f1"],
                "description": "Check B",
                "slot_key": "cell-b",
                "strategy_key": "shared.v1",
            },
        ]
    )
    kind, intents, _ = validate_reason_payload(
        payload,
        True,
        5,
        structured_intent_binding=structured_intent_binding(),
    )
    assert kind == "intents"
    assert [item["slot_key"] for item in intents] == [
        "cell-a",
        "cell-b",
    ]


@pytest.mark.parametrize(
    "intents,error",
    [
        (
            [{"from": ["f1"], "description": "Missing"}],
            "requires slot_key and strategy_key",
        ),
        (
            [{
                "from": ["f1"],
                "description": "Outside",
                "slot_key": "cell-outside",
                "strategy_key": "test.v1",
            }],
            "outside the structured binding",
        ),
        (
            [
                {
                    "from": ["f1"],
                    "description": "First",
                    "slot_key": "cell-a",
                    "strategy_key": "one.v1",
                },
                {
                    "from": ["f1"],
                    "description": "Second",
                    "slot_key": "cell-a",
                    "strategy_key": "two.v1",
                },
            ],
            "must be unique within the batch",
        ),
        (
            [{
                "from": ["f1"],
                "description": "Blocked",
                "slot_key": "cell-b",
                "strategy_key": "blocked.v1",
            }],
            "violates the Cell constraint",
        ),
    ],
)
def test_structured_reason_semantics_reject_invalid_cell_decisions(
    intents,
    error,
):
    with pytest.raises(ValueError, match=error):
        validate_reason_payload(
            reason_payload(intents),
            True,
            5,
            structured_intent_binding=structured_intent_binding(),
        )


def test_structured_reason_with_no_eligible_cells_can_noop():
    assert validate_reason_payload(
        reason_payload([]),
        True,
        5,
        structured_intent_binding=structured_intent_binding(empty=True),
    ) == ("noop", None, [])


def test_evidence_reference_has_shared_exclusive_constraint():
    assert "oneOf" in AssessmentEvidenceRef.model_json_schema()
    with pytest.raises(ValidationError):
        AssessmentEvidenceRef(artifact_id="a1", artifact_handle="h1")
    with pytest.raises(ValidationError):
        AssessmentEvidenceRef()


def test_no_raw_report_updates_or_title_lookup():
    with pytest.raises(ValidationError):
        validate_phase_data("report_execute", {"vulnerability_updates": [], "report_markdown": "text"})
    payload = {"protocol": "cairn-assessment-v2", "accepted": True, "kind": "report_result",
               "data": {"schema_version": 2, "executive_summary": "no findings", "findings": [],
                        "layout": [{"type": "markdown", "markdown": "# Report"}]}}
    assert validate_report_payload(payload)[0] == "report"


def test_worker_evidence_v2_is_single_source_and_rejects_legacy_worker_data():
    from cairn.dispatcher.worker_protocol.submission import FactResult

    item = {"id": "req-1", "kind": "http", "source": "capture",
            "request": {"artifact_handle": "local-request"}}
    finding = {
        "schema_version": 2, "title": "Synthetic finding", "severity": "high",
        "identity": {
            "vulnerability_class": "broken_access_control",
            "asset": {"host": "fixture.invalid", "port": 443},
            "entrypoint": {"route": "/fixture"},
        },
        "evidence_items": [item],
    }
    fact = {"description": "Observed fixture", "vulnerability": finding}
    assert FactResult.model_validate(fact).vulnerability.evidence_items[0].response is None
    with pytest.raises(ValidationError, match="projected"):
        FactResult.model_validate({**fact, "evidence_refs": []})
    for phase in ("explore_execute", "bootstrap_conclude"):
        with pytest.raises(ValidationError, match="projected"):
            validate_phase_data(phase, {"fact": fact, "evidence_refs": []})
    with pytest.raises(ValidationError):
        FactResult.model_validate({**fact, "vulnerability": {**finding, "repro_request": "invented"}})
    external = {
        "schema_version": 3, "outcome": "confirmed", "summary": "合成观察",
        "tested_scope": "本地合成材料", "limitations": [],
        "key_evidence": [{"reason": "原件记录本次观察", "evidence_item_ids": ["req-1"]}],
        "test_steps": [{"action": "登记本地合成原件", "observation": "登记成功", "evidence_item_ids": ["req-1"]}],
        "evidence_items": [item],
    }
    assert validate_phase_data("external_verify_execute", external) == external
    for changes in (
        {"notes": "退役字段"}, {"schema_version": 2},
        {"evidence_items": [item, item]},
        {"key_evidence": [{"reason": "未知条目", "evidence_item_ids": ["missing"]}]},
        {"test_steps": [{"action": "实际操作", "observation": "实际观察", "evidence_item_ids": ["missing"]}]},
    ):
        with pytest.raises(ValidationError):
            validate_phase_data("external_verify_execute", {**external, **changes})
    with pytest.raises(ValidationError):
        validate_phase_data("external_verify_execute", {**external, "evidence_refs": []})
    with pytest.raises(ValidationError, match="current execution evidence"):
        validate_phase_data("external_verify_execute", {**external, "evidence_items": []})
    with pytest.raises(ValidationError):
        validate_phase_data("external_verify_execute", {
            **external, "evidence_items": [{**item, "request_text": "invented"}],
        })
    legacy = {key: value for key, value in external.items() if key not in {"schema_version", "evidence_items"}}
    legacy["evidence_refs"] = [{"artifact_id": "a1"}]
    with pytest.raises(ValidationError):
        validate_phase_data("external_verify_execute", legacy)


@pytest.mark.parametrize("kind,source,content,valid", [
    ("http", "capture", {"request": {"artifact_id": "a1"}, "response": {"artifact_id": "a2"}}, True),
    ("http", "capture", {"request": {"artifact_id": "a1"}}, True),
    ("http", "capture", {"response": {"artifact_id": "a2"}}, True),
    ("artifact", "capture", {"artifact": {"artifact_id": "a1"}}, True),
    ("text", "capture", {"artifact": {"artifact_id": "a1"}}, True),
    ("http", "historical", {"request_text": "GET / HTTP/1.1\r\n\r\n  "}, True),
    ("text", "historical", {"text": "历史原文\n  "}, True),
    ("http", "capture", {"artifact": {"artifact_id": "a1"}}, False),
    ("http", "capture", {"request": {"artifact_id": "a1"}, "artifact": {"artifact_id": "a2"}}, False),
    ("http", "historical", {"text": "不可隐藏的正文"}, False),
    ("artifact", "capture", {"request": {"artifact_id": "a1"}}, False),
    ("text", "capture", {"response": {"artifact_id": "a1"}}, False),
    ("artifact", "historical", {"artifact": {"artifact_id": "a1"}, "response_text": "HTTP/1.1 200 OK"}, False),
    ("artifact", "historical", {"text": "附件应使用原件引用"}, False),
    ("http", "capture", {"request_text": "GET / HTTP/1.1"}, False),
    ("http", "capture", {}, False),
])
def test_evidence_kind_slots_agree_across_python_writes(kind, source, content, valid):
    from cairn.evidence_contracts import EvidenceItem, WorkerEvidenceItem
    from cairn.server.schemas.verification_requests import VerificationResult

    item = {"id": "synthetic", "kind": kind, "source": source, **content}
    inline = bool({"request_text", "response_text", "text"} & content.keys())
    result = {
        "schema_version": 3, "outcome": "confirmed", "summary": "合成结果",
        "tested_scope": "仅隔离合同", "key_evidence": [], "test_steps": [], "limitations": [],
        "evidence_items": [item],
    }
    for model, value, accepted in (
        (EvidenceItem, item, valid),
        (WorkerEvidenceItem, item, valid and not inline),
        (VerificationResult, result, valid and not inline),
    ):
        if accepted:
            parsed = model.model_validate(value)
            assert parsed.model_dump(exclude_unset=True) == value
        else:
            with pytest.raises(ValidationError):
                model.model_validate(value)


@pytest.mark.parametrize("phase,prompt_file", [
    ("explore_execute", "explore.md"), ("explore_conclude", "explore_conclude.md"),
])
def test_loaded_explore_guidance_submits_registered_finding(phase, prompt_file, tmp_path, monkeypatch):
    from copy import deepcopy
    from cairn.dispatcher.prompting import load_prompt
    from cairn.dispatcher.tasks.artifact_records import resolve_declared_evidence_items
    from cairn.dispatcher.tasks.common_base import ProcessArtifactRecord
    from cairn.dispatcher.worker_protocol.submission import FactResult

    monkeypatch.setenv("CAIRN_PROMPTS_ROOT", str(tmp_path))
    instructions = load_prompt("goal-driven-v1", prompt_file)
    assert "capture_http_evidence" in instructions and "evidence_items" in instructions
    assert "packet_artifact_handle" not in instructions
    data = {
        "fact": {"description": "合成观察", "vulnerability": {
            "schema_version": 2, "title": "合成发现", "severity": "high",
            "identity": {"vulnerability_class": "broken_access_control",
                         "asset": {"host": "fixture.invalid", "port": 443},
                         "entrypoint": {"route": "/fixture"}},
            "evidence_items": [{"id": "capture-1", "kind": "http", "source": "capture",
                                "request": {"artifact_handle": "registered-request"},
                                "response": {"artifact_handle": "registered-response"}}],
        }},
        "assessment_record": {"hypothesis": "合成权限假设", "outcome": "finding",
                              "observations": ["合成观察"],
                              "evidence_refs": [{"artifact_handle": "registered-request"}]},
    }
    assert validate_phase_data(phase, data, assessment_protocol=GOAL_DRIVEN_PROTOCOL) == data
    finding = FactResult.model_validate(data["fact"]).vulnerability
    resolved = resolve_declared_evidence_items(finding.evidence_items, ProcessArtifactRecord(
        refs=[], artifact_ids_by_handle={"registered-request": "a1", "registered-response": "a2"},
    ), require_current_run=True)
    assert (resolved[0].request.artifact_id, resolved[0].response.artifact_id) == ("a1", "a2")
    retired = deepcopy(data)
    retired["fact"]["vulnerability"]["packet_artifact_handle"] = "registered-request"
    with pytest.raises(ValidationError):
        validate_phase_data(phase, retired, assessment_protocol=GOAL_DRIVEN_PROTOCOL)


def test_worker_evidence_items_resolve_uploaded_versions_and_preserve_missing_side():
    from cairn.dispatcher.tasks.artifact_records import (
        UnresolvedArtifactEvidenceError, resolve_declared_evidence_items,
    )
    from cairn.dispatcher.tasks.common_base import ProcessArtifactRecord
    from cairn.evidence_contracts import WorkerEvidenceItem, evidence_refs

    item = WorkerEvidenceItem.model_validate({
        "id": "request-only", "kind": "http", "source": "capture",
        "request": {"artifact_handle": "local-request", "pointer": "bytes"},
    })
    uploaded = ProcessArtifactRecord(refs=[], artifact_ids_by_handle={"local-request": "a10"})
    resolved = resolve_declared_evidence_items([item], uploaded, require_current_run=True)
    assert resolved[0].request.artifact_id == "a10"
    assert resolved[0].request.pointer == "bytes"
    assert resolved[0].response is None
    assert [ref.artifact_id for ref in evidence_refs(resolved)] == ["a10"]
    with pytest.raises(UnresolvedArtifactEvidenceError):
        resolve_declared_evidence_items([item], ProcessArtifactRecord(refs=[]), require_current_run=True)
    historical = WorkerEvidenceItem.model_validate({
        "id": "old", "kind": "artifact", "source": "historical", "artifact": {"artifact_id": "a9"},
    })
    with pytest.raises(UnresolvedArtifactEvidenceError, match="this execution"):
        resolve_declared_evidence_items([historical], uploaded, require_current_run=True)

    from cairn.dispatcher.contracts import extract_evidence_refs
    from cairn.dispatcher.tasks.conclude_writer import (
        CoverageEvidenceInsufficientError, build_vulnerability_for_conclude,
    )

    payload = {"accepted": True, "data": {"fact": {
        "description": "Actual request without response",
        "vulnerability": {
            "schema_version": 2, "title": "Synthetic finding", "severity": "high",
            "identity": {"vulnerability_class": "broken_access_control",
                         "asset": {"host": "fixture.invalid", "port": 443},
                         "entrypoint": {"route": "/fixture"}},
            "evidence_items": [item.model_dump(exclude_none=True)],
        },
    }}}
    assert [ref.artifact_id for ref in extract_evidence_refs(payload, section="fact")] == ["local-request"]
    conclude = build_vulnerability_for_conclude(payload, uploaded, require_current_run_reproduction=True)
    assert conclude["schema_version"] == 2
    assert conclude["evidence_items"][0]["request"]["artifact_id"] == "a10"
    assert "response" not in conclude["evidence_items"][0]
    assert "repro_request" not in conclude
    with pytest.raises(CoverageEvidenceInsufficientError):
        build_vulnerability_for_conclude(payload, ProcessArtifactRecord(refs=[]))


def test_depth_review_standalone_and_zero_selection():
    data = {"depth_review": {"input_digest": "a" * 64, "description": "No supported hypothesis",
                             "evidence_refs": [{"artifact_id": "a1"}], "selected": []}}
    payload = {"protocol": "cairn-assessment-v2", "accepted": True, "kind": "reason_result", "data": data}
    assert validate_reason_payload(payload, True, 3)[0] == "depth_review"
    data["abandon"] = ["i1"]
    with pytest.raises(ValueError, match="standalone"):
        validate_reason_payload(payload, True, 3)
