"""受控宿主目录和只读 Docker 替身；不接触正式项目。"""
from contextlib import contextmanager
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from docker.errors import NotFound
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from cairn.server import auth
from cairn.server.routers import project_workspace as router
from cairn.server.services import project_workspace as service


@pytest.fixture
def scene(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    identity = "a" * 64
    labels = {"cairn.project_id": "p1", "cairn.engine": "pi", "cairn.pi.instance_id": "b" * 64,
              "cairn.runtime_signature": "c" * 64, "cairn.scenario_key": "goal-driven-v1"}
    calls = []
    container = SimpleNamespace(id=identity, attrs={"Id": identity, "Config": {"Labels": labels},
        "State": {"Status": "paused"}, "Mounts": [{"Type": "bind", "Destination": service.WORKSPACE_PATH, "Source": str(root)}]},
        reload=lambda: calls.append("inspect"))
    def get(value):
        assert value == identity
        calls.append("get")
        return container
    client = SimpleNamespace(api=SimpleNamespace(base_url="http+docker://localhost"),
                             info=lambda: {"ID": "daemon"}, containers=SimpleNamespace(get=get), close=lambda: None)
    record = ({"scenario_key": "goal-driven-v1"}, [{"dispatcher_id": "owner", "container_id": identity,
                                                  "docker_daemon_id": "daemon"}], True)
    monkeypatch.setattr(service, "_record", lambda project: record)
    monkeypatch.setattr(service.docker, "from_env", lambda **kwargs: client)
    return SimpleNamespace(root=root, client=client, container=container, record=record, calls=calls)


def identity():
    return service.list_workspace("p1")["workspace_id"]


def error(code, action):
    with pytest.raises(HTTPException) as raised:
        action()
    assert raised.value.detail == "workspace_" + code
    return raised.value


@pytest.mark.parametrize("state", ["running", "paused", "exited"])
def test_real_directory_lazy_sort_pagination_and_read_only(scene, monkeypatch, state):
    scene.container.attrs["State"]["Status"] = state
    monkeypatch.setattr(service, "PAGE_SIZE", 2)
    (scene.root / "z-empty").mkdir()
    (scene.root / "a-dir").mkdir()
    (scene.root / "a-dir" / "same.txt").write_text("nested")
    (scene.root / "same.txt").write_text("root")
    (scene.root / "未登记.txt").write_text("unregistered")
    first = service.list_workspace("p1")
    assert [entry["name"] for entry in first["entries"]] == ["a-dir", "z-empty"]
    second = service.list_workspace("p1", workspace_id=first["workspace_id"], cursor=first["next_cursor"])
    assert [entry["name"] for entry in second["entries"]] == ["same.txt", "未登记.txt"]
    assert not second["has_more"]
    assert service.list_workspace("p1", "z-empty", workspace_id=first["workspace_id"])["entries"] == []
    assert service.preview_workspace("p1", "same.txt", workspace_id=first["workspace_id"])["content"] == "root"
    assert service.preview_workspace("p1", "a-dir/same.txt", workspace_id=first["workspace_id"])["content"] == "nested"
    assert set(scene.calls) == {"get", "inspect"}
    assert str(scene.root) not in str(first)


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "a/../b", "a//b", "a\\b", "a\x00b", "./x"])
def test_paths_rejected(scene, path):
    wid = identity()
    for action in (lambda: service.list_workspace("p1", path, workspace_id=wid),
                   lambda: service.preview_workspace("p1", path, workspace_id=wid),
                   lambda: service.download_workspace("p1", path, workspace_id=wid)):
        error("path_invalid", action)


def test_links_special_files_and_private_session_blocked(scene):
    (scene.root / ".cairn/pi").mkdir(parents=True)
    (scene.root / ".cairn/pi/secret").write_text("private")
    (scene.root / ".cairn/artifacts/registered").mkdir(parents=True)
    (scene.root / ".cairn/artifacts/registered/ok").write_text("saved")
    (scene.root / "outside").symlink_to(scene.root.parent, target_is_directory=True)
    (scene.root / "alias").symlink_to(scene.root / ".cairn/pi", target_is_directory=True)
    (scene.root / "one").write_text("linked")
    os.link(scene.root / "one", scene.root / "two")
    os.mkfifo(scene.root / "pipe")
    wid = identity()
    entries = service.list_workspace("p1", workspace_id=wid)["entries"]
    assert all(entry["kind"] == "blocked" for entry in entries if entry["name"] != ".cairn")
    assert [entry["name"] for entry in service.list_workspace("p1", ".cairn", workspace_id=wid)["entries"]] == ["artifacts"]
    for path in ("outside", "alias/secret", "one", "pipe"):
        for operation in (service.preview_workspace, service.download_workspace):
            error("path_blocked", lambda: operation("p1", path, workspace_id=wid))
    for operation in (service.list_workspace, service.preview_workspace, service.download_workspace):
        error("path_private", lambda: operation("p1", ".cairn/pi/secret", workspace_id=wid))
    assert service.preview_workspace("p1", ".cairn/artifacts/registered/ok", workspace_id=wid)["content"] == "saved"


