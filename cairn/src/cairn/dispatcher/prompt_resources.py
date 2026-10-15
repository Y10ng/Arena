"""Prompt 模板资源管理，合并内置模板与用户受管模板组。"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
import os
from pathlib import Path
import re

from cairn.paths import resolve_source_relative_path

PROMPTS_ROOT_ENV_VAR = "CAIRN_PROMPTS_ROOT"
DEFAULT_PROMPTS_ROOT_PATH = "datas/cairn/prompts"
BUILTIN_PROMPTS_PACKAGE = "cairn.dispatcher.prompts"
EXTERNAL_VERIFICATION_PROMPT_GROUP = "external-verification"
DEFAULT_PROMPT_GROUP = "goal-driven-v1"
RETIRED_PROMPT_GROUP_KEYS = frozenset({
    "default", "default-v2", "default-copy", "default-copy-v2", "pentest",
    "logic-vuln", "mock", "ai-agent-security-v1", "ai-agent-security-v2",
    "default-report", "default-durable-report-v1",
})
PROMPT_CAPABILITY_EXPLORATION = "exploration"
PROMPT_CAPABILITY_DURABLE_REPORT = "durable-report"
PROMPT_VISIBILITY_VISIBLE = "visible"
OUTPUT_LANGUAGE_FRAGMENT = "_output_lang.md"
PROMPT_GROUP_KEY_RE = re.compile(r"^[a-z0-9_-]+$")
# 每个 Prompt 文件必须包含这些占位符，避免运行时渲染缺关键上下文。
DEFAULT_PROMPT_REQUIRED_TOKENS: dict[str, tuple[str, ...]] = {
    "reason.md": (
        "{authorization_scope}",
        "{origin}",
        "{goal}",
        "{authorized_targets}",
        "{decision_context}",
        "{decision_input_digest}",
        "{assessment_execution}",
        "{graph_yaml}",
        "{fact_ids}",
        "{open_intents}",
        "{max_intents}",
        "{surface_discovery_contract}",
        "{knowledge_base}",
    ),
    "explore.md": (
        "{authorization_scope}",
        "{origin}",
        "{goal}",
        "{authorized_targets}",
        "{graph_yaml}",
        "{intent_id}",
        "{intent_description}",
        "{surface_discovery_contract}",
        "{knowledge_base}",
    ),
    "explore_conclude.md": (
        "{authorization_scope}",
        "{origin}",
        "{goal}",
        "{authorized_targets}",
        "{graph_yaml}",
        "{intent_id}",
        "{intent_description}",
        "{surface_discovery_contract}",
        "{knowledge_base}",
    ),
    "bootstrap.md": (
        "{authorization_scope}",
        "{origin}",
        "{goal}",
        "{hints}",
        "{targets}",
        "{surface_discovery_contract}",
        "{knowledge_base}",
    ),
    "bootstrap_conclude.md": (
        "{authorization_scope}",
        "{origin}",
        "{goal}",
        "{hints}",
        "{targets}",
        "{surface_discovery_contract}",
        "{knowledge_base}",
    ),
    "verifier.md": (
        "{authorization_scope}",
        "{origin}",
        "{authorized_targets}",
        "{intent_id}",
        "{intent_description}",
        "{target_observation_id}",
        "{verifier_target}",
        "{knowledge_base}",
    ),
    "verifier_conclude.md": (
        "{authorization_scope}",
        "{origin}",
        "{authorized_targets}",
        "{intent_id}",
        "{intent_description}",
        "{target_observation_id}",
        "{verifier_target}",
        "{knowledge_base}",
    ),
    "report.md": (
        "{report_packet_json}",
        "{graph_yaml}",
        "{knowledge_base}",
    ),
}
PROMPT_REQUIRED_FILES = tuple(name for name in DEFAULT_PROMPT_REQUIRED_TOKENS if name != "report.md")
BUILTIN_PROMPT_GROUP_METADATA: dict[str, tuple[frozenset[str], str]] = {
    "goal-driven-v1": (
        frozenset({PROMPT_CAPABILITY_EXPLORATION, PROMPT_CAPABILITY_DURABLE_REPORT}),
        PROMPT_VISIBILITY_VISIBLE,
    ),
}
# 探索与报告能力独立校验；report.md 加入 allowed 以便读取，但不作为探索组必需文件。
# 纯报告组可以只提供 report.md，默认探索组没有该文件也不受影响。
PROMPT_ALLOWED_FILES = set(PROMPT_REQUIRED_FILES) | {
    OUTPUT_LANGUAGE_FRAGMENT,
    "report.md",
    "verifier.md",
    "verifier_conclude.md",
}


@dataclass(frozen=True)
class PromptGroupInfo:
    key: str
    path: str
    builtin: bool
    managed: bool
    file_count: int
    overrides_builtin: bool
    capabilities: tuple[str, ...]
    visibility: str


def runtime_prompts_root_path() -> str:
    configured = os.environ.get(PROMPTS_ROOT_ENV_VAR, "").strip()
    if configured:
        return str(Path(configured).expanduser().resolve())
    return str(
        resolve_source_relative_path(DEFAULT_PROMPTS_ROOT_PATH, anchor=Path(__file__))
    )


def prompts_root() -> Path:
    return Path(runtime_prompts_root_path()).expanduser().resolve()


def ensure_prompts_root() -> Path:
    root = prompts_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def validate_prompt_group_key(key: str) -> str:
    text = key.strip() if isinstance(key, str) else ""
    if not text or not PROMPT_GROUP_KEY_RE.fullmatch(text):
        raise ValueError(
            "Prompt group key must contain only lowercase letters, numbers, underscores, or hyphens"
        )
    if len(text) > 128:
        raise ValueError("Prompt group key must be 128 characters or fewer")
    return text


def validate_prompt_file_name(name: str, *, allow_output_language: bool = True) -> str:
    text = name.strip().replace("\\", "/") if isinstance(name, str) else ""
    allowed = PROMPT_ALLOWED_FILES if allow_output_language else set(PROMPT_REQUIRED_FILES)
    if "/" in text or text not in allowed:
        raise ValueError(
            "Prompt file name must be one of: " + ", ".join(sorted(allowed))
        )
    if Path(text).suffix.lower() != ".md":
        raise ValueError("Prompt file extension must be .md")
    return text


def read_external_verification_instructions() -> str:
    """只装载随包发布的复测指引，不读取受管覆盖或上传资料中的指令。"""
    text = resources.files(BUILTIN_PROMPTS_PACKAGE).joinpath(
        EXTERNAL_VERIFICATION_PROMPT_GROUP, "agent.md",
    ).read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError("复测专用 agent.md 为空")
    return text


def available_builtin_prompt_groups() -> set[str]:
    prompts_dir = resources.files(BUILTIN_PROMPTS_PACKAGE)
    return {
        child.name
        for child in prompts_dir.iterdir()
        if child.is_dir()
        and PROMPT_GROUP_KEY_RE.fullmatch(child.name)
        and child.name not in RETIRED_PROMPT_GROUP_KEYS
        and child.name != EXTERNAL_VERIFICATION_PROMPT_GROUP
    }


def available_managed_prompt_groups() -> set[str]:
    root = ensure_prompts_root()
    return {
        child.name
        for child in root.iterdir()
        if child.is_dir()
        and PROMPT_GROUP_KEY_RE.fullmatch(child.name)
        and child.name not in RETIRED_PROMPT_GROUP_KEYS
    }


def available_prompt_groups() -> set[str]:
    return available_builtin_prompt_groups() | available_managed_prompt_groups()


def list_prompt_group_infos() -> list[PromptGroupInfo]:
    root = ensure_prompts_root()
    builtins = available_builtin_prompt_groups()
    managed = available_managed_prompt_groups()
    infos: list[PromptGroupInfo] = []
    for key in sorted(builtins | managed):
        managed_path = root / key
        if key in managed:
            # 受管组优先，允许同名目录覆盖内置组以便本地调试和配置中心编辑。
            path = str(managed_path)
            file_count = _prompt_file_count(managed_path)
            capabilities = _infer_prompt_group_capabilities(key)
        else:
            path = f"{BUILTIN_PROMPTS_PACKAGE}/{key}"
            file_count = _builtin_prompt_file_count(key)
            capabilities = BUILTIN_PROMPT_GROUP_METADATA.get(
                key,
                (frozenset(), PROMPT_VISIBILITY_VISIBLE),
            )[0]
        visibility = BUILTIN_PROMPT_GROUP_METADATA.get(
            key,
            (frozenset(), PROMPT_VISIBILITY_VISIBLE),
        )[1]
        infos.append(
            PromptGroupInfo(
                key=key,
                path=path,
                builtin=key in builtins,
                managed=key in managed,
                file_count=file_count,
                overrides_builtin=key in builtins and key in managed,
                capabilities=tuple(sorted(capabilities)),
                visibility=visibility,
            )
        )
    return infos


def read_prompt_resource(
    group: str,
    name: str,
    *,
    managed_first: bool = True,
    builtin_only: bool = False,
) -> str:
    group = validate_prompt_group_key(group)
    name = validate_prompt_file_name(name)
    if not builtin_only and managed_first:
        # 默认读取顺序是受管覆盖内置；builtin_only 用于克隆和兜底校验。
        group_path = managed_prompt_group_path(group)
        if group_path.is_dir():
            path = managed_prompt_file_path(group, name, require_exists=True, allow_output_language=True)
            assert path is not None
            return path.read_text(encoding="utf-8")
    try:
        return _builtin_prompt_group(group).joinpath(name).read_text(encoding="utf-8")
    except FileNotFoundError:
        if not builtin_only and not managed_first:
            path = managed_prompt_file_path(group, name, require_exists=True, allow_output_language=True)
            if path is not None:
                return path.read_text(encoding="utf-8")
        raise


def prompt_group_exists(group: str, *, builtin_only: bool = False) -> bool:
    group = validate_prompt_group_key(group)
    if not builtin_only and (ensure_prompts_root() / group).is_dir():
        return True
    return _builtin_prompt_group(group).is_dir()


def managed_prompt_group_path(group: str, *, require_exists: bool = False) -> Path:
    group = validate_prompt_group_key(group)
    root = ensure_prompts_root()
    path = (root / group).resolve()
    if not _path_is_within(path, root):
        raise ValueError("Prompt group path escapes the prompts root")
    if require_exists and (not path.exists() or not path.is_dir()):
        raise FileNotFoundError(group)
    return path


def managed_prompt_file_path(
    group: str,
    name: str,
    *,
    require_exists: bool = False,
    allow_output_language: bool = True,
) -> Path | None:
    group_path = managed_prompt_group_path(group, require_exists=require_exists)
    if not group_path.exists() or not group_path.is_dir():
        if require_exists:
            raise FileNotFoundError(group)
        return None
    name = validate_prompt_file_name(name, allow_output_language=allow_output_language)
    path = (group_path / name).resolve()
    if not _path_is_within(path, group_path):
        # 文件名白名单已很窄，但仍保留 resolve 后路径检查作为防御层。
        raise ValueError("Prompt file path escapes the prompt group")
    if require_exists and (not path.exists() or not path.is_file()):
        raise FileNotFoundError(name)
    return path


def validate_prompt_resources(prompt_group: str, *, builtin_only: bool = False) -> None:
    prompt_group = validate_prompt_group_key(prompt_group)
    if prompt_group in RETIRED_PROMPT_GROUP_KEYS:
        raise ValueError(f"prompt group {prompt_group} is retired and cannot execute")
    if not prompt_group_exists(prompt_group, builtin_only=builtin_only):
        raise ValueError(f"missing prompt group: {prompt_group}")
    required_tokens = required_prompt_tokens_for_group(prompt_group, builtin_only=builtin_only)
    for name, tokens in required_tokens.items():
        # 这里校验的是运行必需模板；输出语言片段是可选资源。
        try:
            content = read_prompt_resource(prompt_group, name, builtin_only=builtin_only)
        except FileNotFoundError as exc:
            raise ValueError(f"prompt group {prompt_group} missing resource: {name}") from exc
        except UnicodeDecodeError as exc:
            raise ValueError(f"prompt group {prompt_group} resource {name} is not valid UTF-8") from exc
        validate_prompt_submission_tools(prompt_group, name, content)
        missing = [token for token in tokens if token not in content]
        if missing:
            raise ValueError(
                f"prompt group {prompt_group} resource {name} missing placeholders: {', '.join(missing)}"
            )


def validate_exploration_prompt_resources(
    prompt_group: str,
    *,
    builtin_only: bool = False,
) -> None:
    """校验探索阶段的 Prompt 文件及其占位符契约。"""
    prompt_group = validate_prompt_group_key(prompt_group)
    if prompt_group in RETIRED_PROMPT_GROUP_KEYS:
        raise ValueError(f"prompt group {prompt_group} is retired and cannot execute")
    if not prompt_group_exists(prompt_group, builtin_only=builtin_only):
        raise ValueError(f"missing prompt group: {prompt_group}")
    required_tokens = exploration_required_prompt_tokens_for_group(
        prompt_group,
        builtin_only=builtin_only,
    )
    for name, tokens in required_tokens.items():
        try:
            content = read_prompt_resource(prompt_group, name, builtin_only=builtin_only)
        except FileNotFoundError as exc:
            raise ValueError(f"prompt group {prompt_group} missing resource: {name}") from exc
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"prompt group {prompt_group} resource {name} is not valid UTF-8"
            ) from exc
        validate_prompt_submission_tools(prompt_group, name, content)
        missing = [token for token in tokens if token not in content]
        if missing:
            raise ValueError(
                f"prompt group {prompt_group} resource {name} missing placeholders: "
                + ", ".join(missing)
            )


def validate_report_prompt_resources(
    prompt_group: str,
    *,
    builtin_only: bool = False,
) -> None:
    """校验 durable 报告 Prompt 使用权威 Report Packet 契约。"""
    prompt_group = validate_prompt_group_key(prompt_group)
    if prompt_group in RETIRED_PROMPT_GROUP_KEYS:
        raise ValueError(f"prompt group {prompt_group} is retired and cannot execute")
    if not prompt_group_exists(prompt_group, builtin_only=builtin_only):
        raise ValueError(f"missing prompt group: {prompt_group}")
    try:
        content = read_prompt_resource(
            prompt_group,
            "report.md",
            builtin_only=builtin_only,
        )
    except FileNotFoundError as exc:
        raise ValueError(f"prompt group {prompt_group} missing resource: report.md") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"prompt group {prompt_group} resource report.md is not valid UTF-8"
        ) from exc
    validate_prompt_submission_tools(prompt_group, "report.md", content)
    if "{report_packet_json}" not in content:
        raise ValueError(
            f"prompt group {prompt_group} resource report.md missing placeholders: "
            "{report_packet_json}"
        )


def validate_prompt_group_contents(contents: dict[str, str], *, prompt_group: str) -> None:
    prompt_group = validate_prompt_group_key(prompt_group)
    if prompt_group in RETIRED_PROMPT_GROUP_KEYS:
        raise ValueError(f"prompt group {prompt_group} is retired and cannot execute")
    required_tokens = required_prompt_tokens_for_contents(contents, prompt_group=prompt_group)
    for name, tokens in required_tokens.items():
        content = contents.get(name)
        if content is None:
            raise ValueError(f"prompt group {prompt_group} missing resource: {name}")
        validate_prompt_submission_tools(prompt_group, name, content)
        missing = [token for token in tokens if token not in content]
        if missing:
            raise ValueError(
                f"prompt group {prompt_group} resource {name} missing placeholders: {', '.join(missing)}"
            )


def exploration_required_prompt_tokens_for_group(
    prompt_group: str,
    *,
    builtin_only: bool = False,
) -> dict[str, tuple[str, ...]]:
    prompt_group = validate_prompt_group_key(prompt_group)
    return {
        name: tokens
        for name, tokens in DEFAULT_PROMPT_REQUIRED_TOKENS.items()
        if name != "report.md"
    }


def required_prompt_tokens_for_group(
    prompt_group: str,
    *,
    builtin_only: bool = False,
) -> dict[str, tuple[str, ...]]:
    prompt_group = validate_prompt_group_key(prompt_group)

    contents: dict[str, str] = {}
    for name in PROMPT_ALLOWED_FILES:
        try:
            contents[name] = read_prompt_resource(
                prompt_group,
                name,
                builtin_only=builtin_only,
            )
        except (FileNotFoundError, UnicodeDecodeError):
            continue
    return required_prompt_tokens_for_contents(contents, prompt_group=prompt_group)


def required_prompt_tokens_for_contents(
    contents: dict[str, str],
    *,
    prompt_group: str,
) -> dict[str, tuple[str, ...]]:
    prompt_group = validate_prompt_group_key(prompt_group)

    required: dict[str, tuple[str, ...]] = {}
    if any(name in contents for name in PROMPT_REQUIRED_FILES):
        required.update(exploration_required_prompt_tokens_for_group(prompt_group))
    report_content = contents.get("report.md")
    if report_content is not None and (
        "{report_packet_json}" in report_content or not required
    ):
        required["report.md"] = DEFAULT_PROMPT_REQUIRED_TOKENS["report.md"]
    return required or DEFAULT_PROMPT_REQUIRED_TOKENS


def validate_prompt_submission_tools(group: str, name: str, content: str) -> None:
    """旧受管模板仍可读取，但必须由用户显式更新为 SDK 工具提交才能执行。"""
    missing = [tool for tool in ("submit_result", "register_artifact") if tool not in content]
    if missing:
        raise ValueError(
            f"prompt group {group} resource {name} missing SDK tools: " + ", ".join(missing)
        )


def _infer_prompt_group_capabilities(prompt_group: str) -> frozenset[str]:
    capabilities: set[str] = set()
    try:
        validate_exploration_prompt_resources(prompt_group)
    except ValueError:
        pass
    else:
        capabilities.add(PROMPT_CAPABILITY_EXPLORATION)
    try:
        validate_report_prompt_resources(prompt_group)
    except ValueError:
        pass
    else:
        capabilities.add(PROMPT_CAPABILITY_DURABLE_REPORT)
    return frozenset(capabilities)


def _builtin_prompt_group(group: str):
    return resources.files(BUILTIN_PROMPTS_PACKAGE).joinpath(group)


def _prompt_file_count(path: Path) -> int:
    if not path.exists() or not path.is_dir():
        return 0
    count = 0
    for item in path.iterdir():
        if not item.is_file():
            continue
        try:
            validate_prompt_file_name(item.name)
        except ValueError:
            continue
        count += 1
    return count


def _builtin_prompt_file_count(group: str) -> int:
    group_dir = _builtin_prompt_group(group)
    if not group_dir.is_dir():
        return 0
    count = 0
    for item in group_dir.iterdir():
        if not item.is_file():
            continue
        try:
            validate_prompt_file_name(item.name)
        except ValueError:
            continue
        count += 1
    return count


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
