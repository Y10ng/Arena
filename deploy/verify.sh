#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(cd "$(dirname "$0")" && pwd)/common.sh"

[[ -x "${PYTHON_BIN}" ]] || { echo "尚未安装，请先运行 deploy/install.sh" >&2; exit 1; }
load_runtime_env
exec "${PYTHON_BIN}" "${DEPLOY_DIR}/verify_deployment.py" --root "${ROOT_DIR}" "$@"
