"""复测永久删除只处理明确拥有的目录，不扫描共享暂存或来源目录。"""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path
import shutil
import stat


def remove_owned_directory(root: Path, target: Path) -> None:
    """拒绝链接和越界；仍在接收的上传必须关闭后才能确认清理完成。"""
    root = root.absolute()
    target = target.absolute()
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError("复测清理目录越界") from exc
    if not relative.parts or any(part in {".", ".."} for part in relative.parts):
        raise ValueError("复测清理目录无效")
    current = root
    for part in (None, *relative.parts):
        if part is not None:
            current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
            raise ValueError("复测清理目录包含链接或非法节点")
    def raise_walk_error(error: OSError) -> None:
        raise error

    for directory, dirs, files in os.walk(target, followlinks=False, onerror=raise_walk_error):
        for name in (*dirs, *files):
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError("复测清理目录包含链接或非法节点")
            if stat.S_ISREG(mode):
                try:
                    with path.open("rb") as stream:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise OSError(errno.EBUSY, "本轮仍有上传正在接收，请稍后重试") from exc
                except FileNotFoundError:
                    # 上传退出可自行移除临时文件；这不代表其他 I/O 错误可以忽略。
                    continue
    try:
        shutil.rmtree(target)
    except FileNotFoundError:
        if target.exists():
            raise
    if target.exists() or target.is_symlink():
        raise OSError(errno.EIO, "本轮目录清理未完成")
