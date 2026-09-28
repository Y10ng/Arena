from __future__ import annotations

from fnmatch import fnmatchcase
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("package_release", ROOT / "deploy/package_release.py")
assert SPEC and SPEC.loader
package_release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package_release)


def test_release_allowlist_excludes_runtime_and_build_inputs() -> None:
    files = package_release.collect_files()
    assert "cairn/pyproject.toml" in files
    assert "cairn/src/cairn/server/static/index.html" in files
    assert "cairn/src/cairn/dispatcher/runtime/containers.py" in files
    assert "deploy/start.sh" in files
    assert "deploy/install.sh" in files
    assert "deploy/update.sh" in files
    assert (ROOT / "deploy/update.sh").stat().st_mode & 0o111
    assert "deploy/common.sh" in files
    assert "deploy/migrate_worker_config.py" in files
    assert "deploy/postgres.compose.yaml" in files
    assert "deploy/postgres.env.example" in files
    assert "deploy/arena.env.example" in files
    assert "docker-compose.yaml" not in files
    assert "dispatch.compose.yaml" not in files
    assert "Dockerfile" not in files
    assert "dispatch.yaml" not in files
    assert not any("DigestWorkerConsole" in path for path in files)
    assert not any(path.startswith("cairn/frontend/") for path in files)
    assert "container/Dockerfile" in files
    assert "container/AGENTS.md" in files
    assert "AGENT.md" in files
    assert not any(".codex" in Path(path).parts for path in files)
    assert "container/pi-agent/package.json" in files
    assert "container/pi-agent/package-lock.json" in files
    assert "container/pi-agent/tsconfig.json" in files
    assert "container/pi-agent/src/cli.ts" in files
    assert "container/pi-agent/contracts/submission-v1.json" in files
    assert not any({"node_modules", "dist", "credentials", ".cairn"}.intersection(Path(path).parts) for path in files)
    assert not any(path.startswith(("runtime/", "container/pi-agent/runtime/")) for path in files)
    assert package_release.RETIRED_FILES.isdisjoint(map(Path, files))
    assert "container/.Dockerfile.swp" not in files
    assert not any("/.venv/" in f"/{path}/" for path in files)
    assert not any(path.endswith((".db", ".log", ".pyc")) for path in files)


