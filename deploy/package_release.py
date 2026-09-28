#!/usr/bin/env python3
"""使用 allowlist 生成不含密钥和运行数据的 Arena 代码发布包。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FIXED_ZIP_TIME = (2020, 1, 1, 0, 0, 0)
EXPECTED_CAIRN_AGENT_VERSION = "1.0.0"
EXPECTED_PI_SDK_VERSION = "0.73.0"
ROOT_FILES = ("README.md", "CHANGELOG.md", "CLAUDE.md", "AGENT.md")
TREE_ROOTS = (
    Path("cairn/src/cairn"),
    Path("datas/cairn/knowledge"),
    Path("deploy"),
    Path("container"),
)
EXACT_FILES = (
    Path("cairn/pyproject.toml"),
    Path("cairn/uv.lock"),
    Path("docs/deployment-linux-zh.md"),
)
DENIED_PARTS = {"__pycache__", ".pytest_cache", ".venv", "node_modules", "dist", ".git", ".claude", ".codex", ".cairn", "secrets", "keys", "credentials"}
DENIED_SUFFIXES = {".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".log", ".swp", ".swo", ".key", ".pem", ".p12", ".pfx"}
DENIED_NAMES = {".env", "id_rsa", "id_ed25519", "auth.json", "credentials.json"}
# Dispatcher的runtime源码必须保留；这里只排除宿主和pi-agent的运行态目录。
DENIED_TREES = (Path("runtime"), Path("container/pi-agent/runtime"))
RETIRED_FILES = {
    Path("cairn/src/cairn/dispatcher/runtime/capture_tool.sh"),
    Path("cairn/src/cairn/dispatcher/runtime/traffic_capture.py"),
    Path("cairn/src/cairn/dispatcher/runtime/traffic_capture_addon.py"),
    Path("cairn/src/cairn/server/routers/traffic.py"),
    Path("cairn/src/cairn/server/services/traffic.py"),
    Path("container/traffic_capture.py"),
}
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"sk-ant-[A-Za-z0-9_-]{16,}"),
    re.compile(rb"ghp_[A-Za-z0-9]{20,}"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
)
STATIC_ROOT = PurePosixPath("cairn/src/cairn/server/static")
STATIC_TEXT_SUFFIXES = {".html", ".js", ".css"}
STATIC_ASSET_SUFFIXES = {
    ".css",
    ".gif",
    ".html",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".png",
    ".svg",
    ".ttf",
    ".wasm",
    ".webp",
    ".woff",
    ".woff2",
}
QUOTED_STATIC_REF = re.compile(
    r'''["']((?:/static/|assets/|\.\.?/)[^"'?#\s)]+)(?:\?[^"']*)?(?:#[^"']*)?["']'''
)
CSS_URL_REF = re.compile(r"url\(\s*(['\"]?)([^)'\"?#\s]+)(?:\?[^)'\"]*)?(?:#[^)'\"]*)?\1\s*\)")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def project_version() -> str:
    path = ROOT / "cairn/pyproject.toml"
    _reject_symlink(path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _excluded_relative(path: Path) -> bool:
    return (
        any(path.is_relative_to(root) for root in DENIED_TREES)
        or any(part in DENIED_PARTS for part in path.parts)
        or path.name.lower() in DENIED_NAMES
        or path.name.lower().startswith(".env.")
        or path.suffix.lower() in DENIED_SUFFIXES
    )


def validate_relative(path: Path) -> None:
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"非法发布路径: {path}")
    if _excluded_relative(path):
        raise RuntimeError(f"禁止进入发布包: {path}")


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(f"发布包不允许符号链接: {path.relative_to(ROOT)}")


def scan_content(path: Path, data: bytes) -> None:
    for pattern in SECRET_PATTERNS:
        if pattern.search(data):
            raise RuntimeError(f"文件疑似包含密钥，拒绝打包: {path}")


def collect_files() -> dict[str, bytes]:
    collected: dict[str, bytes] = {}
    for name in ROOT_FILES:
        path = ROOT / name
        _reject_symlink(path)
        if path.is_file():
            collected[name] = path.read_bytes()
    for relative in EXACT_FILES:
        path = ROOT / relative
        _reject_symlink(path)
        if not path.is_file():
            raise RuntimeError(f"缺少发布必需文件: {relative}")
        collected[relative.as_posix()] = path.read_bytes()
    for tree_root in TREE_ROOTS:
        absolute_root = ROOT / tree_root
        _reject_symlink(absolute_root)
        if not absolute_root.is_dir():
            raise RuntimeError(f"缺少发布目录: {tree_root}")
        for directory, directories, filenames in os.walk(absolute_root, followlinks=False):
            parent = Path(directory)
            # 先剪枝，不遍历本地node_modules的bin链接、构建产物或运行数据。
            directories[:] = sorted(
                name for name in directories
                if not _excluded_relative((parent / name).relative_to(ROOT))
            )
            for name in sorted([*directories, *filenames]):
                path = parent / name
                relative = path.relative_to(ROOT)
                if _excluded_relative(relative):
                    continue
                # 只有被排除的输入可跳过；发布源码中的文件和目录链接仍拒绝。
                _reject_symlink(path)
                if not path.is_file() or relative in RETIRED_FILES:
                    continue
                validate_relative(relative)
                collected[relative.as_posix()] = path.read_bytes()
    for relative, data in collected.items():
        scan_content(Path(relative), data)
    forbidden = {"dispatch.yaml", ".env"}
    if forbidden.intersection(collected):
        raise RuntimeError("发布 allowlist 意外包含本地配置")
    return dict(sorted(collected.items()))


def _resolve_static_ref(source: PurePosixPath, reference: str) -> PurePosixPath | None:
    if reference.startswith("/static/"):
        candidate = STATIC_ROOT / reference.removeprefix("/static/")
    elif reference.startswith("assets/"):
        candidate = STATIC_ROOT / reference
    else:
        candidate = source.parent / reference
    normalized = PurePosixPath(posixpath.normpath(candidate.as_posix()))
    if normalized.suffix.lower() not in STATIC_ASSET_SUFFIXES:
        return None
    if not normalized.is_relative_to(STATIC_ROOT):
        raise RuntimeError(f"前端静态资源引用越界: {source} -> {reference}")
    return normalized


def validate_static(files: dict[str, bytes]) -> None:
    index_key = (STATIC_ROOT / "index.html").as_posix()
    if index_key not in files:
        raise RuntimeError("发布包缺少前端 static/index.html")

    missing: set[str] = set()
    for name, data in files.items():
        source = PurePosixPath(name)
        if not source.is_relative_to(STATIC_ROOT) or source.suffix.lower() not in STATIC_TEXT_SUFFIXES:
            continue
        content = data.decode("utf-8", errors="replace")
        references = set(QUOTED_STATIC_REF.findall(content))
        references.update(match[1] for match in CSS_URL_REF.findall(content))
        for reference in references:
            resolved = _resolve_static_ref(source, reference)
            if resolved is not None and resolved.as_posix() not in files:
                missing.add(f"{source.relative_to(STATIC_ROOT)} -> {resolved.relative_to(STATIC_ROOT)}")
    if missing:
        raise RuntimeError("前端静态资源引用缺失: " + ", ".join(sorted(missing)))


def manifest(version: str, files: dict[str, bytes]) -> dict[str, object]:
    return {
        "format": "arena-code-release/v1",
        "version": version,
        "expected_cairn_agent_version": EXPECTED_CAIRN_AGENT_VERSION,
        "expected_pi_sdk_version": EXPECTED_PI_SDK_VERSION,
        "contents": {
            "worker_image": "excluded",
            "frontend_source": "excluded",
            "frontend_static": "included",
            "runtime_data": "excluded",
            "secrets": "excluded",
        },
        "files": [
            {"path": path, "size": len(data), "sha256": sha256_bytes(data)}
            for path, data in files.items()
        ],
    }


def zip_info(name: str, executable: bool) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, FIXED_ZIP_TIME)
    mode = 0o755 if executable else 0o644
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def build_release(output_dir: Path | None = None) -> tuple[Path, Path]:
    version = project_version()
    files = collect_files()
    validate_static(files)
    manifest_data = json.dumps(manifest(version, files), ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    output = output_dir or (ROOT / "dist")
    output.mkdir(parents=True, exist_ok=True)
    (output / "RELEASE-MANIFEST.json").write_bytes(manifest_data)
    archive_name = f"arena-{version}-linux-code-only.zip"
    archive_path = output / archive_name
    prefix = f"arena-{version}-linux-code-only"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative, data in files.items():
            executable = relative.startswith("deploy/") and Path(relative).suffix in {".sh", ".py"}
            archive.writestr(zip_info(f"{prefix}/{relative}", executable), data)
        archive.writestr(zip_info(f"{prefix}/RELEASE-MANIFEST.json", False), manifest_data)
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    sidecar = output / f"{archive_name}.sha256"
    sidecar.write_text(f"{digest}  {archive_name}\n", encoding="utf-8")
    return archive_path, sidecar


def main() -> int:
    archive, sidecar = build_release()
    print(archive)
    print(sidecar)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
