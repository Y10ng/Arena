#!/usr/bin/env python3
"""新包负责配套镜像的停服前准备与停服后切换，旧版 Web 引导也走同一路径。"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from cairn.dispatcher.config import ContainerConfig
from cairn.server.services.dispatcher_config_store import DispatcherConfigStore

# -I 不加载脚本目录；仅引入同一份已校验发布包中的验收器。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_deployment import EXPECTED_CAIRN_AGENT_VERSION, EXPECTED_PI_SDK_VERSION, verify_gateway_image, verify_image

OFFICIAL = {
    "worker": {"arena-worker", "yong29/arena-worker"},
    "traffic_gateway": {"arena-traffic-gateway", "yong29/arena-traffic-gateway"},
}


def repository(ref: str) -> str:
    name = ref.split("@", 1)[0]
    if ":" in name.rsplit("/", 1)[-1]:
        name = name.rsplit(":", 1)[0]
    return name.removeprefix("docker.io/").removeprefix("index.docker.io/")


def is_official(role: str, ref: str) -> bool:
    name = repository(ref)
    if name not in OFFICIAL[role]:
        return False
    if name.startswith("yong29/"):
        return True
    # 本地同名仓库也可能承载定制 tag，只迁移默认名和正式版本标签。
    tag = ref.rsplit(":", 1)[-1] if ":" in ref else "latest"
    return tag in {"local", "latest"} or re.fullmatch(r"v?\d+\.\d+\.\d+", tag) is not None


def release_images(source: Path) -> dict[str, str]:
    manifest = json.loads((source / "RELEASE-MANIFEST.json").read_text())
    result = {}
    for role in OFFICIAL:
        spec = manifest.get("images", {}).get(role, {})
        image, digest = spec.get("image"), spec.get("digest")
        if (not isinstance(image, str) or not image or image.startswith("-")
                or any(char.isspace() for char in image)
                or not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)):
            raise RuntimeError(f"发布包缺少有效的配套镜像: {role}")
        result[role] = repository(image) + "@" + digest
    return result


def image_slots(config: dict):
    container = config["container"]
    # 合法旧配置可省略网关字段，沿用运行时唯一缺省值后参与本次迁移。
    container.setdefault("traffic_gateway_image", ContainerConfig.model_fields["traffic_gateway_image"].default)
    yield "worker", container, "image"
    yield "traffic_gateway", container, "traffic_gateway_image"
    for index in range(len(container.get("allowed_images", []))):
        yield "worker", container["allowed_images"], index
    for image in config.get("images", []):
        yield "worker", image, "image"


def verify(role: str, image: str) -> None:
    if role == "worker":
        verify_image(image, EXPECTED_CAIRN_AGENT_VERSION, EXPECTED_PI_SDK_VERSION)
    else:
        verify_gateway_image(image)


def image_id(image: str) -> str:
    return subprocess.check_output(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
        text=True, stderr=subprocess.DEVNULL, timeout=30,
    ).strip()


def prepare(source: Path, target: Path, plan_path: Path) -> None:
    refs = release_images(source)
    store = DispatcherConfigStore(target / "runtime/dispatch.yaml")
    config, _, revision = store.read()
    wanted = set()
    replacements = {}
    for role, parent, key in image_slots(config):
        current = parent[key]
        if is_official(role, current):
            replacement = refs[role]
            replacements[current] = replacement
            wanted.add((role, replacement, True))
        else:
            # 真正定制的镜像保留原引用，只验收；不能把定制能力换成官方默认。
            wanted.add((role, current, False))
    identities = {}
    for role, image, managed in sorted(wanted):
        if managed:
            try:
                image_id(image)
            except subprocess.CalledProcessError:
                print(f"准备配套 {role} 镜像（复用已有层）", flush=True)
                subprocess.run(["docker", "pull", image], check=True, timeout=1800)
        try:
            verify(role, image)
        except RuntimeError as exc:
            raise RuntimeError(f"{role} 镜像不配套，旧服务未停止: {image}；{exc}") from None
        identities[image] = image_id(image)
    # 计划只保存引用和不可逆 revision，不复制配置正文或凭据。
    plan = {"revision": revision, "replacements": replacements, "identities": identities}
    with plan_path.open("w", encoding="utf-8") as output:
        os.chmod(plan_path, 0o600)
        json.dump(plan, output)
    print("配套镜像已准备并通过离线检查，运行配置未变更", flush=True)


def apply(target: Path, plan_path: Path, *, check_only: bool = False) -> None:
    plan = json.loads(plan_path.read_text())
    store = DispatcherConfigStore(target / "runtime/dispatch.yaml")
    config, _, revision = store.read()
    if revision != plan["revision"]:
        raise RuntimeError("准备期间 Dispatcher 配置已变化，请重新执行更新；不覆盖新配置")
    for image, expected in plan["identities"].items():
        if image_id(image) != expected:
            raise RuntimeError("准备期间镜像身份已变化，请重新执行更新")
    if check_only:
        return
    updated = deepcopy(config)
    for _, parent, key in image_slots(updated):
        parent[key] = plan["replacements"].get(parent[key], parent[key])
    if updated != config:
        store.write_config(updated, expected_revision=revision)
    print("已切换配套镜像引用；未替换已有项目容器", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "check", "apply"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.phase == "prepare":
            prepare(args.source, args.target, args.plan)
        else:
            apply(args.target, args.plan, check_only=args.phase == "check")
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"镜像更新失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
