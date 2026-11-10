"""镜像准备不修改运行配置，停服后的切换保留定制引用与配置并发边界。"""

import importlib.util
import json
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("release_images", ROOT / "deploy/prepare_release_images.py")
images = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(images)


@pytest.fixture
def installation(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    (target / "runtime").mkdir(parents=True)
    specs = {role: {"image": "yong29/arena-" + ("worker" if role == "worker" else "traffic-gateway") + ":0.2.7",
                    "digest": "sha256:" + letter * 64}
             for role, letter in (("worker", "a"), ("traffic_gateway", "b"))}
    (source / "RELEASE-MANIFEST.json").write_text(json.dumps({"images": specs}))
    config = yaml.safe_load((ROOT / "deploy/dispatch.yaml.example").read_text())
    config["container"]["image"] = "arena-worker:0.2.3"
    config["container"]["allowed_images"] = ["yong29/arena-worker:git-old", "custom/worker:mine"]
    config["images"] = [{"id": "default", "image": "arena-worker:0.2.3"},
                        {"id": "custom", "image": "custom/worker:mine"}]
    config["common_env"] = {"SYNTHETIC_VALUE": "unchanged"}
    path = target / "runtime/dispatch.yaml"
    path.write_text(yaml.safe_dump(config))
    monkeypatch.setattr(images, "verify", Mock())
    monkeypatch.setattr(images, "image_id", lambda image: "id:" + image)
    return source, target, tmp_path / "plan.json", path


def test_prepare_then_apply_preserves_custom_and_all_image_bindings(installation):
    source, target, plan, path = installation
    original = path.read_bytes()
    images.prepare(source, target, plan)
    assert path.read_bytes() == original
    assert "SYNTHETIC_VALUE" not in plan.read_text()
    images.apply(target, plan, check_only=True)
    assert path.read_bytes() == original
    images.apply(target, plan)
    result = yaml.safe_load(path.read_text())
    worker = "yong29/arena-worker@sha256:" + "a" * 64
    assert result["container"]["image"] == worker
    assert result["container"]["allowed_images"] == [worker, "custom/worker:mine"]
    assert result["images"][0]["image"] == worker
    assert result["images"][1]["image"] == "custom/worker:mine"
    assert result["common_env"] == {"SYNTHETIC_VALUE": "unchanged"}


def test_incompatible_custom_worker_fails_before_config_change(installation, monkeypatch):
    source, target, plan, path = installation
    original = path.read_bytes()
    monkeypatch.setattr(images, "verify", Mock(side_effect=RuntimeError("owner protocol missing")))
    with pytest.raises(RuntimeError, match="旧服务未停止"):
        images.prepare(source, target, plan)
    assert path.read_bytes() == original
    assert not plan.exists()


def test_config_changed_during_prepare_is_not_overwritten(installation):
    source, target, plan, path = installation
    images.prepare(source, target, plan)
    path.write_text(path.read_text() + "\n# concurrent edit\n")
    changed = path.read_bytes()
    with pytest.raises(RuntimeError, match="配置已变化"):
        images.apply(target, plan)
    assert path.read_bytes() == changed


def test_missing_images_pull_by_digest_not_mutable_tag(installation, monkeypatch):
    source, target, plan, path = installation
    attempts = set()
    pulls = []
    def identity(ref):
        if "@" in ref and ref not in attempts:
            attempts.add(ref)
            raise subprocess.CalledProcessError(1, "inspect")
        return "id:" + ref
    monkeypatch.setattr(images, "image_id", identity)
    monkeypatch.setattr(images.subprocess, "run", lambda command, **kwargs: pulls.append(command))
    images.prepare(source, target, plan)
    assert len(pulls) == 2
    assert all(command[:2] == ["docker", "pull"] and "@sha256:" in command[2] for command in pulls)


def test_missing_worker_metadata_rejected_without_docker(installation):
    source, target, plan, path = installation
    (source / "RELEASE-MANIFEST.json").write_text(json.dumps({"images": {}}))
    with pytest.raises(RuntimeError, match="worker"):
        images.prepare(source, target, plan)


def test_local_custom_tag_and_omitted_gateway(installation):
    source, target, plan, path = installation
    config = yaml.safe_load(path.read_text())
    config["container"]["image"] = "arena-worker:custom"
    del config["container"]["traffic_gateway_image"]
    path.write_text(yaml.safe_dump(config))
    original = path.read_bytes()
    images.prepare(source, target, plan)
    assert path.read_bytes() == original
    images.apply(target, plan)
    result = yaml.safe_load(path.read_text())
    assert result["container"]["image"] == "arena-worker:custom"
    assert result["container"]["traffic_gateway_image"] == "yong29/arena-traffic-gateway@sha256:" + "b" * 64


@pytest.mark.parametrize("ref, expected", [
    ("docker.io/yong29/arena-worker:latest", "yong29/arena-worker"),
    ("registry.example:5000/team/worker:tag", "registry.example:5000/team/worker"),
    ("yong29/arena-worker@sha256:" + "a" * 64, "yong29/arena-worker"),
])
def test_repository(ref, expected):
    assert images.repository(ref) == expected


def test_image_identity_change_blocks_apply(installation, monkeypatch):
    source, target, plan, path = installation
    images.prepare(source, target, plan)
    monkeypatch.setattr(images, "image_id", lambda ref: "other-id")
    with pytest.raises(RuntimeError, match="镜像身份已变化"):
        images.apply(target, plan)
