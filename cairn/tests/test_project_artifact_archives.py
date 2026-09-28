from __future__ import annotations

import asyncio
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import zipfile

import pytest
from fastapi import HTTPException

from cairn.server import db
from cairn.server.routers import audit
from cairn.server.services import audit_artifacts, project_artifact_archives


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(db.SCHEMA)
    db._ensure_schema_migrations(conn)
    conn.execute(
        """
        INSERT INTO projects (id, title, status, created_at, scenario_key)
        VALUES ('proj-1', 'Archive Test', 'active', '2026-08-27T00:00:00Z', 'default-copy-v2')
        """
    )
    return conn


def _configure_connection(monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection) -> None:
    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(project_artifact_archives, "get_conn", fake_get_conn)


def _insert_fact(
    conn: sqlite3.Connection,
    fact_id: str,
    description: str,
    *,
    task_run_id: str | None = None,
    created_at: str = "2026-08-27T00:01:00Z",
) -> None:
    conn.execute(
        """
        INSERT INTO facts (id, project_id, description, task_run_id, creator, created_at)
        VALUES (?, 'proj-1', ?, ?, 'worker', ?)
        """,
        (fact_id, description, task_run_id, created_at),
    )


def _insert_artifact(
    conn: sqlite3.Connection,
    artifact_id: str,
    filename: str,
    content: bytes,
    *,
    task_run_id: str | None = None,
    kind: str = "file",
    category: str = "evidence",
    created_at: str = "2026-08-27T00:02:00Z",
) -> None:
    run_component = task_run_id or "unscoped"
    conn.execute(
        """
        INSERT INTO artifacts (
            id, project_id, task_run_id, kind, category, uri, sha256, size,
            created_at, metadata_json
        ) VALUES (?, 'proj-1', ?, ?, ?, ?, ?, ?, ?, '{}')
        """,
        (
            artifact_id,
            task_run_id,
            kind,
            category,
            f"artifact://projects/proj-1/runs/{run_component}/{filename}",
            sha256(content).hexdigest(),
            len(content),
            created_at,
        ),
    )


def _configure_content_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    records: dict[str, tuple[str, bytes, str]],
) -> list[str]:
    files: dict[str, audit_artifacts.ArtifactContentFile] = {}
    calls: list[str] = []
    for artifact_id, (filename, content, media_type) in records.items():
        disk_path = tmp_path / artifact_id / filename
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        disk_path.write_bytes(content)
        files[artifact_id] = audit_artifacts.ArtifactContentFile(
            disk_path=disk_path,
            media_type=media_type,
            filename=filename,
            content_disposition_type="attachment",
        )

    def content_file(project_id: str, artifact_id: str):
        calls.append(artifact_id)
        return files[artifact_id]

    monkeypatch.setattr(project_artifact_archives, "artifact_content_file", content_file)
    return calls


