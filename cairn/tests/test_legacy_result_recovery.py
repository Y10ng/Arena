from __future__ import annotations

import copy
import json

import pytest

from cairn.dispatcher.contracts import (
    validate_bootstrap_conclude_payload,
    validate_bootstrap_execute_payload,
    validate_explore_payload,
)
from cairn.dispatcher.tasks.artifact_records import (
    UnresolvedArtifactEvidenceError,
    resolve_declared_artifact_evidence_refs,
    resolve_optional_fact_evidence_refs,
)
from cairn.dispatcher.tasks.common_base import ProcessArtifactRecord
from cairn.dispatcher.worker_result_gateway import decode_pi_result
from cairn.server.models import EvidenceRef


def _envelope(kind: str, data: dict[str, object], **extra: object) -> dict[str, object]:
    return {
        "protocol": "cairn-assessment-v2",
        "accepted": True,
        "kind": kind,
        "data": data,
        **extra,
    }


@pytest.mark.parametrize(
    ("phase", "kind"),
    (
        ("explore_execute", "explore_result"),
        ("explore_conclude", "explore_conclude_result"),
        ("bootstrap_execute", "bootstrap_result"),
        ("bootstrap_conclude", "bootstrap_conclude_result"),
    ),
)
@pytest.mark.parametrize("placement", ("legacy", "collision", "invalid"))
def test_pi_rejects_legacy_vulnerability_without_repair_or_mutation(
    phase: str, kind: str, placement: str,
) -> None:
    data = {"fact": {"description": "可用的结论"}}
    if phase == "bootstrap_execute":
        data["complete"] = {"description": "启动完成"}
    payload = _envelope(kind, data)
    validator = (
        validate_bootstrap_execute_payload if phase == "bootstrap_execute"
        else validate_bootstrap_conclude_payload if phase == "bootstrap_conclude"
        else lambda value: validate_explore_payload(value, expected_kind=kind)
    )

    def decode():
        return decode_pi_result(
            json.dumps({
                "protocol": "cairn-agent-result-v1", "engine": "pi",
                "project_id": "p1", "task_run_id": "tr1", "phase": phase,
                "session": "pi:07b53667-a6ab-44be-badb-e3ed6b347331",
                "status": "succeeded", "result": payload,
                "submission_id": "a" * 64, "usage": {}, "error": None,
            }),
            phase=phase, validator=validator,
        )

    # 先证明正常结果可以通过，避免无关的信封错误掩盖旧字段拒绝契约。
    assert decode().failure is None
    if placement == "collision":
        data["fact"]["vulnerability"] = {"title": "原结论", "severity": "low"}
    data["vulnerability"] = (
        {"title": "缺少严重级别"} if placement == "invalid"
        else {"title": "旧位置结论", "severity": "high"}
    )
    original = copy.deepcopy(payload)

    outcome = decode()

    assert outcome.failure is not None
    assert outcome.failure.failure_class == "result_schema"
    assert outcome.failure.code == "phase_shape_invalid"
    assert outcome.failure.phase == phase
    assert outcome.failure.path is None
    assert outcome.failure.retryable is False
    assert outcome.failure.message == "Worker 结果不符合阶段数据结构：phase_shape_invalid"
    diagnostic = json.dumps(outcome.failure.details(), ensure_ascii=False)
    assert data["vulnerability"]["title"] not in diagnostic
    assert payload == original


def test_structured_explore_accepts_protocol_reason_field() -> None:
    payload = _envelope(
        "explore_result",
        {"fact": {"description": "结构化探索结论"}},
        reason="补充说明",
    )

    assert validate_explore_payload(
        payload,
        strict_contract=True,
        expected_kind="explore_result",
    ) == ("fact", "结构化探索结论")


def test_optional_fact_evidence_drops_only_unresolved_aliases() -> None:
    declared = ProcessArtifactRecord(
        refs=[EvidenceRef(artifact_id="a101"), EvidenceRef(artifact_id="a102")],
        artifact_ids_by_handle={"first": "a101", "second": "a102"},
    )
    refs = [
        EvidenceRef(artifact_id="first", pointer="line:1"),
        EvidenceRef(artifact_id="artifact_001", pointer="line:2"),
        EvidenceRef(artifact_id="a900", pointer="line:3"),
        EvidenceRef(artifact_id="second", pointer="line:4"),
    ]

    resolved, warnings = resolve_optional_fact_evidence_refs(refs, declared)

    assert [(ref.artifact_id, ref.pointer) for ref in resolved] == [
        ("a101", "line:1"),
        ("a900", "line:3"),
        ("a102", "line:4"),
    ]
    assert warnings == [
        {
            "type": "dropped_unresolved_optional_fact_evidence_ref",
            "field": "data.fact.evidence_refs[1]",
            "reference": "artifact_001",
            "reason": "unresolved_artifact_handle",
            "message": "可选 Fact 证据引用无法解析，已丢弃",
        }
    ]


def test_optional_fact_evidence_can_drop_all_aliases() -> None:
    resolved, warnings = resolve_optional_fact_evidence_refs(
        [
            EvidenceRef(artifact_id="artifact_doc"),
            EvidenceRef(artifact_id="artifact_paths"),
        ],
        ProcessArtifactRecord(
            refs=[EvidenceRef(artifact_id="a201")],
            artifact_ids_by_handle={"openapi_docs": "a201"},
        ),
    )

    assert resolved == []
    assert [warning["reference"] for warning in warnings] == [
        "artifact_doc",
        "artifact_paths",
    ]


def test_strict_evidence_resolution_still_rejects_unresolved_alias() -> None:
    with pytest.raises(UnresolvedArtifactEvidenceError, match="artifact_doc"):
        resolve_declared_artifact_evidence_refs(
            [EvidenceRef(artifact_id="artifact_doc")],
            ProcessArtifactRecord(
                refs=[EvidenceRef(artifact_id="a201")],
                artifact_ids_by_handle={"openapi_docs": "a201"},
            ),
        )
