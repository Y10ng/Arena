#!/usr/bin/env python3
"""Arena 代码发布包完整性与运行环境验收。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

EXPECTED_CAIRN_AGENT_VERSION = "1.0.0"
EXPECTED_PI_SDK_VERSION = "0.73.0"
PLACEHOLDERS = ("REPLACE_ME", "REPLACE_WITH_")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_manifest(root: Path) -> dict[str, Any]:
    manifest_path = root / "RELEASE-MANIFEST.json"
    if not manifest_path.is_file():
        raise RuntimeError("缺少 RELEASE-MANIFEST.json；请使用官方 package_release.py 生成的 ZIP")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = manifest.get("files")
    if not isinstance(files, list):
        raise RuntimeError("发布 manifest 的 files 非法")
    resolved_root = root.resolve()
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise RuntimeError("发布 manifest 文件项非法")
        relative = entry.get("path")
        expected_size = entry.get("size")
        expected_hash = entry.get("sha256")
        if not isinstance(relative, str) or not relative or not isinstance(expected_size, int) or not isinstance(expected_hash, str):
            raise RuntimeError("发布 manifest 文件项缺少 path/size/sha256")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts or relative in seen:
            raise RuntimeError(f"发布 manifest 文件路径非法或重复: {relative}")
        seen.add(relative)
        path = (resolved_root / relative_path).resolve()
        if not path.is_relative_to(resolved_root) or not path.is_file():
            raise RuntimeError(f"发布文件缺失或越界: {relative}")
        actual_size = path.stat().st_size
        if actual_size != expected_size:
            raise RuntimeError(f"发布文件大小不匹配: {relative}")
        if sha256_file(path) != expected_hash:
            raise RuntimeError(f"发布文件 SHA-256 不匹配: {relative}")
    return manifest


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RuntimeError(f"环境文件行格式非法: {raw_line}")
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def reject_placeholders(*paths: Path) -> None:
    for path in paths:
        content = path.read_text(encoding="utf-8")
        if any(marker in content for marker in PLACEHOLDERS):
            raise RuntimeError(f"配置仍包含占位符: {path}")


def check_static_assets(root: Path) -> None:
    static_root = root / "cairn/src/cairn/server/static"
    index_path = static_root / "index.html"
    if not index_path.is_file():
        raise RuntimeError("缺少前端 static/index.html")
    content = index_path.read_text(encoding="utf-8")
    references = set(re.findall(r"/static/([^\"']+)", content))
    missing = [reference for reference in references if not (static_root / reference).is_file()]
    if missing:
        raise RuntimeError("前端静态资源缺失: " + ", ".join(sorted(missing)))


def run_command(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)


def verify_image(image: str, expected_version: str, expected_sdk_version: str) -> dict[str, str]:
    inspect = run_command(
        ["docker", "image", "inspect", image, "--format", "{{.Id}} {{.Os}} {{.Architecture}}"],
        timeout=30,
    )
    if inspect.returncode != 0:
        raise RuntimeError(f"Worker 镜像不存在或不可读: {image}")
    parts = inspect.stdout.strip().split()
    if len(parts) != 3 or parts[1] != "linux":
        raise RuntimeError(f"Worker 镜像平台异常: {inspect.stdout.strip()}")
    # 离线构建验收不注入凭据、不启用网络，也不执行模型请求。
    command = ["docker", "run", "--rm", "--network", "none", "--entrypoint", "cairn-agent", image]
    version = run_command([*command, "--version"], timeout=30)
    expected_output = f"cairn-agent {expected_version} (pi-coding-agent {expected_sdk_version})"
    if version.returncode != 0 or version.stdout.strip() != expected_output:
        raise RuntimeError(f"cairn-agent 版本不匹配或命令失败，期望 {expected_output}")
    result = run_command([*command, "--self-check"], timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"cairn-agent 离线自检失败，exit={result.returncode}")
    try:
        check = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("cairn-agent 离线自检返回非法 JSON") from exc
    if (
        not isinstance(check, dict)
        or check.get("status") != "ok"
        or check.get("engine") != "pi"
        or check.get("sdk_version") != expected_sdk_version
        or check.get("offline") is not True
    ):
        raise RuntimeError("cairn-agent 离线自检契约不匹配")
    return {"id": parts[0], "os": parts[1], "architecture": parts[2], "agent_version": expected_output}


def verify_runtime(root: Path, manifest: dict[str, Any], *, full: bool) -> None:
    runtime = root / "runtime"
    dispatch_path = runtime / "dispatch.yaml"
    env_path = runtime / "arena.env"
    if not dispatch_path.is_file() or not env_path.is_file():
        raise RuntimeError("缺少 runtime/dispatch.yaml 或 runtime/arena.env，请先运行 deploy/install.sh")
    reject_placeholders(dispatch_path, env_path)
    env_values = load_env_file(env_path)
    os.environ.update(env_values)

    import yaml

    from cairn.dispatcher.config import DispatchConfig
    from cairn.dispatcher.tasks import process_runner

    raw_config = yaml.safe_load(dispatch_path.read_text(encoding="utf-8")) or {}
    # 历史 engine/type 仅用于明确拒绝，不转换为可执行的 pi Worker。
    for worker in raw_config.get("workers", []):
        if any(worker.get(key) not in (None, "pi") for key in ("engine", "type")):
            raise RuntimeError("Worker 配置不可执行：engine/type 仅允许 pi，请重新配置历史 Worker")
    try:
        config = DispatchConfig.load(dispatch_path)
    except ValueError as exc:
        # 配置校验异常可能包含凭据输入，不将原始内容输出到验收日志。
        raise RuntimeError("Worker 配置不可执行：请检查 pi 配置及模型绑定") from exc
    for worker in config.workers:
        if worker.enabled and worker.model_binding is None:
            raise RuntimeError(f"Worker {worker.id} 不可执行：缺少结构化 pi 模型绑定")

    source_path = Path(process_runner.__file__).resolve()
    expected_root = (root / "cairn/src/cairn").resolve()
    if not source_path.is_relative_to(expected_root):
        raise RuntimeError(f"加载了发布目录外的 process_runner: {source_path}")

    print(f"process_runner={source_path}")
    print(f"process_runner_sha256={sha256_file(source_path)}")
    print(f"enabled_worker_count={sum(1 for worker in config.workers if worker.enabled)}")
    pi_workers = [worker for worker in config.workers if worker.enabled and worker.engine == "pi"]
    print(f"enabled_pi_worker_count={len(pi_workers)}")
    print(f"disabled_pi_worker_count={sum(not worker.enabled for worker in config.workers)}")
    if not pi_workers:
        print("警告: 没有 enabled 静态 pi Worker；Server durable Worker 绑定及可用性需另行验收", file=sys.stderr)
    if not full:
        # 默认验收不触碰 Docker；--full 也仅做离线构建自检。
        return

    expected_version = str(manifest.get("expected_cairn_agent_version") or EXPECTED_CAIRN_AGENT_VERSION)
    expected_sdk_version = str(manifest.get("expected_pi_sdk_version") or EXPECTED_PI_SDK_VERSION)
    images = sorted({entry.image for entry in config.images} | {config.container.image})
    image_info = {image: verify_image(image, expected_version, expected_sdk_version) for image in images}
    for image, info in image_info.items():
        print(f"worker_image={image} id={info['id']} arch={info['architecture']} agent={info['agent_version']}")
    print("offline_self_check=ok；未验证真实模型调用或 Worker 在线可用性")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest-only", action="store_true")
    parser.add_argument("--full", action="store_true", help="验证 Worker 镜像及 cairn-agent/pi SDK 离线自检，不调用模型")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        manifest = verify_manifest(root)
        check_static_assets(root)
        if not args.manifest_only:
            verify_runtime(root, manifest, full=args.full)
    except Exception as exc:
        print(f"验收失败: {exc}", file=sys.stderr)
        return 1
    print("验收通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
