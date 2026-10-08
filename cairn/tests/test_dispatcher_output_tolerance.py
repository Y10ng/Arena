from __future__ import annotations

from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from cairn.dispatcher.contracts import (
    DeclaredArtifact,
    extract_coverage_scope_disposition,
    extract_discovered_surfaces,
    extract_evidence_refs,
    validate_bootstrap_conclude_payload,
    validate_bootstrap_execute_payload,
    validate_explore_payload,
    validate_reason_payload,
    validate_verifier_payload,
)
from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks import artifact_records, bootstrap_results, conclude_writer
from cairn.dispatcher.tasks.artifact_records import (
    UnresolvedArtifactEvidenceError,
    record_declared_artifacts,
    record_process_artifacts,
    resolve_declared_artifact_evidence_refs,
)
from cairn.dispatcher.tasks.command_events import record_command_finished
from cairn.dispatcher.tasks.common_base import ConcludeWriteResult, ProcessArtifactRecord
from cairn.dispatcher.tasks.conclude_writer import (
    build_coverage_scope_disposition_for_conclude,
    build_discovered_surfaces_for_conclude,
    build_verification_result_for_conclude,
    conclude_or_release_on_failure,
)
from cairn.dispatcher.tasks.reason_context import build_reason_prompt_context
from cairn.dispatcher.worker_result_gateway import decode_pi_result
from cairn.server.models import EvidenceRef


class FakeClient:
    def __init__(self) -> None:
        self.artifacts: list[dict[str, object]] = []
        self.events: list[dict[str, object]] = []

    def create_artifact(self, project_id: str, **kwargs) -> ApiResult:
        artifact_id = f"a{len(self.artifacts) + 1:03d}"
        self.artifacts.append({"project_id": project_id, **kwargs})
        return ApiResult(status_code=200, data={"id": artifact_id})

    def create_agent_event(self, project_id: str, **kwargs) -> ApiResult:
        self.events.append({"project_id": project_id, **kwargs})
        return ApiResult(status_code=200, data={"id": f"ev{len(self.events) + 1:03d}"})


class MissingFileContainer:
    def read_file(self, container_name: str, path: str) -> str:
        raise FileNotFoundError(path)


class ReadableFileContainer:
    def read_file(self, container_name: str, path: str) -> str:
        return f"evidence from {path}"


class FailureCleanupClient:
    def __init__(self) -> None:
        self.releases: list[tuple[str, str, str]] = []

    def release(self, project_id: str, intent_id: str, worker_name: str, *, task_run_id: str | None = None) -> ApiResult:
        assert task_run_id == "tr001"
        self.releases.append((project_id, intent_id, worker_name))
        return ApiResult(status_code=200)

    def list_coverage_slots(self, project_id: str) -> ApiResult:
        raise AssertionError("runtime failure must not synthesize a target blocker")

    def conclude_with_evidence(self, *args, **kwargs) -> ApiResult:
        raise AssertionError("runtime failure must not conclude structured coverage")


def test_structured_runtime_failure_releases_without_target_blocker() -> None:
    client = FailureCleanupClient()

    conclude_or_release_on_failure(
        client,
        "proj_001",
        "i001",
        "worker-1",
        "worker timeout",
        task_run_id="tr001",
        enabled=True,
        structured=True,
        slot_key="cell:001",
    )

    assert client.releases == [("proj_001", "i001", "worker-1")]


def test_pointer_only_evidence_refs_are_rejected() -> None:
    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "bootstrap_conclude_result",
        "data": {
            "fact": {
                "description": "确认 SpEL RCE 与 Runtime RCE，但尚未完成内存马。",
                "evidence_refs": [
                    {"pointer": "SpEL RCE 测试: 1+1=2"},
                    {"artifact_id": "a059", "pointer": "stdout"},
                ],
            }
        },
    }

    with pytest.raises(ValueError, match="requires artifact_handle or artifact_id"):
        validate_bootstrap_conclude_payload(payload)


def test_artifact_handle_evidence_refs_are_extracted_without_aliases() -> None:
    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "bootstrap_conclude_result",
        "data": {
            "fact": {
                "description": "确认目标事实。",
                "evidence_refs": [
                    {"artifact_handle": "report", "pointer": "line 4"},
                ],
            }
        },
    }

    refs = extract_evidence_refs(payload, section="fact")

    assert [(ref.artifact_id, ref.pointer) for ref in refs] == [
        ("report", "line 4"),
    ]


def test_missing_declared_artifact_is_collect_failure_not_write_failure() -> None:
    record = record_declared_artifacts(
        FakeClient(),
        MissingFileContainer(),
        "worker-container",
        "proj_015",
        "tr027",
        phase="bootstrap_conclude",
        artifacts=[DeclaredArtifact(handle="target-source", kind="file", path="/tmp/target-only.java")],
    )

    assert record.refs == []
    assert record.write_failed is False
    assert record.collect_failed is True


def test_successful_declared_artifact_records_ordered_aliases() -> None:
    record = record_declared_artifacts(
        FakeClient(),
        ReadableFileContainer(),
        "worker-container",
        "proj_015",
        "tr027",
        phase="explore",
        artifacts=[
            DeclaredArtifact(
                handle="packet",
                kind="report",
                path="/workspace/evidence/packet.txt",
            )
        ],
    )

    assert record.write_failed is False
    assert record.collect_failed is False
    assert record.refs == [
        EvidenceRef(
            artifact_id="a001",
            pointer="packet",
        )
    ]
    assert record.artifact_ids_by_handle == {"packet": "a001"}


def _successful_process_stdout(description: str, *, task_run_id: str = "tr027") -> str:
    return json.dumps({
        "protocol": "cairn-agent-result-v1", "engine": "pi",
        "project_id": "proj_015", "task_run_id": task_run_id, "phase": "explore_execute",
        "session": "pi:07b53667-a6ab-44be-badb-e3ed6b347331",
        "status": "succeeded", "submission_id": "a" * 64, "usage": None, "error": None,
        "result": {"protocol": "cairn-assessment-v2", "accepted": True, "kind": "explore_result",
                   "data": {"fact": {"description": description}}},
    })


def test_process_artifact_reads_complete_file_backed_output(tmp_path) -> None:
    output_path = tmp_path / "stdout.log"
    full_output = _successful_process_stdout("head\n" + ("x" * 80_000) + "\ntail\n")
    output_path.write_text(full_output, encoding="utf-8")
    result = ProcessResult(
        returncode=0,
        stdout="bounded preview",
        stderr="",
        _stdout_path=str(output_path),
    )
    client = FakeClient()

    try:
        record = record_process_artifacts(
            client,
            "proj_015",
            "tr027",
            phase="explore_execute",
            result=result,
        )
    finally:
        result.cleanup()

    assert record.write_failed is False
    assert client.artifacts[0]["content"] == full_output
    assert client.artifacts[0]["metadata"]["original_size"] == len(full_output.encode())
    assert client.artifacts[0]["metadata"]["truncated"] is False


