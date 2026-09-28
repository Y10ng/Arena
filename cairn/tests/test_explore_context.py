from __future__ import annotations

from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.tasks.explore_context import (
    resolve_admitted_coverage_cell,
    resolve_bound_coverage_cell,
)
from cairn.dispatcher.tasks.surface_policy import (
    coverage_scope_disposition_contract,
    surface_discovery_contract,
)
from cairn.server.models import Intent


class FakeClient:
    def __init__(self, slots: list[dict[str, object]]):
        self.slots = slots

    def list_coverage_slots(self, project_id: str) -> ApiResult:
        assert project_id == "p1"
        return ApiResult(status_code=200, data=self.slots)


def _intent(
    *,
    slot_key: str = "cell:real",
    strategy_key: str = "auth.owner-vs-other-v1",
) -> Intent:
    return Intent(
        id="i004",
        **{"from": ["f1"]},
        description="test authorization",
        creator="dispatcher",
        created_at="2026-08-16T00:00:00Z",
        slot_key=slot_key,
        strategy_key=strategy_key,
    )


def _slot(**overrides: object) -> dict[str, object]:
    slot: dict[str, object] = {
        "slot_key": "cell:real",
        "kind": "endpoint",
        "surface_kind": "endpoint",
        "surface_key": "POST https://example.test/api/candidate/1/info/detail",
        "test_family": "authorization",
        "applicability_basis": "object identifier in route",
        "completion_criteria": "compare owner and non-owner sessions",
        "assigned_intent_id": "i004",
    }
    slot.update(overrides)
    return slot


