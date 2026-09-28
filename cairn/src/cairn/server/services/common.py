"""Server 业务服务层，封装数据库读写、文件系统约束和协议清洗逻辑。"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sqlite3
import re
from datetime import datetime, timezone
from hashlib import sha256
from ipaddress import ip_address, ip_network
from json import dumps, loads
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException

from cairn.dispatcher.prompt_resources import (
    DEFAULT_PROMPT_GROUP,
    OUTPUT_LANGUAGE_FRAGMENT,
    PROMPT_ALLOWED_FILES,
    PROMPT_CAPABILITY_DURABLE_REPORT,
    PROMPT_CAPABILITY_EXPLORATION,
    PROMPT_REQUIRED_FILES,
    PROMPT_VISIBILITY_VISIBLE,
    RETIRED_PROMPT_GROUP_KEYS,
    available_prompt_groups as dispatcher_available_prompt_groups,
    ensure_prompts_root,
    list_prompt_group_infos,
    managed_prompt_file_path,
    managed_prompt_group_path,
    prompt_group_exists,
    read_prompt_resource,
    required_prompt_tokens_for_contents,
    required_prompt_tokens_for_group,
    validate_prompt_file_name as validate_prompt_file_name_value,
    validate_exploration_prompt_resources,
    validate_prompt_resources,
    validate_report_prompt_resources,
    validate_prompt_group_contents,
    validate_prompt_group_key as validate_prompt_group_key_value,
)
from cairn.server.models import (
    AgentEvent,
    Artifact,
    EvidenceRef,
    FROZEN_ARTIFACT_KINDS,
    FROZEN_ARTIFACT_KIND_MESSAGE,
    FactEvidence,
    HOOK_EVENT_NAMES,
    HOOK_OPERATION_STATUSES,
    HOOK_RISK_LEVELS,
    HOOK_TOOL_NAMES,
    Intent,
    IntentWorkerHistoryReason,
    KnowledgeFile,
    KnowledgeFileContent,
    KnowledgeSet,
    NETWORK_FLOW_IDS,
    ProjectMeta,
    ProjectReason,
    PromptFile,
    PromptFileContent,
    PromptGroup,
    PromptGroupValidationResult,
    NETWORK_FLOW_NETWORK_PLANES,
    NETWORK_FLOW_PROTOCOLS,
    NETWORK_FLOW_STATUSES,
    STANDARD_TASK_RUN_ERROR_TYPES,
    Scenario,
    ScenarioFeatureSchemaItem,
    Target,
    TaskRunCommandEvent,
    TaskRunHookEvent,
    TaskRunPromptEvent,
    TaskRunResultEvent,
    TaskRun,
)

URL_TARGET_RE = re.compile(r"https?://[^\s<>()\"']+", re.IGNORECASE)
IPV4_TARGET_RE = re.compile(r"(?<![\w.:-])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?![\w.:-])")
DOMAIN_TARGET_RE = re.compile(
    r"(?<![@\w.-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}\.?(?![\w.-])"
)
RUNTIME_CONTEXT_SCALAR_FIELDS = (
    "project_id",
    "task_run_id",
    "intent_id",
    "task_type",
    "worker_name",
    "worker_type",
    "status",
    "session_id",
    "model",
    "prompt_group",
    "prompt_version",
    "execution_profile",
    "config_hash",
    "container_name",
    "container_id",
    "container_state",
    "network_mode",
    "container_image",
    "image_id",
    "image_digest",
    "queued_at",
    "queue_wait_seconds",
    "started_at",
    "ended_at",
    "exit_code",
    "error_type",
    "error_message",
    "artifact_root",
    "worker_cli_version",
    "python_version",
    "node_version",
    "dispatcher_id",
    "config_path",
    "config_sha256",
)
RUNTIME_CONTEXT_AUDIT_FIELDS = set(RUNTIME_CONTEXT_SCALAR_FIELDS) | {
    "execution_environment",
    "scenario",
    "target_scope",
    "worker_selection",
    "knowledge_base",
    "project_resources",
    "tool_versions",
}
RUNTIME_TOOL_VERSION_FIELDS = ("git", "curl", "nmap")
# knowledge_base 是内置根知识库；用户创建的知识集映射到根目录下的子目录。
KNOWLEDGE_BASE_KEY = "knowledge_base"
KNOWLEDGE_BASE_ENV_VAR = "CAIRN_KNOWLEDGE_ROOT"
DEFAULT_RUNTIME_KNOWLEDGE_BASE_PATH = "datas/cairn/knowledge"
RUNTIME_KNOWLEDGE_BASE_PATH = os.environ.get(KNOWLEDGE_BASE_ENV_VAR, "").strip() or DEFAULT_RUNTIME_KNOWLEDGE_BASE_PATH
RUNTIME_KNOWLEDGE_BASE_MODE = "manual_file_knowledge"
RUNTIME_KNOWLEDGE_BASE_FORMATS = {"markdown", "yaml", "json"}
RUNTIME_KNOWLEDGE_BASE_FRONT_MATTER_FORMATS = {"yaml"}
RUNTIME_KNOWLEDGE_BASE_FRONT_MATTER_FIELDS = {
    "title",
    "tags",
    "applies_to",
    "summary",
    "last_verified_at",
}
RUNTIME_EXECUTION_ENVIRONMENT_PROFILES = {"shared_local"}
RUNTIME_EXECUTION_ENVIRONMENT_ISOLATION = {"shared_container"}
TARGET_SCOPE_POLICY_VERSIONS = {"target_scope_policy_v1"}
TARGET_SCOPE_POLICY_SOURCES = {"server_targets_snapshot"}
NETWORK_FLOW_ARTIFACT_ROLES = {
    "target_scope_gateway_metadata",
    "target_scope_gateway_wrapper_metadata",
    "target_scope_policy_handoff",
}
ARTIFACT_METADATA_ROLES = NETWORK_FLOW_ARTIFACT_ROLES
TARGET_SCOPE_STATUSES = {"seed", "approved", "discovered", "rejected"}
SCENARIO_ALLOWED_CAPS = {"NET_RAW", "NET_ADMIN", "SYS_ADMIN", "SYS_RAWIO"}
# Coverage 入口类型兼容 legacy pentest；default-copy 复用同一攻击面类型集合。
COVERAGE_SLOT_KINDS = {"endpoint", "service", "host", "role", "path", "param", "flow_state"}
DEFAULT_COPY_TEST_FAMILIES = {
    "configuration",
    "known_vulnerability",
    "authentication",
    "authorization",
    "session",
    "injection",
    "data_exposure",
    "ssrf",
    "file_handling",
    "business_logic",
    "concurrency",
}
DEFAULT_COPY_COVERAGE_OUTCOMES = {
    "tested_no_finding",
    "finding",
    "temporarily_blocked",
    "permanently_unreachable",
    "out_of_scope",
    "not_applicable",
}
DEFAULT_COPY_TERMINAL_COVERAGE_OUTCOMES = DEFAULT_COPY_COVERAGE_OUTCOMES - {
    "temporarily_blocked"
}
LEGACY_SCENARIO_KEYS = {"web-audit", "unified-dev"}
LEGACY_KNOWLEDGE_KEYS = {"web_standards", "coding_rules"}
LEGACY_PROMPT_GROUP_KEYS = {
    "web_recon",
    "code_audit",
    *RETIRED_PROMPT_GROUP_KEYS,
}
# feature schema 是 Console 场景表单和 dispatcher 之间的最小契约，避免场景 features 退回任意 JSON。
SCENARIO_FEATURE_SCHEMA_DEFS: tuple[dict[str, Any], ...] = (
    {
        "key": "sandbox", "label": "沙箱标记", "value_type": "boolean",
        "default": False, "runtime_driver": False,
        "description": "记录执行上下文；实际隔离由执行画像决定。",
    },
    {
        "key": "network_flow", "label": "网络访问", "value_type": "boolean",
        "default": False, "runtime_driver": True,
        "description": "启用时使用执行画像网络；显式关闭时禁用容器网络。",
    },
    {
        "key": "isolation", "label": "隔离方式", "value_type": "enum",
        "default": "container", "options": ["container"], "runtime_driver": False,
        "description": "执行隔离策略的元数据标记。",
    },
    {
        "key": "report_prompt_group", "label": "报告 Prompt 组", "value_type": "string",
        "default": DEFAULT_PROMPT_GROUP, "runtime_driver": False,
        "description": "报告使用的 Prompt 组；留空时使用目标驱动内置模板。",
    },
    {
        "key": "exploration_limit", "label": "探索投入", "value_type": "enum",
        "default": "balanced", "options": ["compact", "balanced", "thorough"],
        "runtime_driver": False,
        "description": "创建项目时使用的默认探索预算，可在项目中单独设置。",
    },
)
KNOWLEDGE_KEY_RE = re.compile(r"^[a-z0-9_-]+$")
KNOWLEDGE_FILE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
KNOWLEDGE_FILE_EXTENSIONS = {".md", ".yaml", ".yml", ".json", ".txt"}

def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def dump_json(value: dict[str, Any]) -> str:
    # sort_keys + 紧凑分隔符让 JSON 字段稳定，便于测试和后续 diff。
    return dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def dump_json_list(value: list[str]) -> str:
    return dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    loaded = loads(value)
    if isinstance(loaded, dict):
        return loaded
    return {}


def _loads_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    loaded = loads(value)
    if not isinstance(loaded, list):
        return []
    return [text for item in loaded if (text := str(item).strip())]


__all__ = [name for name in globals() if not name.startswith("__")]
