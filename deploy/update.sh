#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
TARGET_ROOT=""
START_ARGS=()

usage() {
  cat <<EOF
用法: ${SOURCE_ROOT}/deploy/update.sh --target 已安装目录 [-- start.sh参数...]

从当前已解压的部署包更新一个没有 Git 的既有安装。脚本只替换部署程序文件，保留
runtime、数据库 volume、cairn/.venv、Artifact 和项目上传资料。
这不是源码同步工具，不更新前端源码或测试。默认本地 Worker 会带缓存重新构建；
自定义镜像保留并提示自行配套更新。--server-only 明确跳过 Worker 准备。

示例:
  ${SOURCE_ROOT}/deploy/update.sh --target /opt/arena
  ${SOURCE_ROOT}/deploy/update.sh --target /opt/arena -- --server-only
EOF
}

while (( $# > 0 )); do
  case "$1" in
    --target)
      (( $# >= 2 )) || { echo "--target 缺少目录" >&2; exit 2; }
      TARGET_ROOT="$2"
      shift 2
      ;;
    --)
      shift
      START_ARGS=("$@")
      break
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
done

[[ -n "${TARGET_ROOT}" ]] || { echo "必须通过 --target 指定既有安装目录" >&2; exit 2; }
command -v python3 >/dev/null || { echo "缺少 python3" >&2; exit 1; }

TARGET_ROOT="$(python3 - "${SOURCE_ROOT}" "${TARGET_ROOT}" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).resolve(strict=True)
target = Path(sys.argv[2]).expanduser().resolve(strict=True)
if not target.is_dir():
    raise SystemExit("目标安装目录不存在")
if source == target or source in target.parents or target in source.parents:
    raise SystemExit("新版本目录和目标安装目录必须彼此独立，不能相同或互相嵌套")
if target / "runtime" == source or target / "runtime" in source.parents:
    raise SystemExit("新版本目录不能位于目标 runtime 内")
print(target)
PY
)"

required_directories=(
  "cairn/src/cairn"
  "datas/cairn/knowledge"
  "container"
)
required_files=(
  "deploy/arena.env.example"
  "deploy/common.sh"
  "deploy/dispatch.yaml.example"
  "deploy/migrate_worker_config.py"
  "deploy/postgres.compose.yaml"
  "deploy/postgres.env.example"
  "deploy/verify_deployment.py"
  "cairn/pyproject.toml"
  "cairn/uv.lock"
  "cairn/src/cairn/server/static/index.html"
)
required_executables=(
  "deploy/install.sh"
  "deploy/logs.sh"
  "deploy/start.sh"
  "deploy/status.sh"
  "deploy/stop.sh"
  "deploy/update.sh"
  "deploy/verify.sh"
)
for relative in "${required_directories[@]}"; do
  [[ -d "${SOURCE_ROOT}/${relative}" ]] || {
    echo "新版本目录缺少必要目录: ${relative}" >&2
    exit 1
  }
done
for relative in "${required_files[@]}"; do
  [[ -f "${SOURCE_ROOT}/${relative}" ]] || {
    echo "新版本目录缺少必要文件: ${relative}" >&2
    exit 1
  }
done
for relative in "${required_executables[@]}"; do
  [[ -f "${SOURCE_ROOT}/${relative}" && -x "${SOURCE_ROOT}/${relative}" ]] || {
    echo "新版本目录缺少可执行脚本: ${relative}" >&2
    exit 1
  }
done
[[ -x "${TARGET_ROOT}/deploy/stop.sh" && -f "${TARGET_ROOT}/deploy/common.sh" ]] || {
  echo "目标目录缺少 deploy/stop.sh 或 deploy/common.sh: ${TARGET_ROOT}" >&2
  exit 1
}

if [[ -f "${SOURCE_ROOT}/RELEASE-MANIFEST.json" ]]; then
  if ! python3 "${SOURCE_ROOT}/deploy/verify_deployment.py" \
      --root "${SOURCE_ROOT}" --manifest-only >/dev/null; then
    echo "警告：新版本 RELEASE-MANIFEST.json 校验未通过；将按目录结构继续更新。" >&2
  fi
else
  echo "提示：新版本未包含 RELEASE-MANIFEST.json，将按目录结构更新。" >&2
fi

stage_root=""
backup_root=""
preserve_backup=0
cleanup_temporary_dirs() {
  local candidate
  if [[ -n "${stage_root}" && "${stage_root}" == "${TARGET_ROOT}"/.arena-update-* ]]; then
    rm -rf -- "${stage_root}"
  fi
  candidate="${backup_root}"
  if [[ "${preserve_backup}" == "0" && -n "${candidate}" && "${candidate}" == "${TARGET_ROOT}"/.arena-update-* ]]; then
    rm -rf -- "${candidate}"
  fi
}
trap cleanup_temporary_dirs EXIT

stage_root="$(python3 - "${SOURCE_ROOT}" "${TARGET_ROOT}" <<'PY'
from pathlib import Path
import shutil
import sys
import tempfile

source = Path(sys.argv[1]).resolve(strict=True)
target = Path(sys.argv[2]).resolve(strict=True)
managed = [
    Path("cairn/src/cairn"),
    Path("datas/cairn/knowledge"),
    Path("deploy"),
    Path("container"),
    Path("cairn/pyproject.toml"),
    Path("cairn/uv.lock"),
    Path("docs/deployment-linux-zh.md"),
]
optional = [
    Path("README.md"),
    Path("CHANGELOG.md"),
    Path("CLAUDE.md"),
    Path("AGENT.md"),
    Path("RELEASE-MANIFEST.json"),
]

def reject_symlinks(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(f"新版本发布内容不允许 symlink: {path.relative_to(source)}")
    if path.is_dir():
        for child in path.rglob("*"):
            if child.is_symlink():
                raise RuntimeError(f"新版本发布内容不允许 symlink: {child.relative_to(source)}")


def reject_target_symlink_ancestors(relative: Path) -> None:
    current = target
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise RuntimeError(f"目标发布路径不允许 symlink: {relative}")


for relative in managed + optional:
    reject_target_symlink_ancestors(relative)

stage = Path(tempfile.mkdtemp(prefix=".arena-update-stage-", dir=target))
try:
    for relative in managed + optional:
        src = source / relative
        if not src.exists():
            if relative in managed:
                raise RuntimeError(f"新版本缺少发布内容: {relative}")
            continue
        reject_symlinks(src)
        dst = stage / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, copy_function=shutil.copy2)
        elif src.is_file():
            shutil.copy2(src, dst)
        else:
            raise RuntimeError(f"新版本发布内容不是普通文件或目录: {relative}")
except BaseException:
    shutil.rmtree(stage, ignore_errors=True)
    raise
print(stage)
PY
)"