def test_docker_context_excludes_sensitive_inputs_and_keeps_pi_sources() -> None:
    patterns = {
        line.strip() for line in (ROOT / "container/.dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    required = {
        "**/node_modules", "**/dist", "**/.git", "**/runtime", "**/*.log",
        "**/.cairn", "**/.claude", "**/.codex", "**/.env", "**/.env.*",
        "**/credentials", "**/credentials.json", "**/auth", "**/auth.json",
        "**/keys", "**/secrets", "**/id_rsa", "**/id_ed25519",
        "**/*.key", "**/*.pem", "**/*.p12", "**/*.pfx",
    }
    assert required <= patterns
    assert not any(pattern.startswith("!") for pattern in patterns)
    # 只检查当前简单排除规则和COPY输入，不模拟Docker或读取任何真实运行态文件。
    for name in (
        "AGENTS.md", "pi-agent/package.json", "pi-agent/package-lock.json",
        "pi-agent/tsconfig.json", "pi-agent/src/cli.ts", "pi-agent/contracts/submission-v1.json",
    ):
        path = Path(name)
        candidates = [name, *(parent.as_posix() for parent in path.parents if parent != Path("."))]
        assert not any(
            fnmatchcase(candidate, rule)
            for candidate in candidates
            for pattern in patterns
            for rule in (pattern, pattern.removeprefix("**/"))
        ), name
        assert (ROOT / "container" / name).is_file()


def test_release_secret_gate_rejects_private_key() -> None:
    with pytest.raises(RuntimeError, match="疑似包含密钥"):
        package_release.scan_content(Path("secret.pem"), b"-----BEGIN PRIVATE KEY-----\nabc")
    for name in (
        "runtime/data.txt", "deploy/private.key", "deploy/.env", "deploy/keys/value.txt",
        "container/pi-agent/runtime/session.json", "container/pi-agent/.env.local",
        "container/pi-agent/auth.json", "container/pi-agent/credentials.json",
    ):
        with pytest.raises(RuntimeError, match="禁止进入发布包"):
            package_release.validate_relative(Path(name))


def test_static_validation_checks_lazy_chunk_dependencies() -> None:
    static_root = "cairn/src/cairn/server/static"
    files = {
        f"{static_root}/index.html": b'<script type="module" src="/static/assets/index.js"></script>',
        f"{static_root}/assets/index.js": b'const page = () => import("./LazyPage.js");',
    }

    with pytest.raises(RuntimeError, match=r"assets/index\.js -> assets/LazyPage\.js"):
        package_release.validate_static(files)

    files[f"{static_root}/assets/LazyPage.js"] = b"export default {};"
    package_release.validate_static(files)


def test_static_validation_rejects_dependency_outside_static_root() -> None:
    static_root = "cairn/src/cairn/server/static"
    files = {
        f"{static_root}/index.html": b'<script type="module" src="/static/assets/index.js"></script>',
        f"{static_root}/assets/index.js": b'import "../../../../../../../outside.js";',
    }

    with pytest.raises(RuntimeError, match="静态资源引用越界"):
        package_release.validate_static(files)


def test_release_manifest_declares_current_pi_runner_versions() -> None:
    manifest = package_release.manifest("0.0.0", {})
    package = json.loads((ROOT / "container/pi-agent/package.json").read_text(encoding="utf-8"))
    assert manifest["expected_cairn_agent_version"] == package["version"] == "1.0.0"
    assert manifest["expected_pi_sdk_version"] == package["dependencies"]["@mariozechner/pi-coding-agent"] == "0.73.0"
    assert "expected_claude_code_version" not in manifest


def test_final_worker_image_labels_match_pi_release_versions() -> None:
    dockerfile = (ROOT / "container/Dockerfile").read_text(encoding="utf-8")
    final_stage = dockerfile.split("FROM worker-node-base AS worker-runtime\n", 1)[1]
    assert "\nFROM " not in final_stage
    manifest = package_release.manifest("0.0.0", {})
    expected = (
        'LABEL io.cairn.agent.engine="pi" \\\n'
        f'      io.cairn.agent.version="{manifest["expected_cairn_agent_version"]}" \\\n'
        f'      io.cairn.pi-sdk.version="{manifest["expected_pi_sdk_version"]}"\n'
    )
    assert expected in final_stage


def test_release_zip_is_reproducible_and_contains_manifest(tmp_path: Path) -> None:
    archive, sidecar = package_release.build_release(tmp_path)
    first_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive, _sidecar = package_release.build_release(tmp_path)
    second_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert first_hash == second_hash
    assert sidecar.read_text(encoding="utf-8").startswith(first_hash)
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        assert any(name.endswith("/RELEASE-MANIFEST.json") for name in names)
        assert not any(name.endswith("/dispatch.yaml") for name in names)
        assert not any("/cairn/frontend/" in name for name in names)
        assert not any({"node_modules", "dist", "credentials", ".cairn"}.intersection(Path(name).parts) for name in names)
        assert not any("/container/pi-agent/runtime/" in name for name in names)
        manifest_name = next(name for name in names if name.endswith("/RELEASE-MANIFEST.json"))
        manifest = json.loads(bundle.read(manifest_name))
        assert manifest["expected_cairn_agent_version"] == "1.0.0"
        assert manifest["expected_pi_sdk_version"] == "0.73.0"
        assert "expected_claude_code_version" not in manifest
        assert {item["path"] for item in manifest["files"]} == {
            "/".join(Path(name).parts[1:]) for name in names if name != manifest_name
        }


@pytest.fixture
def synthetic_release(tmp_path, monkeypatch):
    root = tmp_path / "source"
    files = {
        "CLAUDE.md": "遵循 [AGENT.md](AGENT.md)。\n",
        "AGENT.md": "合成的开发规范。\n",
        "cairn/pyproject.toml": '[project]\nversion = "0.0.0"\n',
        "cairn/uv.lock": "version = 1\n",
        "docs/deployment-linux-zh.md": "synthetic deployment instructions\n",
        "cairn/src/cairn/server/static/index.html": "<html></html>",
        "datas/cairn/knowledge/example.txt": "synthetic knowledge",
        "deploy/start.sh": "#!/bin/sh\nexit 0\n",
        "container/pi-agent/package.json": '{"name":"synthetic-agent"}',
        "container/pi-agent/package-lock.json": '{"lockfileVersion":3}',
        "container/pi-agent/tsconfig.json": '{"compilerOptions":{"outDir":"dist"}}',
        "container/pi-agent/src/cli.ts": "export const source = true;",
        "container/pi-agent/contracts/submission-v1.json": '{"version":1}',
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(package_release, "ROOT", root)
    return root, files


def test_pi_agent_dependencies_and_runtime_inputs_are_pruned_before_reading(synthetic_release, tmp_path, monkeypatch):
    root, expected = synthetic_release
    excluded = (
        "container/.codex/config.toml",
        "container/pi-agent/node_modules/.bin/anthropic-ai-sdk",
        "container/pi-agent/dist/cli.js",
        "container/pi-agent/runtime/session.json",
        "container/pi-agent/.cairn/request.json",
        "container/pi-agent/credentials/provider.json",
        "container/pi-agent/auth.json",
        "container/pi-agent/credentials.json",
        "container/pi-agent/.env.local",
        "runtime/dispatch.yaml",
    )
    for name in excluded:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name.endswith("anthropic-ai-sdk"):
            # 本地npm bin链接不是发布输入，不得为它放宽源码符号链接保护。
            path.symlink_to("../synthetic-sdk/cli.js")
        else:
            path.write_text("synthetic-local-data", encoding="utf-8")
    read_bytes = Path.read_bytes
    scandir = os.scandir
    excluded_paths = {root / name for name in excluded}
    excluded_directories = {
        root / "container/pi-agent" / name
        for name in ("node_modules", "dist", "runtime", ".cairn", "credentials")
    } | {root / "container/.codex"}

    def guarded_read(path):
        assert path not in excluded_paths, "禁止读取被排除的依赖或运行数据"
        return read_bytes(path)

    def guarded_scandir(path):
        assert Path(path) not in excluded_directories, "禁止遍历被排除的目录"
        return scandir(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    monkeypatch.setattr(os, "scandir", guarded_scandir)
    assert set(package_release.collect_files()) == set(expected)
    archive, _ = package_release.build_release(tmp_path / "output")
    assert archive.name == "arena-0.0.0-linux-code-only.zip"
    with zipfile.ZipFile(archive) as bundle:
        names = {"/".join(Path(name).parts[1:]) for name in bundle.namelist()}
        assert names == set(expected) | {"RELEASE-MANIFEST.json"}
        assert all(b"synthetic-local-data" not in bundle.read(name) for name in bundle.namelist())


@pytest.mark.parametrize("relative", [
    "container/pi-agent/src/linked.ts",
    "container/pi-agent/src/linked-directory",
    "container/pi-agent/package-lock.json",
    "cairn/uv.lock",
    "README.md",
])
def test_release_still_rejects_allowed_input_symlinks(synthetic_release, tmp_path, relative):
    root, _ = synthetic_release
    outside = tmp_path / "outside"
    if relative.endswith("directory"):
        outside.mkdir()
    else:
        outside.write_text("synthetic file outside release root", encoding="utf-8")
    link = root / relative
    link.unlink(missing_ok=True)
    link.symlink_to(outside)
    with pytest.raises(RuntimeError, match="不允许符号链接"):
        package_release.collect_files()
