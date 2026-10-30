"""在完整 dispatch.yaml 上定向修改运行参数，加载状态只读取心跳。"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
from pydantic import ValidationError

from cairn.dispatcher.config import RuntimeConfig, TasksConfig
from cairn.server.schemas.dispatcher_runtime_settings import (
    DispatcherConfigLoadStatus,
    DispatcherRuntimeSettingsDocument,
    DispatcherRuntimeSettingsUpdate,
    DispatcherRuntimeValues,
    DispatcherTaskValues,
)
from .dispatcher_config_store import DispatcherConfigStore
from .infra import (
    DISPATCHER_HEARTBEAT_STALE_SECONDS,
    _latest_dispatcher_row,
    _loads_json_object,
    dispatcher_config_store,
)
from .time_utils import timestamp_delta_seconds, utcnow


def _load_status(conn, store: DispatcherConfigStore, revision: str) -> DispatcherConfigLoadStatus:
    row = _latest_dispatcher_row(conn)
    unknown = DispatcherConfigLoadStatus(state="unknown", message="尚无有效配置加载上报")
    if row is None:
        return unknown
    reported_path = row["server_reachable_path"] or row["config_path"]
    if not reported_path or Path(reported_path).expanduser().resolve() != store.path:
        return unknown
    unknown.updated_at = row["updated_at"]
    age = timestamp_delta_seconds(row["updated_at"], utcnow())
    if age is None or age < 0 or age > DISPATCHER_HEARTBEAT_STALE_SECONDS:
        unknown.message = "Dispatcher 上报已过期，加载状态未确认"
        return unknown
    if row["status"] not in {"starting", "running", "idle"}:
        return unknown
    runtime = _loads_json_object(row["runtime_json"])
    hard_cap = runtime.get("hard_cap")
    if type(hard_cap) is not int or hard_cap <= 0:
        hard_cap = None
    loaded_revision = row["config_hash"] or row["config_sha256"]
    if loaded_revision == revision:
        state, message = "loaded", "Dispatcher 已加载当前配置"
    elif row["error_message"] == "config_invalid":
        state, message = "failed", "Dispatcher 加载失败，仍使用上次有效配置；请检查运行日志"
    elif loaded_revision:
        state, message = "pending", "配置已保存，等待 Dispatcher 加载确认"
    else:
        state, message = "unknown", "尚无有效配置加载上报"
    return DispatcherConfigLoadStatus(
        state=state, message=message, hard_cap=hard_cap, updated_at=row["updated_at"],
    )


def _values(config: dict) -> tuple[DispatcherRuntimeValues, DispatcherTaskValues]:
    try:
        runtime = RuntimeConfig.model_validate(config.get("runtime", {}))
        tasks = TasksConfig.model_validate(config.get("tasks", {}))
    except ValidationError as exc:
        # 不输出校验输入或完整异常，未展示的字段可能包含秘密。
        raise HTTPException(422, "运行配置中的 runtime 或 tasks 字段无效，请检查配置文件") from exc
    return (
        DispatcherRuntimeValues.model_validate(runtime.model_dump()),
        DispatcherTaskValues.model_validate(tasks.model_dump()),
    )


def _document(conn, store, config, revision) -> DispatcherRuntimeSettingsDocument:
    runtime, tasks = _values(config)
    return DispatcherRuntimeSettingsDocument(
        revision=revision, runtime=runtime, tasks=tasks,
        load_status=_load_status(conn, store, revision),
    )


def read_runtime_settings(conn) -> DispatcherRuntimeSettingsDocument:
    store = dispatcher_config_store(conn)
    config, _content, revision = store.read()
    return _document(conn, store, config, revision)


def write_runtime_settings(conn, body: DispatcherRuntimeSettingsUpdate) -> DispatcherRuntimeSettingsDocument:
    store = dispatcher_config_store(conn)
    patch = body.model_dump(exclude_unset=True, exclude={"revision"})

    def update(config: dict) -> None:
        if "runtime" in patch:
            config.setdefault("runtime", {}).update(patch["runtime"])
        for task, values in patch.get("tasks", {}).items():
            config.setdefault("tasks", {}).setdefault(task, {}).update(values)
        runtime, _tasks = _values(config)
        if runtime.max_project_workers > runtime.max_workers:
            raise HTTPException(422, "runtime.max_project_workers：单项目并发不能超过总 Worker 并发")
        status = _load_status(conn, store, body.revision)
        if status.hard_cap is not None and runtime.max_workers > status.hard_cap:
            raise HTTPException(
                422,
                f"runtime.max_workers：不能超过当前 Dispatcher 容量上限 {status.hard_cap}；扩容需调整启动参数并协调重启",
            )
        leases = conn.execute("SELECT intent_timeout, reason_timeout FROM settings WHERE id = 1").fetchone()
        if leases is None:
            raise HTTPException(409, "Server 租约设置不可读取，未保存运行配置")
        if any(runtime.interval >= leases[field] for field in ("intent_timeout", "reason_timeout")):
            raise HTTPException(422, "runtime.interval：调度间隔必须小于 Server 当前失联租约阈值")

    config, _content, revision = store.mutate(body.revision, update)
    return _document(conn, store, config, revision)
