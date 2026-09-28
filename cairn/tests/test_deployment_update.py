from __future__ import annotations

from pathlib import Path
import shutil
import stat
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
UPDATE_SCRIPT = ROOT / "deploy/update.sh"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_script(path: Path, body: str) -> None:
    _write(path, f"#!/usr/bin/env bash\nset -euo pipefail\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _managed_source(source: Path, *, install_exit: int = 0, start_exit: int = 0) -> None:
    (source / "deploy").mkdir(parents=True)
    shutil.copy2(UPDATE_SCRIPT, source / "deploy/update.sh")
    _write_script(
        source / "deploy/stop.sh",
        'echo "unexpected-source-stop" >> "$(dirname "$0")/../lifecycle.log"',
    )
    _write_script(
        source / "deploy/install.sh",
        f'''ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
[[ -f "${{ROOT}}/cairn/src/cairn/new.py" ]]
[[ ! -e "${{ROOT}}/cairn/src/cairn/old.py" ]]
[[ "${{ARENA_OPERATION_LOCK_INHERITED:-0}}" == "1" ]]
exec 8>>"${{ROOT}}/runtime/pids/start.lock"
if flock -n 8; then
  echo "operation-lock-not-held" >&2
  exit 71
fi
echo "install" >> "${{ROOT}}/lifecycle.log"
exit {install_exit}''',
    )
    _write_script(
        source / "deploy/start.sh",
        f'''ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
state="running"
for argument in "$@"; do
  [[ "${{argument}}" == "--server-only" ]] && state="server-only"
done
printf '%s\n' "${{state}}" > "${{ROOT}}/.test-status"
echo "start:$*" >> "${{ROOT}}/lifecycle.log"
exit {start_exit}''',
    )
    _write_script(
        source / "deploy/status.sh",
        '''ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
echo "status" >> "${ROOT}/lifecycle.log"
printf 'Status:     %s\n' "$(<"${ROOT}/.test-status")"''',
    )
    _write_script(source / "deploy/logs.sh", "exit 0")
    _write(source / "deploy/common.sh", "# replacement common\n")
    _write(source / "deploy/arena.env.example", "CAIRN_ENV=example\n")
    _write(source / "deploy/dispatch.yaml.example", "workers: []\n")
    _write(source / "deploy/migrate_worker_config.py", "# migration helper\n")
    _write(source / "deploy/postgres.compose.yaml", "services: {}\n")
    _write(source / "deploy/postgres.env.example", "POSTGRES_ENV=example\n")
    _write(source / "deploy/verify_deployment.py", "raise SystemExit(1)\n")
    _write_script(source / "deploy/verify.sh", "exit 0")
    _write(source / "cairn/src/cairn/new.py", "NEW = True\n")
    _write(source / "cairn/src/cairn/server/static/index.html", "new-index\n")
    _write(source / "cairn/src/cairn/server/static/assets/new-hash.js", "new-static\n")
    _write(source / "cairn/pyproject.toml", "[project]\nname = 'new'\n")
    _write(source / "cairn/uv.lock", "new-lock\n")
    _write(source / "datas/cairn/knowledge/new.txt", "new-knowledge\n")
    _write(source / "container/Dockerfile", "FROM scratch\n")
    _write(source / "docs/deployment-linux-zh.md", "new-docs\n")
    _write(source / "README.md", "new-readme\n")
    _write(source / "CHANGELOG.md", "new-changelog\n")
    _write(source / "CLAUDE.md", "new-claude\n")
    _write(source / "AGENT.md", "new-agent\n")


def _installed_target(target: Path, *, stop_exit: int = 0) -> None:
    _write(
        target / "deploy/common.sh",
        f'''DEPLOY_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
ROOT_DIR="$(cd "${{DEPLOY_DIR}}/.." && pwd)"
RUNTIME_DIR="${{ROOT_DIR}}/runtime"
PID_DIR="${{RUNTIME_DIR}}/pids"
DISPATCH_CONFIG="${{RUNTIME_DIR}}/dispatch.yaml"
SERVER_ENDPOINT_FILE="${{PID_DIR}}/server.endpoint"
ensure_runtime_dirs() {{ mkdir -p "${{PID_DIR}}"; }}
acquire_operation_lock() {{
  exec 9>>"${{PID_DIR}}/start.lock"
  flock -n 9
}}
stop_managed_dispatcher() {{
  [[ -f "${{ROOT_DIR}}/cairn/src/cairn/old.py" ]]
  echo "stop:old" >> "${{ROOT_DIR}}/lifecycle.log"
  return {stop_exit}
}}
stop_pid_file() {{ :; }}
''',
    )
    _write_script(
        target / "deploy/stop.sh",
        f'''ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
[[ -f "${{ROOT}}/cairn/src/cairn/old.py" ]]
echo "stop:old" >> "${{ROOT}}/lifecycle.log"
exit {stop_exit}''',
    )
    _write(target / "deploy/old-deploy-file", "old-deploy\n")
    _write(target / "cairn/src/cairn/old.py", "OLD = True\n")
    _write(target / "cairn/src/cairn/server/static/index.html", "old-index\n")
    _write(target / "cairn/src/cairn/server/static/assets/old-hash.js", "old-static\n")
    _write(target / "cairn/pyproject.toml", "old-project\n")
    _write(target / "cairn/uv.lock", "old-lock\n")
    _write(target / "datas/cairn/knowledge/old.txt", "old-knowledge\n")
    _write(target / "container/old.txt", "old-container\n")
    _write(target / "docs/deployment-linux-zh.md", "old-docs\n")
    _write(target / "RELEASE-MANIFEST.json", "old-stale-manifest\n")
    _write(target / "runtime/arena.env", "runtime-secret\n")
    _write(target / "runtime/secrets/key", "key-material\n")
    _write(target / "cairn/.venv/marker", "venv-data\n")
    _write(target / "datas/cairn/project-resources/project/file.bin", "project-data\n")
    _write(target / "datas/cairn/advisory-library-content/note.txt", "advisory-data\n")
    _write(target / "local-notes.txt", "local-data\n")


def _run(source: Path, target: Path, *start_args: str) -> subprocess.CompletedProcess[str]:
    command = [str(source / "deploy/update.sh"), "--target", str(target)]
    if start_args:
        command.extend(("--", *start_args))
    return subprocess.run(command, text=True, capture_output=True, check=False)


def test_update_replaces_managed_code_and_preserves_data(tmp_path: Path) -> None:
    source = tmp_path / "new-release"
    target = tmp_path / "installed"
    _managed_source(source)
    _installed_target(target)

    result = _run(source, target, "--server-only")

    assert result.returncode == 0, result.stderr
    assert (target / "lifecycle.log").read_text(encoding="utf-8").splitlines() == [
        "stop:old",
        "install",
        "start:--server-only",
        "status",
    ]
    assert (target / "cairn/src/cairn/new.py").is_file()
    assert (target / "AGENT.md").read_text(encoding="utf-8") == "new-agent\n"
    assert not (target / "cairn/src/cairn/old.py").exists()
    assert not (target / "cairn/src/cairn/server/static/assets/old-hash.js").exists()
    assert (target / "cairn/src/cairn/server/static/assets/new-hash.js").is_file()
    assert not (target / "deploy/old-deploy-file").exists()
    assert not (target / "datas/cairn/knowledge/old.txt").exists()
    assert not (target / "RELEASE-MANIFEST.json").exists()
    assert (target / "runtime/arena.env").read_text(encoding="utf-8") == "runtime-secret\n"
    assert (target / "runtime/secrets/key").read_text(encoding="utf-8") == "key-material\n"
    assert (target / "cairn/.venv/marker").read_text(encoding="utf-8") == "venv-data\n"
    assert (
        target / "datas/cairn/project-resources/project/file.bin"
    ).read_text(encoding="utf-8") == "project-data\n"
    assert (
        target / "datas/cairn/advisory-library-content/note.txt"
    ).read_text(encoding="utf-8") == "advisory-data\n"
    assert (target / "local-notes.txt").read_text(encoding="utf-8") == "local-data\n"
    assert not list(target.glob(".arena-update-*"))


def test_update_without_manifest_is_supported(tmp_path: Path) -> None:
    source = tmp_path / "new-release"
    target = tmp_path / "installed"
    _managed_source(source)
    _installed_target(target)

    result = _run(source, target)

    assert result.returncode == 0, result.stderr
    assert "未包含 RELEASE-MANIFEST.json" in result.stderr


def test_invalid_manifest_warns_without_blocking_update(tmp_path: Path) -> None:
    source = tmp_path / "new-release"
    target = tmp_path / "installed"
    _managed_source(source)
    _installed_target(target)
    shutil.copy2(ROOT / "deploy/verify_deployment.py", source / "deploy/verify_deployment.py")
    _write(source / "RELEASE-MANIFEST.json", "{}\n")

    result = _run(source, target)

    assert result.returncode == 0, result.stderr
    assert "校验未通过" in result.stderr
    assert (target / "RELEASE-MANIFEST.json").read_text(encoding="utf-8") == "{}\n"


def test_preflight_rejects_same_nested_and_incomplete_sources(tmp_path: Path) -> None:
    source = tmp_path / "new-release"
    target = tmp_path / "installed"
    _managed_source(source)
    _installed_target(target)

    same = _run(source, source)
    assert same.returncode != 0
    assert "不能相同或互相嵌套" in same.stderr

    nested = source / "nested-install"
    _installed_target(nested)
    nested_result = _run(source, nested)
    assert nested_result.returncode != 0
    assert "不能相同或互相嵌套" in nested_result.stderr
    assert not (nested / "lifecycle.log").exists()

    (source / "cairn/src/cairn/server/static/index.html").unlink()
    incomplete = _run(source, target)
    assert incomplete.returncode != 0
    assert "缺少必要文件" in incomplete.stderr
    assert not (target / "lifecycle.log").exists()

    _write(source / "cairn/src/cairn/server/static/index.html", "new-index\n")
    (source / "deploy/common.sh").unlink()
    missing_lifecycle = _run(source, target)
    assert missing_lifecycle.returncode != 0
    assert "deploy/common.sh" in missing_lifecycle.stderr
    assert not (target / "lifecycle.log").exists()

    _write(source / "deploy/common.sh", "# replacement common\n")
    (source / "deploy/start.sh").chmod(0o644)
    non_executable = _run(source, target)
    assert non_executable.returncode != 0
    assert "缺少可执行脚本: deploy/start.sh" in non_executable.stderr
    assert not (target / "lifecycle.log").exists()

    (source / "deploy/start.sh").chmod(0o755)
    (source / "deploy/status.sh").unlink()
    (source / "deploy/status.sh").mkdir()
    wrong_type = _run(source, target)
    assert wrong_type.returncode != 0
    assert "缺少可执行脚本: deploy/status.sh" in wrong_type.stderr
    assert not (target / "lifecycle.log").exists()


def test_target_symlink_ancestor_is_rejected_before_stop(tmp_path: Path) -> None:
    source = tmp_path / "new-release"
    target = tmp_path / "installed"
    external_cairn = tmp_path / "external-cairn"
    _managed_source(source)
    _installed_target(target)
    shutil.move(target / "cairn", external_cairn)
    (target / "cairn").symlink_to(external_cairn, target_is_directory=True)

    result = _run(source, target)

    assert result.returncode != 0
    assert "目标发布路径不允许 symlink" in result.stderr
    assert (external_cairn / "src/cairn/old.py").read_text(encoding="utf-8") == "OLD = True\n"
    assert not (target / "lifecycle.log").exists()


def test_stop_failure_leaves_managed_code_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "new-release"
    target = tmp_path / "installed"
    _managed_source(source)
    _installed_target(target, stop_exit=19)

    result = _run(source, target)

    assert result.returncode == 19
    assert (target / "cairn/src/cairn/old.py").read_text(encoding="utf-8") == "OLD = True\n"
    assert not (target / "cairn/src/cairn/new.py").exists()
    assert (target / "deploy/old-deploy-file").read_text(encoding="utf-8") == "old-deploy\n"
    assert not list(target.glob(".arena-update-*"))


def test_replacement_failure_restores_old_managed_files(tmp_path: Path) -> None:
    source = tmp_path / "new-release"
    target = tmp_path / "installed"
    _managed_source(source)
    _installed_target(target)
    shutil.rmtree(target / "docs")
    _write(target / "docs", "blocks-directory-creation\n")

    result = _run(source, target)

    assert result.returncode != 0
    assert (target / "cairn/src/cairn/old.py").read_text(encoding="utf-8") == "OLD = True\n"
    assert not (target / "cairn/src/cairn/new.py").exists()
    assert (target / "deploy/old-deploy-file").read_text(encoding="utf-8") == "old-deploy\n"
    assert (target / "docs").read_text(encoding="utf-8") == "blocks-directory-creation\n"
    assert not list(target.glob(".arena-update-*"))


def test_degraded_status_fails_and_retains_old_code_backup(tmp_path: Path) -> None:
    source = tmp_path / "new-release"
    target = tmp_path / "installed"
    _managed_source(source)
    _write_script(
        source / "deploy/status.sh",
        '''ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
echo "status" >> "${ROOT}/lifecycle.log"
echo "Status:     degraded"''',
    )
    _installed_target(target)

    result = _run(source, target)

    assert result.returncode == 1
    assert "期望 running，实际 degraded" in result.stderr
    backups = list(target.glob(".arena-update-backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "cairn/src/cairn/old.py").is_file()


@pytest.mark.parametrize(
    ("install_exit", "start_exit", "expected_lines"),
    [
        (23, 0, ["stop:old", "install"]),
        (0, 29, ["stop:old", "install", "start:"]),
    ],
)
def test_install_or_start_failure_stops_the_lifecycle(
    tmp_path: Path,
    install_exit: int,
    start_exit: int,
    expected_lines: list[str],
) -> None:
    source = tmp_path / "new-release"
    target = tmp_path / "installed"
    _managed_source(source, install_exit=install_exit, start_exit=start_exit)
    _installed_target(target)

    result = _run(source, target)

    assert result.returncode == (install_exit or start_exit)
    assert (target / "lifecycle.log").read_text(encoding="utf-8").splitlines() == expected_lines
    assert (target / "cairn/src/cairn/new.py").is_file()
    backups = list(target.glob(".arena-update-backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "cairn/src/cairn/old.py").is_file()
    assert str(backups[0]) in result.stderr
