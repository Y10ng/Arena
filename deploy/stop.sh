#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(cd "$(dirname "$0")" && pwd)/common.sh"

ensure_runtime_dirs
acquire_operation_lock

# Dispatcher 所有权不明确时拒绝继续，避免 Server 停止后遗留失管的调度进程。
stop_managed_dispatcher "${PID_DIR}/dispatcher.pid" "${DISPATCH_CONFIG}"
rm -f "${PID_DIR}/dispatcher.token.sha256"
stop_pid_file "${PID_DIR}/server.pid" "Server"
rm -f "${SERVER_ENDPOINT_FILE}"
