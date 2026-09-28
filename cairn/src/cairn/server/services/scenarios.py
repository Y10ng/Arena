"""场景配置服务。"""

from __future__ import annotations

from .common import *

from .infra import (
    dispatcher_declared_inventory,
    dispatcher_inventory_caps_with_source,
    latest_dispatcher_container_images as dispatcher_report_container_images,
    latest_dispatcher_worker_names as dispatcher_report_worker_names,
    latest_dispatcher_worker_pool as dispatcher_report_worker_pool,
)


def _scenario_keys(conn: sqlite3.Connection) -> set[str]:
    return {row["key"] for row in conn.execute("SELECT key FROM scenarios").fetchall()}


def scenario_from_row(row: sqlite3.Row) -> Scenario:
    return Scenario(
        key=row["key"],
        label=row["label"],
        is_default=bool(row["is_default"]),
        profile_id=str(_row_value(row, "profile_id") or ""),
        image=row["image"],
        prompt_group=row["prompt_group"],
        knowledge=row["knowledge"] or None,
        origin_template=_row_value(row, "origin_template") or None,
        goal_template=_row_value(row, "goal_template") or None,
        cap_add=_loads_json_list(row["cap_add_json"]),
        features=_loads_json(row["features_json"]),
    )


def list_scenarios(conn: sqlite3.Connection) -> list[Scenario]:
    rows = conn.execute("SELECT * FROM scenarios ORDER BY is_default DESC, sort_order, key").fetchall()
    return [scenario_from_row(row) for row in rows]


