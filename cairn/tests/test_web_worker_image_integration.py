"""候选 Web 镜像的独立 opt-in 验收；仅新建测试容器，不拉镜像或接触业务挂载。"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tarfile
import time
import uuid
from unittest.mock import Mock

import docker
from docker.errors import ImageNotFound
import pytest


FIXTURE = Path(__file__).parent / "fixtures" / "web_worker_image_smoke.py"
SOCKET = "unix:///var/run/docker.sock"
POLL_SECONDS = 280


def _run_smoke(evidence: Path) -> dict:
    if os.environ.get("CAIRN_TEST_WEB_IMAGE_INTEGRATION") != "1":
        pytest.skip("requires explicit CAIRN_TEST_WEB_IMAGE_INTEGRATION=1 opt-in")
    candidate = os.environ.get("CAIRN_TEST_BROWSER_IMAGE", "").strip()
    if not candidate:
        pytest.fail("CAIRN_TEST_BROWSER_IMAGE must explicitly name a local candidate image")

    # 不读取 DOCKER_HOST/TLS 配置，且每个 API 请求都有超时；不借用浏览器的 opt-in。
    client = docker.DockerClient(base_url=SOCKET, timeout=5)
    container = None
    try:
        image = client.images.get(candidate)
        assert (image.attrs["Os"], image.attrs["Architecture"]) == ("linux", "amd64")
        config = image.attrs["Config"]
        assert config["User"] == "kali" and config["WorkingDir"] == "/home/kali/workspace"
        assert {key: config["Labels"].get(key) for key in (
            "io.cairn.agent.engine", "io.cairn.agent.version", "io.cairn.pi-sdk.version",
        )} == {
            "io.cairn.agent.engine": "pi", "io.cairn.agent.version": "1.0.0",
            "io.cairn.pi-sdk.version": "0.73.0",
        }
        assert not config.get("Volumes"), "candidate must not declare volumes"
        # 固定本地 ID，create 不会像 run 那样在 ImageNotFound 时自动 pull。
        container = client.containers.create(
            image.id,
            command=[],
            entrypoint=[
                "/usr/bin/env", "-i",
                "PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/home/kali", "LANG=C.UTF-8",
                "NO_UPDATE_NOTIFIER=1", "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1",
                "CI=1", "PYTHONUNBUFFERED=1",
                "timeout", "--signal=TERM", "--kill-after=5s", "240s",
                "python3", "/tmp/cairn-web-image-smoke.py",
            ],
            user="kali", working_dir="/home/kali/workspace",
            name="cairn-web-image-test-" + uuid.uuid4().hex,
            labels={"cairn.test": "web-worker-image-integration"},
            network_mode="none", volumes={}, cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"], init=True,
            pids_limit=128, mem_limit="2g", nano_cpus=2_000_000_000,
        )
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as bundle:
            source = FIXTURE.read_bytes()
            entry = tarfile.TarInfo("cairn-web-image-smoke.py")
            entry.size, entry.mode = len(source), 0o444
            bundle.addfile(entry, io.BytesIO(source))
        assert container.put_archive("/tmp", archive.getvalue()), "fixture upload failed"
        container.start()
        # fixture 作为主进程运行，不使用可能无限等待升级连接的 Docker exec/wait。
        deadline = time.monotonic() + POLL_SECONDS
        while time.monotonic() < deadline:
            container.reload()
            state = container.attrs["State"]
            if state["Status"] in {"exited", "dead"}:
                break
            time.sleep(0.5)
        else:
            pytest.fail(f"Web image smoke exceeded {POLL_SECONDS}s host deadline")
        assert not container.attrs.get("Mounts"), "smoke must have no mounts"
        assert state["ExitCode"] == 0, f"fixture failed: {state}; evidence: {evidence}"
        output = container.logs(stdout=True, stderr=True, tail=200).decode("utf-8", errors="replace")
        reports = [line.removeprefix("WEB_IMAGE_SMOKE=") for line in output.splitlines()
                   if line.startswith("WEB_IMAGE_SMOKE=")]
        assert len(reports) == 1, output
        report = json.loads(reports[0])
        assert report["status"] == "ok", report
        return report
    finally:
        try:
            if container is not None:
                try:
                    # 只保留合成证据的有限日志；失败路径同样尝试取证，绝不枚举/清理其他容器。
                    evidence.write_bytes(container.logs(stdout=True, stderr=True, tail=200))
                finally:
                    container.remove(force=True, v=True)
        finally:
            client.close()


def test_candidate_web_worker_image(tmp_path):
    report = _run_smoke(tmp_path / "web-image-smoke.log")
    print("WEB_IMAGE_INTEGRATION=" + json.dumps(report, ensure_ascii=False))


# 这些单测只验证 Docker 边界与失败清理，不运行容器内工具。
@pytest.fixture
def local_docker(monkeypatch):
    client = Mock(spec=["images", "containers", "close"])
    client.images = Mock(spec=["get"])
    client.containers = Mock(spec=["create"])
    client.images.get.return_value.id = "sha256:" + "a" * 64
    client.images.get.return_value.attrs = {
        "Os": "linux", "Architecture": "amd64",
        "Config": {
            "User": "kali", "WorkingDir": "/home/kali/workspace",
            "Labels": {"io.cairn.agent.engine": "pi", "io.cairn.agent.version": "1.0.0",
                       "io.cairn.pi-sdk.version": "0.73.0"},
        },
    }
    container = client.containers.create.return_value
    container.attrs = {"State": {"Status": "exited", "ExitCode": 0}, "Mounts": []}
    container.logs.return_value = b'WEB_IMAGE_SMOKE={"status":"ok"}\n'
    factory = Mock(return_value=client)
    monkeypatch.setattr(docker, "DockerClient", factory)
    monkeypatch.setenv("CAIRN_TEST_WEB_IMAGE_INTEGRATION", "1")
    monkeypatch.setenv("CAIRN_TEST_BROWSER_IMAGE", " local-candidate:test ")
    return factory, client, container


@pytest.mark.parametrize("opt_in", [None, "", "0", "true", " 1 "])
def test_web_opt_in_is_independent(monkeypatch, tmp_path, local_docker, opt_in):
    monkeypatch.setenv("CAIRN_TEST_BROWSER_INTEGRATION", "1")
    if opt_in is None:
        monkeypatch.delenv("CAIRN_TEST_WEB_IMAGE_INTEGRATION")
    else:
        monkeypatch.setenv("CAIRN_TEST_WEB_IMAGE_INTEGRATION", opt_in)
    with pytest.raises(pytest.skip.Exception, match="opt-in"):
        _run_smoke(tmp_path / "evidence")
    local_docker[0].assert_not_called()


@pytest.mark.parametrize("candidate", [None, "", " \t\n"])
def test_web_opt_in_requires_image(monkeypatch, tmp_path, local_docker, candidate):
    if candidate is None:
        monkeypatch.delenv("CAIRN_TEST_BROWSER_IMAGE")
    else:
        monkeypatch.setenv("CAIRN_TEST_BROWSER_IMAGE", candidate)
    with pytest.raises(pytest.fail.Exception, match="CAIRN_TEST_BROWSER_IMAGE"):
        _run_smoke(tmp_path / "evidence")
    local_docker[0].assert_not_called()


def test_web_smoke_pins_local_image_and_removes_only_its_container(monkeypatch, tmp_path, local_docker):
    factory, client, container = local_docker
    monkeypatch.setenv("DOCKER_HOST", "tcp://must-not-be-used.invalid:2375")
    monkeypatch.delenv("CAIRN_TEST_BROWSER_INTEGRATION", raising=False)
    assert _run_smoke(tmp_path / "evidence") == {"status": "ok"}
    factory.assert_called_once_with(base_url=SOCKET, timeout=5)
    client.images.get.assert_called_once_with("local-candidate:test")
    args, kwargs = client.containers.create.call_args
    assert args == ("sha256:" + "a" * 64,)
    assert kwargs["network_mode"] == "none" and kwargs["volumes"] == {}
    assert kwargs["user"] == "kali" and kwargs["entrypoint"][:2] == ["/usr/bin/env", "-i"]
    assert b"cairn-web-image-smoke.py" in container.put_archive.call_args.args[1]
    container.remove.assert_called_once_with(force=True, v=True)
    client.close.assert_called_once_with()


def test_web_smoke_missing_local_image_never_creates_or_pulls(tmp_path, local_docker):
    _, client, container = local_docker
    client.images.get.side_effect = ImageNotFound("missing candidate")
    with pytest.raises(ImageNotFound):
        _run_smoke(tmp_path / "evidence")
    client.containers.create.assert_not_called()
    container.remove.assert_not_called()
    client.close.assert_called_once_with()


@pytest.mark.parametrize("field,value", [
    ("User", "root"), ("WorkingDir", "/tmp"), ("Volumes", {"/data": {}}),
    ("Labels", {"io.cairn.agent.engine": "legacy"}),
])
def test_web_smoke_rejects_incompatible_image_before_creating_container(tmp_path, local_docker, field, value):
    _, client, _ = local_docker
    client.images.get.return_value.attrs["Config"][field] = value
    with pytest.raises(AssertionError):
        _run_smoke(tmp_path / "evidence")
    client.containers.create.assert_not_called()
    client.close.assert_called_once_with()


@pytest.mark.parametrize("failure", ["upload", "start", "exit", "deadline", "logs"])
def test_web_smoke_failures_still_remove_owned_container(monkeypatch, tmp_path, local_docker, failure):
    _, client, container = local_docker
    expected = (AssertionError, RuntimeError, pytest.fail.Exception)
    if failure == "upload":
        container.put_archive.return_value = False
    elif failure == "start":
        container.start.side_effect = RuntimeError("start failed")
    elif failure == "exit":
        container.attrs["State"]["ExitCode"] = 1
    elif failure == "deadline":
        monkeypatch.setattr(time, "monotonic", Mock(side_effect=[0, POLL_SECONDS + 1]))
    else:
        container.logs.side_effect = RuntimeError("logs failed")
    with pytest.raises(expected):
        _run_smoke(tmp_path / "evidence")
    container.remove.assert_called_once_with(force=True, v=True)
    client.close.assert_called_once_with()
