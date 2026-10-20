#!/usr/bin/env bash
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${DEPLOY_DIR}/.." && pwd)"
RUNTIME_DIR="${ROOT_DIR}/runtime"
VENV_DIR="${ROOT_DIR}/cairn/.venv"
CAIRN_BIN="${VENV_DIR}/bin/cairn"
PYTHON_BIN="${VENV_DIR}/bin/python"
DISPATCH_CONFIG="${RUNTIME_DIR}/dispatch.yaml"
ENV_FILE="${RUNTIME_DIR}/arena.env"
LOG_DIR="${RUNTIME_DIR}/logs"
PID_DIR="${RUNTIME_DIR}/pids"
DATA_DIR="${RUNTIME_DIR}/data"
SECRETS_DIR="${RUNTIME_DIR}/secrets"
KEYRING_FILE="${SECRETS_DIR}/model-credential-keyring.json"
DISPATCHER_TOKEN_FILE="${SECRETS_DIR}/dispatcher.token"
INITIAL_ADMIN_PASSWORD_FILE="${SECRETS_DIR}/initial-admin-password"
SERVER_ENDPOINT_FILE="${PID_DIR}/server.endpoint"
DISPATCHER_LOCK_FILE="${PID_DIR}/dispatcher.lock"
OPERATION_LOCK_FILE="${PID_DIR}/start.lock"
POSTGRES_ENV_FILE="${RUNTIME_DIR}/postgres.env"
POSTGRES_COMPOSE_FILE="${DEPLOY_DIR}/postgres.compose.yaml"
POSTGRES_PROJECT="arena-db"
POSTGRES_SERVICE="arena-postgres"
DEFAULT_WORKER_IMAGE="arena-worker:local"

ensure_runtime_dirs() {
  local dir
  if [[ -L "${RUNTIME_DIR}" ]]; then
    echo "runtime 目录不能是 symlink" >&2
    return 1
  fi
  mkdir -p "${RUNTIME_DIR}"
  if [[ ! -d "${RUNTIME_DIR}" || ! -O "${RUNTIME_DIR}" ]]; then
    echo "runtime 目录必须由当前用户拥有" >&2
    return 1
  fi
  for dir in "${LOG_DIR}" "${PID_DIR}" "${DATA_DIR}" "${SECRETS_DIR}"; do
    if [[ -L "${dir}" ]]; then
      printf 'runtime 目录不能是 symlink: %s\n' "${dir}" >&2
      return 1
    fi
  done
  mkdir -p "${LOG_DIR}" "${PID_DIR}" "${DATA_DIR}" "${SECRETS_DIR}"
  for dir in "${LOG_DIR}" "${PID_DIR}" "${DATA_DIR}" "${SECRETS_DIR}"; do
    if [[ ! -d "${dir}" || ! -O "${dir}" ]]; then
      printf 'runtime 目录必须由当前用户拥有: %s\n' "${dir}" >&2
      return 1
    fi
  done
  chmod 0700 "${RUNTIME_DIR}" "${LOG_DIR}" "${PID_DIR}" "${DATA_DIR}" "${SECRETS_DIR}"
}

ensure_application_initialized() {
  local required=(
    "${PYTHON_BIN}"
    "${CAIRN_BIN}"
    "${ENV_FILE}"
    "${DISPATCH_CONFIG}"
  )
  local missing=0
  local path
  for path in "${required[@]}"; do
    if [[ ! -e "${path}" ]]; then
      missing=1
      break
    fi
  done
  if [[ "${missing}" == "1" ]]; then
    "${DEPLOY_DIR}/install.sh"
  fi
  for path in "${PYTHON_BIN}" "${CAIRN_BIN}"; do
    [[ -x "${path}" ]] || { printf '安装后缺少可执行文件: %s\n' "${path}" >&2; return 1; }
  done
  for path in "${ENV_FILE}" "${DISPATCH_CONFIG}"; do
    if [[ -L "${path}" || ! -f "${path}" || ! -O "${path}" ]]; then
      printf '安装后配置必须是当前用户拥有的普通文件: %s\n' "${path}" >&2
      return 1
    fi
  done
}

validate_external_database_url() {
  CAIRN_DATABASE_URL_VALUE="${CAIRN_DATABASE_URL}" "${PYTHON_BIN}" - <<'PY'
import os
import re
from urllib.parse import unquote, urlsplit

value = os.environ["CAIRN_DATABASE_URL_VALUE"]
if any(ord(ch) < 32 or ch.isspace() for ch in value):
    raise SystemExit("External PostgreSQL URL 包含空白或控制字符")
if re.search(r"%(?![0-9A-Fa-f]{2})", value):
    raise SystemExit("External PostgreSQL URL 包含无效 percent escape")
try:
    parsed = urlsplit(value)
    port = parsed.port
except ValueError:
    raise SystemExit("External PostgreSQL URL 的 host 或 port 无效")
if parsed.scheme not in {"postgresql", "postgres"}:
    raise SystemExit("External PostgreSQL URL 仅支持 postgresql:// 或 postgres://")
if not parsed.hostname:
    raise SystemExit("External PostgreSQL URL 缺少 hostname")
if port is not None and not 1 <= port <= 65535:
    raise SystemExit("External PostgreSQL URL 的 port 必须是 1..65535")
if not parsed.path or not unquote(parsed.path).strip("/"):
    raise SystemExit("External PostgreSQL URL 缺少 database name")
if parsed.fragment:
    raise SystemExit("External PostgreSQL URL 不能包含 fragment")
PY
}

