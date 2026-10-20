from __future__ import annotations

from pathlib import Path
import shlex
import shutil
import stat
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
UPDATE_SCRIPT = ROOT / "deploy/update.sh"
WORKER_STEPS = [
    "worker:common:new",
    "worker:runtime-dirs",
    "worker:lock",
    "worker:initialized",
    "worker:env:target-runtime",
    "worker:docker",
    "worker:image:refresh",
    "worker:python:new:target-runtime",
]
INHERITED_LOCK_CHECK = '''[[ "${ARENA_OPERATION_LOCK_INHERITED:-0}" == "1" ]]
[[ "${ROOT}/runtime/pids/start.lock" -ef "/proc/$$/fd/9" ]]
flock -n 9
exec 8>>"${ROOT}/runtime/pids/start.lock"
if flock -n 8; then
  echo "operation-lock-not-held" >&2
  exit 71
fi
exec 8>&-'''


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_script(path: Path, body: str) -> None:
    _write(path, f"#!/usr/bin/env bash\nset -euo pipefail\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _managed_source(
    source: Path, *, install_exit: int = 0, start_exit: int = 0, worker_exit: int = 0
) -> None:
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
{INHERITED_LOCK_CHECK}
echo "install" >> "${{ROOT}}/lifecycle.log"
[[ {install_exit} == 0 ]] || exit {install_exit}
cp "${{ROOT}}/deploy/worker-python" "${{ROOT}}/cairn/.venv/bin/python"''',
    )
    _write_script(
        source / "deploy/start.sh",
        f'''ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
{INHERITED_LOCK_CHECK}
[[ "${{ARENA_TEST_RUNTIME}}" == "updater-runtime" ]]
state="running"
: > "${{ROOT}}/start-args.bin"
for argument in "$@"; do
  printf '%s\\0' "${{argument}}" >> "${{ROOT}}/start-args.bin"
  [[ "${{argument}}" == "--server-only" ]] && state="server-only"
done
printf '%s\n' "${{state}}" > "${{ROOT}}/.test-status"
echo "start:$*" >> "${{ROOT}}/lifecycle.log"
exit {start_exit}''',
    )
    _write_script(
        source / "deploy/status.sh",
        f'''ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
{INHERITED_LOCK_CHECK}
echo "status" >> "${{ROOT}}/lifecycle.log"
printf 'Status:     %s\n' "$(<"${{ROOT}}/.test-status")"''',
    )
    _write_script(source / "deploy/logs.sh", "exit 0")
    # readonly 哨兵只存在于旧 shell，原地 source 新 common 会失败。
    _write(
        source / "deploy/common.sh",
        f'''readonly ARENA_TEST_COMMON_VERSION="new"
DEPLOY_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd -P)"
ROOT_DIR="$(cd "${{DEPLOY_DIR}}/.." && pwd -P)"
ROOT="${{ROOT_DIR}}"
PID_DIR="${{ROOT_DIR}}/runtime/pids"
ENV_FILE="${{ROOT_DIR}}/runtime/arena.env"
PYTHON_BIN="${{ROOT_DIR}}/cairn/.venv/bin/python"
echo "worker:common:${{ARENA_TEST_COMMON_VERSION}}" >> "${{ROOT_DIR}}/lifecycle.log"
ensure_runtime_dirs() {{
  mkdir -p "${{PID_DIR}}"
  echo "worker:runtime-dirs" >> "${{ROOT_DIR}}/lifecycle.log"
}}
acquire_operation_lock() {{
  {INHERITED_LOCK_CHECK}
  echo "worker:lock" >> "${{ROOT_DIR}}/lifecycle.log"
}}
ensure_application_initialized() {{
  [[ -x "${{PYTHON_BIN}}" ]]
  echo "worker:initialized" >> "${{ROOT_DIR}}/lifecycle.log"
}}
load_runtime_env() {{
  set -a
  source "${{ENV_FILE}}"
  set +a
  echo "worker:env:${{ARENA_TEST_RUNTIME}}" >> "${{ROOT_DIR}}/lifecycle.log"
}}
require_docker_daemon() {{
  [[ "${{PWD}}" == "${{ROOT_DIR}}" ]]
  echo "worker:docker" >> "${{ROOT_DIR}}/lifecycle.log"
}}
ensure_dispatch_worker_image() {{
  [[ "$#" == "1" && "$1" == "refresh" ]]
  echo "worker:image:$1" >> "${{ROOT_DIR}}/lifecycle.log"
  "${{PYTHON_BIN}}" "$@"
}}
''',
    )
    # install 仅替换解释器替身；执行时必须来自目标 .venv，并读到目标 env。
    _write_script(
        source / "deploy/worker-python",
        f'''ROOT="$(cd "$(dirname "$0")/../../.." && pwd -P)"
