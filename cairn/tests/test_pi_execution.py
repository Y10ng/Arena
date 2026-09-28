from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import uuid

import pytest
import requests

from cairn.dispatcher.config import PiModelConfig, RuntimeConfig, WorkerConfig
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks import delivery as delivery_module
from cairn.dispatcher.tasks import hook_records, process_runner
from cairn.dispatcher.tasks.bootstrap_context import bootstrap_retry_prompt_context
from cairn.dispatcher.tasks.candidate_capture_outbox import CandidateCaptureOutbox
from cairn.dispatcher.tasks.delivery import (
    DeliveryUnavailable,
    model_binding_digest,
    result_schema_digest,
    resume_delivery,
)
from cairn.dispatcher.worker_protocol.submission import build_submit_result_schema
from cairn.dispatcher.worker_result_gateway import (
    WorkerRequestError,
    WorkerResultFailure,
    worker_failure_error_type,
)
from cairn.dispatcher.workers.adapters.pi import PiDriver
from cairn.dispatcher.workers.registry import DRIVERS, get_driver
from report_test_support import ephemeral_report_finish_outbox


def worker(protocol="anthropic-messages"):
    return WorkerConfig(
        id="pi-worker", engine="pi", task_types=["bootstrap", "reason", "explore"],
        concurrency=1, priority=0,
        model_binding=PiModelConfig(
            protocol=protocol, model_name="fixture-model",
            inference_endpoint="https://fixture.invalid/custom/infer?api=2",
            base_url="https://fixture.invalid/custom", auth_header="X-Fixture-Key",
            api_key="fixture-secret", custom_headers=[{"name": "X-Trace", "value": "fixture-header"}],
        ),
    )


@pytest.mark.parametrize("protocol", ["anthropic-messages", "openai-chat-completions", "openai-responses"])
@pytest.mark.parametrize("phase", [
    "bootstrap_execute", "bootstrap_conclude", "reason_execute", "explore_execute",
    "explore_conclude", "verifier_execute", "verifier_conclude", "report_execute",
])
def test_all_phases_use_private_sdk_requests_and_preserve_model_binding(protocol, phase):
    driver = PiDriver()
    session = driver.prepare_session()
    invocation = driver.build_execute(
        worker(protocol), "Do not infer project=fake phase=wrong from this prompt.", session,
        phase=phase, project_id="project-authoritative", task_run_id="run-authoritative",
        timeout_seconds=13, resume=phase.endswith("_conclude"), conclude_only=phase.endswith("_conclude"),
    )
    assert invocation.argv == ["cairn-agent", "--request", invocation.input_files[0].path]
    assert len(invocation.input_files) == 2
    input_file, candidate_file = invocation.input_files
    assert input_file.mode == candidate_file.mode == 0o600
    assert input_file.owner == candidate_file.owner == "kali"
    assert input_file.path.startswith(input_file.allowed_root + "/")
    assert invocation.candidate_spool is not None
    assert candidate_file.path == invocation.candidate_spool.path
    assert candidate_file.content == ""
    request = json.loads(input_file.content)
    assert request["phase"] == phase
    assert request["project_id"] == "project-authoritative"
    assert request["task_run_id"] == "run-authoritative"
    assert request["protocol"] == "cairn-agent-request-v1"
    assert request["model"] == worker(protocol).model_binding.model_dump()
    assert request["session"] == session
    assert str(uuid.UUID(session[3:])) == session[3:]
    assert request["resume"] is phase.endswith("_conclude")
    assert request["conclude_only"] is phase.endswith("_conclude")
    assert request["max_result_repairs"] == 2
    assert request["timeout_seconds"] == 13
    assert request["conclude_timeout_seconds"] == 0
    assert request["cwd"] == "/home/kali/workspace"
    assert isinstance(request["result_schema"], dict)
    assert request["capabilities"] == {
        "candidate_capture": {
            "protocol": invocation.candidate_spool.protocol,
            "spool_path": invocation.candidate_spool.path,
            "capture_id": invocation.candidate_spool.capture_id,
            "max_candidates": invocation.candidate_spool.max_candidates,
            "max_candidate_bytes": invocation.candidate_spool.max_candidate_bytes,
            "max_manifest_bytes": invocation.candidate_spool.max_manifest_bytes,
            "max_spool_bytes": invocation.candidate_spool.max_spool_bytes,
            "forbidden_literals": [],
        }
    }
    assert invocation.checkpoint_spool is None
    assert "fixture-secret" not in str(invocation.argv)
    assert "fixture-header" not in str(invocation.argv)
    assert "fixture-secret" not in repr(worker(protocol))


def test_pi_request_carries_conclusion_reserve_inside_total_timeout() -> None:
    driver = PiDriver()
    invocation = driver.build_execute(
        worker(),
        "Explore",
        None,
        phase="explore_execute",
        project_id="p",
        task_run_id="t",
        timeout_seconds=13,
        conclude_timeout_seconds=5,
    )
    request = json.loads(invocation.input_files[0].content)
    assert request["timeout_seconds"] == 13
    assert request["conclude_timeout_seconds"] == 5

    for value in (-1, 14, 1.5, True):
        with pytest.raises(ValueError, match="conclude timeout"):
            driver.build_execute(
                worker(),
                "Explore",
                None,
                phase="explore_execute",
                project_id="p",
                task_run_id="t",
                timeout_seconds=13,
                conclude_timeout_seconds=value,
            )


def test_candidate_capability_includes_only_explicit_sensitive_environment_literals() -> None:
    configured = worker().model_copy(
        update={
            "env": {
                "PUBLIC_SETTING": "visible",
                "SERVICE_TOKEN": "token-value",
                "PRIVATE_KEY": "private-value",
            }
        }
    )

    invocation = PiDriver().build_execute(
        configured,
        "Explore",
        None,
        phase="explore_execute",
        project_id="p",
        task_run_id="t",
        timeout_seconds=5,
    )

    request = json.loads(invocation.input_files[0].content)
    assert request["capabilities"]["candidate_capture"]["forbidden_literals"] == [
        "private-value",
        "token-value",
    ]


def test_candidate_authority_digests_match_ecmascript_canonical_json() -> None:
    driver = PiDriver()
    production = driver.build_execute(
        worker(),
        "Explore",
        None,
        phase="explore_execute",
        project_id="p",
        task_run_id="t-production",
        timeout_seconds=5,
        binding=None,
    )
    assert production.candidate_spool is not None
    assert production.candidate_spool.binding_sha256 == (
        "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b"
    )
    assert production.candidate_spool.result_schema_sha256 == (
        "03f431a1b170e8d6f8823a2ba54c99194b4959328157c72ce4fba55f539eea9d"
    )
    assert result_schema_digest("explore_execute", None) == (
        production.candidate_spool.result_schema_sha256
    )

    vector = driver.build_execute(
        worker(),
        "Explore",
        None,
        phase="explore_execute",
        project_id="p",
        task_run_id="t-vector",
        timeout_seconds=5,
        binding={
            "\U00010000": 1,
            "": 2,
            "中文": "值",
            "numbers": [-0.0, 1e-6, 1e-7, 1.0, 1e20, 1e21, 0.5],
        },
    )
    assert vector.candidate_spool is not None
    assert vector.candidate_spool.binding_sha256 == (
        "602937fdddb468c4981ff96c2ecc522935c094f489ec79d8092cfd4a135601cd"
    )

    with pytest.raises(WorkerRequestError) as caught:
        driver.build_execute(
            worker(),
            "Explore",
            None,
            phase="explore_execute",
            project_id="p",
            task_run_id="t-unsafe-integer",
            timeout_seconds=5,
            binding={"number": 2**53},
        )
    assert caught.value.failure.failure_class == "request_contract"
    assert caught.value.failure.origin == "dispatcher_request"
    assert caught.value.failure.path == "binding"


def test_result_schema_build_failure_is_request_contract(monkeypatch) -> None:
    from cairn.dispatcher.workers.adapters import pi as pi_adapter

    def invalid_schema(*_args, **_kwargs):
        raise ValueError("synthetic schema failure")

    monkeypatch.setattr(pi_adapter, "build_submit_result_schema", invalid_schema)
    with pytest.raises(WorkerRequestError) as caught:
        PiDriver().build_execute(
            worker(),
            "Explore",
            None,
            phase="explore_execute",
            project_id="p",
            task_run_id="t-invalid-schema",
            timeout_seconds=5,
            binding=None,
        )
    assert caught.value.failure.failure_class == "request_contract"
    assert caught.value.failure.origin == "dispatcher_request"
    assert caught.value.failure.code == "invalid_result_schema"
    assert caught.value.failure.path == "result_schema"