# 使用旧版本 lifecycle 函数取得操作锁，并把同一把锁持有到最终状态检查结束。
unset ARENA_OPERATION_LOCK_INHERITED
# shellcheck source=/dev/null
source "${TARGET_ROOT}/deploy/common.sh"
ensure_runtime_dirs
acquire_operation_lock
export ARENA_OPERATION_LOCK_INHERITED=1
stop_managed_dispatcher "${PID_DIR}/dispatcher.pid" "${DISPATCH_CONFIG}"
rm -f "${PID_DIR}/dispatcher.token.sha256"
stop_pid_file "${PID_DIR}/server.pid" "Server"
rm -f "${SERVER_ENDPOINT_FILE}"

backup_root="$(python3 - "${stage_root}" "${TARGET_ROOT}" <<'PY'
from pathlib import Path
import os
import shutil
import sys
import tempfile

stage = Path(sys.argv[1]).resolve(strict=True)
target = Path(sys.argv[2]).resolve(strict=True)
managed = [
    Path("cairn/src/cairn"),
    Path("datas/cairn/knowledge"),
    Path("deploy"),
    Path("container"),
    Path("cairn/pyproject.toml"),
    Path("cairn/uv.lock"),
    Path("docs/deployment-linux-zh.md"),
    Path("README.md"),
    Path("CHANGELOG.md"),
    Path("CLAUDE.md"),
    Path("AGENT.md"),
    Path("RELEASE-MANIFEST.json"),
]
backup = Path(tempfile.mkdtemp(prefix=".arena-update-backup-", dir=target))
applied: list[Path] = []
backed_up: list[Path] = []
remove_when_absent = {Path("RELEASE-MANIFEST.json")}

try:
    for relative in managed:
        staged = stage / relative
        destination = target / relative
        backup_path = backup / relative
        if not staged.exists() and relative not in remove_when_absent:
            continue
        if destination.exists() or destination.is_symlink():
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(destination, backup_path)
            backed_up.append(relative)
        if not staged.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, destination)
        applied.append(relative)
except BaseException as apply_error:
    rollback_errors: list[str] = []
    for relative in reversed(applied):
        destination = target / relative
        try:
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(destination)
            else:
                destination.unlink(missing_ok=True)
        except OSError as exc:
            rollback_errors.append(f"删除新文件 {relative} 失败: {exc}")
    for relative in reversed(backed_up):
        backup_path = backup / relative
        destination = target / relative
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if backup_path.exists() or backup_path.is_symlink():
                os.replace(backup_path, destination)
        except OSError as exc:
            rollback_errors.append(f"恢复旧文件 {relative} 失败: {exc}")
    if rollback_errors:
        details = "\n".join(rollback_errors)
        raise RuntimeError(
            f"更新失败且旧文件未能完整恢复；保留备份目录 {backup}\n{details}"
        ) from apply_error
    shutil.rmtree(backup, ignore_errors=True)
    raise
