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
    "worker:image:missing",
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
  [[ "$#" == "1" && "$1" == "missing" ]]
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
[[ "$#" == "1" && "$1" == "missing" ]]
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
    shutil.copy2(ROOT / "deploy/verify_deployment.py", source / "deploy/verify_deployment.py")
    _write(source / "RELEASE-MANIFEST.json", '{"files": []}\n')
    _write(source / "deploy/prepare_release_images.py", "# lifecycle fixture\n")
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
load_runtime_env() {{
  set -a
  source "${{RUNTIME_DIR}}/arena.env"
  set +a
}}
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
    _write_script(target / "cairn/.venv/bin/python", '''[[ "$1" == "-I" && "$2" == */deploy/prepare_release_images.py ]]
case "$3" in prepare|check|apply) exit 0;; *) exit 74;; esac''')
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


@pytest.mark.parametrize("prepare_exit", [0, 37])
def test_new_package_prepares_before_old_server_stops(tmp_path, prepare_exit):
    source, target = tmp_path / "new-release", tmp_path / "installed"
    _managed_source(source)
    _installed_target(target)
    _write_script(target / "cairn/.venv/bin/python", f'''ROOT={shlex.quote(str(target))}
[[ "$1" == "-I" && "$2" == */deploy/prepare_release_images.py ]]
printf 'images:%s\\n' "$3" >> "$ROOT/lifecycle.log"
if [[ "$3" == prepare ]]; then exit {prepare_exit}; fi''')
    result = _run(source, target)
    steps = (target / "lifecycle.log").read_text().splitlines()
    if prepare_exit:
        assert result.returncode == prepare_exit
        assert steps == ["images:prepare"]
        assert (target / "cairn/src/cairn/old.py").exists()
    else:
        assert result.returncode == 0, result.stderr
        assert steps[:4] == ["images:prepare", "images:check", "stop:old", "images:apply"]
    assert not list((target / "runtime").glob(".update-images.*"))


@pytest.mark.parametrize("external_knowledge", [False, True])
def test_start_sets_material_paths_before_bootstrap(tmp_path: Path, external_knowledge: bool) -> None:
    import sys

    deploy = tmp_path / "deploy"
    deploy.mkdir()
    shutil.copy2(ROOT / "deploy/start.sh", deploy / "start.sh")
    knowledge = tmp_path / ("external-knowledge" if external_knowledge else "runtime/data/knowledge")
    _write(tmp_path / "dispatcher.token", "fixture-token")
    _write_script(deploy / "cairn", '''if [[ "$1" == db ]]; then exit 0; fi
[[ "$1 $2" == "auth bootstrap-runtime" ]]
printf '%s\\n' "$CAIRN_KNOWLEDGE_ROOT" "$CAIRN_ARTIFACT_ROOT" "$CAIRN_ADVISORY_LIBRARY_ROOT" > paths.txt
exit 29''')
    _write(deploy / "common.sh", f'''ROOT_DIR={shlex.quote(str(tmp_path))}
RUNTIME_DIR="${{ROOT_DIR}}/runtime"
DATA_DIR="${{RUNTIME_DIR}}/data"
PID_DIR="${{RUNTIME_DIR}}/pids"
DISPATCH_CONFIG="${{RUNTIME_DIR}}/dispatch.yaml"
DISPATCHER_TOKEN_FILE="${{ROOT_DIR}}/dispatcher.token"
INITIAL_ADMIN_PASSWORD_FILE="${{ROOT_DIR}}/unused-password"
PYTHON_BIN={shlex.quote(sys.executable)}
CAIRN_BIN="${{ROOT_DIR}}/deploy/cairn"
ensure_runtime_dirs() {{ :; }}
acquire_operation_lock() {{ :; }}
ensure_application_initialized() {{ :; }}
load_runtime_env() {{
  export CAIRN_DATABASE_URL=postgresql://fixture/unused
  unset CAIRN_KNOWLEDGE_ROOT
  {'export CAIRN_KNOWLEDGE_ROOT=' + shlex.quote(str(knowledge)) if external_knowledge else ':'}
  export CAIRN_ARTIFACT_ROOT="${{ROOT_DIR}}/original-artifacts"
  export CAIRN_ADVISORY_LIBRARY_ROOT="${{ROOT_DIR}}/original-library"
}}
resolve_server_endpoint() {{ :; }}
configure_trusted_origins() {{ :; }}
validate_external_database_url() {{ :; }}
ensure_local_secrets() {{ :; }}
stage_initial_admin_password() {{ :; }}
''')
    # 只运行脚本到 bootstrap 替身，替身故意退出，绝不进入真实服务或数据库。
    result = subprocess.run([str(deploy / "start.sh"), "--server-only"], text=True, capture_output=True)
    assert result.returncode != 0
    assert "自动初始化失败" in result.stderr
    assert (tmp_path / "paths.txt").read_text().splitlines() == [
        str(knowledge), str(tmp_path / "original-artifacts"), str(tmp_path / "original-library"),
    ]


