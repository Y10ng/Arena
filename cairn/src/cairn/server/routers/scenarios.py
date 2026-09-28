"""场景配置接口，聚合 Prompt 和 Dispatcher 上报能力。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from cairn.server.auth import protect_cookie_request, require_dispatcher_protocol, require_super_admin
from cairn.server.db import get_conn
from cairn.server.models import Scenario, ScenarioCatalogEntry, ScenarioOptions, ScenarioValidationResult
from cairn.server.services import (
    dispatcher_declared_inventory,
    latest_dispatcher_container_images,
    list_scenarios,
    replace_scenarios,
    scenario_allowed_caps_with_source,
    scenario_feature_schema,
    scenario_project_counts,
    validate_scenario_table,
    visible_exploration_prompt_groups,
)

router = APIRouter(tags=["scenarios"])


@router.get(
    "/scenario-catalog",
    response_model=list[ScenarioCatalogEntry],
    dependencies=[Depends(protect_cookie_request)],
)
def get_scenario_catalog():
    with get_conn() as conn:
        return [
            ScenarioCatalogEntry(
                key=scenario.key,
                label=scenario.label,
                is_default=scenario.is_default,
                origin_template=scenario.origin_template,
                goal_template=scenario.goal_template,
            )
            for scenario in list_scenarios(conn)
        ]


@router.get(
    "/scenarios",
    response_model=list[Scenario],
    dependencies=[Depends(require_dispatcher_protocol)],
)
def get_scenarios():
    with get_conn() as conn:
        return list_scenarios(conn)


@router.put(
    "/scenarios",
    response_model=list[Scenario],
    dependencies=[Depends(require_super_admin)],
)
def put_scenarios(body: list[Scenario]):
    with get_conn() as conn:
        # replace_scenarios 会统一做默认场景、引用、cap/image/worker 等保存期校验。
        return replace_scenarios(conn, body)


@router.post(
    "/scenarios/validate",
    response_model=ScenarioValidationResult,
    dependencies=[Depends(require_super_admin)],
)
def validate_scenarios(body: list[Scenario]):
    with get_conn() as conn:
        normalized = validate_scenario_table(conn, body)
        return ScenarioValidationResult(
            valid=True,
            scenario_count=len(normalized),
            project_counts=scenario_project_counts(conn),
        )


@router.get(
    "/scenarios/options",
    response_model=ScenarioOptions,
    dependencies=[Depends(require_super_admin)],
)
def get_scenario_options():
    with get_conn() as conn:
        # 下拉选项仅提供可新选的资源；历史引用由场景详情保留。
        inventory = dispatcher_declared_inventory(conn)
        profiles = _scenario_profile_options(inventory)
        prompt_groups = set(visible_exploration_prompt_groups())
        caps, cap_source = scenario_allowed_caps_with_source(conn)
        images = _scenario_inventory_images(inventory) or sorted(latest_dispatcher_container_images(conn))

        return ScenarioOptions(
            profiles=profiles,
            prompt_groups=sorted(prompt_groups),
            knowledge=[],
            images=images,
            cap_add=sorted(caps),
            cap_add_source=cap_source,
            features=scenario_feature_schema(),
            project_counts=scenario_project_counts(conn),
        )


def _scenario_profile_options(inventory: dict[str, object]) -> list[dict[str, object]]:
    profiles = inventory.get("execution_profiles")
    if not isinstance(profiles, list):
        return []
    image_by_ref = _scenario_inventory_image_map(inventory)
    items: list[dict[str, object]] = []
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        profile_id = str(profile.get("id") or "").strip()
        if not profile_id:
            continue
        image_ref = str(profile.get("image_ref") or "").strip()
        items.append(
            {
                "id": profile_id,
                "label": str(profile.get("label") or profile_id),
                "image_ref": image_ref,
                "image": image_by_ref.get(image_ref, ""),
                "network_policy": str(profile.get("network_policy") or ""),
                "cap_add": profile.get("cap_add") if isinstance(profile.get("cap_add"), list) else [],
                "isolation": str(profile.get("isolation") or ""),
                "completed_action": str(profile.get("completed_action") or ""),
            }
        )
    return sorted(items, key=lambda item: str(item["id"]))


def _scenario_inventory_images(inventory: dict[str, object]) -> list[str]:
    return sorted({image for image in _scenario_inventory_image_map(inventory).values() if image})


def _scenario_inventory_image_map(inventory: dict[str, object]) -> dict[str, str]:
    images = inventory.get("images")
    if not isinstance(images, list):
        return {}
    result: dict[str, str] = {}
    for item in images:
        if isinstance(item, str):
            text = item.strip()
            if text:
                result[text] = text
            continue
        if not isinstance(item, dict):
            continue
        image_id = str(item.get("id") or item.get("ref") or "").strip()
        image = str(item.get("image") or item.get("value") or "").strip()
        if image_id and image:
            result[image_id] = image
    return result
