"""Docker put_archive 用的 tar 包构造工具。"""

from __future__ import annotations

import io
from pathlib import PurePosixPath
import tarfile


def text_file_archive(
    path: str,
    content: str,
    *,
    uid: int = 0,
    gid: int = 0,
    mode: int = 0o644,
    archive_root: str | None = None,
) -> tuple[str, bytes]:
    if uid < 0 or gid < 0:
        raise ValueError("container file uid/gid must be non-negative")
    if mode < 0 or mode > 0o777:
        raise ValueError("container file mode must be between 000 and 777")
    target = PurePosixPath(path)
    # Docker put_archive 需要以目录为落点，路径先做绝对路径和父目录穿越防护。
    if (
        not target.is_absolute()
        or str(target) != path
        or target.name in ("", ".", "..")
    ):
        raise ValueError(f"container file path must be absolute: {path}")
    parts = target.parts[1:]
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"invalid container file path: {path}")
    if archive_root is not None:
        root = PurePosixPath(archive_root)
        root_parts = root.parts[1:]
        if (
            not root.is_absolute()
            or str(root) != archive_root
            or not root_parts
            or any(part in ("", ".", "..") for part in root_parts)
            or target.parent != root
        ):
            raise ValueError("archive root must be the target file's direct parent")
        archive_path = str(root)
        archive_parts = (target.name,)
    elif len(parts) == 1:
        archive_path = "/"
        archive_parts = parts
    else:
        archive_path = f"/{parts[0]}"
        archive_parts = parts[1:]

    payload = content.encode("utf-8")
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        # Docker 会把目录项元数据应用到已有目录，必须显式使用目标用户身份。
        parent = ""
        for part in archive_parts[:-1]:
            parent = f"{parent}/{part}" if parent else part
            info = tarfile.TarInfo(parent)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.uid = uid
            info.gid = gid
            archive.addfile(info)

        file_name = "/".join(archive_parts)
        info = tarfile.TarInfo(file_name)
        info.size = len(payload)
        info.mode = mode
        info.uid = uid
        info.gid = gid
        archive.addfile(info, io.BytesIO(payload))
    return archive_path, stream.getvalue()