postgres_compose() {
  env -u CAIRN_POSTGRES_PASSWORD -u CAIRN_POSTGRES_PORT \
    docker compose \
      -p "${POSTGRES_PROJECT}" \
      --env-file "${POSTGRES_ENV_FILE}" \
      -f "${POSTGRES_COMPOSE_FILE}" \
      "$@"
}

require_docker_daemon() {
  command -v docker >/dev/null 2>&1 || { echo "缺少 Docker CLI" >&2; return 1; }
  docker info >/dev/null 2>&1 || { echo "Docker daemon 不可用，请检查服务状态和当前用户权限" >&2; return 1; }
}

require_docker_compose() {
  docker compose version >/dev/null 2>&1 || { echo "缺少 Docker Compose v2" >&2; return 1; }
}

ensure_managed_postgres_env() {
  if [[ -L "${POSTGRES_ENV_FILE}" ]]; then
    echo "runtime/postgres.env 不能是 symlink" >&2
    return 1
  fi
  if [[ ! -f "${POSTGRES_ENV_FILE}" ]]; then
    "${PYTHON_BIN}" - "${DEPLOY_DIR}/postgres.env.example" "${POSTGRES_ENV_FILE}" <<'PY'
import os
from pathlib import Path
import secrets
import sys
import tempfile

source = Path(sys.argv[1])
target = Path(sys.argv[2])
content = source.read_text(encoding="utf-8")
marker = "CAIRN_POSTGRES_PASSWORD=\n"
if content.count(marker) != 1:
    raise SystemExit("PostgreSQL 配置模板缺少唯一的空密码字段")
content = content.replace(marker, f"CAIRN_POSTGRES_PASSWORD={secrets.token_urlsafe(32)}\n")
target.parent.mkdir(parents=True, exist_ok=True)
fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
tmp = Path(name)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        fd = -1
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
finally:
    if fd >= 0:
        os.close(fd)
    if tmp.exists():
        tmp.unlink()
PY
  fi
  if [[ ! -f "${POSTGRES_ENV_FILE}" || ! -O "${POSTGRES_ENV_FILE}" ]]; then
    echo "runtime/postgres.env 必须是当前用户拥有的普通文件" >&2
    return 1
  fi
  chmod 0600 "${POSTGRES_ENV_FILE}"
}

validate_managed_postgres_env() {
  "${PYTHON_BIN}" - "${POSTGRES_ENV_FILE}" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
try:
    raw = path.read_bytes()
except OSError as exc:
    raise SystemExit(f"无法读取本地 PostgreSQL 配置: {exc}")
if b"\x00" in raw or b"\r" in raw:
    raise SystemExit("本地 PostgreSQL 配置包含不支持的控制字符")
try:
    text = raw.decode("utf-8")
except UnicodeDecodeError:
    raise SystemExit("本地 PostgreSQL 配置必须使用 UTF-8")
values = {}
for line_number, line in enumerate(text.split("\n"), 1):
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        continue
    if line != stripped or "=" not in line:
        raise SystemExit(f"本地 PostgreSQL 配置第 {line_number} 行格式无效")
    key, value = line.split("=", 1)
    if key not in {"CAIRN_POSTGRES_PASSWORD", "CAIRN_POSTGRES_PORT"}:
        raise SystemExit(f"本地 PostgreSQL 配置包含未知字段: {key}")
    if key in values:
        raise SystemExit(f"本地 PostgreSQL 配置重复字段: {key}")
    values[key] = value
password = values.get("CAIRN_POSTGRES_PASSWORD", "")
if not password:
    raise SystemExit("CAIRN_POSTGRES_PASSWORD 不能为空")
if any(ord(ch) < 33 or ord(ch) == 127 for ch in password):
    raise SystemExit("CAIRN_POSTGRES_PASSWORD 不能包含空白或控制字符")
if any(ch in password for ch in "#$'\"\\"):
    raise SystemExit("CAIRN_POSTGRES_PASSWORD 不能包含 #、$、引号或反斜杠")
port = values.get("CAIRN_POSTGRES_PORT", "")
if not re.fullmatch(r"[0-9]+", port) or not 1 <= int(port) <= 65535:
    raise SystemExit("CAIRN_POSTGRES_PORT 必须是 1..65535")
PY
  chmod 0600 "${POSTGRES_ENV_FILE}"
}

