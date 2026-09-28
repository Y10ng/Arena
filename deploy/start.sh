#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(cd "$(dirname "$0")" && pwd)/common.sh"

start_dispatcher=1
START_HOST_OVERRIDE=""
START_PORT_OVERRIDE=""
usage() {
  cat <<EOF
用法: ${DEPLOY_DIR}/start.sh [--server-only] [--host HOST] [--port PORT]

默认自动初始化运行环境，并启动 Server、UI 和 Dispatcher。

  --host HOST    本次监听地址，优先于 ARENA_SERVER_HOST
  --port PORT    本次监听端口，优先于 ARENA_SERVER_PORT
  --server-only  本次仅启动 Server，不拉起 Dispatcher
EOF
}

while (( $# > 0 )); do
  case "$1" in
    --server-only)
      start_dispatcher=0
      ;;
    --host)
      (( $# >= 2 )) || { echo "--host 缺少值" >&2; exit 2; }
      START_HOST_OVERRIDE="$2"
      shift
      ;;
    --port)
      (( $# >= 2 )) || { echo "--port 缺少值" >&2; exit 2; }
      START_PORT_OVERRIDE="$2"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf '未知参数: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

ensure_runtime_dirs
acquire_operation_lock

ensure_application_initialized
unset CAIRN_DATABASE_URL
load_runtime_env
resolve_server_endpoint
configure_trusted_origins
cd "${ROOT_DIR}"

if [[ -n "${CAIRN_DATABASE_URL:-}" ]]; then
  database_mode="external"
  validate_external_database_url
  if [[ "${start_dispatcher}" == "1" ]]; then
    require_docker_daemon
    ensure_dispatch_worker_image
  fi
else
  database_mode="managed-local"
  require_docker_daemon
  require_docker_compose
  ensure_managed_postgres_env
  validate_managed_postgres_env
  start_managed_postgres
  if [[ "${start_dispatcher}" == "1" ]]; then
    ensure_dispatch_worker_image
  fi
  synchronize_managed_postgres_password
  derive_managed_database_url
fi

if ! "${CAIRN_BIN}" db status >/dev/null 2>&1; then
  if [[ "${database_mode}" == "external" ]]; then
    echo "External PostgreSQL 连接检查失败" >&2
  else
    echo "本地 PostgreSQL 连接检查失败" >&2
  fi
  exit 1
fi
ensure_local_secrets

SERVER_PID_FILE="${PID_DIR}/server.pid"
DISPATCHER_PID_FILE="${PID_DIR}/dispatcher.pid"
DISPATCHER_TOKEN_FINGERPRINT_FILE="${PID_DIR}/dispatcher.token.sha256"

# 数据库 URL 不写入 argv、日志或 PID metadata，仅保存不可逆 fingerprint 作为进程身份。
database_fingerprint="$({
  CAIRN_DATABASE_URL_VALUE="${CAIRN_DATABASE_URL}" "${PYTHON_BIN}" - <<'PY'
import hashlib
import os
print(hashlib.sha256(os.environ["CAIRN_DATABASE_URL_VALUE"].encode()).hexdigest())
PY
} 2>/dev/null)"

# 在数据库写入前持久化 owner-only 初始密码，避免提交成功后因交付失败而丢失凭据。
stage_initial_admin_password
dispatcher_token="$(<"${DISPATCHER_TOKEN_FILE}")"
bootstrap_result="$({
  ARENA_BOOTSTRAP_DISPATCHER_TOKEN="${dispatcher_token}" \
  "${CAIRN_BIN}" auth bootstrap-runtime \
    --admin-login "${ARENA_ADMIN_LOGIN:-admin}" \
    --admin-password-file "${INITIAL_ADMIN_PASSWORD_FILE}" \
    --dispatcher-name "${ARENA_DISPATCHER_NAME:-dispatcher-main}" \
    --dispatcher-token-env ARENA_BOOTSTRAP_DISPATCHER_TOKEN
} 2>&1)" || {
  echo "自动初始化失败: ${bootstrap_result}" >&2
  exit 1
}
admin_created="$("${PYTHON_BIN}" -c 'import json,sys; print("1" if json.load(sys.stdin)["admin_created"] else "0")' <<<"${bootstrap_result}")"
admin_login="$("${PYTHON_BIN}" -c 'import json,sys; print(json.load(sys.stdin)["admin_login"])' <<<"${bootstrap_result}")"
if [[ "${admin_created}" == "1" ]]; then
  publish_initial_admin_password