def get_scenario_or_404(conn: sqlite3.Connection, key: str) -> Scenario:
    row = conn.execute("SELECT * FROM scenarios WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise HTTPException(404, f"Scenario {key} not found")
    return scenario_from_row(row)


def get_default_scenario_key(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT key FROM scenarios WHERE is_default = 1 ORDER BY sort_order, key LIMIT 1"
    ).fetchone()
    if row is not None:
        return row["key"]
    row = conn.execute("SELECT key FROM scenarios ORDER BY sort_order, key LIMIT 1").fetchone()
    if row is not None:
        # 历史库可能没有默认场景；发现时选择第一条并立即修正表状态。
        conn.execute("UPDATE scenarios SET is_default = CASE WHEN key = ? THEN 1 ELSE 0 END", (row["key"],))
        return row["key"]
    raise HTTPException(500, "No scenarios configured")


def resolve_scenario_key(conn: sqlite3.Connection, scenario_key: str | None) -> str:
    key = scenario_key.strip() if isinstance(scenario_key, str) else ""
    if not key:
        return get_default_scenario_key(conn)
    get_scenario_or_404(conn, key)
    return key


def replace_scenarios(conn: sqlite3.Connection, scenarios: list[Scenario]) -> list[Scenario]:
    existing_templates = {
        scenario.key: (scenario.origin_template, scenario.goal_template)
        for scenario in list_scenarios(conn)
    }
    merged: list[Scenario] = []
    for scenario in scenarios:
        previous = existing_templates.get(scenario.key)
        if previous is None:
            merged.append(scenario)
            continue
        updates: dict[str, str | None] = {}
        # 兼容旧管理端：字段未随请求提交时保留原模板，显式 null 仍可清空。
        if "origin_template" not in scenario.model_fields_set:
            updates["origin_template"] = previous[0]
        if "goal_template" not in scenario.model_fields_set:
            updates["goal_template"] = previous[1]
        merged.append(scenario.model_copy(update=updates) if updates else scenario)

    normalized = normalize_scenario_list(merged)
    validate_scenario_values(conn, normalized)
    new_keys = {scenario.key for scenario in normalized}
    blocked = scenario_delete_blockers(conn, new_keys)
    if blocked:
        raise HTTPException(409, "Cannot delete scenarios used by projects: " + ", ".join(blocked))

    # 保存场景采用“整表替换”语义，但先阻止删除已被项目引用的 key。
    conn.execute("DELETE FROM scenarios WHERE key NOT IN ({})".format(
        ", ".join("?" for _ in normalized)
    ), tuple(scenario.key for scenario in normalized))
    for sort_order, scenario in enumerate(normalized):
        conn.execute(
            """
            INSERT INTO scenarios
                (key, label, is_default, profile_id, worker_ids_json,
                 image, prompt_group, knowledge, origin_template, goal_template,
                 cap_add_json, features_json, enabled_workers_json, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                label = excluded.label,
                is_default = excluded.is_default,
                profile_id = excluded.profile_id,
                worker_ids_json = excluded.worker_ids_json,
                image = excluded.image,
                prompt_group = excluded.prompt_group,
                knowledge = excluded.knowledge,
                origin_template = excluded.origin_template,
                goal_template = excluded.goal_template,
                cap_add_json = excluded.cap_add_json,
                features_json = excluded.features_json,
                enabled_workers_json = excluded.enabled_workers_json,
                sort_order = excluded.sort_order
            """,
            (
                scenario.key,
                scenario.label,
                1 if scenario.is_default else 0,
                scenario.profile_id,
                dump_json_list([]),
                scenario.image,
                scenario.prompt_group,
                None,
                scenario.origin_template or None,
                scenario.goal_template or None,
                dump_json_list(scenario.cap_add),
                dump_json(scenario.features),
                dump_json_list([]),
                sort_order,
            ),
        )
    return list_scenarios(conn)


def normalize_scenario_list(scenarios: list[Scenario]) -> list[Scenario]:
    if not scenarios:
        raise HTTPException(422, "At least one scenario is required")
    seen: set[str] = set()
    normalized: list[Scenario] = []
    for scenario in scenarios:
        if scenario.key in seen:
            raise HTTPException(422, f"Duplicate scenario key: {scenario.key}")
        seen.add(scenario.key)
        # 资料库在项目创建时单独冻结；旧 payload 的 knowledge 仅接受但不再保存。
        normalized.append(scenario.model_copy(update={"knowledge": None}))
    first_default = next((index for index, scenario in enumerate(normalized) if scenario.is_default), 0)
    # 多个 is_default 时只保留第一个；没有默认值时把第一项设为默认。
    return [
        scenario.model_copy(update={"is_default": index == first_default})
        for index, scenario in enumerate(normalized)
    ]


def validate_scenario_table(conn: sqlite3.Connection, scenarios: list[Scenario]) -> list[Scenario]:
    normalized = normalize_scenario_list(scenarios)
    validate_scenario_values(conn, normalized)
    blocked = scenario_delete_blockers(conn, {scenario.key for scenario in normalized})
    if blocked:
        raise HTTPException(409, "Cannot delete scenarios used by projects: " + ", ".join(blocked))
    return normalized


def validate_scenario_values(conn: sqlite3.Connection, scenarios: list[Scenario]) -> None:
    # Prompt 引用在保存期按角色校验，避免 Dispatcher 运行时才发现协议资源缺失。
    prompt_infos = {info.key: info for info in list_prompt_group_infos()}
    missing_prompt_groups: set[str] = set()
    unsupported_prompt_groups: set[str] = set()
    invalid_prompt_groups: set[str] = set()
    for scenario in scenarios:
        prompt_group = scenario.prompt_group
        info = prompt_infos.get(prompt_group)
        if info is None:
            missing_prompt_groups.add(prompt_group)
            continue
        if info.visibility != PROMPT_VISIBILITY_VISIBLE:
            unsupported_prompt_groups.add(prompt_group)
            continue
        if PROMPT_CAPABILITY_EXPLORATION not in info.capabilities:
            unsupported_prompt_groups.add(prompt_group)
            continue
        try:
            validate_exploration_prompt_resources(prompt_group)
        except ValueError:
            invalid_prompt_groups.add(prompt_group)
    if missing_prompt_groups:
        raise HTTPException(
            422,
            "Unknown prompt groups: " + ", ".join(sorted(missing_prompt_groups)),
        )
    if unsupported_prompt_groups:
        raise HTTPException(
            422,
            "Prompt groups do not support exploration: "
            + ", ".join(sorted(unsupported_prompt_groups)),
        )
    if invalid_prompt_groups:
        raise HTTPException(
            422,
            "Invalid exploration prompt groups: "
            + ", ".join(sorted(invalid_prompt_groups)),
        )
    inventory = dispatcher_declared_inventory(conn)
    inventory_profiles = _inventory_profile_ids(inventory)
    allowed_caps = scenario_allowed_caps(conn)
    invalid_caps = sorted({cap for scenario in scenarios for cap in scenario.cap_add} - allowed_caps)
    if invalid_caps and allowed_caps:
        raise HTTPException(422, "Unsupported cap_add values: " + ", ".join(invalid_caps))
    if inventory_profiles:
        missing_profiles = sorted(
            {
                scenario.profile_id
                for scenario in scenarios
                if scenario.profile_id and scenario.profile_id not in inventory_profiles
            }
        )
        if missing_profiles:
            raise HTTPException(422, "Unknown profile_id values: " + ", ".join(missing_profiles))
    allowed_images = dispatcher_report_container_images(conn)
    inventory_images = _inventory_image_values(inventory)
    if inventory_images:
        allowed_images = inventory_images
    if allowed_images:
        invalid_images = sorted(
            {
                image
                for scenario in scenarios
                if (image := scenario.image.strip())
                and image not in allowed_images
            }
        )
        if invalid_images:
            raise HTTPException(422, "Unsupported scenario images: " + ", ".join(invalid_images))
    validate_scenario_features(scenarios)


def latest_dispatcher_worker_names(conn: sqlite3.Connection) -> set[str]:
    return dispatcher_report_worker_names(conn)


def latest_dispatcher_worker_pool(conn: sqlite3.Connection) -> list[dict[str, object]]:
    return dispatcher_report_worker_pool(conn)


def latest_dispatcher_container_images(conn: sqlite3.Connection) -> set[str]:
    return dispatcher_report_container_images(conn)


def scenario_allowed_caps(conn: sqlite3.Connection) -> set[str]:
    caps, _source = scenario_allowed_caps_with_source(conn)
    return caps


def scenario_allowed_caps_with_source(conn: sqlite3.Connection) -> tuple[set[str], str]:
    return dispatcher_inventory_caps_with_source(conn)


def scenario_feature_schema() -> list[ScenarioFeatureSchemaItem]:
    return [ScenarioFeatureSchemaItem.model_validate(item) for item in SCENARIO_FEATURE_SCHEMA_DEFS]


def validate_scenario_features(scenarios: list[Scenario]) -> None:
    schema = {item.key: item for item in scenario_feature_schema()}
    prompt_infos = {info.key: info for info in list_prompt_group_infos()}
    for scenario in scenarios:
        unknown_keys = sorted(set(scenario.features) - set(schema))
        if unknown_keys:
            raise HTTPException(
                422,
                f"Unsupported features for scenario {scenario.key}: " + ", ".join(unknown_keys),
            )
        for key, value in scenario.features.items():
            item = schema[key]
            if item.value_type == "boolean":
                if not isinstance(value, bool):
                    raise HTTPException(
                        422,
                        f"Feature {key} for scenario {scenario.key} must be a boolean",
                    )
                continue
            if item.value_type == "enum":
                if not isinstance(value, str) or value not in item.options:
                    raise HTTPException(
                        422,
                        f"Feature {key} for scenario {scenario.key} must be one of: "
                        + ", ".join(item.options),
                    )
                continue
            if item.value_type == "string" and not isinstance(value, str):
                raise HTTPException(
                    422,
                    f"Feature {key} for scenario {scenario.key} must be a string",
                )
    # 执行协议在项目创建时冻结；场景只选择报告模板，不保存协议版本。
    for scenario in scenarios:
        report_prompt_group = scenario.features.get("report_prompt_group") or DEFAULT_PROMPT_GROUP
        report_prompt_group = report_prompt_group.strip()
        info = prompt_infos.get(report_prompt_group)
        if info is None:
            raise HTTPException(422, f"Unknown report prompt group: {report_prompt_group}")
        if PROMPT_CAPABILITY_DURABLE_REPORT not in info.capabilities:
            raise HTTPException(
                422,
                f"Scenario {scenario.key}: report prompt group '{report_prompt_group}' "
                "does not support durable reports",
            )
        try:
            validate_report_prompt_resources(report_prompt_group)
        except ValueError as exc:
            raise HTTPException(
                422,
                f"Scenario {scenario.key}: invalid report prompt group '{report_prompt_group}'",
            ) from exc


def _latest_dispatcher_json_lists(
    conn: sqlite3.Connection,
    column: str,
    *,
    skip_null: bool,
) -> list[list[object]]:
    rows = conn.execute(
        f"""
        SELECT {column}
        FROM dispatcher_status
        ORDER BY COALESCE(last_loop_finished_at, updated_at) DESC, updated_at DESC, id
        """
    ).fetchall()
    parsed: list[list[object]] = []
    for row in rows:
        raw = row[column]
        if raw is None and skip_null:
            continue
        try:
            data = loads(raw or "[]")
        except (TypeError, ValueError):
            continue
        if isinstance(data, list):
            parsed.append(data)
    return parsed


def _clean_string_items(data: list[object]) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for item in data:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
    return items


def _row_value(row: sqlite3.Row, key: str) -> object | None:
    return row[key] if key in row.keys() else None


def _inventory_profile_ids(inventory: dict[str, object]) -> set[str]:
    values: set[str] = set()
    profiles = inventory.get("execution_profiles")
    if not isinstance(profiles, list):
        return values
    for item in profiles:
        if not isinstance(item, dict):
            continue
        text = str(item.get("id") or "").strip()
        if text:
            values.add(text)
    return values


def _inventory_image_values(inventory: dict[str, object]) -> set[str]:
    values: set[str] = set()
    images = inventory.get("images")
    if not isinstance(images, list):
        return values
    for item in images:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = str(item.get("image") or item.get("value") or "").strip()
        else:
            text = ""
        if text:
            values.add(text)
    return values


def scenario_project_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT scenario_key, COUNT(*) AS project_count
        FROM projects
        GROUP BY scenario_key
        """
    ).fetchall()
    return {row["scenario_key"]: int(row["project_count"]) for row in rows}


def scenario_delete_blockers(conn: sqlite3.Connection, new_keys: set[str]) -> list[str]:
    return [
        f"{scenario_key}({project_count})"
        for scenario_key, project_count in scenario_project_counts(conn).items()
        if scenario_key not in new_keys
    ]