start_managed_postgres() {
  postgres_compose config --quiet
  postgres_compose up -d --pull missing --wait --wait-timeout 120 "${POSTGRES_SERVICE}"
}

synchronize_managed_postgres_password() {
  if ! postgres_compose exec -T "${POSTGRES_SERVICE}" sh -ceu '
    {
      printf "%s\n" "\\getenv new_password POSTGRES_PASSWORD"
      printf "%s\n" "ALTER ROLE cairn WITH PASSWORD :'\''new_password'\'';"
    } | psql --no-psqlrc --set ON_ERROR_STOP=1 --username cairn --dbname cairn >/dev/null
    PGPASSWORD="${POSTGRES_PASSWORD}" psql \
      --no-psqlrc --set ON_ERROR_STOP=1 \
      --host 127.0.0.1 --username cairn --dbname cairn \
      --command "SELECT 1" >/dev/null
  '; then
    echo "本地 PostgreSQL role 密码同步或认证失败" >&2
    return 1
  fi
}

derive_managed_database_url() {
  CAIRN_DATABASE_URL="$("${PYTHON_BIN}" - "${POSTGRES_ENV_FILE}" <<'PY'
from pathlib import Path
import sys
from urllib.parse import quote

values = {}
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    stripped = line.strip()
    if stripped and not stripped.startswith("#"):
        key, value = stripped.split("=", 1)
        values[key] = value
password = quote(values["CAIRN_POSTGRES_PASSWORD"], safe="")
port = values["CAIRN_POSTGRES_PORT"]
print(f"postgresql://cairn:{password}@127.0.0.1:{port}/cairn")
PY
  )"
  export CAIRN_DATABASE_URL
}

resolve_dispatch_worker_images() {
  "${PYTHON_BIN}" - "${DISPATCH_CONFIG}" <<'PY'
from pathlib import Path
import sys
import yaml
from cairn.dispatcher.config import DispatchConfig

try:
    config = DispatchConfig.load(Path(sys.argv[1]))
except (ValueError, yaml.YAMLError):
    # 校验异常可能包含完整模型绑定，不能把凭据输入写入部署日志。
    raise SystemExit("Worker 配置加载失败；请检查 runtime/dispatch.yaml 的镜像、画像和模型绑定") from None
# 基线用于启动自检，注册项和 allowed_images 还可被画像或场景直接选择。
images = dict.fromkeys([
    config.container.image,
    *config.container.allowed_images,
    *(entry.image for entry in config.images),
])
if any(not image or "\n" in image or "\r" in image for image in images):
    raise SystemExit("runtime/dispatch.yaml 的 Worker 镜像引用不能为空或包含换行")
print("\n".join(images))
PY
}

verify_pi_worker_image_labels() {
  local image="$1"
  local labels
  # 同名旧镜像不可当作 pi 复用；新构建也必须通过同一标签校验。
  if ! labels="$(docker image inspect "${image}" --format '{{ index .Config.Labels "io.cairn.agent.engine" }} {{ index .Config.Labels "io.cairn.agent.version" }} {{ index .Config.Labels "io.cairn.pi-sdk.version" }}')"; then
    printf 'Worker image 不可执行：无法读取 pi 标签: %s\n' "${image}" >&2
    return 1
  fi
  if [[ "${labels}" != "pi 1.0.0 0.73.0" ]]; then
    printf 'Worker image 不可执行：要求 pi 1.0.0 / SDK 0.73.0 标签，请显式重建或更换镜像: %s\n' "${image}" >&2
    return 1
  fi
}

build_default_dispatch_worker_image() (
  local iid_file candidate
  iid_file="$(mktemp "${RUNTIME_DIR}/.worker-image.XXXXXX")" || return
  trap 'rm -f -- "${iid_file}"' EXIT
  # 不直接覆盖可用 tag；构建或离线验收失败时保留原引用及所有构建缓存。
  docker build \
    -f "${ROOT_DIR}/container/Dockerfile" \
    --iidfile "${iid_file}" \
    "${ROOT_DIR}/container" || return
  candidate="$(<"${iid_file}")"
  verify_pi_worker_image_labels "${candidate}" || return
  "${PYTHON_BIN}" - "${DEPLOY_DIR}" "${candidate}" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from verify_deployment import verify_image, EXPECTED_CAIRN_AGENT_VERSION, EXPECTED_PI_SDK_VERSION

verify_image(sys.argv[2], EXPECTED_CAIRN_AGENT_VERSION, EXPECTED_PI_SDK_VERSION)
PY
  local check_status=$?
  [[ "${check_status}" == "0" ]] || return "${check_status}"
  docker tag "${candidate}" "${DEFAULT_WORKER_IMAGE}" || return
  printf '默认本地 Worker 已准备: %s (%s)；仅后续新建容器使用。\n' "${DEFAULT_WORKER_IMAGE}" "${candidate}"
)

