"""场景配置 schema。"""

from __future__ import annotations

from pydantic import ConfigDict

from cairn.dispatcher.prompt_resources import DEFAULT_PROMPT_GROUP

from .common import *

# 场景是项目运行策略的组合入口，引用 Prompt、知识库和执行环境画像。
class Scenario(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str
    label: str
    description: str | None = None
    is_default: bool = False
    profile_id: str = ""
    # Legacy fields kept for stored rows and older clients. New routing config
    # lives in dispatch.yaml workers[] and execution_profiles[].
    image: str = ""
    prompt_group: str = DEFAULT_PROMPT_GROUP
    knowledge: str | None = None
    origin_template: str | None = None
    goal_template: str | None = None
    cap_add: list[str] = Field(default_factory=list)
    features: dict[str, Any] = Field(default_factory=dict)

    @field_validator("key", "label", "prompt_group")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        if len(text) > 128:
            raise ValueError("must be 128 characters or fewer")
        return text

    @field_validator("key", "prompt_group")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise ValueError("must contain only letters, numbers, dots, underscores, or hyphens")
        return value

    @field_validator("knowledge")
    @classmethod
    def validate_knowledge_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        if len(text) > 128:
            raise ValueError("must be 128 characters or fewer")
        if not fullmatch(r"[a-z0-9_-]+", text):
            raise ValueError("must contain only lowercase letters, numbers, underscores, or hyphens")
        return text

    @field_validator("profile_id", "image")
    @classmethod
    def validate_optional_identifier_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("cap_add")
    @classmethod
    def validate_string_list(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = str(item).strip()
            if not text:
                raise ValueError("items must not be empty")
            if text in seen:
                continue
            seen.add(text)
            cleaned.append(text)
        return cleaned


class ScenarioCatalogEntry(BaseModel):
    """普通 Tenant 用户创建项目时可读取的最小场景目录项。"""

    key: str
    label: str
    description: str | None = None
    is_default: bool = False
    origin_template: str | None = None
    goal_template: str | None = None


class ScenarioFeatureSchemaItem(BaseModel):
    key: str
    label: str
    value_type: Literal["boolean", "enum", "string"]
    default: bool | str
    options: list[str] = Field(default_factory=list)
    runtime_driver: bool = False
    description: str = ""


class ScenarioProfileOption(BaseModel):
    id: str
    label: str = ""
    image_ref: str = ""
    image: str = ""
    network_policy: str = ""
    cap_add: list[str] = Field(default_factory=list)
    isolation: str = ""
    completed_action: str = ""


# 场景下拉选项，混合静态资源与当前 dispatcher 上报能力。
class ScenarioOptions(BaseModel):
    profiles: list[ScenarioProfileOption] = Field(default_factory=list)
    prompt_groups: list[str] = Field(default_factory=list)
    knowledge: list[str] = Field(default_factory=list)
    images: list[str] = Field(default_factory=list)
    cap_add: list[str] = Field(default_factory=list)
    cap_add_source: Literal["declared_inventory", "dispatcher_report", "unavailable"] = "unavailable"
    features: list[ScenarioFeatureSchemaItem] = Field(default_factory=list)
    project_counts: dict[str, int] = Field(default_factory=dict)


class ScenarioValidationResult(BaseModel):
    valid: bool = True
    scenario_count: int
    project_counts: dict[str, int] = Field(default_factory=dict)


__all__ = [name for name in globals() if not name.startswith("__")]
