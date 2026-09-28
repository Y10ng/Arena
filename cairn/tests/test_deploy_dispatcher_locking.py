from __future__ import annotations

import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / "deploy/common.sh"
PYTHON_BIN = Path(sys.executable)


@pytest.fixture(autouse=True)
def isolated_common_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """仅在假仓库测试锁与自建 sleep 进程，不接触实际安装目录。"""
    common = tmp_path / "fake-repo/deploy/common.sh"
    common.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "deploy/common.sh", common)
    monkeypatch.setattr(sys.modules[__name__], "COMMON", common)


def _shell(script: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script, "test", *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=15,
    )


def _wait_for_pid_file(path: Path) -> int:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.exists():
            return int(path.read_text(encoding="utf-8"))
        time.sleep(0.02)
    raise AssertionError(f"PID file was not created: {path}")


def _proc_start_time(pid: int) -> str:
    fields = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8").rsplit(") ", 1)[1].split()
    return fields[19]


def _kill_group(pid: int, expected_start_time: str) -> None:
    try:
        if _proc_start_time(pid) != expected_start_time or os.getpgid(pid) != pid:
            return
        os.killpg(pid, signal.SIGKILL)
    except (FileNotFoundError, ProcessLookupError):
        pass


def test_lifetime_lock_survives_launcher_and_missing_metadata_blocks_relaunch(
    tmp_path: Path,
) -> None:
    child_pid_file = tmp_path / "child.pid"
    metadata_file = tmp_path / "dispatcher.pid"
    config = str(tmp_path / "dispatch.yaml")
    launcher = _shell(
        r'''
source "$1"
PYTHON_BIN="$2"
PID_DIR="$3"
DISPATCHER_LOCK_FILE="$PID_DIR/dispatcher.lock"
OPERATION_LOCK_FILE="$PID_DIR/start.lock"
prepare_lock_file "$DISPATCHER_LOCK_FILE"
setsid flock --no-fork -n "$DISPATCHER_LOCK_FILE" sh -c 'sleep 30' 9>&- >/dev/null 2>&1 </dev/null &
child=$!
write_pid_metadata "$4" "$child" "$5" true
printf '%s' "$child" > "$6"
''',
        str(COMMON),
        str(PYTHON_BIN),
        str(tmp_path),
        str(metadata_file),
        config,
        str(child_pid_file),
    )
    assert launcher.returncode == 0, launcher.stderr
    child = _wait_for_pid_file(child_pid_file)
    child_start_time = _proc_start_time(child)

    try:
        tracked = _shell(
            r'''
source "$1"
PYTHON_BIN="$2"
PID_DIR="$3"
DISPATCHER_LOCK_FILE="$PID_DIR/dispatcher.lock"
dispatcher_ownership_state "$4" "$5"
''',
            str(COMMON),
            str(PYTHON_BIN),
            str(tmp_path),
            str(metadata_file),
            config,
        )
        assert tracked.stdout.strip() == "tracked"

        metadata_file.unlink()
        unknown = _shell(
            r'''
source "$1"
PYTHON_BIN="$2"
PID_DIR="$3"
DISPATCHER_LOCK_FILE="$PID_DIR/dispatcher.lock"
state="$(dispatcher_ownership_state "$4" "$5")"
printf '%s\n' "$state"
if dispatcher_lock_is_free; then
  exit 9
fi
''',
            str(COMMON),
            str(PYTHON_BIN),
            str(tmp_path),
            str(metadata_file),
            config,
        )
        assert unknown.returncode == 0, unknown.stderr
        assert unknown.stdout.strip() == "unknown"
        os.kill(child, 0)
    finally:
        _kill_group(child, child_start_time)


