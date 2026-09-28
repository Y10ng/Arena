"""Dispatcher 上报的基础设施能力接口。"""

from __future__ import annotations

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException

from cairn.dispatcher.config import credential_is_overridden_by_environment
from cairn.server.auth import protect_cookie_request, require_super_admin
from cairn.server.db import get_conn
from cairn.server.models import (
    DispatcherConfigDocument,
    DispatcherConfigUpdate,
    DispatcherConnectivityTest,
    DispatcherConnectivityTestAccepted,
    DispatcherConnectivityTestStart,
    DispatcherControlCollection,
    DispatcherControlItem,
    DispatcherProviderCollection,
    DispatcherProviderItem,
    DispatcherControlUpdate,
    DispatcherCredentialStatus,
    DispatcherCredentialUpdate,
    DispatcherRevisionRequest,
    DispatcherYamlDocument,
    InfrastructureCapability,
    InfrastructureImage,
    InfrastructureWorker,
    ProjectWorkerOption,
)
from cairn.server.services import (
    dispatcher_config_store,
    dispatcher_inventory_caps_with_source,
    get_connectivity_test,
    latest_dispatcher_container_images,
    latest_dispatcher_worker_pool,
    read_dispatcher_config,
    read_dispatcher_yaml,
    redact_dispatcher_config,
    redact_dispatcher_document,
    start_connectivity_test,
    write_dispatcher_config,
    write_dispatcher_yaml,
)
router = APIRouter(tags=["infrastructure"], dependencies=[Depends(require_super_admin)])
catalog_router = APIRouter(tags=["infrastructure"])


class DispatcherYamlUpdate(BaseModel):
    content: str
    revision: str | None = None


# worker/image/capability 均来自 dispatcher 最近一次心跳，是监控视角而非 Server 自行发现。
def _worker_pool():
    with get_conn() as conn:
        return latest_dispatcher_worker_pool(conn)


@catalog_router.get(
    "/project-worker-options",
    response_model=list[ProjectWorkerOption],
    dependencies=[Depends(protect_cookie_request)],
)
def list_project_worker_options():
    return [ProjectWorkerOption.model_validate(item) for item in _worker_pool()]


@router.get("/workers", response_model=list[InfrastructureWorker])
def list_workers():
    return [
        InfrastructureWorker.model_validate({**item, "source": "dispatcher_report"})
        for item in _worker_pool()
    ]


@router.get("/images", response_model=list[InfrastructureImage])
def list_images():
    with get_conn() as conn:
        return [
            InfrastructureImage(image=image, source="dispatcher_report")
            for image in sorted(latest_dispatcher_container_images(conn))
        ]


@router.get("/capabilities", response_model=list[InfrastructureCapability])
def list_capabilities():
    with get_conn() as conn:
        caps, source = dispatcher_inventory_caps_with_source(conn)
        return [
            InfrastructureCapability(name=cap, source=source)
            for cap in sorted(caps)
        ]


@router.get("/dispatcher/yaml", response_model=DispatcherYamlDocument)
def get_dispatcher_yaml():
    with get_conn() as conn:
        return read_dispatcher_yaml(conn)


@router.put("/dispatcher/yaml", response_model=DispatcherYamlDocument)
def put_dispatcher_yaml(body: DispatcherYamlUpdate):
    with get_conn() as conn:
        # 写 dispatch.yaml 前会由 service 重新加载校验，避免控制台保存无效运行配置。
        return write_dispatcher_yaml(conn, body.content, expected_revision=body.revision)


@router.get("/dispatcher/config", response_model=DispatcherConfigDocument)
def get_dispatcher_config():
    with get_conn() as conn:
        return read_dispatcher_config(conn)


@router.put("/dispatcher/config", response_model=DispatcherConfigDocument)
def put_dispatcher_config(body: DispatcherConfigUpdate):
    with get_conn() as conn:
        return write_dispatcher_config(conn, body.config, expected_revision=body.revision)


def _control_collection(collection: str) -> DispatcherControlCollection | DispatcherProviderCollection:
    with get_conn() as conn:
        store = dispatcher_config_store(conn)
        config, _content, revision = store.read()
        safe_config = redact_dispatcher_config(config)
        raw_items = safe_config.get(collection) or []
        items = [dict(item) for item in raw_items if isinstance(item, dict)] if isinstance(raw_items, list) else []
        if collection == "providers":
            common_env = config.get("common_env") if isinstance(config.get("common_env"), dict) else {}
            for item in items:
                credential_ref = str(item.get("credential_ref") or "").strip()
                item["credential_ref"] = credential_ref
                item["credential_configured"] = bool(
                    credential_ref and str(common_env.get(credential_ref) or "")
                )
                overridden = credential_is_overridden_by_environment(credential_ref)
                item["credential_env_override"] = overridden
                item["credential_warning"] = (
                    "运行环境中的凭据已覆盖当前保存值；删除页面中的 Key 不会撤销该环境凭据"
                    if overridden
                    else None
                )
            return DispatcherProviderCollection(revision=revision, items=items)
        return DispatcherControlCollection(revision=revision, items=items)


