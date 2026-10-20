from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest
import yaml

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.tasks import process_runner

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("verify_deployment", ROOT / "deploy/verify_deployment.py")
assert SPEC and SPEC.loader
verify_deployment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify_deployment)


@pytest.fixture
def common_script(tmp_path: Path) -> Path:
    """复制原脚本到假仓库，只覆盖解释器，不执行任何安装或服务入口。"""
    deploy = tmp_path / "fake-repo/deploy"
    deploy.mkdir(parents=True)
    common = deploy / "common.sh"
    shutil.copy2(ROOT / "deploy/common.sh", common)
    wrapper = deploy / "test-common.sh"
    wrapper.write_text(
        f"source {shlex.quote(str(common))}\nPYTHON_BIN={shlex.quote(sys.executable)}\n",
        encoding="utf-8",
    )
    return wrapper


def test_manifest_verification_checks_each_file(tmp_path: Path) -> None:
    payload = b"release-data"
    (tmp_path / "file.txt").write_bytes(payload)
    manifest = {
        "files": [{"path": "file.txt", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}]
    }
    (tmp_path / "RELEASE-MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert verify_deployment.verify_manifest(tmp_path)["files"][0]["path"] == "file.txt"

    (tmp_path / "file.txt").write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="大小不匹配|SHA-256 不匹配"):
        verify_deployment.verify_manifest(tmp_path)


@pytest.mark.parametrize("tampered", [False, True])
def test_manifest_only_cli_needs_no_third_party_packages(tmp_path: Path, tampered: bool) -> None:
    payload = b"release-data"
    (tmp_path / "file.txt").write_bytes(payload)
    manifest = {
        "files": [{"path": "file.txt", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}]
    }
    (tmp_path / "RELEASE-MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    static = tmp_path / "cairn/src/cairn/server/static"
    static.mkdir(parents=True)
    (static / "index.html").write_text("<main>release fixture</main>", encoding="utf-8")
    if tampered:
        (tmp_path / "file.txt").write_bytes(b"release-bad!")

    # 更新器使用系统 Python；隔离路径并关闭 site，确保清单校验只需标准库。
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(ROOT / "deploy/verify_deployment.py"),
         "--root", str(tmp_path), "--manifest-only"],
        text=True, capture_output=True, check=False, timeout=15,
    )
    assert result.returncode == (1 if tampered else 0), result.stderr
    if tampered:
        assert "SHA-256 不匹配" in result.stderr
    else:
        assert "验收通过" in result.stdout
        assert result.stderr == ""
    assert not (tmp_path / "runtime").exists()