def test_process_artifact_uses_true_size_and_head_tail_for_large_file(tmp_path) -> None:
    # stdout 有独立的协议大小上限；大文件审计使用合法成功执行的 stderr。
    output_path = tmp_path / "stderr.log"
    raw = b"A" * 1_100_000 + b"MIDDLE" + b"Z" * 1_100_000
    output_path.write_bytes(raw)
    result = ProcessResult(
        returncode=0,
        stdout=_successful_process_stdout("completed bounded testing"),
        stderr="bounded preview",
        _stderr_path=str(output_path),
    )
    client = FakeClient()

    try:
        record_process_artifacts(
            client,
            "proj_015",
            "tr027",
            phase="explore_execute",
            result=result,
        )
    finally:
        result.cleanup()

    artifact = next(item for item in client.artifacts if item["metadata"]["stream"] == "stderr")
    assert artifact["metadata"]["original_size"] == len(raw)
    assert artifact["metadata"]["truncated"] is True
    assert artifact["content"].startswith("A" * 1_000_000)
    assert artifact["content"].endswith("Z" * 1_000_000)
    assert "MIDDLE" not in artifact["content"]


def test_unresolved_artifact_handle_is_not_forwarded_as_artifact_id() -> None:
    with pytest.raises(UnresolvedArtifactEvidenceError, match="missing-evidence"):
        resolve_declared_artifact_evidence_refs(
            [EvidenceRef(artifact_id="missing-evidence", pointer="response")],
            ProcessArtifactRecord(refs=[], collect_failed=True),
        )


def test_artifact_resolution_uses_exact_handle_only() -> None:
    declared = ProcessArtifactRecord(
        refs=[
            EvidenceRef(artifact_id="a001"),
            EvidenceRef(artifact_id="a002"),
        ],
        artifact_ids_by_handle={
            "request-packet": "a001",
            "response-packet": "a002",
        },
    )

    refs = resolve_declared_artifact_evidence_refs(
        [EvidenceRef(artifact_id="response-packet", pointer="response")],
        declared,
    )

    assert refs == [EvidenceRef(artifact_id="a002", pointer="response")]


def test_default_copy_explore_requires_coverage_result() -> None:
    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "explore_conclude_result",
        "data": {"fact": {"description": "已完成当前 Cell 测试。"}},
    }

    assert validate_explore_payload(payload) == ("fact", "已完成当前 Cell 测试。")
    with pytest.raises(ValueError, match="coverage_result is required"):
        validate_explore_payload(payload, require_coverage_result=True)


def test_default_copy_explore_accepts_advertised_canonical_contract() -> None:
    output = """{
      "protocol": "cairn-assessment-v2",
      "accepted": true,
      "kind": "explore_result",
      "data": {
        "fact": {"description": "已完成当前 Cell 的基线与变体测试。"},
        "coverage_result": {
          "slot_key": "cell:configuration",
          "surface_key": "GET https://example.test/config",
          "test_family": "configuration",
          "outcome": "tested_no_finding",
          "baseline": "记录默认响应",
          "variants": ["比较未认证请求"],
          "evidence_refs": [
            {
              "artifact_id": "/container/path/evidence.md",
              "pointer": "request-response"
            }
          ]
        },
        "artifacts": [
          {"handle": "evidence", "kind": "report", "path": "/container/path/evidence.md"}
        ],
        "discovered_surfaces": [
          {
            "surface_kind": "endpoint",
            "surface_key": "GET https://example.test/status",
            "traits": {"authentication": "not_required"},
            "applicable_tests": [
              {
                "test_family": "configuration",
                "applicability_basis": "状态端点暴露运行配置",
                "completion_criteria": "比较默认与异常请求的响应"
              }
            ],
            "evidence_refs": [
              {
                "artifact_id": "/container/path/evidence.md",
                "pointer": "adjacent-surface"
              }
            ]
          }
        ]
      }
    }"""

    payload = json.loads(output)

    assert validate_explore_payload(
        payload,
        require_coverage_result=True,
        surface_manifest=True,
    ) == ("fact", "已完成当前 Cell 的基线与变体测试。")


def _structured_explore_payload(
    *,
    kind: str = "explore_result",
    outcome: str = "tested_no_finding",
) -> dict[str, object]:
    coverage_result: dict[str, object] = {
        "slot_key": "cell:authorization",
        "surface_key": "GET https://example.test/orders/1",
        "test_family": "authorization",
        "outcome": outcome,
    }
    if outcome in {"tested_no_finding", "finding"}:
        coverage_result.update(
            {
                "baseline": "owner request returned the owner object",
                "variants": ["other object id was tested"],
                "evidence_refs": [
                    {
                        "artifact_handle": "evidence",
                        "pointer": "request-response",
                    }
                ],
            }
        )
    if outcome == "temporarily_blocked":
        coverage_result.update(
            {
                "blocker_code": "missing_second_principal",
                "strategy_key": "auth.owner-vs-other-v1",
                "limitation_reason": "a second principal is unavailable",
                "limitation_details": "only the owner session is available",
                "attempted_strategies": ["obtain another approved account"],
            }
        )
    fact: dict[str, object] = {"description": "completed bounded testing"}
    if outcome == "finding":
        fact["vulnerability"] = {
            "title": "object authorization bypass",
            "severity": "high",
            "vuln_type": "IDOR",
            "target": "GET /orders/1",
            "confidence": 0.9,
            "repro_request": "GET /orders/2 HTTP/1.1",
            "repro_response": "HTTP/1.1 200 OK",
            "identity": {
                "vulnerability_class": "IDOR",
                "asset": {"protocol": "https", "host": "example.test", "port": 443},
                "entrypoint": {"method": "GET", "route": "/orders/1"},
                "vector": {"parameter": "order_id", "variant": "horizontal"},
            },
        }
    return {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": kind,
        "data": {
            "fact": fact,
            "coverage_result": coverage_result,
            "artifacts": [
                {"handle": "evidence", "kind": "raw_output", "path": "/workspace/evidence.txt"}
            ],
        },
    }


@pytest.mark.parametrize(
    "outcome",
    ("tested_no_finding", "finding", "temporarily_blocked"),
)
def test_structured_explore_accepts_canonical_outcomes(outcome: str) -> None:
    payload = _structured_explore_payload(outcome=outcome)

    assert validate_explore_payload(
        payload,
        require_coverage_result=True,
        strict_contract=True,
        expected_kind="explore_result",
        outcome_contract_version=2,
        blocker_cooldown=True,
    ) == ("fact", "completed bounded testing")


