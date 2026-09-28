from __future__ import annotations

from unittest.mock import Mock

import pytest
from docker.errors import APIError, ImageNotFound

from browser_integration_support import local_docker_client, resolve_browser_image


@pytest.fixture
def docker_factory(monkeypatch):
    # 仅允许本地镜像查询和关闭连接，避免单测意外访问 Docker 或拉取镜像。
    client = Mock(spec=["images", "close"])
    client.images = Mock(spec=["get"])
    factory = Mock(return_value=client)
    monkeypatch.setattr("browser_integration_support.docker.DockerClient", factory)
    monkeypatch.setattr(
        "browser_integration_support.docker.from_env",
        Mock(side_effect=AssertionError("Docker environment discovery is forbidden")),
    )
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote-docker.invalid:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "remote-context")
    return factory


def test_browser_docker_client_ignores_remote_environment(docker_factory):
    assert local_docker_client() is docker_factory.return_value

    docker_factory.assert_called_once_with(base_url="unix:///var/run/docker.sock", timeout=30)


@pytest.mark.parametrize("candidate", [None, "", " ", "\t\n"])
def test_opt_in_requires_nonblank_browser_image(monkeypatch, docker_factory, candidate):
    monkeypatch.setenv("CAIRN_TEST_BROWSER_INTEGRATION", "1")
    if candidate is None:
        monkeypatch.delenv("CAIRN_TEST_BROWSER_IMAGE", raising=False)
    else:
        monkeypatch.setenv("CAIRN_TEST_BROWSER_IMAGE", candidate)

    with pytest.raises(pytest.fail.Exception, match="CAIRN_TEST_BROWSER_IMAGE"):
        resolve_browser_image()

    docker_factory.assert_not_called()


@pytest.mark.parametrize("opt_in", [None, "", "0", "true", " 1 "])
@pytest.mark.parametrize("candidate", [None, "", "cairn-browser-candidate:test"])
def test_without_explicit_opt_in_skips_before_docker(monkeypatch, docker_factory, opt_in, candidate):
    if opt_in is None:
        monkeypatch.delenv("CAIRN_TEST_BROWSER_INTEGRATION", raising=False)
    else:
        monkeypatch.setenv("CAIRN_TEST_BROWSER_INTEGRATION", opt_in)
    if candidate is None:
        monkeypatch.delenv("CAIRN_TEST_BROWSER_IMAGE", raising=False)
    else:
        monkeypatch.setenv("CAIRN_TEST_BROWSER_IMAGE", candidate)

    with pytest.raises(pytest.skip.Exception, match="opt-in"):
        resolve_browser_image()

    docker_factory.assert_not_called()


@pytest.mark.parametrize("candidate", [
    "cairn-browser-candidate:test",
    "  cairn-browser-candidate:test\t",
    "registry.invalid/cairn/browser@sha256:" + "b" * 64,
    "sha256:" + "a" * 64,
])
def test_explicit_candidate_resolves_to_local_image_id(monkeypatch, docker_factory, candidate):
    monkeypatch.setenv("CAIRN_TEST_BROWSER_INTEGRATION", "1")
    monkeypatch.setenv("CAIRN_TEST_BROWSER_IMAGE", candidate)
    client = docker_factory.return_value
    client.images.get.return_value.id = "sha256:" + "a" * 64

    assert resolve_browser_image() == "sha256:" + "a" * 64

    docker_factory.assert_called_once_with(base_url="unix:///var/run/docker.sock", timeout=30)
    client.images.get.assert_called_once_with(candidate.strip())
    client.close.assert_called_once_with()


@pytest.mark.parametrize("error_type", [ImageNotFound, APIError])
def test_local_image_lookup_failure_does_not_pull_or_fallback(monkeypatch, docker_factory, error_type):
    monkeypatch.setenv("CAIRN_TEST_BROWSER_INTEGRATION", "1")
    monkeypatch.setenv("CAIRN_TEST_BROWSER_IMAGE", "cairn-browser-candidate:missing")
    client = docker_factory.return_value
    error = error_type("local candidate lookup failed")
    client.images.get.side_effect = error

    with pytest.raises(error_type) as raised:
        resolve_browser_image()

    assert raised.value is error
    docker_factory.assert_called_once_with(base_url="unix:///var/run/docker.sock", timeout=30)
    client.images.get.assert_called_once_with("cairn-browser-candidate:missing")
    client.close.assert_called_once_with()