def test_manifest_verification_rejects_missing_and_traversal_files(tmp_path: Path) -> None:
    manifest_path = tmp_path / "RELEASE-MANIFEST.json"
    manifest_path.write_text(json.dumps({"files": [{"path": "missing", "size": 0, "sha256": "0" * 64}]}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="缺失"):
        verify_deployment.verify_manifest(tmp_path)

    manifest_path.write_text(json.dumps({"files": [{"path": "../escape", "size": 0, "sha256": "0" * 64}]}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="路径非法"):
        verify_deployment.verify_manifest(tmp_path)


def test_dispatch_template_loads_with_fresh_install_baseline() -> None:
    template_path = ROOT / "deploy/dispatch.yaml.example"
    template = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    config = DispatchConfig.load(template_path)

    assert config.dispatcher_id == "arena-local"
    assert config.runtime.max_workers == 8
    assert config.runtime.max_project_workers == 4
    assert config.runtime.prompt_group == "goal-driven-v1"
    assert config.container.image == "arena-worker:local"
    assert config.container.network_mode == "host"
    assert config.providers == []
    assert config.models == []
    assert [profile["id"] for profile in template["execution_profiles"]] == [
        "default",
        "network",
        "restricted",
    ]
    assert {profile.network_policy for profile in config.execution_profiles} == {"host"}
    restricted_profile = next(
        profile for profile in config.execution_profiles if profile.id == "restricted"
    )
    assert restricted_profile.mem_limit == 1536 * 1024 * 1024
    assert restricted_profile.memswap_limit == 1536 * 1024 * 1024
    assert restricted_profile.pids_limit == 256
    assert config.common_env == {}
    assert config.workers == []


def test_dispatch_config_allows_omitted_workers(tmp_path: Path) -> None:
    content = (ROOT / "deploy/dispatch.yaml.example").read_text(encoding="utf-8")
    path = tmp_path / "dispatch.yaml"
    path.write_text(content.replace("workers: []\n", ""), encoding="utf-8")

    config = DispatchConfig.load(path)

    assert config.workers == []


def test_dispatch_config_accepts_runtime_server_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "dispatch.yaml"
    path.write_text(
        (ROOT / "deploy/dispatch.yaml.example").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setenv("CAIRN_SERVER_URL", "http://127.0.0.1:18084")

    assert DispatchConfig.load(path).server == "http://127.0.0.1:18084"


def test_default_runtime_verification_skips_worker_images(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "dispatch.yaml").write_text(
        (ROOT / "deploy/dispatch.yaml.example").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (runtime / "arena.env").write_text(
        (ROOT / "deploy/arena.env.example").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    source_path = tmp_path / "cairn/src/cairn/dispatcher/tasks/process_runner.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("# 测试占位文件\n", encoding="utf-8")
    monkeypatch.setattr(process_runner, "__file__", str(source_path))

    def fail_verify_image(*_args, **_kwargs):
        raise AssertionError("默认验收不应检查 Worker 镜像")

    monkeypatch.setattr(verify_deployment, "verify_image", fail_verify_image)

    verify_deployment.verify_runtime(tmp_path, {}, full=False)


def test_runtime_verification_empty_static_pi_pool_requires_durable_binding_acceptance(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "dispatch.yaml").write_text(
        (ROOT / "deploy/dispatch.yaml.example").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (runtime / "arena.env").write_text("ARENA_SERVER_PORT=8000\n", encoding="utf-8")
    source_path = tmp_path / "cairn/src/cairn/dispatcher/tasks/process_runner.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("# 测试占位文件\n", encoding="utf-8")
    monkeypatch.setattr(process_runner, "__file__", str(source_path))

    verify_deployment.verify_runtime(tmp_path, {}, full=False)

    captured = capsys.readouterr()
    assert "enabled_pi_worker_count=0" in captured.out
    assert "Server durable Worker 绑定及可用性需另行验收" in captured.err


def test_deploy_scripts_use_server_then_dispatcher_runtime_settings() -> None:
    start = (ROOT / "deploy/start.sh").read_text(encoding="utf-8")
    common = (ROOT / "deploy/common.sh").read_text(encoding="utf-8")
    stop = (ROOT / "deploy/stop.sh").read_text(encoding="utf-8")
    status = (ROOT / "deploy/status.sh").read_text(encoding="utf-8")
    install = (ROOT / "deploy/install.sh").read_text(encoding="utf-8")
    env_example = (ROOT / "deploy/arena.env.example").read_text(encoding="utf-8")
    postgres_env_example = (ROOT / "deploy/postgres.env.example").read_text(encoding="utf-8")

    assert 'export CAIRN_DISPATCH_CONFIG="${DISPATCH_CONFIG}"' in start
    assert 'resolve_server_endpoint' in common
    assert 'START_HOST_OVERRIDE:-${ARENA_SERVER_HOST:-0.0.0.0}' in common
    assert 'START_PORT_OVERRIDE:-${ARENA_SERVER_PORT:-8000}' in common
    assert 'HEALTH_HOST="127.0.0.1"' in common
    assert "resolve_server_endpoint" in start and "resolve_server_endpoint" in status
    assert '--host' in start and '--port' in start
    assert 'start_dispatcher=1' in start
    assert '--with-dispatcher' not in start and '--server-only' in start
    assert '"${CAIRN_BIN}" dispatch --config runtime/dispatch.yaml' in start
    assert 'auth bootstrap-runtime' in start
    assert 'ensure_local_secrets' in start
    assert 'model-credential-keyring.json' in common
    assert 'dispatcher.token' in common
    assert 'os.urandom(32)' in common and 'secrets.token_urlsafe(32)' in common
    assert 'setsid env -u CAIRN_DISPATCHER_TOKEN "${CAIRN_BIN}" serve' in start
    assert '-u CAIRN_DATABASE_URL' in start
    assert '-u CAIRN_MODEL_CREDENTIAL_KEYRING_FILE' in start
    assert 'CAIRN_SERVER_URL="${SERVER_URL}"' in start
    assert 'CAIRN_DISPATCHER_TOKEN="${dispatcher_token}"' in start
    assert 'stage_initial_admin_password' in start
    assert '--admin-password-file "${INITIAL_ADMIN_PASSWORD_FILE}"' in start
    assert 'ARENA_BOOTSTRAP_ADMIN_PASSWORD' not in start
    assert 'publish_initial_admin_password' in start
    assert 'INITIAL_ADMIN_PASSWORD_FILE="${SECRETS_DIR}/initial-admin-password"' in common
    assert 'export CAIRN_ARTIFACT_ROOT="${CAIRN_ARTIFACT_ROOT:-${DATA_DIR}/artifacts}"' in start
    assert 'ensure_application_initialized' in start
    assert 'unset CAIRN_DATABASE_URL' in start
    assert 'database_mode="external"' in start
    assert 'database_mode="managed-local"' in start
    assert 'validate_external_database_url' in start
    assert 'start_managed_postgres' in start
    assert 'synchronize_managed_postgres_password' in start
    assert 'derive_managed_database_url' in start
    assert 'ensure_dispatch_worker_image' in start
    assert '--db-path "${DATA_DIR}/cairn.db"' not in start
    assert '--database-url "${CAIRN_DATABASE_URL}"' not in start
    assert 'CAIRN_DATABASE_URL_VALUE="${CAIRN_DATABASE_URL}"' in start
    assert 'database_fingerprint' in start
    assert 'write_pid_metadata "${SERVER_PID_FILE}" "${server_pid}" "${database_fingerprint}"' in start
    assert 'pid_is_running "${SERVER_PID_FILE}" "${database_fingerprint}"' in start
    assert 'Server 数据库身份已变化，重启 Server' in start
    assert '"${SERVER_URL}/health"' in start
    assert '"${SERVER_URL}/health"' in status
    assert 'write_server_endpoint' in start and 'load_active_server_endpoint' in status
    managed_start = start.index('start_managed_postgres')
    managed_image = start.index('ensure_dispatch_worker_image', managed_start)
    managed_sync = start.index('synchronize_managed_postgres_password', managed_start)
    assert managed_start < managed_image < managed_sync
    assert start.index('"${CAIRN_BIN}" db status') < start.index('ensure_local_secrets')
    assert start.index('ensure_local_secrets') < start.index('stage_initial_admin_password')
    assert start.index('stage_initial_admin_password') < start.index('auth bootstrap-runtime')
    assert start.index('auth bootstrap-runtime') < start.index('publish_initial_admin_password')
    assert start.index('publish_initial_admin_password') < start.index('setsid env -u CAIRN_DISPATCHER_TOKEN')
    assert start.index('/health"') < start.index('dispatch --config runtime/dispatch.yaml')
    assert 'acquire_operation_lock' in start and 'acquire_operation_lock' in stop
    assert 'flock -n 9' in common and '9>&-' in start
    assert 'flock --no-fork -n "${DISPATCHER_LOCK_FILE}"' in start
    assert 'dispatcher_pid_holds_lock_file "${dispatcher_pid}"' in start
    assert 'prepare_lock_file "${DISPATCHER_LOCK_FILE}"' in start
    assert 'env -u CAIRN_POSTGRES_PASSWORD -u CAIRN_POSTGRES_PORT' in common
    assert 'up -d --pull missing --wait --wait-timeout 120' in common
    assert '\\getenv new_password POSTGRES_PASSWORD' in common
    assert "ALTER ROLE cairn WITH PASSWORD" in common and "new_password" in common
    assert 'docker image inspect "${image}"' in common
    assert 'container/Dockerfile' in common
    assert "startup-healthcheck-only" not in start
    assert '"${DEPLOY_DIR}/verify.sh"' not in start
    assert 'write_pid_metadata "${DISPATCHER_PID_FILE}"' in start
    assert 'kill -TERM -- "-${dispatcher_pid}"' in start
    assert 'pid_is_running "${DISPATCHER_PID_FILE}" "${DISPATCH_CONFIG}" true' in start
    assert 'DISPATCHER_TOKEN_FINGERPRINT_FILE=' in start
    assert 'hashlib.sha256' in start
    assert 'Dispatcher credential 已变化，重启 Dispatcher' in start
    assert 'stop_managed_dispatcher "${DISPATCHER_PID_FILE}"' in start
    assert 'stop_managed_dispatcher "${PID_DIR}/dispatcher.pid"' in stop
    assert '"proc_start_time"' in common and 'os.getpgid(pid) != pid' in common
    assert 'dispatcher_ownership_state' in common
    assert all(state in common for state in ("tracked", "unknown", "untracked", "stale"))
    assert stop.index('dispatcher.pid') < stop.index('server.pid')
    assert all(f'state="{state}"' in status for state in ("running", "server-only", "degraded", "stopped"))
    assert 'ownership=%s' in status
    assert '/dispatcher/monitoring/summary' in status
    assert 'curl -fsS --config -' in status
    assert "printf 'CAIRN_DISPATCHER_TOKEN" not in status
    assert "CAIRN_ADMIN_TOKEN" not in start
    assert 'verify_deployment.py' not in install
    assert "CAIRN_ADMIN_TOKEN" not in install and "CAIRN_ADMIN_TOKEN" not in env_example
    assert 'runtime/locks"' not in install
    assert 'ensure_runtime_dirs' in install
    assert 'ensure_managed_postgres_env' not in install
    assert 'ensure_managed_postgres_env' in start
    assert 'postgres.env.example' in common
    assert 'POSTGRES_ENV_FILE="${RUNTIME_DIR}/postgres.env"' in common
    assert 'migrate_worker_config.py' in install
    assert 'if "${ROOT_DIR}/cairn/.venv/bin/python"' in install
    assert 'Server 可继续启动' in install
    assert 'start.sh --with-dispatcher' not in install
    assert '无需再次执行启动命令' in install
    assert "docker info" not in install and "docker image inspect" not in install
    assert "ARENA_SERVER_HOST=0.0.0.0" in env_example
    assert "ARENA_SERVER_PORT=8000" in env_example
    assert "ARENA_ADMIN_LOGIN=admin" in env_example
    assert "CAIRN_DATABASE_URL" in env_example
    assert "CAIRN_ARTIFACT_ROOT" in env_example
    assert "dispatcher.token" in env_example
    assert "CAIRN_TRUSTED_ORIGINS" in env_example
    assert "CAIRN_SESSION_COOKIE_SECURE" in env_example
    assert "CAIRN_MODEL_CREDENTIAL_KEYRING_FILE" in env_example
    assert "随机管理员密码" in install
    assert "直接运行 install.sh 只用于升级或刷新依赖" in install
    assert "不设置 CAIRN_DATABASE_URL" in env_example
    assert "External PostgreSQL 模式" in env_example
    assert "CAIRN_POSTGRES_PASSWORD=\n" in postgres_env_example
    assert "生成随机本机密码" in postgres_env_example
    assert "secrets.token_urlsafe(32)" in common
    assert "tempfile.mkstemp" in common and "os.replace(tmp, target)" in common
    assert "数据库 URL 与 Model keyring 只提供给 Server" in env_example
    verify_deployment.reject_placeholders(ROOT / "deploy/dispatch.yaml.example", ROOT / "deploy/arena.env.example")


def test_managed_postgres_env_is_strict_and_url_is_derived_in_memory(tmp_path: Path, common_script: Path) -> None:
    env_file = tmp_path / "postgres.env"
    env_file.write_text(
        "CAIRN_POSTGRES_PASSWORD=test%password\nCAIRN_POSTGRES_PORT=55432\n",
        encoding="utf-8",
    )
    command = r'''
source "$1"
POSTGRES_ENV_FILE="$2"
validate_managed_postgres_env
derive_managed_database_url
[[ "$CAIRN_DATABASE_URL" == "postgresql://cairn:test%25password@127.0.0.1:55432/cairn" ]]
'''

    result = subprocess.run(
        ["bash", "-c", command, "_", str(common_script), str(env_file)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "test%password" not in result.stdout
    assert "test%password" not in result.stderr

    env_file.write_text(
        "CAIRN_POSTGRES_PASSWORD=first\nCAIRN_POSTGRES_PASSWORD=second\nCAIRN_POSTGRES_PORT=55432\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; POSTGRES_ENV_FILE="$2"; validate_managed_postgres_env', "_", str(common_script), str(env_file)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "重复字段" in result.stderr
    assert "first" not in result.stderr and "second" not in result.stderr


def test_runtime_directories_reject_symlinks(tmp_path: Path, common_script: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    logs = tmp_path / "logs"
    logs.symlink_to(target, target_is_directory=True)
    command = r'''
source "$1"
RUNTIME_DIR="$2"
LOG_DIR="$3"
PID_DIR="$4"
DATA_DIR="$5"
SECRETS_DIR="$6"
ensure_runtime_dirs
'''

    result = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "_",
            str(common_script),
            str(tmp_path),
            str(logs),
            str(tmp_path / "pids"),
            str(tmp_path / "data"),
            str(tmp_path / "secrets"),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "不能是 symlink" in result.stderr

    runtime_link = tmp_path / "runtime-link"
    runtime_link.symlink_to(target, target_is_directory=True)
    result = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "_",
            str(common_script),
            str(runtime_link),
            str(runtime_link / "logs"),
            str(runtime_link / "pids"),
            str(runtime_link / "data"),
            str(runtime_link / "secrets"),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "runtime 目录不能是 symlink" in result.stderr

    runtime = tmp_path / "clean-runtime"
    directories = [runtime / name for name in ("logs", "pids", "data", "secrets")]
    result = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "_",
            str(common_script),
            str(runtime),
            *(str(path) for path in directories),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert all((path.stat().st_mode & 0o777) == 0o700 for path in [runtime, *directories])


def test_managed_postgres_env_is_generated_once_with_private_permissions(tmp_path: Path, common_script: Path) -> None:
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    (deploy_dir / "postgres.env.example").write_text(
        "# 本机数据库\nCAIRN_POSTGRES_PASSWORD=\nCAIRN_POSTGRES_PORT=55432\n",
        encoding="utf-8",
    )
    env_file = tmp_path / "runtime/postgres.env"
    command = r'''
source "$1"
PYTHON_BIN="$2"
DEPLOY_DIR="$3"
POSTGRES_ENV_FILE="$4"
ensure_managed_postgres_env
'''
    args = [
        "bash",
        "-c",
        command,
        "_",
        str(common_script),
        sys.executable,
        str(deploy_dir),
        str(env_file),
    ]

    first = subprocess.run(args, text=True, capture_output=True, check=False)

    assert first.returncode == 0, first.stderr
    content = env_file.read_text(encoding="utf-8")
    password_line = next(line for line in content.splitlines() if line.startswith("CAIRN_POSTGRES_PASSWORD="))
    password = password_line.partition("=")[2]
    assert password
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"
    assert password not in first.stdout and password not in first.stderr

    second = subprocess.run(args, text=True, capture_output=True, check=False)

    assert second.returncode == 0, second.stderr
    assert env_file.read_text(encoding="utf-8") == content
    assert password not in second.stdout and password not in second.stderr


def test_initial_admin_password_is_staged_before_noninteractive_delivery(tmp_path: Path, common_script: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(mode=0o700)
    password_file = secrets_dir / "initial-admin-password"
    command = r'''
source "$1"
PYTHON_BIN="$2"
SECRETS_DIR="$3"
INITIAL_ADMIN_PASSWORD_FILE="$4"
stage_initial_admin_password
[[ "$INITIAL_ADMIN_PASSWORD_STAGED" == "1" ]]
publish_initial_admin_password
'''

    result = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "_",
            str(common_script),
            sys.executable,
            str(secrets_dir),
            str(password_file),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    password = password_file.read_text(encoding="utf-8").rstrip("\n")
    assert password
    assert oct(password_file.stat().st_mode & 0o777) == "0o600"
    assert str(password_file) in result.stdout
    assert password not in result.stdout and password not in result.stderr

    second = subprocess.run(
        [
            "bash",
            "-c",
            command.replace(
                '[[ "$INITIAL_ADMIN_PASSWORD_STAGED" == "1" ]]',
                '[[ "$INITIAL_ADMIN_PASSWORD_STAGED" == "0" ]]',
            ),
            "_",
            str(common_script),
            sys.executable,
            str(secrets_dir),
            str(password_file),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert second.returncode == 0, second.stderr
    assert password_file.read_text(encoding="utf-8").rstrip("\n") == password


def test_postgres_compose_clears_ambient_authority(tmp_path: Path, common_script: Path) -> None:
    docker = tmp_path / "docker"
    record = tmp_path / "record"
    docker.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "${CAIRN_POSTGRES_PASSWORD-unset}" >"${DOCKER_RECORD}"\nprintf "%s\\n" "$*" >>"${DOCKER_RECORD}"\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env_file = tmp_path / "postgres.env"
    env_file.write_text(
        "CAIRN_POSTGRES_PASSWORD=file-authority\nCAIRN_POSTGRES_PORT=55432\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tmp_path}:{env['PATH']}",
            "CAIRN_POSTGRES_PASSWORD": "ambient-must-not-win",
            "CAIRN_POSTGRES_PORT": "60000",
            "DOCKER_RECORD": str(record),
        }
    )
    command = 'source "$1"; POSTGRES_ENV_FILE="$2"; postgres_compose config --quiet'

    result = subprocess.run(
        ["bash", "-c", command, "_", str(common_script), str(env_file)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    recorded = record.read_text(encoding="utf-8")
    assert recorded.splitlines()[0] == "unset"
    assert "-p arena-db" in recorded
    assert f"--env-file {env_file}" in recorded
    assert "-f" in recorded and "postgres.compose.yaml" in recorded
    assert "ambient-must-not-win" not in recorded


def test_native_postgres_compose_is_isolated_and_loopback_only() -> None:
    compose = (ROOT / "deploy/postgres.compose.yaml").read_text(encoding="utf-8")

    assert "postgres:16-alpine" in compose
    assert "CAIRN_POSTGRES_PASSWORD:?" in compose
    assert '127.0.0.1:${CAIRN_POSTGRES_PORT:-55432}:5432' in compose
    assert "arena_postgres_data:" in compose
    assert "pg_isready -U $$POSTGRES_USER -d $$POSTGRES_DB" in compose
    assert "restart: unless-stopped" in compose
    assert "cairn-server" not in compose
    assert "cairn-dispatcher" not in compose
    assert "CAIRN_DATABASE_URL" not in compose


def test_full_stack_container_startup_files_are_removed() -> None:
    assert not (ROOT / "docker-compose.yaml").exists()
    assert not (ROOT / "dispatch.compose.yaml").exists()
    assert not (ROOT / "Dockerfile").exists()
    assert not (ROOT / ".dockerignore").exists()
    assert not (ROOT / "container/Dockerfile.traffic").exists()


@pytest.fixture
def offline_verification_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """全部安装数据为临时合成内容，镜像入口只允许由命令 stub 响应。"""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "dispatch.yaml").write_text(
        (ROOT / "deploy/dispatch.yaml.example").read_text(encoding="utf-8"), encoding="utf-8",
    )
    (runtime / "arena.env").write_text("", encoding="utf-8")
    source = tmp_path / "cairn/src/cairn/dispatcher/tasks/process_runner.py"
    source.parent.mkdir(parents=True)
    source.write_text("# 离线验收测试占位\n", encoding="utf-8")
    monkeypatch.setattr(process_runner, "__file__", str(source))
    static = tmp_path / "cairn/src/cairn/server/static"
    static.mkdir(parents=True)
    (static / "index.html").write_text("<html></html>", encoding="utf-8")
    (tmp_path / "RELEASE-MANIFEST.json").write_text(json.dumps({
        "files": [], "expected_cairn_agent_version": "1.0.0", "expected_pi_sdk_version": "0.73.0",
    }), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("failure", [
    None, "inspect_exit", "platform", "version_exit", "agent_version", "sdk_version",
    "version_suffix", "version_stderr_only", "manifest_agent_version", "manifest_sdk_version",
    "self_check_exit", "self_check_json", "self_check_status", "self_check_engine",
    "self_check_sdk", "self_check_online", "self_check_numeric_offline", "self_check_array",
])
def test_full_verification_uses_offline_pi_checks_and_propagates_failures(
    offline_verification_root: Path, monkeypatch: pytest.MonkeyPatch, capsys, failure: str | None,
) -> None:
    calls = []
    if failure in {"manifest_agent_version", "manifest_sdk_version"}:
        path = offline_verification_root / "RELEASE-MANIFEST.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        field = "expected_cairn_agent_version" if failure == "manifest_agent_version" else "expected_pi_sdk_version"
        manifest[field] = "9.0.0"
        path.write_text(json.dumps(manifest), encoding="utf-8")

    def command(args, *, timeout=60):
        calls.append(args)
        if args[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(args, int(failure == "inspect_exit"),
                                               "sha256:fixture windows amd64" if failure == "platform" else "sha256:fixture linux amd64", "")
        assert args[:7] == ["docker", "run", "--rm", "--network", "none", "--entrypoint", "cairn-agent"]
        assert args[7] == "arena-worker:local"
        assert len(args) == 9
        if args[-1] == "--version":
            version = "cairn-agent 1.0.0 (pi-coding-agent 0.73.0)"
            if failure == "agent_version":
                version = version.replace("1.0.0", "9.0.0")
            if failure == "sdk_version":
                version = version.replace("0.73.0", "0.72.0")
            if failure == "version_suffix":
                version += " unexpected-extra-output"
            if failure == "version_stderr_only":
                return subprocess.CompletedProcess(args, 0, "", version)
            return subprocess.CompletedProcess(args, int(failure == "version_exit"), version + "\n", "")
        assert args[-1] == "--self-check"
        payload = {"status": "ok", "engine": "pi", "sdk_version": "0.73.0", "offline": True}
        for case, field, value in [
            ("self_check_status", "status", "failed"), ("self_check_engine", "engine", "legacy"),
            ("self_check_sdk", "sdk_version", "0.72.0"), ("self_check_online", "offline", False),
            ("self_check_numeric_offline", "offline", 1),
        ]:
            if failure == case:
                payload[field] = value
        output = "not-json" if failure == "self_check_json" else json.dumps(payload)
        if failure == "self_check_array":
            output = "[]"
        return subprocess.CompletedProcess(args, int(failure == "self_check_exit"), output, "")

    monkeypatch.setattr(verify_deployment, "run_command", command)
    monkeypatch.setattr(sys, "argv", ["verify_deployment.py", "--root", str(offline_verification_root), "--full"])

    result = verify_deployment.main()

    captured = capsys.readouterr()
    if failure is None:
        assert result == 0
        assert "验收通过" in captured.out
        assert [args[-1] for args in calls[1:]] == ["--version", "--self-check"]
    else:
        assert result == 1
        assert "验收失败" in captured.err
        assert "验收通过" not in captured.out
        expected_error = (
            "镜像不存在或不可读" if failure == "inspect_exit" else
            "镜像平台异常" if failure == "platform" else
            "离线自检" if failure.startswith("self_check") else "版本不匹配或命令失败"
        )
        assert expected_error in captured.err
    expected_checks = [] if failure in {"inspect_exit", "platform"} else ["--version"]
    if failure is None or failure.startswith("self_check"):
        expected_checks.append("--self-check")
    assert [args[-1] for args in calls[1:]] == expected_checks
    assert all("--request" not in args and "--env-file" not in args for args in calls)


@pytest.mark.parametrize("engine", ["claudecode", "codex", "mock", "pi"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("engine_field", ["engine", "type"])
def test_unexecutable_worker_config_never_counts_as_available(
    offline_verification_root: Path, monkeypatch: pytest.MonkeyPatch, capsys,
    engine: str, enabled: bool, engine_field: str,
) -> None:
    path = offline_verification_root / "runtime/dispatch.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["workers"] = [{"id": "fixture", engine_field: engine, "enabled": enabled,
                          "task_types": ["reason"], "concurrency": 1, "priority": 0}]
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setattr(verify_deployment, "run_command", lambda *args, **kwargs: pytest.fail("无效配置不能启动镜像验收"))
    monkeypatch.setattr(sys, "argv", ["verify_deployment.py", "--root", str(offline_verification_root)])

    result = verify_deployment.main()

    captured = capsys.readouterr()
    if engine == "pi" and not enabled:
        assert result == 0
        assert "enabled_pi_worker_count=0" in captured.out
    else:
        assert result == 1
        assert "不可执行" in captured.err
        assert "验收通过" not in captured.out
    assert "executable_worker_count=1" not in captured.out


@pytest.mark.parametrize("image,exists,labels,build_exit,label_exit,expected", [
    ("arena-worker:local", True, "pi 1.0.0 0.73.0", 0, 0, 0),
    ("custom-worker:fixture", True, "pi 1.0.0 0.73.0", 0, 0, 0),
    ("arena-worker:local", True, "<no value> <no value> <no value>", 0, 0, 1),
    ("arena-worker:local", True, "claudecode 1.0.0 0.73.0", 0, 0, 1),
    ("arena-worker:local", True, "pi 9.0.0 0.73.0", 0, 0, 1),
    ("arena-worker:local", True, "pi 1.0.0 0.72.0", 0, 0, 1),
    ("arena-worker:local", True, "pi 1.0.0 0.73.0", 0, 17, 1),
    ("custom-worker:fixture", False, "pi 1.0.0 0.73.0", 0, 0, 1),
    ("arena-worker:local", False, "pi 1.0.0 0.73.0", 0, 0, 0),
    ("arena-worker:local", False, "pi 1.0.0 0.72.0", 0, 0, 1),
    ("arena-worker:local", False, "pi 1.0.0 0.73.0", 23, 0, 23),
])
def test_worker_image_pi_labels_fail_closed_using_shell_stub(
    tmp_path: Path, common_script: Path, image: str, exists: bool, labels: str,
    build_exit: int, label_exit: int, expected: int,
) -> None:
    """只调用替身，检查已有镜像标签和候选验收后才更新引用。"""
    log = tmp_path / "docker-stub.log"
    (common_script.parent / "verify_deployment.py").write_text(
        'import os\nfrom pathlib import Path\n'
        'EXPECTED_CAIRN_AGENT_VERSION = "1.0.0"\nEXPECTED_PI_SDK_VERSION = "0.73.0"\n'
        'def verify_image(image, version, sdk):\n'
        '    assert (image, version, sdk) == ("sha256:candidate", "1.0.0", "0.73.0")\n'
        '    with Path(os.environ["LOG"]).open("a") as log:\n'
        '        log.write("verify candidate\\n")\n',
        encoding="utf-8",
    )
    command = r'''
source "$1"
resolve_dispatch_worker_images() { printf '%s\n' "$IMAGE"; }
docker() {
  printf '%s\n' "$*" >>"$LOG"
  if [[ "$1 $2" == "image inspect" ]]; then
    if [[ "$#" == 3 ]]; then return "$MISSING"; fi
    [[ "$#" == 5 && "$4" == "--format" ]] || return 96
    [[ "$5" == *io.cairn.agent.engine* && "$5" == *io.cairn.agent.version* && "$5" == *io.cairn.pi-sdk.version* ]] || return 96
    printf '%s\n' "$LABELS"
    return "$LABEL_EXIT"
  fi
  if [[ "$1" == "build" ]]; then
    [[ "$BUILD_EXIT" == 0 ]] || return "$BUILD_EXIT"
    while [[ "$1" != "--iidfile" ]]; do shift; done
    printf '%s\n' sha256:candidate >"$2"
    return 0
  fi
  [[ "$1 $2 $3" == "tag sha256:candidate arena-worker:local" ]] || return 95
}
ensure_runtime_dirs
ensure_dispatch_worker_image
'''
    result = subprocess.run(
        ["bash", "-c", command, "bash", str(common_script)],
        env={**os.environ, "IMAGE": image, "MISSING": str(int(not exists)), "LABELS": labels,
             "BUILD_EXIT": str(build_exit), "LABEL_EXIT": str(label_exit), "LOG": str(log)},
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == expected, result.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    should_build = not exists and image == "arena-worker:local"
    assert sum(line.startswith("build ") for line in calls) == int(should_build)
    should_check_labels = exists or (should_build and not build_exit)
    assert sum("--format" in line for line in calls) == int(should_check_labels)
    if should_check_labels and expected:
        assert "pi" in result.stderr and "不可执行" in result.stderr
    assert all(not line.startswith(("run ", "pull ", "rm ")) for line in calls)
    if should_build and not expected:
        assert calls[-2:] == ["verify candidate", "tag sha256:candidate arena-worker:local"]
    else:
        assert not any(line.startswith("tag ") for line in calls)


@pytest.mark.parametrize("baseline,registered,profile_ref,expected", [
    ("arena-worker:local", [], "default", ["arena-worker:local"]),
    ("custom:base", [{"id": "runner", "image": "arena-worker:local"}], "runner",
     ["custom:base", "arena-worker:local"]),
    ("custom:base", [{"id": "default", "image": "registry.invalid/runner@sha256:" + "a" * 64}], "default",
     ["custom:base", "registry.invalid/runner@sha256:" + "a" * 64]),
    ("custom:base", [{"id": "runner", "image": "custom:profile"}], "missing", None),
])
def test_worker_image_resolution_uses_dispatch_registries(
    tmp_path: Path, common_script: Path, baseline: str, registered: list[dict[str, str]],
    profile_ref: str, expected: list[str] | None,
) -> None:
    config = yaml.safe_load((ROOT / "deploy/dispatch.yaml.example").read_text())
    config["container"]["image"] = baseline
    config["images"] = registered
    config["execution_profiles"] = [{"id": "default", "image_ref": profile_ref}]
    path = tmp_path / "dispatch.yaml"
    content = yaml.safe_dump(config)
    path.write_text(content)
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; DISPATCH_CONFIG="$2"; resolve_dispatch_worker_images',
         "bash", str(common_script), str(path)],
        text=True, capture_output=True, check=False,
    )
    if expected is None:
        assert result.returncode != 0
        assert "Worker 配置加载失败" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == expected
    assert path.read_text() == content


@pytest.mark.parametrize("build_exit,check_exit,custom_missing,expected", [
    (0, 0, False, 0), (21, 0, False, 21), (0, 22, False, 22), (0, 0, True, 1),
])
def test_worker_refresh_promotes_only_verified_candidate(
    tmp_path: Path, common_script: Path, build_exit: int, check_exit: int,
    custom_missing: bool, expected: int,
) -> None:
    log = tmp_path / "refresh.log"
    (common_script.parent / "verify_deployment.py").write_text(
        'import os\nfrom pathlib import Path\n'
        'EXPECTED_CAIRN_AGENT_VERSION = "1.0.0"\nEXPECTED_PI_SDK_VERSION = "0.73.0"\n'
        'def verify_image(image, version, sdk):\n'
        '    with Path(os.environ["LOG"]).open("a") as log:\n'
        '        log.write("verify candidate\\n")\n'
        '    raise SystemExit(int(os.environ["CHECK_EXIT"]))\n',
        encoding="utf-8",
    )
    command = r'''
source "$1"
resolve_dispatch_worker_images() { printf '%s\n' arena-worker:local custom:profile; }
docker() {
  printf '%s\n' "$*" >>"$LOG"
  case "$1 $2" in
    "image inspect")
      if [[ "$#" == 3 ]]; then return "$CUSTOM_MISSING"; fi
      printf '%s\n' 'pi 1.0.0 0.73.0';;
    "build -f")
      [[ "$BUILD_EXIT" == 0 ]] || return "$BUILD_EXIT"
      while [[ "$1" != "--iidfile" ]]; do shift; done
      printf '%s\n' sha256:candidate >"$2";;
    "tag sha256:candidate")
      [[ "$3" == arena-worker:local ]];;
    *) return 95;;
  esac
}
ensure_runtime_dirs
ensure_dispatch_worker_image refresh
'''
    result = subprocess.run(
        ["bash", "-c", command, "bash", str(common_script)],
        env={**os.environ, "BUILD_EXIT": str(build_exit), "CHECK_EXIT": str(check_exit),
             "CUSTOM_MISSING": str(int(custom_missing)), "LOG": str(log)},
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == expected, result.stderr
    calls = log.read_text().splitlines()
    assert not any(line.startswith("image inspect arena-worker:local") for line in calls)
    assert sum(line.startswith("build ") for line in calls) == int(not custom_missing)
    assert not any("--no-cache" in line or line.startswith(("pull ", "prune ", "rm ")) for line in calls)
    assert not list((common_script.parent.parent / "runtime").glob(".worker-image.*"))
    if not expected:
        assert calls[-2:] == ["verify candidate", "tag sha256:candidate arena-worker:local"]
        assert "自定义 Worker 未自动更新: custom:profile" in result.stderr
    else:
        assert not any(line.startswith("tag ") for line in calls)


def test_custom_only_worker_refresh_does_not_build_or_retag(common_script: Path) -> None:
    command = r'''
source "$1"
resolve_dispatch_worker_images() { printf '%s\n' custom:profile; }
docker() {
  [[ "$1 $2 $3" == "image inspect custom:profile" ]] || return 95
  if [[ "$#" == 5 ]]; then printf '%s\n' 'pi 1.0.0 0.73.0'; fi
}
ensure_dispatch_worker_image refresh
'''
    result = subprocess.run(
        ["bash", "-c", command, "bash", str(common_script)],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "自定义 Worker 未自动更新: custom:profile" in result.stderr
    assert "默认本地 Worker 已准备" not in result.stdout


def test_placeholder_detection_rejects_unconfigured_files(tmp_path: Path) -> None:
    path = tmp_path / "dispatch.yaml"
    path.write_text("model: REPLACE_WITH_MODEL_ID", encoding="utf-8")
    try:
        verify_deployment.reject_placeholders(path)
    except RuntimeError as exc:
        assert "占位符" in str(exc)
    else:
        raise AssertionError("placeholder should be rejected")
