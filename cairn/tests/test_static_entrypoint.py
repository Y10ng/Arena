from __future__ import annotations

from cairn.server.app import index


def test_spa_entrypoint_disables_browser_cache() -> None:
    response = index()

    assert response.headers["cache-control"] == "no-cache, no-store, must-revalidate"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["expires"] == "0"