ensure_dispatch_worker_image() {
  local mode="${1:-missing}"
  local images image build_default=0
  images="$(resolve_dispatch_worker_images)" || return
  while IFS= read -r image; do
    if [[ "${image}" == "${DEFAULT_WORKER_IMAGE}" && "${mode}" == "refresh" ]]; then
      build_default=1
    elif docker image inspect "${image}" >/dev/null 2>&1; then
      verify_pi_worker_image_labels "${image}" || return
    elif [[ "${image}" == "${DEFAULT_WORKER_IMAGE}" ]]; then
      build_default=1
    else
      printf 'runtime/dispatch.yaml 配置的 Worker image 不存在: %s；请先 pull、load 或 build 配套镜像。\n' "${image}" >&2
      return 1
    fi
    if [[ "${mode}" == "refresh" && "${image}" != "${DEFAULT_WORKER_IMAGE}" ]]; then
      printf '自定义 Worker 未自动更新: %s；需自行提供配套镜像，标签检查不证明版本配套。\n' "${image}" >&2
    fi
  done <<<"${images}"
  if [[ "${build_default}" == "1" ]]; then
    echo "使用本地缓存构建 ${DEFAULT_WORKER_IMAGE} 的新版候选镜像。"
    build_default_dispatch_worker_image || return
  fi
}

ensure_runtime_env() {
  if [[ -L "${ENV_FILE}" ]]; then
    echo "runtime/arena.env 不能是 symlink" >&2
    return 1
  fi
  if [[ ! -f "${ENV_FILE}" ]]; then
    cp "${DEPLOY_DIR}/arena.env.example" "${ENV_FILE}"
  fi
  if [[ ! -f "${ENV_FILE}" || ! -O "${ENV_FILE}" ]]; then
    echo "runtime/arena.env 必须是当前用户拥有的普通文件" >&2
    return 1
  fi
  chmod 0600 "${ENV_FILE}"
}

load_runtime_env() {
  ensure_runtime_env
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
}

resolve_server_endpoint() {
  SERVER_HOST="${START_HOST_OVERRIDE:-${ARENA_SERVER_HOST:-0.0.0.0}}"
  SERVER_PORT="${START_PORT_OVERRIDE:-${ARENA_SERVER_PORT:-8000}}"
  [[ -n "${SERVER_HOST}" ]] || { echo "Host 不能为空" >&2; return 1; }
  if [[ ! "${SERVER_PORT}" =~ ^[0-9]+$ ]] || (( SERVER_PORT < 1 || SERVER_PORT > 65535 )); then
    printf 'Port 必须是 1..65535: %s\n' "${SERVER_PORT}" >&2
    return 1
  fi
  HEALTH_HOST="${SERVER_HOST}"
  if [[ "${HEALTH_HOST}" == "0.0.0.0" || "${HEALTH_HOST}" == "::" || "${HEALTH_HOST}" == "[::]" ]]; then
    HEALTH_HOST="127.0.0.1"
  fi
  if [[ "${HEALTH_HOST}" == *:* && "${HEALTH_HOST}" != \[*\] ]]; then
    HEALTH_HOST="[${HEALTH_HOST}]"
  fi
  SERVER_URL="http://${HEALTH_HOST}:${SERVER_PORT}"
}

configure_trusted_origins() {
  local stock="http://127.0.0.1:8000,http://localhost:8000"
  if [[ -n "${CAIRN_TRUSTED_ORIGINS:-}" && "${CAIRN_TRUSTED_ORIGINS}" != "${stock}" ]]; then
    export CAIRN_TRUSTED_ORIGINS
    return
  fi
  local origins="http://127.0.0.1:${SERVER_PORT},http://localhost:${SERVER_PORT}"
  if [[ "${SERVER_HOST}" != "0.0.0.0" && "${SERVER_HOST}" != "::" && "${SERVER_HOST}" != "[::]" && "${SERVER_HOST}" != "127.0.0.1" && "${SERVER_HOST}" != "localhost" ]]; then
    local origin_host="${SERVER_HOST}"
    if [[ "${origin_host}" == *:* && "${origin_host}" != \[*\] ]]; then
      origin_host="[${origin_host}]"
    fi
    origins="${origins},http://${origin_host}:${SERVER_PORT}"
  fi
  export CAIRN_TRUSTED_ORIGINS="${origins}"
}

write_server_endpoint() {
  local tmp
  tmp="$(mktemp "${PID_DIR}/.server.endpoint.XXXXXX")"
  chmod 0600 "${tmp}"
  printf '%s\n%s\n' "${SERVER_HOST}" "${SERVER_PORT}" >"${tmp}"
  mv -f "${tmp}" "${SERVER_ENDPOINT_FILE}"
}

