"""Artifact URI 到安全磁盘路径的共享解析器。"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping


class UnsafeArtifactPath(ValueError):
    """Artifact URI 不匹配记录或尝试逃逸持久化根。"""


def resolve_artifact_path(root: Path, row: Mapping[str, object]) -> Path:
    """按数据库记录解析路径，并拒绝 URI 欺骗、穿越和 symlink 逃逸。"""
    project_id = _safe_segment(row["project_id"], "project_id")
    run_segment = _safe_segment(row.get("task_run_id") or "unscoped", "task_run_id")
    uri = str(row["uri"])
    prefix = f"artifact://projects/{project_id}/runs/{run_segment}/"
    if not uri.startswith(prefix):
        raise UnsafeArtifactPath("artifact URI does not match its project and task run")
    filename = _safe_segment(uri[len(prefix) :], "filename")

    resolved_root = root.resolve()
    base = root / "projects" / project_id / "runs" / run_segment
    disk_path = base / filename
    try:
        resolved_base = base.resolve()
        resolved_base.relative_to(resolved_root)
        disk_path.resolve().relative_to(resolved_base)
        disk_path.resolve().relative_to(resolved_root)
    except ValueError as exc:
        raise UnsafeArtifactPath("artifact path escapes its storage root") from exc
    return disk_path


def _safe_segment(value: object, name: str) -> str:
    segment = str(value)
    if not segment or segment in {".", ".."} or "/" in segment or "\\" in segment:
        raise UnsafeArtifactPath(f"artifact {name} is not a safe path segment")
    return segment