@pytest.mark.parametrize("git_kind", ["directory", "worktree"])
def test_update_rejects_git_before_staging_or_stopping(tmp_path: Path, git_kind: str) -> None:
    source = tmp_path / "new-release"
    target = tmp_path / "installed"
    _managed_source(source)
    _installed_target(target)
    if git_kind == "directory":
        (target / ".git").mkdir()
    else:
        _write(target / ".git", "gitdir: /original/repository/.git/worktrees/installed\n")
    before = {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()}

    result = _run(source, target)

    assert result.returncode != 0
    assert "Git 工作区" in result.stderr
    assert not (target / "lifecycle.log").exists()
    assert not list(target.glob(".arena-update-*"))
    assert before == {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()}


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
    assert (target / "RELEASE-MANIFEST.json").read_bytes() == (source / "RELEASE-MANIFEST.json").read_bytes()
    for path, (content, mode) in before.items():
        assert path.read_bytes() == content, path
        assert stat.S_IMODE(path.stat().st_mode) == mode, path
    assert (target / "runtime/data/artifacts").is_symlink()
    assert (target / "runtime/data/artifacts").resolve() == external / "artifacts"
    assert not list(target.glob(".arena-update-*"))


@pytest.mark.parametrize("location", ["absolute", "relative", "symlink", "inherited"])
def test_update_syncs_external_static_without_removing_old_or_custom_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    source, target = tmp_path / "new-release", tmp_path / "installed"
    _managed_source(source)
    _installed_target(target)
    external = target / "runtime/static files 'custom'"
    _write(external / "index.html", "stale-external-index\n")
    _write(external / "assets/old-hash.js", "old-static\n")
    _write(external / "assets/new-hash.js", "new-static\n")
    _write(external / "custom/local.css", "user-style\n")
    (external / "custom/link.css").symlink_to("local.css")
    _write(source / "cairn/src/cairn/server/static/assets/nested/new.css", "new-style\n")
    configured = str(external)
    if location == "relative":
        configured = str(external.relative_to(target))
    elif location == "symlink":
        (target / "static-link").symlink_to(external, target_is_directory=True)
        configured = str(target / "static-link")
    if location == "inherited":
        monkeypatch.setenv("CAIRN_STATIC_DIR", configured)
    else:
        # runtime 配置覆盖继承值；含空格和引号的路径沿用 shell 加载规则。
        monkeypatch.setenv("CAIRN_STATIC_DIR", str(tmp_path / "wrong-static"))
        env = target / "runtime/arena.env"
        _write(env, env.read_text() + f"CAIRN_STATIC_DIR={shlex.quote(configured)}\n")
    before = {path: path.read_bytes() for path in external.rglob("*") if path.is_file()}
    install = source / "deploy/install.sh"
    _write_script(
        install,
        install.read_text()
        + f'\n[[ "$(<{shlex.quote(str(external / "index.html"))})" == stale-external-index ]]',
    )
    start = source / "deploy/start.sh"
    _write_script(start, f'''[[ "$(<{shlex.quote(str(external / "index.html"))})" == new-index ]]
[[ -f {shlex.quote(str(external / "assets/nested/new.css"))} ]]
''' + start.read_text())

    start_args = ("--server-only",) if location == "relative" else ()
    result = _run(source, target, *start_args)

    assert result.returncode == 0, result.stderr
    assert (external / "index.html").read_text() == "new-index\n"
    assert (external / "assets/nested/new.css").read_text() == "new-style\n"
    for path, content in before.items():
        if path.name != "index.html":
            assert path.read_bytes() == content
    assert (external / "custom/link.css").is_symlink()
    assert not (tmp_path / "wrong-static").exists()
    assert not list(external.glob(".arena-index-*"))
    assert not list(target.glob(".arena-update-*"))