[[ "$PWD" == "${{ROOT}}" ]]
[[ "$#" == "1" && "$1" == "refresh" ]]
[[ "${{ARENA_TEST_RUNTIME}}" == "target-runtime" ]]
echo "worker:python:new:${{ARENA_TEST_RUNTIME}}" >> "${{ROOT}}/lifecycle.log"
exit {worker_exit}''',
    )
    _write_script(source / "cairn/.venv/bin/python", "exit 73")
    _write(source / "runtime/arena.env", "ARENA_TEST_RUNTIME=source-runtime\n")
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
        f'''readonly ARENA_TEST_COMMON_VERSION="old"
export ARENA_TEST_RUNTIME=updater-runtime
DEPLOY_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
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
ensure_application_initialized() {{ return 72; }}
load_runtime_env() {{ return 72; }}
require_docker_daemon() {{ return 72; }}
ensure_dispatch_worker_image() {{ return 72; }}
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
    _write(target / "runtime/arena.env", "ARENA_TEST_RUNTIME=target-runtime\n")
    _write(target / "runtime/dispatch.yaml", "workers: [existing-worker]\n")
    _write(target / "runtime/postgres.env", "POSTGRES_ENV=existing\n")
    _write(target / "runtime/secrets/key", "key-material\n")
    _write(target / "runtime/secrets/model-credential-keyring.json", '{"test": "keyring"}\n')
    _write(target / "runtime/secrets/dispatcher.token", "existing-test-token\n")
    for path in (target / "runtime/secrets").iterdir():
        path.chmod(0o600)
    _write(target / "runtime/data/artifacts/existing.bin", "artifact-data\n")
    _write(target / "cairn/.venv/marker", "venv-data\n")
    _write_script(target / "cairn/.venv/bin/python", "exit 74")
    _write(target / "datas/cairn/project-resources/project/file.bin", "project-data\n")
    _write(target / "datas/cairn/advisory-library-content/note.txt", "advisory-data\n")
    _write(target / "local-notes.txt", "local-data\n")


def _run(source: Path, target: Path, *start_args: str) -> subprocess.CompletedProcess[str]:
    command = [str(source / "deploy/update.sh"), "--target", str(target)]
    if start_args:
        command.extend(("--", *start_args))
    return subprocess.run(
        command, cwd=source.parent, text=True, capture_output=True, check=False
    )


@pytest.mark.parametrize("option", ["--host", "--port"])
def test_start_option_value_cannot_skip_worker_preparation(tmp_path: Path, option: str) -> None:
    source = tmp_path / "new-release"
    target = tmp_path / "installed"
    _managed_source(source, worker_exit=31)
    _installed_target(target)

    result = _run(source, target, option, "--server-only")

    assert result.returncode == 31, result.stderr
    assert (target / "lifecycle.log").read_text().splitlines() == [
        "stop:old", "install", *WORKER_STEPS,
    ]
    assert not (target / "start-args.bin").exists()


