"""测试出口的公开投影与只写认证输入。"""

from __future__ import annotations

from ipaddress import ip_address
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class TestEgressView(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Literal["direct", "http", "socks5"] = "direct"
    host: str | None = None
    port: int | None = None
    auth_configured: bool = False
    revision: int = 0


class TestEgressInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    mode: Literal["direct", "http", "socks5"] = "direct"
    host: str | None = Field(default=None, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535, strict=True)
    auth_action: Literal["keep", "replace", "clear"] = "keep"
    username: SecretStr | None = Field(default=None, exclude=True, repr=False)
    password: SecretStr | None = Field(default=None, exclude=True, repr=False)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value or value != value.strip() or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError("代理地址必须是主机名或 IP，不含空白或控制字符")
        if any(c in value for c in "/@?#\\"):
            raise ValueError("代理地址不能包含协议、路径或认证")
        try:
            ip_address(value)
        except ValueError:
            if ":" in value or "[" in value or "]" in value:
                raise ValueError("代理端口须单独填写，IPv6 地址不带方括号") from None
        return value

    @model_validator(mode="after")
    def validate_settings(self):
        if self.mode == "direct":
            if self.host is not None or self.port is not None or self.auth_action == "replace":
                raise ValueError("直接连接不能指定代理地址或认证")
        elif self.host is None or self.port is None:
            raise ValueError("代理模式需要地址和端口")
        if self.auth_action == "replace":
            if self.username is None or self.password is None:
                raise ValueError("替换认证必须同时提供用户名和密码")
            username, password = self.username.get_secret_value(), self.password.get_secret_value()
            if not username or any(c in username for c in ":\r\n\0"):
                raise ValueError("代理用户名不能为空或含冒号、CRLF、NUL")
            if any(c in password for c in "\r\n\0"):
                raise ValueError("代理密码不能含 CRLF 或 NUL")
        elif self.username is not None or self.password is not None:
            raise ValueError("只有替换认证时才能提交用户名和密码")
        return self


def egress_request_identity(value: TestEgressInput) -> dict:
    """仅供创建请求摘要临时使用；调用方只能持久化最终摘要。"""
    result = value.model_dump(mode="json")
    if value.auth_action == "replace":
        result["username"] = value.username.get_secret_value()
        result["password"] = value.password.get_secret_value()
    return result
