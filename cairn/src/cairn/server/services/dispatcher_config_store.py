"""带并发控制的 Dispatcher 配置存储。"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import fcntl
from hashlib import sha256
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException
import yaml

from cairn.dispatcher.config import DispatchConfig


_CONTROL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_CREDENTIAL_REF_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_LEGACY_SECRET_REF_PREFIX = "secret://"
_REDACTED_VALUE = "[REDACTED]"


def _redact_url_userinfo(value: str) -> str:
    """解析 URL authority 并完整移除 userinfo，包括密码中的 @。"""
    try:
        parsed = urlsplit(value)
        if not parsed.scheme or (parsed.username is None and parsed.password is None):
            return value
        hostname = parsed.hostname
        if not hostname:
            return _REDACTED_VALUE
        host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit((parsed.scheme, f"{_REDACTED_VALUE}@{host}{port}", parsed.path, parsed.query, parsed.fragment))
    except (ValueError, UnicodeError):
        # URL-like 值一旦含 userinfo 标记却无法解析，应整体隐藏而不是返回潜在凭据。
        return _REDACTED_VALUE if "://" in value and "@" in value else value


def _redact_config_values(value: Any) -> Any:
    if isinstance(value, dict):
        safe = {key: _redact_config_values(item) for key, item in value.items()}
        # pi 显式绑定与 Provider 自定义头可能携带凭据，不能只保护旧 env 路径。
        if "api_key" in safe:
            safe["api_key"] = _REDACTED_VALUE
        headers = safe.get("custom_headers")
        if isinstance(headers, list):
            for header in headers:
                if isinstance(header, dict) and "value" in header:
                    header["value"] = _REDACTED_VALUE
        return safe
    if isinstance(value, list):
        return [_redact_config_values(item) for item in value]
    if isinstance(value, str):
        return _redact_url_userinfo(value)
    return value


def redact_dispatcher_config(config: dict[str, Any]) -> dict[str, Any]:
    """返回可供控制面展示的副本，保留结构但移除环境凭据和 URL userinfo。"""
    safe = _redact_config_values(deepcopy(config))
    common_env = safe.get("common_env")
    if isinstance(common_env, dict):
        for key in list(common_env):
            common_env[key] = _REDACTED_VALUE
    workers = safe.get("workers")
    if isinstance(workers, list):
        for worker in workers:
            if not isinstance(worker, dict) or not isinstance(worker.get("env"), dict):
                continue
            # 控制面不需要读取 worker env 的任何值；全部脱敏可避免未知命名的凭据漏出。
            for key in list(worker["env"]):
                worker["env"][key] = _REDACTED_VALUE
    return safe


def redact_dispatcher_document(config: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """同时生成脱敏后的结构化配置和 YAML，避免同一响应的 content 泄漏。"""
    safe = redact_dispatcher_config(config)
    return safe, _dump_mapping(safe)


def _restore_matching_redactions(submitted: Any, current: Any) -> Any:
    """只还原与当前脱敏投影完全相同的节点，保留用户真实修改。"""
    if submitted == _redact_config_values(current) and submitted != current:
        return deepcopy(current)
    if isinstance(submitted, dict) and isinstance(current, dict):
        restored = {
            key: _restore_matching_redactions(value, current[key]) if key in current else value
            for key, value in submitted.items()
        }
        if restored.get("api_key") == _REDACTED_VALUE and "api_key" in current:
            restored["api_key"] = current["api_key"]
        headers = restored.get("custom_headers")
        previous_headers = current.get("custom_headers")
        if isinstance(headers, list) and isinstance(previous_headers, list):
            previous_by_name = {
                header.get("name"): header.get("value")
                for header in previous_headers
                if isinstance(header, dict) and isinstance(header.get("name"), str)
            }
            for header in headers:
                if (
                    isinstance(header, dict)
                    and isinstance(header.get("name"), str)
                    and header.get("value") == _REDACTED_VALUE
                    and header["name"] in previous_by_name
                ):
                    header["value"] = previous_by_name[header["name"]]
        return restored
    if isinstance(submitted, list) and isinstance(current, list):
        current_by_id = {
            _item_id(item): item
            for item in current
            if isinstance(item, dict) and _item_id(item)
        }
        submitted_has_ids = any(
            isinstance(item, dict) and _item_id(item)
            for item in submitted
        )
        if current_by_id and submitted_has_ids:
            return [
                _restore_matching_redactions(value, current_by_id[item_id])
                if isinstance(value, dict)
                and (item_id := _item_id(value))
                and item_id in current_by_id
                else value
                for value in submitted
            ]
        return [
            _restore_matching_redactions(value, current[index]) if index < len(current) else value
            for index, value in enumerate(submitted)
        ]
    return submitted


def _restore_redacted_values(
    submitted: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    """把控制面占位符还原为已保存值，避免普通编辑意外覆盖运行凭据。"""
    if _REDACTED_VALUE not in str(submitted):
        return submitted
    restored = _restore_matching_redactions(deepcopy(submitted), current)
    submitted_common = restored.get("common_env")
    current_common = current.get("common_env")
    if isinstance(submitted_common, dict) and isinstance(current_common, dict):
        for key, value in list(submitted_common.items()):
            if value == _REDACTED_VALUE and key in current_common:
                submitted_common[key] = current_common[key]
    submitted_workers = restored.get("workers")
    current_workers = current.get("workers")
    if isinstance(submitted_workers, list) and isinstance(current_workers, list):
        current_by_id = {
            _item_id(item): item
            for item in current_workers
            if isinstance(item, dict) and _item_id(item)
        }
        for worker in submitted_workers:
            if not isinstance(worker, dict) or not isinstance(worker.get("env"), dict):
                continue
            previous = current_by_id.get(_item_id(worker))
            previous_env = previous.get("env") if isinstance(previous, dict) else None
            if not isinstance(previous_env, dict):
                continue
            for key, value in list(worker["env"].items()):
                if value == _REDACTED_VALUE and key in previous_env:
                    worker["env"][key] = previous_env[key]
    return restored


class DispatcherConfigStore:
    """使用 flock、revision 和原子替换维护 dispatch.yaml。"""

    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self.lock_dir = self.path.parent / "locks"
        self.lock_path = self.lock_dir / "dispatch-config.lock"

    def read(self) -> tuple[dict[str, Any], str, str]:
        raw = self._read_bytes()
        content = raw.decode("utf-8")
        data = self._parse_mapping(content)
        return data, content, revision_for_bytes(raw)

    def write_content(
        self,
        content: str,
        *,
        expected_revision: str | None = None,
    ) -> tuple[dict[str, Any], str, str]:
        submitted = self._parse_mapping(content)
        with self._exclusive_lock():
            self._check_revision(expected_revision)
            current = self._parse_mapping(self._read_bytes().decode("utf-8"))
            restored = _restore_redacted_values(submitted, current)
            payload = content if restored is submitted else _dump_mapping(restored)
            self._validate_and_replace(payload)
        return self.read()

    def write_config(
        self,
        config: dict[str, Any],
        *,
        expected_revision: str | None = None,
    ) -> tuple[dict[str, Any], str, str]:
        with self._exclusive_lock():
            self._check_revision(expected_revision)
            current = self._parse_mapping(self._read_bytes().decode("utf-8"))
            restored = _restore_redacted_values(config, current)
            self._validate_and_replace(_dump_mapping(restored))
        return self.read()

    def mutate(
        self,
        expected_revision: str,
        mutator: Callable[[dict[str, Any]], None],
    ) -> tuple[dict[str, Any], str, str]:
        with self._exclusive_lock():
            self._check_revision(expected_revision)
            data = self._parse_mapping(self._read_bytes().decode("utf-8"))
            mutator(data)
            self._validate_and_replace(_dump_mapping(data))
        return self.read()

    def create_item(
        self,
        collection: str,
        item: dict[str, Any],
        *,
        expected_revision: str,
    ) -> tuple[dict[str, Any], str, str]:
        item_id, normalized = _normalize_control_item(collection, item, creating=True)

        def create(config: dict[str, Any]) -> None:
            items = _collection(config, collection)
            if _find_item(config, collection, item_id) is not None:
                raise HTTPException(409, f"{collection} already contains {item_id}")
            items.append(normalized)

        return self.mutate(expected_revision, create)

    def update_item(
        self,
        collection: str,
        item_id: str,
        item: dict[str, Any],
        *,
        expected_revision: str,
    ) -> tuple[dict[str, Any], str, str]:
        item_id = _validate_control_id(item_id)
        credential_ref_supplied = collection != "providers" or "credential_ref" in item
        normalized_id, normalized = _normalize_control_item(
            collection,
            item,
            expected_id=item_id,
            creating=False,
        )

        def update(config: dict[str, Any]) -> None:
            items = _collection(config, collection)
            for index, current in enumerate(items):
                if not isinstance(current, dict) or _item_id(current) != normalized_id:
                    continue
                if collection == "workers" and (
                    current.get("engine", current.get("type")) != "pi"
                    or current.get("type", "pi") != "pi"
                ):
                    # 历史配置可读，但不能借省略 engine 的更新原地转换为新执行器。
                    raise HTTPException(422, "Worker is unavailable: engine_unsupported; create a new pi Worker")
                if collection == "providers":
                    old_ref = str(current.get("credential_ref") or "").strip()
                    if not credential_ref_supplied:
                        # 普通 Provider 编辑不暴露内部 ref，必须保留旧引用和已有 Key。
                        normalized["credential_ref"] = old_ref
                    new_ref = str(normalized.get("credential_ref") or "").strip()
                    _move_common_env_value(config, old_ref, new_ref)
                items[index] = normalized
                return
            raise HTTPException(404, f"{collection} item not found: {item_id}")

        return self.mutate(expected_revision, update)

    def delete_item(
        self,
        collection: str,
        item_id: str,
        *,
        expected_revision: str,
    ) -> tuple[dict[str, Any], str, str]:
        item_id = _validate_control_id(item_id)

        def delete(config: dict[str, Any]) -> None:
            _protect_referenced_delete(config, collection, item_id)
            items = _collection(config, collection)
            for index, current in enumerate(items):
                if not isinstance(current, dict) or _item_id(current) != item_id:
                    continue
                credential_ref = str(current.get("credential_ref") or "").strip()
                del items[index]
                if collection == "providers" and credential_ref:
                    if not _providers_referencing(config, credential_ref):
                        _common_env(config).pop(credential_ref, None)
                return
            raise HTTPException(404, f"{collection} item not found: {item_id}")

        return self.mutate(expected_revision, delete)

    def put_credential(
        self,
        provider_id: str,
        credential: str,
        *,
        expected_revision: str,
    ) -> tuple[str, str, bool]:
        provider_id = _validate_control_id(provider_id)
        credential_ref = ""

        def put(config: dict[str, Any]) -> None:
            nonlocal credential_ref
            provider = _find_item(config, "providers", provider_id)
            if provider is None:
                raise HTTPException(404, f"Provider not found: {provider_id}")
            credential_ref = str(provider.get("credential_ref") or "").strip()
            if not credential_ref:
                credential_ref = provider_credential_ref(provider_id)
                provider["credential_ref"] = credential_ref
            _common_env(config)[credential_ref] = credential

        _config, _content, revision = self.mutate(expected_revision, put)
        return revision, credential_ref, True

    def delete_credential(
        self,
        provider_id: str,
        *,
        expected_revision: str,
    ) -> tuple[str, str, bool]:
        provider_id = _validate_control_id(provider_id)
        credential_ref = ""

        def delete(config: dict[str, Any]) -> None:
            nonlocal credential_ref
            provider = _find_item(config, "providers", provider_id)
            if provider is None:
                raise HTTPException(404, f"Provider not found: {provider_id}")
            credential_ref = str(provider.get("credential_ref") or "").strip()
            if not credential_ref:
                return
            shared = [item for item in _providers_referencing(config, credential_ref) if item != provider_id]
            if shared:
                raise HTTPException(
                    409,
                    f"Provider {provider_id} credential_ref 仍被其他 Provider 引用: "
                    + ", ".join(sorted(shared)),
                )
            _common_env(config).pop(credential_ref, None)

        _config, _content, revision = self.mutate(expected_revision, delete)
        return revision, credential_ref, False

    def _validate_and_replace(self, content: str) -> None:
        self._parse_mapping(content)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        tmp_path = Path(tmp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                DispatchConfig.load(tmp_path)
            except Exception as exc:
                # Pydantic 错误可能包含 input_value，控制面只返回错误类型以免反射 credential。
                raise HTTPException(422, f"Invalid dispatch.yaml: {type(exc).__name__}") from exc
            os.replace(tmp_path, self.path)
            os.chmod(self.path, 0o600, follow_symlinks=False)
            self._fsync_directory(self.path.parent)
        finally:
            if fd >= 0:
                os.close(fd)
            if tmp_path.exists():
                tmp_path.unlink()

    def _check_revision(self, expected_revision: str | None) -> None:
        if expected_revision is None:
            return
        current = revision_for_bytes(self._read_bytes())
        if expected_revision != current:
            raise HTTPException(409, "dispatch.yaml revision conflict")

    def _read_bytes(self) -> bytes:
        if not self.path.exists() or not self.path.is_file():
            raise HTTPException(409, f"Dispatcher YAML is not readable by the server: {self.path}")
        return self.path.read_bytes()

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        if self.lock_dir.is_symlink():
            raise HTTPException(409, "locks directory must not be a symlink")
        self.lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.lock_dir, 0o700, follow_symlinks=False)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise HTTPException(409, f"Cannot open dispatcher config lock: {exc}") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise HTTPException(409, "dispatcher config lock must be a regular file")
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @staticmethod
    def _parse_mapping(content: str) -> dict[str, Any]:
        try:
            data = yaml.safe_load(content) or {}
        except Exception as exc:
            raise HTTPException(422, "Invalid dispatch.yaml: YAML parsing failed") from exc
        if not isinstance(data, dict):
            raise HTTPException(422, "Invalid dispatch.yaml: root value must be an object")
        return data

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def revision_for_bytes(content: bytes) -> str:
    return sha256(content).hexdigest()


def provider_credential_ref(provider_id: str) -> str:
    """为结构化 Provider 创建稳定且不碰撞的 common_env key。"""
    provider_id = _validate_control_id(provider_id)
    normalized = re.sub(r"[^A-Z0-9]+", "_", provider_id.upper()).strip("_") or "PROVIDER"
    digest = sha256(provider_id.encode("utf-8")).hexdigest()[:8].upper()
    return f"CAIRN_PROVIDER_{normalized}_{digest}_API_KEY"


def migrate_legacy_secret_references(config: dict[str, Any]) -> dict[str, str]:
    """仅生成 secret:// 到新 ref 的迁移映射；调用方负责取得旧值并写回 common_env。"""
    mapping: dict[str, str] = {}
    providers = config.get("providers") or []
    if not isinstance(providers, list):
        return mapping
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        old_ref = str(provider.get("credential_ref") or "").strip()
        provider_id = str(provider.get("id") or "").strip()
        if old_ref.startswith(_LEGACY_SECRET_REF_PREFIX) and provider_id:
            mapping[old_ref] = provider_credential_ref(provider_id)
    return mapping