def test_delivery_resume_rejects_result_schema_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_worker = worker()
    snapshot = {
        "session_id": "pi:11111111-1111-4111-8111-111111111111",
        "phase": "explore_execute",
        "deadline_at": "2099-09-19T12:00:00+00:00",
        "container_id": "container-frozen",
        "model_binding_sha256": model_binding_digest(configured_worker),
        "result_schema_sha256": result_schema_digest("explore_execute", None),
        "binding": None,
        "inputs": {},
    }

    manager_calls: list[str] = []

    class ContainerManager:
        @staticmethod
        def container_name(project_id: str) -> str:
            manager_calls.append("container_name")
            assert project_id == "project-frozen"
            return "container-name"

        @staticmethod
        def runtime_metadata(name: str) -> dict[str, str]:
            manager_calls.append("runtime_metadata")
            assert name == "container-name"
            return {
                "container_state": "running",
                "container_id": "container-frozen",
            }

    container_id, resumed, _remaining = resume_delivery(
        ContainerManager(),
        "project-frozen",
        configured_worker,
        {"delivery_retry": {"source_task_run_id": "run-1"}, "execution_delivery": snapshot},
        "explore_execute",
    )
    assert container_id == "container-frozen"
    assert resumed == snapshot
    assert manager_calls == ["container_name", "runtime_metadata"]
    manager_calls.clear()

    monkeypatch.setattr(
        delivery_module,
        "build_submit_result_schema",
        lambda *_args, **_kwargs: {"type": "object", "properties": {}},
    )
    with pytest.raises(DeliveryUnavailable):
        resume_delivery(
            ContainerManager(),
            "project-frozen",
            configured_worker,
            {
                "delivery_retry": {"source_task_run_id": "run-1"},
                "execution_delivery": snapshot,
            },
            "explore_execute",
        )
    assert manager_calls == []


def test_delivery_registration_never_runs_past_authorization_expiry() -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=60)
    updates = []
    client = SimpleNamespace(
        update_task_run=lambda *args, **kwargs: (
            updates.append((args, kwargs)) or SimpleNamespace(ok=True)
        )
    )
    manager = SimpleNamespace(
        runtime_metadata=lambda _name: {
            "container_id": "original-container",
            "container_state": "running",
        }
    )
    project = SimpleNamespace(
        project=SimpleNamespace(
            id="p",
            explore_deadline=None,
            authorization_expires_at=expires_at.isoformat(),
        )
    )

    snapshot = delivery_module.register_delivery(
        client,
        manager,
        "original-name",
        project,
        "run-1",
        worker(),
        phase="reason_execute",
        session="pi:11111111-1111-4111-8111-111111111111",
        timeout_seconds=120,
        binding={"allowed_fact_ids": ["origin"], "max_intents": 3},
        inputs={"open_intent_ids": []},
    )

    assert datetime.fromisoformat(snapshot["deadline_at"]) == expires_at
    assert updates[0][1]["runtime_context"]["execution_delivery"] == snapshot


def test_checkpoint_capability_materializes_a_private_unique_spool() -> None:
    driver = PiDriver()
    first = driver.build_execute(
        worker(), "Explore", None, phase="explore_execute", project_id="p",
        task_run_id="t1", timeout_seconds=5, enable_checkpoints=True,
    )
    second = driver.build_execute(
        worker(), "Explore", None, phase="explore_execute", project_id="p",
        task_run_id="t2", timeout_seconds=5, enable_checkpoints=True,
    )

    assert first.checkpoint_spool is not None
    assert second.checkpoint_spool is not None
    assert first.checkpoint_spool.path != second.checkpoint_spool.path
    assert first.candidate_spool is not None
    assert second.candidate_spool is not None
    assert first.candidate_spool.path != second.candidate_spool.path
    assert len(first.input_files) == 3
    request_file, candidate_file, checkpoint_file = first.input_files
    assert first.argv == ["cairn-agent", "--request", request_file.path]
    assert request_file.mode == candidate_file.mode == checkpoint_file.mode == 0o600
    assert request_file.owner == candidate_file.owner == checkpoint_file.owner == "kali"
    assert candidate_file.path == first.candidate_spool.path
    assert checkpoint_file.path == first.checkpoint_spool.path
    assert candidate_file.content == checkpoint_file.content == ""
    request = json.loads(request_file.content)
    assert request["capabilities"] == {
        "candidate_capture": {
            "protocol": first.candidate_spool.protocol,
            "spool_path": first.candidate_spool.path,
            "capture_id": first.candidate_spool.capture_id,
            "max_candidates": first.candidate_spool.max_candidates,
            "max_candidate_bytes": first.candidate_spool.max_candidate_bytes,
            "max_manifest_bytes": first.candidate_spool.max_manifest_bytes,
            "max_spool_bytes": first.candidate_spool.max_spool_bytes,
            "forbidden_literals": [],
        },
        "execution_checkpoints": {
            "protocol": first.checkpoint_spool.protocol,
            "spool_path": first.checkpoint_spool.path,
            "max_events": first.checkpoint_spool.max_events,
            "max_event_bytes": first.checkpoint_spool.max_event_bytes,
            "max_spool_bytes": first.checkpoint_spool.max_spool_bytes,
        },
    }


@pytest.mark.parametrize(
    ("phase", "resume", "conclude_only"),
    [
        ("report_execute", False, False),
        ("explore_execute", True, False),
        ("explore_conclude", True, True),
    ],
)
def test_checkpoint_spool_is_forbidden_for_non_exploration_delivery_modes(
    phase: str,
    resume: bool,
    conclude_only: bool,
) -> None:
    invocation = PiDriver().build_execute(
        worker(), "Use the existing result", PiDriver().prepare_session() if resume else None,
        phase=phase, project_id="p", task_run_id="t", timeout_seconds=5,
        resume=resume, conclude_only=conclude_only, enable_checkpoints=True,
    )

    assert invocation.checkpoint_spool is None
    assert invocation.candidate_spool is not None
    assert len(invocation.input_files) == 2
    capabilities = json.loads(invocation.input_files[0].content)["capabilities"]
    assert set(capabilities) == {"candidate_capture"}


@pytest.fixture(scope="module")
def built_pi_protocol():
    root = Path(__file__).resolve().parents[2] / "container" / "pi-agent"
    subprocess.run(["npm", "--prefix", str(root), "run", "build"], check=True, capture_output=True, text=True)
    return root, (root / "dist" / "protocol.js").as_uri()


def reason_admission_context(phase: str) -> dict[str, object] | None:
    if phase not in {"mapping", "baseline"}:
        return None
    payload = {
        "version": 1,
        "assessment_phase": phase,
        "issued_at": "2026-09-19T08:00:00Z",
        "eligible_cells": [
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
        ],
    }
    payload["digest"] = sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {"structured_intent_binding": payload}


@pytest.mark.parametrize("phase", ["mapping", "baseline", "deep", "finalizing", "depth_review"])
@pytest.mark.parametrize("digest", [None, "b" * 64, "", "A" * 64, "bad", 42])
def test_reason_context_request_crosses_real_typescript_parser(built_pi_protocol, phase, digest):
    from test_reason_depth_review import build_context, execution_view, project_with_execution

    execution = execution_view(phase=phase, review_input_digest=digest)
    admission_context = reason_admission_context(phase)
    if digest is not None and (not isinstance(digest, str) or digest != "b" * 64) or (phase == "depth_review" and digest is None):
        with pytest.raises(ValueError, match="review_input_digest"):
            build_context(
                project_with_execution(execution),
                admission_context=admission_context,
            )
        return
    context, _ = build_context(
        project_with_execution(execution),
        admission_context=admission_context,
    )
    bound_worker = worker().model_copy(update={
        "model_binding": worker().model_binding.model_copy(update={"auth_header": "x-api-key"}),
    })
    invocation = PiDriver().build_execute(
        bound_worker, "Use frozen execution", None, phase="reason_execute", project_id="p1",
        task_run_id="reason-wire", timeout_seconds=5, binding=context.binding,
    )
    root, protocol_url = built_pi_protocol
    parsed = subprocess.run(
        ["node", "--input-type=module", "-e", f'''
import {{ Compile }} from "typebox/compile";
import {{ parseRequest }} from {json.dumps(protocol_url)};
let text = "";
for await (const chunk of process.stdin) text += chunk;
const request = parseRequest(JSON.parse(text));
const validator = Compile(request.result_schema);
const valid = {{ accepted: true, data: {{ intents: [{{
  from: ["f1"], description: "Check A", slot_key: "cell-a", strategy_key: "shared.v1"
}}] }} }};
const blocked = {{ accepted: true, data: {{ intents: [{{
  from: ["f1"], description: "Check B", slot_key: "cell-b", strategy_key: "blocked.v1"
}}] }} }};
process.stdout.write(JSON.stringify({{
  binding: request.binding,
  schema_compiled: true,
  valid_structured_intent: request.binding?.structured_intent_binding === undefined
    ? null : validator.Check(valid),
  blocked_structured_intent: request.binding?.structured_intent_binding === undefined
    ? null : validator.Check(blocked),
}}));
'''], input=invocation.input_files[0].content, capture_output=True, text=True,
        cwd=root,
    )
    assert parsed.returncode == 0, parsed.stderr
    result = json.loads(parsed.stdout)
    assert result["schema_compiled"] is True
    binding = result["binding"]
    assert binding["allowed_fact_ids"] == ["f1"]
    assert binding["max_intents"] == (3 if phase == "depth_review" else 5)
    if admission_context is None:
        assert "structured_intent_binding" not in binding
        assert result["valid_structured_intent"] is None
        assert result["blocked_structured_intent"] is None
    else:
        assert binding["structured_intent_binding"] == admission_context[
            "structured_intent_binding"
        ]
        assert result["valid_structured_intent"] is True
        assert result["blocked_structured_intent"] is False
    if digest is None:
        assert "review_input_digest" not in binding
    else:
        assert binding["review_input_digest"] == digest
    # 领域原始快照不能因 wire optional 规则被递归清洗。
    assert json.loads(context.replacements["assessment_execution"])["depth_review_fact_id"] is None
    assert execution["review_input_digest"] == digest


