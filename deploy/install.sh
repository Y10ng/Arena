#!/usr/bin/env bash
set -euo pipefail

# 安装创建本机运行环境，并可迁移私有配置；日志绝不回显模型密钥。
# shellcheck source=common.sh
source "$(cd "$(dirname "$0")" && pwd)/common.sh"
cd "${ROOT_DIR}"

[[ "$(uname -s)" == "Linux" ]] || { echo "仅支持 Linux 部署" >&2; exit 1; }
command -v python3 >/dev/null || { echo "缺少 python3" >&2; exit 1; }
command -v uv >/dev/null || { echo "缺少 uv: https://docs.astral.sh/uv/" >&2; exit 1; }

python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit(f"需要 Python >= 3.11，当前为 {sys.version.split()[0]}")
PY

uv sync --project "${ROOT_DIR}/cairn" --frozen --no-dev

ensure_runtime_dirs
mkdir -p \
  "${ROOT_DIR}/runtime/data/knowledge" \
  "${ROOT_DIR}/runtime/data/prompts"
if [[ -d "${ROOT_DIR}/datas/cairn/knowledge" ]] && [[ -z "$(find "${ROOT_DIR}/runtime/data/knowledge" -mindepth 1 -print -quit)" ]]; then
  cp -a "${ROOT_DIR}/datas/cairn/knowledge/." "${ROOT_DIR}/runtime/data/knowledge/"
fi
if [[ -L "${DISPATCH_CONFIG}" ]]; then
  echo "runtime/dispatch.yaml 不能是 symlink" >&2
  exit 1
fi
if [[ ! -f "${DISPATCH_CONFIG}" ]]; then
  cp "${DEPLOY_DIR}/dispatch.yaml.example" "${DISPATCH_CONFIG}"
fi
if [[ ! -f "${DISPATCH_CONFIG}" || ! -O "${DISPATCH_CONFIG}" ]]; then
  echo "runtime/dispatch.yaml 必须是当前用户拥有的普通文件" >&2
  exit 1
fi
ensure_runtime_env
chmod 600 "${DISPATCH_CONFIG}"

# 独立 executor 配置与 legacy secret:// 一次转换；失败不继续启动或更新。
"${PYTHON_BIN}" "${DEPLOY_DIR}/migrate_worker_config.py" --root "${ROOT_DIR}"
echo "Worker 配置迁移检查完成。"

cat <<EOF
环境准备完成。

前端静态资源已准备好后，运行：
  ${ROOT_DIR}/deploy/start.sh

start.sh 会先准备并离线检查研究镜像（Worker 与 executor 共用）和独立采集镜像，
再启动默认 DB-only PostgreSQL、同步本地数据库密码，初始化并启动 Server、UI 和 Dispatcher。
直接运行 install.sh 只用于安装锁定依赖和迁移配置，不启动服务。
源码安装或更新需先在 cairn/frontend 执行 npm ci 和 npm run build:server；部署 ZIP 已带静态资源。
install.sh 不构建前端或镜像；update.sh 统一用缓存构建默认本地镜像，离线检查通过才提升 tag。
自定义镜像不自动构建或覆盖，需自行提供包含执行服务的配套研究镜像及采集镜像。

首次启动会自动创建 Model keyring、Dispatcher machine credential 和超级管理员。
交互终端立即显示一次随机管理员密码；非交互启动写入 owner-only secret 文件。配置完成后无需再次执行启动命令。

修改监听地址或端口：
  ${ROOT_DIR}/deploy/start.sh --host 0.0.0.0 --port 8080

查看状态：
  ${ROOT_DIR}/deploy/status.sh
EOF
