from __future__ import annotations

from copy import deepcopy
from importlib import import_module
import json
from types import SimpleNamespace

import pytest

from cairn.dispatcher.config import PiModelConfig, WorkerConfig
from cairn.dispatcher.prompt_resources import read_prompt_resource
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.workers.adapters.pi import PiDriver
from cairn.dispatcher.scheduler.common import (
    ERROR_MESSAGES,
    REJECTION_RETRY_ERROR_TYPES,
    STANDARD_ERROR_TYPES,
    WORKER_COOLDOWN_ERROR_TYPES,
)
from cairn.dispatcher.tasks.artifact_records import (
    UnresolvedArtifactEvidenceError,
    resolve_declared_artifact_evidence_refs,
)
from cairn.dispatcher.tasks.common_base import ProcessArtifactRecord
from cairn.dispatcher.tasks.conclude_writer import (
    VerificationEvidenceInsufficientError,
    build_verification_result_for_conclude,
)
from cairn.dispatcher.worker_protocol import (
    protocol_json_schema,
    protocol_schema_digest,
)
from cairn.dispatcher.worker_result_gateway import (
    decode_pi_result,
    validate_worker_payload,
    worker_failure_error_type,
)
from cairn.server.models import EvidenceRef, UpdateTaskRunRequest
from cairn.server.services.assessment_attempts import RUNTIME_RETRYABLE_ERROR_TYPES


def test_proj028_verifier_metadata_shape_is_rejected_as_contract_error() -> None:
    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "verifier_result",
        "data": {
            "description": "independent verification",
            "verification_result": {
                "observation_id": "vo1",
                "outcome": "confirmed",
                "evidence_refs": [{"artifact_id": "copied-evidence"}],
            },
            "artifacts": [
                {
                    "id": "copied-evidence",
                    "kind": "raw_output",
                    "filename": "response.log",
                    "pointer": "response",
                    "source": "fact_evidence",
                }
            ],
        },
    }

    from cairn.dispatcher.contracts import validate_verifier_payload

    with pytest.raises(ValueError, match=r"unexpected artifact fields at index 0"):
        validate_verifier_payload(payload, target_observation_id="vo1")


def test_unresolved_current_run_evidence_is_never_silently_dropped() -> None:
    declared = ProcessArtifactRecord(
        refs=[EvidenceRef(artifact_id="a001")],
        artifact_ids_by_handle={"evidence": "a001"},
    )

    with pytest.raises(UnresolvedArtifactEvidenceError, match="missing-handle"):
        resolve_declared_artifact_evidence_refs(
            [EvidenceRef(artifact_id="missing-handle", pointer="response")],
            declared,
        )


def test_verifier_rejects_evidence_that_becomes_empty_after_resolution() -> None:
    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "verifier_result",
        "data": {
            "description": "independent verification",
            "verification_result": {
                "observation_id": "vo1",
                "outcome": "confirmed",
                "evidence_refs": [
                    {"artifact_id": "unrelated-reference", "pointer": "response"}
                ],
            },
        },
    }

    with pytest.raises(VerificationEvidenceInsufficientError):
        build_verification_result_for_conclude(
            payload,
            ProcessArtifactRecord(refs=[]),
        )


@pytest.mark.parametrize("name", ("verifier.md", "verifier_conclude.md"))
def test_verifier_prompt_distinguishes_prior_evidence_from_current_artifacts(name: str) -> None:
    prompt = read_prompt_resource("default-copy-v2", name)

    assert "`submit_result`" in prompt
    assert "`register_artifact`" in prompt
    assert "existing absolute `path` and `kind`" in prompt
    assert "`evidence_refs[].artifact_handle`" in prompt
    assert "handwrite `data.artifacts`" in prompt
    assert "copy prior Artifact metadata into new declarations" in prompt
    assert "non-empty current-run `evidence_refs`" in prompt
    assert "Runtime injects the bound `observation_id`" in prompt
    assert "timeout, or budget-not-executed is not a verification outcome" in prompt
    assert "optional `data.artifacts`" not in prompt