def test_invalid_depth_binding_stops_reason_before_execution(monkeypatch):
    from cairn.dispatcher.tasks import reason
    from cairn.dispatcher.worker_result_gateway import WorkerResultFailure, worker_failure_error_type
    from test_reason_depth_review import execution_view, project_with_execution

    calls = []
    lease = SimpleNamespace(start=lambda: None, stop=lambda: None, failure=None)
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", lambda *a: lease)
    monkeypatch.setattr(reason, "best_effort_release_reason", lambda *a, **k: None)
    monkeypatch.setattr(reason, "record_validation_result", lambda *a, **k: calls.append(k))
    config = SimpleNamespace(
        runtime=SimpleNamespace(healthcheck_timeout=3, interval=1, prompt_group="default"),
        tasks=SimpleNamespace(reason=SimpleNamespace(max_intents=3, timeout=5)),
    )
    failure = reason.run_reason_task(
        config, SimpleNamespace(), SimpleNamespace(ensure_running=lambda *a: "fixture"),
        project_with_execution(execution_view(review_input_digest=None)), "facts: []", worker(), TaskCancellation(), "t",
    )
    assert isinstance(failure, WorkerResultFailure)
    assert worker_failure_error_type(failure) == "request_contract"
    assert failure.path == "binding.review_input_digest"


@pytest.mark.parametrize("invalid_context", [None, {"structured_intent_binding": {"version": 1}}])
def test_invalid_structured_reason_admission_stops_before_container_start(
    monkeypatch,
    invalid_context,
):
    from cairn.dispatcher.tasks import reason
    from test_reason_depth_review import execution_view, project_with_execution

    calls = []
    lease = SimpleNamespace(start=lambda: None, stop=lambda: None, failure=None)
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", lambda *a: lease)
    monkeypatch.setattr(
        reason,
        "best_effort_release_reason",
        lambda *a, **k: calls.append("released"),
    )
    monkeypatch.setattr(
        reason,
        "record_validation_result",
        lambda *a, **k: calls.append(("validation", k)),
    )
    config = SimpleNamespace(
        runtime=SimpleNamespace(
            healthcheck_timeout=3,
            interval=1,
            prompt_group="default-v2",
        ),
        tasks=SimpleNamespace(reason=SimpleNamespace(max_intents=3, timeout=5)),
    )
    manager = SimpleNamespace(
        ensure_running=lambda *a: calls.append("container")
    )

    failure = reason.run_reason_task(
        config,
        SimpleNamespace(),
        manager,
        project_with_execution(
            execution_view(phase="baseline", review_input_digest=None)
        ),
        "facts: []",
        worker(),
        TaskCancellation(),
        "t",
        admission_context=invalid_context,
    )

    assert isinstance(failure, WorkerResultFailure)
    assert worker_failure_error_type(failure) == "request_contract"
    assert failure.code == "invalid_request"
    assert failure.path == "binding.structured_intent_binding"
    assert "container" not in calls
    assert calls[-1] == "released"


def test_structured_reason_prompt_without_binding_token_stops_before_container_start(
    monkeypatch,
):
    from cairn.dispatcher.tasks import reason
    from test_reason_depth_review import execution_view, project_with_execution

    calls = []
    lease = SimpleNamespace(start=lambda: None, stop=lambda: None, failure=None)
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", lambda *a: lease)
    monkeypatch.setattr(reason, "load_prompt", lambda *a: "No binding token")
    monkeypatch.setattr(
        reason,
        "best_effort_release_reason",
        lambda *a, **k: calls.append("released"),
    )
    monkeypatch.setattr(
        reason,
        "record_validation_result",
        lambda *a, **k: calls.append(("validation", k)),
    )
    config = SimpleNamespace(
        runtime=SimpleNamespace(
            healthcheck_timeout=3,
            interval=1,
            prompt_group="default-v2",
        ),
        tasks=SimpleNamespace(reason=SimpleNamespace(max_intents=3, timeout=5)),
    )
    manager = SimpleNamespace(
        ensure_running=lambda *a: calls.append("container")
    )

    failure = reason.run_reason_task(
        config,
        SimpleNamespace(),
        manager,
        project_with_execution(
            execution_view(phase="baseline", review_input_digest=None)
        ),
        "facts: []",
        worker(),
        TaskCancellation(),
        "t",
        admission_context=reason_admission_context("baseline"),
    )

    assert isinstance(failure, WorkerResultFailure)
    assert worker_failure_error_type(failure) == "request_contract"
    assert failure.code == "structured_prompt_binding_unsupported"
    assert failure.path == "prompt.structured_intent_binding"
    assert "container" not in calls
    assert calls[-1] == "released"


def test_missing_explore_admission_binding_stops_before_container_start(monkeypatch):
    from cairn.dispatcher.tasks import explore
    from cairn.server.models import Intent

    calls = []
    lease = SimpleNamespace(start=lambda: None, stop=lambda: None, failure=None)
    monkeypatch.setattr(explore.HeartbeatLease, "for_intent", lambda *a: lease)
    monkeypatch.setattr(
        explore,
        "best_effort_release",
        lambda *a, **k: calls.append("released"),
    )
    project = SimpleNamespace(
        project=SimpleNamespace(
            id="p1",
            assessment_execution={
                "version": 2,
                "executable": True,
                "execution_contract_version": 1,
                "execution_engine": "pi",
                "enabled": True,
                "structured": True,
                "surface_manifest_enabled": True,
                "outcome_contract_version": 2,
                "blocker_cooldown_enabled": True,
            },
            vulnerability_fingerprint_version=1,
        ),
        facts=[],
    )
    intent = Intent(
        id="i1",
        **{"from": ["origin"]},
        description="test authorization",
        creator="reason",
        created_at="2026-09-19T08:00:00Z",
        slot_key="cell-1",
        strategy_key="auth.owner-vs-other-v1",
    )
    config = SimpleNamespace(
        runtime=SimpleNamespace(
            healthcheck_timeout=3,
            interval=1,
            prompt_group="default-v2",
        ),
        tasks=SimpleNamespace(explore=SimpleNamespace(timeout=5)),
    )
    manager = SimpleNamespace(
        ensure_running=lambda *a: calls.append("container")
    )

    failure = explore.run_explore_task(
        config,
        SimpleNamespace(),
        manager,
        project,
        "facts: []",
        intent,
        worker(),
        TaskCancellation(),
        "t1",
    )

    assert isinstance(failure, WorkerResultFailure)
    assert worker_failure_error_type(failure) == "request_contract"
    assert failure.code == "admission_contract_invalid"
    assert failure.path == "runtime_context.structured_explore_binding"
    assert calls == ["released"]


def _verifier_preflight_fixture(monkeypatch, failure, *, target_observation_id="vo-1"):
    from cairn.dispatcher.tasks import explore
    from cairn.server.models import Intent

    calls = []
    lease = SimpleNamespace(start=lambda: None, stop=lambda: None, failure=None)
    monkeypatch.setattr(explore.HeartbeatLease, "for_intent", lambda *a: lease)
    monkeypatch.setattr(
        explore,
        "best_effort_release",
        lambda *a, **k: calls.append("released"),
    )

    class Client:
        def get_observation_verifier_target(self, project_id, observation_id):
            raise failure

        def release(self, project_id, intent_id, worker_name, **kwargs):
            calls.append("released")
            return SimpleNamespace(ok=True, status_code=200)

    project = SimpleNamespace(
        project=SimpleNamespace(
            id="p1",
            assessment_execution={
                "version": 2,
                "executable": True,
                "execution_contract_version": 1,
                "execution_engine": "pi",
                "enabled": True,
                "structured": True,
                "surface_manifest_enabled": True,
                "outcome_contract_version": 2,
                "blocker_cooldown_enabled": True,
            },
            vulnerability_fingerprint_version=1,
        ),
        facts=[],
    )
    intent = Intent(
        id="i-verifier",
        **{"from": ["origin"]},
        description="verify observation",
        creator="system",
        created_at="2026-09-19T08:00:00Z",
        assessment_role="verifier",
        target_observation_id=target_observation_id,
    )
    config = SimpleNamespace(
        runtime=SimpleNamespace(
            healthcheck_timeout=3,
            interval=1,
            prompt_group="default-v2",
        ),
        tasks=SimpleNamespace(explore=SimpleNamespace(timeout=5)),
    )
    manager = SimpleNamespace(
        ensure_running=lambda *a: calls.append("container")
    )
    result = explore.run_explore_task(
        config,
        Client(),
        manager,
        project,
        "facts: []",
        intent,
        worker(),
        TaskCancellation(),
        "t-verifier",
    )
    return result, calls


