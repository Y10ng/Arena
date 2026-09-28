"""兼容配置清理服务。"""

from __future__ import annotations

from .common import *

from .knowledge import available_knowledge_set_keys, delete_knowledge_set
from .prompts import delete_prompt_group
from .scenarios import _scenario_keys, scenario_delete_blockers

def cleanup_legacy_presets(conn: sqlite3.Connection) -> dict[str, list[str]]:
    # legacy preset 清理必须先检查项目引用，防止删除后已有项目无法解析场景。
    blocked = scenario_delete_blockers(conn, set(_scenario_keys(conn)) - LEGACY_SCENARIO_KEYS)
    legacy_blocked = [item for item in blocked if item.split("(", 1)[0] in LEGACY_SCENARIO_KEYS]
    if legacy_blocked:
        raise HTTPException(409, "Cannot delete legacy scenarios used by projects: " + ", ".join(legacy_blocked))

    removed_scenarios = [
        row["key"]
        for row in conn.execute(
            "SELECT key FROM scenarios WHERE key IN ({}) ORDER BY key".format(
                ", ".join("?" for _ in LEGACY_SCENARIO_KEYS)
            ),
            tuple(sorted(LEGACY_SCENARIO_KEYS)),
        ).fetchall()
    ]
    if removed_scenarios:
        conn.execute(
            "DELETE FROM scenarios WHERE key IN ({})".format(
                ", ".join("?" for _ in removed_scenarios)
            ),
            tuple(removed_scenarios),
        )

    removed_knowledge: list[str] = []
    for key in sorted(LEGACY_KNOWLEDGE_KEYS):
        if key in available_knowledge_set_keys():
            try:
                delete_knowledge_set(conn, key)
            except HTTPException as exc:
                if exc.status_code == 409:
                    # 被场景引用的知识集保留；清理命令只移除确定无引用的兼容资源。
                    continue
                raise
            removed_knowledge.append(key)

    removed_prompts: list[str] = []
    for key in sorted(LEGACY_PROMPT_GROUP_KEYS):
        try:
            delete_prompt_group(conn, key)
        except HTTPException as exc:
            if exc.status_code in {404, 409}:
                # 已不存在或仍被引用都视为无需清理，避免维护命令破坏运行配置。
                continue
            raise
        removed_prompts.append(key)

    return {
        "scenarios": removed_scenarios,
        "knowledge": removed_knowledge,
        "prompt_groups": removed_prompts,
    }
