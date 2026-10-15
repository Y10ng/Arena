"""持久启动计划的输入契约。"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .projects import CreateProjectRequest


class ScheduleTiming(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["once", "daily", "weekly"]
    timezone: str
    start_at: str
    local_time: str | None = None
    weekdays: list[int] = Field(default_factory=list)
    end_date: str | None = None
    max_runs: int | None = Field(default=None, ge=1)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("请选择可用的 IANA 时区") from exc
        return value

    @field_validator("start_at")
    @classmethod
    def validate_start_at(cls, value: str) -> str:
        return CreateProjectRequest._validate_absolute_timestamp(value)

    @field_validator("local_time")
    @classmethod
    def validate_time(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                parsed = time.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("本地时间必须为 HH:MM") from exc
            if len(value) != 5 or parsed.tzinfo is not None:
                raise ValueError("本地时间必须为 HH:MM")
        return value

    @field_validator("end_date")
    @classmethod
    def validate_date(cls, value: str | None) -> str | None:
        if value is not None:
            date.fromisoformat(value)
        return value

    @field_validator("weekdays")
    @classmethod
    def validate_weekdays(cls, value: list[int]) -> list[int]:
        if any(day < 0 or day > 6 for day in value):
            raise ValueError("星期必须介于 0（周一）与 6（周日）")
        return sorted(set(value))

    @model_validator(mode="after")
    def validate_rule(self):
        if self.mode == "once":
            if self.local_time is not None or self.weekdays or self.end_date or self.max_runs:
                raise ValueError("单次计划只需启动时间和显示时区")
            # 带偏移时间必须确实存在于该显示时区，不能把夏令时缺失时间自动挪动。
            supplied = datetime.fromisoformat(self.start_at.replace("Z", "+00:00"))
            local = supplied.astimezone(ZoneInfo(self.timezone))
            if supplied.replace(tzinfo=None) != local.replace(tzinfo=None) or supplied.utcoffset() != local.utcoffset():
                raise ValueError("启动时间须带所选时区的有效偏移；重复时刻请明确选择偏移")
        elif self.local_time is None:
            raise ValueError("重复运行必须填写本地时间")
        if self.mode == "weekly" and not self.weekdays:
            raise ValueError("每周运行必须选择至少一个星期")
        if self.mode == "daily" and self.weekdays:
            raise ValueError("每日运行不需要星期选择")
        return self


class CreateScheduleRequest(ScheduleTiming):
    request_id: UUID
    project: CreateProjectRequest

    @model_validator(mode="after")
    def validate_project_input(self):
        if self.project.request_id is not None:
            raise ValueError("请求身份属于计划保存，不应放在计划的项目配置中")
        if self.mode != "once" and self.project.resources:
            raise ValueError("周期资料请使用临时批次上传")
        return self


class UpdateScheduleRequest(ScheduleTiming):
    version: int = Field(ge=1)
    project: CreateProjectRequest | None = None
    resource_ids: list[str] | None = None
    refresh_library_keys: list[str] = Field(default_factory=list)
    explore_deadline: str | None = None

    @field_validator("explore_deadline")
    @classmethod
    def validate_deadline(cls, value: str | None) -> str | None:
        return CreateProjectRequest._validate_absolute_timestamp(value) if value is not None else None

    @model_validator(mode="after")
    def validate_future_input(self):
        if self.mode == "once":
            if self.project is not None or self.resource_ids is not None or self.refresh_library_keys:
                raise ValueError("单次计划输入归属于已创建项目，改期不重复提交输入")
        elif "explore_deadline" in self.model_fields_set:
            raise ValueError("周期执行停止边界应在未来项目配置中显式更新")
        elif self.project is None:
            raise ValueError("周期编辑必须提供完整未来输入")
        elif self.project.request_id is not None or self.project.resources:
            raise ValueError("周期资料使用批次，请求身份不属于未来项目配置")
        return self


class ScheduleActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    action: Literal["cancel", "start_now", "pause", "resume", "disable"]
