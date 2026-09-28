from __future__ import annotations

import pytest

from cairn.server.models import SurfaceApplicabilityInput
from cairn.server.services.common import COVERAGE_SLOT_KINDS
from cairn.server.services.minimum_coverage import derive_coverage_cells


def test_rules_derive_required_families_and_keep_worker_extras_optional() -> None:
    cells = derive_coverage_cells(
        surface_id="surface:login",
        surface_kind="endpoint",
        surface_key="POST https://example.com/login",
        description="Login endpoint",
        traits={
            "accepts_credentials": "true",
            "sets_cookie": "yes",
            "object_identifier": "account_id",
            "roles": "member,admin",
        },
        applicable_tests=[
            SurfaceApplicabilityInput(
                test_family="authentication",
                applicability_basis="Worker observed a login form",
                completion_criteria="Compare valid and invalid credentials",
            ),
            SurfaceApplicabilityInput(
                test_family="data_exposure",
                applicability_basis="Response contains account metadata",
                completion_criteria="Inspect authenticated response fields",
            ),
        ],
        rule_version=1,
    )

    by_family = {cell.test_family: cell for cell in cells}
    assert {"authentication", "authorization", "session"} <= set(by_family)
    assert by_family["authentication"].coverage_requirement == "required"
    assert by_family["authentication"].coverage_rule_id == "v1.endpoint.credentials"
    assert by_family["data_exposure"].coverage_requirement == "optional"
    assert by_family["data_exposure"].coverage_rule_id == "v1.worker-explicit"


@pytest.mark.parametrize("surface_kind", sorted(COVERAGE_SLOT_KINDS))
def test_every_supported_surface_kind_has_a_required_fallback(surface_kind: str) -> None:
    cells = derive_coverage_cells(
        surface_id=f"surface:{surface_kind}",
        surface_kind=surface_kind,
        surface_key=f"example-{surface_kind}",
        description="",
        traits={},
        applicable_tests=[],
        rule_version=1,
    )

    assert cells
    assert all(cell.coverage_requirement == "required" for cell in cells)
    assert all(cell.surface_id == f"surface:{surface_kind}" for cell in cells)


def test_minimum_coverage_rejects_unknown_rule_version() -> None:
    with pytest.raises(ValueError, match="minimum_coverage_rule_version_unsupported"):
        derive_coverage_cells(
            surface_id="surface:test",
            surface_kind="endpoint",
            surface_key="GET https://example.com/",
            description="",
            traits={},
            applicable_tests=[],
            rule_version=2,
        )