def _admission_context(**binding_updates: object) -> dict[str, object]:
    binding: dict[str, object] = {
        "version": 1,
        "assessment_phase": "baseline",
        "admitted_at": "2026-09-19T08:00:00Z",
        "intent_id": "i004",
        "strategy_key": "auth.owner-vs-other-v1",
        "cell": {
            "slot_key": "cell:real",
            "surface_kind": "endpoint",
            "surface_key": "POST https://example.test/api/candidate/1/info/detail",
            "test_family": "authorization",
            "applicability_basis": "object identifier in route",
            "completion_criteria": "compare owner and non-owner sessions",
            "coverage_requirement": "required",
            "tier": "core",
            "strategy": {"mode": "any"},
        },
    }
    binding.update(binding_updates)
    binding["digest"] = sha256(
        json.dumps(
            binding,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {"structured_explore_binding": binding}


def _project(*, manifest_enabled: bool = True):
    return SimpleNamespace(
        project=SimpleNamespace(
            assessment_protocol="default-copy-v1",
            # 故意与服务端执行视图相反，防止重新从旧快照推导能力。
            scenario_snapshot={"features": {"surface_manifest_version": int(not manifest_enabled)}},
            assessment_execution={
                "version": 2, "executable": True, "execution_contract_version": 1,
                "execution_engine": "pi", "enabled": True, "structured": True,
                "surface_manifest_enabled": manifest_enabled,
                "minimum_coverage_enabled": manifest_enabled,
            },
        )
    )


def test_surface_discovery_prompt_contract_uses_authoritative_execution_view() -> None:
    disabled = surface_discovery_contract(_project(manifest_enabled=False), phase="bootstrap")
    manifest = surface_discovery_contract(_project(), phase="bootstrap")
    reason = surface_discovery_contract(_project(), phase="reason")

    assert "do not submit discovered_surfaces or coverage_result" in disabled
    assert "discovered_surfaces using the submit_result tool Schema" in manifest
    assert "data.slots" not in manifest
    assert "standalone decision: omit intents, complete and depth_review" in reason
    assert "Server-issued Cell keys in a later decision" in reason


@pytest.mark.parametrize("phase", ("bootstrap", "explore", "reason"))
def test_surface_discovery_prompt_uses_registered_artifact_handles(phase: str) -> None:
    project = _project()

    contract = surface_discovery_contract(project, phase=phase)

    assert "register_artifact(path,kind)" in contract
    assert "reference its returned handle" in contract
    assert "do not invent handles or put file paths in evidence_refs" in contract
    assert "declared in data.artifacts" not in contract


def test_coverage_scope_disposition_uses_registered_artifact_handles() -> None:
    project = _project()

    contract = coverage_scope_disposition_contract(project)

    assert "Register each local evidence file with register_artifact" in contract
    assert "use the returned artifact_handle" in contract
    assert "existing Artifact IDs must belong to this project" in contract
    assert "non-empty evidence_refs" in contract
    assert "declared in data.artifacts" not in contract


def test_resolve_admitted_coverage_cell_uses_frozen_snapshot() -> None:
    cell = resolve_admitted_coverage_cell(_admission_context(), _intent())

    assert json.loads(cell.prompt_json()) == {
        "slot_key": "cell:real",
        "surface_kind": "endpoint",
        "surface_key": "POST https://example.test/api/candidate/1/info/detail",
        "test_family": "authorization",
        "applicability_basis": "object identifier in route",
        "completion_criteria": "compare owner and non-owner sessions",
    }


@pytest.mark.parametrize(
    "context,intent,error",
    [
        (None, _intent(), "context is missing"),
        (_admission_context(intent_id="other"), _intent(), "intent does not match"),
        (_admission_context(), _intent(slot_key="cell:other"), "slot does not match"),
        (
            _admission_context(),
            _intent(strategy_key="auth.different-v1"),
            "strategy does not match",
        ),
    ],
)
def test_resolve_admitted_coverage_cell_rejects_mismatched_contract(
    context: dict[str, object] | None,
    intent: Intent,
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        resolve_admitted_coverage_cell(context, intent)


def test_resolve_admitted_coverage_cell_rejects_digest_tampering() -> None:
    context = _admission_context()
    binding = context["structured_explore_binding"]
    assert isinstance(binding, dict)
    binding["strategy_key"] = "auth.changed-v1"

    with pytest.raises(Exception, match="structured_explore_binding_invalid"):
        resolve_admitted_coverage_cell(context, _intent())


def test_resolve_bound_coverage_cell_uses_exact_assigned_identity() -> None:
    cell = resolve_bound_coverage_cell(
        FakeClient([_slot(slot_key="cell:other"), _slot()]),
        "p1",
        _intent(),
    )

    prompt = json.loads(cell.prompt_json())
    assert prompt == {
        "slot_key": "cell:real",
        "surface_kind": "endpoint",
        "surface_key": "POST https://example.test/api/candidate/1/info/detail",
        "test_family": "authorization",
        "applicability_basis": "object identifier in route",
        "completion_criteria": "compare owner and non-owner sessions",
    }

    coverage_result: dict[str, object] = {
        "slot_key": "cell:i004",
        "surface_key": "https://example.test/api/candidate/1/info/detail",
        "test_family": "authorization",
        "strategy_key": "model-guessed-strategy",
        "outcome": "temporarily_blocked",
    }
    assert cell.bind_result_identity(
        coverage_result,
        strategy_key="auth.owner-vs-other-v1",
    ) == ["slot_key", "surface_key", "strategy_key"]
    assert coverage_result["slot_key"] == "cell:real"
    assert coverage_result["surface_key"] == prompt["surface_key"]
    assert coverage_result["test_family"] == "authorization"
    assert coverage_result["strategy_key"] == "auth.owner-vs-other-v1"


def test_resolve_bound_coverage_cell_rejects_other_intent_assignment() -> None:
    with pytest.raises(ValueError, match="not assigned"):
        resolve_bound_coverage_cell(
            FakeClient([_slot(assigned_intent_id="i999")]),
            "p1",
            _intent(),
        )


def test_resolve_bound_coverage_cell_rejects_incomplete_identity() -> None:
    with pytest.raises(ValueError, match="identity is incomplete"):
        resolve_bound_coverage_cell(
            FakeClient([_slot(surface_key="")]),
            "p1",
            _intent(),
        )