def test_bounded_windows_binary_safe_and_full_stream(scene):
    raw = b"a" * service.PREVIEW_BYTES + b"tail"
    (scene.root / "large.log").write_bytes(raw)
    (scene.root / "empty").touch()
    (scene.root / "script.svg").write_text('<svg onload="alert(1)"/>')
    (scene.root / "script.html").write_text('<script>alert(1)</script>')
    (scene.root / "binary").write_bytes(b"\x00\xff")
    wid = identity()
    assert service.preview_workspace("p1", "empty", workspace_id=wid)["content"] == ""
    head = service.preview_workspace("p1", "large.log", workspace_id=wid)
    tail = service.preview_workspace("p1", "large.log", workspace_id=wid, window="tail")
    assert head["bytes_read"] == service.PREVIEW_BYTES and head["truncated"]
    assert tail["content"].endswith("tail")
    assert service.preview_workspace("p1", "binary", workspace_id=wid)["preview_kind"] == "binary"
    for path in ("script.svg", "script.html"):
        assert service.preview_workspace("p1", path, workspace_id=wid)["preview_kind"] == "text"
        error("inline_unsupported", lambda: service.download_workspace("p1", path, workspace_id=wid, inline=True))
    download = service.download_workspace("p1", "large.log", workspace_id=wid)
    try:
        chunks = list(download.chunks())
        assert b"".join(chunks) == raw
        assert max(map(len, chunks)) <= service.CHUNK_BYTES
    finally:
        os.close(download.fd)


def test_open_descriptor_survives_replacement_without_following_link(scene):
    (scene.root / "file").write_text("original")
    download = service.download_workspace("p1", "file", workspace_id=identity())
    (scene.root / "file").rename(scene.root / "old")
    (scene.root / "file").symlink_to("/etc/passwd")
    try:
        assert b"".join(download.chunks()) == b"original"
    finally:
        os.close(download.fd)
    error("path_blocked", lambda: service.preview_workspace("p1", "file", workspace_id=identity()))


def test_workspace_identity_and_cursor_fences(scene, monkeypatch):
    (scene.root / "one").touch()
    (scene.root / "two").touch()
    monkeypatch.setattr(service, "PAGE_SIZE", 1)
    listing = service.list_workspace("p1")
    error("cursor_invalid", lambda: service.list_workspace("p1", workspace_id=listing["workspace_id"], cursor="!bad"))
    error("identity_required", lambda: service.list_workspace("p1", "child"))
    scene.root.rename(scene.root.with_name("old-root"))
    scene.root.mkdir()
    error("identity_changed", lambda: service.list_workspace("p1", workspace_id=listing["workspace_id"]))
    error("cursor_invalid", lambda: service.list_workspace("p1", workspace_id=identity(), cursor=listing["next_cursor"]))


def test_deleted_unknown_new_layer_remote_and_permission_are_distinct(scene, monkeypatch):
    monkeypatch.setattr(service, "_record", lambda project: (scene.record[0], [], False))
    error("not_created", identity)
    monkeypatch.setattr(service, "_record", lambda project: (scene.record[0], [], True))
    error("owner_unknown", identity)
    monkeypatch.setattr(service, "_record", lambda project: scene.record)
    original_get = scene.client.containers.get
    def missing(_):
        raise NotFound("private Docker diagnostic")
    scene.client.containers.get = missing
    assert error("deleted", identity).status_code == 410
    scene.client.containers.get = original_get
    scene.client.api.base_url = "http://remote:2375"
    error("topology_unsupported", identity)
    scene.client.api.base_url = "http+docker://localhost"
    original_mounts = scene.container.attrs["Mounts"]
    scene.container.attrs["Mounts"] = []
    error("layer_unsupported", identity)
    scene.container.attrs["Mounts"] = original_mounts
    def denied(_):
        raise PermissionError("private host path")
    monkeypatch.setattr(service, "_open_root", denied)
    assert error("permission_denied", identity).status_code == 403


def test_identity_mount_alias_and_nested_mount_boundaries(scene):
    labels = scene.container.attrs["Config"]["Labels"]
    labels["cairn.project_id"] = "p2"
    error("owner_unknown", identity)
    labels["cairn.project_id"] = "p1"
    labels["cairn.container_role"] = "report"
    labels["cairn.report_task_run_id"] = "tr1"
    error("owner_unknown", identity)
    labels.pop("cairn.container_role")
    labels.pop("cairn.report_task_run_id")
    scene.container.attrs["Mounts"].append({"Type": "bind", "Source": str(scene.root / "private"), "Destination": "/control"})
    error("path_private", identity)
    scene.container.attrs["Mounts"].pop()
    (scene.root / "nested").mkdir()
    scene.container.attrs["Mounts"].append({"Type": "volume", "Destination": service.WORKSPACE_PATH + "/nested"})
    wid = identity()
    assert service.list_workspace("p1", workspace_id=wid)["entries"] == []
    error("path_private", lambda: service.list_workspace("p1", "nested", workspace_id=wid))


