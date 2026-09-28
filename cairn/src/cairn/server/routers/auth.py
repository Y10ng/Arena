"""登录、当前会话和退出路由。"""

from pydantic import BaseModel, Field
from fastapi import APIRouter, Cookie, Depends, Request, Response

from cairn.server.auth import (
    SESSION_COOKIE,
    RequestPrincipal,
    change_user_password,
    login,
    logout,
    protect_auth_cookie_request,
    rotate_session_csrf,
)

router = APIRouter(prefix="/auth", tags=["authentication"])


class LoginRequest(BaseModel):
    login: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1024)


class SessionView(BaseModel):
    user_id: str
    tenant_id: str
    role: str
    must_change_password: bool
    csrf_token: str | None = None


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(
        min_length=1,
        max_length=1024,
        repr=False,
        json_schema_extra={"writeOnly": True},
    )
    new_password: str = Field(
        min_length=1,
        max_length=1024,
        repr=False,
        json_schema_extra={"writeOnly": True},
    )


def _view(principal: RequestPrincipal, csrf_token: str | None = None) -> SessionView:
    assert principal.tenant_id is not None and principal.role is not None
    return SessionView(
        user_id=principal.subject_id,
        tenant_id=principal.tenant_id,
        role=principal.role,
        must_change_password=principal.must_change_password,
        csrf_token=csrf_token,
    )


@router.post("/login", response_model=SessionView)
def login_route(body: LoginRequest, response: Response):
    principal, csrf = login(response, body.login, body.password)
    return _view(principal, csrf)


@router.get("/session", response_model=SessionView)
def session_route(principal: RequestPrincipal = Depends(protect_auth_cookie_request)):
    return _view(principal)


@router.post("/csrf", response_model=SessionView)
def csrf_route(
    request: Request,
    response: Response,
    token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
):
    principal, csrf = rotate_session_csrf(request, token)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return _view(principal, csrf)


@router.post("/password/change", response_model=SessionView)
def password_change_route(
    body: PasswordChangeRequest,
    _principal: RequestPrincipal = Depends(protect_auth_cookie_request),
    token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
):
    principal = change_user_password(token, body.current_password, body.new_password)
    return _view(principal)


@router.post("/logout", status_code=204)
def logout_route(
    response: Response,
    _principal: RequestPrincipal = Depends(protect_auth_cookie_request),
    token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
):
    logout(response, token)