def _seed_relationship_graph(conn: sqlite3.Connection) -> dict[str, tuple[str, bytes, str]]:
    _insert_fact(conn, "fact-canonical", "canonical finding with request and response")
    _insert_fact(conn, "fact-observation", "independent observation")
    _insert_fact(conn, "fact-verification", "verification reproduced the issue")
    conn.execute(
        """
        INSERT INTO task_runs (
            id, project_id, task_type, worker_name, worker_type, status,
            session_id, started_at, ended_at
        ) VALUES (
            'run-discovery', 'proj-1', 'explore', 'discoverer', 'claudecode',
            'succeeded', 'session-discovery',
            '2026-08-27T00:01:00Z', '2026-08-27T00:04:00Z'
        )
        """
    )

    records = {
        "a-packet": ("request-response.txt", b"packet", "text/plain"),
        "a-shared": ("capture.pcap", b"pcap", "application/vnd.tcpdump.pcap"),
        "a-fact": ("worker.log", b"fact evidence", "text/plain"),
        "a-verify": ("verification.json", b'{"confirmed":true}', "application/json"),
        "a-image": ("SCREENSHOT.PNG", b"missing image bytes", "image/png"),
        "a-script": ("payload.bin", b"console.log(1)", "application/javascript"),
        "a-html": ("response.XHTML", b"<html></html>", "application/xhtml+xml"),
        "a-unlinked": ("same-run.txt", b"not evidence", "text/plain"),
    }
    for index, (artifact_id, (filename, content, _)) in enumerate(records.items(), start=1):
        _insert_artifact(
            conn,
            artifact_id,
            filename,
            content,
            task_run_id="run-discovery" if artifact_id == "a-unlinked" else None,
            kind="screenshot" if artifact_id == "a-image" else "raw_output",
            category="deliverable" if artifact_id == "a-unlinked" else "evidence",
            created_at=f"2026-08-27T00:02:{index:02d}Z",
        )

    conn.execute(
        """
        INSERT INTO vulnerabilities (
            id, project_id, title, severity, vuln_type, target, confidence, status,
            source_fact_id, packet_artifact_id, remediation, repro_request,
            repro_response, repro_steps, discovered_by_run_id,
            discovered_by_worker, slot_key, fingerprint, identity_json,
            created_at, updated_at
        ) VALUES (
            'vuln-1', 'proj-1', '越权 / ../ 管理接口', 'high', 'idor', '/admin', 0.91,
            'confirmed', 'fact-canonical', 'a-packet', '校验对象权限',
            'GET /admin HTTP/1.1', 'HTTP/1.1 200 OK', '1. 使用低权限账号请求',
            'run-discovery', 'discoverer', 'slot-authz', 'fingerprint-1',
            '{"asset":{"host":"example.test"}}',
            '2026-08-27T00:03:00Z', '2026-08-27T00:04:00Z'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO vulnerabilities (
            id, project_id, title, severity, source_fact_id, packet_artifact_id,
            created_at, updated_at
        ) VALUES (
            'vuln-2', 'proj-1', '共享证据漏洞', 'medium', NULL, 'a-shared',
            '2026-08-27T00:05:00Z', '2026-08-27T00:05:00Z'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO vulnerabilities (
            id, project_id, title, severity, remediation, repro_request, repro_response,
            repro_steps, created_at, updated_at
        ) VALUES (
            'vuln-3', 'proj-1', '仅文字证据', 'low', '升级组件',
            'POST /check', 'HTTP/1.1 500', '1. 提交请求',
            '2026-08-27T00:06:00Z', '2026-08-27T00:06:00Z'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO vulnerability_observations (
            id, project_id, vulnerability_id, source_fact_id, task_run_id,
            worker, slot_key, packet_artifact_id, identity_json, severity,
            confidence, repro_request, repro_response, observed_at
        ) VALUES (
            'observation-1', 'proj-1', 'vuln-1', 'fact-observation',
            'run-discovery', 'observer', 'slot-authz', 'a-shared',
            '{"vector":{"parameter":"id"}}', 'high', 0.88,
            'GET /admin?id=2', 'HTTP/1.1 200 OK', '2026-08-27T00:03:30Z'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO fact_evidence (
            project_id, fact_id, artifact_id, pointer, verifier_run_id, created_at
        ) VALUES
            ('proj-1', 'fact-canonical', 'a-fact', '/response', NULL, '2026-08-27T00:03:00Z'),
            ('proj-1', 'fact-canonical', 'a-image', '', NULL, '2026-08-27T00:03:01Z'),
            ('proj-1', 'fact-canonical', 'a-script', '', NULL, '2026-08-27T00:03:02Z'),
            ('proj-1', 'fact-canonical', 'a-html', '', NULL, '2026-08-27T00:03:03Z'),
            ('proj-1', 'fact-observation', 'a-fact', '/request', NULL, '2026-08-27T00:03:04Z'),
            ('proj-1', 'fact-verification', 'a-verify', '/fact', 'run-verify', '2026-08-27T00:04:00Z')
        """
    )
    conn.execute(
        """
        INSERT INTO intents (
            id, project_id, description, creator, created_at,
            assessment_role, target_observation_id, verifier_schedule_key
        ) VALUES (
            'intent-verify', 'proj-1', 'verify', 'system', '2026-08-27T00:03:30Z',
            'verifier', 'observation-1', 'observation-1'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO task_runs (
            id, project_id, intent_id, task_type, worker_name, worker_type,
            status, session_id, started_at, ended_at
        ) VALUES (
            'run-verify', 'proj-1', 'intent-verify', 'verify', 'verifier',
            'claudecode', 'succeeded', 'session-verify',
            '2026-08-27T00:03:30Z', '2026-08-27T00:04:00Z'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO vulnerability_verifications (
            id, project_id, vulnerability_id, observation_id, intent_id,
            fact_id, verifier_task_run_id, verifier_worker,
            discovery_task_run_id, discovery_worker, independence_level,
            outcome, evidence_json, notes, created_at
        ) VALUES (
            'verification-1', 'proj-1', 'vuln-1', 'observation-1',
            'intent-verify', 'fact-verification', 'run-verify', 'verifier',
            'run-discovery', 'discoverer', 'different_worker', 'confirmed', ?,
            'independently reproduced', '2026-08-27T00:04:00Z'
        )
        """,
        (
            json.dumps(
                [
                    {
                        "artifact_id": "a-verify",
                        "pointer": "/response",
                        "verifier_run_id": "run-verify",
                    },
                    {"ignored": "invalid reference"},
                ]
            ),
        ),
    )
    conn.commit()
    return records


