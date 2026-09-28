"""Dispatcher 配置模型与 pi 执行绑定校验。"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import re
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cairn.dispatcher.prompt_resources import (
    DEFAULT_PROMPT_GROUP,
    validate_exploration_prompt_resources,
    validate_prompt_resources,
)


TaskType = Literal["reason", "explore", "bootstrap"]
WorkerType = Literal["pi"]
ProviderApi = Literal["anthropic-messages", "openai-responses", "openai-chat-completions"]
CompletedAction = Literal["remove", "stop"]
IsolationPolicy = Literal["shared_container", "shared_local"]
ExecutionProfile = IsolationPolicy
_MEMORY_LIMIT_RE = re.compile(r"^([1-9][0-9]*)([bkmgBKMG]?)$")
_MEMORY_LIMIT_MULTIPLIERS = {
    "": 1,
    "b": 1,
    "k": 1024,
    "m": 1024**2,
    "g": 1024**3,
}

# 仅保留给旧控制面调用方的兼容判断；Dispatcher 运行时不再据此过滤 worker.env。
SENSITIVE_ENV_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "PRIVATE_KEY", "CREDENTIAL")


def is_sensitive_env_key(key: str) -> bool:
    """识别旧控制面视为敏感的环境变量名。"""
    normalized = key.strip().upper()
    return any(marker in normalized for marker in SENSITIVE_ENV_MARKERS)


class ReasonTaskConfig(BaseModel):
    timeout: int = Field(gt=0)
    max_intents: int = Field(gt=0, default=3)


class ExploreTaskConfig(BaseModel):
    timeout: int = Field(gt=0)
    conclude_timeout: int = Field(gt=0)


class BootstrapTaskConfig(BaseModel):
    timeout: int = Field(gt=0)
    conclude_timeout: int = Field(gt=0)


class ReportTaskConfig(BaseModel):
    # 报告任务使用冻结的场景策略和独立报告 Prompt，不再由运行时探索组决定。
    timeout: int = Field(gt=0, default=600)


class TasksConfig(BaseModel):
    bootstrap: BootstrapTaskConfig
    reason: ReasonTaskConfig
    explore: ExploreTaskConfig
    # report 可选：未配置时用默认 timeout，保持旧 YAML 兼容。
    report: ReportTaskConfig = Field(default_factory=ReportTaskConfig)


# 容器运行时基线，场景只能在 dispatcher YAML 能力范围内收窄或选择。
class ContainerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image: str
    allowed_images: list[str] = Field(default_factory=list)
    network_mode: str
    completed_action: CompletedAction
    # Legacy field kept for old YAML files. New scenario routing should use
    # execution_profiles[].isolation.
    execution_profile: IsolationPolicy = "shared_local"
    cap_add: list[str] = Field(default_factory=list)
    dns: list[str] = Field(default_factory=list)

    @field_validator("dns")
    @classmethod
    def validate_dns(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("dns entries must not be empty")
            try:
                address = ipaddress.ip_address(text)
            except ValueError as exc:
                raise ValueError("dns entries must be IPv4 or IPv6 literals") from exc
            if address.is_unspecified or address.is_multicast:
                raise ValueError("dns entries must not be unspecified or multicast addresses")
            normalized = str(address)
            if normalized in seen:
                continue
            seen.add(normalized)
            cleaned.append(normalized)
        return cleaned

    @field_validator("allowed_images")
    @classmethod
    def validate_allowed_images(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("allowed_images entries must not be empty")
            if text in seen:
                continue
            seen.add(text)
            cleaned.append(text)
        return cleaned


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_workers: int = Field(gt=0)
    max_running_projects: int = Field(gt=0)
    max_project_workers: int = Field(gt=0)
    interval: int = Field(gt=0)
    healthcheck_timeout: int = Field(gt=0)
    prompt_group: str = Field(default=DEFAULT_PROMPT_GROUP, min_length=1)
    playwright_session_policy: Literal["legacy", "task"] = "legacy"
    execution_checkpoints: Literal["off", "stage"] = "off"


class ModelHeader(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    value: str


class PiModelConfig(BaseModel):
    """显式模型绑定；凭据仅随 0600 请求文件传入 SDK，不进入 argv。"""

    model_config = ConfigDict(extra="forbid")

    protocol: ProviderApi
    model_name: str = Field(min_length=1)
    inference_endpoint: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    auth_header: str = Field(min_length=1)
    api_key: str = Field(min_length=1, repr=False)
    custom_headers: list[ModelHeader] = Field(default_factory=list, repr=False)


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    base_url: str
    provider_api: ProviderApi = "openai-chat-completions"
    credential_ref: str = ""
    inference_endpoint: str = ""
    auth_header: str = "Authorization"
    custom_headers: list[ModelHeader] = Field(default_factory=list, repr=False)

    @field_validator("id", "base_url")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("credential_ref")
    @classmethod
    def validate_optional_text(cls, value: str) -> str:
        return value.strip()


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    provider_id: str
    upstream_model: str
    context_window: int | None = Field(default=None, gt=0)

    @field_validator("id", "provider_id", "upstream_model")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ImageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    image: str

    @field_validator("id", "image")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ExecutionProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    image_ref: str = "default"
    network_policy: str = "host"
    cap_add: list[str] = Field(default_factory=list)
    isolation: IsolationPolicy = "shared_container"
    completed_action: CompletedAction = "stop"
    mem_limit: int | str | None = None
    memswap_limit: int | str | None = None
    pids_limit: int | None = Field(default=None, gt=0)

    @field_validator("id", "image_ref", "network_policy")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("cap_add")
    @classmethod
    def validate_cap_add(cls, value: list[str]) -> list[str]:
        return _clean_unique_string_list(value, field_name="cap_add")

    @field_validator("mem_limit", "memswap_limit", mode="before")
    @classmethod
    def validate_memory_limit(cls, value: object) -> int | None:
        return _normalize_memory_limit(value)

    @model_validator(mode="after")
    def validate_memory_relationship(self) -> "ExecutionProfileConfig":
        if self.memswap_limit is not None and self.mem_limit is None:
            raise ValueError("memswap_limit requires mem_limit")
        if (
            isinstance(self.mem_limit, int)
            and isinstance(self.memswap_limit, int)
            and self.memswap_limit < self.mem_limit
        ):
            raise ValueError("memswap_limit cannot be smaller than mem_limit")
        if self.id == "restricted":
            if self.mem_limit is None or self.memswap_limit is None or self.pids_limit is None:
                raise ValueError(
                    "restricted requires mem_limit, memswap_limit, and pids_limit"
                )
            if self.memswap_limit != self.mem_limit:
                raise ValueError(
                    "restricted memswap_limit must equal mem_limit"
                )
        return self


# 单个 worker 的静态能力和运行约束。新字段是 id/engine/model_id/concurrency；
# name/type/max_running/env 继续作为旧配置兼容入口。
class WorkerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = ""
    name: str = ""
    engine: WorkerType | None = None
    type: WorkerType | None = None
    model_id: str = ""
    model_binding: PiModelConfig | None = Field(default=None, repr=False)
    task_types: list[TaskType]
    concurrency: int | None = Field(default=None, gt=0)
    max_running: int | None = Field(default=None, gt=0)
    priority: int = Field(ge=0)
    enabled: bool = True
    env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        if not normalized.get("id") and normalized.get("name"):
            normalized["id"] = normalized["name"]
        if not normalized.get("name") and normalized.get("id"):
            normalized["name"] = normalized["id"]
        if not normalized.get("engine") and normalized.get("type"):
            normalized["engine"] = normalized["type"]
        if not normalized.get("type") and normalized.get("engine"):
            normalized["type"] = normalized["engine"]
        if normalized.get("concurrency") is None and normalized.get("max_running") is not None:
            normalized["concurrency"] = normalized["max_running"]
        if normalized.get("max_running") is None and normalized.get("concurrency") is not None:
            normalized["max_running"] = normalized["concurrency"]
        return normalized

    @field_validator("id", "name")
    @classmethod
    def validate_worker_identity(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("model_id")
    @classmethod
    def validate_optional_model_id(cls, value: str) -> str:
        return value.strip()

    @field_validator("task_types")
    @classmethod
    def validate_task_types(cls, value: list[TaskType]) -> list[TaskType]:
        if not value:
            raise ValueError("task_types must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("task_types must be unique")
        return value

    @model_validator(mode="after")
    def validate_env(self) -> "WorkerConfig":
        if self.type is None or self.engine is None:
            raise ValueError(f"worker {self.name or self.id} requires engine/type")
        if self.max_running is None or self.concurrency is None:
            raise ValueError(f"worker {self.name} requires concurrency/max_running")
        return self


# dispatcher.yaml 的完整配置快照。
class DispatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server: str
    dispatcher_id: str = Field(default_factory=lambda: os.environ.get("CAIRN_DISPATCHER_ID", "").strip() or "default")
    server_reachable_path: str | None = None
    runtime: RuntimeConfig
    tasks: TasksConfig
    container: ContainerConfig
    providers: list[ProviderConfig] = Field(default_factory=list)
    models: list[ModelConfig] = Field(default_factory=list)
    images: list[ImageConfig] = Field(default_factory=list)
    execution_profiles: list[ExecutionProfileConfig] = Field(default_factory=list)
    common_env: dict[str, str] = Field(default_factory=dict)
    workers: list[WorkerConfig] = Field(default_factory=list)

    @field_validator("server", "dispatcher_id")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("server_reachable_path")
    @classmethod
    def validate_optional_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @model_validator(mode="before")
    @classmethod
    def merge_common_env(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        common_env = data.get("common_env")
        if common_env is None:
            common_env = {}
        workers = data.get("workers")
        if not isinstance(common_env, dict) or not isinstance(workers, list):
            return data

        merged = dict(data)
        merged_workers: list[Any] = []
        for worker in workers:
            if not isinstance(worker, dict):
                merged_workers.append(worker)
                continue
            worker_env = worker.get("env")
            if worker_env is None:
                worker_env = {}
            if not isinstance(worker_env, dict):
                merged_workers.append(worker)
                continue
            worker_copy = dict(worker)
            # common_env 作为默认值，worker.env 对同名变量有覆盖权。
            worker_copy["env"] = {**common_env, **worker_env}
            merged_workers.append(worker_copy)
        merged["workers"] = merged_workers
        return merged

    @model_validator(mode="after")
    def validate_workers(self) -> "DispatchConfig":
        self._ensure_default_registries()
        self._resolve_worker_models()
        self._validate_registry_references()
        names = [worker.name for worker in self.workers]
        if len(set(names)) != len(names):
            raise ValueError("worker names must be unique")
        ids = [worker.id for worker in self.workers]
        if len(set(ids)) != len(ids):
            raise ValueError("worker ids must be unique")
        if self.runtime.max_project_workers > self.runtime.max_workers:
            raise ValueError("max_project_workers cannot exceed max_workers")
        return self

    @classmethod
    def load(cls, path: Path) -> "DispatchConfig":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        server_override = os.environ.get("CAIRN_SERVER_URL", "").strip()
        if server_override:
            # 脚本可覆盖 Dispatcher 访问地址，避免修改 durable YAML 或影响 Server bind 配置。
            data = {**data, "server": server_override}
        config = cls.model_validate(data)
        # Prompt 资源是运行时必需依赖，配置加载阶段即校验必需文件和占位符。
        validate_exploration_prompt_resources(config.runtime.prompt_group)
        return config

    def _ensure_default_registries(self) -> None:
        if not self.images:
            images = [ImageConfig(id="default", image=self.container.image)]
            seen = {self.container.image}
            for image in self.container.allowed_images:
                if image in seen:
                    continue
                seen.add(image)
                images.append(ImageConfig(id=_default_image_id(image), image=image))
            self.images = images
        if not self.execution_profiles:
            self.execution_profiles = [
                ExecutionProfileConfig(
                    id="default",
                    image_ref="default",
                    network_policy=self.container.network_mode,
                    cap_add=list(self.container.cap_add),
                    isolation="shared_container",
                    completed_action=self.container.completed_action,
                ),
                ExecutionProfileConfig(
                    id="network",
                    image_ref="default",
                    network_policy=self.container.network_mode,
                    cap_add=list(self.container.cap_add),
                    isolation="shared_container",
                    completed_action=self.container.completed_action,
                ),
                ExecutionProfileConfig(
                    id="restricted",
                    image_ref="default",
                    network_policy=self.container.network_mode,
                    cap_add=[
                        cap for cap in self.container.cap_add if cap == "NET_RAW"
                    ],
                    isolation="shared_container",
                    completed_action=self.container.completed_action,
                    mem_limit="1536m",
                    memswap_limit="1536m",
                    pids_limit=256,
                ),
            ]
    def _resolve_worker_models(self) -> None:
        models_by_id = {model.id: model for model in self.models}
        providers_by_id = {provider.id: provider for provider in self.providers}
        for worker in self.workers:
            if not worker.model_id or worker.model_binding is not None:
                continue
            model = models_by_id.get(worker.model_id)
            if model is None:
                raise ValueError(f"worker {worker.id} references unknown model: {worker.model_id}")
            provider = providers_by_id.get(model.provider_id)
            if provider is None:
                raise ValueError(f"model {model.id} references unknown provider: {model.provider_id}")
            if not provider.inference_endpoint:
                raise ValueError(f"provider {provider.id} requires an explicit inference_endpoint")
            credential = resolve_credential(provider.credential_ref, self.common_env)
            if not credential and not worker.enabled:
                continue
            worker.model_binding = PiModelConfig(
                protocol=provider.provider_api,
                model_name=model.upstream_model,
                inference_endpoint=provider.inference_endpoint,
                base_url=provider.base_url,
                auth_header=provider.auth_header,
                api_key=credential,
                custom_headers=provider.custom_headers,
            )

    def _validate_registry_references(self) -> None:
        _validate_unique_ids("providers", [provider.id for provider in self.providers])
        _validate_unique_ids("models", [model.id for model in self.models])
        _validate_unique_ids("images", [image.id for image in self.images])
        _validate_unique_ids("execution_profiles", [profile.id for profile in self.execution_profiles])

        image_ids = {image.id for image in self.images}
        baseline_caps = set(self.container.cap_add)
        for profile in self.execution_profiles:
            if profile.image_ref not in image_ids:
                raise ValueError(f"execution profile {profile.id} references unknown image: {profile.image_ref}")
            unknown_caps = sorted(set(profile.cap_add) - baseline_caps)
            if unknown_caps:
                raise ValueError(
                    f"execution profile {profile.id} cap_add outside container baseline: "
                    + ", ".join(unknown_caps)
                )
        model_ids = {model.id for model in self.models}
        for worker in self.workers:
            if worker.model_id and worker.model_id not in model_ids:
                raise ValueError(f"worker {worker.id} references unknown model: {worker.model_id}")
            if worker.concurrency is not None and worker.concurrency > self.runtime.max_project_workers:
                raise ValueError("worker.concurrency cannot exceed runtime.max_project_workers")

    def declared_inventory(self) -> dict[str, object]:
        """Return the deterministic inventory used by Server-side scenario validation."""
        return {
            "providers": [
                provider.model_dump(mode="json", exclude={"credential_ref"}) | {"credential_ref": provider.credential_ref}
                for provider in self.providers
            ],
            "models": [model.model_dump(mode="json") for model in self.models],
            "images": [image.model_dump(mode="json") for image in self.images],
            "capabilities": list(self.container.cap_add),
            "execution_profiles": [profile.model_dump(mode="json") for profile in self.execution_profiles],
            "workers": [
                {
                    "id": worker.id,
                    "name": worker.name,
                    "engine": worker.engine,
                    "type": worker.type,
                    "model_id": worker.model_id,
                    "task_types": list(worker.task_types),
                    "concurrency": worker.concurrency,
                    "max_running": worker.max_running,
                    "priority": worker.priority,
                    "enabled": worker.enabled,
                }
                for worker in self.workers
            ],
        }


def resolve_credential(credential_ref: str, common_env: dict[str, str]) -> str:
    """按 Cairn 运行时优先级解析 credential：进程环境覆盖 common_env。"""
    ref = credential_ref.strip()
    if not ref:
        return ""
    return os.environ.get(ref, common_env.get(ref, ""))


def credential_is_overridden_by_environment(credential_ref: str) -> bool:
    """只报告覆盖事实，不读取或返回环境变量值。"""
    ref = credential_ref.strip()
    return bool(ref and ref in os.environ)


def _validate_unique_ids(label: str, ids: list[str]) -> None:
    if len(set(ids)) != len(ids):
        raise ValueError(f"{label} ids must be unique")


def _clean_unique_string_list(value: list[str], *, field_name: str) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = item.strip()
        if not text:
            raise ValueError(f"{field_name} entries must not be empty")
        if text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return cleaned


def _normalize_memory_limit(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("memory limit must be a positive byte count or Docker unit string")
    if isinstance(value, int):
        if value <= 0:
            raise ValueError("memory limit must be positive")
        return value
    if not isinstance(value, str):
        raise ValueError("memory limit must be a positive byte count or Docker unit string")
    match = _MEMORY_LIMIT_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError("memory limit must use bytes or a b/k/m/g Docker unit")
    amount, suffix = match.groups()
    return int(amount) * _MEMORY_LIMIT_MULTIPLIERS[suffix.lower()]


def _default_image_id(image: str) -> str:
    safe = "".join(ch if ch.isalnum() else "-" for ch in image.lower()).strip("-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe[:80] or "image"