def _control_item(collection: str, item_id: str) -> DispatcherControlItem | DispatcherProviderItem:
    document = _control_collection(collection)
    for item in document.items:
        if collection == "providers":
            current_id = item.id
        else:
            current_id = str(item.get("id") or item.get("name") or "")
        if current_id == item_id:
            if collection == "providers":
                return DispatcherProviderItem(revision=document.revision, item=item)
            return DispatcherControlItem(revision=document.revision, item=item)
    raise HTTPException(404, f"{collection} item not found: {item_id}")


def _control_update_item(body: DispatcherControlUpdate, kind: str) -> dict[str, object]:
    nested = getattr(body, kind, None)
    if isinstance(nested, dict):
        return nested
    if body.item:
        return body.item
    raise HTTPException(422, f"请求体缺少 {kind} 配置")


def _control_result(store, result) -> DispatcherConfigDocument:
    config, _content, revision = result
    safe, safe_content = redact_dispatcher_document(config)
    return DispatcherConfigDocument(
        path=str(store.path),
        content=safe_content,
        config=safe,
        revision=revision,
    )


@router.get("/dispatcher/providers", response_model=DispatcherProviderCollection)
def list_dispatcher_providers():
    return _control_collection("providers")


@router.get("/dispatcher/providers/{provider_id}", response_model=DispatcherProviderItem)
def get_dispatcher_provider(provider_id: str):
    return _control_item("providers", provider_id)


@router.post("/dispatcher/providers", response_model=DispatcherConfigDocument)
def create_dispatcher_provider(body: DispatcherControlUpdate):
    with get_conn() as conn:
        store = dispatcher_config_store(conn)
        return _control_result(store, store.create_item("providers", _control_update_item(body, "provider"), expected_revision=body.revision))


@router.put("/dispatcher/providers/{provider_id}", response_model=DispatcherConfigDocument)
def update_dispatcher_provider(provider_id: str, body: DispatcherControlUpdate):
    with get_conn() as conn:
        store = dispatcher_config_store(conn)
        return _control_result(
            store,
            store.update_item("providers", provider_id, _control_update_item(body, "provider"), expected_revision=body.revision),
        )


@router.delete("/dispatcher/providers/{provider_id}", response_model=DispatcherConfigDocument)
def delete_dispatcher_provider(provider_id: str, body: DispatcherRevisionRequest):
    with get_conn() as conn:
        store = dispatcher_config_store(conn)
        return _control_result(store, store.delete_item("providers", provider_id, expected_revision=body.revision))


@router.put("/dispatcher/providers/{provider_id}/credential", response_model=DispatcherCredentialStatus)
def put_dispatcher_provider_credential(provider_id: str, body: DispatcherCredentialUpdate):
    with get_conn() as conn:
        store = dispatcher_config_store(conn)
        revision, credential_ref, configured = store.put_credential(
            provider_id,
            body.credential,
            expected_revision=body.revision,
        )
        return DispatcherCredentialStatus(
            provider_id=provider_id,
            credential_ref=credential_ref,
            configured=configured,
            revision=revision,
        )


@router.delete("/dispatcher/providers/{provider_id}/credential", response_model=DispatcherCredentialStatus)
def delete_dispatcher_provider_credential(provider_id: str, body: DispatcherRevisionRequest):
    with get_conn() as conn:
        store = dispatcher_config_store(conn)
        revision, credential_ref, configured = store.delete_credential(
            provider_id,
            expected_revision=body.revision,
        )
        return DispatcherCredentialStatus(
            provider_id=provider_id,
            credential_ref=credential_ref,
            configured=configured,
            revision=revision,
        )


@router.get("/dispatcher/models", response_model=DispatcherControlCollection)
def list_dispatcher_models():
    return _control_collection("models")


@router.get("/dispatcher/models/{model_id}", response_model=DispatcherControlItem)
def get_dispatcher_model(model_id: str):
    return _control_item("models", model_id)