elif [[ "${INITIAL_ADMIN_PASSWORD_STAGED}" == "1" ]]; then
  rm -f "${INITIAL_ADMIN_PASSWORD_FILE}"
fi
unset INITIAL_ADMIN_PASSWORD_STAGED

export CAIRN_KNOWLEDGE_ROOT="${DATA_DIR}/knowledge"
export CAIRN_PROMPTS_ROOT="${DATA_DIR}/prompts"
export CAIRN_ARTIFACT_ROOT="${CAIRN_ARTIFACT_ROOT:-${DATA_DIR}/artifacts}"
export CAIRN_DISPATCH_CONFIG="${DISPATCH_CONFIG}"
export CAIRN_DISPATCH_YAML_ALLOWED_ROOTS="${RUNTIME_DIR}"
export CAIRN_KNOWLEDGE_DOCKER_SOURCE="${DATA_DIR}/knowledge"

requested_endpoint="${SERVER_HOST}:${SERVER_PORT}"
if pid_is_running "${SERVER_PID_FILE}"; then
  active_endpoint=""
  if load_active_server_endpoint; then
    active_endpoint="${ACTIVE_SERVER_HOST}:${ACTIVE_SERVER_PORT}"
  fi
  if [[ "${active_endpoint}" != "${requested_endpoint}" ]]; then
    echo "Server endpoint 已变化，重启 Server"
    stop_pid_file "${SERVER_PID_FILE}" "Server"
  elif ! pid_is_running "${SERVER_PID_FILE}" "${database_fingerprint}"; then
    echo "Server 数据库身份已变化，重启 Server"
    stop_pid_file "${SERVER_PID_FILE}" "Server"
  fi
fi

server_started=0
if pid_is_running "${SERVER_PID_FILE}" "${database_fingerprint}"; then
  echo "复用已运行的 Arena Server"
else
  rm -f "${SERVER_PID_FILE}" "${SERVER_ENDPOINT_FILE}"
  setsid env -u CAIRN_DISPATCHER_TOKEN "${CAIRN_BIN}" serve \
    --host "${SERVER_HOST}" \
    --port "${SERVER_PORT}" \
    --no-access-log \
    9>&- >"${LOG_DIR}/server.log" 2>&1 < /dev/null &
  server_pid=$!
  if ! write_pid_metadata "${SERVER_PID_FILE}" "${server_pid}" "${database_fingerprint}"; then
    kill -TERM "${server_pid}" 2>/dev/null || true
    rm -f "${SERVER_PID_FILE}"
    echo "Server 启动后无法记录进程身份，日志: ${LOG_DIR}/server.log" >&2
    exit 1
  fi
  write_server_endpoint
  server_started=1
fi

ready=0
for _ in $(seq 1 30); do
  if curl -fsS "${SERVER_URL}/health" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "${ready}" != "1" ]]; then
  echo "Server 未 ready，日志: ${LOG_DIR}/server.log" >&2
  if [[ "${server_started}" == "1" ]]; then
    stop_pid_file "${SERVER_PID_FILE}" "Server" || true
    rm -f "${SERVER_ENDPOINT_FILE}"
  fi
  exit 1
fi

dispatcher_token_fingerprint() {
  DISPATCHER_TOKEN_VALUE="$1" "${PYTHON_BIN}" - <<'PY'
import hashlib
import os
print(hashlib.sha256(os.environ["DISPATCHER_TOKEN_VALUE"].encode()).hexdigest())
PY
}

dispatcher_state="server-only"
dispatcher_token_changed=0
current_dispatcher_token_fingerprint=""
if [[ "${start_dispatcher}" == "1" ]]; then
  current_dispatcher_token_fingerprint="$(dispatcher_token_fingerprint "${dispatcher_token}")"
  stored_dispatcher_token_fingerprint=""
  if [[ -f "${DISPATCHER_TOKEN_FINGERPRINT_FILE}" ]]; then
    stored_dispatcher_token_fingerprint="$(<"${DISPATCHER_TOKEN_FINGERPRINT_FILE}")"
  fi
  if [[ "${stored_dispatcher_token_fingerprint}" != "${current_dispatcher_token_fingerprint}" ]]; then
    dispatcher_token_changed=1
  fi
fi

