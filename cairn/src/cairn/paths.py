"""应用目录与 site-packages 安装环境共享的路径解析。"""

from __future__ import annotations

from pathlib import Path


def resolve_source_relative_path(default: str | Path, *, anchor: Path) -> Path:
    """解析相对默认路径；应用目录以发布根为基准，site-packages 环境退回 cwd。"""
    path = Path(default)
    if path.is_absolute():
        return path.expanduser().resolve()
    for parent in anchor.resolve().parents:
        if (parent / "cairn/pyproject.toml").is_file() and (
            parent / "deploy/dispatch.yaml.example"
        ).is_file():
            return (parent / path).resolve()
    return (Path.cwd() / path).resolve()
