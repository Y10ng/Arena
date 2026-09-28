"""普通用户的 Tenant Model、草稿发现与真实测试接口。"""

from fastapi import APIRouter, Depends, HTTPException, Response

from cairn.server.auth import RequestPrincipal, protect_cookie_request
from cairn.server.schemas.models import (
    CreateTenantModelRequest,
    DeleteModelCredentialRequest,
    DeleteTenantModelRequest,
    DiscoverTenantModelRequest,
    ModelDiscoveryResult,
    ModelTestResult,
    TenantModel,
    TestTenantModelRequest,
    UpdateTenantModelRequest,
)
from cairn.server.services.model_credentials import (
    CredentialConfigurationError,
    CredentialDecryptionError,
)
from cairn.server.services.model_operations import discover_model_draft, test_model_draft
from cairn.server.services.model_transport import ModelTransportError
from cairn.server.services.tenant_models import (
    create_tenant_model,
    delete_tenant_model,
    delete_tenant_model_credential,
    get_tenant_model,
    list_tenant_models,
    update_tenant_model,
)

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=list[TenantModel])
def get_models(principal: RequestPrincipal = Depends(protect_cookie_request)):
    return list_tenant_models(tenant_id=_tenant_id(principal))


@router.post("", response_model=TenantModel, status_code=201)
def post_model(
    body: CreateTenantModelRequest,
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    try:
        return create_tenant_model(body, principal=principal)
    except CredentialConfigurationError as exc:
        raise HTTPException(503, "Model credential service is unavailable") from exc


@router.post("/discover", response_model=ModelDiscoveryResult)
def post_model_discovery(
    body: DiscoverTenantModelRequest,
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    return _draft_request(discover_model_draft, body, principal)


@router.post("/test", response_model=ModelTestResult)
def post_model_test(
    body: TestTenantModelRequest,
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    return _draft_request(test_model_draft, body, principal)


@router.get("/{model_id}", response_model=TenantModel)
def get_model(
    model_id: str,
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    return get_tenant_model(model_id, tenant_id=_tenant_id(principal))


@router.put("/{model_id}", response_model=TenantModel)
def put_model(
    model_id: str,
    body: UpdateTenantModelRequest,
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    try:
        return update_tenant_model(model_id, body, principal=principal)
    except CredentialConfigurationError as exc:
        raise HTTPException(503, "Model credential service is unavailable") from exc


@router.delete("/{model_id}", status_code=204)
def delete_model(
    model_id: str,
    body: DeleteTenantModelRequest,
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    delete_tenant_model(
        model_id,
        expected_revision=body.expected_revision,
        principal=principal,
    )
    return Response(status_code=204)


@router.delete("/{model_id}/credential", response_model=TenantModel)
def delete_model_credential(
    model_id: str,
    body: DeleteModelCredentialRequest,
    principal: RequestPrincipal = Depends(protect_cookie_request),
):
    return delete_tenant_model_credential(
        model_id,
        expected_revision=body.expected_revision,
        principal=principal,
    )


def _draft_request(handler, body, principal: RequestPrincipal):
    try:
        return handler(body, principal=principal)
    except (CredentialConfigurationError, CredentialDecryptionError) as exc:
        raise HTTPException(503, "Model credential service is unavailable") from exc
    except ModelTransportError as exc:
        raise HTTPException(502, exc.detail) from exc


def _tenant_id(principal: RequestPrincipal) -> str:
    if principal.kind != "user" or not principal.tenant_id:
        raise HTTPException(403, "User tenant is required")
    return principal.tenant_id
