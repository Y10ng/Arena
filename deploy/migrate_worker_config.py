#!/usr/bin/env python3
"""将 legacy Provider secret 引用显式迁移到 dispatch.yaml。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "cairn" / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from cairn.dispatcher.config import DispatchConfig  # noqa: E402
from cairn.server.services.dispatcher_config_store import (  # noqa: E402
    DispatcherConfigStore,
    migrate_legacy_secret_references,
    provider_credential_ref,
)

_SECRET_REF_PATTERN = re.compile(r"^secret://([A-Za-z0-9][A-Za-z0-9._-]{0,127})$")


class MigrationError(RuntimeError):
    """迁移无法安全完成。"""


@dataclass(frozen=True)
class MigrationResult:
    provider_count: int
    changed: bool
    backup_path: Path | None


def _safe_regular_file(path: Path, *, root: Path, required: bool) -> Path | None:
    root = root.resolve()
    candidate = path if path.is_absolute() else root / path
    if candidate.is_symlink():
        raise MigrationError(f"拒绝符号链接: {candidate}")
    try:
        resolved = candidate.resolve(strict=required)
    except FileNotFoundError:
        if required:
            raise MigrationError(f"缺少 secret 文件: {candidate.name}") from None
        return None
    if not resolved.is_relative_to(root):
        raise MigrationError(f"路径超出 secrets 目录: {candidate.name}")
    if not resolved.exists():
        return None
    if not resolved.is_file():
        raise MigrationError(f"secret 路径不是普通文件: {candidate.name}")
    return resolved


def _secret_path(reference: str, secrets_dir: Path) -> Path:
    match = _SECRET_REF_PATTERN.fullmatch(reference.strip())
    if not match:
        raise MigrationError(f"不支持的 legacy secret 引用格式: {reference}")
    return secrets_dir / f"{match.group(1)}.secret"


def _read_secret(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise MigrationError(f"无法安全读取 secret 文件: {path.name}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise MigrationError(f"secret 路径不是普通文件: {path.name}")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            value = handle.read().rstrip("\r\n")
    except (OSError, UnicodeError) as exc:
        raise MigrationError(f"无法安全读取 secret 文件: {path.name}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if not value:
        raise MigrationError(f"secret 文件为空: {path.name}")
    return value


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            with source.open("rb") as source_handle:
                shutil.copyfileobj(source_handle, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600, follow_symlinks=False)
        directory_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary.exists():
            temporary.unlink()


def _next_backup_path(config_path: Path) -> Path:
    base = config_path.with_name(f"{config_path.name}.pre-worker-migration.bak")
    candidate = base
    index = 1
    while candidate.exists():
        candidate = base.with_name(f"{base.name}.{index}")
        index += 1
    return candidate


def migrate(*, config_path: Path, secrets_dir: Path) -> MigrationResult:
    raw_config_path = config_path.expanduser()
    raw_secrets_dir = secrets_dir.expanduser()
    if raw_config_path.is_symlink() or not raw_config_path.is_file():
        raise MigrationError(f"dispatch.yaml 必须是普通文件: {raw_config_path}")
    if raw_secrets_dir.is_symlink() or not raw_secrets_dir.is_dir():
        raise MigrationError(f"secrets 目录不可用: {raw_secrets_dir}")
    config_path = raw_config_path.resolve()
    secrets_dir = raw_secrets_dir.resolve()

    store = DispatcherConfigStore(config_path)
    config, _content, revision = store.read()
    mapping = migrate_legacy_secret_references(config)

    resolved: dict[str, str] = {}
    for old_ref in mapping:
        secret_file = _safe_regular_file(
            _secret_path(old_ref, secrets_dir), root=secrets_dir, required=True
        )
        assert secret_file is not None
        resolved[old_ref] = _read_secret(secret_file)

    if not mapping:
        return MigrationResult(0, False, None)

    candidate: dict[str, Any] = yaml.safe_load(yaml.safe_dump(config, sort_keys=False)) or {}
    common_env = candidate.setdefault("common_env", {})
    if not isinstance(common_env, dict):
        raise MigrationError("dispatch.yaml common_env 必须是对象")
    providers = candidate.get("providers") or []
    if not isinstance(providers, list):
        raise MigrationError("dispatch.yaml providers 必须是数组")

    migrated = 0
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        old_ref = str(provider.get("credential_ref") or "").strip()
        if old_ref not in mapping:
            continue
        provider_id = str(provider.get("id") or "").strip()
        new_ref = provider_credential_ref(provider_id)
        value = resolved[old_ref]
        existing = common_env.get(new_ref)
        if existing is not None and str(existing) != value:
            raise MigrationError(f"common_env 已存在冲突项: {new_ref}")
        provider["credential_ref"] = new_ref
        common_env[new_ref] = value
        migrated += 1

    try:
        DispatchConfig.model_validate(candidate)
    except Exception as exc:
        raise MigrationError(f"迁移后的 dispatch.yaml 校验失败: {type(exc).__name__}") from exc

    backup_path = _next_backup_path(config_path)
    _atomic_copy(config_path, backup_path)
    store.write_config(candidate, expected_revision=revision)
    return MigrationResult(migrated, True, backup_path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="迁移 legacy Provider credential 配置")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--config", type=Path, default=Path("runtime/dispatch.yaml"))
    parser.add_argument("--secrets-dir", type=Path, default=Path("runtime/secrets"))
    return parser


def main() -> int:
    args = _parser().parse_args()
    root = args.root.expanduser().resolve()
    try:
        result = migrate(
            config_path=args.config if args.config.is_absolute() else root / args.config,
            secrets_dir=args.secrets_dir if args.secrets_dir.is_absolute() else root / args.secrets_dir,
        )
    except Exception as exc:
        print(f"Worker 配置迁移失败：{exc}", file=sys.stderr)
        return 1
    if not result.changed:
        print("Worker 配置迁移：无需变更")
        return 0
    print(f"Worker 配置迁移完成：providers={result.provider_count}")
    if result.backup_path is not None:
        print(f"备份：{result.backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
