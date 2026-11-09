from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "cairn/src"))

from cairn.server.services import system_update as su


# ---------------------------------------------------------------------------
# apply_update.py：以独立脚本方式加载（它必须保持 stdlib-only）
# ---------------------------------------------------------------------------

_SPEC = importlib.util.spec_from_file_location(
    "apply_update", ROOT / "deploy/apply_update.py")
assert _SPEC and _SPEC.loader
apply_update = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(apply_update)


def _write_state(path: Path, **fields) -> Path:
    payload = {
        "phase": "applying",
        "handover": {
            "staged_dir": str(path.parent / "staged"),
            "target_root": str(path.parent / "target"),
            "log_path": str(path.parent / "logs" / "update-apply.log"),
        },
    }
    payload.update(fields)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_apply_update_requires_complete_handover(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"phase": "applying", "handover": {}}), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "deploy/apply_update.py"), "--state", str(state)],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "交接信息不完整" in result.stderr


def test_apply_update_runs_staged_update_sh_and_records_done(tmp_path: Path) -> None:
    staged = tmp_path / "staged"
    (staged / "deploy").mkdir(parents=True)
    # stub update.sh：把参数原样落盘，证明执行的是 staged 副本且参数正确传递
    (staged / "deploy/update.sh").write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$STUB_OUT\"\n",
        encoding="utf-8",
    )
    (staged / "deploy/update.sh").chmod(0o755)
    # apply_update.py 通常从 staged 包内执行；复制过去模拟真实布局
    (staged / "deploy/apply_update.py").write_bytes(
        (ROOT / "deploy/apply_update.py").read_bytes())
    target = tmp_path / "target"
    target.mkdir()
    state = _write_state(tmp_path / "state.json")
    state_data = json.loads(state.read_text())
    state_data["handover"]["staged_dir"] = str(staged)
    state_data["handover"]["target_root"] = str(target)
    state.write_text(json.dumps(state_data), encoding="utf-8")

    stub_out = tmp_path / "stub-out.txt"
    env = dict(os.environ, STUB_OUT=str(stub_out))
    result = subprocess.run(
        [sys.executable, str(staged / "deploy/apply_update.py"), "--state", str(state)],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, result.stderr
    args = stub_out.read_text().splitlines()
    assert "--target" in args and str(target) in args
    final = json.loads(state.read_text())
    assert final["phase"] == "done"
    assert final["apply_exit_code"] == 0


def test_apply_update_records_failed_on_nonzero(tmp_path: Path) -> None:
    staged = tmp_path / "staged"
    (staged / "deploy").mkdir(parents=True)
    (staged / "deploy/update.sh").write_text("#!/usr/bin/env bash\nexit 3\n", encoding="utf-8")
    (staged / "deploy/update.sh").chmod(0o755)
    (staged / "deploy/apply_update.py").write_bytes(
        (ROOT / "deploy/apply_update.py").read_bytes())
    target = tmp_path / "target"
    target.mkdir()
    state = _write_state(tmp_path / "state.json")
    state_data = json.loads(state.read_text())
    state_data["handover"]["staged_dir"] = str(staged)
    state_data["handover"]["target_root"] = str(target)
    state.write_text(json.dumps(state_data), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(staged / "deploy/apply_update.py"), "--state", str(state)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    final = json.loads(state.read_text())
    assert final["phase"] == "failed"
    assert final["apply_exit_code"] == 3


# ---------------------------------------------------------------------------
# 版本比较与 release.json 校验
# ---------------------------------------------------------------------------

def test_parse_version_strict() -> None:
    assert su.parse_version("0.2.5") == (0, 2, 5)
    assert su.parse_version("1.0.0") == (1, 0, 0)
    assert su.parse_version("0.2.5-rc1") is None
    assert su.parse_version("v0.2.5") is None
    assert su.parse_version("0.2") is None
    assert su.parse_version(None) is None
    assert su.parse_version("") is None


def test_compare_versions() -> None:
    assert su.compare_versions("0.2.5", "0.2.6") == -1
    assert su.compare_versions("0.2.5", "0.2.5") == 0
    assert su.compare_versions("0.2.10", "0.2.9") == 1
    with pytest.raises(su.UpdateError):
        su.compare_versions("dev", "0.2.6")


def test_validate_channel_doc_accepts_valid() -> None:
    doc = {
        "format": "arena-release-channel/v1",
        "version": "0.2.6",
        "notes_url": "https://example.com/releases/tag/v0.2.6",
        "app": {"asset": "arena-0.2.6-linux-code-only.zip", "sha256": "a" * 64, "size": 1},
        "worker": {"reuse_version": True},
        "images": {"traffic_gateway": {"image": "yong29/arena-traffic-gateway:latest",
                                        "digest": "sha256:" + "b" * 64}},
    }
    assert su.validate_channel_doc(doc) == doc


@pytest.mark.parametrize("mutation", [
    {"format": "wrong"},
    {"version": "not-a-version"},
    {"app": {"asset": 1}},
    {"app": {"asset": "x.zip", "sha256": "short"}},
    {"images": {}},
    {"images": {"traffic_gateway": {"image": "img", "digest": "md5:xx"}}},
])
def test_validate_channel_doc_rejects_invalid(mutation: dict) -> None:
    doc = {
        "format": "arena-release-channel/v1",
        "version": "0.2.6",
        "notes_url": "https://example.com",
        "app": {"asset": "arena-0.2.6-linux-code-only.zip", "sha256": "a" * 64, "size": 1},
        "worker": {"reuse_version": True},
        "images": {"traffic_gateway": {"image": "yong29/arena-traffic-gateway:latest",
                                        "digest": "sha256:" + "b" * 64}},
    }
    doc.update(mutation)
    with pytest.raises(su.UpdateError):
        su.validate_channel_doc(doc)


def test_asset_url_rejects_escape() -> None:
    assert su._asset_url("http://x/ch/release.json", "arena.zip") == "http://x/ch/arena.zip"
    with pytest.raises(su.UpdateError):
        su._asset_url("http://x/ch/release.json", "..")
    with pytest.raises(su.UpdateError):
        su._asset_url("http://x/ch/release.json", "")


# ---------------------------------------------------------------------------
# ZIP 安全解压
# ---------------------------------------------------------------------------

def test_safe_extract_rejects_path_escape(tmp_path: Path) -> None:
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr("../escape.txt", "x")
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(su.UpdateError, match="非法路径"):
        su._safe_extract(evil, dest)
    assert not (tmp_path / "escape.txt").exists()


def test_safe_extract_normal(tmp_path: Path) -> None:
    good = tmp_path / "good.zip"
    with zipfile.ZipFile(good, "w") as archive:
        archive.writestr("arena-0.2.6-linux-code-only/deploy/update.sh", "#!/bin/bash\n")
    dest = tmp_path / "dest"
    dest.mkdir()
    su._safe_extract(good, dest)
    assert (dest / "arena-0.2.6-linux-code-only/deploy/update.sh").is_file()


# ---------------------------------------------------------------------------
# 状态机：安装根、状态对账
# ---------------------------------------------------------------------------

def test_install_root_detects_repo_root() -> None:
    assert su.install_root() == ROOT


def test_read_state_recovers_dead_updater(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """applying 状态且更新器进程死亡超过宽限 → 翻转 failed。"""
    monkeypatch.setattr(su, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(su, "install_root", lambda: tmp_path)
    stale = "2026-11-01T00:00:00Z"
    (tmp_path / "state.json").write_text(json.dumps({
        "phase": "applying",
        "apply_started_at": stale,
        "updater": {"pid": 999999999, "proc_start_time": "1"},
    }), encoding="utf-8")
    su._STATE.clear()
    su._PREPARE_THREAD = None
    state = su.read_state()
    assert state["phase"] == "failed"
    su._STATE.clear()


def test_read_state_marks_interrupted_preparing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(su, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(su, "install_root", lambda: tmp_path)
    (tmp_path / "state.json").write_text(json.dumps({"phase": "preparing"}), encoding="utf-8")
    su._STATE.clear()
    su._PREPARE_THREAD = None
    state = su.read_state()
    assert state["phase"] == "interrupted"
    su._STATE.clear()


def test_repository_extraction() -> None:
    assert su._repository("yong29/arena-traffic-gateway:latest") == "yong29/arena-traffic-gateway"
    assert su._repository("yong29/arena-traffic-gateway@sha256:abc") == "yong29/arena-traffic-gateway"
    assert su._repository("registry.local:5000/arena-gw:1") == "registry.local:5000/arena-gw"


# ---------------------------------------------------------------------------
# package_release.py：release.json 生成
# ---------------------------------------------------------------------------

def test_package_release_channel_doc_constants() -> None:
    assert package_release_module.CHANNEL_DOC_FORMAT == "arena-release-channel/v1"
    assert package_release_module.GITHUB_REPO == "Y10ng/Arena"


_SPEC_PR = importlib.util.spec_from_file_location(
    "package_release", ROOT / "deploy/package_release.py")
assert _SPEC_PR and _SPEC_PR.loader
package_release_module = importlib.util.module_from_spec(_SPEC_PR)
_SPEC_PR.loader.exec_module(package_release_module)