def test_untracked_dispatcher_is_not_stopped(tmp_path: Path) -> None:
    metadata_file = tmp_path / "dispatcher.pid"
    config = str(tmp_path / "dispatch.yaml")
    process = subprocess.Popen(
        ["setsid", "sh", "-c", "sleep 30"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=False,
    )
    process_start_time = _proc_start_time(process.pid)
    try:
        write = _shell(
            r'''
source "$1"
PYTHON_BIN="$2"
write_pid_metadata "$3" "$4" "$5" true
''',
            str(COMMON),
            str(PYTHON_BIN),
            str(metadata_file),
            str(process.pid),
            config,
        )
        assert write.returncode == 0, write.stderr

        result = _shell(
            r'''
source "$1"
PYTHON_BIN="$2"
PID_DIR="$3"
DISPATCHER_LOCK_FILE="$PID_DIR/dispatcher.lock"
state="$(dispatcher_ownership_state "$4" "$5")"
printf '%s\n' "$state"
stop_managed_dispatcher "$4" "$5"
''',
            str(COMMON),
            str(PYTHON_BIN),
            str(tmp_path),
            str(metadata_file),
            config,
        )
        assert result.returncode != 0
        assert result.stdout.strip() == "untracked"
        assert "拒绝停止 Dispatcher" in result.stderr
        os.kill(process.pid, 0)
        assert metadata_file.exists()
    finally:
        _kill_group(process.pid, process_start_time)
        process.wait(timeout=5)


def test_opening_locked_inode_without_owning_flock_is_not_tracked(tmp_path: Path) -> None:
    lock_file = tmp_path / "dispatcher.lock"
    lock_file.touch(mode=0o600)
    owner = subprocess.Popen(
        ["setsid", "flock", "--no-fork", "-n", str(lock_file), "sleep", "30"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    owner_start = _proc_start_time(owner.pid)
    bystander = subprocess.Popen(
        ["setsid", "sh", "-c", 'exec 8>>"$1"; sleep 30', "test", str(lock_file)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    bystander_start = _proc_start_time(bystander.pid)
    metadata_file = tmp_path / "dispatcher.pid"
    config = str(tmp_path / "dispatch.yaml")
    try:
        write = _shell(
            r'''
source "$1"
PYTHON_BIN="$2"
write_pid_metadata "$3" "$4" "$5" true
''',
            str(COMMON),
            str(PYTHON_BIN),
            str(metadata_file),
            str(bystander.pid),
            config,
        )
        assert write.returncode == 0, write.stderr
        result = _shell(
            r'''
source "$1"
PYTHON_BIN="$2"
PID_DIR="$3"
DISPATCHER_LOCK_FILE="$PID_DIR/dispatcher.lock"
dispatcher_ownership_state "$4" "$5"
''',
            str(COMMON),
            str(PYTHON_BIN),
            str(tmp_path),
            str(metadata_file),
            config,
        )
        assert result.stdout.strip() == "unknown"
    finally:
        _kill_group(owner.pid, owner_start)
        _kill_group(bystander.pid, bystander_start)
        owner.wait(timeout=5)
        bystander.wait(timeout=5)


def test_start_and_stop_operation_lock_is_serialized(tmp_path: Path) -> None:
    ready_file = tmp_path / "ready"
    holder = subprocess.Popen(
        [
            "bash",
            "-c",
            r'''
source "$1"
PYTHON_BIN="$2"
PID_DIR="$3"
OPERATION_LOCK_FILE="$PID_DIR/start.lock"
acquire_operation_lock
printf '%s' "$$" > "$4"
sleep 30
''',
            "test",
            str(COMMON),
            str(PYTHON_BIN),
            str(tmp_path),
            str(ready_file),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_for_pid_file(ready_file)
    try:
        result = _shell(
            r'''
source "$1"
PYTHON_BIN="$2"
PID_DIR="$3"
OPERATION_LOCK_FILE="$PID_DIR/start.lock"
acquire_operation_lock
''',
            str(COMMON),
            str(PYTHON_BIN),
            str(tmp_path),
        )
        assert result.returncode != 0
        assert "启动或停止过程正在执行" in result.stderr
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_lock_preparation_rejects_hardlink_without_truncation(tmp_path: Path) -> None:
    target = tmp_path / "important"
    target.write_text("must-stay", encoding="utf-8")
    lock_file = tmp_path / "dispatcher.lock"
    os.link(target, lock_file)

    result = _shell(
        r'''
source "$1"
PYTHON_BIN="$2"
prepare_lock_file "$3"
''',
        str(COMMON),
        str(PYTHON_BIN),
        str(lock_file),
    )

    assert result.returncode != 0
    assert target.read_text(encoding="utf-8") == "must-stay"


def test_stop_force_kills_lock_holding_child_after_leader_exits(tmp_path: Path) -> None:
    leader_pid_file = tmp_path / "leader.pid"
    child_pid_file = tmp_path / "survivor.pid"
    metadata_file = tmp_path / "dispatcher.pid"
    config = str(tmp_path / "dispatch.yaml")
    launcher = _shell(
        r'''
source "$1"
PYTHON_BIN="$2"
PID_DIR="$3"
DISPATCHER_LOCK_FILE="$PID_DIR/dispatcher.lock"
prepare_lock_file "$DISPATCHER_LOCK_FILE"
SURVIVOR_PID_FILE="$6" setsid flock --no-fork -n "$DISPATCHER_LOCK_FILE" sh -c '
  trap "exit 0" TERM
  sh -c '\''trap "" TERM; sleep 30'\'' &
  printf "%s" "$!" > "$SURVIVOR_PID_FILE"
  wait
' 9>&- >/dev/null 2>&1 </dev/null &
leader=$!
write_pid_metadata "$4" "$leader" "$5" true
printf '%s' "$leader" > "$7"
''',
        str(COMMON),
        str(PYTHON_BIN),
        str(tmp_path),
        str(metadata_file),
        config,
        str(child_pid_file),
        str(leader_pid_file),
    )
    assert launcher.returncode == 0, launcher.stderr
    leader = _wait_for_pid_file(leader_pid_file)
    leader_start = _proc_start_time(leader)
    survivor = _wait_for_pid_file(child_pid_file)
    survivor_start = _proc_start_time(survivor)

    try:
        result = _shell(
            r'''
source "$1"
PYTHON_BIN="$2"
PID_DIR="$3"
DISPATCHER_LOCK_FILE="$PID_DIR/dispatcher.lock"
stop_managed_dispatcher "$4" "$5"
''',
            str(COMMON),
            str(PYTHON_BIN),
            str(tmp_path),
            str(metadata_file),
            config,
        )
        assert result.returncode == 0, result.stderr
        assert "Dispatcher 已停止" in result.stdout
        with pytest.raises(ProcessLookupError):
            os.kill(survivor, 0)
    finally:
        _kill_group(leader, leader_start)
        try:
            if _proc_start_time(survivor) == survivor_start:
                os.kill(survivor, signal.SIGKILL)
        except (FileNotFoundError, ProcessLookupError):
            pass


def test_managed_dispatcher_stop_releases_lifetime_lock(tmp_path: Path) -> None:
    child_pid_file = tmp_path / "child.pid"
    metadata_file = tmp_path / "dispatcher.pid"
    config = str(tmp_path / "dispatch.yaml")
    launcher = _shell(
        r'''
source "$1"
PYTHON_BIN="$2"
PID_DIR="$3"
DISPATCHER_LOCK_FILE="$PID_DIR/dispatcher.lock"
prepare_lock_file "$DISPATCHER_LOCK_FILE"
setsid flock --no-fork -n "$DISPATCHER_LOCK_FILE" sh -c 'sleep 30' 9>&- >/dev/null 2>&1 </dev/null &
child=$!
write_pid_metadata "$4" "$child" "$5" true
printf '%s' "$child" > "$6"
''',
        str(COMMON),
        str(PYTHON_BIN),
        str(tmp_path),
        str(metadata_file),
        config,
        str(child_pid_file),
    )
    assert launcher.returncode == 0, launcher.stderr
    child = _wait_for_pid_file(child_pid_file)
    child_start_time = _proc_start_time(child)

    try:
        result = _shell(
            r'''
source "$1"
PYTHON_BIN="$2"
PID_DIR="$3"
DISPATCHER_LOCK_FILE="$PID_DIR/dispatcher.lock"
stop_managed_dispatcher "$4" "$5"
dispatcher_ownership_state "$4" "$5"
''',
            str(COMMON),
            str(PYTHON_BIN),
            str(tmp_path),
            str(metadata_file),
            config,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines()[-1] == "stale"
        assert not metadata_file.exists()
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)
    finally:
        _kill_group(child, child_start_time)