load_active_server_endpoint() {
  [[ -f "${SERVER_ENDPOINT_FILE}" ]] || return 1
  local lines=()
  mapfile -t lines <"${SERVER_ENDPOINT_FILE}"
  [[ "${#lines[@]}" -eq 2 ]] || return 1
  ACTIVE_SERVER_HOST="${lines[0]}"
  ACTIVE_SERVER_PORT="${lines[1]}"
  [[ -n "${ACTIVE_SERVER_HOST}" && "${ACTIVE_SERVER_PORT}" =~ ^[0-9]+$ ]]
}

ensure_local_secrets() {
  export CAIRN_MODEL_CREDENTIAL_KEYRING_FILE="${CAIRN_MODEL_CREDENTIAL_KEYRING_FILE:-${KEYRING_FILE}}"
  "${PYTHON_BIN}" - "${CAIRN_MODEL_CREDENTIAL_KEYRING_FILE}" "${DISPATCHER_TOKEN_FILE}" <<'PY'
import base64
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile

keyring = Path(sys.argv[1]).expanduser().resolve(strict=False)
token_file = Path(sys.argv[2])


def validate_private_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError(f"权限不安全（要求 0600）: {path}")
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise RuntimeError(f"文件 owner 与当前用户不一致: {path}")
        return os.read(fd, 1024 * 1024)
    finally:
        os.close(fd)


def atomic_private_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise RuntimeError(f"secret 目录不能是 symlink: {path.parent}")
    os.chmod(path.parent, 0o700)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600, follow_symlinks=False)
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp.exists():
            tmp.unlink()


if not keyring.exists():
    document = {
        "active_version": "key-v1",
        "keys": {"key-v1": base64.b64encode(os.urandom(32)).decode("ascii")},
    }
    atomic_private_write(keyring, (json.dumps(document, separators=(",", ":")) + "\n").encode())
raw = validate_private_file(keyring)
try:
    document = json.loads(raw)
    active = document["active_version"]
    encoded = document["keys"][active]
    if len(base64.b64decode(encoded, validate=True)) != 32:
        raise ValueError
except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
    raise RuntimeError(f"Model credential keyring 格式无效: {keyring}") from exc

if not token_file.exists():
    atomic_private_write(token_file, (secrets.token_urlsafe(32) + "\n").encode())
token = validate_private_file(token_file).decode("utf-8").strip()
if not token or len(token) > 1024 or any(ch.isspace() for ch in token):
    raise RuntimeError(f"Dispatcher token 文件无效: {token_file}")
PY
}

stage_initial_admin_password() {
  INITIAL_ADMIN_PASSWORD_STAGED=0
  if [[ -e "${INITIAL_ADMIN_PASSWORD_FILE}" || -L "${INITIAL_ADMIN_PASSWORD_FILE}" ]]; then
    "${PYTHON_BIN}" - "${INITIAL_ADMIN_PASSWORD_FILE}" <<'PY'
import os
from pathlib import Path
import stat
import sys

path = Path(sys.argv[1])
flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
try:
    fd = os.open(path, flags)
except OSError as exc:
    raise SystemExit(f"无法读取初始管理员密码文件: {path}") from exc
try:
    info = os.fstat(fd)
    if info.st_size > 4096:
        raise SystemExit(f"初始管理员密码文件内容过长: {path}")
    raw = os.read(fd, 4097)
    if len(raw) > 4096:
        raise SystemExit(f"初始管理员密码文件内容过长: {path}")
finally:
    os.close(fd)
if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
    raise SystemExit(f"初始管理员密码文件权限不安全（要求 0600）: {path}")
if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
    raise SystemExit(f"初始管理员密码文件 owner 与当前用户不一致: {path}")
try:
    value = raw.decode("utf-8")
except UnicodeDecodeError as exc:
    raise SystemExit(f"初始管理员密码文件必须使用 UTF-8: {path}") from exc
if value.endswith("\n"):
    value = value[:-1]
if not value or "\n" in value or "\r" in value:
    raise SystemExit(f"初始管理员密码文件内容无效: {path}")
PY
    return 0
  fi
  "${PYTHON_BIN}" - "${INITIAL_ADMIN_PASSWORD_FILE}" <<'PY'
import os
from pathlib import Path
import secrets
import sys
import tempfile

path = Path(sys.argv[1])
fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
tmp = Path(name)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        fd = -1
        handle.write(secrets.token_urlsafe(18) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    os.chmod(path, 0o600, follow_symlinks=False)
finally:
    if fd >= 0:
        os.close(fd)
    if tmp.exists():
        tmp.unlink()
PY
  INITIAL_ADMIN_PASSWORD_STAGED=1
}

publish_initial_admin_password() {
  if [[ -t 1 ]]; then
    local password
    password="$(<"${INITIAL_ADMIN_PASSWORD_FILE}")"
    printf '  初始管理员密码: %s\n' "${password}"
    rm -f "${INITIAL_ADMIN_PASSWORD_FILE}"
    unset password
    return 0
  fi
  printf '初始管理员密码已写入 owner-only 文件: %s\n' "${INITIAL_ADMIN_PASSWORD_FILE}"
}

pid_file_identity() {
  local pid_file="$1"
  local expected_config="${2:-}"
  local require_group_leader="${3:-false}"
  "${PYTHON_BIN}" - "${pid_file}" "${expected_config}" "${require_group_leader}" <<'PY'
import json
import os
from pathlib import Path
import sys

path = Path(sys.argv[1])
expected_config = sys.argv[2]
require_group_leader = sys.argv[3] == "true"
try:
    raw = path.read_text(encoding="utf-8").strip()
except OSError:
    raise SystemExit(1)
if raw.isdigit():
    if expected_config or require_group_leader:
        raise SystemExit(1)
    print(raw)
    raise SystemExit(0)
try:
    metadata = json.loads(raw)
    pid = int(metadata["pid"])
    expected = str(metadata["proc_start_time"])
    stat_fields = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8").rsplit(") ", 1)[1].split()
except (KeyError, OSError, TypeError, ValueError, IndexError, json.JSONDecodeError):
    raise SystemExit(1)
if not stat_fields or stat_fields[0] == "Z" or len(stat_fields) <= 19 or stat_fields[19] != expected:
    raise SystemExit(1)
if expected_config and metadata.get("config") != expected_config:
    raise SystemExit(1)
if require_group_leader and os.getpgid(pid) != pid:
    raise SystemExit(1)
print(pid)
PY
}