def test_archive_groups_explicit_evidence_and_generates_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    conn = _make_conn()
    try:
        records = _seed_relationship_graph(conn)
        _configure_connection(monkeypatch, conn)
        calls = _configure_content_files(
            monkeypatch,
            tmp_path,
            {key: value for key, value in records.items() if key not in {"a-image", "a-html"}},
        )

        result = project_artifact_archives.create_project_vulnerability_evidence_archive("proj-1")
        try:
            with zipfile.ZipFile(result.disk_path) as archive:
                names = archive.namelist()
                first_directory = next(
                    name.rsplit("/", 1)[0]
                    for name in names
                    if name.endswith("--vuln-1/vulnerability.md")
                )
                second_directory = next(
                    name.rsplit("/", 1)[0]
                    for name in names
                    if name.endswith("--vuln-2/vulnerability.md")
                )
                third_directory = next(
                    name.rsplit("/", 1)[0]
                    for name in names
                    if name.endswith("--vuln-3/vulnerability.md")
                )

                assert names[0] == "README.txt"
                assert ".." not in first_directory
                assert archive.read(f"{first_directory}/artifacts/a-packet/request-response.txt") == b"packet"
                assert archive.read(f"{first_directory}/artifacts/a-shared/capture.pcap") == b"pcap"
                assert archive.read(f"{first_directory}/artifacts/a-fact/worker.log") == b"fact evidence"
                assert archive.read(f"{first_directory}/artifacts/a-verify/verification.json") == b'{"confirmed":true}'
                assert archive.read(f"{second_directory}/artifacts/a-shared/capture.pcap") == b"pcap"
                assert not any("a-unlinked" in name for name in names)
                assert not any("a-image" in name and "/artifacts/" in name for name in names)
                assert not any("a-script" in name and "/artifacts/" in name for name in names)
                assert not any("a-html" in name and "/artifacts/" in name for name in names)
                assert not any(name.startswith(f"{third_directory}/artifacts/") for name in names)

                vulnerability_text = archive.read(
                    f"{first_directory}/vulnerability.md"
                ).decode("utf-8")
                observations_text = archive.read(
                    f"{first_directory}/observations.md"
                ).decode("utf-8")
                verifications_text = archive.read(
                    f"{first_directory}/verifications.md"
                ).decode("utf-8")
                index_text = archive.read(f"{first_directory}/evidence-index.md").decode("utf-8")
                text_only = archive.read(f"{third_directory}/vulnerability.md").decode("utf-8")

                assert "GET /admin HTTP/1.1" in vulnerability_text
                assert "canonical finding with request and response" in vulnerability_text
                assert "GET /admin?id=2" in observations_text
                assert "independently reproduced" in verifications_text
                assert "a-verify" in verifications_text
                assert index_text.count("### a-fact") == 1
                assert "canonical fact evidence" in index_text
                assert "observation observation-1 fact evidence" in index_text
                assert "SCREENSHOT.PNG" in index_text
                assert "图片证据已排除" in index_text
                assert "payload.bin" in index_text
                assert "JavaScript MIME" in index_text
                assert "response.XHTML" in index_text
                assert "HTML 扩展名" in index_text
                assert "POST /check" in text_only
                assert "HTTP/1.1 500" in text_only
        finally:
            result.cleanup()

        assert not result.temporary_directory.exists()
        assert "a-image" not in calls
        assert "a-html" not in calls
        assert "a-unlinked" not in calls
        assert calls.count("a-fact") == 1
        assert calls.count("a-shared") == 2
    finally:
        conn.close()


