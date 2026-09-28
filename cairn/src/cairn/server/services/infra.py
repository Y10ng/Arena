"""Dispatcher 上报清单与可选 dispatch.yaml 编辑服务。"""

from __future__ import annotations

import yaml

from .common import *

from cairn.dispatcher.config import DispatchConfig
from cairn.server.models import DispatcherConfigDocument, DispatcherYamlDocument
from .dispatcher_config_store import DispatcherConfigStore, redact_dispatcher_document
from .time_utils import timestamp_delta_seconds, utcnow

DISPATCH_CONFIG_ENV = "CAIRN_DISPATCH_CONFIG"
DISPATCH_YAML_ALLOWED_ROOTS_ENV = "CAIRN_DISPATCH_YAML_ALLOWED_ROOTS"
DISPATCHER_HEARTBEAT_STALE_SECONDS = 60


def latest_dispatcher_worker_pool(conn: sqlite3.Connection) -> list[dict[str, object]]:
    row = _latest_dispatcher_row(conn)
    if row is None:
        return []
    # 心跳中的 worker_pool 是 JSON 快照，服务端只保留 dict 项，坏数据不阻断控制台读取。
    payload = _loads_json_array(row["worker_pool_json"])
    items: list[dict[str, object]] = []
    for item in payload:
        if isinstance(item, dict):
            items.append(dict(item))
    return items


def dispatcher_effective_status(
    conn: sqlite3.Connection,
    *,
    now: str | None = None,
) -> str | None:
    """返回考虑 heartbeat stale 后的 Dispatcher 运行状态。"""
    row = _latest_dispatcher_row(conn)
    if row is None:
        return None
    status = str(row["status"] or "").strip() or None
    age_seconds = timestamp_delta_seconds(row["updated_at"], now or utcnow())
    if (
        age_seconds is not None
        and age_seconds > DISPATCHER_HEARTBEAT_STALE_SECONDS
        and status in {"starting", "running", "idle"}
    ):
        return "stale"
    return status


def dispatcher_executor_available(conn: sqlite3.Connection) -> bool:
    """Tenant Worker 只依赖活跃 Dispatcher，不依赖旧静态 worker pool。"""
    return dispatcher_effective_status(conn) in {"starting", "running", "idle"}


def latest_dispatcher_worker_names(conn: sqlite3.Connection) -> set[str]:
    names: set[str] = set()
    for item in latest_dispatcher_worker_pool(conn):
        text = str(item.get("worker_name") or "").strip()
        if text:
            names.add(text)
    return names


def latest_dispatcher_container_images(conn: sqlite3.Connection) -> set[str]:
    row = _latest_dispatcher_row(conn)
    if row is None:
        return set()
    return set(_clean_string_items(_loads_json_array(row["container_images_json"])))


def dispatcher_report_caps_with_source(conn: sqlite3.Connection) -> tuple[set[str], str]:
    row = _latest_dispatcher_row(conn)
    if row is None or row["cap_baseline_json"] is None:
        return set(), "unavailable"
    return set(_clean_string_items(_loads_json_array(row["cap_baseline_json"]))), "dispatcher_report"


def dispatcher_declared_inventory(conn: sqlite3.Connection) -> dict[str, object]:
    row = _latest_dispatcher_row(conn)
    if row is not None:
        inventory = _loads_json_object(row["declared_inventory_json"])
        if _inventory_has_items(inventory):
            return inventory
    yaml_inventory = _dispatcher_yaml_inventory(conn)
    if _inventory_has_items(yaml_inventory):
        return yaml_inventory
    return _legacy_inventory_from_report(conn)


def dispatcher_inventory_caps_with_source(conn: sqlite3.Connection) -> tuple[set[str], str]:
    inventory = dispatcher_declared_inventory(conn)
    caps = set(_clean_string_items(list(inventory.get("capabilities") or [])))
    if caps:
        return caps, "declared_inventory"
    reported, source = dispatcher_report_caps_with_source(conn)
    if reported:
        return reported, source
    return set(), "unavailable"


def dispatcher_config_store(conn: sqlite3.Connection) -> DispatcherConfigStore:
    return DispatcherConfigStore(_server_reachable_yaml_path(conn))


def read_dispatcher_yaml(conn: sqlite3.Connection) -> DispatcherYamlDocument:
    store = dispatcher_config_store(conn)
    config, _content, revision = store.read()
    _safe, safe_content = redact_dispatcher_document(config)
    return DispatcherYamlDocument(path=str(store.path), content=safe_content, revision=revision)


def read_dispatcher_config(conn: sqlite3.Connection) -> DispatcherConfigDocument:
    store = dispatcher_config_store(conn)
    try:
        # 控制面可读取历史 engine 元数据；执行配置校验仅留在写入与 Dispatcher 启动边界。
        config, _content, revision = store.read()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, f"Invalid dispatch.yaml: {type(exc).__name__}") from exc
    safe, safe_content = redact_dispatcher_document(config)
    return DispatcherConfigDocument(path=str(store.path), content=safe_content, config=safe, revision=revision)


def write_dispatcher_yaml(
    conn: sqlite3.Connection,
    content: str,
    *,
    expected_revision: str | None = None,
) -> DispatcherYamlDocument:
    store = dispatcher_config_store(conn)
    config, _content, revision = store.write_content(content, expected_revision=expected_revision)
    _safe, safe_content = redact_dispatcher_document(config)
    return DispatcherYamlDocument(path=str(store.path), content=safe_content, revision=revision)