def _validate_control_id(value: str) -> str:
    text = str(value).strip()
    if not _CONTROL_ID_PATTERN.fullmatch(text):
        raise HTTPException(422, "id 格式非法；仅允许字母、数字、点、下划线和连字符，长度 1-80")
    return text


def _validate_credential_ref(value: str, provider_id: str) -> str:
    text = str(value).strip() or provider_credential_ref(provider_id)
    if not _CREDENTIAL_REF_PATTERN.fullmatch(text):
        raise HTTPException(422, "credential_ref 格式非法；必须是可用作环境变量名的标识符")
    return text


def _dump_mapping(config: dict[str, Any]) -> str:
    return yaml.safe_dump(config, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _collection(config: dict[str, Any], key: str) -> list[Any]:
    if key not in {"providers", "models", "workers"}:
        raise HTTPException(404, f"Unsupported dispatcher collection: {key}")
    items = config.setdefault(key, [])
    if not isinstance(items, list):
        raise HTTPException(422, f"dispatch.yaml {key} must be an array")
    return items


def _common_env(config: dict[str, Any]) -> dict[str, Any]:
    common_env = config.setdefault("common_env", {})
    if not isinstance(common_env, dict):
        raise HTTPException(422, "dispatch.yaml common_env must be an object")
    return common_env


def _item_id(item: dict[str, Any]) -> str:
    return str(item.get("id") or item.get("name") or "")


def _normalize_control_item(
    collection: str,
    item: dict[str, Any],
    *,
    expected_id: str | None = None,
    creating: bool,
) -> tuple[str, dict[str, Any]]:
    if collection not in {"providers", "models", "workers"}:
        raise HTTPException(404, f"Unsupported dispatcher collection: {collection}")
    normalized = deepcopy(item)
    item_id = _validate_control_id(_item_id(normalized))
    if expected_id is not None and item_id != expected_id:
        raise HTTPException(422, "请求路径 id 与 payload id 不一致")
    if collection == "providers":
        normalized["id"] = item_id
        normalized["credential_ref"] = _validate_credential_ref(
            str(normalized.get("credential_ref") or ""),
            item_id,
        )
    elif collection == "models":
        normalized["id"] = item_id
    else:
        allowed = {
            "id",
            "name",
            "engine",
            "model_id",
            "task_types",
            "concurrency",
            "priority",
            "enabled",
            "env",
        }
        unexpected = sorted(str(key) for key in normalized if key not in allowed)
        if unexpected:
            raise HTTPException(422, "Worker payload 包含不支持字段: " + ", ".join(unexpected))
        if normalized.get("env") not in (None, {}):
            raise HTTPException(422, "结构化 Worker API 不接受 env；请通过 Model 配置 pi 模型绑定")
        if normalized.get("engine") not in (None, "pi"):
            raise HTTPException(422, "Worker is unavailable: engine_unsupported; only pi is supported")
        normalized = {key: value for key, value in normalized.items() if key != "env"}
        normalized["id"] = item_id
        normalized.setdefault("name", item_id)
        normalized["engine"] = "pi"
    return item_id, normalized


def _move_common_env_value(config: dict[str, Any], old_ref: str, new_ref: str) -> None:
    if not old_ref or old_ref == new_ref:
        return
    common_env = _common_env(config)
    if old_ref not in common_env:
        return
    if new_ref not in common_env:
        common_env[new_ref] = common_env[old_ref]
    if len(_providers_referencing(config, old_ref)) <= 1:
        common_env.pop(old_ref, None)


def _providers_referencing(config: dict[str, Any], credential_ref: str) -> list[str]:
    providers = config.get("providers") or []
    if not isinstance(providers, list):
        return []
    return [
        _item_id(provider)
        for provider in providers
        if isinstance(provider, dict)
        and str(provider.get("credential_ref") or "").strip() == credential_ref
    ]


def _protect_referenced_delete(config: dict[str, Any], collection: str, item_id: str) -> None:
    if collection == "providers":
        references = [
            _item_id(item)
            for item in _collection(config, "models")
            if isinstance(item, dict) and item.get("provider_id") == item_id
        ]
        if references:
            raise HTTPException(409, f"Provider {item_id} 仍被 Model 引用: " + ", ".join(sorted(references)))
    elif collection == "models":
        references = [
            _item_id(item)
            for item in _collection(config, "workers")
            if isinstance(item, dict) and item.get("model_id") == item_id
        ]
        if references:
            raise HTTPException(409, f"Model {item_id} 仍被 Worker 引用: " + ", ".join(sorted(references)))


def _find_item(config: dict[str, Any], key: str, item_id: str) -> dict[str, Any] | None:
    items = config.get(key) or []
    if not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict) and _item_id(item) == item_id:
            return item
    return None
