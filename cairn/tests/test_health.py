from __future__ import annotations

from contextlib import contextmanager

from cairn.server import app as server_app
from cairn.server.routers import health
from route_helpers import iter_app_routes


def test_health_checks_configured_database(monkeypatch) -> None:
    executed: list[str] = []

    class Connection:
        def execute(self, sql: str) -> None:
            executed.append(sql)

    @contextmanager
    def fake_get_conn():
        yield Connection()

    monkeypatch.setattr(health, "get_conn", fake_get_conn)

    assert health.healthcheck() == {"status": "ok"}
    assert executed == ["SELECT 1"]


def test_health_route_is_registered_without_authentication_dependency() -> None:
    route = next(route for route in iter_app_routes(server_app.app) if getattr(route, "path", None) == "/health")

    assert route.methods == {"GET"}
    assert route.dependant.dependencies == []


def test_iter_app_routes_preserves_nested_prefixes_and_inherited_dependencies() -> None:
    from fastapi import APIRouter, Depends, FastAPI

    def require_outer():
        pass

    def require_inner():
        pass

    child = APIRouter()

    @child.get("/protected")
    def protected():
        return {}

    outer = APIRouter()
    outer.include_router(child, prefix="/inner", dependencies=[Depends(require_inner)])
    application = FastAPI()
    application.include_router(outer, prefix="/outer", dependencies=[Depends(require_outer)])
    routes = list(iter_app_routes(application))
    route = next(route for route in routes if route.path == "/outer/inner/protected")
    assert route.methods == {"GET"}
    assert {dependency.call for dependency in route.dependant.dependencies} == {require_outer, require_inner}
    assert any(route.path == "/openapi.json" for route in routes)


def test_iter_app_routes_keeps_all_flat_routes_on_older_fastapi(monkeypatch) -> None:
    from types import SimpleNamespace
    from fastapi import routing

    routes = [object(), object()]
    monkeypatch.delattr(routing, "iter_route_contexts", raising=False)
    assert list(iter_app_routes(SimpleNamespace(routes=routes))) == routes
