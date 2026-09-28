"""真实浏览器验收的显式镜像配置入口，单测不需要连接 Docker。"""

from __future__ import annotations

import os

import docker
import pytest


def local_docker_client() -> docker.DockerClient:
    # 固定本机 socket，避免 DOCKER_HOST/DOCKER_CONTEXT 将验收连接到其他 daemon。
    return docker.DockerClient(base_url="unix:///var/run/docker.sock", timeout=30)


def resolve_browser_image() -> str:
    if os.environ.get("CAIRN_TEST_BROWSER_INTEGRATION") != "1":
        pytest.skip("requires explicit isolated Docker/browser integration opt-in")

    candidate = os.environ.get("CAIRN_TEST_BROWSER_IMAGE", "").strip()
    if not candidate:
        pytest.fail("CAIRN_TEST_BROWSER_IMAGE must explicitly name a local candidate image")

    client = local_docker_client()
    try:
        # 仅查询本地候选镜像，固定 ID 后不受 tag 后续移动影响；禁止拉取或回退。
        return client.images.get(candidate).id
    finally:
        client.close()
