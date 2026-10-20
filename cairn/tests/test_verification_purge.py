"""永久删除只操作隔离 DB、合成字节与进程证明，不调用模型、目标或 Docker。"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from threading import Event
from urllib.parse import quote
from uuid import uuid4

from fastapi import HTTPException
import psycopg
import pytest
from starlette.requests import Request

from cairn.server import db
from cairn.server.auth import RequestPrincipal, require_tenant_project
from cairn.server.models import BatchInjectionPayloadOperationsRequest, CreateAgentEventRequest, CreateArtifactRequest, CreateRawArtifactRequest, CreateVulnerabilityRequest, UpdateTaskRunRequest
from cairn.server.schemas.tasks import CreateTaskRunRequest
from cairn.server.schemas.verification_requests import CreateVerificationRequest, VerificationClaimRequest, VerificationExecutionBinding, VerificationFailureSubmission
from cairn.server.services import audit_artifacts, audit_task_runs, project_resources, verification_requests as service
from cairn.server.services.audit_events import create_agent_event_record
from cairn.server.services.candidate_store import _load_locked_execution
from cairn.server.services.ids import next_fact_id
from cairn.server.services.injection_payloads import apply_injection_payload_operations
from cairn.server.services.project_records import list_project_summaries
from cairn.server.services.verification_purge import remove_owned_directory
from cairn.server.services.vulnerabilities import create_vulnerability
from test_assessment_migration_v34 import database, private_postgres
from test_external_verification_evidence import execution, _new_execution, _submission, _upload
from test_external_verification_receipts import _confirm_stopped


@pytest.fixture
def purge_execution(database, tmp_path, monkeypatch):
    backend, connect = database
    if backend == "postgres":
        connection = connect()
        info = connection._connection.info
        url = f"postgresql://{quote(info.user)}@/{quote(info.dbname)}?host={quote(info.host, safe='')}"
        connection.close()
        configure = db.configure
        monkeypatch.setattr(db, "configure", lambda path=None, *, artifact_path=None, **kwargs: configure(database_url=url, artifact_path=artifact_path))
    yield from execution.__wrapped__(tmp_path, monkeypatch)


def _accepted(execution):
    user, machine, _, _, detail, claim = execution
    artifact = _upload(detail, claim, "log", "observed.log", b"synthetic observed bytes\r\n \t")
    submission = _submission(detail, claim, [artifact])
    service.submit_verification_result(detail["id"], submission, machine)
    _confirm_stopped(detail, claim)
    return detail, claim, artifact, submission


def _pending(execution, monkeypatch):
    detail, claim, artifact, submission = _accepted(execution)
    with monkeypatch.context() as patch:
        patch.setattr(service, "remove_owned_directory", lambda *args: (_ for _ in ()).throw(PermissionError("synthetic permission denied")))
        with pytest.raises(HTTPException) as failed:
            service.delete_verification_request(detail["id"], execution[0])
    assert failed.value.status_code == 503
    return detail, claim, artifact, submission


def test_owned_purge_preserves_original_and_accepted_successors(purge_execution):
    user, machine, _, worker, original, original_claim = purge_execution
    original_bytes = b"original evidence must survive\r\n \t"
    original_artifact = _upload(original, original_claim, "log", "source.log", original_bytes)
    with db.get_conn() as conn:
        vulnerability = create_vulnerability(conn, original["project_id"], CreateVulnerabilityRequest(
            title="保留的原漏洞", severity="low", evidence_items=[{"id": "original", "kind": "artifact", "source": "manual",
            "artifact": {"artifact_id": original_artifact.id}}],
        ))
    detail, claim = _new_execution(user, machine, worker, report={"type": "platform_vulnerability", "project_id": original["project_id"], "vulnerability_id": vulnerability.id})
    artifact = _upload(detail, claim, "log", "new.log", b"new round bytes")
    internal = _upload(detail, claim, "raw_output", "internal.log", b"internal archive bytes", metadata={"stream": "stderr"})
    service.submit_verification_result(detail["id"], _submission(detail, claim, [artifact]), machine)
    _confirm_stopped(detail, claim)
    copied = service.create_verification_batch(detail["id"], user)
    independent = service.create_verification_batch(detail["id"], user)
    independent_bytes = {item.id: project_resources._batch_member_disk_path(independent.id, item.id).read_bytes() for item in independent.members}
    main = next(item for item in copied.members if item.source_resource_id == detail["report"]["resource_id"])
    body = CreateVerificationRequest(schema_version=1, request_id=uuid4(), mode="retest", title="已受理独立后轮",
        resource_batch_id=copied.id, previous_request_id=detail["id"], report={"type": "file", "batch_member_id": main.id})
    successor, _ = service.create_verification_request(body, user)
    successor_bytes = {item["id"]: project_resources._project_resource_disk_path(successor["project_id"], item["id"]).read_bytes() for item in successor["materials"]}
    # 仅按 consumed_project_id 删除精确批次，不按项目内 rNNN 或来源映射扩大范围。
    consumed = service.create_verification_batch(None, user)
    with db.get_conn() as conn:
        conn.execute("UPDATE project_resource_batches SET state = 'consumed', consumed_project_id = ? WHERE id = ?", (detail["project_id"], consumed.id))
    consumed_dir = project_resources._batch_directory(consumed.id, create=True)
    (consumed_dir / "leftover").write_bytes(b"owned consumed leftover")
    resource_dir = project_resources.project_resource_root() / detail["project_id"]
    artifact_dir = db.artifact_root() / "projects" / detail["project_id"]
    assert service.verification_artifact(detail["id"], artifact.id, user).disk_path.exists()
    assert audit_artifacts._artifact_disk_path(detail["project_id"], claim["task_run"]["id"], Path(internal.uri).name).exists()
    service.delete_verification_request(detail["id"], user)
    service.delete_verification_request(detail["id"], user)
    assert not resource_dir.exists() and not artifact_dir.exists() and not consumed_dir.exists()
    assert {item.id: project_resources._batch_member_disk_path(independent.id, item.id).read_bytes() for item in independent.members} == independent_bytes
    assert {item["id"]: project_resources._project_resource_disk_path(successor["project_id"], item["id"]).read_bytes() for item in successor["materials"]} == successor_bytes
    assert service.create_verification_request(body, user)[1] is True
    assert service.verification_artifact(original["id"], original_artifact.id, user).disk_path.read_bytes() == original_bytes
    with db.get_conn() as conn:
        assert conn.execute("SELECT 1 FROM vulnerabilities WHERE project_id = ? AND id = ?", (original["project_id"], vulnerability.id)).fetchone()
        assert conn.execute("SELECT 1 FROM project_resource_batches WHERE id = ?", (consumed.id,)).fetchone() is None
        for table in ("projects", "facts", "intents", "task_runs", "agent_events", "artifacts", "project_resources", "scoped_counters"):
            column = "id" if table == "projects" else "project_id"
            assert conn.execute(f"SELECT 1 FROM {table} WHERE {column} = ?", (detail["project_id"],)).fetchone() is None
        row = conn.execute("SELECT * FROM verification_requests WHERE id = ?", (detail["id"],)).fetchone()
        assert row["purge_state"] == "completed" and row["deleted_execution_owner_id"] == machine.subject_id
        assert all(row[key] is None for key in ("project_id", "intent_id", "current_task_run_id", "report_resource_id", "result_json", "error_json", "claim_token", "terminal_digest", "source_project_id"))
    new, replay = service.create_verification_request(CreateVerificationRequest(schema_version=1, request_id=uuid4(), mode="retest", title="仍可从原漏洞新建",
        report={"type": "platform_vulnerability", "project_id": original["project_id"], "vulnerability_id": vulnerability.id}), user)
    assert not replay and not new["runs"]
    with pytest.raises(HTTPException) as source_gone:
        service.create_verification_request(body.model_copy(update={"request_id": uuid4(), "resource_batch_id": independent.id}), user)
    assert source_gone.value.status_code == 410 and source_gone.value.detail == service.SOURCE_DELETED_DETAIL


@pytest.mark.parametrize("failure", ["filesystem", "final_commit"])
def test_pending_survives_failure_and_retry_without_reading_partial_archive(purge_execution, monkeypatch, failure):
    user = purge_execution[0]
    detail, _, _, _ = _accepted(purge_execution)
    directory = db.artifact_root() / "projects" / detail["project_id"]
    if failure == "filesystem":
        original = service.remove_owned_directory
        def fail(root, target):
            if target == directory:
                raise PermissionError("synthetic failure")
            return original(root, target)
        monkeypatch.setattr(service, "remove_owned_directory", fail)
    else:
        cls = db.PostgresConnection if db._config.backend == "postgresql" else db.SQLiteConnection
        commit = cls.commit
        failed = False
        def fail_commit(conn):
            nonlocal failed
            row = conn.execute("SELECT purge_state FROM verification_requests WHERE id = ?", (detail["id"],)).fetchone()
            if row and row["purge_state"] == "completed" and not failed:
                failed = True
                raise RuntimeError("synthetic final commit failure")
            return commit(conn)
        monkeypatch.setattr(cls, "commit", fail_commit)
    with pytest.raises(HTTPException) as error:
        service.delete_verification_request(detail["id"], user)
    assert error.value.status_code == 503
    assert directory.exists() is (failure == "filesystem")
    with monkeypatch.context() as patch:
        patch.setattr(service, "get_project_or_404", lambda *args: pytest.fail("pending 不得读取 Project"))
        projection = service.get_verification_request(detail["id"], user)
        assert projection["purge_state"] == "pending" and projection["can_delete"] and projection["purge_error"]
        assert projection["result"] is None and projection["report"] is None and projection["materials"] == []
        assert service.list_verification_requests(user, limit=50, offset=0)["items"][0]["execution_status"] == "cleanup_pending"
    if failure == "filesystem":
        monkeypatch.setattr(service, "remove_owned_directory", original)
    else:
        monkeypatch.setattr(cls, "commit", commit)
    service.delete_verification_request(detail["id"], user)
    assert service.list_verification_requests(user, limit=50, offset=0)["total"] == 0
    assert not directory.exists()


def test_pending_and_completed_reject_all_late_machine_endpoints_and_project_bypasses(purge_execution, monkeypatch):
    user, machine, _, worker, _, _ = purge_execution
    detail, claim, artifact, submission = _pending(purge_execution, monkeypatch)
    binding = VerificationExecutionBinding(**{key: getattr(submission, key) for key in ("project_id", "task_run_id", "execution_generation", "claim_token")})
    failure = VerificationFailureSubmission(**binding.model_dump(), error={"code": "synthetic_failure", "message": "synthetic"})
    request = Request({"type": "http", "method": "GET", "path": "/synthetic", "headers": []})
    payload_operations = BatchInjectionPayloadOperationsRequest.model_validate({"operations": [{
        "operation": "finish", "capture_id": "late-capture", "task_run_id": claim["task_run"]["id"],
        "response_status": "returned", "response_text": "late synthetic output", "response_format": "text",
    }]})
    for stage in ("pending", "completed"):
        assert detail["project_id"] not in {item.id for item in list_project_summaries(include_verification=True)}
        assert detail["project_id"] not in {item.id for item in list_project_summaries(user.tenant_id, include_verification=True)}
        for call in (
            lambda: service.verification_input(detail["id"], machine),
            lambda: service.claim_verification_request(detail["id"], VerificationClaimRequest(task_run=CreateTaskRunRequest(task_type="external_verify", worker_type="pi", worker_name=worker, runtime_context={"dispatcher_id": machine.subject_id})), machine),
            lambda: service.heartbeat_verification_request(detail["id"], binding, machine),
            lambda: service.submit_verification_result(detail["id"], submission, machine),
            lambda: service.submit_verification_failure(detail["id"], failure, machine),
            lambda: service.verification_result_receipt(detail["id"], submission, machine),
            lambda: service.acknowledge_verification_cancel(detail["id"], binding, machine),
            lambda: service.verification_report(detail["id"], user),
            lambda: service.verification_artifact(detail["id"], artifact.id, user),
        ):
            with pytest.raises(HTTPException) as rejected:
                call()
            assert rejected.value.status_code == 410
        for caller in (user, machine):
            with pytest.raises(HTTPException) as bypass:
                require_tenant_project(detail["project_id"], request, caller)
            assert bypass.value.status_code == 404
        other = RequestPrincipal(kind="machine", subject_id="another-dispatcher")
        with pytest.raises(HTTPException) as isolated:
            service.verification_result_receipt(detail["id"], submission, other)
        assert isolated.value.status_code == 404
        if stage == "pending":
            for call in (
                lambda: audit_artifacts.create_artifact_record(detail["project_id"], CreateArtifactRequest(kind="log", content="late")),
                lambda: audit_task_runs.update_task_run_record(detail["project_id"], claim["task_run"]["id"], UpdateTaskRunRequest(status="succeeded"), principal=machine),
                lambda: create_agent_event_record(detail["project_id"], CreateAgentEventRequest(task_run_id=claim["task_run"]["id"], kind="protocol_write", payload={})),
            ):
                with pytest.raises(HTTPException) as late:
                    call()
                assert late.value.status_code == 410
            with db.get_conn() as conn:
                for call in (
                    lambda: _load_locked_execution(conn, detail["project_id"], claim["task_run"]["id"]),
                    lambda: next_fact_id(conn, detail["project_id"]),
                    lambda: apply_injection_payload_operations(conn, detail["project_id"], payload_operations),
                ):
                    with pytest.raises(HTTPException) as late:
                        call()
                    assert late.value.status_code == 410
            service.delete_verification_request(detail["id"], user)


def test_raw_stream_prevents_false_completion_and_cannot_publish_after_pending(purge_execution):
    user = purge_execution[0]
    detail, claim, _, _ = _accepted(purge_execution)
    async def race():
        started, release = asyncio.Event(), asyncio.Event()
        async def chunks():
            yield b"first"
            started.set()
            await release.wait()
            yield b"last"
        upload = asyncio.create_task(audit_artifacts.create_raw_artifact_record(detail["project_id"], CreateRawArtifactRequest(
            task_run_id=claim["task_run"]["id"], kind="log", filename="late.log"), chunks()))
        await started.wait()
        with pytest.raises(HTTPException) as busy:
            service.delete_verification_request(detail["id"], user)
        assert busy.value.status_code == 503
        owned = db.artifact_root() / "projects" / detail["project_id"] / ".uploads"
        assert list(owned.glob(".raw-artifact-*"))
        assert not (db.artifact_root() / ".uploads").exists()
        release.set()
        with pytest.raises(HTTPException) as closed:
            await upload
        assert closed.value.status_code == 410
        service.delete_verification_request(detail["id"], user)
        assert not owned.parent.exists()
    asyncio.run(race())


def test_consumed_batch_keeps_active_upload_until_closed(purge_execution):
    user = purge_execution[0]
    batch = service.create_verification_batch(None, user)
    async def race():
        started, release = asyncio.Event(), asyncio.Event()
        async def chunks():
            yield b"first"
            started.set()
            await release.wait()
            yield b"last"
        upload = asyncio.create_task(project_resources.upload_project_resource_batch_file(batch.id, chunks(), path="late.txt", kind="note",
            tenant_id=user.tenant_id, user_id=user.subject_id))
        await started.wait()
        detail, _ = service.create_verification_request(CreateVerificationRequest(schema_version=1, request_id=uuid4(), mode="retest", title="消费在途批次",
            resource_batch_id=batch.id, report={"type": "text", "content": "local"}), user)
        service.cancel_verification_request(detail["id"], user)
        with pytest.raises(HTTPException) as busy:
            service.delete_verification_request(detail["id"], user)
        assert busy.value.status_code == 503
        assert list(project_resources._batch_directory(batch.id).glob(".upload-*"))
        release.set()
        with pytest.raises(HTTPException) as consumed:
            await upload
        assert consumed.value.status_code == 409
        service.delete_verification_request(detail["id"], user)
        assert not project_resources._batch_directory(batch.id).exists()
    asyncio.run(race())


def test_result_accepted_live_execution_can_be_stopped_without_rewriting_result(purge_execution):
    user, machine, _, _, detail, claim = purge_execution
    artifact = _upload(detail, claim, "log", "accepted-live.log", b"synthetic evidence")
    submission = _submission(detail, claim, [artifact])
    service.submit_verification_result(detail["id"], submission, machine)
    with db.get_conn() as conn:
        before = dict(conn.execute("SELECT result_json,terminal_digest,terminal_at,accepted_at FROM verification_requests WHERE id = ?", (detail["id"],)).fetchone())
    stopped = service.cancel_verification_request(detail["id"], user)
    assert stopped["execution_status"] == "cancelling" and stopped["result"]
    with db.get_conn() as conn:
        assert dict(conn.execute("SELECT result_json,terminal_digest,terminal_at,accepted_at FROM verification_requests WHERE id = ?", (detail["id"],)).fetchone()) == before
        assert conn.execute("SELECT pause_requested FROM task_runs WHERE project_id = ?", (detail["project_id"],)).fetchone()["pause_requested"] == 1
    _confirm_stopped(detail, claim, cleanup="pending")
    with pytest.raises(HTTPException) as environment:
        service.delete_verification_request(detail["id"], user)
    assert environment.value.status_code == 409 and "尚未回收" in environment.value.detail
    _confirm_stopped(detail, claim)
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET execution_generation = execution_generation + 1 WHERE id = ?", (detail["project_id"],))
    with pytest.raises(HTTPException) as generation:
        service.delete_verification_request(detail["id"], user)
    assert generation.value.status_code == 409 and "执行结束" in generation.value.detail


def test_same_id_purge_serializes_both_callers(purge_execution, monkeypatch):
    user = purge_execution[0]
    detail, _, _, _ = _accepted(purge_execution)
    entered, release = Event(), Event()
    remove = service.remove_owned_directory
    first = True
    def wait(root, target):
        nonlocal first
        if first:
            first = False
            entered.set()
            assert release.wait(15)
        return remove(root, target)
    monkeypatch.setattr(service, "remove_owned_directory", wait)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(service.delete_verification_request, detail["id"], user)
        assert entered.wait(15)
        two = pool.submit(service.delete_verification_request, detail["id"], user)
        release.set()
        one.result(timeout=20)
        two.result(timeout=20)
    with db.get_conn() as conn:
        assert conn.execute("SELECT purge_state FROM verification_requests WHERE id = ?", (detail["id"],)).fetchone()["purge_state"] == "completed"


@pytest.mark.parametrize("operation", ["copy", "accept"])
def test_real_copy_or_accept_holds_source_lock_until_independent_bytes_commit(purge_execution, monkeypatch, operation):
    user = purge_execution[0]
    detail, _, _, _ = _accepted(purge_execution)
    entered, release = Event(), Event()
    if operation == "copy":
        copy = service.copy_verified_resource_content
        def pause(*args, **kwargs):
            entered.set()
            assert release.wait(15)
            return copy(*args, **kwargs)
        monkeypatch.setattr(service, "copy_verified_resource_content", pause)
        perform = lambda: service.create_verification_batch(detail["id"], user)
    else:
        batch = service.create_verification_batch(detail["id"], user)
        main = next(member for member in batch.members if member.source_resource_id == detail["report"]["resource_id"])
        body = CreateVerificationRequest(schema_version=1, request_id=uuid4(), mode="retest", title="并发受理独立字节", resource_batch_id=batch.id,
            report={"type": "file", "batch_member_id": main.id})
        consume = service.consume_project_resource_batch
        def pause(*args, **kwargs):
            entered.set()
            assert release.wait(15)
            return consume(*args, **kwargs)
        monkeypatch.setattr(service, "consume_project_resource_batch", pause)
        perform = lambda: service.create_verification_request(body, user)[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        copied = pool.submit(perform)
        assert entered.wait(15)
        deleted = pool.submit(service.delete_verification_request, detail["id"], user)
        try:
            with pytest.raises(TimeoutError):
                deleted.result(timeout=0.2)
        finally:
            release.set()
        value = copied.result(timeout=20)
        deleted.result(timeout=20)
    if operation == "copy":
        assert all(project_resources._batch_member_disk_path(value.id, item.id).read_bytes() for item in value.members)
    else:
        assert all(project_resources._project_resource_disk_path(value["project_id"], item["id"]).read_bytes() for item in value["materials"])
        assert service.create_verification_request(body, user)[1] is True


def test_pending_wins_against_actual_copy_and_accept(purge_execution, monkeypatch):
    user = purge_execution[0]
    detail, _, _, _ = _accepted(purge_execution)
    batch = service.create_verification_batch(detail["id"], user)
    before = {member.id: project_resources._batch_member_disk_path(batch.id, member.id).read_bytes() for member in batch.members}
    entered, release = Event(), Event()
    remove = service.remove_owned_directory
    first = True
    def pause(*args):
        nonlocal first
        if first:
            first = False
            entered.set()
            assert release.wait(15)
        return remove(*args)
    monkeypatch.setattr(service, "remove_owned_directory", pause)
    body = CreateVerificationRequest(schema_version=1, request_id=uuid4(), mode="retest", title="pending 后不能受理",
        resource_batch_id=batch.id, report={"type": "file", "batch_member_id": batch.members[0].id})
    # 省略 previous 也必须检查已提交的真实来源 pending，不能进入普通上传路径。
    with ThreadPoolExecutor(max_workers=3) as pool:
        deleted = pool.submit(service.delete_verification_request, detail["id"], user)
        assert entered.wait(15)
        copied = pool.submit(service.create_verification_batch, detail["id"], user)
        accepted = pool.submit(service.create_verification_request, body, user)
        release.set()
        deleted.result(timeout=20)
        for result in (copied, accepted):
            with pytest.raises(HTTPException) as rejected:
                result.result(timeout=20)
            assert rejected.value.status_code == 410 and rejected.value.detail == service.SOURCE_DELETED_DETAIL
    assert {member.id: project_resources._batch_member_disk_path(batch.id, member.id).read_bytes() for member in batch.members} == before


@pytest.mark.parametrize("database", ["postgres"], indirect=True)
def test_postgres_v57_preserves_legacy_batch_metadata_and_adds_source_foreign_key(database, monkeypatch):
    backend, connect = database
    conn = connect()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(db, "POSTGRES_MIGRATIONS", tuple(item for item in db.POSTGRES_MIGRATIONS if item.version <= 56))
            db.run_migrations(conn, backend)
        conn.execute("INSERT INTO users(id, login, password_hash, role, created_at, updated_at) VALUES ('batch-owner', 'batch-owner', 'synthetic', 'user', '2000', '2000')")
        conn.execute("INSERT INTO project_resource_batches(id, tenant_id, user_id, created_at, expires_at) VALUES ('legacy-batch', 'tenant_legacy', 'batch-owner', '2000', '2099')")
        conn.execute("INSERT INTO project_resource_batch_members(id, batch_id, kind, path, size_bytes, sha256, source, created_at, source_resource_id) VALUES ('legacy-member', 'legacy-batch', 'note', 'old.txt', 1, ?, 'file', '2000', 'r001')", ("a" * 64,))
        before_batch = dict(conn.execute("SELECT * FROM project_resource_batches WHERE id = 'legacy-batch'").fetchone())
        before_members = [dict(row) for row in conn.execute("SELECT * FROM project_resource_batch_members").fetchall()]
        applied = [dict(row) for row in conn.execute("SELECT * FROM schema_migrations WHERE version IN (55, 56) ORDER BY version").fetchall()]
        conn.commit()
        db.run_migrations(conn, backend)
        conn.commit()
        assert dict(conn.execute("SELECT * FROM project_resource_batches WHERE id = 'legacy-batch'").fetchone()) == before_batch | {"source_request_id": None}
        assert [dict(row) for row in conn.execute("SELECT * FROM project_resource_batch_members").fetchall()] == before_members
        assert [dict(row) for row in conn.execute("SELECT * FROM schema_migrations WHERE version IN (55, 56) ORDER BY version").fetchall()] == applied
        assert conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()["version"] == 57
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("UPDATE project_resource_batches SET source_request_id = 'unknown-source' WHERE id = 'legacy-batch'")
        conn.rollback()
    finally:
        conn.close()


@pytest.mark.parametrize("database", ["postgres"], indirect=True)
def test_explicit_batch_delete_keeps_ownership_and_fences_inflight_upload(purge_execution):
    user = purge_execution[0]
    scope = service.caller_scope(user)
    batch = service.create_verification_batch(None, user)
    async def initial():
        yield b"finished member bytes"
    member = asyncio.run(project_resources.upload_project_resource_batch_file(
        batch.id, initial(), path="finished.txt", kind="note", **scope))
    async def race():
        started, release = asyncio.Event(), asyncio.Event()
        async def chunks():
            yield b"inflight first"
            started.set()
            await release.wait()
            yield b"inflight last"
        upload = asyncio.create_task(project_resources.upload_project_resource_batch_file(
            batch.id, chunks(), path="inflight.txt", kind="note", **scope))
        await started.wait()
        with pytest.raises(HTTPException) as busy:
            project_resources.delete_project_resource_batch(batch.id, **scope)
        assert busy.value.status_code == 503 and "清理未完成" in busy.value.detail
        with db.get_conn() as conn:
            row = conn.execute("SELECT * FROM project_resource_batches WHERE id = ?", (batch.id,)).fetchone()
            assert row["user_id"] == user.subject_id and row["expires_at"] <= project_resources.utcnow()
            assert conn.execute("SELECT id FROM project_resource_batch_members WHERE batch_id = ?", (batch.id,)).fetchone()["id"] == member.id
        assert project_resources._batch_member_disk_path(batch.id, member.id).read_bytes() == b"finished member bytes"
        assert project_resources.cleanup_expired_project_resource_batches() == 0
        with pytest.raises(HTTPException) as wrong_owner:
            project_resources.delete_project_resource_batch(batch.id, tenant_id=user.tenant_id, user_id="another-user")
        assert wrong_owner.value.status_code == 404
        release.set()
        with pytest.raises(HTTPException) as fenced:
            await upload
        assert fenced.value.status_code == 410
        project_resources.delete_project_resource_batch(batch.id, **scope)
        assert not project_resources._batch_directory(batch.id).exists()
        with db.get_conn() as conn:
            assert conn.execute("SELECT 1 FROM project_resource_batches WHERE id = ?", (batch.id,)).fetchone() is None
            assert conn.execute("SELECT 1 FROM project_resource_batch_members WHERE batch_id = ?", (batch.id,)).fetchone() is None
    asyncio.run(race())


@pytest.mark.parametrize("database", ["postgres"], indirect=True)
def test_batch_io_failure_remains_ttl_eligible_and_normal_consumption_survives(purge_execution, monkeypatch):
    user = purge_execution[0]
    scope = service.caller_scope(user)
    failed = service.create_verification_batch(None, user)
    normal = service.create_verification_batch(None, user)
    async def content():
        yield b"independent upload bytes"
    failed_member = asyncio.run(project_resources.upload_project_resource_batch_file(
        failed.id, content(), path="failed.txt", kind="note", **scope))
    normal_member = asyncio.run(project_resources.upload_project_resource_batch_file(
        normal.id, content(), path="normal.txt", kind="note", **scope))
    with monkeypatch.context() as patch:
        patch.setattr(project_resources, "remove_owned_directory", lambda *args: (_ for _ in ()).throw(PermissionError("synthetic I/O failure")))
        with pytest.raises(HTTPException) as failure:
            project_resources.delete_project_resource_batch(failed.id, **scope)
        assert failure.value.status_code == 503
    assert project_resources._batch_member_disk_path(failed.id, failed_member.id).read_bytes() == b"independent upload bytes"
    assert project_resources.cleanup_expired_project_resource_batches() == 1
    assert not project_resources._batch_directory(failed.id).exists()
    detail, _ = service.create_verification_request(CreateVerificationRequest(
        schema_version=1, request_id=uuid4(), mode="retest", title="正常独立上传不需要 previous",
        resource_batch_id=normal.id, report={"type": "file", "batch_member_id": normal_member.id}), user)
    resource_path = project_resources._project_resource_disk_path(detail["project_id"], detail["report"]["resource_id"])
    assert resource_path.read_bytes() == b"independent upload bytes"
    with db.get_conn() as conn:
        assert conn.execute("SELECT source_request_id FROM project_resource_batches WHERE id = ?", (normal.id,)).fetchone()["source_request_id"] is None
        conn.execute("UPDATE project_resource_batches SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?", (normal.id,))
    assert project_resources.cleanup_expired_project_resource_batches() == 1
    assert resource_path.read_bytes() == b"independent upload bytes"


@pytest.mark.parametrize("database", ["postgres"], indirect=True)
@pytest.mark.parametrize("report_type", ["text", "markdown"])
def test_copied_batch_checks_real_source_without_or_with_replaced_previous(purge_execution, monkeypatch, report_type):
    user = purge_execution[0]
    source, _ = service.create_verification_request(CreateVerificationRequest(
        schema_version=1, request_id=uuid4(), mode="retest", title="真实复制来源",
        report={"type": report_type, "content": "保留的旧输入原文\r\n \t"}), user)
    other, _ = service.create_verification_request(CreateVerificationRequest(
        schema_version=1, request_id=uuid4(), mode="retest", title="无关上一轮",
        report={"type": "text", "content": "不是复制来源"}), user)
    accepted_batch = service.create_verification_batch(source["id"], user)
    main = next(member for member in accepted_batch.members if member.source_resource_id == source["report"]["resource_id"])
    accepted_body = CreateVerificationRequest(schema_version=1, request_id=uuid4(), mode="retest", title="来源有效时可省略 previous",
        resource_batch_id=accepted_batch.id, report={"type": "file", "batch_member_id": main.id})
    accepted, _ = service.create_verification_request(accepted_body, user)
    accepted_bytes = {item["id"]: project_resources._project_resource_disk_path(accepted["project_id"], item["id"]).read_bytes() for item in accepted["materials"]}
    kept = service.create_verification_batch(source["id"], user)
    kept_bytes = {item.id: project_resources._batch_member_disk_path(kept.id, item.id).read_bytes() for item in kept.members}
    with db.get_conn() as conn:
        assert conn.execute("SELECT source_request_id FROM project_resource_batches WHERE id = ?", (kept.id,)).fetchone()["source_request_id"] == source["id"]
    service.cancel_verification_request(source["id"], user)
    with monkeypatch.context() as patch:
        patch.setattr(service, "remove_owned_directory", lambda *args: (_ for _ in ()).throw(PermissionError("synthetic deletion failure")))
        with pytest.raises(HTTPException) as pending:
            service.delete_verification_request(source["id"], user)
        assert pending.value.status_code == 503
    for stage in ("pending", "completed"):
        if stage == "completed":
            service.delete_verification_request(source["id"], user)
        for previous in (None, other["id"]):
            body = CreateVerificationRequest(schema_version=1, request_id=uuid4(), mode="retest", title="不能更换真实来源",
                previous_request_id=previous, resource_batch_id=kept.id,
                report={"type": "file", "batch_member_id": kept.members[0].id})
            with pytest.raises(HTTPException) as deleted:
                service.create_verification_request(body, user)
            assert deleted.value.status_code == 410 and deleted.value.detail == service.SOURCE_DELETED_DETAIL
        assert {item.id: project_resources._batch_member_disk_path(kept.id, item.id).read_bytes() for item in kept.members} == kept_bytes
        assert {item["id"]: project_resources._project_resource_disk_path(accepted["project_id"], item["id"]).read_bytes() for item in accepted["materials"]} == accepted_bytes
        assert service.create_verification_request(accepted_body, user)[1] is True
    project_resources.delete_project_resource_batch(kept.id, **service.caller_scope(user))
    assert not project_resources._batch_directory(kept.id).exists()


@pytest.mark.parametrize("database", ["postgres"], indirect=True)
def test_legacy_copied_batch_is_not_guessed_and_remains_explicitly_cleanable(purge_execution):
    user = purge_execution[0]
    source = purge_execution[4]
    copied = service.create_verification_batch(source["id"], user)
    original = {item.id: project_resources._batch_member_disk_path(copied.id, item.id).read_bytes() for item in copied.members}
    with db.get_conn() as conn:
        # 模拟迁移前只有项目内 source_resource_id 的复制批次，不按 rNNN 反查来源。
        conn.execute("UPDATE project_resource_batches SET source_request_id = NULL WHERE id = ?", (copied.id,))
    body = CreateVerificationRequest(schema_version=1, request_id=uuid4(), mode="retest", title="旧复制来源无法核验",
        previous_request_id=source["id"], resource_batch_id=copied.id,
        report={"type": "file", "batch_member_id": copied.members[0].id})
    with pytest.raises(HTTPException) as unverifiable:
        service.create_verification_request(body, user)
    assert unverifiable.value.status_code == 422 and "缺少可核验的来源请求身份" in unverifiable.value.detail
    assert {item.id: project_resources._batch_member_disk_path(copied.id, item.id).read_bytes() for item in copied.members} == original
    project_resources.delete_project_resource_batch(copied.id, **service.caller_scope(user))
    assert not project_resources._batch_directory(copied.id).exists()


@pytest.mark.parametrize("database", ["postgres"], indirect=True)
def test_pending_filters_keep_retry_projection_with_consistent_paging_and_isolation(purge_execution, monkeypatch):
    user, machine, model, worker, _, _ = purge_execution
    pending, _, _, _ = _pending(purge_execution, monkeypatch)
    normal, claim = _new_execution(user, machine, worker)
    normal, _, _, _ = _accepted((user, machine, model, worker, normal, claim))
    queued, _ = service.create_verification_request(CreateVerificationRequest(
        schema_version=1, request_id=uuid4(), mode="retest", title="正常记录仍遵守筛选",
        report={"type": "text", "content": "独立输入"}), user)
    with db.get_conn() as conn:
        for integration in ("filter-a", "filter-b"):
            conn.execute("INSERT INTO verification_integrations(id, tenant_id, name, token_hash, created_at, updated_at) VALUES (?, ?, ?, ?, '2000', '2000')",
                         (integration, user.tenant_id, integration, integration))
        for request_id in (pending["id"], normal["id"], queued["id"]):
            conn.execute("UPDATE verification_requests SET caller_type = 'integration', caller_id = 'filter-a', external_id = 'wanted' WHERE id = ?", (request_id,))
    caller = RequestPrincipal(kind="integration", subject_id="filter-a", tenant_id=user.tenant_id)
    other = RequestPrincipal(kind="integration", subject_id="filter-b", tenant_id=user.tenant_id)
    other_tenant = RequestPrincipal(kind="user", subject_id="other-user", tenant_id="another-tenant", role="user")
    for filters in ({"execution_status": "completed"}, {"outcome": "confirmed"}, {"execution_status": "completed", "outcome": "confirmed"}):
        listing = service.list_verification_requests(caller, limit=50, offset=0, **filters)
        assert listing["total"] == 2 and {item["id"] for item in listing["items"]} == {pending["id"], normal["id"]}
        projection = next(item for item in listing["items"] if item["id"] == pending["id"])
        assert projection["execution_status"] == "cleanup_pending" and projection["result"] is None and projection["can_delete"]
        pages = [service.list_verification_requests(caller, limit=1, offset=index, **filters) for index in range(3)]
        assert [page["total"] for page in pages] == [2, 2, 2]
        assert [item["id"] for page in pages for item in page["items"]] == [item["id"] for item in listing["items"]]
        assert service.list_verification_requests(other, limit=50, offset=0, **filters)["total"] == 0
        assert service.list_verification_requests(other_tenant, limit=50, offset=0, **filters)["total"] == 0
        assert service.list_verification_requests(caller, limit=50, offset=0, external_id="other", **filters)["total"] == 0
        assert service.list_verification_requests(caller, limit=50, offset=0, mode="reproduce", **filters)["total"] == 0
        assert service.list_verification_requests(user, limit=50, offset=0, source_project_id="unrelated", source_vulnerability_id="v999", **filters)["total"] == 0
    service.delete_verification_request(pending["id"], caller)
    listing = service.list_verification_requests(caller, limit=50, offset=0, execution_status="completed", outcome="confirmed")
    assert listing["total"] == 1 and listing["items"][0]["id"] == normal["id"]


def test_strict_cleanup_rejects_links_and_outside_paths(tmp_path):
    root = tmp_path / "owned"
    root.mkdir()
    target = root / "p"
    target.mkdir()
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (outside / "keep").write_bytes(b"keep")
    (target / "link").symlink_to(outside, target_is_directory=True)
    for path in (target, outside, root):
        with pytest.raises(ValueError):
            remove_owned_directory(root, path)
    assert (outside / "keep").read_bytes() == b"keep"
    remove_owned_directory(root, root / "absent")


def _personal_verification_user(name):
    from cairn.server.auth import create_user_with_personal_tenant
    from cairn.server.schemas.models import CreateTenantModelRequest
    from cairn.server.services.tenant_models import create_tenant_model

    user_id, tenant_id = create_user_with_personal_tenant(name, "synthetic password")
    principal = RequestPrincipal(kind="user", subject_id=user_id, tenant_id=tenant_id, role="user")
    model = create_tenant_model(CreateTenantModelRequest(
        display_name=f"{name} model", protocol="openai-responses", base_url="https://unused.example.test",
        model_name=f"{name}-model", api_key=f"synthetic-{name}-secret",
    ), principal=principal)
    return principal, model


def _verification_browser_headers(name):
    from fastapi import Response
    from cairn.server.auth import login

    response = Response()
    _, csrf = login(response, name, "synthetic password")
    return {"cookie": response.headers["set-cookie"].split(";", 1)[0], "x-cairn-csrf-token": csrf}


@pytest.fixture
def verification_api(monkeypatch):
    import json
    from fastapi import FastAPI
    from cairn.server.routers import audit, monitoring, project_resources as resource_router, projects, verification_requests, vulnerabilities

    # 仅挂载真实路由，不进入应用 lifespan，也不启动服务、Dispatcher 或执行进程。
    app = FastAPI()
    for module in (verification_requests, projects, resource_router, audit, vulnerabilities, monitoring):
        app.include_router(module.router)
    monkeypatch.setenv("CAIRN_TRUSTED_ORIGINS", "http://testserver")

    def request(method, path, *, headers=None, body=None):
        payload = json.dumps(body).encode() if body is not None else b""
        request_headers = {"host": "testserver", "origin": "http://testserver", "content-type": "application/json", **(headers or {})}
        raw_path, _, query = path.partition("?")
        messages = []
        received = False

        async def receive():
            nonlocal received
            if received:
                return {"type": "http.disconnect"}
            received = True
            return {"type": "http.request", "body": payload, "more_body": False}

        async def send(message):
            messages.append(message)

        asyncio.run(app({
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
            "method": method, "scheme": "http", "path": raw_path, "raw_path": raw_path.encode(),
            "query_string": query.encode(), "root_path": "", "client": ("testclient", 50000),
            "server": ("testserver", 80), "headers": [(key.encode(), value.encode()) for key, value in request_headers.items()],
        }, receive, send))
        start = next(message for message in messages if message["type"] == "http.response.start")
        response_headers = {key.decode(): value.decode() for key, value in start["headers"]}
        content = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
        value = json.loads(content) if content and response_headers.get("content-type", "").startswith("application/json") else content
        return start["status"], response_headers, value

    return request


@pytest.mark.parametrize("database", ["postgres"], indirect=True)
def test_personal_configuration_browser_scope_and_token_admin_stay_separate(purge_execution, verification_api):
    import json
    from cairn.server.schemas.verification_requests import CreateVerificationIntegration, UpdateVerificationConfiguration
    from cairn.server.services import verification_configuration as configuration, verification_integrations as integrations

    admin, machine, admin_model, _, _, _ = purge_execution
    user, model = _personal_verification_user("config-owner")
    other, other_model = _personal_verification_user("config-other")
    headers = _verification_browser_headers("config-owner")
    other_headers = _verification_browser_headers("config-other")
    admin_headers = _verification_browser_headers("evidence-admin")
    path = "/api/v1/verification-configuration"
    body = {"model_id": model.id}
    status, response_headers, initial = verification_api("GET", path, headers=headers)
    assert status == 200 and initial["tenant_id"] == user.tenant_id and initial["tenant_worker_id"] is None
    assert response_headers["cache-control"] == "no-store"
    status, _, options = verification_api("GET", path + "/options", headers=headers)
    assert status == 200 and [item["id"] for item in options["models"]] == [model.id]
    assert all(secret not in json.dumps(options) for secret in ("synthetic-config-owner-secret", "synthetic-config-other-secret", "synthetic-platform-model-secret"))
    assert admin_model.id not in json.dumps(options) and other_model.id not in json.dumps(options)
    no_csrf = {"cookie": headers["cookie"]}
    assert verification_api("PUT", path, headers=no_csrf, body=body)[0] == 403
    status, _, saved = verification_api("PUT", path, headers=headers, body=body)
    assert status == 200 and saved["ready"] and saved["revision"] == 1
    status, _, updated = verification_api("PUT", path + f"?tenant_id={user.tenant_id}", headers=headers, body=body)
    assert status == 200 and updated["tenant_worker_id"] == saved["tenant_worker_id"] and updated["revision"] == 2
    assert verification_api("GET", "/api/v1/verification-readiness", headers=headers)[2]["ready"]
    for suffix in ("", "/options"):
        assert verification_api("GET", path + suffix + f"?tenant_id={other.tenant_id}", headers=headers)[0] == 403
        assert verification_api("GET", path + suffix, headers=admin_headers)[0] == 422
    assert verification_api("PUT", path + f"?tenant_id={other.tenant_id}", headers=headers, body=body)[0] == 403
    assert verification_api("PUT", path, headers=headers, body={"model_id": other_model.id})[0] == 404
    assert verification_api("GET", path + "?tenant_id=missing-synthetic-tenant", headers=admin_headers)[0] == 404
    managed = verification_api("PUT", path + f"?tenant_id={other.tenant_id}", headers=admin_headers, body={"model_id": other_model.id})
    assert managed[0] == 200
    owned = verification_api("PUT", path, headers=other_headers, body={"model_id": other_model.id})
    assert owned[0] == 200 and owned[2]["tenant_worker_id"] == managed[2]["tenant_worker_id"]
    with db.get_conn() as conn:
        worker = conn.execute("SELECT * FROM tenant_workers WHERE id = ?", (saved["tenant_worker_id"],)).fetchone()
        assert worker["tenant_id"] == user.tenant_id and worker["purpose"] == "verification"
        assert conn.execute("SELECT created_by FROM tenant_workers WHERE id = ?", (managed[2]["tenant_worker_id"],)).fetchone()["created_by"] == admin.subject_id
        assert conn.execute("SELECT COUNT(*) AS c FROM tenant_workers WHERE tenant_id = ? AND purpose = 'verification'", (user.tenant_id,)).fetchone()["c"] == 1
    token = integrations.create_integration(CreateVerificationIntegration(name="synthetic configuration isolation"), admin, tenant_id=user.tenant_id)
    integration = RequestPrincipal(kind="integration", subject_id=token["integration"]["id"], tenant_id=user.tenant_id)
    for principal in (integration, machine):
        for operation in (lambda: configuration.get_verification_configuration(principal),
                          lambda: configuration.verification_configuration_options(principal),
                          lambda: configuration.update_verification_configuration(UpdateVerificationConfiguration(**body), principal)):
            with pytest.raises(HTTPException) as denied:
                operation()
            assert denied.value.status_code == 403
    for bearer in (token["token"], "synthetic-machine-token"):
        for method, suffix in (("GET", ""), ("GET", "/options"), ("PUT", "")):
            assert verification_api(method, path + suffix, headers={"authorization": f"Bearer {bearer}"}, body=body if method == "PUT" else None)[0] == 401
    integration_path = "/api/v1/verification-integrations"
    for method, suffix, payload in (("GET", "", None), ("POST", "", {"name": "forbidden"}),
                                    ("PATCH", f"/{integration.subject_id}", {"name": "forbidden"}),
                                    ("POST", f"/{integration.subject_id}/disable", None),
                                    ("POST", f"/{integration.subject_id}/rotate-token", None)):
        assert verification_api(method, integration_path + suffix + f"?tenant_id={user.tenant_id}", headers=headers, body=payload)[0] == 403
    with pytest.raises(HTTPException) as token_admin:
        integrations.list_integrations(user, tenant_id=user.tenant_id)
    assert token_admin.value.status_code == 403
    # 受控非 owner、legacy 与多 owner 记录只验证拒绝，不修复或猜测归属。
    with db.get_conn() as conn:
        conn.execute("UPDATE tenant_memberships SET role = 'member' WHERE user_id = ?", (user.subject_id,))
    with pytest.raises(HTTPException) as non_owner:
        configuration.get_verification_configuration(user)
    assert non_owner.value.status_code == 403
    with db.get_conn() as conn:
        conn.execute("UPDATE tenant_memberships SET role = 'owner' WHERE user_id = ?", (user.subject_id,))
        conn.execute("UPDATE tenants SET kind = 'legacy' WHERE id = ?", (user.tenant_id,))
    with pytest.raises(HTTPException) as legacy:
        configuration.verification_configuration_options(user)
    assert legacy.value.status_code == 403
    with db.get_conn() as conn:
        conn.execute("UPDATE tenants SET kind = 'personal' WHERE id = ?", (user.tenant_id,))
        conn.execute("UPDATE tenant_memberships SET tenant_id = ? WHERE user_id = ?", (user.tenant_id, other.subject_id))
    with pytest.raises(HTTPException) as ambiguous:
        configuration.update_verification_configuration(UpdateVerificationConfiguration(**body), user)
    assert ambiguous.value.status_code == 403


@pytest.mark.parametrize("database", ["postgres"], indirect=True)
def test_request_caller_scope_covers_history_sources_and_internal_project_reads(purge_execution, verification_api):
    from cairn.server.schemas.verification_requests import CreateVerificationIntegration, UpdateVerificationConfiguration
    from cairn.server.services import verification_configuration as configuration, verification_integrations as integrations

    admin, machine, _, _, _, _ = purge_execution
    user, model = _personal_verification_user("record-owner")
    peer, _ = _personal_verification_user("record-peer")
    worker = configuration.update_verification_configuration(UpdateVerificationConfiguration(model_id=model.id), user)["tenant_worker_id"]
    # 同 Tenant 不同 caller 的历史记录独立于个人配置准入，不要求重新分配历史归属。
    with db.get_conn() as conn:
        conn.execute("UPDATE tenant_memberships SET tenant_id = ? WHERE user_id = ?", (user.tenant_id, peer.subject_id))
    peer = RequestPrincipal(kind="user", subject_id=peer.subject_id, tenant_id=user.tenant_id, role="user")
    headers = _verification_browser_headers("record-owner")
    peer_headers = _verification_browser_headers("record-peer")
    admin_headers = _verification_browser_headers("evidence-admin")
    detail, claim = _new_execution(user, machine, worker)
    with db.get_conn() as conn:
        vulnerability = create_vulnerability(conn, detail["project_id"], CreateVulnerabilityRequest(title="受控内部复测漏洞", severity="low"))
        conn.execute("INSERT INTO projects(id, tenant_id, title, status, created_at) VALUES ('ordinary-synthetic', ?, '普通探索范围不变', 'active', '2000')", (user.tenant_id,))
        ordinary = create_vulnerability(conn, "ordinary-synthetic", CreateVulnerabilityRequest(title="普通探索漏洞", severity="low"))
    detail, _, artifact, _ = _accepted((user, machine, model, worker, detail, claim))

    def queued(principal, title, **extra):
        return service.create_verification_request(CreateVerificationRequest(schema_version=1, request_id=uuid4(), mode="retest", title=title,
            report={"type": "text", "content": "受控历史原文"}, **extra), principal)[0]

    successor = queued(user, "本人后轮", previous_request_id=detail["id"])
    foreign = queued(peer, "同租户另一用户")
    token = integrations.create_integration(CreateVerificationIntegration(name="synthetic record caller"), admin, tenant_id=user.tenant_id)
    integration = RequestPrincipal(kind="integration", subject_id=token["integration"]["id"], tenant_id=user.tenant_id)
    integration_record = queued(integration, "同租户集成")
    with db.get_conn() as conn:
        conn.execute("UPDATE verification_requests SET previous_request_id = ? WHERE id = ?", (foreign["id"], detail["id"]))
        conn.execute("UPDATE verification_requests SET previous_request_id = ? WHERE id = ?", (detail["id"], foreign["id"]))
        conn.execute("UPDATE task_runs SET status = 'failed' WHERE project_id = ?", (detail["project_id"],))
    listing = service.list_verification_requests(user, limit=50, offset=0)
    assert listing["total"] == 2 and {item["id"] for item in listing["items"]} == {detail["id"], successor["id"]}
    pages = [service.list_verification_requests(user, limit=1, offset=index) for index in range(3)]
    assert [page["total"] for page in pages] == [2, 2, 2]
    assert [item["id"] for page in pages for item in page["items"]] == [item["id"] for item in listing["items"]]
    assert service.list_verification_requests(peer, limit=50, offset=0)["total"] == 1
    assert service.list_verification_requests(integration, limit=50, offset=0)["items"][0]["id"] == integration_record["id"]
    visible = service.get_verification_request(detail["id"], user)
    assert visible["previous_request_id"] is None and {item["id"] for item in visible["history"]} == {successor["id"]}
    assert service.get_verification_request(detail["id"], admin)["previous_request_id"] == foreign["id"]
    with db.get_conn() as conn:
        assert conn.execute("SELECT previous_request_id FROM verification_requests WHERE id = ?", (detail["id"],)).fetchone()["previous_request_id"] == foreign["id"]
    material_id = detail["report"]["resource_id"]
    root = f"/api/v1/verification-requests/{detail['id']}"
    for suffix in ("", "/report", f"/materials/{material_id}", f"/materials/{material_id}/preview", f"/artifacts/{artifact.id}", f"/artifacts/{artifact.id}/preview"):
        assert verification_api("GET", root + suffix, headers=headers)[0] == 200
        assert verification_api("GET", root + suffix, headers=peer_headers)[0] == 404
        assert verification_api("GET", root + suffix, headers={"authorization": f"Bearer {token['token']}"})[0] == 404
    for method, suffix in (("POST", "/cancel"), ("DELETE", "")):
        assert verification_api(method, root + suffix, headers=peer_headers)[0] == 404
    assert verification_api("POST", "/api/v1/verification-resource-batches", headers=peer_headers, body={"source_request_id": detail["id"]})[0] == 404
    denied_body = CreateVerificationRequest(schema_version=1, request_id=uuid4(), mode="retest", title="不得复制他人历史", previous_request_id=detail["id"], report={"type": "text", "content": "synthetic"})
    assert verification_api("POST", "/api/v1/verification-requests", headers=peer_headers, body=denied_body.model_dump(mode="json"))[0] == 404
    project = f"/projects/{detail['project_id']}"
    for path in (project, project + "/resources", project + f"/resources/{material_id}", project + "/artifacts",
                 project + f"/artifacts/{artifact.id}/content", project + f"/artifacts/{artifact.id}/preview",
                 project + "/artifacts/archive", project + "/vulnerabilities", project + f"/vulnerabilities/{vulnerability.id}",
                 project + "/monitoring/summary"):
        assert verification_api("GET", path, headers=peer_headers)[0] == 404
    assert verification_api("GET", project, headers=headers)[0] == 200
    assert verification_api("GET", "/projects/ordinary-synthetic", headers=peer_headers)[0] == 200
    own_vulnerabilities = verification_api("GET", "/vulnerabilities", headers=headers)[2]
    peer_vulnerabilities = verification_api("GET", "/vulnerabilities", headers=peer_headers)[2]
    assert {(item["project_id"], item["id"]) for item in own_vulnerabilities} == {(detail["project_id"], vulnerability.id), ("ordinary-synthetic", ordinary.id)}
    assert [(item["project_id"], item["id"]) for item in peer_vulnerabilities] == [("ordinary-synthetic", ordinary.id)]
    own_monitor = verification_api("GET", "/monitoring/summary", headers=headers)
    peer_monitor = verification_api("GET", "/monitoring/summary", headers=peer_headers)
    assert own_monitor[0] == peer_monitor[0] == 200
    assert own_monitor[2]["project_count"] == 3 and peer_monitor[2]["project_count"] == 2
    assert detail["project_id"] in {item["project_id"] for item in own_monitor[2]["recent_failures"]}
    assert not peer_monitor[2]["recent_failures"]
    assert {item["project_id"] for item in peer_monitor[2]["vulnerabilities"]["per_project"]} == {"ordinary-synthetic"}
    # 来源关联只用于列表／history 过滤 fixture，不把文本输入伪装成可读取的平台快照。
    with db.get_conn() as conn:
        for item in (detail, successor, foreign, integration_record):
            conn.execute("UPDATE verification_requests SET source_project_id = ?, source_vulnerability_id = ? WHERE id = ?", (detail["project_id"], vulnerability.id, item["id"]))
    source = f"/api/v1/verification-sources/projects/{detail['project_id']}/vulnerabilities/{vulnerability.id}"
    for suffix in ("/preview", "/history"):
        assert verification_api("GET", source + suffix, headers=peer_headers)[0] == 404
        assert verification_api("GET", source + suffix, headers=headers)[0] == 200
        assert verification_api("GET", source + suffix, headers=admin_headers)[0] == 200
    filtered = service.list_verification_requests(admin, limit=50, offset=0, source_project_id=detail["project_id"], source_vulnerability_id=vulnerability.id)
    assert filtered["total"] == 4
    assert verification_api("GET", source + "/history", headers=admin_headers)[2]["total"] == 4
    cross_tenant = CreateVerificationRequest(schema_version=1, request_id=uuid4(), mode="retest", title="超管只读跨租户来源",
        report={"type": "platform_vulnerability", "project_id": detail["project_id"], "vulnerability_id": vulnerability.id})
    assert verification_api("POST", "/api/v1/verification-requests", headers=admin_headers, body=cross_tenant.model_dump(mode="json"))[0] == 404
    with pytest.raises(HTTPException) as cross_copy:
        service.create_verification_batch(detail["id"], admin)
    assert cross_copy.value.status_code == 404
    with db.get_conn() as conn:
        for item in (detail, successor, foreign, integration_record):
            conn.execute("UPDATE verification_requests SET source_project_id = NULL, source_vulnerability_id = NULL WHERE id = ?", (item["id"],))
    assert verification_api("POST", f"/api/v1/verification-requests/{foreign['id']}/cancel", headers=peer_headers)[0] == 200
    assert verification_api("DELETE", f"/api/v1/verification-requests/{foreign['id']}", headers=peer_headers)[0] == 204
    assert verification_api("POST", f"/api/v1/verification-requests/{successor['id']}/cancel", headers=headers)[0] == 200
    assert verification_api("DELETE", f"/api/v1/verification-requests/{successor['id']}", headers=headers)[0] == 204


@pytest.mark.parametrize("database", ["postgres"], indirect=True)
def test_personal_configuration_preserves_idempotency_first_claim_and_fencing(purge_execution):
    from cairn.server.schemas.models import CreateTenantModelRequest
    from cairn.server.schemas.verification_requests import UpdateVerificationConfiguration
    from cairn.server.services.tenant_models import create_tenant_model
    from cairn.server.services.verification_configuration import update_verification_configuration

    machine = purge_execution[1]
    user, model = _personal_verification_user("binding-owner")
    initial = update_verification_configuration(UpdateVerificationConfiguration(model_id=model.id), user)
    body = CreateVerificationRequest(schema_version=1, request_id=uuid4(), mode="retest", title="首次 claim 才绑定",
        report={"type": "text", "content": "无外部执行的合成材料"})
    detail, replay = service.create_verification_request(body, user)
    assert not replay and detail["execution_binding"] is None
    replacement = create_tenant_model(CreateTenantModelRequest(display_name="Synthetic replacement", protocol="openai-responses",
        base_url="https://unused.example.test", model_name="replacement-model", api_key="synthetic-replacement-secret"), principal=user)
    latest = update_verification_configuration(UpdateVerificationConfiguration(model_id=replacement.id), user)
    assert latest["tenant_worker_id"] == initial["tenant_worker_id"] and latest["revision"] == 2
    claim_body = VerificationClaimRequest(task_run=CreateTaskRunRequest(task_type="external_verify", worker_type="pi", worker_name=initial["tenant_worker_id"],
        session_id=f"pi:{uuid4()}", expected_execution_generation=0, runtime_context={"dispatcher_id": machine.subject_id}))
    claim = service.claim_verification_request(detail["id"], claim_body, machine)
    binding = service.get_verification_request(detail["id"], user)["execution_binding"]
    assert binding["model_id"] == replacement.id and binding["worker_revision"] == 2
    update_verification_configuration(UpdateVerificationConfiguration(model_id=model.id, enabled=False), user)
    assert service.create_verification_request(body, user)[1] is True
    with pytest.raises(HTTPException) as not_ready:
        service.create_verification_request(body.model_copy(update={"request_id": uuid4()}), user)
    assert not_ready.value.status_code == 422
    repeated = service.claim_verification_request(detail["id"], claim_body, machine)
    assert repeated["replayed"] and repeated["claim_token"] == claim["claim_token"]
    assert service.get_verification_request(detail["id"], user)["execution_binding"] == binding
    fence = VerificationExecutionBinding(project_id=detail["project_id"], task_run_id=claim["task_run"]["id"], claim_token=claim["claim_token"], execution_generation=claim["execution_generation"])
    assert service.heartbeat_verification_request(detail["id"], fence, machine)
    for principal, invalid, expected_status in ((machine, fence.model_copy(update={"claim_token": "invalid-synthetic-token"}), 409),
                                                (machine, fence.model_copy(update={"execution_generation": fence.execution_generation + 1}), 409),
                                                (RequestPrincipal(kind="machine", subject_id="other-synthetic-dispatcher"), fence, 403)):
        with pytest.raises(HTTPException) as fenced:
            service.heartbeat_verification_request(detail["id"], invalid, principal)
        assert fenced.value.status_code == expected_status