def test_deterministic_worker_result_failures_are_not_retried() -> None:
    assert "protocol_error" not in REJECTION_RETRY_ERROR_TYPES
    assert "contract_error" not in REJECTION_RETRY_ERROR_TYPES
    assert "artifact_error" not in REJECTION_RETRY_ERROR_TYPES
    assert ERROR_MESSAGES["transport_error"] == "Worker 结果通道失败"
    assert ERROR_MESSAGES["protocol_error"] == "Worker 结果协议无效"
    assert ERROR_MESSAGES["contract_error"] == "Worker 结果不符合阶段契约"
    assert ERROR_MESSAGES["worker_oom"] == "Worker 内存超限退出"
    assert "worker_oom" in REJECTION_RETRY_ERROR_TYPES
    assert "worker_oom" not in WORKER_COOLDOWN_ERROR_TYPES
    assert UpdateTaskRunRequest(status="failed", error_type="worker_oom").error_type == "worker_oom"
    for error_type in (
        "transport_error",
        "protocol_error",
        "contract_error",
        "artifact_error",
    ):
        assert UpdateTaskRunRequest(
            status="failed",
            error_type=error_type,
        ).error_type == error_type


def test_delivery_and_acceptance_failures_cannot_relaunch_model_work() -> None:
    for error_type in (
        "policy_denied",
        "server_write_failed",
        "stale_business_state",
        "receipt_conflict",
        "candidate_integrity",
    ):
        assert error_type not in REJECTION_RETRY_ERROR_TYPES
        assert error_type not in RUNTIME_RETRYABLE_ERROR_TYPES
    assert "candidate_integrity" in STANDARD_ERROR_TYPES
    assert ERROR_MESSAGES["candidate_integrity"] == "Worker candidate 捕获完整性校验失败"
    for error_type in (
        "submission_missing",
        "submission_invalid",
        "delivery_unavailable",
        "stale_business_state",
        "receipt_conflict",
    ):
        assert error_type in STANDARD_ERROR_TYPES
        assert UpdateTaskRunRequest(
            status="failed",
            error_type=error_type,
        ).error_type == error_type


@pytest.mark.parametrize("error_kind", ("value", "type", "pydantic"))
def test_business_contract_failure_has_stable_nonretryable_fingerprint(error_kind: str) -> None:
    payload = {
        "protocol": "cairn-assessment-v2", "accepted": True,
        "kind": "verifier_result", "data": {},
    }

    def reject(_payload):
        if error_kind == "pydantic":
            EvidenceRef.model_validate({"artifact_id": ["private fixture detail"]})
        error_type = TypeError if error_kind == "type" else ValueError
        raise error_type("verification_result.evidence_refs is required: private fixture detail")

    first = validate_worker_payload(payload, phase="verifier_execute", validator=reject)
    second = validate_worker_payload(payload, phase="verifier_execute", validator=reject)
    assert first.failure is not None and second.failure is not None
    assert first.failure.failure_class == "business_acceptance"
    assert worker_failure_error_type(first.failure) == "business_acceptance"
    assert first.failure.retryable is False
    assert first.failure.fingerprint == second.failure.fingerprint
    assert first.failure.message == "Worker 结果未通过阶段业务验收：phase_contract_invalid"
    assert "private fixture detail" not in json.dumps(first.failure.details())


@pytest.mark.parametrize(
    ("coverage_fields", "public_reason"),
    (
        ({}, "finding requires baseline and variants"),
        (
            {"baseline": "private baseline", "variants": ["private variant"]},
            "finding requires evidence_refs",
        ),
    ),
)
def test_gateway_preserves_authoritative_phase_rule_reasons(coverage_fields, public_reason) -> None:
    from cairn.dispatcher.contracts import validate_explore_payload

    payload = {
        "protocol": "cairn-assessment-v2", "accepted": True,
        "kind": "explore_result",
        "data": {
            "description": "private candidate",
            "coverage_result": {
                "slot_key": "private-slot", "surface_key": "private-surface",
                "test_family": "authorization", "outcome": "finding",
                **coverage_fields,
            },
        },
    }
    original = deepcopy(payload)
    outcome = validate_worker_payload(
        payload, phase="explore_execute",
        validator=lambda value: validate_explore_payload(
            value, strict_contract=True, outcome_contract_version=2,
        ),
    )

    assert outcome.failure is not None
    assert outcome.failure.code == "phase_contract_invalid"
    assert worker_failure_error_type(outcome.failure) == "business_acceptance"
    assert outcome.failure.retryable is False
    assert outcome.failure.message == f"Worker 结果未通过阶段业务验收：{public_reason}"
    assert outcome.failure.details()["message"] == outcome.failure.message
    assert "private" not in json.dumps(outcome.failure.details())
    assert outcome.payload is payload and payload == original


