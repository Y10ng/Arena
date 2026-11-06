"""从已校验的本地 bundle 准备一次性的普通文件工作副本。"""

from __future__ import annotations

import ctypes
import errno
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from cairn.dispatcher.advisory_bundle import (
    _bundle_expected_files,
    _make_read_only,
    _verify_materialized_bundle,
)

COPY_CHUNK_BYTES = 1024 * 1024


def _publish(temporary: Path, destination: Path) -> None:
    # rename 会替换已有空目录；用户在复制期间创建 inputs 也必须保留，故使用 Linux no-replace。
    rename = ctypes.CDLL(None, use_errno=True).renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(temporary), -100, os.fsencode(destination), 1) != 0:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise RuntimeError("workspace inputs conflict: destination already exists")
        raise OSError(code, os.strerror(code))


def prepare_workspace_inputs(
    workspace: Path, local_bundle: Path, manifest: dict[str, object], digest: str,
) -> None:
    """相同材料只校验复用；冲突和失败不覆盖现场，也不发布半成品。"""
    if not stat.S_ISDIR(workspace.lstat().st_mode):
        raise RuntimeError("workspace inputs require a regular workspace directory")
    manifest_bytes = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    if sha256(manifest_bytes).hexdigest() != digest:
        raise RuntimeError("workspace inputs manifest digest mismatch")
    destination = workspace / "inputs"
    if os.path.lexists(destination):
        try:
            _verify_materialized_bundle(destination, manifest, manifest_bytes)
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(f"workspace inputs conflict: {exc}") from exc
        return

    _verify_materialized_bundle(local_bundle, manifest, manifest_bytes)
    # 临时树留在 bind 外的同级私有目录，文件页和执行者在发布前看不到半成品。
    temporary = Path(tempfile.mkdtemp(prefix=".inputs-staging-", dir=workspace.parent))
    try:
        for relative, (size, expected_digest) in _bundle_expected_files(manifest, manifest_bytes).items():
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(local_bundle / relative, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source, target.open("xb") as output:
                initial = os.fstat(source.fileno())
                if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1 or initial.st_size != size:
                    raise RuntimeError(f"workspace inputs source changed: {relative}")
                actual = sha256()
                remaining = size
                while remaining:
                    chunk = source.read(min(remaining, COPY_CHUNK_BYTES))
                    if not chunk:
                        raise RuntimeError(f"workspace inputs incomplete copy: {relative}")
                    output.write(chunk)
                    actual.update(chunk)
                    remaining -= len(chunk)
                if source.read(1) or actual.hexdigest() != expected_digest:
                    raise RuntimeError(f"workspace inputs source sha256 mismatch: {relative}")
        _make_read_only(temporary)
        _verify_materialized_bundle(temporary, manifest, manifest_bytes)
        _publish(temporary, destination)
    finally:
        # 只清理本次尚未发布的临时副本，不碰已有 inputs 或其他工作文件。
        if temporary.exists():
            for directory, _children, _files in os.walk(temporary, followlinks=False):
                os.chmod(directory, 0o700, follow_symlinks=False)
            shutil.rmtree(temporary)