def test_identity_only_contract_requires_identity_without_coverage_fields() -> None:
    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "explore_result",
        "data": {
            "fact": {
                "description": "confirmed finding",
                "vulnerability": {
                    "title": "IDOR",
                    "severity": "high",
                    "confidence": 0.8,
                },
            }
        },
    }

    with pytest.raises(ValueError, match="vulnerability identity is required"):
        validate_explore_payload(
            payload,
            require_vulnerability_identity=True,
            expected_kind="explore_result",
        )

    payload["data"]["fact"]["vulnerability"]["identity"] = {
        "vulnerability_class": "IDOR",
        "asset": {"protocol": "https", "host": "example.test", "port": 443},
        "entrypoint": {"method": "GET", "route": "/orders/{order_id}"},
        "vector": {"parameter": "order_id", "variant": "horizontal"},
    }
    assert validate_explore_payload(
        payload,
        require_vulnerability_identity=True,
        expected_kind="explore_result",
    ) == ("fact", "confirmed finding")


def test_structured_v1_finding_keeps_legacy_evidence_requirements() -> None:
    payload = _structured_explore_payload(outcome="finding")
    coverage_result = payload["data"]["coverage_result"]
    coverage_result.pop("baseline")
    coverage_result.pop("variants")
    coverage_result.pop("evidence_refs")

    assert validate_explore_payload(
        payload,
        require_coverage_result=True,
        strict_contract=True,
        expected_kind="explore_result",
        outcome_contract_version=1,
    ) == ("fact", "completed bounded testing")


def test_structured_v1_blocked_requires_only_canonical_reason() -> None:
    payload = _structured_explore_payload(outcome="temporarily_blocked")
    coverage_result = payload["data"]["coverage_result"]
    for key in (
        "blocker_code",
        "strategy_key",
        "limitation_details",
        "attempted_strategies",
    ):
        coverage_result.pop(key)

    assert validate_explore_payload(
        payload,
        require_coverage_result=True,
        strict_contract=True,
        expected_kind="explore_result",
        outcome_contract_version=1,
        blocker_cooldown=False,
    ) == ("fact", "completed bounded testing")


@pytest.mark.parametrize("kind", ("explore_result", "explore_conclude_result"))
def test_structured_explore_rejects_nested_fields_without_repair(kind: str) -> None:
    payload = _structured_explore_payload(kind=kind)
    fact = payload["data"]["fact"]
    fact["coverage_result"] = payload["data"].pop("coverage_result")
    fact["artifacts"] = payload["data"].pop("artifacts")
    original = json.dumps(payload, sort_keys=True)
    with pytest.raises(ValueError):
        validate_explore_payload(
            payload, require_coverage_result=True, strict_contract=True, expected_kind=kind,
        )
    assert json.dumps(payload, sort_keys=True) == original
    assert "coverage_result" in fact
    assert "artifacts" in fact


@pytest.mark.parametrize("field", ("coverage_result", "artifacts"))
@pytest.mark.parametrize("phase", ("explore_execute", "explore_conclude"))
def test_structured_explore_rejects_ambiguous_nested_field(field: str, phase: str) -> None:
    kind = "explore_result" if phase == "explore_execute" else "explore_conclude_result"
    payload = _structured_explore_payload(kind=kind)
    session = "pi:07b53667-a6ab-44be-badb-e3ed6b347331"
    envelope = {
        "protocol": "cairn-agent-result-v1", "engine": "pi", "project_id": "p1",
        "task_run_id": "tr1", "phase": phase, "session": session,
        "status": "succeeded", "submission_id": "a" * 64, "usage": {}, "error": None,
        "result": payload,
    }

    def decode():
        return decode_pi_result(
            json.dumps(envelope), phase=phase, project_id="p1", task_run_id="tr1",
            session=session, validator=lambda value: validate_explore_payload(
                value, require_coverage_result=True, strict_contract=True,
                expected_kind=kind,
            ),
        )

    # 结构校验现由 pi Gateway 统一执行；先确认有效基线，避免其他字段掩盖负例。
    assert decode().failure is None
    payload["data"]["fact"][field] = payload["data"][field]
    original = json.dumps(payload, sort_keys=True)
    outcome = decode()

    assert outcome.failure is not None
    assert outcome.failure.failure_class == "result_schema"
    assert outcome.failure.code == "phase_shape_invalid"
    assert outcome.failure.phase == phase
    assert outcome.failure.path is None
    assert outcome.failure.retryable is False
    assert outcome.failure.message == "Worker 结果不符合阶段数据结构：phase_shape_invalid"
    assert "example.test" not in json.dumps(outcome.failure.details())
    assert json.dumps(payload, sort_keys=True) == original




@pytest.mark.parametrize(
    "record",
    [
        {
            "hypothesis": "The target may be temporarily inaccessible",
            "outcome": "temporarily_blocked",
            "blocker_code": "target_access_denied",
            "limitation_reason": "Target denied the authorized request",
            "limitation_details": "The same denial was observed across retries",
            "attempted_strategies": ["Retry with the authorized target configuration"],
        },
        {
            "hypothesis": "The response may expose a new surface",
            "outcome": "observation",
            "observations": ["The response referenced an undocumented route"],
        },
    ],
)
def test_goal_driven_explore_requires_evidence_for_non_test_outcomes(record) -> None:
    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "explore_result",
        "data": {
            "fact": {"description": "Recorded goal-driven result"},
            "assessment_record": record,
        },
    }

    with pytest.raises(ValueError, match="assessment_record.evidence_refs is required"):
        validate_explore_payload(
            payload,
            strict_contract=True,
            expected_kind="explore_result",
            goal_driven=True,
        )


def test_structured_explore_keeps_semantic_evidence_validation() -> None:
    payload = _structured_explore_payload()
    del payload["data"]["coverage_result"]["evidence_refs"]
    with pytest.raises(ValueError, match="tested_no_finding requires evidence_refs"):
        validate_explore_payload(
            payload, require_coverage_result=True, strict_contract=True,
            expected_kind="explore_result", outcome_contract_version=2,
        )