def test_gateway_preserves_bootstrap_surface_resolution_reason() -> None:
    from cairn.dispatcher.contracts import validate_bootstrap_execute_payload

    payload = {
        "protocol": "cairn-assessment-v2", "accepted": True,
        "kind": "bootstrap_result",
        "data": {
            "fact": {"description": "private candidate and credential"},
            "complete": {"description": "private completion"},
        },
    }
    original = deepcopy(payload)
    outcome = validate_worker_payload(
        payload, phase="bootstrap_execute",
        validator=lambda value: validate_bootstrap_execute_payload(
            value, require_surface_resolution=True,
        ),
    )

    assert outcome.failure is not None
    assert outcome.failure.code == "phase_contract_invalid"
    assert outcome.failure.message == (
        "Worker 结果未通过阶段业务验收："
        "minimum coverage bootstrap requires discovered_surfaces or coverage_scope_disposition"
    )
    assert outcome.failure.details()["message"] == outcome.failure.message
    assert "private" not in json.dumps(outcome.failure.details())
    assert outcome.payload is payload and payload == original


@pytest.mark.parametrize("invalid_part", ("extra_field", "duplicate_handle", "evidence_shape"))
def test_gateway_publishes_rule_without_dynamic_contract_values(invalid_part: str) -> None:
    from cairn.dispatcher.contracts import PhaseContractError, validate_explore_payload

    secret = "fixture-secret"
    artifact = {"handle": secret, "kind": "file", "path": "/workspace/evidence"}
    data = {"fact": {"description": secret}}
    if invalid_part == "extra_field":
        data["artifacts"] = [{**artifact, secret: secret}]
        public_reason = "unexpected artifact fields at index 0"
    elif invalid_part == "duplicate_handle":
        data["artifacts"] = [artifact, dict(artifact)]
        public_reason = "artifact.handle must be unique"
    else:
        data["fact"]["evidence_refs"] = [{"artifact_id": "a001", "pointer": {secret: secret}}]
        public_reason = "invalid fact.evidence_refs[0]"
    payload = {
        "protocol": "cairn-assessment-v2", "accepted": True,
        "kind": "explore_result", "data": data,
    }
    original = deepcopy(payload)
    with pytest.raises(PhaseContractError) as raised:
        validate_explore_payload(payload)
    assert secret in str(raised.value)
    assert raised.value.public_reason == public_reason

    outcome = validate_worker_payload(
        payload, phase="explore_execute", validator=validate_explore_payload,
    )
    assert outcome.failure is not None
    assert outcome.failure.message == f"Worker 结果未通过阶段业务验收：{public_reason}"
    assert outcome.failure.details()["message"] == outcome.failure.message
    assert secret not in json.dumps(outcome.failure.details())
    assert outcome.payload is payload and payload == original


@pytest.mark.parametrize(
    "payload",
    (
        {"accepted": True, "kind": "reason_result", "data": {}},
        {
            "protocol": "cairn-assessment-v1",
            "accepted": True,
            "kind": "reason_result",
            "data": {},
        },
    ),
)
def test_gateway_rejects_missing_or_wrong_protocol_as_protocol_failure(
    payload: dict[str, object],
) -> None:
    from cairn.dispatcher.contracts import validate_reason_payload

    outcome = validate_worker_payload(
        payload,
        phase="reason_execute",
        validator=lambda value: validate_reason_payload(
            value,
            open_intents_empty=False,
            max_intents=3,
        )[:2],
    )

    assert outcome.failure is not None
    assert outcome.failure.failure_class == "protocol"
    assert outcome.failure.code == "result_envelope_invalid"
    assert outcome.failure.retryable is False