@pytest.mark.parametrize(
    "conflict", ["asset", "asset-directory", "asset-symlink", "index-directory", "root-file"]
)
def test_external_static_conflict_stops_before_service_changes(tmp_path: Path, conflict: str) -> None:
    source, target = tmp_path / "new-release", tmp_path / "installed"
    _managed_source(source)
    _installed_target(target)
    external = tmp_path / "external-static"
    if conflict == "root-file":
        _write(external, "user-file\n")
    else:
        _write(external / "index.html", "stale-external-index\n")
        if conflict == "asset":
            _write(external / "assets/new-hash.js", "custom-conflict\n")
        elif conflict == "asset-directory":
            (external / "assets/new-hash.js").mkdir(parents=True)
        elif conflict == "asset-symlink":
            outside = tmp_path / "custom-assets"
            _write(outside / "new-hash.js", "new-static\n")
            (external / "assets").symlink_to(outside, target_is_directory=True)
        else:
            (external / "index.html").unlink()
            (external / "index.html").mkdir()
    env = target / "runtime/arena.env"
    _write(env, env.read_text() + f"CAIRN_STATIC_DIR={shlex.quote(str(external))}\n")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    result = _run(source, target)

    assert result.returncode != 0
    assert "外置静态资源" in result.stderr
    assert not (target / "lifecycle.log").exists()
    assert not list(target.glob(".arena-update-*"))
    for path, content in before.items():
        assert path.read_bytes() == content


def test_external_static_env_load_failure_stops_before_service_changes(tmp_path: Path) -> None:
    source, target = tmp_path / "new-release", tmp_path / "installed"
    _managed_source(source)
    _installed_target(target)
    (target / "runtime/arena.env").unlink()

    result = _run(source, target)

    assert result.returncode != 0
    assert not (target / "lifecycle.log").exists()
    assert (target / "cairn/src/cairn/old.py").exists()
    assert not list(target.glob(".arena-update-*"))


def test_external_static_rechecks_before_publishing_index(tmp_path: Path) -> None:
    source, target = tmp_path / "new-release", tmp_path / "installed"
    _managed_source(source)
    _installed_target(target)
    external = tmp_path / "external-static"
    _write(external / "index.html", "stale-external-index\n")
    _write(external / "assets/old-hash.js", "old-static\n")
    env = target / "runtime/arena.env"
    _write(env, env.read_text() + f"CAIRN_STATIC_DIR={shlex.quote(str(external))}\n")
    install = source / "deploy/install.sh"
    _write_script(
        install,
        install.read_text()
        + f"\nprintf custom-after-preflight > {shlex.quote(str(external / 'assets/new-hash.js'))}",
    )

    result = _run(source, target, "--server-only")

    assert result.returncode != 0
    assert "外置静态资源冲突" in result.stderr
    assert (external / "index.html").read_text() == "stale-external-index\n"
    assert (external / "assets/new-hash.js").read_text() == "custom-after-preflight"
    assert (external / "assets/old-hash.js").read_text() == "old-static\n"
    assert (target / "lifecycle.log").read_text().splitlines() == ["stop:old", "install"]
    assert len(list(target.glob(".arena-update-backup-*"))) == 1


def test_update_without_manifest_stops_before_service_changes(tmp_path: Path) -> None:
    source = tmp_path / "new-release"
    target = tmp_path / "installed"
    _managed_source(source)
    _installed_target(target)
    (source / "RELEASE-MANIFEST.json").unlink()

    result = _run(source, target)

    assert result.returncode != 0
    assert not (target / "lifecycle.log").exists()
    assert (target / "cairn/src/cairn/old.py").exists()


def test_invalid_manifest_blocks_before_stopping(tmp_path: Path) -> None:
    source = tmp_path / "new-release"
    target = tmp_path / "installed"
    _managed_source(source)
    _installed_target(target)
    shutil.copy2(ROOT / "deploy/verify_deployment.py", source / "deploy/verify_deployment.py")
    _write(source / "RELEASE-MANIFEST.json", "{}\n")

    result = _run(source, target)

    assert result.returncode != 0
    assert not (target / "lifecycle.log").exists()
    assert (target / "cairn/src/cairn/old.py").exists()


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