def test_command_finished_event_preserves_positive_oom_evidence() -> None:
    from cairn.server.services.event_payloads import command_event_to_model

    client = FakeClient()
    result = ProcessResult(
        returncode=137,
        stdout="",
        stderr="",
        oom_killed=True,
        oom_evidence_source="cgroup_v2_memory_events",
    )

    record_command_finished(
        client,
        "p1",
        "tr1",
        phase="bootstrap_execute",
        worker=SimpleNamespace(name="worker-1", type="pi"),
        argv=["cairn-agent", "--request", "/tmp/cairn-prompts/fixture.json"],
        result=result,
        duration_seconds=1.5,
    )

    assert client.events[0]["kind"] == "command_finished"
    payload = client.events[0]["payload"]
    assert payload["status"] == "failed"
    assert payload["timed_out"] is False
    assert payload["oom_killed"] is True
    assert payload["oom_evidence_source"] == "cgroup_v2_memory_events"

    model = command_event_to_model(
        {
            "id": "ev1",
            "task_run_id": "tr1",
            "project_id": "p1",
            "ts": "2026-08-19T00:00:00Z",
            "kind": "command_finished",
            "payload_json": json.dumps(payload),
        }
    )
    assert model.oom_killed is True
    assert model.oom_evidence_source == "cgroup_v2_memory_events"
    assert model.timed_out is False


def test_structured_explore_rejects_top_level_artifacts() -> None:
    payload = _structured_explore_payload()
    payload["artifacts"] = payload["data"]["artifacts"]
    del payload["data"]["artifacts"]

    with pytest.raises(ValueError, match="artifacts must be nested under data"):
        validate_explore_payload(
            payload,
            require_coverage_result=True,
            strict_contract=True,
            expected_kind="explore_result",
        )


def test_structured_explore_rejects_wrong_phase_kind() -> None:
    payload = _structured_explore_payload(kind="explore_conclude_result")

    with pytest.raises(ValueError, match="unexpected result kind"):
        validate_explore_payload(
            payload,
            require_coverage_result=True,
            strict_contract=True,
            expected_kind="explore_result",
        )


@pytest.mark.parametrize("alias", ("limitations", "limitation_detail"))
def test_structured_blocked_rejects_limitation_aliases(alias: str) -> None:
    payload = _structured_explore_payload(outcome="temporarily_blocked")
    coverage_result = payload["data"]["coverage_result"]
    coverage_result[alias] = "unsupported alias"

    with pytest.raises(ValueError, match="coverage_result has unexpected keys"):
        validate_explore_payload(
            payload,
            require_coverage_result=True,
            strict_contract=True,
            expected_kind="explore_result",
        )


def test_structured_blocked_requires_canonical_limitation_fields() -> None:
    payload = _structured_explore_payload(outcome="temporarily_blocked")
    del payload["data"]["coverage_result"]["limitation_reason"]

    with pytest.raises(
        ValueError,
        match="temporarily_blocked requires limitation_reason",
    ):
        validate_explore_payload(
            payload,
            require_coverage_result=True,
            strict_contract=True,
            expected_kind="explore_result",
        )


@pytest.mark.parametrize("outcome", ("tested_no_finding", "temporarily_blocked"))
def test_structured_non_finding_rejects_vulnerability(outcome: str) -> None:
    payload = _structured_explore_payload(outcome="finding")
    payload["data"]["coverage_result"] = _structured_explore_payload(
        outcome=outcome
    )["data"]["coverage_result"]

    with pytest.raises(
        ValueError,
        match="non-finding coverage outcome must not include vulnerability",
    ):
        validate_explore_payload(
            payload,
            require_coverage_result=True,
            strict_contract=True,
            expected_kind="explore_result",
        )


def test_structured_finding_requires_vulnerability() -> None:
    payload = _structured_explore_payload(outcome="finding")
    del payload["data"]["fact"]["vulnerability"]

    with pytest.raises(ValueError, match="finding requires vulnerability"):
        validate_explore_payload(
            payload,
            require_coverage_result=True,
            strict_contract=True,
            expected_kind="explore_result",
        )


def test_verifier_payload_requires_exact_target_and_evidence() -> None:
    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "verifier_result",
        "data": {
            "description": "独立会话复现了越权读取。",
            "verification_result": {
                "observation_id": "vo1",
                "outcome": "confirmed",
                "evidence_refs": [
                    {"artifact_id": "/workspace/verifier-packet.txt", "pointer": "response"}
                ],
                "notes": "使用第二个账号复现",
            },
            "artifacts": [
                {"handle": "evidence", "kind": "file", "path": "/workspace/verifier-packet.txt"}
            ],
        },
    }

    assert validate_verifier_payload(payload, target_observation_id="vo1") == (
        "fact",
        "独立会话复现了越权读取。",
    )
    with pytest.raises(ValueError, match="does not match"):
        validate_verifier_payload(payload, target_observation_id="vo2")

    payload["data"]["verification_result"]["evidence_refs"] = []
    with pytest.raises(ValueError, match="evidence_refs is required"):
        validate_verifier_payload(payload, target_observation_id="vo1")


def test_verifier_result_resolves_exact_artifact_handles() -> None:
    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "verifier_conclude_result",
        "data": {
            "description": "复现完成。",
            "verification_result": {
                "observation_id": "vo1",
                "outcome": "confirmed",
                "evidence_refs": [
                    {"artifact_handle": "verifier"},
                    {"artifact_handle": "response", "pointer": "response"},
                ],
            },
        },
    }
    declared = ProcessArtifactRecord(
        refs=[
            EvidenceRef(artifact_id="a-verifier"),
            EvidenceRef(artifact_id="a-response"),
        ],
        artifact_ids_by_handle={
            "verifier": "a-verifier",
            "response": "a-response",
        },
    )

    assert validate_verifier_payload(payload, target_observation_id="vo1") == (
        "fact",
        "复现完成。",
    )
    result, refs = build_verification_result_for_conclude(payload, declared)

    assert result == {
        "observation_id": "vo1",
        "outcome": "confirmed",
        "evidence_refs": [
            {"artifact_id": "a-verifier", "pointer": ""},
            {"artifact_id": "a-response", "pointer": "response"},
        ],
        "notes": "",
    }
    assert refs == [
        EvidenceRef(artifact_id="a-verifier"),
        EvidenceRef(artifact_id="a-response", pointer="response"),
    ]


def test_coverage_result_requires_resolved_current_run_evidence() -> None:
    payload = _structured_explore_payload()
    payload["data"]["coverage_result"]["evidence_refs"] = [
        {"artifact_id": "a999", "pointer": "request-response"}
    ]
    declared = ProcessArtifactRecord(refs=[EvidenceRef(artifact_id="a001")])

    with pytest.raises(conclude_writer.CoverageEvidenceInsufficientError):
        conclude_writer.build_coverage_result_for_conclude(
            payload,
            declared,
            require_current_run_evidence=True,
        )


def test_coverage_result_accepts_current_run_artifact_id() -> None:
    payload = _structured_explore_payload()
    payload["data"]["coverage_result"]["evidence_refs"] = [
        {"artifact_handle": "evidence", "pointer": "request-response"}
    ]
    declared = ProcessArtifactRecord(
        refs=[EvidenceRef(artifact_id="a001")],
        artifact_ids_by_handle={"evidence": "a001"},
    )

    result, refs = conclude_writer.build_coverage_result_for_conclude(
        payload,
        declared,
        require_current_run_evidence=True,
    )

    assert result is not None
    assert result["evidence_refs"] == [
        {"artifact_id": "a001", "pointer": "request-response", "verifier_run_id": None}
    ]
    assert refs == [EvidenceRef(artifact_id="a001", pointer="request-response")]