finally:
    shutil.rmtree(stage, ignore_errors=True)

print(backup)
PY
)"
stage_root=""
preserve_backup=1

expected_state="running"
for (( argument_index=0; argument_index<${#START_ARGS[@]}; argument_index++ )); do
  case "${START_ARGS[argument_index]}" in
    --server-only) expected_state="server-only" ;;
    # 与 start.sh 一致，host / port 的值不作为独立开关解释。
    --host|--port) argument_index=$((argument_index + 1)) ;;
  esac
done
# 重试仍走完整更新入口，并保留空格等 shell 参数边界，不引导绕过 Worker 准备。
printf -v retry_command '%q ' "${SOURCE_ROOT}/deploy/update.sh" --target "${TARGET_ROOT}" -- "${START_ARGS[@]}"

if "${TARGET_ROOT}/deploy/install.sh"; then
  :
else
  install_status=$?
  cat >&2 <<EOF
更新后的依赖安装失败，服务保持停止；尚未进入 Worker 准备和数据库启动迁移。
依赖或 legacy 私有配置可能已变化。旧程序备份不包含 runtime 或数据库，不是整体回滚保障。
旧程序文件保留在：${backup_root}
修复后重新执行完整更新：
  ${retry_command}
EOF
  exit "${install_status}"
fi

if [[ "${expected_state}" == "server-only" ]]; then
  echo "按 --server-only 跳过 Worker 准备；本次不更新 Worker。"
# 必须在新 shell 加载目标的新 helper、环境和依赖，不能复用当前 shell 的旧函数。
elif bash -c '
  set -euo pipefail
  source "$1/deploy/common.sh"
  ensure_runtime_dirs
  acquire_operation_lock
  ensure_application_initialized
  load_runtime_env
  cd "${ROOT_DIR}"
  require_docker_daemon
  ensure_dispatch_worker_image refresh
' arena-update-worker "${TARGET_ROOT}"; then
  echo "Worker 镜像准备完成；自定义引用未自动更新，已有项目容器不受替换。"
else
  worker_status=$?
  cat >&2 <<EOF
Worker 准备失败，服务保持停止；未进入 start.sh 或数据库启动迁移。
旧程序文件保留在：${backup_root}
此备份不包含 runtime 或数据库，不是整体回滚保障。修复后重新执行完整更新：
  ${retry_command}
EOF
  exit "${worker_status}"
fi

if "${TARGET_ROOT}/deploy/start.sh" "${START_ARGS[@]}"; then
  :
else
  start_status=$?
  cat >&2 <<EOF
新版本启动失败。旧程序文件保留在：${backup_root}
请检查：
  ${TARGET_ROOT}/deploy/status.sh
  ${TARGET_ROOT}/deploy/logs.sh
数据库可能已执行迁移，旧程序备份不构成数据库或整体回滚保障。
修复后重新执行完整更新（不要跳过 Worker 准备或绕过迁移阻断）：
  ${retry_command}
EOF
  exit "${start_status}"
fi

if status_output="$("${TARGET_ROOT}/deploy/status.sh")"; then
  printf '%s\n' "${status_output}"
else
  status_code=$?
  printf '%s\n' "${status_output}" >&2
  printf '状态检查失败，旧程序文件保留在：%s\n' "${backup_root}" >&2
  exit "${status_code}"
fi

actual_state=""
while IFS= read -r status_line; do
  if [[ "${status_line}" == Status:* ]]; then
    actual_state="${status_line#Status:}"
    actual_state="${actual_state#"${actual_state%%[![:space:]]*}"}"
    break
  fi
done <<<"${status_output}"
if [[ "${actual_state}" != "${expected_state}" ]]; then
  printf '更新后状态异常：期望 %s，实际 %s；旧程序文件保留在：%s\n' \
    "${expected_state}" "${actual_state:-unknown}" "${backup_root}" >&2
  exit 1
fi

rm -rf -- "${backup_root}"
backup_root=""
preserve_backup=0
echo "Arena 应用更新与启动检查完成，原 runtime、数据库、Artifact、项目资料和 .venv 位置已保留，依赖已同步。"
echo "Worker 准备范围见上方输出；未替换已有项目容器，也未验证真实任务。"