def test_missing_verifier_target_binding_stops_before_container_start(monkeypatch):
    result, calls = _verifier_preflight_fixture(
        monkeypatch,
        AssertionError("target lookup must not run"),
        target_observation_id=None,
    )

    assert isinstance(result, WorkerResultFailure)
    assert worker_failure_error_type(result) == "request_contract"
    assert result.code == "verifier_target_binding_invalid"
    assert result.path == "binding.verification_observation_id"
    assert result.origin == "dispatcher_admission"
    assert calls == ["released"]


@pytest.mark.parametrize(
    ("status_code", "detail", "expected_error_type", "expected_code"),
    [
        (404, "vulnerability_observation_not_found", "stale_business_state", "vulnerability_observation_not_found"),
        (422, "observation_discovery_context_incomplete", "business_acceptance", "observation_discovery_context_incomplete"),
        (503, "server_unavailable", "server_write_failed", "server_unavailable"),
    ],
)
def test_verifier_target_http_failure_has_stable_classification(
    monkeypatch,
    status_code,
    detail,
    expected_error_type,
    expected_code,
):
    response = requests.Response()
    response.status_code = status_code
    response.headers["content-type"] = "application/json"
    response._content = json.dumps({"detail": detail}).encode()
    failure = requests.HTTPError(
        f"status {status_code}",
        response=response,
    )

    result, calls = _verifier_preflight_fixture(monkeypatch, failure)

    assert isinstance(result, WorkerResultFailure)
    assert worker_failure_error_type(result) == expected_error_type
    assert result.code == expected_code
    assert result.origin == "server_admission"
    assert calls == ["released"]


def test_verifier_target_transport_failure_is_not_business_rejection(monkeypatch):
    result, calls = _verifier_preflight_fixture(
        monkeypatch,
        requests.ConnectionError("server unavailable"),
    )

    assert isinstance(result, WorkerResultFailure)
    assert worker_failure_error_type(result) == "server_write_failed"
    assert result.code == "verifier_target_unavailable"
    assert result.origin == "server_admission"
    assert calls == ["released"]


def test_invalid_verifier_target_payload_is_a_request_contract_failure(monkeypatch):
    result, calls = _verifier_preflight_fixture(
        monkeypatch,
        ValueError("invalid verifier target payload"),
    )

    assert isinstance(result, WorkerResultFailure)
    assert worker_failure_error_type(result) == "request_contract"
    assert result.code == "verifier_target_invalid"
    assert result.path == "verifier_target"
    assert result.origin == "server_admission"
    assert calls == ["released"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prompt", 1),
        ("prompt", ""),
        ("phase", True),
        ("project_id", True),
        ("project_id", ""),
        ("task_run_id", 1),
        ("task_run_id", ""),
        ("timeout_seconds", True),
        ("timeout_seconds", 1.5),
        ("resume", 1),
        ("conclude_only", 1),
        ("enable_checkpoints", 1),
    ],
)
def test_pi_driver_rejects_non_literal_request_primitives(
    field: str,
    value: object,
) -> None:
    arguments: dict[str, object] = {
        "prompt": "prompt",
        "phase": "reason_execute",
        "project_id": "p",
        "task_run_id": "t",
        "timeout_seconds": 5,
        "resume": False,
        "conclude_only": False,
        "enable_checkpoints": False,
    }
    arguments[field] = value

    with pytest.raises(WorkerRequestError) as caught:
        PiDriver().build_execute(
            worker(),
            arguments.pop("prompt"),  # type: ignore[arg-type]
            PiDriver().prepare_session(),
            **arguments,  # type: ignore[arg-type]
        )
    assert caught.value.failure.failure_class == "request_contract"
    assert caught.value.failure.origin == "dispatcher_request"
    assert caught.value.failure.path == field


@pytest.mark.parametrize(
    "phase",
    [
        "bootstrap_execute",
        "reason_execute",
        "explore_execute",
        "verifier_execute",
        "report_execute",
    ],
)
def test_execute_phase_can_resume_as_readonly_delivery(phase: str) -> None:
    session = PiDriver().prepare_session()
    invocation = PiDriver().build_execute(
        worker(),
        "Use existing evidence only",
        session,
        phase=phase,
        project_id="p",
        task_run_id="t",
        timeout_seconds=5,
        resume=True,
        conclude_only=True,
    )
    request = json.loads(invocation.input_files[0].content)
    assert request["phase"] == phase
    assert request["session"] == session
    assert request["resume"] is True
    assert request["conclude_only"] is True
    assert request["timeout_seconds"] == 5
    assert invocation.checkpoint_spool is None


def test_resume_requires_explicit_existing_pi_identity_and_keeps_authoritative_binding():
    driver = PiDriver()
    session = driver.prepare_session()
    binding = {"slot_key": "cell-1", "surface_key": "surface-1", "test_family": "object_authorization"}
    invocation = driver.build_conclude(
        worker(), "Use existing evidence only", session, phase="explore_conclude",
        project_id="project-1", task_run_id="run-1", timeout_seconds=4, binding=binding,
    )
    request = json.loads(invocation.input_files[0].content)
    assert request["session"] == session
    assert request["resume"] is True
    assert request["conclude_only"] is True
    assert request["binding"] == binding
    for invalid in ("old-claude-session", str(uuid.uuid4()), "pi:broken"):
        with pytest.raises(ValueError, match="session_"):
            driver.build_execute(worker(), "prompt", invalid, phase="reason_execute", project_id="p", task_run_id="t", timeout_seconds=1)
    with pytest.raises(ValueError, match="session_missing"):
        driver.build_execute(worker(), "prompt", None, phase="reason_execute", project_id="p", task_run_id="t", timeout_seconds=1, resume=True)


def test_production_registry_and_configuration_have_no_legacy_or_mock_entry():
    assert set(DRIVERS) == {"pi"}
    assert isinstance(get_driver("pi"), PiDriver)
    for engine in ("mock", "claudecode", "codex"):
        with pytest.raises(KeyError):
            get_driver(engine)
        with pytest.raises(ValueError):
            WorkerConfig(id="rejected", engine=engine, task_types=["reason"], concurrency=1, priority=0)
    assert "allow_mock_workers" not in RuntimeConfig.model_fields
    unbound = worker().model_copy(update={"model_binding": None})
    with pytest.raises(ValueError, match="model_binding_missing"):
        PiDriver().build_execute(
            unbound,
            "prompt",
            None,
            phase="reason_execute",
            project_id="p",
            task_run_id="t",
            timeout_seconds=1,
        )
    with pytest.raises(ValueError, match="unsupported result phase"):
        build_submit_result_schema("healthcheck")
    with pytest.raises(ValueError, match="unsupported result phase"):
        PiDriver().build_execute(
            worker(),
            "prompt",
            None,
            phase="healthcheck",
            project_id="p",
            task_run_id="t",
            timeout_seconds=1,
        )


class FakeProcess:
    execution_identity = ("container-id", "exec-id")
    execution_control_path = "/tmp/cairn-exec-0123456789abcdef0123456789abcdef.json"
    execution_start_attempted = True
    execution_started = True
    execution_completed = True

    def __init__(self, result):
        self.result = result
        self.started = False
        self.on_execution_prepared = None
        self.on_execution_starting = None
        self.control_retained = False

    def retain_execution_control(self):
        self.control_retained = True

    def start(self):
        self.started = True
        if self.on_execution_prepared is not None:
            self.on_execution_prepared(
                *self.execution_identity,
                self.execution_control_path,
            )
        if self.on_execution_starting is not None:
            self.on_execution_starting()

    def communicate(self, timeout):
        return self.result


class FakeManager:
    def __init__(self, result):
        self.process = FakeProcess(result)
        self.writes = []
        self.removes = []
        self.commands = []

    def docker_daemon_id(self):
        return "daemon-1"

    def write_text_file(self, *args, **kwargs):
        self.writes.append((args, kwargs))

    def remove_file(self, *args, **kwargs):
        self.removes.append((args, kwargs))

    def cleanup_candidate_execution_control(self, *args, **kwargs):
        self.removes.append((args, kwargs))

    def build_exec_process(self, container_name, env, command, *, timeout_seconds):
        self.commands.append((list(command), dict(env), timeout_seconds))
        return self.process


def test_failed_worker_process_still_projects_pi_tool_audit(monkeypatch) -> None:
    result = ProcessResult(137, "", "audit stream", timed_out=True)
    manager = FakeManager(result)
    projected = []
    monkeypatch.setattr(
        process_runner,
        "record_pi_tool_events",
        lambda *args, **kwargs: projected.append(kwargs["result"]),
    )
    monkeypatch.setattr(process_runner, "record_payload_operations", lambda *args, **kwargs: None)
    client = SimpleNamespace(
        create_agent_event=lambda *args, **kwargs: SimpleNamespace(ok=True, status_code=201)
    )

    observed = process_runner.run_worker_process(
        manager,
        "fixture",
        worker(),
        ["cairn-agent", "--request", "/tmp/cairn-prompts/request.json"],
        phase="bootstrap_execute",
        timeout_seconds=3,
        client=client,
        project_id="p",
        task_run_id="t",
    )

    assert observed is result
    assert projected == [result]