def test_archive_without_vulnerabilities_contains_readme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _make_conn()
    try:
        _configure_connection(monkeypatch, conn)
        result = project_artifact_archives.create_project_vulnerability_evidence_archive("proj-1")
        try:
            with zipfile.ZipFile(result.disk_path) as archive:
                assert archive.namelist() == ["README.txt"]
                assert "漏洞数量: 0" in archive.read("README.txt").decode("utf-8")
        finally:
            result.cleanup()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "filename",
    [
        "proof.svg",
        "image.DDS",
        "image.qoi",
        "photo.apng",
        "page.mhtml",
        "page.MHT",
        "page.hta",
        "page.STM",
        "payload.jse",
    ],
)
def test_missing_excluded_artifact_does_not_block_archive(
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    conn = _make_conn()
    try:
        _insert_artifact(conn, "a-image", filename, b"missing", kind="file")
        conn.execute(
            """
            INSERT INTO vulnerabilities (
                id, project_id, title, severity, packet_artifact_id, created_at, updated_at
            ) VALUES (
                'vuln-1', 'proj-1', 'Image only', 'low', 'a-image',
                '2026-08-27T00:03:00Z', '2026-08-27T00:03:00Z'
            )
            """
        )
        conn.commit()
        _configure_connection(monkeypatch, conn)
        monkeypatch.setattr(
            project_artifact_archives,
            "artifact_content_file",
            lambda project_id, artifact_id: (_ for _ in ()).throw(
                AssertionError("must not read excluded image, JavaScript, or HTML")
            ),
        )

        result = project_artifact_archives.create_project_vulnerability_evidence_archive("proj-1")
        try:
            with zipfile.ZipFile(result.disk_path) as archive:
                index_name = next(name for name in archive.namelist() if name.endswith("evidence-index.md"))
                assert filename in archive.read(index_name).decode("utf-8")
        finally:
            result.cleanup()
    finally:
        conn.close()


def test_archive_rejects_dangling_observation_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _make_conn()
    try:
        conn.execute(
            """
            INSERT INTO vulnerabilities (
                id, project_id, title, severity, created_at, updated_at
            ) VALUES (
                'vuln-1', 'proj-1', 'Dangling', 'high',
                '2026-08-27T00:03:00Z', '2026-08-27T00:03:00Z'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO vulnerability_observations (
                id, project_id, vulnerability_id, packet_artifact_id,
                identity_json, severity, confidence, observed_at
            ) VALUES (
                'observation-1', 'proj-1', 'vuln-1', 'missing-artifact',
                '{}', 'high', 0.8, '2026-08-27T00:03:00Z'
            )
            """
        )
        conn.commit()
        _configure_connection(monkeypatch, conn)

        with pytest.raises(
            project_artifact_archives.VulnerabilityEvidenceArchiveUnavailableError,
            match="missing-artifact",
        ):
            project_artifact_archives.create_project_vulnerability_evidence_archive("proj-1")
    finally:
        conn.close()


def test_archive_rejects_integrity_mismatch_and_cleans_temporary_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    conn = _make_conn()
    try:
        _insert_artifact(conn, "a1", "evidence.txt", b"expected")
        conn.execute(
            """
            INSERT INTO vulnerabilities (
                id, project_id, title, severity, packet_artifact_id, created_at, updated_at
            ) VALUES (
                'vuln-1', 'proj-1', 'Mismatch', 'high', 'a1',
                '2026-08-27T00:03:00Z', '2026-08-27T00:03:00Z'
            )
            """
        )
        conn.commit()
        _configure_connection(monkeypatch, conn)
        _configure_content_files(
            monkeypatch,
            tmp_path,
            {"a1": ("evidence.txt", b"tampered", "text/plain")},
        )
        created_directories: list[Path] = []
        real_mkdtemp = project_artifact_archives.tempfile.mkdtemp

        def tracked_mkdtemp(*args, **kwargs):
            path = Path(real_mkdtemp(*args, dir=tmp_path, **kwargs))
            created_directories.append(path)
            return str(path)

        monkeypatch.setattr(project_artifact_archives.tempfile, "mkdtemp", tracked_mkdtemp)

        with pytest.raises(
            project_artifact_archives.VulnerabilityEvidenceArchiveUnavailableError,
            match="a1",
        ):
            project_artifact_archives.create_project_vulnerability_evidence_archive("proj-1")

        assert created_directories
        assert all(not path.exists() for path in created_directories)
    finally:
        conn.close()


def test_archive_cleans_temporary_directory_when_file_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    conn = _make_conn()
    try:
        _configure_connection(monkeypatch, conn)
        created_directories: list[Path] = []
        real_mkdtemp = project_artifact_archives.tempfile.mkdtemp

        def tracked_mkdtemp(*args, **kwargs):
            path = Path(real_mkdtemp(*args, dir=tmp_path, **kwargs))
            created_directories.append(path)
            return str(path)

        monkeypatch.setattr(project_artifact_archives.tempfile, "mkdtemp", tracked_mkdtemp)
        real_open = project_artifact_archives.os.open

        def fail_archive_open(path, *args, **kwargs):
            if Path(path).name == "vulnerability-evidence.zip":
                raise OSError("disk full")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(project_artifact_archives.os, "open", fail_archive_open)

        with pytest.raises(OSError, match="disk full"):
            project_artifact_archives.create_project_vulnerability_evidence_archive("proj-1")

        assert created_directories
        assert all(not path.exists() for path in created_directories)
    finally:
        conn.close()


def _archive_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    archive_path = archive_dir / "vulnerability-evidence.zip"
    with zipfile.ZipFile(archive_path, "w"):
        pass
    result = project_artifact_archives.ProjectVulnerabilityEvidenceArchive(
        disk_path=archive_path,
        filename="cairn-proj-1-vulnerability-evidence.zip",
        temporary_directory=archive_dir,
    )
    monkeypatch.setattr(
        audit,
        "create_project_vulnerability_evidence_archive",
        lambda project_id: result,
    )
    return audit.download_artifact_archive("proj-1"), archive_dir


def _scope(*, range_header: bytes | None = None) -> dict[str, object]:
    headers = [] if range_header is None else [(b"range", range_header)]
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/projects/proj-1/artifacts/archive",
        "raw_path": b"/projects/proj-1/artifacts/archive",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "extensions": {},
    }


