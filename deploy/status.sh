#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(cd "$(dirname "$0")" && pwd)/common.sh"

load_runtime_env
if pid_is_running "${PID_DIR}/server.pid" && load_active_server_endpoint; then
  START_HOST_OVERRIDE="${ACTIVE_SERVER_HOST}"
  START_PORT_OVERRIDE="${ACTIVE_SERVER_PORT}"
fi
resolve_server_endpoint

server_running=0
dispatcher_running=0
http_ready=0
dispatcher_ownership="unknown"
pid_is_running "${PID_DIR}/server.pid" && server_running=1
if dispatcher_ownership="$(dispatcher_ownership_state "${PID_DIR}/dispatcher.pid" "${DISPATCH_CONFIG}")"; then
  [[ "${dispatcher_ownership}" == "tracked" ]] && dispatcher_running=1
fi
if curl -fsS "${SERVER_URL}/health" >/dev/null 2>&1; then
  http_ready=1
fi

state="degraded"
if [[ "${server_running}" == "1" && "${dispatcher_running}" == "1" && "${http_ready}" == "1" ]]; then
  state="running"
elif [[ "${server_running}" == "1" && "${dispatcher_ownership}" == "stale" && "${http_ready}" == "1" ]]; then
  state="server-only"
elif [[ "${server_running}" == "0" && "${dispatcher_ownership}" == "stale" && "${http_ready}" == "0" ]]; then
  state="stopped"
fi

printf 'Status:     %s\n' "${state}"
printf 'Server:     %s\n' "$([[ "${server_running}" == "1" ]] && echo running || echo stopped)"
printf 'Dispatcher: %s (ownership=%s)\n' \
  "$([[ "${dispatcher_running}" == "1" ]] && echo running || echo stopped)" \
  "${dispatcher_ownership}"
printf 'HTTP API:   %s (%s)\n' "$([[ "${http_ready}" == "1" ]] && echo ready || echo unreachable)" "${SERVER_URL}"

heartbeat="unavailable"
dispatcher_token=""
if [[ -f "${DISPATCHER_TOKEN_FILE}" ]]; then
  dispatcher_token="$(<"${DISPATCHER_TOKEN_FILE}")"
elif [[ -n "${CAIRN_DISPATCHER_TOKEN:-}" ]]; then
  dispatcher_token="${CAIRN_DISPATCHER_TOKEN}"
fi
if [[ "${http_ready}" == "1" && -n "${dispatcher_token}" ]]; then
  heartbeat="$(printf 'header = "Authorization: Bearer %s"\n' "${dispatcher_token}" \
    | curl -fsS --config - "${SERVER_URL}/dispatcher/monitoring/summary" 2>/dev/null \
    | "${PYTHON_BIN}" -c '
import json
import sys
try:
    dispatcher = json.load(sys.stdin).get("dispatcher") or {}
except (json.JSONDecodeError, OSError):
    print("unavailable")
    raise SystemExit(0)
status = dispatcher.get("effective_status") or dispatcher.get("last_status")
if not status:
    print("not-reported")
    raise SystemExit(0)
age = dispatcher.get("heartbeat_age_seconds")
stale = dispatcher.get("heartbeat_stale")
parts = [str(status)]
if age is not None:
    parts.append(f"age={age}s")
if stale:
    parts.append("stale=yes")
print(" ".join(parts))
' || true)"
  heartbeat="${heartbeat:-unavailable}"
fi
unset dispatcher_token
printf 'Heartbeat:  %s\n' "${heartbeat}"