pid_is_running() {
  local pid_file="$1"
  local expected_config="${2:-}"
  local require_group_leader="${3:-false}"
  [[ -f "${pid_file}" ]] || return 1
  local pid
  pid="$(pid_file_identity "${pid_file}" "${expected_config}" "${require_group_leader}")" || return 1
  kill -0 "${pid}" 2>/dev/null
}

write_pid_metadata() {
  local pid_file="$1"
  local pid="$2"
  local config="$3"
  local require_group_leader="${4:-false}"
  "${PYTHON_BIN}" - "${pid_file}" "${pid}" "${config}" "${require_group_leader}" <<'PY'
import json
import os
from pathlib import Path
import sys
import tempfile
import time

path = Path(sys.argv[1])
pid = int(sys.argv[2])
config = sys.argv[3]
require_group_leader = sys.argv[4] == "true"
deadline = time.monotonic() + 2.0
proc_start_time = None
while time.monotonic() < deadline:
    try:
        fields = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8").rsplit(") ", 1)[1].split()
        if fields and fields[0] != "Z" and len(fields) > 19:
            proc_start_time = fields[19]
            break
    except (FileNotFoundError, IndexError, OSError):
        pass
    time.sleep(0.02)
if proc_start_time is None:
    raise SystemExit(1)
if require_group_leader and os.getpgid(pid) != pid:
    raise SystemExit(1)
path.parent.mkdir(parents=True, exist_ok=True)
fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
tmp_path = Path(tmp_name)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        fd = -1
        json.dump(
            {"config": config, "pid": pid, "proc_start_time": proc_start_time},
            handle,
            ensure_ascii=False,
            sort_keys=True,
        )
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    os.chmod(path, 0o600, follow_symlinks=False)
finally:
    if fd >= 0:
        os.close(fd)
    if tmp_path.exists():
        tmp_path.unlink()
PY
}

stop_pid_file() {
  local pid_file="$1"
  local label="$2"
  local process_group="${3:-false}"
  local expected_config="${4:-}"
  if ! pid_is_running "${pid_file}" "${expected_config}" "${process_group}"; then
    rm -f "${pid_file}"
    return 0
  fi
  local pid
  pid="$(pid_file_identity "${pid_file}" "${expected_config}" "${process_group}")"
  if [[ "${process_group}" == "true" ]]; then
    kill -TERM -- "-${pid}" 2>/dev/null || true
  else
    kill -TERM "${pid}" 2>/dev/null || true
  fi
  for _ in 1 2 3 4 5; do
    kill -0 "${pid}" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "${pid}" 2>/dev/null; then
    if [[ "${process_group}" == "true" ]]; then
      kill -KILL -- "-${pid}" 2>/dev/null || true
    else
      kill -KILL "${pid}" 2>/dev/null || true
    fi
  fi
  rm -f "${pid_file}"
  printf '%s 已停止\n' "${label}"
}

require_flock() {
  command -v flock >/dev/null 2>&1 || {
    echo "Arena 进程所有权保护需要 flock，当前系统未安装" >&2
    return 1
  }
}

