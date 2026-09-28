"""Candidate capture 跨 Python/ECMAScript 的权威 JSON 摘要。"""

from __future__ import annotations

from hashlib import sha256
import json
import math


def _ecmascript_number(value: float) -> str:
    """按 ECMAScript Number::toString 的阈值渲染 finite IEEE-754 数。"""
    if not math.isfinite(value):
        raise ValueError("candidate authority JSON does not permit non-finite numbers")
    if value == 0:
        return "0"
    text = repr(value).lower()
    sign = ""
    if text.startswith("-"):
        sign, text = "-", text[1:]
    if "e" not in text:
        return sign + (text[:-2] if text.endswith(".0") else text)

    mantissa, exponent_text = text.split("e", 1)
    exponent = int(exponent_text)
    digits = mantissa.replace(".", "")
    magnitude = abs(value)
    if 1e-6 <= magnitude < 1e21:
        decimal_position = mantissa.find(".")
        if decimal_position < 0:
            decimal_position = len(mantissa)
        decimal_position += exponent
        if decimal_position <= 0:
            rendered = "0." + ("0" * -decimal_position) + digits
        elif decimal_position >= len(digits):
            rendered = digits + ("0" * (decimal_position - len(digits)))
        else:
            rendered = digits[:decimal_position] + "." + digits[decimal_position:]
        return sign + rendered

    normalized_mantissa = mantissa[:-2] if mantissa.endswith(".0") else mantissa
    exponent_sign = "+" if exponent >= 0 else "-"
    return f"{sign}{normalized_mantissa}e{exponent_sign}{abs(exponent)}"


def candidate_authority_json(value: object) -> str:
    """按 Pi JSON.stringify 语义生成递归规范 JSON。"""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if type(value) is int:
        if not -(2**53 - 1) <= value <= 2**53 - 1:
            raise ValueError("candidate authority integer exceeds ECMAScript safe range")
        return str(value)
    if isinstance(value, float):
        return _ecmascript_number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join(candidate_authority_json(item) for item in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("candidate authority object keys must be strings")
        keys = sorted(
            value,
            key=lambda key: key.encode("utf-16-be", errors="surrogatepass"),
        )
        return "{" + ",".join(
            candidate_authority_json(key)
            + ":"
            + candidate_authority_json(value[key])
            for key in keys
        ) + "}"
    raise ValueError("candidate authority JSON contains an unsupported value")


def candidate_authority_sha256(value: object) -> str:
    """计算与 Pi candidate manifest 相同的 lowercase SHA-256。"""
    return sha256(candidate_authority_json(value).encode("utf-8")).hexdigest()


__all__ = ["candidate_authority_json", "candidate_authority_sha256"]