def test_v1_coverage_result_preserves_prior_project_artifact_reference() -> None:
    payload = _structured_explore_payload(outcome="finding")
    payload["data"]["coverage_result"]["evidence_refs"] = [
        {"artifact_id": "a999", "pointer": "request-response"}
    ]
    declared = ProcessArtifactRecord(refs=[EvidenceRef(artifact_id="a001")])

    result, refs = conclude_writer.build_coverage_result_for_conclude(
        payload,
        declared,
        require_current_run_evidence=False,
    )

    assert result is not None
    assert refs == [EvidenceRef(artifact_id="a999", pointer="request-response")]
    assert result["evidence_refs"][0]["artifact_id"] == "a999"


def test_finding_requires_resolved_packet_or_complete_http_reproduction() -> None:
    payload = _structured_explore_payload(outcome="finding")
    vulnerability = payload["data"]["fact"]["vulnerability"]
    vulnerability["packet_artifact_handle"] = "missing-packet"
    vulnerability["repro_response"] = ""
    declared = ProcessArtifactRecord(refs=[EvidenceRef(artifact_id="a001")])

    with pytest.raises(UnresolvedArtifactEvidenceError, match="missing-packet"):
        conclude_writer.build_vulnerability_for_conclude(
            payload,
            declared,
            slot_key="cell:authorization",
            require_current_run_reproduction=True,
        )


def test_finding_rejects_explicit_unresolved_packet_handle() -> None:
    payload = _structured_explore_payload(outcome="finding")
    vulnerability = payload["data"]["fact"]["vulnerability"]
    vulnerability["packet_artifact_handle"] = "missing-packet"
    declared = ProcessArtifactRecord(refs=[EvidenceRef(artifact_id="a001")])

    with pytest.raises(
        UnresolvedArtifactEvidenceError,
        match="missing-packet",
    ):
        conclude_writer.build_vulnerability_for_conclude(
            payload,
            declared,
            slot_key="cell:authorization",
            require_current_run_reproduction=False,
        )


def test_finding_accepts_resolved_packet_artifact() -> None:
    payload = _structured_explore_payload(outcome="finding")
    vulnerability = payload["data"]["fact"]["vulnerability"]
    vulnerability["packet_artifact_handle"] = "finding-packet"
    vulnerability["repro_request"] = ""
    vulnerability["repro_response"] = ""
    declared = ProcessArtifactRecord(
        refs=[EvidenceRef(artifact_id="a001")],
        artifact_ids_by_handle={"finding-packet": "a001"},
    )

    result = conclude_writer.build_vulnerability_for_conclude(
        payload,
        declared,
        slot_key="cell:authorization",
        require_current_run_reproduction=True,
    )

    assert result is not None
    assert result["packet_artifact_id"] == "a001"


def test_manifest_surface_contract_normalizes_applicable_tests() -> None:
    surface = {
        "surface_kind": "endpoint",
        "surface_key": "GET HTTPS://Example.COM:443/account",
        "description": "账户入口",
        "traits": {"auth": "cookie"},
        "applicable_tests": [
            {
                "test_family": "Business Logic",
                "applicability_basis": "账户操作包含状态约束",
                "completion_criteria": "比较合法与越序操作",
            }
        ],
        "evidence_refs": [{"artifact_id": "a001", "pointer": "request"}],
    }
    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "bootstrap_result",
        "data": {
            "fact": {"description": "确认账户入口"},
            "complete": {"description": "完成初始 Surface 枚举"},
            "discovered_surfaces": [surface],
        },
    }

    kind, result = validate_bootstrap_execute_payload(payload)
    assert kind == "complete"
    assert result is not None
    discovered = extract_discovered_surfaces(payload)
    assert discovered[0]["applicable_tests"][0]["test_family"] == "business_logic"
    assert discovered[0]["evidence_refs"] == [
        {"artifact_id": "a001", "pointer": "request"}
    ]


@pytest.mark.parametrize(
    ("kind", "validator"),
    (
        ("bootstrap_result", validate_bootstrap_execute_payload),
        ("bootstrap_conclude_result", validate_bootstrap_conclude_payload),
    ),
)
def test_bootstrap_normalizes_scalar_surface_traits(kind, validator) -> None:
    data = {
        "fact": {"description": "confirmed endpoint"},
        "discovered_surfaces": [
            {
                "surface_kind": "endpoint",
                "surface_key": "GET https://example.test/admin",
                "traits": {
                    " auth_required ": True,
                    "enabled": False,
                    "port": 443,
                    "confidence": 0.75,
                    "role": " administrator ",
                },
            }
        ],
    }
    if kind == "bootstrap_result":
        data["complete"] = {"description": "initial enumeration complete"}
    payload = {"protocol":"cairn-assessment-v2","accepted": True, "kind": kind, "data": data}

    validator(payload, require_surface_resolution=True)

    assert extract_discovered_surfaces(payload)[0]["traits"] == {
        "auth_required": "true",
        "enabled": "false",
        "port": "443",
        "confidence": "0.75",
        "role": "administrator",
    }


@pytest.mark.parametrize("value", (None, [], {}, float("nan"), float("inf"), float("-inf")))
def test_bootstrap_rejects_non_scalar_surface_traits(value) -> None:
    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "bootstrap_result",
        "data": {
            "fact": {"description": "confirmed endpoint"},
            "complete": {"description": "initial enumeration complete"},
            "discovered_surfaces": [
                {
                    "surface_kind": "endpoint",
                    "surface_key": "GET https://example.test/admin",
                    "traits": {"invalid": value},
                }
            ],
        },
    }

    with pytest.raises(ValueError, match="traits must contain scalar values"):
        validate_bootstrap_execute_payload(payload, require_surface_resolution=True)


def test_bootstrap_rejects_artifact_without_canonical_kind() -> None:
    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "bootstrap_result",
        "data": {
            "fact": {"description": "confirmed host"},
            "complete": {"description": "request bounded completion"},
            "artifacts": [
                {
                    "id": "recon_notes",
                    "path": "/workspace/recon.md",
                    "summary": "recon notes",
                }
            ],
            "discovered_surfaces": [
                {
                    "surface_kind": "host",
                    "surface_key": "example.test",
                }
            ],
        },
    }

    with pytest.raises(ValueError, match="unexpected artifact fields at index 0"):
        validate_bootstrap_execute_payload(payload)

    payload["data"]["artifacts"] = [
        {"handle": "evidence", "kind": "report", "path": "/workspace/recon.md"}
    ]
    kind, result = validate_bootstrap_execute_payload(payload)
    assert kind == "complete"
    assert result is not None


