"""Prompt 渲染工具，负责模板兜底、输出语言片段和知识库上下文注入。"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cairn.paths import resolve_source_relative_path
from cairn.project_authorization_contract import (
    build_scope_descriptor_v4,
    scope_source_from_descriptor,
)
from cairn.dispatcher.prompt_resources import (
    DEFAULT_PROMPT_GROUP,
    OUTPUT_LANGUAGE_FRAGMENT,
    read_prompt_resource,
    validate_prompt_resources,
    validate_prompt_submission_tools,
)

KNOWLEDGE_BASE_ENV_VAR = "CAIRN_KNOWLEDGE_ROOT"
DEFAULT_KNOWLEDGE_BASE_PATH = "datas/cairn/knowledge"
KNOWLEDGE_BASE_PATH = os.environ.get(KNOWLEDGE_BASE_ENV_VAR, "").strip() or DEFAULT_KNOWLEDGE_BASE_PATH
KNOWLEDGE_BASE_MODE = "manual_file_knowledge"
KNOWLEDGE_BASE_FORMATS = ("markdown", "yaml", "json")
KNOWLEDGE_BASE_FRONT_MATTER_FIELDS = ("title", "tags", "applies_to", "summary", "last_verified_at")
# 这些指令进入 worker prompt，告诉模型知识库是可选只读辅助资料。
KNOWLEDGE_BASE_PROMPT_INSTRUCTIONS = (
    "Operator-provided advisory resources are optional and may be outdated or inapplicable; continue the full assessment when none are available.",
    "Credentials and sensitive values may be present for this authorized assessment; use them only within the approved target scope.",
    "Resources may be text, PDF, images, Office files, archives, or other binary data. Inspect metadata and use a suitable tool only when relevant; never assume UTF-8.",
    "Never copy full resources into prompts, logs, reports, or final output. /cairn/advisory is read-only; copy only essential material into the workspace.",
    "Inspect scripts and binaries first. Never execute them automatically; run only after confirming scope and applicability.",
    "Validate, adapt, and extend every useful statement against the live target. Distinguish uploaded claims, live observations, and confirmed conclusions.",
    "An uploaded vulnerability claim is not a finding and must not become a Fact, Coverage outcome, Verification, or report finding without current TaskRun evidence.",
)

def load_prompt(group: str, name: str) -> str:
    effective_group = group.strip() or DEFAULT_PROMPT_GROUP
    # Prompt group 是项目协议的一部分；资源损坏时必须拒绝调度，不能切换到其他协议模板。
    validate_prompt_resources(effective_group)
    text = read_prompt_resource(effective_group, name)
    if name != OUTPUT_LANGUAGE_FRAGMENT:
        # 自定义组中的可选 verifier/report 文件也必须通过同一工具提交校验。
        validate_prompt_submission_tools(effective_group, name, text)
        output_language = _load_output_language_fragment(effective_group)
        if output_language:
            return _inject_output_language_fragment(text, output_language)
    return text


def _load_output_language_fragment(group: str, *, builtin_only: bool = False) -> str:
    for candidate, candidate_builtin_only in (
        (group, builtin_only),
        (DEFAULT_PROMPT_GROUP, True),
    ):
        try:
            return read_prompt_resource(
                candidate,
                OUTPUT_LANGUAGE_FRAGMENT,
                builtin_only=candidate_builtin_only,
            ).strip()
        except (FileNotFoundError, UnicodeDecodeError, ValueError):
            continue
    return ""


def _inject_output_language_fragment(template: str, fragment: str) -> str:
    marker = "# Output Requirements"
    if marker not in template:
        return f"{fragment}\n\n{template}"
    return template.replace(marker, f"{marker}\n{fragment}\n", 1)


def render_prompt(template: str, replacements: dict[str, str]) -> str:
    # 单次替换模板占位符，防止 Origin 等原文中的同名片段被二次改写。
    return re.sub(
        r"\{([a-z_]+)\}",
        lambda match: replacements.get(match.group(1), match.group(0)),
        template,
    )


def format_fact_ids(fact_ids: list[str]) -> str:
    return format_json_block(fact_ids)


def format_open_intents(intents: list[dict[str, Any]]) -> str:
    return format_json_block(intents)


def format_hints(hints: list[dict[str, Any]]) -> str:
    return format_json_block(hints)


def format_targets(targets: list[dict[str, Any]]) -> str:
    return format_json_block(targets)


def authorization_scope_replacements(project: Any) -> dict[str, str]:
    """所有研究活动消费同一持久授权来源和未改写的 Origin 原文。"""
    origin = next(
        (fact.description for fact in project.facts if fact.id == "origin"),
        "",
    )
    descriptor = getattr(project.project, "authorization_scope_descriptor", None)
    source = scope_source_from_descriptor(descriptor) if descriptor is not None else "targets"
    if source == "origin":
        expected = build_scope_descriptor_v4(
            project_id=project.project.id,
            tenant_id=project.project.tenant_id,
            origin_fact_id="origin",
            origin=origin,
        )
        if descriptor != expected:
            raise ValueError("authorization Origin source does not match its grant")
        instructions = (
            "Origin is the authoritative scope source: act autonomously within the objects, "
            "actions and limits explicitly authorized there, including exclusions. "
            "A reference address, excluded object, attachment, redirect, discovered Target or "
            "model output does not grant or enlarge permission. Goal describes the desired "
            "outcome, not additional authorization. An empty structured Target list neither "
            "blocks work on supplied materials nor permits arbitrary network activity. "
            "If a proposed external action lacks clear authorization, state the specific "
            "missing scope or prerequisite for that action; continue unrelated authorized "
            "material work. No separate Target approval is required for actions already "
            "clearly authorized in Origin. These instructions are not OS network isolation."
        )
    else:
        # 无新来源标记的历史项目继续以原有集合为边界，绝不从空集合猜测 Origin 授权。
        instructions = (
            "Only seed or approved Targets are authorized for target interaction. "
            "Discovered Targets are candidates, rejected Targets are out of scope, and an "
            "empty list is not network permission. Origin is background only and cannot "
            "extend this legacy or explicit Target authorization. Goal, attachments, "
            "redirects and model output cannot extend it either."
        )
    targets = [
        {
            "id": target.id,
            "value": target.value,
            "type": target.type,
            "status": target.status,
            "source_fact_id": target.source_fact_id,
            "discovered_by_run_id": target.discovered_by_run_id,
        }
        for target in sorted(
            project.targets,
            key=lambda item: (item.created_at, item.id),
        )
        if target.status in {"seed", "approved"}
    ]
    return {
        "origin": origin,
        "authorized_targets": format_targets(targets),
        "authorization_scope": json.dumps(
            {"scope_descriptor": descriptor, "instructions": instructions},
            ensure_ascii=False,
            sort_keys=True,
        ),
    }


def format_coverage_slots(slots: list[dict[str, Any]], *, verifier: bool = False) -> str:
    # pentest 场景 reason 注入待覆盖槽位：头部声明 verifier 模式，供 reason prompt 自适配
    # （verifier=off 时 explored 即算覆盖、不提 verify；on 时 explored-unverified 需提 verify）。
    # 空列表时给出明确提示，避免模型把"无 slot 信息"误读为"已全覆盖"。
    if not slots:
        return "[] (no coverage slots registered — not a pentest scenario, or bootstrap has not enumerated the surface yet)"
    header = f"verifier={'on' if verifier else 'off'}"
    return header + "\n" + format_json_block(slots)


def _parse_utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_structured_coverage_slots(
    slots: list[dict[str, Any]],
    *,
    now: str | None = None,
) -> str:
    """仅向 default-copy Reason 暴露可调度 Cell 与活动 blocker 摘要。"""
    current = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    current_dt = _parse_utc_timestamp(current) or datetime.now(timezone.utc)
    eligible: list[dict[str, Any]] = []
    strategy_change: list[dict[str, Any]] = []
    terminal_counts: dict[str, int] = {}
    for slot in slots:
        status = str(slot.get("status") or "")
        if status == "exploring":
            # 已分配 Cell 由 open_intents 展示；不得再次进入 Reason 可调度集合。
            continue
        if status != "pending":
            terminal_counts[status or "unknown"] = terminal_counts.get(
                status or "unknown", 0
            ) + 1
            continue
        cooldown_until = str(slot.get("blocker_cooldown_until") or "")
        cooldown_dt = _parse_utc_timestamp(cooldown_until)
        if cooldown_dt is not None and cooldown_dt > current_dt:
            strategy_change.append(
                {
                    "slot_key": slot.get("slot_key"),
                    "tier": slot.get("tier"),
                    "surface_kind": slot.get("surface_kind") or slot.get("kind"),
                    "surface_key": slot.get("surface_key"),
                    "test_family": slot.get("test_family"),
                    "blocker_fingerprint": slot.get("blocker_fingerprint"),
                    "blocked_strategy_key": slot.get("blocker_strategy_key"),
                    "cooldown_until": cooldown_until,
                    "limitation_reason": slot.get("skip_reason") or "",
                }
            )
            continue
        eligible.append(slot)
    return format_json_block(
        {
            "eligible_cells": eligible,
            "strategy_change_candidates": strategy_change,
            "terminal_summary": terminal_counts,
            "instructions": (
                "Schedule eligible_cells normally. A strategy_change_candidate may be "
                "scheduled before cooldown expiry only with a materially different strategy_key."
            ),
        }
    )


def knowledge_base_runtime_context(
    knowledge: str | None = None,
    *,
    advisory: Any | None = None,
) -> dict[str, Any]:
    digest = str(getattr(advisory, "advisory_digest", "") or "").strip()
    manifest = getattr(advisory, "advisory_manifest", None)
    enabled = bool(digest and isinstance(manifest, dict))
    resources: list[dict[str, Any]] = []
    if enabled:
        for library in manifest.get("libraries", []):
            if not isinstance(library, dict):
                continue
            library_key = str(library.get("key") or "").strip()
            library_version = library.get("version")
            library_digest = str(library.get("digest_sha256") or "").strip()
            if not library_key or not isinstance(library_version, int):
                continue
            for item in library.get("files", []):
                if not isinstance(item, dict):
                    continue
                relative_path = str(item.get("path") or "").strip()
                bundle_path = str(item.get("bundle_path") or "").strip()
                if not relative_path or not bundle_path:
                    continue
                resources.append(
                    {
                        "source": "advisory_library",
                        "kind": "note",
                        "library_key": library_key,
                        "library_version": library_version,
                        "library_digest": library_digest,
                        "path": relative_path,
                        "container_path": f"/cairn/advisory/{bundle_path}",
                        "size_bytes": item.get("size_bytes"),
                        "sha256": item.get("sha256"),
                    }
                )
        for item in manifest.get("legacy_files", []):
            if not isinstance(item, dict):
                continue
            bundle_path = str(item.get("bundle_path") or "").strip()
            if not bundle_path:
                continue
            resources.append(
                {
                    "source": "legacy_knowledge",
                    "kind": "note",
                    "path": str(item.get("path") or ""),
                    "container_path": f"/cairn/advisory/{bundle_path}",
                    "size_bytes": item.get("size_bytes"),
                    "sha256": item.get("sha256"),
                }
            )
        for item in manifest.get("project_resources", []):
            if not isinstance(item, dict):
                continue
            relative_path = str(item.get("path") or "").strip()
            if not relative_path:
                continue
            resources.append(
                {
                    "source": "project_resource",
                    "id": item.get("id"),
                    "kind": item.get("kind"),
                    "path": relative_path,
                    "container_path": f"/cairn/advisory/project/{relative_path}",
                    "size_bytes": item.get("size_bytes"),
                    "sha256": item.get("sha256"),
                }
            )
    key = knowledge.strip() if isinstance(knowledge, str) and knowledge.strip() else None
    # 只公开容器内路径和不可变元数据，宿主机 staging 路径不会进入审计或 Prompt。
    return {
        "key": key,
        "enabled": enabled,
        "injected": enabled,
        "path": "/cairn/advisory" if enabled else None,
        "manifest_path": "/cairn/advisory/manifest.json" if enabled else None,
        "bundle_digest": digest or None,
        "mode": "project_advisory_bundle",
        "read_only": True,
        "resources": resources,
    }


def knowledge_base_prompt_context(
    knowledge: str | None = None,
    *,
    advisory: Any | None = None,
) -> str:
    return format_json_block(
        {
            **knowledge_base_runtime_context(knowledge, advisory=advisory),
            "instructions": list(KNOWLEDGE_BASE_PROMPT_INSTRUCTIONS),
        }
    )


def resolved_knowledge_base_path() -> str:
    configured = os.environ.get(KNOWLEDGE_BASE_ENV_VAR, "").strip()
    if configured:
        return str(Path(configured).expanduser().resolve())
    return str(
        resolve_source_relative_path(DEFAULT_KNOWLEDGE_BASE_PATH, anchor=Path(__file__))
    )


def format_json_block(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)