@router.post("/dispatcher/models", response_model=DispatcherConfigDocument)
def create_dispatcher_model(body: DispatcherControlUpdate):
    with get_conn() as conn:
        store = dispatcher_config_store(conn)
        return _control_result(store, store.create_item("models", _control_update_item(body, "model"), expected_revision=body.revision))


@router.put("/dispatcher/models/{model_id}", response_model=DispatcherConfigDocument)
def update_dispatcher_model(model_id: str, body: DispatcherControlUpdate):
    with get_conn() as conn:
        store = dispatcher_config_store(conn)
        return _control_result(store, store.update_item("models", model_id, _control_update_item(body, "model"), expected_revision=body.revision))


@router.delete("/dispatcher/models/{model_id}", response_model=DispatcherConfigDocument)
def delete_dispatcher_model(model_id: str, body: DispatcherRevisionRequest):
    with get_conn() as conn:
        store = dispatcher_config_store(conn)
        return _control_result(store, store.delete_item("models", model_id, expected_revision=body.revision))


@router.get("/dispatcher/workers", response_model=DispatcherControlCollection)
def list_dispatcher_workers():
    return _control_collection("workers")


@router.get("/dispatcher/workers/{worker_id}", response_model=DispatcherControlItem)
def get_dispatcher_worker(worker_id: str):
    return _control_item("workers", worker_id)


@router.post("/dispatcher/workers", response_model=DispatcherConfigDocument)
def create_dispatcher_worker(body: DispatcherControlUpdate):
    with get_conn() as conn:
        store = dispatcher_config_store(conn)
        return _control_result(store, store.create_item("workers", _control_update_item(body, "worker"), expected_revision=body.revision))


@router.put("/dispatcher/workers/{worker_id}", response_model=DispatcherConfigDocument)
def update_dispatcher_worker(worker_id: str, body: DispatcherControlUpdate):
    with get_conn() as conn:
        store = dispatcher_config_store(conn)
        return _control_result(store, store.update_item("workers", worker_id, _control_update_item(body, "worker"), expected_revision=body.revision))


@router.delete("/dispatcher/workers/{worker_id}", response_model=DispatcherConfigDocument)
def delete_dispatcher_worker(worker_id: str, body: DispatcherRevisionRequest):
    with get_conn() as conn:
        store = dispatcher_config_store(conn)
        return _control_result(store, store.delete_item("workers", worker_id, expected_revision=body.revision))


def _start_connectivity(target_type: str, target_id: str, body: DispatcherConnectivityTestStart):
    with get_conn() as conn:
        store = dispatcher_config_store(conn)
        test = start_connectivity_test(
            conn,
            store,
            target_type,
            target_id,
            timeout_seconds=body.timeout_seconds,
        )
    return DispatcherConnectivityTestAccepted(
        test_id=test.id,
        status=test.status,
        poll_url=f"/dispatcher/connectivity-tests/{test.id}",
    )


@router.post(
    "/dispatcher/providers/{provider_id}/connectivity-tests",
    response_model=DispatcherConnectivityTestAccepted,
    status_code=202,
)
@router.post(
    "/dispatcher/providers/{provider_id}/test",
    response_model=DispatcherConnectivityTestAccepted,
    status_code=202,
    include_in_schema=False,
)
def test_dispatcher_provider(provider_id: str, body: DispatcherConnectivityTestStart):
    return _start_connectivity("provider", provider_id, body)


@router.post(
    "/dispatcher/models/{model_id}/connectivity-tests",
    response_model=DispatcherConnectivityTestAccepted,
    status_code=202,
)
@router.post(
    "/dispatcher/models/{model_id}/test",
    response_model=DispatcherConnectivityTestAccepted,
    status_code=202,
    include_in_schema=False,
)
def test_dispatcher_model(model_id: str, body: DispatcherConnectivityTestStart):
    return _start_connectivity("model", model_id, body)


@router.post(
    "/dispatcher/workers/{worker_id}/connectivity-tests",
    response_model=DispatcherConnectivityTestAccepted,
    status_code=202,
)
@router.post(
    "/dispatcher/workers/{worker_id}/test",
    response_model=DispatcherConnectivityTestAccepted,
    status_code=202,
    include_in_schema=False,
)
def test_dispatcher_worker(worker_id: str, body: DispatcherConnectivityTestStart):
    return _start_connectivity("worker", worker_id, body)


@router.get(
    "/dispatcher/connectivity-tests/{test_id}",
    response_model=DispatcherConnectivityTest,
)
def get_dispatcher_connectivity_test(test_id: str):
    with get_conn() as conn:
        store = dispatcher_config_store(conn)
        return get_connectivity_test(conn, store, test_id)
