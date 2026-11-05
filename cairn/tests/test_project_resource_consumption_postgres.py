"""500 MB 资料流与项目消费投影；只连接一次性 PostgreSQL，不运行项目或模型。"""
from __future__ import annotations

import asyncio
import base64
from hashlib import file_digest, sha256
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import socket
from threading import Thread
import time
from uuid import uuid4
import zipfile

from fastapi import FastAPI, HTTPException, Response
import pytest
import requests
import uvicorn

from cairn.dispatcher.advisory_bundle import stage_advisory_bundle
from cairn.dispatcher.protocol.client import CairnClient
from cairn.server import db
from cairn.server.auth import RequestPrincipal, create_machine_principal, create_user_with_personal_tenant, login
from cairn.server.routers import project_resources as resource_routes, projects as project_routes
from cairn.server.services import project_resources as resources
from cairn.server.services.agent_attachments import upload_attachment
from cairn.server.services.consumption import FIELDS, project_consumption, register_binding, save_response
from cairn.server.services.project_records import get_project_detail, list_project_summaries
from test_postgres_bootstrap import empty_postgres_url, postgres_instance

pytestmark = pytest.mark.skipif(
    os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1",
    reason="仅显式授权的一次性 PostgreSQL",
)


@pytest.fixture
def configured(empty_postgres_url, tmp_path, monkeypatch):
    # 隔离默认额度测试，不读取或修改正式环境的材料及配额。
    for name in os.environ:
        if name.startswith(("CAIRN_PROJECT_RESOURCE_MAX_", "CAIRN_AGENT_ATTACHMENT_MAX_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("CAIRN_PROJECT_RESOURCE_ROOT", str(tmp_path / "resources"))
    monkeypatch.setenv("CAIRN_ADVISORY_ROOT", str(tmp_path / "advisory"))
    monkeypatch.delenv("CAIRN_ADVISORY_DOCKER_SOURCE", raising=False)
    db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")
    user, tenant = create_user_with_personal_tenant("resource-fixture", "isolated fixture password")
    principal = RequestPrincipal("user", user, tenant_id=tenant, role="user")
    with db.get_conn() as conn:
        for identifier in ("project", "zero", "unknown", "unobserved", "empty", "other"):
            conn.execute(
                "INSERT INTO projects(id,title,status,created_at,tenant_id,library_selection_explicit) "
                "VALUES (?,?,'stopped','2026-11-05T00:00:00Z',?,1)",
                (identifier, identifier, tenant),
            )
    return principal


def owner(principal):
    return dict(tenant_id=principal.tenant_id, user_id=principal.subject_id)


async def chunks(size, digest=None):
    block = bytes(range(256)) * 1024
    remaining = size
    while remaining:
        chunk = block[:min(remaining, len(block))]
        if digest is not None:
            digest.update(chunk)
        yield chunk
        remaining -= len(chunk)


def upload(batch, principal, size, *, path="input.bin", upload_id=None):
    return asyncio.run(resources.upload_project_resource_batch_file(
        batch.id, chunks(size), path=path, kind=None, upload_id=upload_id, **owner(principal),
    ))


@pytest.fixture
def local_http(configured):
    # 不加载正式 app lifespan，只有真实路由与认证；没有调度器或后台业务。
    app = FastAPI()
    app.include_router(resource_routes.router)
    app.include_router(project_routes.router)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(0.01)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


def test_real_500mb_receive_bind_http_download_materialize(configured, local_http, tmp_path, monkeypatch):
    size = 500_000_000
    assert shutil.disk_usage(tmp_path).free > 2_000_000_000
    limits = resources.project_resource_config().limits
    assert (limits.max_file_bytes, limits.max_zip_upload_bytes) == (size, size)
    assert (limits.max_active_total_bytes, limits.max_staged_bytes_per_user) == (2_000_000_000, 4_000_000_000)
    batch = resources.create_project_resource_batch(**owner(configured))
    digest = sha256()
    member = asyncio.run(resources.upload_project_resource_batch_file(
        batch.id, chunks(size, digest), path="binary/500MB.bin", kind=None,
        upload_id="prm_" + uuid4().hex, **owner(configured),
    ))
    assert member.size_bytes == size and member.sha256 == digest.hexdigest()
    staged = resources._batch_member_disk_path(batch.id, member.id)
    assert staged.stat().st_size == size
    assert staged.stat().st_blocks * 512 >= size  # 实际落盘，不用稀疏文件或伪造 size。

    received = 0
    async def oversized():
        nonlocal received
        async for chunk in chunks(size + 1):
            received += len(chunk)
            yield chunk
    with pytest.raises(HTTPException) as rejected:
        asyncio.run(resources.upload_project_resource_batch_file(
            batch.id, oversized(), path="too-large.bin", kind=None, **owner(configured),
        ))
    assert rejected.value.status_code == 413
    assert rejected.value.detail == {
        "code": "project_resource_file_too_large", "category": "file", "limit_bytes": size,
        "path": None, "message": "文件超过单文件大小限制",
    }
    assert received == size + 1
    assert list(staged.parent.glob(".upload-*")) == []
    assert resources.get_project_resource_batch(batch.id, **owner(configured)).members == [member]

    # 禁止大原件被重新组装成 bytes；HTTP JSON 元数据不在此限制内。
    read_bytes = Path.read_bytes
    content_property = requests.Response.content
    def bounded_read_bytes(path):
        assert path.stat().st_size < 1_000_000, "大原件不得 read_bytes"
        return read_bytes(path)
    def bounded_response_content(response):
        assert response.status_code != 200 or "/resources/" not in response.url, "原件下载不得 response.content"
        return content_property.fget(response)
    monkeypatch.setattr(Path, "read_bytes", bounded_read_bytes)
    monkeypatch.setattr(requests.Response, "content", property(bounded_response_content))
    path_open = Path.open
    read_sizes = []
    class BoundedReader:
        def __init__(self, stream):
            self.stream = stream
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def read(self, length=-1):
            assert 0 <= length <= 256 * 1024
            read_sizes.append(length)
            return self.stream.read(length)
        def readinto(self, buffer):
            assert len(buffer) <= 256 * 1024
            read_sizes.append(len(buffer))
            return self.stream.readinto(buffer)
    def bounded_open(path, mode="r", *args, **kwargs):
        stream = path_open(path, mode, *args, **kwargs)
        return BoundedReader(stream) if mode == "rb" and path.stat().st_size >= size else stream
    monkeypatch.setattr(Path, "open", bounded_open)

    bound = resources.commit_project_resource_batch("project", batch.id, created_by=configured.subject_id, **owner(configured))
    assert len(bound) == 1 and bound[0].sha256 == member.sha256
    assert not staged.exists()
    assert resources.get_project_resource_batch(batch.id, **owner(configured)).state == "consumed"
    raw = resources.read_project_resource_content("project", bound[0].id)
    assert raw.disk_path.stat().st_size == size and raw.path == member.path
    route_response = resource_routes.read_project_resource("project", bound[0].id)
    assert route_response.path == raw.disk_path and route_response.chunk_size <= 256 * 1024
    _, token = create_machine_principal("stream-fixture")
    client = CairnClient(local_http, timeout=30, token=token)
    try:
        url = f"{local_http}/projects/project/resources/{bound[0].id}"
        with requests.get(url, headers={"Authorization": "Bearer " + token}, stream=True, timeout=30) as response:
            assert response.status_code == 200
            assert response.headers["Content-Length"] == str(size)
            assert response.headers["Content-Type"] == "application/octet-stream"
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert "500MB.bin" in response.headers["Content-Disposition"]
            wire_digest, wire_size = sha256(), 0
            for chunk in response.iter_content(256 * 1024):
                wire_digest.update(chunk)
                wire_size += len(chunk)
            assert wire_size == size and wire_digest.hexdigest() == member.sha256
        bundle = stage_advisory_bundle(client, "project", None)
        assert bundle is not None and bundle.resource_count == 1
        materialized = Path(bundle.source_path) / "project" / member.path
        assert materialized.stat().st_size == size and materialized.stat().st_mode & 0o222 == 0
        with materialized.open("rb") as stream:
            assert file_digest(stream, "sha256").hexdigest() == member.sha256
        assert bundle.manifest["project_resources"][0]["sha256"] == member.sha256
        assert read_sizes and max(read_sizes) <= 256 * 1024
        monkeypatch.setattr(client, "download_project_resource", lambda *_: pytest.fail("缓存不应重复下载"))
        assert stage_advisory_bundle(client, "project", None).digest == bundle.digest
        assert not list((tmp_path / "advisory").rglob(".staging-*"))
        assert requests.get(url, timeout=10).status_code in (401, 403)
        create_user_with_personal_tenant("other-fixture", "other isolated password")
        cookie_response = Response()
        login(cookie_response, "other-fixture", "other isolated password")
        cookie = cookie_response.headers["set-cookie"].split(";", 1)[0]
        assert requests.get(url, headers={"Cookie": cookie}, timeout=10).status_code == 404
    finally:
        client.close()


def test_head_resource_and_frozen_library_permissions_integrity(configured, local_http, tmp_path, monkeypatch):
    from cairn.server.schemas.libraries import LibraryFileWrite, LibrarySelection
    from cairn.server.services import libraries

    monkeypatch.setenv("CAIRN_ADVISORY_LIBRARY_ROOT", str(tmp_path / "library-content"))
    batch = resources.create_project_resource_batch(**owner(configured))
    upload(batch, configured, 11, path="nested/input.bin")
    resource = resources.commit_project_resource_batch(
        "project", batch.id, created_by=configured.subject_id, **owner(configured),
    )[0]
    raw = resources.read_project_resource_content("project", resource.id)
    with db.get_conn() as conn:
        libraries.create_library(conn, "head-fixture", "隔离 HEAD 资料库")
        manifest = libraries.replace_library_files(conn, "head-fixture", [
            LibraryFileWrite(path="notes/frozen.txt", content="冻结原文\n"),
            LibraryFileWrite(path="unselected.txt", content="未选择"),
        ])
        selected = next(file for file in manifest.files if file.path == "notes/frozen.txt")
        revision = libraries.resolve_library_selections(conn, [LibrarySelection(
            library_key="head-fixture", source="current", paths=[selected.path],
            expected_files={selected.path: selected.sha256},
        )], tenant_id=configured.tenant_id, user_id=configured.subject_id)[0]
        libraries.bind_library_revision(conn, "project", revision)
        libraries.replace_library_file(conn, "head-fixture", selected.path, "current 已变化且长度不同")
        stored = conn.execute(
            "SELECT storage_key FROM advisory_library_files WHERE library_key=? AND version=? AND path=?",
            (revision.library_key, revision.version, selected.path),
        ).fetchone()
    library_path = libraries.library_storage_path(stored["storage_key"])
    cookie_response = Response()
    login(cookie_response, "resource-fixture", "isolated fixture password")
    headers = {"Cookie": cookie_response.headers["set-cookie"].split(";", 1)[0]}
    create_user_with_personal_tenant("head-other", "isolated other password")
    other_response = Response()
    login(other_response, "head-other", "isolated other password")
    other_headers = {"Cookie": other_response.headers["set-cookie"].split(";", 1)[0]}
    resource_url = f"{local_http}/projects/project/resources/{resource.id}"
    library_base = f"{local_http}/projects/project/advisory-libraries/head-fixture/versions/{revision.version}/files"
    library_url = f"{library_base}/{selected.path}"

    for url, expected, filename in (
        (resource_url, bytes(range(11)), "input.bin"),
        (library_url, "冻结原文\n".encode(), "frozen.txt"),
    ):
        head = requests.head(url, headers=headers, timeout=10)
        get = requests.get(url, headers=headers, timeout=10)
        assert head.status_code == get.status_code == 200
        assert head.content == b"" and get.content == expected
        for name in ("Content-Length", "Content-Type", "Content-Disposition", "Cache-Control", "X-Content-Type-Options"):
            assert head.headers[name] == get.headers[name]
        assert head.headers["Content-Length"] == str(len(expected))
        assert head.headers["Content-Type"] == "application/octet-stream"
        assert head.headers["Content-Disposition"].startswith("attachment;")
        assert filename in head.headers["Content-Disposition"]
        assert head.headers["Cache-Control"] == "no-store"
        assert head.headers["X-Content-Type-Options"] == "nosniff"
        anonymous = requests.head(url, timeout=10)
        foreign = requests.head(url, headers=other_headers, timeout=10)
        assert anonymous.status_code == 401 and anonymous.content == b""
        assert foreign.status_code == 404 and foreign.content == b""

    for url in (
        resource_url.rsplit("/", 1)[0] + "/missing",
        library_base + "/missing.txt", library_base + "/unselected.txt",
        library_url.replace(f"/versions/{revision.version}/", f"/versions/{revision.version + 1}/"),
    ):
        missing = requests.head(url, headers=headers, timeout=10)
        assert missing.status_code == 404 and missing.content == b""
    # HEAD 必须执行与 GET 一样的原件完整性校验，不能只信数据库元数据。
    for path, url, damaged_status, missing_status in (
        (raw.disk_path, resource_url, 409, 404),
        (library_path, library_url, 500, 500),
    ):
        original = path.read_bytes()
        path.write_bytes(b"X" + original[1:])
        damaged = requests.head(url, headers=headers, timeout=10)
        assert damaged.status_code == damaged_status and damaged.content == b""
        assert requests.get(url, headers=headers, timeout=10).status_code == damaged_status
        path.unlink()
        missing = requests.head(url, headers=headers, timeout=10)
        assert missing.status_code == missing_status and missing.content == b""
        assert requests.get(url, headers=headers, timeout=10).status_code == missing_status


def test_overview_project_consumption_matches_summary_and_detail(configured, monkeypatch):
    from cairn.server.services import overview

    with db.get_conn() as conn:
        for project in ("project", "zero", "unknown", "unobserved"):
            conn.execute("INSERT INTO model_consumption_bindings(id,tenant_id,owner_id,source,project_id,model_json) "
                         "VALUES (?,?,'fixture','explore',?,'{}')", (project, configured.tenant_id, project))
        usage = dict(zip(FIELDS, (17, 40, 30, 43)))
        for identifier, values in (("project", usage), ("zero", dict.fromkeys(FIELDS, 0)), ("unknown", {})):
            save_response(conn, identifier, dict(response_seq=0, revision=1, measurement="raw", usage=values))
        conn.execute("INSERT INTO projects(id,title,status,created_at,tenant_id) "
                     "VALUES ('foreign','foreign','active','2026-11-05T00:00:00Z','tenant_legacy')")
        conn.execute("INSERT INTO model_consumption_bindings(id,tenant_id,owner_id,source,project_id,model_json) "
                     "VALUES ('foreign','tenant_legacy','fixture','explore','project','{}')")
        save_response(conn, "foreign", dict(response_seq=0, revision=1, measurement="raw", usage=dict.fromkeys(FIELDS, 999)))

    calls, aggregate_queries = [], []
    project_projection = overview.project_consumption
    execute = db.PostgresConnection.execute
    def projected(conn, projects):
        calls.append(projects)
        return project_projection(conn, projects)
    def counted(conn, query, *args, **kwargs):
        if "FROM model_consumption" in query and "GROUP BY b.project_id" in query:
            aggregate_queries.append(query)
        return execute(conn, query, *args, **kwargs)
    monkeypatch.setattr(overview, "project_consumption", projected)
    monkeypatch.setattr(db.PostgresConnection, "execute", counted)
    with db.get_conn() as conn:
        result = overview.get_overview(conn, configured)
    expected_ids = {"project", "zero", "unknown", "unobserved", "empty", "other"}
    assert len(result.project_previews) == 6
    assert {item.id for item in result.project_previews} == expected_ids
    assert calls == [dict.fromkeys(expected_ids, configured.tenant_id)]
    assert len(aggregate_queries) == 2
    summaries = {item.id: item.consumption for item in list_project_summaries(tenant_id=configured.tenant_id)}
    for item in result.project_previews:
        assert item.consumption == summaries[item.id]
        assert item.consumption == get_project_detail(item.id, tenant_id=configured.tenant_id).consumption
    main = next(item for item in result.project_previews if item.id == "project")
    assert main.consumption.tokens == 130 and main.consumption.usage.model_dump() == usage


def test_duplicate_upload_identity_never_overwrites_and_failures_leave_batch_open(configured, monkeypatch):
    batch = resources.create_project_resource_batch(**owner(configured))
    identity = "prm_" + uuid4().hex
    first = upload(batch, configured, 3, upload_id=identity)
    path = resources._batch_member_disk_path(batch.id, identity)
    for target in ("input.bin", "different.bin"):
        with pytest.raises(HTTPException) as duplicate:
            upload(batch, configured, 7, path=target, upload_id=identity)
        assert duplicate.value.status_code == 409
        assert path.read_bytes() == bytes(range(3))
    other_batch = resources.create_project_resource_batch(**owner(configured))
    with pytest.raises(HTTPException, match="Upload identity"):
        upload(other_batch, configured, 7, upload_id=identity)
    assert not resources._batch_member_disk_path(other_batch.id, identity).exists()
    async def interrupted():
        yield b"unfinished"
        raise ConnectionError("isolated disconnect")
    with pytest.raises(ConnectionError):
        asyncio.run(resources.upload_project_resource_batch_file(
            batch.id, interrupted(), path="interrupted", kind=None, **owner(configured),
        ))
    assert not list(path.parent.glob(".upload-*"))
    assert resources.get_project_resource_batch(batch.id, **owner(configured)).members == [first]
    with monkeypatch.context() as patch:
        patch.setenv("CAIRN_PROJECT_RESOURCE_MAX_ACTIVE_TOTAL_BYTES", "2")
        with pytest.raises(HTTPException, match="total limit"):
            resources.commit_project_resource_batch("project", batch.id, created_by=configured.subject_id, **owner(configured))
    assert resources.get_project_resource_batch(batch.id, **owner(configured)).state == "open"
    assert resources.list_project_resource_records("project") == []
    assert path.read_bytes() == bytes(range(3))


def test_zip_categories_and_total_staged_quotas_keep_originals(configured, monkeypatch):
    batch = resources.create_project_resource_batch(**owner(configured))
    upload(batch, configured, 3)
    for setting, message in (("MAX_ACTIVE_TOTAL_BYTES", "total limit"), ("MAX_STAGED_BYTES_PER_USER", "staged")):
        with monkeypatch.context() as patch:
            patch.setenv("CAIRN_PROJECT_RESOURCE_" + setting, "4")
            with pytest.raises(HTTPException, match=message) as error:
                upload(batch, configured, 2, path="second.bin")
            assert error.value.status_code == 413 and isinstance(error.value.detail, str)
    def archive(path, content):
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
            zipped.writestr(path, content)
        return buffer.getvalue()
    async def payload(data):
        yield data
    for data, settings, category in (
        (archive("entry", b"12345"), {"MAX_FILE_BYTES": "4"}, "zip_entry"),
        (archive("entry", b"123"), {"MAX_ZIP_UPLOAD_BYTES": "4"}, "zip"),
        (archive("../escape", b"x"), {}, None),
        (archive("bomb", b"x" * 100_000), {}, None),
    ):
        with monkeypatch.context() as patch:
            for name, value in settings.items():
                patch.setenv("CAIRN_PROJECT_RESOURCE_" + name, value)
            with pytest.raises(HTTPException) as error:
                asyncio.run(resources.import_project_resource_batch_zip(batch.id, payload(data), **owner(configured)))
            if category:
                assert error.value.status_code == 413 and error.value.detail["category"] == category
            else:
                assert error.value.status_code in (400, 413) and isinstance(error.value.detail, str)
    data = archive("nested/allowed.bin", b"\x00\xff")
    imported = asyncio.run(resources.import_project_resource_batch_zip(batch.id, payload(data), **owner(configured)))
    assert len(imported.members) == 2
    assert not list(resources._batch_directory(batch.id).glob(".*"))
    ordinary = resources.create_project_resource_batch(**owner(configured))
    member = asyncio.run(resources.upload_project_resource_batch_file(
        ordinary.id, payload(data), path="opaque.zip", kind=None, **owner(configured),
    ))
    assert member.size_bytes == len(data) and member.sha256 == sha256(data).hexdigest()
    assert resources.get_project_resource_batch(ordinary.id, **owner(configured)).resource_count == 1


def test_chat_attachment_default_limit_stays_independent(configured, monkeypatch, tmp_path):
    from cairn.server.schemas.models import CreateTenantModelRequest
    from cairn.server.services.tenant_models import create_tenant_model

    keyring = tmp_path / "keyring.json"
    keyring.write_text(json.dumps({"active_version": "fixture", "keys": {
        "fixture": base64.b64encode(b"k" * 32).decode("ascii"),
    }}))
    keyring.chmod(0o600)
    monkeypatch.setenv("CAIRN_MODEL_CREDENTIAL_KEYRING_FILE", str(keyring))
    # 仅保存合成绑定供附件准出检查，不向此端点发请求。
    model = create_tenant_model(CreateTenantModelRequest(
        display_name="隔离模型", protocol="openai-chat-completions", base_url="http://127.0.0.1:1",
        model_name="fixture", api_key="synthetic-attachment-key",
    ), principal=configured)
    with db.get_conn() as conn:
        conn.execute("INSERT INTO agent_conversations(id,tenant_id,created_by,model_id,session_id,created_at) VALUES ('chat',?,?,?,'fixture','2026-11-05T00:00:00Z')",
                     (configured.tenant_id, configured.subject_id, model.id))
    monkeypatch.setenv("CAIRN_PROJECT_RESOURCE_MAX_FILE_BYTES", "500000000")
    attachment = asyncio.run(upload_attachment("chat", "accepted.bin", chunks(10 * 1024 * 1024), configured))
    assert attachment["size"] == 10 * 1024 * 1024
    with pytest.raises(HTTPException) as error:
        asyncio.run(upload_attachment("chat", "rejected.bin", chunks(10 * 1024 * 1024 + 1), configured))
    assert error.value.status_code == 413 and "10485760" in error.value.detail
    with db.get_conn() as conn:
        assert conn.execute("SELECT count(*) AS n FROM agent_attachments").fetchone()["n"] == 1
        # 合成占用记录验证旧 100 MiB 总额，不再落盘九份相同附件。
        conn.execute("UPDATE artifacts SET size=? WHERE conversation_id='chat'", (100 * 1024 * 1024,))
    with pytest.raises(HTTPException, match="数量或总大小"):
        asyncio.run(upload_attachment("chat", "over-total.bin", chunks(1), configured))


def test_project_consumption_scope_revisions_unknowns_and_two_queries(configured, monkeypatch):
    machine_id, _ = create_machine_principal("consumption-fixture")
    machine = RequestPrincipal("machine", machine_id)
    snapshot = {"model_id": "fixture", "model_revision": 1, "model_name": "fixture", "protocol": "openai-responses",
                "pricing": {"currency": "USD", "input": "1", "output": "2", "cache_read": "3", "cache_write": "4"}}
    with db.get_conn() as conn:
        conn.execute("INSERT INTO model_consumption_rates(tenant_id,model_id,revision,model_json) VALUES (?,'fixture',1,?)",
                     (configured.tenant_id, json.dumps(snapshot)))
        for project in ("project", "zero", "unknown", "unobserved", "other"):
            conn.execute("INSERT INTO task_runs(id,project_id,task_type,worker_name,worker_type,status,runtime_context_json) "
                         "VALUES ('same-run',?,'explore','fixture','pi','succeeded',?)",
                         (project, json.dumps({"dispatcher_id": machine_id, "execution_binding": snapshot})))
            register_binding(conn, project, principal=machine, task_run_id="same-run", project_id=project)
        event = dict(response_seq=0, revision=1, measurement="raw", usage=dict(zip(FIELDS, (10, 20, 30, 40))))
        save_response(conn, "project", event)
        updated = {**event, "revision": 2, "usage": {**event["usage"], "output_tokens": 40}}
        save_response(conn, "project", updated)
        save_response(conn, "project", updated)
        save_response(conn, "project", event)
        save_response(conn, "project", dict(response_seq=1, revision=1, measurement="raw", missing_responses=1, usage={"input_tokens": 7}))
        save_response(conn, "project", dict(response_seq=2, revision=1, measurement="sdk_normalized", usage=dict.fromkeys(FIELDS, 0)))
        save_response(conn, "project", dict(response_seq=3, revision=1, measurement="historical_summary", usage={"cache_write_tokens": 3}))
        save_response(conn, "zero", {**event, "usage": dict.fromkeys(FIELDS, 0)})
        save_response(conn, "unknown", {**event, "measurement": "started", "usage": {}})
        save_response(conn, "other", {**event, "usage": dict.fromkeys(FIELDS, 999)})
        # 旧聊天上下文提及项目，不等于消费事实绑定项目；错租户绑定同样不计入。
        conn.execute("INSERT INTO agent_conversations(id,tenant_id,created_by,project_id,session_id,created_at) VALUES ('context-chat',?,?,'project','fixture','2026-11-05T00:00:00Z')",
                     (configured.tenant_id, configured.subject_id))
        for identifier, tenant, project, conversation in (
            ("context-only", configured.tenant_id, None, "context-chat"),
            ("wrong-tenant", "tenant_legacy", "project", None),
        ):
            conn.execute("INSERT INTO model_consumption_bindings(id,tenant_id,owner_id,source,project_id,conversation_id,model_json) VALUES (?,?,'fixture','agent_chat',?,?,?)",
                         (identifier, tenant, project, conversation, json.dumps(snapshot)))
            save_response(conn, identifier, {**event, "usage": dict.fromkeys(FIELDS, 9999)})
        conn.execute("INSERT INTO model_consumption_bindings(id,tenant_id,owner_id,source,project_id,model_json) VALUES ('wrong-unobserved','tenant_legacy','fixture','explore','project','{}')")

    queries = []
    execute = db.PostgresConnection.execute
    def counted(conn, query, *args, **kwargs):
        if "FROM model_consumption" in query:
            queries.append(query)
        return execute(conn, query, *args, **kwargs)
    monkeypatch.setattr(db.PostgresConnection, "execute", counted)
    scope = dict.fromkeys(("project", "zero", "unknown", "unobserved", "empty"), configured.tenant_id)
    with db.get_conn() as conn:
        assert project_consumption(conn, {}) == {} and not queries
        actual = project_consumption(conn, scope)
        assert len(queries) == 2 and all("GROUP BY" in query for query in queries)
        queries.clear()
        assert project_consumption(conn, {"project": "unrelated-tenant"})["project"]["responses"] == 0
        assert len(queries) == 2
    main = actual["project"]
    assert main["usage"] == dict(zip(FIELDS, (17, 40, 30, 43)))
    assert (main["tokens"], main["responses"], main["partial_responses"], main["unobserved_bindings"]) == (130, 4, 3, 0)
    assert actual["zero"]["usage"] == dict.fromkeys(FIELDS, 0)
    assert (actual["zero"]["tokens"], actual["zero"]["responses"], actual["zero"]["partial_responses"]) == (0, 1, 0)
    assert actual["unknown"]["usage"] == dict.fromkeys(FIELDS, None)
    assert (actual["unknown"]["responses"], actual["unknown"]["partial_responses"], actual["unknown"]["unpriced_responses"]) == (1, 1, 1)
    for project in ("unobserved", "empty"):
        assert actual[project]["responses"] == 0 and actual[project]["usage"] == dict.fromkeys(FIELDS, None)
    assert actual["unobserved"]["unobserved_bindings"] == 1 and actual["empty"]["unobserved_bindings"] == 0
    queries.clear()
    summaries = list_project_summaries(tenant_id=configured.tenant_id)
    assert len(queries) == 2
    for summary in summaries:
        if summary.id in actual:
            assert summary.consumption.model_dump() == actual[summary.id]
    queries.clear()
    assert get_project_detail("project", tenant_id=configured.tenant_id).consumption.model_dump() == main
    assert len(queries) == 2