def healthcheck_envelope(session, **updates):
    value = {
        "protocol": "cairn-agent-result-v1", "engine": "pi", "project_id": "p", "task_run_id": "t",
        "phase": "healthcheck", "session": session, "status": "succeeded",
        "result": {"protocol": "cairn-assessment-v2", "accepted": True, "kind": "healthcheck_result", "data": {"status": "ok"}},
        "submission_id": "a" * 64, "usage": {}, "error": None,
    }
    value.update(updates)
    return json.dumps(value)


def test_historical_healthcheck_request_error_decodes_without_raw_body():
    session = PiDriver().prepare_session()
    stdout = healthcheck_envelope(
        session,
        status="failed",
        result=None,
        submission_id=None,
        error={
            "code": "invalid_request",
            "path": "binding.review_input_digest",
            "message": "private-provider-body",
        },
    )
    result = ProcessResult(1, stdout, "private-stderr-token")
    failure = process_runner.worker_process_failure(
        result,
        phase="healthcheck",
        project_id="p",
        task_run_id="t",
        session=session,
    )

    details = failure.details()
    assert details["error_type"] == "request_contract"
    assert details["exit_code"] == 1
    assert details["origin"] == "pi_request"
    assert details["code"] == "invalid_request"
    assert details["path"] == "binding.review_input_digest"
    assert details["phase"] == "healthcheck"
    assert "private-" not in json.dumps(details)


@pytest.mark.parametrize("returncode,status", [(1, "failed"), (0, "failed"), (1, "succeeded")])
def test_failed_process_outputs_are_not_published_as_raw_artifacts(returncode, status):
    from cairn.dispatcher.tasks.artifact_records import record_process_artifacts
    from cairn.dispatcher.protocol.client import ApiResult

    raw = healthcheck_envelope(PiDriver().prepare_session())
    if status == "failed":
        raw = healthcheck_envelope(PiDriver().prepare_session(), status="failed", result=None, submission_id=None,
                                  error={"code": "provider_error", "message": "private-provider-body"})
    published = []
    client = SimpleNamespace(
        create_artifact=lambda *a, **kw: (published.append(kw) or ApiResult(200, {"id": "a1"})),
        create_agent_event=lambda *a, **kw: (published.append(kw) or ApiResult(200, {})),
    )
    record = record_process_artifacts(
        client, "p", "t", phase="reason_healthcheck", result=ProcessResult(returncode, raw, "private-stderr"),
    )
    assert not published
    assert not record.refs


def test_heartbeat_failure_between_precheck_and_start_is_attached_before_start(
    tmp_path: Path,
):
    invocation = PiDriver().build_execute(
        worker(),
        "prompt",
        None,
        phase="reason_execute",
        project_id="p",
        task_run_id="t",
        timeout_seconds=3,
    )
    observed = []
    lease = SimpleNamespace(failure=None)

    class Process(FakeProcess):
        cancelled = False

        def start(self):
            observed.append(self.cancelled)
            if self.cancelled:
                self.execution_identity = None
                self.execution_start_attempted = False
                self.execution_started = False
            else:
                if self.on_execution_prepared is not None:
                    self.on_execution_prepared(
                        *self.execution_identity,
                        self.execution_control_path,
                    )
                if self.on_execution_starting is not None:
                    self.on_execution_starting()

        def communicate(self, timeout):
            return ProcessResult(1, "", "", cancelled=self.cancelled)

    manager = FakeManager(ProcessResult(0, "unused", ""))
    manager.process = Process(manager.process.result)

    def write_input(*args, **kwargs):
        lease.failure = SimpleNamespace(status_code=409)

    def attach(process):
        if process is not None and lease.failure is not None:
            process.cancelled = True

    manager.write_text_file = write_input
    lease.attach_process = attach
    result = process_runner.run_worker_process(
        manager,
        "fixture",
        worker(),
        invocation,
        phase="reason_execute",
        timeout_seconds=3,
        lease=lease,
        client=SimpleNamespace(
            capture_candidate=lambda *_args, **_kwargs: pytest.fail(
                "cancelled process must not capture candidate bytes"
            ),
            create_agent_event=lambda *_args, **_kwargs: SimpleNamespace(
                ok=True,
                status_code=201,
            ),
        ),
        project_id="p",
        task_run_id="t",
        candidate_outbox=CandidateCaptureOutbox(
            tmp_path / "candidate-outbox",
            owner_id="dispatcher-test",
        ),
    )
    assert observed == [True]
    assert result.cancelled


def test_cancelled_task_does_not_start_another_process_or_write_input_files(tmp_path):
    manager = FakeManager(ProcessResult(0, "unused", ""))
    cancellation = TaskCancellation()
    cancellation.cancel("project_stopped")
    invocation = PiDriver().build_execute(
        worker(),
        "prompt",
        None,
        phase="reason_execute",
        project_id="p",
        task_run_id="t",
        timeout_seconds=3,
    )
    result = process_runner.run_worker_process(
        manager,
        "fixture",
        worker(),
        invocation,
        phase="reason_execute",
        timeout_seconds=3,
        cancellation=cancellation,
        client=object(),
        project_id="p",
        task_run_id="t",
        candidate_outbox=CandidateCaptureOutbox(
            tmp_path / "candidate-outbox",
            owner_id="dispatcher-test",
        ),
    )
    assert result.cancelled
    assert result.cancel_reason == "project_stopped"
    assert manager.commands == manager.writes == manager.removes == []


def test_bootstrap_retry_context_reuses_only_the_previous_related_failure_metadata() -> None:
    previous = {
        "id": "tr-prev",
        "task_type": "bootstrap",
        "intent_id": "intent-1",
        "execution_generation": 4,
        "status": "timed_out",
        "error_type": "worker_timeout",
        "exit_code": 1,
        "started_at": "2026-09-21T00:00:00Z",
        "ended_at": "2026-09-21T00:20:00Z",
        "pause_requested": False,
        "runtime_context": {
            "worker_failure": {
                "origin": "dispatcher_process",
                "code": "deadline_exceeded",
                "phase": "bootstrap_execute",
                "error_type": "worker_timeout",
                "message": "must-not-copy private stderr",
            },
            "project_resources": {
                "bundle_digest": "a" * 64,
                "manifest_path": "/cairn/advisory/manifest.json",
                "resources": [
                    {
                        "source": "project_resource",
                        "id": "resource-1",
                        "kind": "note",
                        "path": "initial.txt",
                        "container_path": "/cairn/advisory/project/initial.txt",
                        "size_bytes": 12,
                        "sha256": "b" * 64,
                    }
                ],
            },
        },
    }

    class Client:
        def list_task_runs(self, project_id):
            assert project_id == "p"
            return SimpleNamespace(ok=True, data=[
                {
                    "id": "tr-other",
                    "task_type": "bootstrap",
                    "intent_id": "intent-other",
                    "execution_generation": 4,
                    "status": "failed",
                    "error_type": "worker_timeout",
                },
                previous,
                {
                    "id": "tr-current",
                    "task_type": "bootstrap",
                    "intent_id": "intent-1",
                    "execution_generation": 4,
                    "status": "running",
                    "error_type": None,
                },
            ])

        def list_task_run_command_events(self, project_id, task_run_ids):
            assert (project_id, task_run_ids) == ("p", ["tr-prev"])
            return SimpleNamespace(ok=True, data=[{
                "task_run_id": "tr-prev",
                "events": [{
                    "kind": "command_finished",
                    "phase": "bootstrap_execute",
                    "status": "failed",
                    "returncode": 1,
                    "timed_out": True,
                    "cancelled": False,
                    "oom_killed": False,
                    "duration_seconds": 1200.0,
                }],
            }])

        def list_task_run_hook_events(self, project_id, task_run_ids):
            assert (project_id, task_run_ids) == ("p", ["tr-prev"])
            return SimpleNamespace(ok=True, data=[{
                "task_run_id": "tr-prev",
                "events": [{
                    "hook_event_name": "tool_execution_start",
                    "tool_name": "bash",
                    "operation_id": "call-1",
                    "status": "started",
                    "recorded_at": "2026-09-21T00:02:13Z",
                }],
            }])

        def list_artifacts(self, project_id, *, task_run_id=None):
            assert (project_id, task_run_id) == ("p", "tr-prev")
            return SimpleNamespace(ok=True, data=[{
                "id": "artifact-1",
                "kind": "hook_log",
                "category": "evidence",
                "uri": "artifact://projects/p/runs/tr-prev/hook-log",
                "sha256": "c" * 64,
                "size": 2048,
                "metadata": {"secret": "must-not-copy artifact metadata"},
            }])

    context = bootstrap_retry_prompt_context(
        Client(),
        "p",
        "tr-current",
        "intent-1",
        4,
    )

    assert "Previous bounded Bootstrap attempt" in context
    assert '"source_task_run_id": "tr-prev"' in context
    assert '"duration_seconds": 1200.0' in context
    assert '"last_unfinished_tool": "bash"' in context
    assert '"artifact_id": "artifact-1"' in context
    assert '"id": "resource-1"' in context
    assert "not current evidence handles" in context
    assert "must-not-copy" not in context
    assert "tr-other" not in context