prepare_lock_file() {
  local lock_file="$1"
  "${PYTHON_BIN}" - "${lock_file}" <<'PY'
import os
from pathlib import Path
import stat
import sys

path = Path(sys.argv[1])
flags = os.O_CREAT | os.O_RDWR
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
try:
    fd = os.open(path, flags, 0o600)
except OSError as exc:
    raise SystemExit(f"无法安全打开锁文件 {path}: {exc}")
try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise SystemExit(f"锁文件必须是当前用户拥有且无硬链接的普通文件: {path}")
    os.fchmod(fd, 0o600)
finally:
    os.close(fd)
PY
}

validate_lock_fd() {
  local fd="$1"
  local lock_file="$2"
  "${PYTHON_BIN}" - "${fd}" "${lock_file}" <<'PY'
import os
from pathlib import Path
import stat
import sys

fd = int(sys.argv[1])
path = Path(sys.argv[2])
try:
    opened = os.fstat(fd)
    current = path.stat(follow_symlinks=False)
except OSError:
    raise SystemExit(1)
valid = (
    stat.S_ISREG(opened.st_mode)
    and opened.st_uid == os.getuid()
    and opened.st_nlink == 1
    and (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)
)
raise SystemExit(0 if valid else 1)
PY
}

lock_file_identity() {
  local lock_file="$1"
  "${PYTHON_BIN}" - "${lock_file}" <<'PY'
from pathlib import Path
import stat
import sys

try:
    info = Path(sys.argv[1]).stat(follow_symlinks=False)
except OSError:
    raise SystemExit(1)
if not stat.S_ISREG(info.st_mode):
    raise SystemExit(1)
print(f"{info.st_dev}:{info.st_ino}")
PY
}

flock_owner_pid() {
  local lock_identity="$1"
  "${PYTHON_BIN}" - "${lock_identity}" <<'PY'
import os
from pathlib import Path
import sys

expected_dev, expected_ino = (int(part) for part in sys.argv[1].split(":"))
try:
    lines = Path("/proc/locks").read_text(encoding="utf-8").splitlines()
except OSError:
    raise SystemExit(1)
for line in lines:
    fields = line.split()
    if len(fields) < 8 or fields[1] != "FLOCK" or fields[3] != "WRITE":
        continue
    try:
        owner = int(fields[4])
        major_hex, minor_hex, inode_text = fields[5].split(":")
        device = os.makedev(int(major_hex, 16), int(minor_hex, 16))
        inode = int(inode_text)
    except (ValueError, IndexError):
        continue
    if (device, inode) == (expected_dev, expected_ino):
        print(owner)
        raise SystemExit(0)
raise SystemExit(1)
PY
}

acquire_operation_lock() {
  require_flock
  prepare_lock_file "${OPERATION_LOCK_FILE}"
  if [[ "${ARENA_OPERATION_LOCK_INHERITED:-0}" == "1" ]]; then
    validate_lock_fd 9 "${OPERATION_LOCK_FILE}" || {
      echo "继承的操作锁文件描述符无效" >&2
      return 1
    }
    flock -n 9 || {
      echo "继承的操作锁未持有排他锁" >&2
      return 1
    }
    return 0
  fi
  exec 9>>"${OPERATION_LOCK_FILE}"
  validate_lock_fd 9 "${OPERATION_LOCK_FILE}" || {
    exec 9>&-
    echo "操作锁文件身份在打开期间发生变化" >&2
    return 1
  }
  if ! flock -n 9; then
    echo "已有 Arena 启动或停止过程正在执行，请稍后重试" >&2
    return 1
  fi
}

dispatcher_pid_holds_lock_file() {
  local pid="$1"
  local lock_file="${2:-${DISPATCHER_LOCK_FILE}}"
  local identity owner
  identity="$(lock_file_identity "${lock_file}")" || return 1
  owner="$(flock_owner_pid "${identity}")" || return 1
  [[ "${owner}" == "${pid}" ]]
}

dispatcher_lock_is_free() {
  local lock_file="${1:-${DISPATCHER_LOCK_FILE}}"
  prepare_lock_file "${lock_file}" || return 1
  local fd
  exec {fd}>>"${lock_file}"
  if ! validate_lock_fd "${fd}" "${lock_file}"; then
    eval "exec ${fd}>&-"
    return 1
  fi
  if flock -n "${fd}"; then
    if validate_lock_fd "${fd}" "${lock_file}"; then
      flock -u "${fd}"
      eval "exec ${fd}>&-"
      return 0
    fi
    flock -u "${fd}"
  fi
  eval "exec ${fd}>&-"
  return 1
}

# 输出 tracked、unknown、untracked 或 stale，调用方必须对非 tracked/stale 状态 fail closed。
dispatcher_ownership_state() {
  local pid_file="$1"
  local expected_config="$2"
  require_flock || return 1
  local pid=""
  if pid_is_running "${pid_file}" "${expected_config}" true; then
    pid="$(pid_file_identity "${pid_file}" "${expected_config}" true)"
  fi
  if dispatcher_lock_is_free; then
    if [[ -n "${pid}" ]]; then
      echo "untracked"
    else
      echo "stale"
    fi
    return 0
  fi
  if [[ -n "${pid}" ]] && dispatcher_pid_holds_lock_file "${pid}"; then
    echo "tracked"
  else
    echo "unknown"
  fi
}

