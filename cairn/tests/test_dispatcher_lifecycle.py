from __future__ import annotations

import importlib.util

from cairn.server.app import app
from route_helpers import iter_app_routes


def test_dispatcher_lifecycle_backend_is_removed() -> None:
    schema = app.openapi()

    assert not any(path.startswith("/dispatcher/lifecycle") for path in schema["paths"])
    assert "DispatcherLifecycleActionRequest" not in schema.get("components", {}).get("schemas", {})
    assert importlib.util.find_spec("cairn.server.services.dispatcher_supervisor") is None


def test_dispatcher_lifecycle_routes_are_not_registered() -> None:
    registered_paths = {route.path for route in iter_app_routes(app)}

    assert "/dispatcher/lifecycle" not in registered_paths
    assert "/dispatcher/lifecycle/start" not in registered_paths
    assert "/dispatcher/lifecycle/stop" not in registered_paths
    assert "/dispatcher/lifecycle/restart" not in registered_paths