dispatcher_ownership="$(dispatcher_ownership_state "${DISPATCHER_PID_FILE}" "${DISPATCH_CONFIG}")"
if [[ "${dispatcher_ownership}" == "tracked" ]]; then
  if [[ "${start_dispatcher}" == "1" && "${dispatcher_token_changed}" == "1" ]]; then
    echo "Dispatcher credential 已变化，重启 Dispatcher"
    if ! stop_managed_dispatcher "${DISPATCHER_PID_FILE}" "${DISPATCH_CONFIG}"; then
      dispatcher_state="degraded"
    fi
  else
    echo "复用已运行的 Dispatcher"
    dispatcher_state="running"
  fi
elif [[ "${dispatcher_ownership}" == "stale" ]]; then
  rm -f "${DISPATCHER_PID_FILE}" "${DISPATCHER_TOKEN_FINGERPRINT_FILE}"
else
  printf 'Dispatcher 所有权状态为 %s；为避免重复调度，不会覆盖 PID metadata 或启动第二个进程\n' \
    "${dispatcher_ownership}" >&2
  dispatcher_state="degraded"
fi

if [[ "${dispatcher_state}" != "running" && "${dispatcher_state}" != "degraded" && "${start_dispatcher}" == "1" ]]; then
  rm -f "${DISPATCHER_PID_FILE}" "${DISPATCHER_TOKEN_FINGERPRINT_FILE}"
  if ! prepare_lock_file "${DISPATCHER_LOCK_FILE}"; then
    echo "无法准备 Dispatcher 生命周期所有权锁" >&2
    dispatcher_state="degraded"
  else
    # flock --no-fork 与 Dispatcher 使用同一 PID，使 /proc/locks 可验证精确锁所有者。
    setsid flock --no-fork -n "${DISPATCHER_LOCK_FILE}" env \
      -u CAIRN_DATABASE_URL \
      -u CAIRN_MODEL_CREDENTIAL_KEYRING_FILE \
      CAIRN_SERVER_URL="${SERVER_URL}" \
      CAIRN_DISPATCHER_TOKEN="${dispatcher_token}" \
      "${CAIRN_BIN}" dispatch --config runtime/dispatch.yaml \
      9>&- >"${LOG_DIR}/dispatcher.log" 2>&1 < /dev/null &
    dispatcher_pid=$!
    if ! write_pid_metadata "${DISPATCHER_PID_FILE}" "${dispatcher_pid}" "${DISPATCH_CONFIG}" true; then
      kill -TERM -- "-${dispatcher_pid}" 2>/dev/null || true
      rm -f "${DISPATCHER_PID_FILE}"
      dispatcher_state="degraded"
    else
      sleep 2
      if pid_is_running "${DISPATCHER_PID_FILE}" "${DISPATCH_CONFIG}" true \
        && dispatcher_pid_holds_lock_file "${dispatcher_pid}"; then
        printf '%s\n' "${current_dispatcher_token_fingerprint}" >"${DISPATCHER_TOKEN_FINGERPRINT_FILE}"
        chmod 0600 "${DISPATCHER_TOKEN_FINGERPRINT_FILE}"
        dispatcher_state="running"
      else
        kill -TERM -- "-${dispatcher_pid}" 2>/dev/null || true
        rm -f "${DISPATCHER_PID_FILE}" "${DISPATCHER_TOKEN_FINGERPRINT_FILE}"
        dispatcher_state="degraded"
      fi
    fi
  fi
fi
unset dispatcher_token

if [[ "${dispatcher_state}" == "degraded" ]]; then
  cat >&2 <<EOF
Arena 以 degraded 状态运行：Server 已 ready，但 Dispatcher 启动后立即退出。
  Dispatcher 日志: ${LOG_DIR}/dispatcher.log
  修复配置后重新执行 ${DEPLOY_DIR}/start.sh。
EOF
elif [[ "${dispatcher_state}" == "running" ]]; then
  echo "Arena 状态: running"
else
  echo "Arena Server 已 ready，当前按 --server-only 模式运行。"
fi

cat <<EOF
  UI: ${SERVER_URL}
  管理员账号: ${admin_login}
EOF
if [[ "${admin_created}" == "1" ]]; then
  echo "  管理员密码: 已在数据库 bootstrap 完成后立即交付"
else
  echo "  管理员密码: 沿用现有密码（启动脚本不会重置）"
fi
cat <<EOF
  Dispatcher 配置: ${DISPATCH_CONFIG}
  状态: ${DEPLOY_DIR}/status.sh
  日志: ${DEPLOY_DIR}/logs.sh
  停止: ${DEPLOY_DIR}/stop.sh
EOF
