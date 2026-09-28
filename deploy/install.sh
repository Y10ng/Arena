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

# 迁移只处理 legacy secret:// 引用；失败时保留原配置，安装继续完成，Server 仍可单独启动。
if "${ROOT_DIR}/cairn/.venv/bin/python" "${DEPLOY_DIR}/migrate_worker_config.py" --root "${ROOT_DIR}"; then
  echo "Worker 配置迁移检查完成。"
else
  echo "警告：Worker 配置迁移未完成；原配置和旧 secret 文件均已保留。Server 可继续启动，请修复 runtime/secrets 后重跑迁移。" >&2
fi

cat <<EOF
环境准备完成。

前端静态资源已准备好后，运行：
  ${ROOT_DIR}/deploy/start.sh

start.sh 会自动启动默认 DB-only PostgreSQL、同步本地数据库密码、构建缺失的默认 Worker image，
然后初始化并启动 Server、UI 和 Dispatcher。直接运行 install.sh 只用于升级或刷新依赖，不启动服务。
源码安装或更新需先在 cairn/frontend 执行 npm ci 和 npm run build:server；部署 ZIP 已带静态资源。
install.sh 不构建前端或镜像；已有 Worker 镜像更新需显式执行 docker build -t arena-worker:local container。

首次启动会自动创建 Model keyring、Dispatcher machine credential 和超级管理员。
交互终端立即显示一次随机管理员密码；非交互启动写入 owner-only secret 文件。配置完成后无需再次执行启动命令。

修改监听地址或端口：
  ${ROOT_DIR}/deploy/start.sh --host 0.0.0.0 --port 8080

查看状态：
  ${ROOT_DIR}/deploy/status.sh
EOF