def test_proj042_bootstrap_result_allows_top_level_reason() -> None:
    from cairn.dispatcher.contracts import validate_bootstrap_execute_payload

    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "bootstrap_result",
        "reason": "已完成 Bootstrap 探测并注册攻击面。",
        "data": {
            "fact": {"description": "确认目标服务可达。"},
            "complete": {"description": "继续执行覆盖单元。"},
        },
    }
    outcome = validate_worker_payload(
        payload,
        phase="bootstrap_execute",
        validator=validate_bootstrap_execute_payload,
    )

    assert outcome.failure is None
    assert outcome.payload == payload


def test_proj042_reason_result_allows_top_level_reason() -> None:
    from cairn.dispatcher.contracts import validate_reason_payload

    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "reason_result",
        "reason": "根据现有事实调度下一轮测试。",
        "data": {
            "intents": [
                {
                    "from": ["f001"],
                    "description": "验证已确认服务的访问控制。",
                }
            ]
        },
    }
    outcome = validate_worker_payload(
        payload,
        phase="reason_execute",
        validator=lambda value: validate_reason_payload(
            value,
            open_intents_empty=True,
            max_intents=3,
        )[:2],
    )

    assert outcome.failure is None
    assert outcome.payload == payload


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("reason", ""),
        ("unexpected", "value"),
    ),
)
def test_accepted_result_still_rejects_invalid_top_level_fields(
    field: str,
    value: str,
) -> None:
    from cairn.dispatcher.contracts import validate_bootstrap_execute_payload

    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "bootstrap_result",
        "data": {
            "fact": {"description": "确认目标服务可达。"},
            "complete": {"description": "继续执行覆盖单元。"},
        },
        field: value,
    }
    outcome = validate_worker_payload(
        payload,
        phase="bootstrap_execute",
        validator=validate_bootstrap_execute_payload,
    )

    assert outcome.failure is not None
    assert outcome.failure.failure_class == "protocol"
    assert outcome.failure.code == "result_envelope_invalid"


def test_protocol_schema_digest_is_stable() -> None:
    first = protocol_schema_digest()

    assert len(first) == 64
    assert first == protocol_schema_digest()
    assert protocol_json_schema()["discriminator"]["propertyName"] == "accepted"




def test_duplicate_artifact_handles_are_rejected() -> None:
    from cairn.dispatcher.contracts import validate_explore_payload

    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "explore_result",
        "data": {
            "fact": {"description": "bounded result"},
            "artifacts": [
                {"handle": "evidence", "kind": "report", "path": "/workspace/a.md"},
                {"handle": "evidence", "kind": "report", "path": "/workspace/b.md"},
            ],
        },
    }

    with pytest.raises(ValueError, match="artifact.handle must be unique"):
        validate_explore_payload(payload)


@pytest.mark.parametrize("handle", ("a999", " evidence "))
def test_ambiguous_or_padded_artifact_handles_are_rejected(handle: str) -> None:
    from cairn.dispatcher.contracts import validate_explore_payload

    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "explore_result",
        "data": {
            "fact": {"description": "bounded result"},
            "artifacts": [
                {"handle": handle, "kind": "report", "path": "/workspace/a.md"}
            ],
        },
    }

    with pytest.raises(ValueError, match="artifact.handle"):
        validate_explore_payload(payload)


def test_server_id_shaped_artifact_handle_cannot_bypass_resolution() -> None:
    from cairn.dispatcher.contracts import validate_explore_payload

    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "explore_result",
        "data": {
            "fact": {
                "description": "bounded result",
                "evidence_refs": [{"artifact_handle": "a999"}],
            }
        },
    }

    with pytest.raises(ValueError, match="must not use a Server artifact id"):
        validate_explore_payload(payload)