def write_dispatcher_config(
    conn: sqlite3.Connection,
    config: dict[str, object],
    *,
    expected_revision: str | None = None,
) -> DispatcherConfigDocument:
    store = dispatcher_config_store(conn)
    saved, _content, revision = store.write_config(dict(config), expected_revision=expected_revision)
    safe, safe_content = redact_dispatcher_document(saved)
    return DispatcherConfigDocument(path=str(store.path), content=safe_content, config=safe, revision=revision)


def _dispatcher_yaml_inventory(conn: sqlite3.Connection) -> dict[str, object]:
    try:
        path = _server_reachable_yaml_path(conn)
    except HTTPException:
        return {}
    if not path.exists() or not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        config = DispatchConfig.model_validate(data)
    except Exception:
        return {}
    return config.declared_inventory()


def _legacy_inventory_from_report(conn: sqlite3.Connection) -> dict[str, object]:
    row = _latest_dispatcher_row(conn)
    if row is None:
        return {}
    worker_items: list[dict[str, object]] = []
    for item in latest_dispatcher_worker_pool(conn):
        worker_id = str(item.get("worker_id") or item.get("worker_name") or "").strip()
        if not worker_id:
            continue
        worker_items.append(
            {
                "id": worker_id,
                "name": str(item.get("worker_name") or worker_id),
                "engine": item.get("engine") or item.get("worker_type"),
                "type": item.get("worker_type"),
                "model_id": str(item.get("model_id") or ""),
                "task_types": item.get("task_types") if isinstance(item.get("task_types"), list) else [],
                "concurrency": item.get("concurrency") or item.get("max_running"),
                "max_running": item.get("max_running") or item.get("concurrency"),
                "priority": item.get("priority"),
                "enabled": bool(item.get("enabled", True)),
            }
        )

    image_names = _clean_string_items(_loads_json_array(row["container_images_json"]))
    images = [
        {"id": "default" if index == 0 else _inventory_id(image), "image": image}
        for index, image in enumerate(image_names)
    ]
    cap_baseline = _clean_string_items(_loads_json_array(row["cap_baseline_json"]))
    container = _loads_json_object(row["container_json"])
    completed_action = str(container.get("completed_action") or "stop")
    network_policy = str(container.get("network_mode") or "none")
    profiles: list[dict[str, object]] = []
    if images or cap_baseline:
        image_ref = str(images[0]["id"]) if images else "default"
        profiles.append(
            {
                "id": "default",
                "image_ref": image_ref,
                "network_policy": network_policy,
                "cap_add": cap_baseline,
                "isolation": "shared_container",
                "completed_action": completed_action,
            }
        )
        if network_policy != "none":
            profiles.append(
                {
                    "id": "network",
                    "image_ref": image_ref,
                    "network_policy": network_policy,
                    "cap_add": cap_baseline,
                    "isolation": "shared_container",
                    "completed_action": completed_action,
                }
            )
    return {
        "providers": [],
        "models": [],
        "images": images,
        "capabilities": cap_baseline,
        "execution_profiles": profiles,
        "workers": worker_items,
    }


def _latest_dispatcher_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM dispatcher_status
        ORDER BY COALESCE(last_loop_finished_at, updated_at) DESC, updated_at DESC, id
        LIMIT 1
        """
    ).fetchone()


def _server_reachable_yaml_path(conn: sqlite3.Connection) -> Path:
    # Server-first 部署通过固定环境变量定位配置，不要求 Dispatcher 先发送心跳。
    raw_path = os.environ.get(DISPATCH_CONFIG_ENV, "").strip()
    if not raw_path:
        row = _latest_dispatcher_row(conn)
        if row is not None:
            # 保留旧部署兼容：未设置固定路径时继续接受 Dispatcher 上报路径。
            raw_path = row["server_reachable_path"] or row["config_path"]
    if not raw_path:
        raise HTTPException(409, f"Dispatch YAML path is not configured; set {DISPATCH_CONFIG_ENV}")
    path = Path(str(raw_path)).expanduser().resolve()
    # 无论路径来自环境变量还是心跳，都必须经过允许根检查，避免任意文件读写。
    if not _path_allowed_for_yaml_edit(path):
        roots = os.environ.get(DISPATCH_YAML_ALLOWED_ROOTS_ENV, "").strip()
        raise HTTPException(409, f"Dispatcher YAML path is outside allowed roots: {roots}")
    return path


def _path_allowed_for_yaml_edit(path: Path) -> bool:
    raw_roots = os.environ.get(DISPATCH_YAML_ALLOWED_ROOTS_ENV, "").strip()
    if not raw_roots:
        return True
    for raw_root in raw_roots.split(os.pathsep):
        text = raw_root.strip()
        if not text:
            continue
        try:
            path.relative_to(Path(text).expanduser().resolve())
            return True
        except ValueError:
            continue
    return False


def _loads_json_array(raw: str | None) -> list[object]:
    try:
        value = loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _loads_json_object(raw: str | None) -> dict[str, object]:
    try:
        value = loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _clean_string_items(data: list[object]) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for item in data:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
    return items


def _inventory_has_items(inventory: dict[str, object]) -> bool:
    return any(
        bool(inventory.get(key))
        for key in ("workers", "execution_profiles", "images", "capabilities", "models", "providers")
    )


def _inventory_id(value: str) -> str:
    safe = "".join(ch if ch.isalnum() else "-" for ch in value.lower()).strip("-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe[:80] or "item"