def test_bootstrap_retry_context_is_best_effort_when_history_is_unavailable() -> None:
    class Client:
        def list_task_runs(self, project_id):
            raise requests.ConnectionError(project_id)

    assert bootstrap_retry_prompt_context(Client(), "p", "tr-current", "intent-1", 4) == ""


@pytest.mark.parametrize("task_name", ["bootstrap", "reason", "explore", "verifier"])
def test_task_consumers_preserve_startup_request_contract_failure(monkeypatch, task_name):
    from importlib import import_module
    from cairn.dispatcher.protocol.client import ApiResult

    module_name = "explore" if task_name == "verifier" else task_name
    execute_phase = "verifier_execute" if task_name == "verifier" else f"{task_name}_execute"
    module = import_module(f"cairn.dispatcher.tasks.{module_name}")
    session = PiDriver().prepare_session()
    process_calls = []
    validation = []
    releases = []

    class InvalidRequestDriver:
        @staticmethod
        def prepare_session():
            return session

        @staticmethod
        def build_execute(worker, prompt, session, **kwargs):
            raise WorkerRequestError(
                "session",
                code="session_invalid",
                message="session_invalid: synthetic startup rejection",
                phase=kwargs["phase"],
            )

    lease = SimpleNamespace(failure=None, start=lambda: None, stop=lambda: None)
    monkeypatch.setattr(module, "HeartbeatLease", SimpleNamespace(for_intent=lambda *a: lease, for_reason=lambda *a: lease))
    monkeypatch.setattr(module, "get_driver", lambda name: InvalidRequestDriver())
    monkeypatch.setattr(module, "surface_manifest_enabled", lambda project: False)
    for name in ("minimum_coverage_enabled", "blocker_cooldown_enabled"):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, lambda project: False)
    if hasattr(module, "outcome_contract_version"):
        monkeypatch.setattr(module, "outcome_contract_version", lambda project: 1)
    if hasattr(module, "bootstrap_prompt_replacements"):
        monkeypatch.setattr(module, "bootstrap_prompt_replacements", lambda *a, **k: {})
        monkeypatch.setattr(module, "bootstrap_retry_prompt_context", lambda *a, **k: "")
    if hasattr(module, "explore_prompt_replacements"):
        monkeypatch.setattr(module, "explore_prompt_replacements", lambda *a, **k: {})
    if hasattr(module, "verifier_prompt_replacements"):
        monkeypatch.setattr(module, "verifier_prompt_replacements", lambda *a, **k: {})
    if hasattr(module, "build_reason_prompt_context"):
        monkeypatch.setattr(module, "build_reason_prompt_context", lambda *a, **k: SimpleNamespace(
            replacements={}, allowed_fact_ids={"f1"}, open_intents=[], coverage_slots=[],
            binding={"max_intents": 3, "allowed_fact_ids": ["f1"], "review_input_digest": None},
        ))
    monkeypatch.setattr(module, "load_prompt", lambda *a: "fixture prompt")
    monkeypatch.setattr(module, "render_prompt", lambda *a: "fixture prompt")
    monkeypatch.setattr(module, "record_prompt_rendered", lambda *a, **k: None)
    monkeypatch.setattr(module, "record_validation_result", lambda *a, **k: validation.append(k))
    monkeypatch.setattr(module, "run_worker_process", lambda *a, **k: process_calls.append(True) or pytest.fail("request rejection cannot start a process"))
    for name in ("best_effort_release", "best_effort_release_reason", "conclude_or_release_on_failure"):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, lambda *a, **k: releases.append(True))
    config = SimpleNamespace(
        runtime=SimpleNamespace(healthcheck_timeout=3, interval=1, prompt_group="default"),
        tasks=SimpleNamespace(**{
            name: SimpleNamespace(timeout=4, conclude_timeout=2, max_intents=3)
            for name in ("bootstrap", "reason", "explore")
        }),
    )
    project = SimpleNamespace(project=SimpleNamespace(
        id="p", assessment_execution={"enabled": task_name == "verifier"}, vulnerability_fingerprint_version=None,
        assessment_protocol="", explore_deadline=None, authorization_expires_at=None,
    ), facts=[], hints=[])
    intent = SimpleNamespace(
        id="intent-1",
        assessment_role="verifier" if task_name == "verifier" else "discovery",
        slot_key=None,
        target_observation_id="observation-1" if task_name == "verifier" else None,
    )

    def update_task_run(project_id, task_run_id, **kwargs):
        assert (project_id, task_run_id) == ("p", "t")
        return ApiResult(status_code=200, data={"id": task_run_id})

    manager = SimpleNamespace(
        ensure_running=lambda *a: "fixture-container",
        runtime_metadata=lambda name: {"container_id": "fixture-container-id", "container_state": "running"},
    )
    kwargs = dict(
        config=config,
        client=SimpleNamespace(
            update_task_run=update_task_run,
            get_observation_verifier_target=lambda *a: SimpleNamespace(),
        ),
        container_manager=manager,
        project=project,
        worker=worker(),
        cancellation=TaskCancellation(),
        task_run_id="t",
    )
    if task_name != "reason":
        kwargs["intent"] = intent
    if task_name != "bootstrap":
        kwargs["export_yaml"] = "fixture"
    task_function = "explore" if task_name == "verifier" else task_name
    outcome = getattr(module, f"run_{task_function}_task")(**kwargs)
    assert isinstance(outcome, WorkerResultFailure)
    assert worker_failure_error_type(outcome) == "request_contract"
    assert outcome.code == "session_invalid"
    assert outcome.path == "session"
    assert outcome.phase == execute_phase
    assert outcome.exit_code is None
    assert process_calls == []
    assert validation[-1]["outcome"] == "request_contract"
    assert releases


