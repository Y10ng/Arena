"""Dispatcher 大型临时文件的持久磁盘目录。"""

from __future__ import annotations

import os
from pathlib import Path
import stat

RUNTIME_TEMP_DIR_ENV = "CAIRN_RUNTIME_TEMP_DIR"


def runtime_temp_dir() -> Path:
    """返回当前用户私有的临时目录，默认避开通常由 tmpfs 承载的 /tmp。"""
    configured = os.environ.get(RUNTIME_TEMP_DIR_ENV)
    path = Path(configured) if configured else Path(f"/var/tmp/cairn-runtime-{os.getuid()}")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise RuntimeError(f"runtime temporary path is not a directory: {path}")
    if info.st_uid != os.getuid():
        raise PermissionError(f"runtime temporary directory is not owned by current user: {path}")
    os.chmod(path, 0o700)
    return path