@pytest.mark.parametrize("server_only", [False, True])
def test_update_replaces_managed_code_and_preserves_data(
    tmp_path: Path, server_only: bool
) -> None:
    source = tmp_path / "new-release"
    target = tmp_path / "installed"
    _managed_source(source, worker_exit=31 if server_only else 0)
    _installed_target(target)
    external = tmp_path / "external storage 'private'"
    external_keyring = external / "secrets/keyring.json"
    _write(external_keyring, '{"test": "external-keyring"}\n')
    external_keyring.chmod(0o600)
    shutil.move(target / "runtime/data/artifacts", external / "artifacts")
    (target / "runtime/data/artifacts").symlink_to(
        external / "artifacts", target_is_directory=True
    )
    env = target / "runtime/arena.env"
    _write(
        env,
        env.read_text(encoding="utf-8")
        + f"CAIRN_ARTIFACT_ROOT={shlex.quote(str(external / 'artifacts'))}\n"
        + f"CAIRN_MODEL_CREDENTIAL_KEYRING_FILE={shlex.quote(str(external_keyring))}\n",
    )
    protected = [
        target / relative
        for relative in (
            "runtime/arena.env",
            "runtime/dispatch.yaml",
            "runtime/postgres.env",
            "runtime/secrets/key",
            "runtime/secrets/model-credential-keyring.json",
            "runtime/secrets/dispatcher.token",
            "runtime/data/artifacts/existing.bin",
            "cairn/.venv/marker",
            "datas/cairn/project-resources/project/file.bin",
            "datas/cairn/advisory-library-content/note.txt",
            "local-notes.txt",
        )
    ]
    protected.extend((external_keyring, external / "artifacts/existing.bin"))
    before = {
        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in protected
    }
    start_args = ("--server-only",) if server_only else ()

    result = _run(source, target, *start_args)

    assert result.returncode == 0, result.stderr
    assert (target / "lifecycle.log").read_text(encoding="utf-8").splitlines() == [
        "stop:old",
        "install",
        *([] if server_only else WORKER_STEPS),
        f"start:{' '.join(start_args)}",
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
    for path, (content, mode) in before.items():
        assert path.read_bytes() == content, path
        assert stat.S_IMODE(path.stat().st_mode) == mode, path
    assert (target / "runtime/data/artifacts").is_symlink()
    assert (target / "runtime/data/artifacts").resolve() == external / "artifacts"
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
    new_common = (source / "deploy/common.sh").read_text(encoding="utf-8")
    (source / "deploy/common.sh").unlink()
    missing_lifecycle = _run(source, target)
    assert missing_lifecycle.returncode != 0
    assert "deploy/common.sh" in missing_lifecycle.stderr
    assert not (target / "lifecycle.log").exists()

    _write(source / "deploy/common.sh", new_common)
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
    ("install_exit", "worker_exit", "start_exit"),
    [(23, 0, 0), (0, 31, 0), (0, 0, 29)],
    ids=["install", "worker", "start"],
)
def test_install_worker_or_start_failure_stops_the_lifecycle(
    tmp_path: Path,
    install_exit: int,
    worker_exit: int,
    start_exit: int,
) -> None:
    source = tmp_path / "new release 'candidate' \"$(touch retry-injected)\""
    target = tmp_path / "installed \"target\" 'data'"
    _managed_source(
        source, install_exit=install_exit, worker_exit=worker_exit, start_exit=start_exit
    )
    _installed_target(target)
    start_args = (
        "--host",
        "host with spaces 'single' \"double\"",
        "--port",
        "8000",
        "",
        r"$(touch retry-injected); * \ $HOME",
    )
    runtime_before = {
        path: path.read_bytes()
        for path in (target / "runtime").rglob("*")
        if path.is_file()
    }

    result = _run(source, target, *start_args)

    assert result.returncode == (install_exit or worker_exit or start_exit), result.stderr
    expected_lines = ["stop:old", "install"]
    if not install_exit:
        expected_lines.extend(WORKER_STEPS)
    if start_exit:
        expected_lines.append(f"start:{' '.join(start_args)}")
        assert (target / "start-args.bin").read_bytes() == b"".join(
            argument.encode() + b"\0" for argument in start_args
        )
    else:
        assert not (target / "start-args.bin").exists()
    assert (target / "lifecycle.log").read_text(encoding="utf-8").splitlines() == expected_lines
    assert (target / "cairn/src/cairn/new.py").is_file()
    for path, content in runtime_before.items():
        assert path.read_bytes() == content, path
    backups = list(target.glob(".arena-update-backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "cairn/src/cairn/old.py").is_file()
    assert str(backups[0]) in result.stderr

    # 将真正的更新入口换成参数捕获器，直接执行提示，避免重试触碰任何服务。
    _write_script(
        source / "deploy/update.sh",
        '''ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
printf '%s\\0' "$@" > "${ROOT}/retry-args.bin"''',
    )
    retry = subprocess.run(
        ["bash", "-c", result.stderr.splitlines()[-1].strip()],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert retry.returncode == 0, retry.stderr
    assert (source / "retry-args.bin").read_bytes() == b"".join(
        argument.encode() + b"\0"
        for argument in ("--target", str(target), "--", *start_args)
    )
    assert not (tmp_path / "retry-injected").exists()