@pytest.mark.parametrize("task_name", ["bootstrap", "reason", "explore"])
@pytest.mark.parametrize("failure", ["timeout", "cancelled", "exit_nonzero", "transport_error", "provider_auth", "provider_rate_limit", "session_corrupt"])
def test_tasks_never_start_conclude_or_accept_success_payload_after_process_failure(monkeypatch, task_name, failure):
    from importlib import import_module
    from cairn.dispatcher.protocol.client import ApiResult
    from cairn.dispatcher.workers.base import DriverResult

    module = import_module(f"cairn.dispatcher.tasks.{task_name}")
    session = PiDriver().prepare_session()
    calls = []

    class FakeDriver:
        def prepare_session(self):
            return session

        def build_execute(self, worker, prompt, session, **kwargs):
            calls.append({"prompt": prompt, **kwargs})
            return DriverResult(["fixture-execute"], session=session)

    lease = SimpleNamespace(failure=None, start=lambda: None, stop=lambda: None)
    monkeypatch.setattr(module, "HeartbeatLease", SimpleNamespace(for_intent=lambda *a: lease, for_reason=lambda *a: lease))
    monkeypatch.setattr(module, "get_driver", lambda name: FakeDriver())
    monkeypatch.setattr(module, "surface_manifest_enabled", lambda project: False)
    for name in ("minimum_coverage_enabled", "blocker_cooldown_enabled"):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, lambda project: False)
    if hasattr(module, "outcome_contract_version"):
        monkeypatch.setattr(module, "outcome_contract_version", lambda project: 1)
    if hasattr(module, "bootstrap_prompt_replacements"):
        monkeypatch.setattr(module, "bootstrap_prompt_replacements", lambda *a, **k: {})
        monkeypatch.setattr(
            module,
            "bootstrap_retry_prompt_context",
            lambda *a, **k: "fixture previous failure context",
        )
    if hasattr(module, "explore_prompt_replacements"):
        monkeypatch.setattr(module, "explore_prompt_replacements", lambda *a, **k: {})
    if hasattr(module, "build_reason_prompt_context"):
        monkeypatch.setattr(module, "build_reason_prompt_context", lambda *a, **k: SimpleNamespace(
            replacements={}, allowed_fact_ids={"f1"}, open_intents=[], coverage_slots=[],
            binding={"max_intents": 3, "allowed_fact_ids": ["f1"], "review_input_digest": None},
        ))
    monkeypatch.setattr(module, "load_prompt", lambda *a: "fixture prompt")
    monkeypatch.setattr(module, "render_prompt", lambda *a: "fixture prompt")
    monkeypatch.setattr(module, "record_prompt_rendered", lambda *a, **k: None)
    monkeypatch.setattr(module, "record_process_artifacts", lambda *a, **k: SimpleNamespace(refs=[]))
    data = {"fact": {"description": "fixture fact"}} if task_name != "reason" else {}
    if task_name == "bootstrap":
        data["complete"] = {"description": "fixture complete"}
    stdout = healthcheck_envelope(
        session, phase=f"{task_name}_execute",
        result={"protocol": "cairn-assessment-v2", "accepted": True, "kind": f"{task_name}_result", "data": data},
    )
    if failure in {"provider_auth", "provider_rate_limit", "session_corrupt"}:
        stdout = healthcheck_envelope(
            session, phase=f"{task_name}_execute", status="failed", result=None, submission_id=None,
            error={"code": failure, "message": "fixture private error"},
        )
    result = ProcessResult(
        0 if failure == "transport_error" else 1,
        stdout, "", timed_out=failure == "timeout", cancelled=failure == "cancelled",
        transport_error=failure == "transport_error",
    )
    monkeypatch.setattr(module, "run_worker_process", lambda *a, **k: result)
    for name in ("best_effort_release", "best_effort_release_reason", "conclude_or_release_on_failure"):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, lambda *a, **k: None)
    config = SimpleNamespace(
        runtime=SimpleNamespace(healthcheck_timeout=3, interval=1, prompt_group="default"),
        tasks=SimpleNamespace(**{
            name: SimpleNamespace(timeout=4, conclude_timeout=2, max_intents=3)
            for name in ("bootstrap", "reason", "explore")
        }),
    )
    project = SimpleNamespace(project=SimpleNamespace(id="p", assessment_execution={"enabled": False}, vulnerability_fingerprint_version=None), facts=[], hints=[])
    intent = SimpleNamespace(id="intent-1", assessment_role="discovery", slot_key=None)
    updates = []

    def runtime_metadata(container_name):
        assert container_name == "fixture-container"
        return {"container_id": "fixture-container-id", "container_state": "running"}

    def update_task_run(project_id, task_run_id, **kwargs):
        assert (project_id, task_run_id) == ("p", "t")
        updates.append(kwargs)
        return ApiResult(status_code=200, data={"id": task_run_id})

    manager = SimpleNamespace(ensure_running=lambda *a: "fixture-container", runtime_metadata=runtime_metadata)
    kwargs = dict(config=config, client=SimpleNamespace(update_task_run=update_task_run), container_manager=manager, project=project,
                  worker=worker(), cancellation=TaskCancellation(), task_run_id="t")
    if task_name != "reason":
        kwargs["intent"] = intent
    if task_name != "bootstrap":
        kwargs["export_yaml"] = "fixture"
    status = getattr(module, f"run_{task_name}_task")(**kwargs)
    assert isinstance(status, WorkerResultFailure)
    assert status.phase == f"{task_name}_execute"
    assert status.exit_code == result.returncode
    assert worker_failure_error_type(status) == {"timeout": "worker_timeout", "cancelled": "cancelled", "exit_nonzero": "worker_command_failed", "transport_error": "transport_error", "provider_auth": "provider_auth", "provider_rate_limit": "provider_rate_limit", "session_corrupt": "session_error"}[failure]
    assert len(calls) == 1
    assert calls[0]["phase"] == f"{task_name}_execute"
    assert calls[0]["project_id"] == "p"
    assert calls[0]["task_run_id"] == "t"
    assert calls[0]["resume"] is False
    if task_name in {"bootstrap", "explore"}:
        assert calls[0]["conclude_timeout_seconds"] == 2
    if task_name == "bootstrap":
        assert calls[0]["prompt"].endswith("fixture previous failure context\n")
    assert len(updates) == 1
    assert updates[0]["status"] == "running"
    assert updates[0]["session_id"] == session
    snapshot = updates[0]["runtime_context"]["execution_delivery"]
    assert snapshot["session_id"] == session
    assert snapshot["phase"] == f"{task_name}_execute"
    assert snapshot["container_id"] == "fixture-container-id"


def audit_event(**updates):
    value = {
        "protocol": "cairn-agent-audit-v1", "project_id": "p", "task_run_id": "t", "phase": "explore_execute",
        "session": "pi:session", "event": "tool_execution_start", "tool_name": "bash", "tool_call_id": "call-1",
        "timestamp": "2026-09-16T00:00:00Z", "input_bytes": 17,
    }
    value.update(updates)
    return json.dumps(value).encode() + b"\n"


def project_events(content):
    return list(hook_records._pi_tool_events(
        BytesIO(content), project_id="p", task_run_id="t", phase="explore_execute", session="pi:session", artifact_id="a1",
    ))


def test_sdk_audit_projection_is_identity_bound_bounded_and_does_not_copy_tool_bodies():
    lines = b"".join(audit_event(tool_call_id=f"c-{index}") for index in range(1005))
    events = project_events(lines)
    assert len(events) == 1005
    assert events[-1]["operation_id"] == "c-1004"
    assert events[-1]["hook_source"] == "pi"
    assert events[-1]["status"] == "started"
    assert project_events(audit_event(project_id="other")) == []
    assert project_events(audit_event(task_run_id="other")) == []
    assert project_events(audit_event(session="other")) == []
    assert project_events(audit_event(event="session_start")) == []
    assert project_events(b"broken\n" + audit_event())[0]["operation_id"] == "call-1"
    events = project_events(b"x" * 300_000 + b"\n" + audit_event(event="tool_execution_end", is_error=True, args={"secret": "must-not-copy"}))
    assert len(events) == 1
    assert events[0]["status"] == "failed"
    assert "must-not-copy" not in json.dumps(events)


def test_pi_audit_retains_full_raw_stream_before_projecting_events(tmp_path):
    content = audit_event() + b"diagnostic non-json\n" + audit_event(event="tool_execution_end", is_error=False)
    path = tmp_path / "stderr"
    path.write_bytes(content)
    result = ProcessResult(0, "", "bounded-preview", _stderr_path=str(path))
    artifacts, events = [], []

    def create_artifact(*args, **kwargs):
        artifacts.append(kwargs["content"].read())
        return SimpleNamespace(ok=True, data={"id": "a1"})

    client = SimpleNamespace(
        create_artifact=create_artifact,
        create_agent_event=lambda *args, **kwargs: events.append(kwargs["payload"]) or SimpleNamespace(ok=True),
    )
    hook_records.record_pi_tool_events(client, "p", "t", phase="explore_execute", session="pi:session", result=result)
    assert artifacts == [content]
    assert len(events) == 2
    assert path.exists()
    result.cleanup()
    assert not path.exists()