def test_bootstrap_rejects_noncanonical_surface_shape() -> None:
    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "bootstrap_result",
        "data": {
            "fact": {"description": "confirmed host"},
            "complete": {"description": "request bounded completion"},
            "discovered_surfaces": [
                {
                    "id": "s-host-01",
                    "kind": "host",
                    "value": "example.test",
                    "observed_traits": {"reachable": "true"},
                    "applicable_tests": ["configuration"],
                }
            ],
        },
    }

    with pytest.raises(
        ValueError,
        match=r"discovered_surfaces\[0\] has unexpected keys",
    ):
        validate_bootstrap_execute_payload(payload)


def test_bootstrap_rejects_string_applicable_tests() -> None:
    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "bootstrap_result",
        "data": {
            "fact": {"description": "confirmed host"},
            "complete": {"description": "request bounded completion"},
            "discovered_surfaces": [
                {
                    "surface_kind": "host",
                    "surface_key": "example.test",
                    "applicable_tests": ["configuration"],
                }
            ],
        },
    }

    with pytest.raises(
        ValueError,
        match=(
            r"discovered_surfaces\[0\]\.applicable_tests\[0\] "
            r"must be an object"
        ),
    ):
        validate_bootstrap_execute_payload(payload)


def test_discovered_surface_evidence_aliases_resolve_before_conclude() -> None:
    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "explore_result",
        "data": {
            "fact": {"description": "observed adjacent endpoint"},
            "discovered_surfaces": [
                {
                    "surface_kind": "endpoint",
                    "surface_key": "GET https://example.com/account",
                    "evidence_refs": [
                        {"artifact_handle": "account-packet", "pointer": "request"}
                    ],
                }
            ],
        },
    }
    declared = ProcessArtifactRecord(
        refs=[EvidenceRef(artifact_id="a059")],
        artifact_ids_by_handle={"account-packet": "a059"},
    )

    surfaces = build_discovered_surfaces_for_conclude(payload, declared)

    assert surfaces[0]["evidence_refs"] == [
        {"artifact_id": "a059", "pointer": "request"}
    ]


def test_unresolved_explicit_surface_evidence_fails_before_server_write() -> None:
    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "explore_result",
        "data": {
            "fact": {"description": "observed adjacent endpoint"},
            "discovered_surfaces": [
                {
                    "surface_kind": "endpoint",
                    "surface_key": "GET https://example.com/account",
                    "evidence_refs": [
                        {"artifact_handle": "account", "pointer": "request"},
                        {"artifact_handle": "missing", "pointer": "response"},
                    ],
                }
            ],
        },
    }
    declared = ProcessArtifactRecord(
        refs=[EvidenceRef(artifact_id="a059")],
        artifact_ids_by_handle={"account": "a059"},
    )

    with pytest.raises(UnresolvedArtifactEvidenceError, match="missing"):
        build_discovered_surfaces_for_conclude(payload, declared)


def test_reason_accepts_canonical_noop_only_while_work_drains() -> None:
    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "reason_result",
        "data": {"rationale": "current work is still draining"},
    }

    assert validate_reason_payload(
        payload,
        open_intents_empty=False,
        max_intents=3,
        surface_manifest=True,
    ) == ("noop", None, [])
    with pytest.raises(ValueError, match="intents is required"):
        validate_reason_payload(
            payload,
            open_intents_empty=True,
            max_intents=3,
            surface_manifest=True,
        )


def test_reason_context_excludes_concluded_unconnected_intents() -> None:
    class FakeContainerManager:
        def write_text_file(self, container_name, path, content) -> None:
            assert container_name == "container-p1"
            assert content == "facts: []"

    project = SimpleNamespace(
        project=SimpleNamespace(
            id="p1",
            scenario_key="default-copy-v2",
            assessment_protocol="default-copy-v1",
            assessment_execution={
                "version": 2, "executable": True, "execution_contract_version": 1,
                "execution_engine": "pi", "enabled": True, "structured": True,
                "phase": "baseline", "surface_manifest_enabled": True,
            },
        ),
        facts=[SimpleNamespace(id="goal"), SimpleNamespace(id="f001")],
        intents=[
            SimpleNamespace(
                id="i-open",
                from_=["f001"],
                description="still open",
                assessment_role="discovery",
                target_observation_id=None,
                slot_key="cell:001",
                strategy_key="baseline.v1",
                depth_review_fact_id=None,
                depends_on=None,
                worker=None,
                to=None,
                concluded_at=None,
            ),
            SimpleNamespace(
                id="i-abandoned",
                from_=["f001"],
                description="already abandoned",
                worker=None,
                to=None,
                concluded_at="2026-08-18T00:00:00Z",
            ),
            SimpleNamespace(
                id="i-complete",
                from_=["f001"],
                description="connected fact",
                worker=None,
                to="f002",
                concluded_at="2026-08-18T00:00:00Z",
            ),
        ],
    )
    binding = {
        "version": 1,
        "assessment_phase": "baseline",
        "issued_at": "2026-09-19T08:00:00Z",
        "eligible_cells": [
            {
                "slot_key": "cell:001",
                "surface_kind": "endpoint",
                "surface_key": "GET /synthetic",
                "test_family": "authorization",
                "applicability_basis": "synthetic authorized state",
                "completion_criteria": "compare controlled identities",
                "coverage_requirement": "required",
                "tier": "core",
                "strategy": {"mode": "any"},
            }
        ],
    }
    binding["digest"] = sha256(
        json.dumps(
            binding,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    context = build_reason_prompt_context(
        SimpleNamespace(tasks=SimpleNamespace(reason=SimpleNamespace(max_intents=4))),
        FakeContainerManager(),
        "container-p1",
        project,
        "facts: []",
        admission_context={"structured_intent_binding": binding},
    )

    assert [item["id"] for item in context.open_intents] == ["i-open"]
    assert "i-open" in context.replacements["open_intents"]
    assert "i-abandoned" not in context.replacements["open_intents"]
    assert "i-complete" not in context.replacements["open_intents"]




def test_reason_rejects_missing_accepted_envelope() -> None:
    payload = {
        "protocol": "cairn-assessment-v2",
        "kind": "reason_result",
        "data": {
            "intents": [
                {
                    "from": ["f001"],
                    "description": "test pending cell",
                    "slot_key": "cell:001",
                    "strategy_key": "baseline.v1",
                }
            ]
        },
    }

    with pytest.raises(ValueError, match="discriminator 'accepted'"):
        validate_reason_payload(
            payload,
            open_intents_empty=True,
            max_intents=3,
            surface_manifest=True,
        )


def test_reason_rejects_source_fact_aliases() -> None:
    for alias in ("source_fact_ids", "source_facts"):
        payload = {
            "protocol": "cairn-assessment-v2",
            "accepted": True,
            "kind": "reason_result",
            "data": {
                "intents": [
                    {
                        alias: ["f001"],
                        "description": "test pending cell",
                        "slot_key": "cell:001",
                        "strategy_key": "baseline.v1",
                    }
                ]
            },
        }

        with pytest.raises(ValueError, match="invalid intent at index 0"):
            validate_reason_payload(
                payload,
                open_intents_empty=True,
                max_intents=3,
                surface_manifest=True,
            )


def test_reason_rejects_false_complete_with_intents() -> None:
    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "reason_result",
        "data": {
            "complete": False,
            "intents": [
                {
                    "from": ["f001"],
                    "description": "test pending cell",
                    "slot_key": "cell:001",
                    "strategy_key": "baseline.v1",
                }
            ],
        },
    }

    with pytest.raises(ValueError, match="complete and intents cannot coexist"):
        validate_reason_payload(
            payload,
            open_intents_empty=True,
            max_intents=3,
            surface_manifest=True,
        )


def test_reason_accepts_canonical_intent_contract() -> None:
    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "reason_result",
        "data": {
            "intents": [
                {
                    "from": ["f001"],
                    "description": "test pending cell",
                    "slot_key": "cell:001",
                    "strategy_key": "baseline.v1",
                }
            ]
        },
    }

    kind, intents, abandon = validate_reason_payload(
        payload,
        open_intents_empty=True,
        max_intents=3,
        surface_manifest=True,
    )

    assert kind == "intents"
    assert intents == payload["data"]["intents"]
    assert abandon == []


