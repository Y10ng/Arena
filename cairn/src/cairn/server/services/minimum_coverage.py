"""default-copy v1 的确定性 minimum Coverage 规则派生。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from .common import COVERAGE_SLOT_KINDS, DEFAULT_COPY_TEST_FAMILIES


CoverageRequirement = Literal["required", "optional"]


@dataclass(frozen=True, slots=True)
class DerivedCoverageCell:
    surface_id: str
    surface_kind: str
    surface_key: str
    test_family: str
    coverage_requirement: CoverageRequirement
    coverage_rule_id: str
    description: str
    applicability_basis: str
    completion_criteria: str


@dataclass(frozen=True, slots=True)
class _Rule:
    rule_id: str
    surface_kinds: frozenset[str]
    test_family: str
    any_traits: tuple[str, ...] = ()
    all_trait_groups: tuple[tuple[str, ...], ...] = ()
    applicability_basis: str = ""
    completion_criteria: str = ""


_RULES_V1: tuple[_Rule, ...] = (
    _Rule(
        "v1.endpoint.credentials",
        frozenset({"endpoint", "param"}),
        "authentication",
        any_traits=(
            "accepts_credentials",
            "credential_input",
            "credentials",
            "login",
            "password",
        ),
        applicability_basis="Surface accepts or processes authentication credentials.",
        completion_criteria="Exercise valid, invalid, missing, and boundary credential states with captured Evidence.",
    ),
    _Rule(
        "v1.object-role-boundary",
        frozenset({"endpoint", "param", "role"}),
        "authorization",
        all_trait_groups=(
            ("object_id", "object_identifier", "resource_id"),
            ("role", "roles", "role_boundary"),
        ),
        applicability_basis="Surface combines an object identity with an observed role boundary.",
        completion_criteria="Compare owner, non-owner, and higher/lower-role access to the same object operation.",
    ),
    _Rule(
        "v1.session-material",
        frozenset({"endpoint", "param", "service"}),
        "session",
        any_traits=(
            "cookie",
            "cookies",
            "session",
            "session_token",
            "sets_cookie",
            "token",
        ),
        applicability_basis="Surface issues, accepts, or changes session material.",
        completion_criteria="Test session creation, reuse, rotation, invalidation, and boundary transitions.",
    ),
    _Rule(
        "v1.interpreter-input",
        frozenset({"endpoint", "param", "service"}),
        "injection",
        any_traits=(
            "backend_input",
            "command_input",
            "interpreter",
            "query_input",
            "user_input",
        ),
        applicability_basis="Observed input reaches a backend query, command, or interpreter boundary.",
        completion_criteria="Compare a safe baseline with syntax, type, encoding, and delimiter variants.",
    ),
    _Rule(
        "v1.file-boundary",
        frozenset({"endpoint", "param", "path"}),
        "file_handling",
        any_traits=(
            "file",
            "file_path",
            "path",
            "upload",
            "uploads_file",
        ),
        applicability_basis="Surface accepts file content, a filename, or a filesystem path.",
        completion_criteria="Test type, name, path, storage, retrieval, and execution boundaries with Artifact Evidence.",
    ),
    _Rule(
        "v1.outbound-url",
        frozenset({"endpoint", "param", "service"}),
        "ssrf",
        any_traits=(
            "fetch_url",
            "outbound_request",
            "remote_url",
            "url_fetch",
            "webhook",
        ),
        applicability_basis="Surface can initiate an outbound request from user-controlled location data.",
        completion_criteria="Compare approved, alternate-host, redirect, and restricted-address destinations.",
    ),
    _Rule(
        "v1.state-workflow",
        frozenset({"endpoint", "flow_state", "role"}),
        "business_logic",
        any_traits=(
            "multi_step",
            "state",
            "state_transition",
            "workflow",
        ),
        applicability_basis="Surface participates in an observed multi-step or stateful business workflow.",
        completion_criteria="Test valid order, skipped steps, replay, stale state, and role/state transitions.",
    ),
    _Rule(
        "v1.concurrent-transition",
        frozenset({"endpoint", "flow_state"}),
        "concurrency",
        any_traits=("concurrent", "race_sensitive", "repeatable_transition"),
        applicability_basis="Observed transition may be invoked concurrently or replayed.",
        completion_criteria="Compare serialized baseline with bounded concurrent and replay variants.",
    ),
    _Rule(
        "v1.sensitive-output",
        frozenset({"endpoint", "service", "path"}),
        "data_exposure",
        any_traits=("personal_data", "secret", "sensitive_data", "sensitive_output"),
        applicability_basis="Surface returns or stores observed sensitive data.",
        completion_criteria="Compare unauthenticated, least-privileged, and intended-recipient data exposure.",
    ),
)

_FALLBACK_V1: dict[str, tuple[str, str, str, str]] = {
    "endpoint": (
        "configuration",
        "v1.endpoint-baseline",
        "Registered endpoint requires a protocol and security-control baseline.",
        "Capture a normal request/response and assess exposed methods, headers, transport, and error behavior.",
    ),
    "service": (
        "known_vulnerability",
        "v1.service-baseline",
        "Registered service requires product/version and exposure assessment.",
        "Identify the service conservatively and assess applicable known weaknesses with supporting Evidence.",
    ),
    "host": (
        "configuration",
        "v1.host-baseline",
        "Registered host requires an exposed-service and trust-boundary baseline.",
        "Assess reachable services, transport posture, and externally observable host configuration.",
    ),
    "role": (
        "authorization",
        "v1.role-baseline",
        "Registered role defines an authorization boundary.",
        "Compare permitted operations and object access against at least one adjacent role boundary.",
    ),
    "path": (
        "file_handling",
        "v1.path-baseline",
        "Registered path defines a file or resource-location boundary.",
        "Assess normalization, traversal, access control, and content handling applicable to the path.",
    ),
    "param": (
        "injection",
        "v1.param-baseline",
        "Registered parameter defines a user-controlled input boundary.",
        "Compare a valid baseline with bounded type, syntax, delimiter, and encoding variants.",
    ),
    "flow_state": (
        "business_logic",
        "v1.flow-state-baseline",
        "Registered state defines a business workflow transition boundary.",
        "Assess allowed predecessors, successors, replay, skipped steps, and role-dependent transitions.",
    ),
}

_FALSE_TRAIT_VALUES = {"", "0", "false", "no", "none", "null", "off", "unknown"}


def _plain_mapping(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return dict(model_dump(exclude_none=True))
    raise ValueError("minimum_coverage_applicability_invalid")


def _normalized_traits(traits: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(key).strip().lower().replace("-", "_").replace(" ", "_"): str(value).strip().lower()
        for key, value in traits.items()
        if str(key).strip()
    }


def _trait_present(traits: Mapping[str, str], aliases: Sequence[str]) -> bool:
    return any(
        alias in traits and traits[alias] not in _FALSE_TRAIT_VALUES
        for alias in aliases
    )


def _rule_matches(rule: _Rule, surface_kind: str, traits: Mapping[str, str]) -> bool:
    if surface_kind not in rule.surface_kinds:
        return False
    if rule.any_traits and not _trait_present(traits, rule.any_traits):
        return False
    return all(_trait_present(traits, group) for group in rule.all_trait_groups)


def derive_coverage_cells(
    *,
    surface_id: str,
    surface_kind: str,
    surface_key: str,
    description: str,
    traits: Mapping[str, Any],
    applicable_tests: Sequence[object],
    rule_version: int,
) -> list[DerivedCoverageCell]:
    """由可信 Manifest 属性派生 required Cells，并把额外 Worker 建议降为 optional。"""
    if rule_version != 1:
        raise ValueError("minimum_coverage_rule_version_unsupported")
    kind = surface_kind.strip().lower()
    if kind not in COVERAGE_SLOT_KINDS:
        raise ValueError("minimum_coverage_surface_kind_invalid")
    stable_surface_id = surface_id.strip()
    if not stable_surface_id:
        raise ValueError("minimum_coverage_surface_id_required")

    normalized_traits = _normalized_traits(traits)
    by_family: dict[str, DerivedCoverageCell] = {}
    for rule in _RULES_V1:
        if not _rule_matches(rule, kind, normalized_traits):
            continue
        by_family.setdefault(
            rule.test_family,
            DerivedCoverageCell(
                surface_id=stable_surface_id,
                surface_kind=kind,
                surface_key=surface_key,
                test_family=rule.test_family,
                coverage_requirement="required",
                coverage_rule_id=rule.rule_id,
                description=description.strip(),
                applicability_basis=rule.applicability_basis,
                completion_criteria=rule.completion_criteria,
            ),
        )

    if not by_family:
        family, rule_id, basis, criteria = _FALLBACK_V1[kind]
        by_family[family] = DerivedCoverageCell(
            surface_id=stable_surface_id,
            surface_kind=kind,
            surface_key=surface_key,
            test_family=family,
            coverage_requirement="required",
            coverage_rule_id=rule_id,
            description=description.strip(),
            applicability_basis=basis,
            completion_criteria=criteria,
        )

    for value in applicable_tests:
        test = _plain_mapping(value)
        family = str(test.get("test_family") or "").strip().lower().replace("-", "_").replace(" ", "_")
        if family not in DEFAULT_COPY_TEST_FAMILIES:
            raise ValueError("minimum_coverage_test_family_invalid")
        if family in by_family:
            continue
        basis = str(test.get("applicability_basis") or "").strip()
        criteria = str(test.get("completion_criteria") or "").strip()
        if not basis or not criteria:
            raise ValueError("minimum_coverage_optional_basis_required")
        by_family[family] = DerivedCoverageCell(
            surface_id=stable_surface_id,
            surface_kind=kind,
            surface_key=surface_key,
            test_family=family,
            coverage_requirement="optional",
            coverage_rule_id="v1.worker-explicit",
            description=str(test.get("description") or description).strip(),
            applicability_basis=basis,
            completion_criteria=criteria,
        )

    return [by_family[family] for family in sorted(by_family)]