lock_identity_is_held() {
  flock_owner_pid "$1" >/dev/null 2>&1
}

wait_dispatcher_lock_free() {
  local lock_identity="$1"
  for _ in 1 2 3 4 5; do
    lock_identity_is_held "${lock_identity}" || return 0
    sleep 1
  done
  return 1
}

signal_managed_dispatcher_group() {
  local pid_file="$1"
  local expected_config="$2"
  local lock_identity="$3"
  local signal_name="$4"
  "${PYTHON_BIN}" - "${pid_file}" "${expected_config}" "${lock_identity}" "${signal_name}" <<'PY'
import json
import os
from pathlib import Path
import signal
import sys

pid_file = Path(sys.argv[1])
expected_config = sys.argv[2]
expected_dev, expected_ino = (int(part) for part in sys.argv[3].split(":"))
signal_value = getattr(signal, f"SIG{sys.argv[4]}")
try:
    metadata = json.loads(pid_file.read_text(encoding="utf-8"))
    leader = int(metadata["pid"])
    expected_start = str(metadata["proc_start_time"])
except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
if metadata.get("config") != expected_config:
    raise SystemExit(1)
owner = None
for line in Path("/proc/locks").read_text(encoding="utf-8").splitlines():
    fields = line.split()
    if len(fields) < 8 or fields[1] != "FLOCK" or fields[3] != "WRITE":
        continue
    try:
        candidate = int(fields[4])
        major_hex, minor_hex, inode_text = fields[5].split(":")
        device = os.makedev(int(major_hex, 16), int(minor_hex, 16))
        inode = int(inode_text)
    except (ValueError, IndexError):
        continue
    if (device, inode) == (expected_dev, expected_ino):
        owner = candidate
        break
if owner is None or owner != leader:
    raise SystemExit(1)
try:
    if signal_value == signal.SIGTERM:
        # pidfd 保持原 PID identity，避免校验后 PID 被复用再误杀其他进程组。
        pidfd = os.pidfd_open(leader) if hasattr(os, "pidfd_open") else None
        try:
            fields = (Path("/proc") / str(leader) / "stat").read_text(encoding="utf-8").rsplit(") ", 1)[1].split()
            if fields[0] == "Z" or len(fields) <= 19 or fields[19] != expected_start:
                raise SystemExit(1)
            if os.getpgid(leader) != leader:
                raise SystemExit(1)
            os.killpg(leader, signal_value)
        finally:
            if pidfd is not None:
                os.close(pidfd)
    else:
        # leader 退出后锁仍可能由同进程组子进程继承；以锁 inode 和 PGID 双重确认。
        member_found = False
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                fields = (proc / "stat").read_text(encoding="utf-8").rsplit(") ", 1)[1].split()
                if fields[0] == "Z" or int(fields[2]) != leader:
                    continue
                for entry in (proc / "fd").iterdir():
                    try:
                        info = entry.stat()
                    except OSError:
                        continue
                    if (info.st_dev, info.st_ino) == (expected_dev, expected_ino):
                        member_found = True
                        break
            except (OSError, ValueError, IndexError):
                continue
            if member_found:
                break
        if not member_found:
            raise SystemExit(1)
        os.killpg(leader, signal_value)
except (OSError, IndexError):
    raise SystemExit(1)
PY
}

stop_managed_dispatcher() {
  local pid_file="$1"
  local expected_config="$2"
  local state
  state="$(dispatcher_ownership_state "${pid_file}" "${expected_config}")" || return 1
  if [[ "${state}" == "stale" ]]; then
    rm -f "${pid_file}"
    return 0
  fi
  if [[ "${state}" != "tracked" ]]; then
    printf '拒绝停止 Dispatcher：进程所有权状态为 %s\n' "${state}" >&2
    return 1
  fi
  local lock_identity
  lock_identity="$(lock_file_identity "${DISPATCHER_LOCK_FILE}")" || return 1
  if ! signal_managed_dispatcher_group "${pid_file}" "${expected_config}" "${lock_identity}" TERM; then
    echo "Dispatcher 身份在停止前发生变化，已拒绝发送信号" >&2
    return 1
  fi
  if ! wait_dispatcher_lock_free "${lock_identity}"; then
    signal_managed_dispatcher_group "${pid_file}" "${expected_config}" "${lock_identity}" KILL || {
      echo "Dispatcher 子进程仍持有生命周期锁，且无法安全确认进程组身份" >&2
      return 1
    }
    if ! wait_dispatcher_lock_free "${lock_identity}"; then
      echo "Dispatcher 已收到 SIGKILL，但生命周期锁未释放" >&2
      return 1
    fi
  fi
  rm -f "${pid_file}"
  echo "Dispatcher 已停止"
}
