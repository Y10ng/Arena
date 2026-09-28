"""项目授权范围的纯值合同；迁移和运行期必须生成相同摘要。"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Iterable, Mapping

AUTHORIZATION_PROTOCOL = "cairn-project-authorization-v1"
AUTHORIZATION_SCOPE_VERSION = 1
AUTHORIZATION_SCOPE_PREFIX = "authorization-scope:"
TARGET_SCOPE_PREFIX = "target-scope:"
AUTHORIZATION_V2_PROTOCOL = "cairn-project-authorization-v2"
AUTHORIZATION_SCOPE_V3_VERSION = 3
EXPLORATION_WINDOW_SECONDS = {
    "compact": 2 * 60 * 60,
    "balanced": 6 * 60 * 60,
    "thorough": 12 * 60 * 60,
}


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_object(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def scenario_snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    material = "cairn-scenario-snapshot-v1\n" + canonical_json(dict(snapshot))
    return "scenario-snapshot:" + sha256(material.encode("utf-8")).hexdigest()


def target_scope_digest(targets: Iterable[Mapping[str, Any]]) -> tuple[str, list[str]]:
    snapshot = [
        {
            "id": str(target["id"]),
            "type": str(target["type"]),
            "value": str(target["value"]),
            "status": str(target["status"]),
        }
        for target in targets
    ]
    snapshot.sort(key=lambda target: target["id"])
    material = "default-copy-target-scope-v1\n" + canonical_json(snapshot)
    digest = TARGET_SCOPE_PREFIX + sha256(material.encode("utf-8")).hexdigest()
    target_ids = sorted(
        target["id"]
        for target in snapshot
        if target["status"] in {"seed", "approved"}
    )
    return digest, target_ids


def authorization_window_seconds(snapshot: Mapping[str, Any]) -> int:
    features = snapshot.get("features")
    if not isinstance(features, Mapping):
        features = {}
    limit = str(features.get("exploration_limit") or "balanced")
    return EXPLORATION_WINDOW_SECONDS.get(
        limit,
        EXPLORATION_WINDOW_SECONDS["balanced"],
    )


def build_scope_descriptor(
    *,
    project_id: str,
    tenant_id: str,
    scenario_key: str,
    scenario_snapshot: Mapping[str, Any],
    target_digest: str,
    target_ids: list[str],
    assessment_protocol: str,
    assessment_contract_version: int,
    explore_budget: int | None,
    verification_reserve: int,
    window_seconds: int,
) -> dict[str, Any]:
    return {
        "version": AUTHORIZATION_SCOPE_VERSION,
        "project_id": project_id,
        "tenant_id": tenant_id,
        "scenario_key": scenario_key,
        "scenario_snapshot_digest": scenario_snapshot_digest(scenario_snapshot),
        "target_scope_digest": target_digest,
        "target_ids": sorted(target_ids),
        "assessment_protocol": assessment_protocol,
        "assessment_contract_version": assessment_contract_version,
        "explore_budget": explore_budget,
        "verification_reserve": verification_reserve,
        "authorization_window_seconds": window_seconds,
    }


def authorization_scope_digest(descriptor: Mapping[str, Any]) -> str:
    material = "cairn-project-authorization-scope-v1\n" + canonical_json(
        dict(descriptor)
    )
    return AUTHORIZATION_SCOPE_PREFIX + sha256(material.encode("utf-8")).hexdigest()


def target_scope_v3(
    targets: Iterable[Mapping[str, Any]],
) -> tuple[str, list[str]]:
    """绑定可执行集合及目标身份，不绑定 seed/approved 集合内的展示状态。"""
    snapshot = [
        {
            "id": str(target["id"]),
            "type": str(target["type"]),
            "value": str(target["value"]),
        }
        for target in targets
        if str(target["status"]) in {"seed", "approved"}
    ]
    snapshot.sort(key=lambda target: target["id"])
    material = "cairn-target-scope-v3\n" + canonical_json(snapshot)
    digest = TARGET_SCOPE_PREFIX + sha256(material.encode("utf-8")).hexdigest()
    return digest, [target["id"] for target in snapshot]


def build_scope_descriptor_v3(
    *,
    project_id: str,
    tenant_id: str,
    target_digest: str,
    target_ids: list[str],
) -> dict[str, Any]:
    """范围格式独立版本化；仍使用授权 v2 的期限与不可变 grant 合同。"""
    return {
        "version": AUTHORIZATION_SCOPE_V3_VERSION,
        "project_id": project_id,
        "tenant_id": tenant_id,
        "target_scope_digest": target_digest,
        "target_ids": sorted(target_ids),
    }


def authorization_scope_digest_v3(descriptor: Mapping[str, Any]) -> str:
    material = "cairn-project-authorization-scope-v3\n" + canonical_json(
        dict(descriptor)
    )
    return AUTHORIZATION_SCOPE_PREFIX + sha256(material.encode("utf-8")).hexdigest()
