from __future__ import annotations

from hashlib import sha256
import json

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from cairn.assessment_contracts import (
    StructuredCoverageCellBinding,
    StructuredIntentBinding,
    StructuredStrategyConstraint,
)
from cairn.server.services.structured_intent_bindings import (
    parse_structured_explore_binding,
    parse_structured_intent_binding,
)


def canonical_digest(payload: dict[str, object]) -> str:
    canonical = {
        key: value for key, value in payload.items() if key != "digest"
    }
    return sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def cell_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "slot_key": "cell-auth-owner",
        "surface_kind": "route",
        "surface_key": "GET /accounts/{id}",
        "test_family": "authorization",
        "applicability_basis": "authenticated owner and non-owner states exist",
        "completion_criteria": "compare owner and non-owner responses",
        "coverage_requirement": "required",
        "tier": "core",
        "strategy": {"mode": "any"},
    }
    payload.update(updates)
    return payload


def intent_binding_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": 1,
        "assessment_phase": "baseline",
        "issued_at": "2026-09-19T08:00:00Z",
        "eligible_cells": [cell_payload()],
    }
    payload.update(updates)
    payload["digest"] = canonical_digest(payload)
    return payload


def explore_binding_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": 1,
        "assessment_phase": "baseline",
        "admitted_at": "2026-09-19T08:01:00Z",
        "intent_id": "i001",
        "strategy_key": "authorization.owner-vs-other-v1",
        "cell": cell_payload(),
    }
    payload.update(updates)
    payload["digest"] = canonical_digest(payload)
    return payload


def test_structured_binding_models_forbid_unknown_fields():
    with pytest.raises(ValidationError):
        StructuredCoverageCellBinding.model_validate(
            cell_payload(unexpected=True)
        )


def test_structured_intent_binding_requires_unique_slot_keys():
    payload = intent_binding_payload(
        eligible_cells=[cell_payload(), cell_payload()]
    )

    with pytest.raises(ValidationError, match="unique slot_key"):
        StructuredIntentBinding.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "must_differ"},
        {
            "mode": "any",
            "excluded_strategy_key": "authorization.owner-baseline-v1",
        },
    ],
)
def test_strategy_constraint_requires_consistent_exclusion(
    payload: dict[str, object],
):
    with pytest.raises(ValidationError):
        StructuredStrategyConstraint.model_validate(payload)


def test_strategy_key_rejects_noncanonical_syntax():
    payload = explore_binding_payload(strategy_key="Authorization Owner V1")

    with pytest.raises(HTTPException) as denied:
        parse_structured_explore_binding(payload)

    assert (denied.value.status_code, denied.value.detail) == (
        422,
        "structured_explore_binding_invalid",
    )


def test_intent_binding_digest_is_independent_of_object_key_order():
    payload = intent_binding_payload()
    reordered = {
        "digest": payload["digest"],
        "eligible_cells": payload["eligible_cells"],
        "issued_at": payload["issued_at"],
        "assessment_phase": payload["assessment_phase"],
        "version": payload["version"],
    }

    parsed = parse_structured_intent_binding(reordered)

    assert parsed.digest == payload["digest"]
    assert parsed.eligible_cells[0].slot_key == "cell-auth-owner"


def test_intent_binding_rejects_digest_tampering():
    payload = intent_binding_payload()
    payload["eligible_cells"][0]["surface_key"] = "GET /admin"  # type: ignore[index]

    with pytest.raises(HTTPException) as denied:
        parse_structured_intent_binding(payload)

    assert (denied.value.status_code, denied.value.detail) == (
        422,
        "structured_intent_binding_invalid",
    )


@pytest.mark.parametrize("include_source_digest", [False, True])
def test_explore_binding_digest_preserves_optional_field_shape(
    include_source_digest: bool,
):
    updates: dict[str, object] = {}
    if include_source_digest:
        updates["source_binding_digest"] = "a" * 64
    payload = explore_binding_payload(**updates)

    parsed = parse_structured_explore_binding(payload)
    serialized = parsed.model_dump(mode="json", exclude_none=True)

    assert ("source_binding_digest" in serialized) is include_source_digest
    assert parse_structured_explore_binding(serialized).digest == payload["digest"]
