"""资料库选择在 Server 与 Dispatcher 之间共享的摘要约定。"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Iterable, Mapping


def selection_sha256(
    library_key: str,
    version: int,
    revision_sha256: str,
    files: Iterable[Mapping[str, object]],
) -> str:
    """绑定原版身份和所选文件元数据，不将其误认为整版内容摘要。"""
    payload = {
        "library_key": library_key,
        "version": version,
        "revision_sha256": revision_sha256,
        "files": sorted(
            (
                {"path": item["path"], "size_bytes": item["size_bytes"], "sha256": item["sha256"]}
                for item in files
            ),
            key=lambda item: str(item["path"]),
        ),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()
