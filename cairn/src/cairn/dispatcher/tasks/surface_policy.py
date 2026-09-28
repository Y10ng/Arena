"""消费 Server 解析的冻结执行策略，不从提示词名称推断能力。"""

from __future__ import annotations


def execution_view(project) -> dict:
    meta = getattr(project, "project", project)
    view = getattr(meta, "assessment_execution", None)
    if not isinstance(view, dict) or view.get("version") != 2 or view.get("executable") is not True:
        raise ValueError("project execution contract is unsupported")
    return view


def surface_manifest_version(project) -> int:
    return 1 if surface_manifest_enabled(project) else 0


def surface_manifest_enabled(project) -> bool:
    return execution_view(project).get("surface_manifest_enabled") is True


def outcome_contract_version(project) -> int:
    return int(execution_view(project).get("outcome_contract_version", 2))


def blocker_cooldown_enabled(project) -> bool:
    return execution_view(project).get("blocker_cooldown_enabled") is True


def minimum_coverage_version(project) -> int:
    return 1 if minimum_coverage_enabled(project) else 0


def minimum_coverage_enabled(project) -> bool:
    return execution_view(project).get("minimum_coverage_enabled") is True


def coverage_scope_disposition_contract(project) -> str:
    if not minimum_coverage_enabled(project):
        return "Do not submit coverage_scope_disposition for this scenario."
    return (
        "Only when no confirmed in-scope Surface exists, submit coverage_scope_disposition "
        "instead of discovered_surfaces. Use no_in_scope_targets with target_ids:[] or "
        "no_reachable_surfaces with the exact active Target IDs, a concrete reason, and "
        "non-empty evidence_refs. Register each local evidence file with register_artifact "
        "and use the returned artifact_handle; existing Artifact IDs must belong to this project."
    )


def surface_discovery_contract(
    project=None,
    *,
    phase: str,
    enabled: bool | None = None,
    allow_reason_intents: bool = False,
) -> str:
    manifest = surface_manifest_enabled(project) if enabled is None else enabled
    if not manifest:
        return "This scenario has no Web Coverage Cells; do not submit discovered_surfaces or coverage_result."
    rule = (
        "Submit only actually observed discovered_surfaces using the submit_result tool Schema. "
        "Each Surface has surface_kind, surface_key, description, traits, applicable_tests and evidence_refs. "
        "Traits are scalar observations, not assumptions. Each applicable_tests entry includes test_family, "
        "applicability_basis and completion_criteria. Register local evidence using register_artifact(path,kind) "
        "and reference its returned handle; do not invent handles or put file paths in evidence_refs. "
        "Re-testing a selected deep hypothesis uses its existing Cell, not a duplicate Surface. "
        "Authorization negatives require a real second identity and normal controls; missing identity "
        "means temporarily_blocked, not tested_no_finding."
    )
    if phase == "reason" and allow_reason_intents:
        rule += (
            " Reason may register observed Surfaces and create source-linked Intents in the same "
            "bounded decision. Surface registration does not authorize a Target or prove a finding."
        )
    elif phase == "reason":
        rule += (
            " New Surface registration is a standalone decision: omit intents, complete and depth_review. "
            "Only use Server-issued Cell keys in a later decision after registration succeeds."
        )
    return rule
