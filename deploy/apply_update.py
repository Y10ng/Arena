#!/usr/bin/env python3
"""Arena 一键更新的独立执行器（stdlib-only）。

由 Server 在 apply 阶段以 detached 会话启动，与被替换的 Server 进程解耦：
Server 退出不影响本进程，本进程按状态文件中的交接信息调用新版包内的
deploy/update.sh 完成停服、替换、重启，并把结果写回状态文件。

不导入任何第三方依赖：运行环境只有系统 python3，此时 cairn/.venv 可能
正在被 install.sh 重建。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# 更新器被杀后状态文件停留 applying；Server 端存活检查据此恢复，无需本文件处理。
PHASE_APPLYING = "applying"
PHASE_DONE = "done"
PHASE_FAILED = "failed"

# 等待 Server 完成 apply 响应并退出；过早启动 update.sh 会与 Server 的
# 停止流程竞争 pid 文件和操作锁。
START_DELAY_SECONDS = 2.0


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _update_phase(state_path: Path, **fields) -> None:
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    state.update(fields)
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _atomic_write_json(state_path, state)


def _start_args(handover: dict) -> list[str]:
    args: list[str] = []
    host = handover.get("host")
    port = handover.get("port")
    if host:
        args += ["--host", str(host)]
    if port:
        args += ["--port", str(port)]
    return args


def main() -> int:
    parser = argparse.ArgumentParser(description="Arena 一键更新执行器")
    parser.add_argument("--state", type=Path, required=True, help="更新状态文件路径")
    args = parser.parse_args()

    state_path = args.state.resolve()
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"无法读取更新状态文件 {state_path}: {exc}", file=sys.stderr)
        return 2

    handover = state.get("handover") or {}
    staged = handover.get("staged_dir")
    target = handover.get("target_root")
    log_path = handover.get("log_path")
    if not staged or not target or not log_path:
        print("交接信息不完整：需要 staged_dir、target_root、log_path", file=sys.stderr)
        return 2
    staged_dir = Path(staged)
    target_root = Path(target)
    log_file = Path(log_path)
    if not (staged_dir / "deploy/update.sh").is_file():
        print(f"staged 包缺少 deploy/update.sh: {staged_dir}", file=sys.stderr)
        return 2

    time.sleep(START_DELAY_SECONDS)
    _update_phase(state_path, phase=PHASE_APPLYING, apply_started_at=time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    # 只执行 staged 包内的 update.sh：它会在运行中途替换 target/deploy，
    # 执行 target 副本存在脚本被替换后继续读取损坏内容的风险。
    command = [
        "bash", str(staged_dir / "deploy/update.sh"),
        "--target", str(target_root),
        "--", *_start_args(handover),
    ]
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("ab") as log:
        log.write(f"\n===== apply {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} =====\n".encode())
        log.flush()
        # 清除 CAIRN_* 环境变量：update.sh/start.sh 从 runtime/arena.env 重新推导，
        # 避免旧 Server 进程环境（数据库 URL、静态目录等）泄漏进更新流程。
        env = {key: value for key, value in os.environ.items() if not key.startswith("CAIRN_")}
        result = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT)

    exit_code = int(result.returncode)
    _update_phase(
        state_path,
        phase=PHASE_DONE if exit_code == 0 else PHASE_FAILED,
        apply_exit_code=exit_code,
        apply_finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        apply_log_path=str(log_file),
    )
    return 0 if exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