def test_surface_discovery_is_registration_only_and_not_ambiguous() -> None:
    surface = {
        "surface_kind": "host",
        "surface_key": "api.example.com",
        "applicable_tests": [],
    }
    kind, data, abandon = validate_reason_payload(
        {
            "protocol": "cairn-assessment-v2",
            "accepted": True,
            "kind": "reason_result",
            "data": {"discovered_surfaces": [surface]},
        },
        open_intents_empty=True,
        max_intents=4,
        surface_manifest=True,
    )
    assert (kind, data, abandon) == ("noop", None, [])

    with pytest.raises(ValueError, match="registration-only"):
        validate_reason_payload(
            {
                "protocol": "cairn-assessment-v2",
                "accepted": True,
                "kind": "reason_result",
                "data": {
                    "discovered_surfaces": [surface],
                    "intents": [{"from": ["f001"], "description": "立即测试"}],
                },
            },
            open_intents_empty=True,
            max_intents=4,
            surface_manifest=True,
        )


def test_reason_surface_discovery_rejects_cross_mode_payloads() -> None:
    surface = {
        "surface_kind": "host",
        "surface_key": "api.example.com",
        "applicable_tests": [],
    }
    slot = {
        "kind": "endpoint",
        "surface_kind": "endpoint",
        "surface_key": "POST https://api.example.com/login",
        "test_family": "authentication",
        "applicability_basis": "登录端点处理凭据",
        "completion_criteria": "比较有效和无效凭据",
    }

    assert validate_reason_payload(
        {"protocol":"cairn-assessment-v2","accepted": True, "kind": "reason_result", "data": {"discovered_surfaces": [surface]}},
        open_intents_empty=True,
        max_intents=4,
        surface_manifest=True,
    ) == ("noop", None, [])
    with pytest.raises(ValueError, match="discovered_slots is invalid"):
        validate_reason_payload(
            {"protocol":"cairn-assessment-v2","accepted": True, "kind": "reason_result", "data": {"discovered_slots": [slot]}},
            open_intents_empty=True,
            max_intents=4,
            surface_manifest=True,
        )

    assert validate_reason_payload(
        {"protocol":"cairn-assessment-v2","accepted": True, "kind": "reason_result", "data": {"discovered_slots": [slot]}},
        open_intents_empty=True,
        max_intents=4,
        surface_manifest=False,
    ) == ("noop", None, [])
    with pytest.raises(ValueError, match="discovered_surfaces is invalid"):
        validate_reason_payload(
            {"protocol":"cairn-assessment-v2","accepted": True, "kind": "reason_result", "data": {"discovered_surfaces": [surface]}},
            open_intents_empty=True,
            max_intents=4,
            surface_manifest=False,
        )

    with pytest.raises(ValueError, match="cannot coexist"):
        validate_explore_payload(
            {
                "protocol": "cairn-assessment-v2",
                "accepted": True,
                "kind": "explore_result",
                "data": {
                    "fact": {"description": "完成测试"},
                    "discovered_surfaces": [surface],
                    "discovered_slots": [],
                },
            }
        )


def test_bootstrap_scope_disposition_validates_and_resolves_artifact_alias() -> None:
    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "bootstrap_result",
        "data": {
            "fact": {"description": "No reachable Surface was confirmed."},
            "complete": {"description": "Request bounded completion."},
            "artifacts": [{"handle": "evidence", "kind": "log", "path": "/tmp/reachability.log"}],
            "coverage_scope_disposition": {
                "disposition_kind": "no_reachable_surfaces",
                "target_ids": ["target2", "target1", "target1"],
                "reason": "Both approved targets were unreachable.",
                "evidence_refs": [{"artifact_handle": "evidence"}],
            },
        },
    }

    kind, data = validate_bootstrap_execute_payload(payload)
    assert kind == "complete"
    assert data is not None
    assert data["coverage_scope_disposition"]["target_ids"] == [
        "target1",
        "target2",
    ]
    assert extract_coverage_scope_disposition(payload) is not None

    resolved = build_coverage_scope_disposition_for_conclude(
        payload,
        ProcessArtifactRecord(
            refs=[EvidenceRef(artifact_id="a123")],
            artifact_ids_by_handle={"evidence": "a123"},
        ),
    )
    assert resolved is not None
    assert resolved["evidence_refs"] == [{"artifact_id": "a123", "pointer": ""}]


def test_bootstrap_scope_disposition_rejects_surface_coexistence_and_empty_evidence() -> None:
    base = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "bootstrap_conclude_result",
        "data": {
            "fact": {"description": "Bootstrap stopped."},
            "coverage_scope_disposition": {
                "disposition_kind": "no_in_scope_targets",
                "target_ids": [],
                "reason": "No active target remains in scope.",
                "evidence_refs": [],
            },
        },
    }
    with pytest.raises(ValueError, match="evidence_refs must not be empty"):
        validate_bootstrap_conclude_payload(base)

    base["data"]["coverage_scope_disposition"]["evidence_refs"] = ["a1"]
    base["data"]["discovered_surfaces"] = [
        {
            "surface_kind": "host",
            "surface_key": "example.com",
            "applicable_tests": [],
        }
    ]
    with pytest.raises(ValueError, match="cannot coexist"):
        validate_bootstrap_conclude_payload(base)