async def _receive() -> dict[str, object]:
    return {"type": "http.request", "body": b"", "more_body": False}


def test_archive_route_sets_download_headers_and_cleans_response_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    response, archive_dir = _archive_response(monkeypatch, tmp_path)
    messages: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    asyncio.run(response(_scope(), _receive, send))

    assert response.media_type == "application/zip"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "cairn-proj-1-vulnerability-evidence.zip" in response.headers["content-disposition"]
    assert messages[0]["status"] == 200
    assert not archive_dir.exists()


def test_archive_route_cleans_after_unsatisfiable_range(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    response, archive_dir = _archive_response(monkeypatch, tmp_path)
    messages: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    asyncio.run(response(_scope(range_header=b"bytes=999999-"), _receive, send))

    assert messages[0]["status"] == 416
    assert not archive_dir.exists()


def test_archive_route_cleans_after_interrupted_stream(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    response, archive_dir = _archive_response(monkeypatch, tmp_path)

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body":
            raise RuntimeError("client disconnected")

    with pytest.raises(RuntimeError, match="client disconnected"):
        asyncio.run(response(_scope(), _receive, send))

    assert not archive_dir.exists()


def test_archive_route_maps_unavailable_artifact_to_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(project_id: str):
        raise project_artifact_archives.VulnerabilityEvidenceArchiveUnavailableError("a9")

    monkeypatch.setattr(audit, "create_project_vulnerability_evidence_archive", fail)

    with pytest.raises(HTTPException) as exc_info:
        audit.download_artifact_archive("proj-1")

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == (
        "Artifact a9 is unavailable for vulnerability evidence archive"
    )