def test_proj038_nested_json_text_survives_pi_program_envelope() -> None:
    from cairn.dispatcher.contracts import validate_bootstrap_conclude_payload

    description = '响应 body {"checkPtKey":true}\n第二行中文证据'
    session = "pi:07b53667-a6ab-44be-badb-e3ed6b347331"
    stdout = json.dumps({
        "protocol": "cairn-agent-result-v1", "engine": "pi", "project_id": "project-1",
        "task_run_id": "run-1", "phase": "bootstrap_conclude", "session": session,
        "status": "succeeded", "submission_id": "a" * 64, "usage": {}, "error": None,
        "result": {
            "protocol": "cairn-assessment-v2", "accepted": True,
            "kind": "bootstrap_conclude_result", "data": {"fact": {"description": description}},
        },
    }, ensure_ascii=False)
    outcome = decode_pi_result(
        stdout, phase="bootstrap_conclude", project_id="project-1", task_run_id="run-1",
        session=session, validator=validate_bootstrap_conclude_payload,
    )
    assert outcome.failure is None
    assert outcome.description == description


@pytest.mark.parametrize("task_name", ["bootstrap", "explore"])
@pytest.mark.parametrize(("failure", "expected"), [
    ("incomplete", "result_schema"),
    ("malformed", "protocol_error"),
    ("timeout", "worker_timeout"),
    ("cancelled", "cancelled"),
    ("oom", "worker_oom"),
])
def test_pi_task_failure_keeps_evidence_releases_lease_and_never_falls_back(
    monkeypatch, tmp_path, task_name: str, failure: str, expected: str,
) -> None:
    module = import_module(f"cairn.dispatcher.tasks.{task_name}")
    phase = f"{task_name}_execute"
    session = "pi:07b53667-a6ab-44be-badb-e3ed6b347331"
    driver = PiDriver()
    monkeypatch.setattr(driver, "prepare_session", lambda: session)
    monkeypatch.setattr(module, "get_driver", lambda _name: driver)
    data = {"fact": {"description": "partial fixture evidence"}}
    if task_name == "bootstrap" and failure != "incomplete":
        data["complete"] = {"description": "fixture completion"}
    elif task_name == "explore" and failure == "incomplete":
        data["fact"].pop("description")
    stdout = json.dumps({
        "protocol": "cairn-agent-result-v1", "engine": "pi", "project_id": "p1",
        "task_run_id": "tr1", "phase": phase, "session": session,
        "status": "succeeded", "submission_id": "a" * 64, "usage": {}, "error": None,
        "result": {"protocol": "cairn-assessment-v2", "accepted": True,
                   "kind": f"{task_name}_result", "data": data},
    })
    if failure == "malformed":
        stdout = "not a program-generated envelope"

    class TrackedResult(ProcessResult):
        cleanup_calls = 0

        def cleanup(self):
            self.cleanup_calls += 1
            super().cleanup()

    stdout_path, stderr_path = tmp_path / "execute.stdout", tmp_path / "execute.stderr"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text("raw fixture evidence", encoding="utf-8")
    execute = TrackedResult(
        returncode=137 if failure in {"timeout", "cancelled", "oom"} else 0,
        stdout="preview is not the result", stderr="preview is not the evidence",
        _stdout_path=str(stdout_path), _stderr_path=str(stderr_path),
        timed_out=failure in {"timeout", "cancelled", "oom"},
        cancelled=failure == "cancelled", oom_killed=failure in {"oom", "cancelled"},
        oom_evidence_source="cgroup_v2_memory_events" if failure in {"oom", "cancelled"} else None,
    )
    lease_events, calls, releases, artifacts, validations = [], [], [], [], []
    lease = SimpleNamespace(
        failure=None, start=lambda: lease_events.append("start"),
        stop=lambda: lease_events.append("stop"),
    )
    monkeypatch.setattr(module, "HeartbeatLease", SimpleNamespace(for_intent=lambda *args: lease))

    def run_process(_manager, _container, _worker, invocation, **kwargs):
        request = json.loads(invocation.input_files[0].content)
        calls.append((request, kwargs))
        return execute

    monkeypatch.setattr(module, "run_worker_process", run_process)
    monkeypatch.setattr(module, f"{task_name}_prompt_replacements", lambda *args, **kwargs: {})
    monkeypatch.setattr(module, "load_prompt", lambda *args: "fixture prompt")
    monkeypatch.setattr(module, "record_prompt_rendered", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "record_validation_result", lambda *args, **kwargs: validations.append(kwargs))

    def no_business_write(*args, **kwargs):
        pytest.fail("失败执行不能写业务结论、收集声明证据或启动 conclude fallback")

    monkeypatch.setattr(driver, "build_conclude", no_business_write)
    monkeypatch.setattr(module, "record_declared_artifacts", no_business_write)
    monkeypatch.setattr(module, "write_bootstrap_complete_result" if task_name == "bootstrap"
                        else "write_conclude_result_with_fact_id", no_business_write)

    class Client:
        def create_artifact(self, _project, **kwargs):
            artifacts.append(kwargs)
            return ApiResult(201, data={"id": f"a{len(artifacts):03d}"})

        def release(self, *args, task_run_id=None):
            assert task_run_id == "tr1"
            releases.append(args)
            return ApiResult(200)

        def create_agent_event(self, *args, **kwargs):
            return ApiResult(201)

        def conclude_with_evidence(self, *args, **kwargs):
            no_business_write()

        def update_task_run(self, *args, **kwargs):
            return ApiResult(200)

    worker = WorkerConfig(
        id="fixture-pi", engine="pi", task_types=[task_name], concurrency=1, priority=0,
        model_binding=PiModelConfig(
            protocol="anthropic-messages", model_name="fixture-model",
            base_url="https://fixture.invalid", inference_endpoint="https://fixture.invalid/messages",
            auth_header="x-api-key", api_key="fixture-not-a-real-key",
        ),
    )
    config = SimpleNamespace(
        runtime=SimpleNamespace(healthcheck_timeout=5, interval=1, prompt_group="default"),
        tasks=SimpleNamespace(**{task_name: SimpleNamespace(timeout=31)}),
    )
    project = SimpleNamespace(project=SimpleNamespace(
        id="p1", vulnerability_fingerprint_version=None,
        assessment_execution={"version": 2, "executable": True, "execution_contract_version": 1,
                              "execution_engine": "pi", "enabled": False},
    ), facts=[], hints=[])
    kwargs = dict(
        config=config, client=Client(),
        container_manager=SimpleNamespace(
            ensure_running=lambda *args: "fixture-container",
            runtime_metadata=lambda *args: {"container_id": "fixture-container-id", "container_state": "running"},
        ),
        project=project, intent=SimpleNamespace(id="i1", slot_key=None, assessment_role="discovery"),
        worker=worker, cancellation=TaskCancellation(), task_run_id="tr1",
    )
    if task_name == "explore":
        kwargs["export_yaml"] = "fixture graph"
    try:
        outcome = getattr(module, f"run_{task_name}_task")(**kwargs)
        assert worker_failure_error_type(outcome) == expected
        assert outcome.phase == phase
        assert outcome.exit_code == execute.returncode
        assert len(calls) == 1
        request, invocation = calls[0]
        assert request["session"] == session
        assert request["phase"] == invocation["phase"] == phase
        assert request["timeout_seconds"] == invocation["timeout_seconds"]
        assert 0 < request["timeout_seconds"] <= 31
        assert request["resume"] is False
        assert request["conclude_only"] is False
        assert releases == [("p1", "i1", "fixture-pi")]
        assert lease_events == ["start", "stop"]
        assert execute.cleanup_calls == 1
        assert not stdout_path.exists() and not stderr_path.exists()
        stored = [artifact["content"] for artifact in artifacts]
        assert stdout not in stored
        assert "raw fixture evidence" not in stored
        if failure in {"incomplete", "malformed"}:
            assert validations[0]["outcome"] == expected
    finally:
        if stdout_path.exists() or stderr_path.exists():
            execute.cleanup()
