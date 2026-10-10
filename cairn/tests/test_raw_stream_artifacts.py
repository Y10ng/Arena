from __future__ import annotations

import asyncio
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.server import db
from cairn.server.app import app
from cairn.server.auth import create_machine_principal
from cairn.server.models import CreateArtifactRequest, CreateRawArtifactRequest
from cairn.server.routers.audit import create_raw_artifact
from cairn.server.services import audit_artifacts


@pytest.fixture
def configured_database(tmp_path: Path):
    db._reset_for_tests()
    database_path = tmp_path / "cairn.db"
    artifact_path = tmp_path / "artifacts"
    db.configure(database_path, artifact_path=artifact_path)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, scenario_key) "
            "VALUES ('p1', 'Raw stream', 'active', '2026-08-25T00:00:00Z', 'default')"
        )
        conn.execute(
            """
            INSERT INTO task_runs
                (id, project_id, task_type, worker_name, worker_type, status,
                 prompt_group, started_at, runtime_context_json)
            VALUES
                ('tr001', 'p1', 'explore', 'worker-1', 'claudecode', 'running',
                 'default', '2026-08-25T00:00:01Z', '{"source":"test"}')
            """
        )
    yield artifact_path
    db._reset_for_tests()


def test_text_artifact_preserves_exact_utf8_bytes(configured_database: Path) -> None:
    content = "  第一行\r\nsecond  \n"
    body = CreateArtifactRequest(
        task_run_id="tr001",
        kind="report",
        category="deliverable",
        filename="exact.txt",
        content=content,
        metadata={"source": "test"},
    )

    assert body.content == content
    artifact = audit_artifacts.create_artifact_record("p1", body)
    encoded = content.encode("utf-8")
    assert artifact.sha256 == sha256(encoded).hexdigest()
    assert artifact.size == len(encoded)
    assert (
        configured_database / "projects/p1/runs/tr001/a001-exact.txt"
    ).read_bytes() == encoded

    with pytest.raises(ValueError, match="must not be empty"):
        CreateArtifactRequest(kind="report", content=" \r\n ")


def _streaming_request(chunks: list[bytes]) -> Request:
    messages = iter(
        [
            {
                "type": "http.request",
                "body": chunk,
                "more_body": index < len(chunks) - 1,
            }
            for index, chunk in enumerate(chunks)
        ]
    )

    async def receive():
        return next(messages)

    return Request({"type": "http", "method": "POST", "headers": []}, receive)