@pytest.mark.parametrize("retry_lifecycle", [False, True])
@pytest.mark.parametrize("failure", [None, "exit_nonzero", "transport_error", "timeout", "cancelled", "provider_rate_limit", "invalid_request", "startup_request"])
def test_report_executes_once_from_frozen_packet_and_never_accepts_failed_process(monkeypatch, failure, retry_lifecycle):
    from cairn.dispatcher.protocol.client import ApiResult
    from cairn.dispatcher.tasks import report
    from test_report_frozen_rendering import _data, _packet

    packet = _packet()
    artifacts, updates, calls = [], [], []

    class Client:
        def create_task_run(self, project_id, **kwargs):
            pytest.fail("报告不能重复创建 claim 已创建的 TaskRun")

        def get_assessment_report_packet(self, project_id):
            return ApiResult(status_code=200, data=packet)

        def create_artifact(self, project_id, **kwargs):
            artifacts.append(kwargs)
            digest = sha256(kwargs["content"].encode("utf-8")).hexdigest()
            return ApiResult(
                status_code=201,
                data={"id": f"a{len(artifacts)}", "sha256": digest},
            )

        def bind_assessment_report_input_snapshot(self, *args, **kwargs):
            return ApiResult(status_code=200, data={})

        def update_task_run(self, project_id, task_run_id, **kwargs):
            assert (project_id, task_run_id) == ("p1", "t")
            updates.append(kwargs)
            lifecycle = [item for item in updates if "status" not in item]
            if retry_lifecycle and "status" not in kwargs and len(lifecycle) == 1:
                return ApiResult(status_code=503, data={})
            return ApiResult(status_code=200, data={"id": task_run_id})

    def execute(manager, container, config, command, **kwargs):
        request = json.loads(command.input_files[0].content)
        calls.append(request)
        assert request["phase"] == "report_execute"
        assert request["resume"] is False
        assert request["project_id"] == "p1" and request["task_run_id"] == "t"
        raw = json.loads(healthcheck_envelope(
            command.session, phase="report_execute", project_id="p1",
            result={"protocol": "cairn-assessment-v2", "accepted": True, "kind": "report_result", "data": _data("v1", "v2")},
        ))
        if failure in {"provider_rate_limit", "invalid_request"}:
            raw.update(status="failed", result=None, submission_id=None, error={
                "code": failure, "message": "private-provider-body", "path": "binding.review_input_digest",
            })
        return ProcessResult(
            1 if failure in {"exit_nonzero", "provider_rate_limit", "invalid_request"} else 0, json.dumps(raw), "",
            timed_out=failure == "timeout", cancelled=failure == "cancelled", transport_error=failure == "transport_error",
        )

    monkeypatch.setattr(report, "run_worker_process", execute)
    if failure == "startup_request":
        class InvalidRequestDriver:
            @staticmethod
            def prepare_session():
                return PiDriver().prepare_session()

            @staticmethod
            def build_execute(worker, prompt, session, **kwargs):
                raise WorkerRequestError(
                    "session",
                    code="session_invalid",
                    message="session_invalid: synthetic startup rejection",
                    phase=kwargs["phase"],
                )

        monkeypatch.setattr(report, "get_driver", lambda name: InvalidRequestDriver())
    monkeypatch.setattr(report, "record_process_artifacts", lambda *a, **k: None)
    monkeypatch.setattr(report, "record_prompt_rendered", lambda *a, **k: None)
    monkeypatch.setattr(report, "record_validation_result", lambda *a, **k: None)
    def runtime_metadata(container_name):
        assert container_name == "fixture"
        return {"container_id": "fixture-report-container-id", "container_state": "running"}

    manager = SimpleNamespace(
        ensure_running=lambda *a: "fixture", write_text_file=lambda *a, **k: None,
        runtime_metadata=runtime_metadata,
    )
    project = SimpleNamespace(project=SimpleNamespace(id="p1", model_dump=lambda: packet["project"]))
    config = SimpleNamespace(
        dispatcher_id="synthetic-dispatcher",
        tasks=SimpleNamespace(report=SimpleNamespace(timeout=5)),
    )
    finish_state = report.ReportFinishState()
    status = report.run_report_task(
        config, Client(), manager, project, "frozen graph", worker(), prompt_group="default", task_run_id="t",
        durable_request=True, report_generation_token="fixture-generation", report_claim_token="fixture-claim",
        report_execution_plan={"packet_version": 2, "output_contract_version": 2, "completeness_rule_version": 2},
        frozen_report_prompt="Use the frozen packet {{report_packet_json}} and submit_result.",
        finish_state=finish_state,
        finish_outbox=ephemeral_report_finish_outbox(),
    )
    expected = {None: "success", "exit_nonzero": "worker_command_failed", "transport_error": "transport_error",
                "timeout": "worker_timeout", "cancelled": "cancelled", "provider_rate_limit": "provider_rate_limit",
                "invalid_request": "request_contract", "startup_request": "request_contract"}[failure]
    assert (worker_failure_error_type(status) if isinstance(status, WorkerResultFailure) else status) == expected
    terminal = [
        item
        for item in updates
        if "status" in item and item["status"] != "running"
    ]
    assert terminal == []
    assert finish_state.task_run_result is not None
    if failure is not None:
        assert isinstance(status, WorkerResultFailure)
        assert finish_state.task_run_result["worker_failure"] == status.details()
        assert finish_state.task_run_result["exit_code"] == status.exit_code
        assert "private-provider-body" not in json.dumps(updates)
        if failure == "invalid_request":
            assert status.path == "binding.review_input_digest"
        if failure == "startup_request":
            assert status.path == "session"
            assert status.phase == "report_execute"
            assert status.exit_code is None
    else:
        assert finish_state.task_run_result["status"] == "succeeded"
        assert finish_state.artifact is not None
        assert finish_state.artifact.artifact_id == "a2"
    assert finish_state.input_snapshot is not None
    assert finish_state.input_snapshot.artifact_id == "a1"
    assert len(calls) == (0 if failure == "startup_request" else 1)
    assert artifacts[0]["filename"] == "report-input-snapshot.json"
    assert json.loads(artifacts[0]["content"])["report_packet"] == packet
    reports = [artifact for artifact in artifacts if artifact["filename"] == report.REPORT_ARTIFACT_FILENAME]
    assert len(reports) == (1 if failure is None else 0)
    assert finish_state.task_run_result["status"] == (
        "succeeded"
        if failure is None
        else "timed_out"
        if failure == "timeout"
        else "cancelled"
        if failure == "cancelled"
        else "failed"
    )
    running = [item for item in updates if item.get("status") == "running"]
    assert len(running) == 2
    assert running[0]["runtime_context"]["report_inputs"]["input_snapshot_artifact_id"] == "a1"
    execution_session = running[1]["session_id"]
    assert execution_session.startswith("pi:")
    uuid.UUID(execution_session[3:])
    if failure != "startup_request":
        assert execution_session == calls[0]["session"]
    snapshot = running[1]["runtime_context"]["execution_delivery"]
    assert snapshot["session_id"] == execution_session
    assert snapshot["phase"] == "report_execute"
    assert snapshot["container_id"] == "fixture-report-container-id"
    # 此处替换了进程执行边界，finally 只能证明未启动进程，不能伪造清理确认。
    proof = {"runtime_context": {
        "execution_container_ids": [],
        "execution_lifecycle": {
            "generation": 0, "process_started": False,
            "termination_confirmed": True, "cleanup_confirmed": False,
        },
    }}
    lifecycle = [item for item in updates if "status" not in item]
    assert lifecycle == [proof] * (2 if retry_lifecycle else 1)
    assert updates == [*running, *lifecycle]


def test_scheduler_terminal_retry_preserves_structured_failure_without_running_patch():
    from concurrent.futures import Future, ThreadPoolExecutor
    from cairn.dispatcher.protocol.client import ApiResult
    from cairn.dispatcher.scheduler.task_lifecycle import TaskLifecycleMixin

    session = PiDriver().prepare_session()
    raw = healthcheck_envelope(session, status="failed", result=None, submission_id=None, error={
        "code": "invalid_request", "path": "binding.review_input_digest", "message": "private-body",
    })
    failure = process_runner.worker_process_failure(
        ProcessResult(1, raw, "private-stderr"), phase="healthcheck", project_id="p", task_run_id="t", session=session,
    )
    updates = []

    def update(*args, **kwargs):
        updates.append(kwargs)
        return ApiResult(503 if len(updates) == 1 else 200, {})

    class Scheduler(TaskLifecycleMixin):
        def _finish_runtime_context(self, *args):
            return {}

        def _clear_project_log_state(self, *args):
            pass

        def _worker_cooldown_error_type(self, error_type):
            return None

    scheduler = Scheduler()
    scheduler.client = SimpleNamespace(update_task_run=update)
    future = Future()
    future.set_result(failure)
    scheduler.futures = {future: SimpleNamespace(
        project_id="p", task_run_id="t", task_type="reason", worker_name="pi-worker",
        worker_type="pi", container_runtime=None, binding_circuit_key=None,
    )}
    with ThreadPoolExecutor(max_workers=1) as pool:
        scheduler.cleanup_executor = pool
        scheduler._reap_futures()
        assert updates == []
        assert future in scheduler.futures
        for _ in range(3):
            scheduler._flush_pending_task_run_finishes()
            for pending_future, _ in scheduler._task_run_finish_futures.values():
                pending_future.result(timeout=5)
        assert scheduler._pending_task_run_finishes() == {}
        assert scheduler.futures == {}
    assert len(updates) == 2
    assert updates[0] == updates[1]
    assert updates[1]["status"] == "failed"
    assert updates[1]["error_type"] == "request_contract"
    assert updates[1]["exit_code"] == 1
    assert "invalid_request" in updates[1]["error_message"]
    assert updates[1]["runtime_context"]["worker_failure"] == failure.details()
    assert "private-" not in json.dumps(updates)


@pytest.mark.parametrize("outcome", ["provider_auth", "provider_rate_limit", "provider_error", "session_error"])
def test_scheduler_records_sdk_failure_without_submission_retry(outcome):
    from concurrent.futures import Future
    from cairn.dispatcher.scheduler.common import WORKER_COOLDOWN_ERROR_TYPES
    from cairn.dispatcher.scheduler.task_lifecycle import TaskLifecycleMixin

    events = []

    class Scheduler(TaskLifecycleMixin):
        def _finish_task_run(self, *args, **kwargs):
            events.append(("finish", kwargs))

        def _clear_project_log_state(self, *args):
            pass

        def _mark_binding_auth_circuit(self, key):
            events.append(("binding", key))
            return 5, 1

        def _worker_cooldown_error_type(self, value):
            return value if value in WORKER_COOLDOWN_ERROR_TYPES else None

        def _mark_worker_cooldown(self, worker_name, error_type):
            events.append(("cooldown", error_type))
            return 5, 1

        def _mark_worker_rejected(self, *args):
            pytest.fail("SDK provider failure must not enter target rejection retry")

    binding = SimpleNamespace(model_id="m", credential_version_id="cv")
    task = SimpleNamespace(project_id="p", task_run_id="t", task_type="explore", worker_name="pi-worker",
                           worker_type="pi", container_runtime=None, binding_circuit_key=binding)
    future = Future()
    future.set_result(outcome)
    scheduler = Scheduler()
    scheduler.futures = {future: task}
    scheduler._reap_futures()
    assert scheduler.futures == {future: task}
    assert events[0][0] == "finish" and events[0][1]["error_type"] == outcome
    assert ("binding", binding) in events if outcome == "provider_auth" else all(event[0] != "binding" for event in events)
    assert ("cooldown", outcome) in events if outcome in {"provider_rate_limit", "provider_error"} else all(event[0] != "cooldown" for event in events)