def test_http_auth_guards_all_reads_and_stream_headers(scene, monkeypatch):
    app = FastAPI()
    app.include_router(router.router)
    client = TestClient(app)
    @contextmanager
    def connection():
        yield object()
    monkeypatch.setattr(auth, "get_conn", connection)
    monkeypatch.setattr(auth, "_project_access_exists", lambda conn, project, principal: project == "p1")
    app.dependency_overrides[auth.require_dispatcher_protocol] = lambda: SimpleNamespace(kind="user")
    for endpoint in ("entries", "preview", "content", "search"):
        response = client.get(f"/projects/p2/workspace/{endpoint}", params={"workspace_id": "a" * 64, "path": "x", "q": "x"})
        assert response.status_code == 404
    assert scene.calls == []
    (scene.root / "script.html").write_text("<script>bad()</script>")
    wid = client.get("/projects/p1/workspace/entries").json()["workspace_id"]
    response = client.get("/projects/p1/workspace/content", params={"workspace_id": wid, "path": "script.html"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.content == b"<script>bad()</script>"


def test_search_nested_paths_pages_and_bounded_continuation_without_content(scene, monkeypatch):
    (scene.root / "unopened/deep").mkdir(parents=True)
    for index in range(120):
        (scene.root / f"unopened/deep/{index:03}.TXT").write_text("not searched")
    monkeypatch.setattr(service, "SEARCH_STEPS", 12)
    monkeypatch.setattr(service, "PAGE_SIZE", 7)
    monkeypatch.setattr(service, "SEARCH_SECONDS", 10)
    def no_content(*args):
        pytest.fail("搜索不能读取文件正文")
    monkeypatch.setattr(service.os, "pread", no_content)
    wid = identity()
    def collect(q):
        cursor, result, pages = None, [], 0
        while True:
            page = service.search_workspace("p1", q, workspace_id=wid, cursor=cursor)
            pages += 1
            assert pages < 100
            assert len(page["entries"]) <= 7
            result.extend(entry["path"] for entry in page["entries"])
            cursor = page["next_cursor"]
            assert page["has_more"] == bool(cursor)
            if not cursor:
                return result, pages
    paths, pages = collect("  DEEP/  ")
    assert pages > 1 and len(paths) == len(set(paths)) == 120
    assert collect(".txt")[0] == paths
    assert collect("not searched")[0] == []
    assert collect("missing")[1] > 1


def test_search_private_mounts_links_and_cursor_binding(scene, monkeypatch):
    for path in ("visible", ".cairn/pi", "nested"):
        (scene.root / path).mkdir(parents=True)
        (scene.root / path / "match.txt").write_text("private or public")
    (scene.root / "alias").symlink_to(scene.root / ".cairn/pi", target_is_directory=True)
    (scene.root / "linked-match").write_text("linked")
    os.link(scene.root / "linked-match", scene.root / "linked-copy")
    os.mkfifo(scene.root / "match-pipe")
    scene.container.attrs["Mounts"].append({"Type": "volume", "Destination": service.WORKSPACE_PATH + "/nested"})
    wid = identity()
    page = service.search_workspace("p1", "match", workspace_id=wid)
    assert [item["path"] for item in page["entries"]] == ["visible/match.txt"]
    assert not page["has_more"]
    monkeypatch.setattr(service, "SEARCH_STEPS", 3)
    first = service.search_workspace("p1", "match", workspace_id=wid)
    assert first["has_more"]
    error("cursor_invalid", lambda: service.search_workspace("p1", "different", workspace_id=wid, cursor=first["next_cursor"]))
    error("cursor_invalid", lambda: service.search_workspace("p1", "match", workspace_id=wid, cursor="!invalid"))
    error("query_required", lambda: service.search_workspace("p1", "  ", workspace_id=wid))
    error("owner_unknown", lambda: service.search_workspace("p2", "match", workspace_id=wid, cursor=first["next_cursor"]))
    scene.root.rename(scene.root.with_name("old-root"))
    scene.root.mkdir()
    error("identity_changed", lambda: service.search_workspace("p1", "match", workspace_id=wid))


def test_search_read_error_is_not_complete_no_match(scene, monkeypatch):
    (scene.root / "unreadable").mkdir()
    original = service.Workspace.open
    def opened(self, path, **kwargs):
        if path == "unreadable":
            raise PermissionError("host-private-path")
        return original(self, path, **kwargs)
    monkeypatch.setattr(service.Workspace, "open", opened)
    error("permission_denied", lambda: service.search_workspace("p1", "missing", workspace_id=identity()))


def test_response_closes_fd_on_disconnect(scene):
    (scene.root / "file").write_text("test")
    download = service.download_workspace("p1", "file", workspace_id=identity())
    response = router._WorkspaceResponse(download, inline=False)
    async def send(message):
        raise OSError("disconnected")
    async def receive():
        return {"type": "http.disconnect"}
    with pytest.raises(Exception):
        asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
    with pytest.raises(OSError):
        os.fstat(download.fd)