def test_raw_route_streams_body_and_reuses_artifact_audit_semantics(
    configured_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = [b"first-", b"second", b"-third"]
    calls = {"fsync": 0, "replace": 0}
    real_fsync = audit_artifacts.os.fsync
    real_replace = audit_artifacts.os.replace

    def tracked_fsync(fd: int) -> None:
        calls["fsync"] += 1
        real_fsync(fd)

    def tracked_replace(source, destination) -> None:
        calls["replace"] += 1
        real_replace(source, destination)

    monkeypatch.setattr(audit_artifacts.os, "fsync", tracked_fsync)
    monkeypatch.setattr(audit_artifacts.os, "replace", tracked_replace)
    body = CreateRawArtifactRequest(
        task_run_id="tr001",
        kind="file",
        filename="result.bin",
        metadata_json=json.dumps({"source_path": "/work/result.bin"}),
    )

    artifact = asyncio.run(create_raw_artifact("p1", _streaming_request(chunks), body))

    content = b"".join(chunks)
    assert artifact.id == "a001"
    assert artifact.category == "deliverable"
    assert artifact.uri.endswith("/runs/tr001/a001-result.bin")
    assert artifact.sha256 == sha256(content).hexdigest()
    assert artifact.size == len(content)
    assert artifact.metadata["source_path"] == "/work/result.bin"
    assert artifact.metadata["runtime_context"]["task_run_id"] == "tr001"
    assert (configured_database / "projects/p1/runs/tr001/a001-result.bin").read_bytes() == content

    with db.get_conn() as conn:
        event = conn.execute(
            "SELECT kind, payload_json FROM agent_events WHERE project_id = 'p1'"
        ).fetchone()
    assert event["kind"] == "artifact_created"
    payload = json.loads(event["payload_json"])
    assert payload["artifact_id"] == "a001"
    assert payload["sha256"] == artifact.sha256
    assert payload["size"] == len(content)
    assert calls == {"fsync": 1, "replace": 1}
    assert not list(configured_database.rglob("*.tmp"))


@pytest.fixture
def raw_http_client(configured_database: Path):
    _, token = create_machine_principal("raw-artifact-dispatcher", "synthetic-raw-token")
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
        yield client


def _registered_metadata(content: bytes) -> dict[str, object]:
    return {
        "phase": "explore_execute",
        "source_path": "/work/evidence/原始响应.bin",
        "registered_path": "/work/.cairn/registered/evidence-v1.bin",
        "registered_handle": "evidence-v1",
        "registered_sha256": sha256(content).hexdigest(),
        "registered_size_bytes": len(content),
        "declared_by_worker": True,
    }


def test_raw_http_preserves_registered_content_and_source_identity(
    configured_database: Path,
    raw_http_client: TestClient,
) -> None:
    content = b"\x00\xff" + "  固定材料\r\n".encode("utf-8") + b"trailing  \n\x00"
    metadata = _registered_metadata(content)
    response = raw_http_client.post(
        "/projects/p1/artifacts/raw",
        params={
            "task_run_id": "tr001",
            "kind": "file",
            "filename": "fixed-evidence.bin",
            "metadata_json": json.dumps(metadata, ensure_ascii=False),
        },
        content=content,
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 201, response.text
    artifact = response.json()
    assert artifact["project_id"] == "p1"
    assert artifact["task_run_id"] == "tr001"
    assert artifact["sha256"] == sha256(content).hexdigest()
    assert artifact["size"] == len(content)
    assert artifact["category"] == "deliverable"
    assert artifact["uri"] == "artifact://projects/p1/runs/tr001/a001-fixed-evidence.bin"
    for key, value in metadata.items():
        assert artifact["metadata"][key] == value
    context = artifact["metadata"]["runtime_context"]
    assert context["project_id"] == "p1"
    assert context["task_run_id"] == "tr001"
    assert context["worker_name"] == "worker-1"
    assert context["worker_type"] == "claudecode"

    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM artifacts").fetchall()
    assert len(rows) == 1
    row = rows[0]
    for key in ("id", "project_id", "task_run_id", "sha256", "size", "uri"):
        assert row[key] == artifact[key]
    assert json.loads(row["metadata_json"]) == artifact["metadata"]
    disk_path = configured_database / "projects/p1/runs/tr001/a001-fixed-evidence.bin"
    persisted = disk_path.read_bytes()
    assert persisted == content
    assert sha256(persisted).hexdigest() == row["sha256"]
    assert disk_path.stat().st_size == row["size"]
    assert list((configured_database / ".uploads").iterdir()) == []
    assert [path for path in configured_database.rglob("*") if path.is_file()] == [disk_path]


@pytest.mark.parametrize("mismatched_field", ["registered_sha256", "registered_size_bytes"])
def test_raw_http_rejects_registered_content_mismatch_without_residue(
    configured_database: Path,
    raw_http_client: TestClient,
    mismatched_field: str,
) -> None:
    content = b"\x00fixed-registered-content\xff\r\n"
    metadata = _registered_metadata(content)
    metadata[mismatched_field] = (
        sha256(b"different-content").hexdigest()
        if mismatched_field == "registered_sha256"
        else len(content) + 1
    )
    response = raw_http_client.post(
        "/projects/p1/artifacts/raw",
        params={
            "task_run_id": "tr001",
            "kind": "file",
            "filename": "rejected-evidence.bin",
            "metadata_json": json.dumps(metadata, ensure_ascii=False),
        },
        content=content,
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 422, response.text
    assert response.json() == {"detail": "registered_content_version_mismatch"}
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM artifacts").fetchone()["count"] == 0
        assert conn.execute("SELECT COUNT(*) AS count FROM agent_events").fetchone()["count"] == 0
    # .uploads 已创建，确保验证经过 raw 接收路径，而非仅在请求解析阶段拒绝。
    assert list((configured_database / ".uploads").iterdir()) == []
    assert not [path for path in configured_database.rglob("*") if path.is_file()]


def test_raw_stream_failure_cleans_temporary_and_database_state(
    configured_database: Path,
) -> None:
    body = CreateRawArtifactRequest(kind="raw_output", metadata_json="{}")

    async def failed_chunks():
        yield b"partial"
        raise RuntimeError("stream interrupted")

    with pytest.raises(RuntimeError, match="stream interrupted"):
        asyncio.run(audit_artifacts.create_raw_artifact_record("p1", body, failed_chunks()))

    assert not [path for path in configured_database.rglob("*") if path.is_file()]
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM artifacts").fetchone()["count"] == 0
        assert conn.execute("SELECT COUNT(*) AS count FROM agent_events").fetchone()["count"] == 0


def test_raw_stream_allocates_artifact_id_only_after_upload_finishes(
    configured_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = CreateRawArtifactRequest(kind="file", filename="deferred.bin", metadata_json="{}")
    allocated = False
    real_next_artifact_id = audit_artifacts.next_artifact_id

    def tracked_next_artifact_id(conn, project_id: str) -> str:
        nonlocal allocated
        allocated = True
        return real_next_artifact_id(conn, project_id)

    monkeypatch.setattr(audit_artifacts, "next_artifact_id", tracked_next_artifact_id)

    async def chunks():
        assert allocated is False
        # 上传尚未完成时，另一个短事务仍应能写入 SQLite，而不是被 artifact counter 锁住。
        with db.get_conn() as conn:
            conn.execute("UPDATE projects SET title = 'upload in progress' WHERE id = 'p1'")
        yield b"first"
        assert allocated is False
        yield b"second"
        assert allocated is False

    artifact = asyncio.run(audit_artifacts.create_raw_artifact_record("p1", body, chunks()))

    assert allocated is True
    assert artifact.id == "a001"
    assert (configured_database / "projects/p1/runs/unscoped/a001-deferred.bin").read_bytes() == b"firstsecond"
    with db.get_conn() as conn:
        assert conn.execute("SELECT title FROM projects WHERE id = 'p1'").fetchone()["title"] == "upload in progress"


def test_raw_database_failure_cleans_replaced_file(
    configured_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = CreateRawArtifactRequest(kind="file", filename="orphan.bin", metadata_json="{}")

    def fail_insert(*_args, **_kwargs):
        raise RuntimeError("database write failed")

    monkeypatch.setattr(audit_artifacts, "_insert_artifact_record", fail_insert)

    async def chunks():
        yield b"complete-before-database-failure"

    with pytest.raises(RuntimeError, match="database write failed"):
        asyncio.run(audit_artifacts.create_raw_artifact_record("p1", body, chunks()))

    assert not [path for path in configured_database.rglob("*") if path.is_file()]
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM artifacts").fetchone()["count"] == 0


def test_dispatcher_sends_file_object_to_raw_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    client = CairnClient("http://server.test", token="test-token")
    content = BytesIO(b"streamed-content")
    captured: dict[str, object] = {}

    class Response:
        status_code = 201
        headers = {"content-type": "application/json"}
        text = '{"id":"a001"}'

        def json(self):
            return {"id": "a001"}

    class Session:
        def post(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            return Response()

    monkeypatch.setattr(client, "_session", lambda: Session())

    result = client.create_artifact(
        "p1",
        task_run_id="tr001",
        kind="file",
        content=content,
        filename="result.bin",
        metadata={"source": "worker"},
        category="deliverable",
    )

    assert result == ApiResult(status_code=201, data={"id": "a001"}, text=Response.text)
    assert captured["url"] == "http://server.test/projects/p1/artifacts/raw"
    assert captured["data"] is content
    assert captured["headers"] == {"Content-Type": "application/octet-stream"}
    assert captured["params"] == {
        "kind": "file",
        "filename": "result.bin",
        "metadata_json": '{"source":"worker"}',
        "task_run_id": "tr001",
        "category": "deliverable",
    }


def test_dispatcher_keeps_bytes_on_legacy_json_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    client = CairnClient("http://server.test", token="test-token")
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, **kwargs) -> ApiResult:
        captured.update(method=method, path=path, **kwargs)
        return ApiResult(status_code=201)

    monkeypatch.setattr(client, "_request_json", fake_request)

    client.create_artifact(
        "p1",
        task_run_id=None,
        kind="file",
        content=b"legacy",
        filename="legacy.bin",
        metadata={},
    )

    assert captured["path"] == "/projects/p1/artifacts"
    assert captured["json"]["content_base64"] == "bGVnYWN5"