def test_explore_surface_discovery_rejects_cross_mode_payloads() -> None:
    surface = {
        "surface_kind": "host",
        "surface_key": "api.example.com",
        "applicable_tests": [],
    }
    legacy_slot = {
        "kind": "endpoint",
        "key": "GET /health",
        "surface_key": "GET /health",
    }

    manifest_payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "explore_result",
        "data": {
            "fact": {"description": "发现相邻 Surface"},
            "discovered_surfaces": [surface],
        },
    }
    legacy_payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "explore_result",
        "data": {
            "fact": {"description": "发现 legacy Cell"},
            "discovered_slots": [legacy_slot],
        },
    }

    assert validate_explore_payload(
        manifest_payload,
        surface_manifest=True,
    ) == ("fact", "发现相邻 Surface")
    with pytest.raises(ValueError, match="discovered_surfaces is invalid"):
        validate_explore_payload(manifest_payload, surface_manifest=False)

    assert validate_explore_payload(
        legacy_payload,
        surface_manifest=False,
    ) == ("fact", "发现 legacy Cell")
    with pytest.raises(ValueError, match="discovered_slots is invalid"):
        validate_explore_payload(legacy_payload, surface_manifest=True)


def test_minimum_coverage_bootstrap_requires_surface_resolution() -> None:
    execute_payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "bootstrap_result",
        "data": {
            "fact": {"description": "完成启动分析"},
            "complete": {"description": "请求完成"},
        },
    }
    conclude_payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "bootstrap_conclude_result",
        "data": {"fact": {"description": "完成启动分析"}},
    }

    with pytest.raises(ValueError, match="requires discovered_surfaces or coverage_scope_disposition"):
        validate_bootstrap_execute_payload(
            execute_payload,
            require_surface_resolution=True,
        )
    with pytest.raises(ValueError, match="requires discovered_surfaces or coverage_scope_disposition"):
        validate_bootstrap_conclude_payload(
            conclude_payload,
            require_surface_resolution=True,
        )


def test_bootstrap_scope_disposition_allows_empty_surface_list() -> None:
    payload = {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": "bootstrap_conclude_result",
        "data": {
            "fact": {"description": "未确认可测试 Surface"},
            "discovered_surfaces": [],
            "coverage_scope_disposition": {
                "disposition_kind": "no_in_scope_targets",
                "target_ids": [],
                "reason": "没有 active Target。",
                "evidence_refs": [{"artifact_id": "a1"}],
            },
        },
    }

    assert validate_bootstrap_conclude_payload(payload) == (
        "fact",
        "未确认可测试 Surface",
    )


def test_client_transports_coverage_scope_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CairnClient("http://server.test", token="test-token")
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs) -> ApiResult:
        captured.update({"method": method, "path": path, **kwargs})
        return ApiResult(status_code=200)

    monkeypatch.setattr(client, "_request_json", fake_request)
    disposition = {
        "disposition_kind": "no_in_scope_targets",
        "target_ids": [],
        "reason": "No active targets remain.",
        "evidence_refs": [{"artifact_id": "a1"}],
    }
    client.conclude_with_evidence(
        "proj1",
        "intent1",
        "worker1",
        "No Surface",
        [],
        task_run_id="tr1",
        coverage_scope_disposition=disposition,
    )

    assert captured["json"]["coverage_scope_disposition"] == disposition


def test_client_transports_verification_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CairnClient("http://server.test", token="test-token")
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs) -> ApiResult:
        captured.update({"method": method, "path": path, **kwargs})
        return ApiResult(status_code=200)

    monkeypatch.setattr(client, "_request_json", fake_request)
    verification = {
        "observation_id": "vo1",
        "outcome": "confirmed",
        "evidence_refs": [{"artifact_id": "a1"}],
        "notes": "independent session",
    }
    client.conclude_with_evidence(
        "proj1",
        "intent1",
        "worker1",
        "confirmed",
        [],
        task_run_id="tr1",
        verification_result=verification,
    )

    assert captured["json"]["verification_result"] == verification


def test_bootstrap_completion_succeeds_without_report_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CompletionClient:
        def complete_with_evidence(self, *_args, **_kwargs) -> ApiResult:
            return ApiResult(status_code=200)

    monkeypatch.setattr(
        bootstrap_results,
        "write_conclude_result_with_fact_id",
        lambda *_args, **_kwargs: ConcludeWriteResult("success", "f001"),
    )
    monkeypatch.setattr(
        bootstrap_results,
        "record_protocol_write",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        bootstrap_results,
        "record_decision_result",
        lambda *_args, **_kwargs: None,
    )

    result = bootstrap_results.write_bootstrap_complete_result(
        CompletionClient(),
        "proj1",
        "intent1",
        "worker1",
        "fact",
        "complete",
        source="bootstrap",
        phase_ms=1,
    )

    assert result == "success"


def test_process_artifact_truncates_large_stdout(monkeypatch) -> None:
    monkeypatch.setattr(artifact_records, "PROCESS_ARTIFACT_MAX_BYTES", 20)
    monkeypatch.setattr(artifact_records, "PROCESS_ARTIFACT_HEAD_BYTES", 8)
    monkeypatch.setattr(artifact_records, "PROCESS_ARTIFACT_TAIL_BYTES", 8)
    client = FakeClient()
    full_output = _successful_process_stdout("A" * 30 + "Z" * 30, task_run_id="tr028")

    record = record_process_artifacts(
        client,
        "proj_015",
        "tr028",
        phase="explore_execute",
        result=ProcessResult(returncode=0, stdout=full_output, stderr=""),
    )

    assert record.write_failed is False
    assert record.refs[0].artifact_id == "a001"
    stored = client.artifacts[0]["content"]
    metadata = client.artifacts[0]["metadata"]
    assert isinstance(stored, str)
    assert stored.startswith(full_output[:8])
    assert stored.endswith(full_output[-8:])
    assert "raw output truncated" in stored
    assert metadata["truncated"] is True
    assert metadata["original_size"] == len(full_output.encode())


def test_timeout_does_not_publish_raw_artifacts_even_with_success_stdout() -> None:
    client = FakeClient()
    result = ProcessResult(
        returncode=124,
        stdout=_successful_process_stdout("private-target-body"),
        stderr="private-provider-body",
        timed_out=True,
    )

    record = record_process_artifacts(
        client, "proj_015", "tr027", phase="explore_execute", result=result,
    )

    assert record.refs == []
    assert record.write_failed is False
    assert client.artifacts == []
    assert client.events == []
