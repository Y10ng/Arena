"""Goal-driven 停止只封闭新准入，并允许既有执行严格结算后收敛。"""

from __future__ import annotations

from base64 import b64decode, b64encode
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest
from fastapi import HTTPException

from cairn.server import db
from cairn.server.auth import RequestPrincipal
from cairn.server.models import (
    ConcludeRequest,
    CreateArtifactRequest,
    CreateIntentRequest,
    CreateTaskRunRequest,
    EvidenceRef,
    HeartbeatRequest,
    ReasonClaimRequest,
    UpdateTaskRunRequest,
    VerificationResultInput,
)
from cairn.server.routers import intents as intents_router
from cairn.server.schemas.candidates import (
    CandidateFrameManifest,
    CandidateOutcomeManifest,
    canonical_json_bytes,
    canonical_value_sha256,
)
from cairn.server.services import (
    assessment_reports,
    audit_artifacts,
    audit_task_runs,
    candidate_store,
    project_authorization,
    project_reason,
    project_records,
)
from cairn.server.services.assessment_convergence import advance_goal_driven_convergence
from cairn.server.services.explore_attempts import start_explore_attempt
from cairn.server.services import projects as projects_service
from cairn.server.services.projects import validate_execution_generation
from cairn.server.services.time_utils import utcnow


_DISPATCHER = RequestPrincipal(kind="machine", subject_id="dispatcher")
_USER = RequestPrincipal(kind="user", subject_id="operator")
_SESSION = "pi:11111111-1111-4111-8111-111111111111"
_CAPTURE = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def lifecycle_db(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    db.run_migrations(conn, "sqlite")
    conn.commit()

    @contextmanager
    def same_connection():
        yield conn

    for module in (
        project_records,
        intents_router,
        candidate_store,
        audit_artifacts,
        audit_task_runs,
        project_reason,
        assessment_reports,
    ):
        monkeypatch.setattr(module, "get_conn", same_connection)
    monkeypatch.setattr(
        audit_artifacts,
        "_artifact_disk_path",
        lambda _project_id, _task_run_id, filename: tmp_path / filename,
    )
    yield conn
    conn.close()


def _seed_running_goal_driven_explore(conn: sqlite3.Connection) -> None:
    now = utcnow()
    features = {
        "execution_contract_version": 1,
        "execution_engine": "pi",
        "assessment_protocol": "goal-driven-v1",
        "assessment_contract_version": 2,
        "outcome_contract_version": 2,
        "verifier_contract_version": 1,
        "report_policy_version": "1",
        "report_prompt_group": "goal-driven-v1",
    }
    conn.execute(
        """
        INSERT INTO projects (
            id, title, status, created_at, scenario_key,
            assessment_protocol, scenario_snapshot_json,
            vulnerability_fingerprint_version, explore_budget,
            explore_attempts_started, verification_attempts_started,
            execution_generation
        ) VALUES (
            'p', 'goal-driven', 'active', ?, 'default',
            'goal-driven-v1', ?, 1, 8, 1, 0, 0
        )
        """,
        (
            now,
            json.dumps(
                {
                    "key": "default",
                    "prompt_group": "goal-driven-v1",
                    "profile_id": "default",
                    "features": features,
                }
            ),
        ),
    )
    for fact_id, description in (
        ("origin", "authorized target"),
        ("goal", "verify access boundaries"),
        ("f1", "bootstrap accepted"),
    ):
        conn.execute(
            """
            INSERT INTO facts (id, project_id, description, creator, created_at)
            VALUES (?, 'p', ?, 'system', ?)
            """,
            (fact_id, description, now),
        )
    conn.execute(
        """
        INSERT INTO intents (
            id, project_id, description, creator, worker,
            last_heartbeat_at, created_at, assessment_role, priority
        ) VALUES (
            'i1', 'p', 'test the accepted target', 'reasoner', 'worker',
            ?, ?, 'discovery', 1
        )
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO intent_sources (intent_id, project_id, fact_id, source_order)
        VALUES ('i1', 'p', 'f1', 0)
        """
    )
    conn.execute(
        """
        INSERT INTO assessment_attempts (
            id, project_id, intent_id, assessment_role, status, charged,
            execution_count, max_runtime_retries, strategy_key, started_at
        ) VALUES (
            'attempt-1', 'p', 'i1', 'discovery', 'active', 1,
            1, 2, '', ?
        )
        """,
        (now,),
    )
    delivery = {
        "session_id": _SESSION,
        "phase": "explore_execute",
        "deadline_at": "2099-01-01T00:00:00Z",
        "container_id": "container-p",
        "model_binding_sha256": "a" * 64,
        "result_schema_sha256": "b" * 64,
        "binding": None,
        "inputs": {},
    }
    runtime_context = {
        "dispatcher_id": "dispatcher",
        "execution_delivery": delivery,
        "execution_lifecycle": {
            "generation": 0,
            "process_started": True,
            "termination_confirmed": False,
            "cleanup_confirmed": False,
        },
    }
    conn.execute(
        """
        INSERT INTO task_runs (
            id, project_id, intent_id, task_type, worker_name, worker_type,
            status, session_id, assessment_attempt_id, runtime_context_json,
            execution_generation, started_at
        ) VALUES (
            'run-1', 'p', 'i1', 'explore', 'worker', 'pi',
            'running', ?, 'attempt-1', ?, 0, ?
        )
        """,
        (_SESSION, json.dumps(runtime_context, sort_keys=True), now),
    )
    project_authorization.initialize_project_authorization(
        conn,
        "p",
        actor_id="operator",
        authorized_at=now,
    )
    conn.commit()


def _candidate_manifest(raw: bytes) -> CandidateFrameManifest:
    return CandidateFrameManifest(
        protocol="cairn-candidate-frame-v1",
        record_kind="candidate",
        capture_id=_CAPTURE,
        producer_candidate_key=f"{_CAPTURE}:1:0",
        parent_producer_candidate_key=None,
        assistant_turn=1,
        content_index=0,
        project_id="p",
        task_run_id="run-1",
        phase="explore_execute",
        session=_SESSION,
        binding_sha256=canonical_value_sha256(None),
        result_schema_sha256="b" * 64,
        raw_sha256=sha256(raw).hexdigest(),
        raw_size_bytes=len(raw),
        artifacts=[],
    )


def _outcome_manifest() -> CandidateOutcomeManifest:
    return CandidateOutcomeManifest(
        protocol="cairn-candidate-frame-v1",
        record_kind="outcome",
        capture_id=_CAPTURE,
        producer_candidate_key=f"{_CAPTURE}:1:0",
        outcome_sequence=1,
        code="locally_validated",
        path=None,
        keyword=None,
        count=1,
        artifact_status="validated",
        binding_status="not_required",
    )


def _project_state(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT * FROM projects WHERE id = 'p'").fetchone()


def _retry_explore(conn: sqlite3.Connection):
    return start_explore_attempt(
        conn, "p", "i1",
        CreateTaskRunRequest(
            expected_execution_generation=0, intent_id="i1", task_type="explore",
            worker_name="worker", worker_type="pi", status="running",
            runtime_context={"dispatcher_id": "dispatcher"},
        ),
        principal=_DISPATCHER,
    )


def _fail_explore(conn: sqlite3.Connection, run_id: str, error_type: str) -> None:
    audit_task_runs.update_task_run_record(
        "p", run_id,
        UpdateTaskRunRequest(status="failed", error_type=error_type),
        principal=_DISPATCHER,
    )
    conn.execute(
        "UPDATE intents SET worker=NULL, last_heartbeat_at=NULL "
        "WHERE project_id='p' AND id='i1'"
    )
    conn.commit()


def test_provider_explore_retries_keep_one_attempt_candidate_and_charge_history(lifecycle_db, monkeypatch):
    from cairn.server.services import assessment_attempts, explore_attempts

    conn = lifecycle_db
    _seed_running_goal_driven_explore(conn)
    conn.execute(
        "UPDATE projects SET explore_budget=40, explore_attempts_started=45 WHERE id='p'"
    )
    conn.execute("UPDATE assessment_attempts SET max_runtime_retries=10 WHERE id='attempt-1'")
    conn.commit()
    before = dict(conn.execute(
        "SELECT authorization_epoch, authorization_scope_digest, execution_generation, "
        "explore_budget, explore_attempts_started, verification_attempts_started "
        "FROM projects WHERE id='p'"
    ).fetchone())
    raw = b'{"protocol":"cairn-agent-result-v1"}'
    manifest = _candidate_manifest(raw)
    candidate_store.capture_candidate(
        "p", "run-1", manifest,
        canonical_json_bytes(manifest.model_dump(mode="json")), raw, principal=_DISPATCHER,
    )
    outcome = _outcome_manifest()
    candidate_store.capture_candidate_outcome(
        "p", "run-1", outcome,
        canonical_json_bytes(outcome.model_dump(mode="json")), b"", principal=_DISPATCHER,
    )
    assert conn.execute("SELECT COUNT(*) FROM facts WHERE project_id='p'").fetchone()[0] == 3

    clock = (datetime.now(timezone.utc) + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(assessment_attempts, "utcnow", lambda: clock)
    monkeypatch.setattr(explore_attempts, "utcnow", lambda: clock)
    run_id = "run-1"
    for failures in range(1, 11):
        _fail_explore(conn, run_id, "provider_error" if failures % 2 else "provider_rate_limit")
        attempt = conn.execute("SELECT * FROM assessment_attempts WHERE id='attempt-1'").fetchone()
        assert attempt["status"] == "active"
        assert attempt["execution_count"] == failures
        assert conn.execute("SELECT concluded_at FROM intents WHERE id='i1'").fetchone()[0] is None
        next_run = _retry_explore(conn)
        assert next_run.assessment_attempt_id == "attempt-1"
        assert next_run.execution_generation == 0
        assert next_run.session_id is None
        assert "delivery_retry" not in next_run.runtime_context
        assert "execution_delivery" not in next_run.runtime_context
        if failures == 1:
            new_session = "pi:33333333-3333-4333-8333-333333333333"
            audit_task_runs.update_task_run_record(
                "p", next_run.id, UpdateTaskRunRequest(session_id=new_session),
                principal=_DISPATCHER,
            )
            assert new_session != _SESSION
        run_id = next_run.id
    assert conn.execute("SELECT execution_count FROM assessment_attempts WHERE id='attempt-1'").fetchone()[0] == 11
    _fail_explore(conn, run_id, "provider_error")
    attempt = conn.execute("SELECT status, outcome FROM assessment_attempts WHERE id='attempt-1'").fetchone()
    assert dict(attempt) == {"status": "failed", "outcome": "runtime_retry_exhausted:provider_error"}
    with pytest.raises(HTTPException) as exhausted:
        _retry_explore(conn)
    assert exhausted.value.detail in {"Intent already concluded", "assessment_attempt_already_closed"}
    assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE project_id='p' AND intent_id='i1'").fetchone()[0] == 11
    assert conn.execute("SELECT COUNT(*) FROM facts WHERE project_id='p'").fetchone()[0] == 3
    assert dict(conn.execute(
        "SELECT authorization_epoch, authorization_scope_digest, execution_generation, "
        "explore_budget, explore_attempts_started, verification_attempts_started "
        "FROM projects WHERE id='p'"
    ).fetchone()) == before
    assert conn.execute("SELECT COUNT(*) FROM assessment_attempts WHERE project_id='p'").fetchone()[0] == 1


@pytest.mark.parametrize("transient", ["provider_error", "transport_error"])
def test_goal_driven_original_delivery_transient_keeps_attempt_and_frozen_inputs(lifecycle_db, monkeypatch, transient):
    from cairn.server.services import assessment_attempts, explore_attempts

    conn = lifecycle_db
    _seed_running_goal_driven_explore(conn)
    clock = ["2050-01-01T00:00:00Z"]
    for module in (assessment_attempts, explore_attempts, audit_task_runs):
        monkeypatch.setattr(module, "utcnow", lambda: clock[0])
    snapshot = json.loads(conn.execute("SELECT runtime_context_json FROM task_runs WHERE id='run-1'").fetchone()[0])["execution_delivery"]
    run_id = "run-1"
    for index, error in enumerate(("submission_missing", transient)):
        _fail_explore(conn, run_id, error)
        audit_task_runs.update_task_run_record("p", run_id, UpdateTaskRunRequest(runtime_context={"execution_lifecycle": {
            "generation": 0, "process_started": True, "termination_confirmed": True, "cleanup_confirmed": True,
        }}), principal=_DISPATCHER)
        clock[0] = f"2050-01-01T00:0{index + 1}:00Z"
        next_run = _retry_explore(conn)
        assert next_run.assessment_attempt_id == "attempt-1"
        assert next_run.session_id == _SESSION
        assert next_run.worker_name == "worker"
        assert next_run.execution_generation == 0
        assert next_run.runtime_context["dispatcher_id"] == "dispatcher"
        assert next_run.runtime_context["execution_delivery"] == snapshot
        assert next_run.runtime_context["delivery_retry"] == {"source_task_run_id": run_id}
        run_id = next_run.id
    assert dict(conn.execute("SELECT charged, execution_count, status FROM assessment_attempts WHERE id='attempt-1'").fetchone()) == {"charged": 1, "execution_count": 3, "status": "active"}
    assert conn.execute("SELECT COUNT(*) FROM assessment_attempts WHERE project_id='p'").fetchone()[0] == 1
    assert tuple(conn.execute("SELECT explore_attempts_started, verification_attempts_started FROM projects WHERE id='p'").fetchone()) == (1, 0)
    assert conn.execute("SELECT concluded_at FROM intents WHERE id='i1'").fetchone()[0] is None


def test_goal_driven_artifact_rejection_after_provider_retry_remains_terminal(lifecycle_db, monkeypatch):
    from cairn.server.services import assessment_attempts, explore_attempts

    conn = lifecycle_db
    _seed_running_goal_driven_explore(conn)
    clock = ["2050-01-01T00:00:00Z"]
    for module in (assessment_attempts, explore_attempts, audit_task_runs):
        monkeypatch.setattr(module, "utcnow", lambda: clock[0])
    _fail_explore(conn, "run-1", "provider_error")
    clock[0] = "2050-01-01T00:01:00Z"
    retried = _retry_explore(conn)
    _fail_explore(conn, retried.id, "artifact_error")
    assert dict(conn.execute("SELECT status, outcome, execution_count FROM assessment_attempts WHERE id='attempt-1'").fetchone()) == {"status": "failed", "outcome": "artifact_error", "execution_count": 2}
    with pytest.raises(HTTPException):
        _retry_explore(conn)
    assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE project_id='p'").fetchone()[0] == 2
    assert conn.execute("SELECT explore_attempts_started FROM projects WHERE id='p'").fetchone()[0] == 1


def test_fixed_material_versions_and_identity_repair_reach_server(lifecycle_db, tmp_path):
    from cairn.dispatcher import contracts
    from cairn.dispatcher.protocol.client import ApiResult
    from cairn.dispatcher.tasks.artifact_records import record_declared_artifacts
    from cairn.dispatcher.tasks.candidate_intake import decode_candidate_spool
    from cairn.dispatcher.tasks.conclude_writer import build_assessment_record_for_conclude, build_vulnerability_for_conclude
    from cairn.dispatcher.worker_protocol.submission import build_submit_result_schema
    from cairn.dispatcher.worker_result_gateway import decode_pi_result
    from cairn.dispatcher.workers.base import WorkerCandidateSpool

    conn = lifecycle_db
    _seed_running_goal_driven_explore(conn)
    before = dict(conn.execute(
        "SELECT authorization_epoch, authorization_scope_digest, execution_generation, "
        "explore_budget, explore_attempts_started, verification_attempts_started "
        "FROM projects WHERE id='p'"
    ).fetchone())
    binding = {"assessment_protocol": "goal-driven-v1"}
    schema = build_submit_result_schema("explore_execute", binding)
    context = json.loads(conn.execute("SELECT runtime_context_json FROM task_runs WHERE id='run-1'").fetchone()[0])
    context["execution_delivery"].update(binding=binding, result_schema_sha256=canonical_value_sha256(schema))
    conn.execute("UPDATE task_runs SET runtime_context_json=? WHERE id='run-1'", (json.dumps(context),))
    conn.commit()
    # 只替代 provider 与容器 IO；真实 SDK 的捕获、生产解码、持久化及业务接受不替代。
    script = r"""
import { readFile, writeFile, appendFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fixture } from './tests/fixtures.mjs';
import { runRequest } from './dist/runner.js';
import { parseRequest } from './dist/protocol.js';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const schema = JSON.parse(input);
const cleanups = [];
let cwd;
let registered;
let rejection;
const original = '合成观察：registered evidence version 1。\r\n';
const identity = { vulnerability_class: 'synthetic_access_boundary', asset: { host: 'fixture.invalid', protocol: 'http' }, entrypoint: { method: 'GET', route: '/synthetic-evidence' } };
const submit = (withIdentity) => ({ tools: [{ name: 'submit_result', args: {
  accepted: true,
  data: {
    fact: { description: '合成已有材料的交付，不主张真实漏洞', vulnerability: {
      title: '合成材料接受测试', severity: 'low',
      repro_request: 'GET /synthetic-evidence HTTP/1.1\r\nHost: fixture.invalid\r\n\r\n',
      repro_response: 'HTTP/1.1 200 OK\r\n\r\nsynthetic evidence',
      ...(withIdentity ? { identity } : {}),
    } },
    assessment_record: {
      version: 1, kind: 'experiment', hypothesis: '已有合成观察能否按固定版本可靠交付？',
      outcome: 'finding', observations: [original], evidence_refs: registered.map((item) => ({ artifact_handle: item.handle })),
    },
  },
} }] });
try {
  const f = await fixture({ after: (cleanup) => cleanups.push(cleanup) }, 'openai-chat-completions', [
    { tools: [
      ...Array.from({ length: 20 }, (_, index) => ({ name: 'register_artifact', args: { path: `evidence-${index}.txt`, kind: 'raw_output' } })),
      { name: 'bash', args: { command: `node -e "process.stdout.write(('fixture-output-'+String.fromCharCode(10)).repeat(30000))"` } },
    ] },
    async (call) => {
      registered = call.body.messages.filter((message) => message.role === 'tool').slice(0, 20).map((message) => JSON.parse(message.content));
      await appendFile(join(cwd, 'evidence-0.txt'), 'continued background output');
      return submit(false);
    },
    (call) => {
      rejection = JSON.parse(call.body.messages.filter((message) => message.role === 'tool').at(-1).content);
      return submit(true);
    },
  ]);
  cwd = f.cwd;
  for (let index = 0; index < 20; index += 1) await writeFile(join(cwd, `evidence-${index}.txt`), index === 0 ? original : `synthetic evidence ${index}`);
  const request = parseRequest({ ...f.request, project_id: 'p', task_run_id: 'run-1',
    session: process.argv[1], result_schema: schema, binding: { assessment_protocol: 'goal-driven-v1' } });
  const audit = [];
  const envelope = await runRequest(request, { audit: (event) => audit.push(event) });
  const materials = {};
  for (const item of envelope.result?.data.artifacts ?? []) materials[item.path] = (await readFile(item.path)).toString('base64');
  process.stdout.write(JSON.stringify({
    raw: JSON.stringify(envelope), registered, rejection, cwd, materials,
    source: await readFile(join(cwd, 'evidence-0.txt'), 'utf8'),
    spool_base64: (await readFile(request.capabilities.candidate_capture.spool_path)).toString('base64'),
    capture: request.capabilities.candidate_capture, binding: request.binding, audit,
    tool_scopes: f.calls.map((call) => call.body.tools.map((tool) => tool.function.name).sort()),
  }));
} finally {
  for (const cleanup of cleanups.reverse()) await cleanup();
}
"""
    runner = subprocess.run(
        ["node", "--input-type=module", "-e", script, _SESSION],
        cwd=Path(__file__).resolve().parents[2] / "container" / "pi-agent",
        input=json.dumps(schema, ensure_ascii=False), text=True, capture_output=True, timeout=30,
    )
    assert runner.returncode == 0, runner.stderr
    captured = json.loads(runner.stdout)
    envelope = json.loads(captured["raw"])
    assert envelope["status"] == "succeeded", envelope
    registered = captured["registered"]
    assert len(registered) == 20
    declarations = envelope["result"]["data"]["artifacts"]
    assert len(declarations) == 21
    materials = {path: b64decode(raw, validate=True) for path, raw in captured["materials"].items()}
    first = registered[0]
    material = materials[first["path"]]
    assert material == "合成观察：registered evidence version 1。\r\n".encode()
    assert captured["source"] == material.decode() + "continued background output"
    assert first["path"] != first["source_path"]
    for item in declarations:
        assert item["sha256"] == sha256(materials[item["path"]]).hexdigest()
        assert item["size_bytes"] == len(materials[item["path"]])
    assert declarations[:20] == registered
    assert any(event.get("source") == "sdk_bash_overflow" for event in captured["audit"])
    assert captured["rejection"]["code"] == "submission_invalid"
    assert any(
        rule.get("path") == "/data/fact/vulnerability"
        and any("identity" in alternative.get("required", []) for alternative in rule.get("alternatives", []))
        for rule in captured["rejection"]["issue"]["rules"]
    ), captured["rejection"]["issue"]
    assert not Path(captured["cwd"]).exists()
    assert not Path(captured["capture"]["spool_path"]).exists()
    assert len(captured["tool_scopes"]) == 3
    assert "bash" in captured["tool_scopes"][0]
    assert captured["tool_scopes"][2] == ["read_evidence", "register_artifact", "submit_result"]
    assert {event["session"] for event in captured["audit"]} == {_SESSION}
    assert {event["task_run_id"] for event in captured["audit"]} == {"run-1"}
    repairs = [event for event in captured["audit"] if event["event"] == "result_repair"]
    assert [event["code"] for event in repairs] == ["submission_invalid"]

    capture = captured["capture"]
    entries = decode_candidate_spool(
        b64decode(captured["spool_base64"], validate=True),
        WorkerCandidateSpool(
            path=capture["spool_path"], capture_id=capture["capture_id"],
            binding_sha256=canonical_value_sha256(captured["binding"]),
            result_schema_sha256=canonical_value_sha256(schema),
        ),
        project_id="p", task_run_id="run-1", phase="explore_execute", session=_SESSION,
    )
    candidates = [entry for entry in entries if entry.record_kind == "candidate"]
    assert len(candidates) == 2
    identities = sorted([{key: item[key] for key in ("handle", "kind", "sha256", "size_bytes")} for item in declarations], key=lambda item: item["handle"])
    for index, candidate in enumerate(candidates):
        manifest = json.loads(candidate.manifest_bytes)
        assert manifest["artifacts"] == identities
        assert len(candidate.manifest_bytes) > 4 * 1024
        assert ("identity" in json.loads(candidate.raw)["data"]["fact"]["vulnerability"]) == (index == 1)
        outcomes = [json.loads(entry.manifest_bytes) for entry in entries
                    if entry.record_kind == "outcome" and entry.producer_candidate_key == candidate.producer_candidate_key]
        assert {(outcome["code"], outcome["artifact_status"]) for outcome in outcomes} == {
            ("submission_invalid", "not_evaluated") if index == 0 else ("locally_validated", "validated")
        }
    assert json.loads(candidates[1].manifest_bytes)["parent_producer_candidate_key"] == candidates[0].producer_candidate_key
    for entry in entries:
        manifest = json.loads(entry.manifest_bytes)
        if entry.record_kind == "candidate":
            receipt = candidate_store.capture_candidate(
                "p", "run-1", CandidateFrameManifest.model_validate(manifest), entry.manifest_bytes,
                entry.raw, principal=_DISPATCHER,
            )
            replay = candidate_store.capture_candidate(
                "p", "run-1", CandidateFrameManifest.model_validate(manifest), entry.manifest_bytes,
                entry.raw, principal=_DISPATCHER,
            )
        else:
            receipt = candidate_store.capture_candidate_outcome(
                "p", "run-1", CandidateOutcomeManifest.model_validate(manifest), entry.manifest_bytes,
                entry.raw, principal=_DISPATCHER,
            )
            replay = candidate_store.capture_candidate_outcome(
                "p", "run-1", CandidateOutcomeManifest.model_validate(manifest), entry.manifest_bytes,
                entry.raw, principal=_DISPATCHER,
            )
        assert receipt.disposition == "received" and replay.disposition == "replayed"
        assert receipt.request_sha256 == replay.request_sha256
    saved_candidates = conn.execute("SELECT artifact_count, raw_bytes, manifest_json FROM task_run_candidates WHERE task_run_id='run-1' ORDER BY assistant_turn").fetchall()
    assert len(saved_candidates) == 2
    for saved, candidate in zip(saved_candidates, candidates, strict=True):
        assert saved["artifact_count"] == 21
        assert bytes(saved["raw_bytes"]) == candidate.raw
        assert saved["manifest_json"].encode() == candidate.manifest_bytes

    decoded = decode_pi_result(
        captured["raw"], phase="explore_execute", project_id="p", task_run_id="run-1",
        session=_SESSION, assessment_protocol="goal-driven-v1",
        validator=lambda payload: contracts.validate_explore_payload(
            payload, goal_driven=True, strict_contract=True, expected_kind="explore_result",
            outcome_contract_version=2,
        ),
    )
    assert decoded.succeeded, decoded.failure
    assert envelope["status"] == "succeeded"
    artifacts = contracts.extract_declared_artifacts(decoded.payload)
    assert [(artifact.handle, artifact.path, artifact.kind) for artifact in artifacts] == [
        (item["handle"], item["path"], item["kind"]) for item in declarations
    ]
    reads = []
    stored = []

    class MaterialIO:
        def read_file(self, container_name, path):
            assert container_name == "isolated-container"
            reads.append(path)
            return materials[path]

    class ArtifactIO:
        def create_artifact(self, project_id, *, task_run_id, kind, content, filename, metadata):
            artifact = audit_artifacts.create_artifact_record(
                project_id, CreateArtifactRequest(
                    task_run_id=task_run_id, kind=kind,
                    content_base64=b64encode(content.read()).decode("ascii"),
                    filename=filename, metadata=metadata,
                ),
            )
            stored.append(artifact)
            return ApiResult(status_code=201, data=artifact.model_dump(mode="json"))

    declared = record_declared_artifacts(
        ArtifactIO(), MaterialIO(), "isolated-container", "p", "run-1",
        phase="explore_execute", artifacts=artifacts,
    )
    assert not declared.collect_failed and not declared.write_failed
    assert reads == [item["path"] for item in declarations]
    assert len(stored) == 21
    for artifact, item in zip(stored, declarations, strict=True):
        assert (artifact.project_id, artifact.task_run_id, artifact.kind) == ("p", "run-1", "raw_output")
        assert artifact.sha256 == item["sha256"] == sha256(materials[item["path"]]).hexdigest()
        assert artifact.size == item["size_bytes"] == len(materials[item["path"]])
        assert (tmp_path / artifact.uri.rsplit("/", 1)[1]).read_bytes() == materials[item["path"]]
        assert artifact.metadata["source_path"] == item["source_path"]
        assert artifact.metadata["registered_path"] == item["path"]
        assert declared.artifact_ids_by_handle[item["handle"]] == artifact.id
    artifact = stored[0]
    assert dict(conn.execute(
        "SELECT project_id, task_run_id, sha256, size FROM artifacts WHERE id=?", (artifact.id,),
    ).fetchone()) == {"project_id": "p", "task_run_id": "run-1", "sha256": first["sha256"], "size": len(material)}
    # 上传前副本再被修改也不能写入；Server 同样核对实际 bytes，不相信元数据自报。
    materials[first["path"]] = b"tampered after local validation"
    rejected = record_declared_artifacts(ArtifactIO(), MaterialIO(), "isolated-container", "p", "run-1", phase="explore_execute", artifacts=artifacts[:1])
    assert rejected.collect_failed and len(stored) == 21
    with pytest.raises(HTTPException) as mismatch:
        audit_artifacts.create_artifact_record("p", CreateArtifactRequest(task_run_id="run-1", kind="raw_output", content="tampered", metadata={"declared_by_worker": True, "registered_sha256": first["sha256"], "registered_size_bytes": first["size_bytes"]}))
    assert mismatch.value.status_code == 422

    wire, assessment_refs = build_assessment_record_for_conclude(decoded.payload, declared)
    assert wire["evidence_refs"] == [{"artifact_id": declared.artifact_ids_by_handle[item["handle"]], "pointer": ""} for item in registered]
    vulnerability = build_vulnerability_for_conclude(decoded.payload, declared, require_current_run_reproduction=True)
    body = ConcludeRequest(
        worker="worker", task_run_id="run-1", description=decoded.description,
        evidence=declared.refs + assessment_refs, assessment_record=wire, vulnerability=vulnerability,
    )
    accepted = intents_router.conclude("p", "i1", body)
    assert accepted.fact.task_run_id == "run-1"
    assert accepted.fact.description == decoded.description
    assert accepted.fact.assessment_record["outcome"] == "finding"
    assert accepted.fact.assessment_record["observations"] == [material.decode()]
    assert accepted.fact.assessment_record["evidence_refs"] == wire["evidence_refs"]
    fact = conn.execute("SELECT * FROM facts WHERE id=?", (accepted.fact.id,)).fetchone()
    assert fact["task_run_id"] == "run-1"
    assert json.loads(fact["assessment_record_json"]) == accepted.fact.assessment_record
    from cairn.server.services.vulnerability_identity import canonicalize_vulnerability_identity, vulnerability_fingerprint

    expected_identity = canonicalize_vulnerability_identity(vulnerability["identity"], version=1)
    saved_vulnerability = conn.execute("SELECT * FROM vulnerabilities WHERE source_fact_id=?", (accepted.fact.id,)).fetchone()
    assert saved_vulnerability["status"] == "new"
    assert json.loads(saved_vulnerability["identity_json"]) == expected_identity
    assert saved_vulnerability["fingerprint"] == vulnerability_fingerprint(expected_identity)
    observation = conn.execute("SELECT * FROM vulnerability_observations WHERE source_fact_id=?", (accepted.fact.id,)).fetchone()
    assert (observation["task_run_id"], observation["worker"], observation["vulnerability_id"]) == ("run-1", "worker", saved_vulnerability["id"])
    assert json.loads(observation["identity_json"]) == expected_identity
    assert (observation["repro_request"], observation["repro_response"]) == (vulnerability["repro_request"], vulnerability["repro_response"])
    evidence = conn.execute(
        "SELECT artifact_id, pointer FROM fact_evidence WHERE project_id='p' AND fact_id=?",
        (accepted.fact.id,),
    ).fetchall()
    assert {(row["artifact_id"], row["pointer"]) for row in evidence} == {
        (declared.artifact_ids_by_handle[item["handle"]], item["handle"]) for item in declarations
    } | {(declared.artifact_ids_by_handle[item["handle"]], "") for item in registered}
    assert dict(conn.execute(
        "SELECT status, outcome, charged, execution_count FROM assessment_attempts WHERE id='attempt-1'"
    ).fetchone()) == {"status": "completed", "outcome": "finding", "charged": 1, "execution_count": 1}
    run = conn.execute("SELECT * FROM task_runs WHERE id='run-1'").fetchone()
    assert (run["session_id"], run["assessment_attempt_id"], run["execution_generation"]) == (_SESSION, "attempt-1", 0)
    assert json.loads(run["runtime_context_json"])["accepted_fact_id"] == accepted.fact.id

    # ACK 采用真实 Runner 终态；不靠 SQL 改状态来证明 Server 已接受材料。
    ack = audit_task_runs.update_task_run_record(
        "p", "run-1", UpdateTaskRunRequest(status=envelope["status"], exit_code=runner.returncode,
            runtime_context={"execution_lifecycle": {
                "generation": run["execution_generation"], "process_started": True,
                "termination_confirmed": True, "cleanup_confirmed": True,
            }}), principal=_DISPATCHER,
    )
    assert ack.status == envelope["status"]
    replay = intents_router.conclude("p", "i1", body)
    assert replay.fact == accepted.fact
    assert conn.execute("SELECT COUNT(*) FROM vulnerability_observations WHERE source_fact_id=?", (accepted.fact.id,)).fetchone()[0] == 1
    with pytest.raises(HTTPException):
        _retry_explore(conn)
    assert tuple(conn.execute(
        "SELECT (SELECT COUNT(*) FROM facts WHERE project_id='p'), "
        "(SELECT COUNT(*) FROM task_runs WHERE project_id='p'), "
        "(SELECT COUNT(*) FROM assessment_attempts WHERE project_id='p'), "
        "(SELECT COUNT(*) FROM artifacts WHERE project_id='p')"
    ).fetchone()) == (4, 1, 1, 21)
    assert conn.execute("SELECT charged, execution_count FROM assessment_attempts WHERE id='attempt-1'").fetchone()[:] == (1, 1)
    assert dict(conn.execute(
        "SELECT authorization_epoch, authorization_scope_digest, execution_generation, "
        "explore_budget, explore_attempts_started, verification_attempts_started "
        "FROM projects WHERE id='p'"
    ).fetchone()) == before
    assert before["explore_attempts_started"] == 1

    followup = intents_router.create_intent(
        "p", CreateIntentRequest(from_=[accepted.fact.id], description="基于合成观察研究另一个问题"),
        principal=_USER,
    )
    assert followup.from_ == [accepted.fact.id]
    next_run = start_explore_attempt(
        conn, "p", followup.id,
        CreateTaskRunRequest(
            expected_execution_generation=0, intent_id=followup.id, task_type="explore",
            worker_name="worker", worker_type="pi", status="running",
            runtime_context={"dispatcher_id": "dispatcher"},
        ), principal=_DISPATCHER,
    )
    assert next_run.id != "run-1"
    assert next_run.assessment_attempt_id != "attempt-1"
    assert next_run.intent_id == followup.id
    assert next_run.execution_generation == before["execution_generation"]
    assert conn.execute("SELECT explore_attempts_started FROM projects WHERE id='p'").fetchone()[0] == 2
    assert conn.execute("SELECT execution_count FROM assessment_attempts WHERE id='attempt-1'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM facts WHERE task_run_id='run-1'").fetchone()[0] == 1


@pytest.mark.parametrize("error_type", [
    "provider_auth", "request_contract", "result_schema", "session_error", "transport_error", "artifact_error",
])
def test_nonretryable_explore_failure_closes_attempt(lifecycle_db, error_type):
    conn = lifecycle_db
    _seed_running_goal_driven_explore(conn)
    conn.execute("UPDATE assessment_attempts SET max_runtime_retries=10 WHERE id='attempt-1'")
    conn.commit()
    _fail_explore(conn, "run-1", error_type)
    assert dict(conn.execute(
        "SELECT status, outcome FROM assessment_attempts WHERE id='attempt-1'"
    ).fetchone()) == {"status": "failed", "outcome": error_type}
    with pytest.raises(HTTPException):
        _retry_explore(conn)
    assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE project_id='p'").fetchone()[0] == 1


@pytest.mark.parametrize("acceptance", ["fact", "receipt"])
def test_accepted_explore_result_prevents_fresh_execution(lifecycle_db, monkeypatch, acceptance):
    from cairn.server.services import assessment_attempts, explore_attempts

    conn = lifecycle_db
    _seed_running_goal_driven_explore(conn)
    _fail_explore(conn, "run-1", "provider_error")
    if acceptance == "fact":
        conn.execute(
            "INSERT INTO facts (id, project_id, description, creator, created_at, task_run_id) "
            "VALUES ('accepted-1', 'p', 'accepted observation', 'worker', ?, 'run-1')",
            (utcnow(),),
        )
    else:
        row = conn.execute("SELECT runtime_context_json FROM task_runs WHERE id='run-1'").fetchone()
        context = json.loads(row["runtime_context_json"])
        context["accepted_result_digest"] = "accepted-server-receipt"
        conn.execute("UPDATE task_runs SET runtime_context_json=? WHERE id='run-1'", (json.dumps(context),))
    conn.commit()
    clock = (datetime.now(timezone.utc) + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(assessment_attempts, "utcnow", lambda: clock)
    monkeypatch.setattr(explore_attempts, "utcnow", lambda: clock)
    project = conn.execute("SELECT * FROM projects WHERE id='p'").fetchone()
    assert assessment_attempts.execution_retry_eligibility(
        conn, project, "explore", intent_id="i1",
    )["reason"] == "execution_retry_blocked"
    with pytest.raises(HTTPException) as rejected:
        _retry_explore(conn)
    assert rejected.value.detail == "assessment_result_already_accepted"
    assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE project_id='p'").fetchone()[0] == 1
    assert conn.execute("SELECT execution_count FROM assessment_attempts WHERE id='attempt-1'").fetchone()[0] == 1


def test_closed_provider_attempts_never_reopen_or_recharge(lifecycle_db):
    conn = lifecycle_db
    _seed_running_goal_driven_explore(conn)
    for index, intent_id in enumerate(("i004", "i013"), 1):
        conn.execute(
            "INSERT INTO intents (id, project_id, description, creator, created_at, assessment_role) "
            "VALUES (?, 'p', 'closed provider failure', 'reasoner', ?, 'discovery')",
            (intent_id, utcnow()),
        )
        conn.execute(
            "INSERT INTO assessment_attempts "
            "(id, project_id, intent_id, assessment_role, status, charged, "
            "execution_count, max_runtime_retries, started_at, ended_at, outcome) "
            "VALUES (?, 'p', ?, 'discovery', 'failed', 1, 1, 2, ?, ?, 'provider_error')",
            (f"closed-{index}", intent_id, utcnow(), utcnow()),
        )
    conn.commit()
    before = conn.execute(
        "SELECT explore_attempts_started FROM projects WHERE id='p'"
    ).fetchone()[0]
    for intent_id in ("i004", "i013"):
        with pytest.raises(HTTPException) as rejected:
            start_explore_attempt(
                conn, "p", intent_id,
                CreateTaskRunRequest(
                    expected_execution_generation=0, intent_id=intent_id, task_type="explore",
                    worker_name="worker", worker_type="pi", status="running",
                    runtime_context={"dispatcher_id": "dispatcher"},
                ), principal=_DISPATCHER,
            )
        assert rejected.value.detail == "assessment_attempt_already_closed"
        assert conn.execute(
            "SELECT COUNT(*) FROM assessment_attempts WHERE project_id='p' AND intent_id=?",
            (intent_id,),
        ).fetchone()[0] == 1
    assert conn.execute("SELECT explore_attempts_started FROM projects WHERE id='p'").fetchone()[0] == before
    assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE project_id='p'").fetchone()[0] == 1


def test_lease_uses_elapsed_boot_time_not_wall_clock(lifecycle_db, monkeypatch):
    conn = lifecycle_db
    _seed_running_goal_driven_explore(conn)
    clock = ["boot-a", 100_000_000_000]
    monkeypatch.setattr(projects_service, "lease_clock", lambda: tuple(clock))
    monkeypatch.setattr(project_reason, "lease_clock", lambda: tuple(clock))
    monkeypatch.setattr(intents_router, "lease_clock", lambda: tuple(clock))
    conn.execute(
        "UPDATE projects SET authorization_expires_at = '2099-01-01T00:00:00Z' WHERE id = 'p'"
    )

    # 迁移前的租约没有可比较刻度；先观察，再让同一个 Run 续租。
    projects_service.expire_workers(conn, "p")
    conn.execute("UPDATE intents SET last_heartbeat_at = '1900-01-01T00:00:00Z' WHERE id = 'i1'")
    clock[1] += 300_000_000
    projects_service.expire_workers(conn, "p")
    assert intents_router.heartbeat(
        "p", "i1", HeartbeatRequest(worker="worker", task_run_id="run-1")
    ).worker == "worker"
    assert conn.execute("SELECT status FROM task_runs WHERE id = 'run-1'").fetchone()[0] == "running"
    assert conn.execute("SELECT authorization_expires_at FROM projects WHERE id = 'p'").fetchone()[0] == "2099-01-01T00:00:00Z"

    # Reason 使用相同的计时基准；跨 boot 的旧刻度只在新 boot 首次观察时重锚。
    conn.execute(
        """UPDATE projects SET reason_worker = 'reasoner', reason_last_heartbeat_at = '1900-01-01T00:00:00Z',
               reason_heartbeat_boot_id = 'boot-a', reason_heartbeat_boottime_ns = ? WHERE id = 'p'""",
        (clock[1],),
    )
    clock[0] = "boot-b"
    clock[1] = 20_000_000_000
    projects_service.expire_workers(conn, "p")
    projects_service.expire_reason_leases(conn, "p")
    assert conn.execute("SELECT worker FROM intents WHERE id = 'i1'").fetchone()[0] == "worker"
    assert conn.execute("SELECT reason_worker FROM projects WHERE id = 'p'").fetchone()[0] == "reasoner"

    clock[1] += 181_000_000_000
    projects_service.expire_workers(conn, "p")
    projects_service.expire_reason_leases(conn, "p")
    assert conn.execute("SELECT status, error_type FROM task_runs WHERE id = 'run-1'").fetchone()[:] == ("failed", "heartbeat_lost")
    assert conn.execute("SELECT reason_worker FROM projects WHERE id = 'p'").fetchone()[0] is None


def test_user_finish_drains_historical_result_candidate_and_cleanup(lifecycle_db):
    conn = lifecycle_db
    _seed_running_goal_driven_explore(conn)

    first_finish = project_records.project_lifecycle_action("p", "finish")
    assert first_finish.status == "active"
    assert first_finish.execution_generation == 1
    assert first_finish.coverage_stop_reason == "user_stopped"
    run = conn.execute(
        "SELECT * FROM task_runs WHERE project_id='p' AND id='run-1'"
    ).fetchone()
    assert run["pause_requested"] == 1
    assert run["pause_interrupted"] == 1
    assert conn.execute(
        "SELECT worker FROM intents WHERE project_id='p' AND id='i1'"
    ).fetchone()["worker"] == "worker"

    repeated = project_records.project_lifecycle_action("p", "finish")
    assert repeated.status == "active"
    assert repeated.execution_generation == 1

    with pytest.raises(HTTPException) as stopped_admission:
        intents_router.create_intent(
            "p",
            CreateIntentRequest(from_=["f1"], description="new work"),
            principal=_USER,
        )
    assert stopped_admission.value.detail == "assessment_stopping"

    stopping_calls = (
        lambda: intents_router.heartbeat(
            "p", "i1", HeartbeatRequest(worker="worker", task_run_id="run-1")
        ),
        lambda: project_reason.claim_project_reason_record(
            "p", ReasonClaimRequest(worker="reasoner", trigger="graph_changed")
        ),
        lambda: project_reason.heartbeat_project_reason_record(
            "p", HeartbeatRequest(worker="reasoner", task_run_id="missing")
        ),
        lambda: audit_task_runs.create_task_run_record(
            "p",
            CreateTaskRunRequest(
                expected_execution_generation=1,
                task_type="bootstrap",
                worker_name="bootstrap-worker",
                worker_type="pi",
                runtime_context={"dispatcher_id": "dispatcher"},
            ),
            principal=_DISPATCHER,
        ),
        lambda: start_explore_attempt(
            conn,
            "p",
            "i1",
            CreateTaskRunRequest(
                expected_execution_generation=1,
                intent_id="i1",
                task_type="explore",
                worker_name="worker",
                worker_type="pi",
                status="running",
                runtime_context={"dispatcher_id": "dispatcher"},
            ),
            principal=_DISPATCHER,
        ),
    )
    for call in stopping_calls:
        with pytest.raises(HTTPException) as stopping:
            call()
        assert stopping.value.detail == "assessment_stopping"

    with pytest.raises(HTTPException) as report_while_draining:
        assessment_reports.request_assessment_report_generation(
            conn,
            "p",
            template_key="standard-pentest",
            requested_by="operator",
        )
    assert report_while_draining.value.detail == "project_pause_draining"

    raw = b'{"protocol":"cairn-agent-result-v1"}'
    candidate_manifest = _candidate_manifest(raw)
    candidate_receipt = candidate_store.capture_candidate(
        "p",
        "run-1",
        candidate_manifest,
        canonical_json_bytes(candidate_manifest.model_dump(mode="json")),
        raw,
        principal=_DISPATCHER,
    )
    assert candidate_receipt.generation_disposition == "historical_at_receipt"
    assert _project_state(conn)["status"] == "active"

    artifact = audit_artifacts.create_artifact_record(
        "p",
        CreateArtifactRequest(
            task_run_id="run-1",
            kind="raw_output",
            content="HTTP/1.1 200 OK\n\nobserved response",
            filename="observed.txt",
        ),
    )
    evidence = EvidenceRef(artifact_id=artifact.id, pointer="response")
    concluded = intents_router.conclude(
        "p",
        "i1",
        ConcludeRequest(
            worker="worker",
            description="accepted in-flight observation",
            task_run_id="run-1",
            evidence=[evidence],
            assessment_record={
                "version": 1,
                "kind": "experiment",
                "hypothesis": "The target returns an observable response",
                "outcome": "observation",
                "observations": ["The current run returned HTTP 200"],
                "evidence_refs": [evidence.model_dump(exclude_none=True)],
            },
        ),
    )
    assert concluded.fact.task_run_id == "run-1"
    assert concluded.fact.assessment_record["outcome"] == "observation"
    assert conn.execute(
        "SELECT status FROM assessment_attempts WHERE id='attempt-1'"
    ).fetchone()["status"] == "completed"
    assert conn.execute(
        "SELECT 1 FROM fact_evidence WHERE project_id='p' AND fact_id=? AND artifact_id=?",
        (concluded.fact.id, artifact.id),
    ).fetchone() is not None
    assert _project_state(conn)["status"] == "active"

    audit_task_runs.update_task_run_record(
        "p",
        "run-1",
        UpdateTaskRunRequest(
            status="succeeded",
            exit_code=0,
            runtime_context={
                "execution_lifecycle": {
                    "generation": 0,
                    "process_started": True,
                    "termination_confirmed": False,
                    "cleanup_confirmed": False,
                }
            },
        ),
        principal=_DISPATCHER,
    )
    assert _project_state(conn)["status"] == "active"

    outcome_manifest = _outcome_manifest()
    outcome_bytes = canonical_json_bytes(outcome_manifest.model_dump(mode="json"))
    outcome_receipt = candidate_store.capture_candidate_outcome(
        "p",
        "run-1",
        outcome_manifest,
        outcome_bytes,
        b"",
        principal=_DISPATCHER,
    )
    assert outcome_receipt.generation_disposition == "historical_at_receipt"
    assert _project_state(conn)["status"] == "active"
    replay = candidate_store.capture_candidate_outcome(
        "p",
        "run-1",
        outcome_manifest,
        outcome_bytes,
        b"",
        principal=_DISPATCHER,
    )
    assert replay.disposition == "replayed"
    assert _project_state(conn)["status"] == "active"

    audit_task_runs.update_task_run_record(
        "p",
        "run-1",
        UpdateTaskRunRequest(
            runtime_context={
                "execution_lifecycle": {
                    "generation": 0,
                    "process_started": True,
                    "termination_confirmed": True,
                    "cleanup_confirmed": True,
                }
            },
        ),
        principal=_DISPATCHER,
    )
    stopped = _project_state(conn)
    assert stopped["status"] == "stopped"
    assert stopped["coverage_stop_reason"] == "user_stopped"
    assert stopped["execution_generation"] == 1

    report = assessment_reports.request_assessment_report_generation(
        conn,
        "p",
        template_key="standard-pentest",
        requested_by="operator",
    )
    assert report.status == "pending"

    final_replay = project_records.project_lifecycle_action("p", "finish")
    assert final_replay.status == "stopped"
    assert final_replay.execution_generation == 1


def test_finish_after_pause_reuses_existing_generation_fence(lifecycle_db):
    conn = lifecycle_db
    _seed_running_goal_driven_explore(conn)

    paused = project_records.project_lifecycle_action("p", "pause")
    assert paused.status == "paused"
    assert paused.execution_generation == 1

    finished = project_records.project_lifecycle_action("p", "finish")
    assert finished.status == "active"
    assert finished.execution_generation == 1
    assert finished.coverage_stop_reason == "user_stopped"

    run = validate_execution_generation(
        conn,
        "p",
        "run-1",
        allow_stopping_settlement=True,
    )
    assert run["execution_generation"] == 0
    assert run["pause_requested"] == 1


def test_stopping_settlement_fence_accepts_only_immediately_previous_draining_run(
    lifecycle_db,
):
    conn = lifecycle_db
    _seed_running_goal_driven_explore(conn)
    conn.execute(
        """
        UPDATE projects
        SET execution_generation = 2, coverage_stop_reason = 'user_stopped'
        WHERE id = 'p'
        """
    )
    conn.execute(
        "UPDATE task_runs SET execution_generation = 1, pause_requested = 1 WHERE id = 'run-1'"
    )

    accepted = validate_execution_generation(
        conn,
        "p",
        "run-1",
        allow_stopping_settlement=True,
    )
    assert accepted["execution_generation"] == 1

    conn.execute("UPDATE task_runs SET pause_requested = 0 WHERE id = 'run-1'")
    with pytest.raises(HTTPException) as not_draining:
        validate_execution_generation(
            conn,
            "p",
            "run-1",
            allow_stopping_settlement=True,
        )
    assert not_draining.value.detail == "execution_generation_mismatch"

    conn.execute(
        "UPDATE task_runs SET execution_generation = 0, pause_requested = 1 WHERE id = 'run-1'"
    )
    with pytest.raises(HTTPException) as too_old:
        validate_execution_generation(
            conn,
            "p",
            "run-1",
            allow_stopping_settlement=True,
        )
    assert too_old.value.detail == "execution_generation_mismatch"


def _explore_start_body(*, expected_generation: int = 0) -> CreateTaskRunRequest:
    return CreateTaskRunRequest(
        expected_execution_generation=expected_generation,
        intent_id="i1",
        task_type="explore",
        worker_name="worker",
        worker_type="pi",
        status="running",
        runtime_context={"dispatcher_id": "dispatcher"},
    )


def _remove_seeded_execution(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM task_runs WHERE project_id='p'")
    conn.execute("DELETE FROM assessment_attempts WHERE project_id='p'")
    conn.execute(
        "UPDATE intents SET worker=NULL, last_heartbeat_at=NULL WHERE project_id='p' AND id='i1'"
    )


def test_expired_authorization_blocks_every_new_execution_admission(lifecycle_db):
    conn = lifecycle_db
    _seed_running_goal_driven_explore(conn)
    _remove_seeded_execution(conn)
    conn.execute(
        "UPDATE projects SET authorization_expires_at='2000-01-01T00:00:00Z' "
        "WHERE id='p'"
    )
    conn.commit()

    admissions = (
        lambda: intents_router.heartbeat(
            "p",
            "i1",
            HeartbeatRequest(worker="worker"),
        ),
        lambda: project_reason.claim_project_reason_record(
            "p",
            ReasonClaimRequest(worker="reasoner", trigger="graph_changed"),
        ),
        lambda: audit_task_runs.create_task_run_record(
            "p",
            CreateTaskRunRequest(
                task_type="bootstrap",
                worker_name="bootstrap-worker",
                worker_type="pi",
                runtime_context={"dispatcher_id": "dispatcher"},
            ),
            principal=_DISPATCHER,
        ),
        lambda: start_explore_attempt(
            conn,
            "p",
            "i1",
            _explore_start_body(),
            principal=_DISPATCHER,
        ),
    )
    for admission in admissions:
        with pytest.raises(HTTPException) as expired:
            admission()
        assert expired.value.detail == "project_reauthorization_required"

    assert conn.execute(
        "SELECT COUNT(*) AS c FROM task_runs WHERE project_id='p'"
    ).fetchone()["c"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM assessment_attempts WHERE project_id='p'"
    ).fetchone()["c"] == 0
    assert conn.execute(
        "SELECT worker FROM intents WHERE project_id='p' AND id='i1'"
    ).fetchone()["worker"] is None
    assert _project_state(conn)["reason_worker"] is None


def test_legacy_budget_and_reserve_do_not_block_discovery_verifier_or_reason(lifecycle_db):
    conn = lifecycle_db
    _seed_running_goal_driven_explore(conn)
    _remove_seeded_execution(conn)
    snapshot = json.loads(_project_state(conn)["scenario_snapshot_json"])
    snapshot["features"]["verification_reserve"] = 8
    conn.execute(
        "UPDATE projects SET explore_budget=40, explore_attempts_started=45, "
        "verification_attempts_started=1, scenario_snapshot_json=? WHERE id='p'",
        (json.dumps(snapshot),),
    )
    discovery = start_explore_attempt(
        conn, "p", "i1", _explore_start_body(), principal=_DISPATCHER,
    )
    audit_task_runs.update_task_run_record(
        "p", discovery.id, UpdateTaskRunRequest(session_id=_SESSION), principal=_DISPATCHER,
    )
    artifact = audit_artifacts.create_artifact_record(
        "p",
        CreateArtifactRequest(
            task_run_id=discovery.id, kind="raw_output",
            content="synthetic discovery evidence", filename="discovery.txt",
        ),
    )
    evidence = EvidenceRef(artifact_id=artifact.id)
    accepted = intents_router.conclude(
        "p", "i1",
        ConcludeRequest(
            worker="worker", task_run_id=discovery.id,
            description="synthetic finding beyond the historical attempt limit",
            evidence=[evidence],
            assessment_record={
                "version": 1, "kind": "experiment",
                "hypothesis": "The isolated fixture can produce a durable finding",
                "outcome": "finding", "observations": ["synthetic discovery evidence"],
                "evidence_refs": [evidence.model_dump(exclude_none=True)],
            },
            vulnerability={
                "title": "合成证据接受测试", "severity": "low",
                "repro_request": "GET /synthetic HTTP/1.1\r\nHost: fixture.invalid\r\n\r\n",
                "repro_response": "HTTP/1.1 200 OK\r\n\r\nsynthetic evidence",
                "identity": {
                    "vulnerability_class": "synthetic_access_boundary",
                    "asset": {"host": "fixture.invalid", "protocol": "http"},
                    "entrypoint": {"method": "GET", "route": "/synthetic"},
                },
            },
        ),
    )
    assert accepted.fact.task_run_id == discovery.id
    audit_task_runs.update_task_run_record(
        "p", discovery.id, UpdateTaskRunRequest(status="succeeded", exit_code=0),
        principal=_DISPATCHER,
    )
    verifier_intent = conn.execute(
        "SELECT id, target_observation_id FROM intents "
        "WHERE project_id='p' AND assessment_role='verifier'"
    ).fetchone()
    verifier = start_explore_attempt(
        conn, "p", verifier_intent["id"],
        CreateTaskRunRequest(
            expected_execution_generation=0, intent_id=verifier_intent["id"],
            task_type="explore", worker_name="verifier", worker_type="pi", status="running",
            runtime_context={"dispatcher_id": "dispatcher"},
        ),
        principal=_DISPATCHER,
    )
    audit_task_runs.update_task_run_record(
        "p", verifier.id,
        UpdateTaskRunRequest(session_id="pi:33333333-3333-4333-8333-333333333333"),
        principal=_DISPATCHER,
    )
    verifier_artifact = audit_artifacts.create_artifact_record(
        "p",
        CreateArtifactRequest(
            task_run_id=verifier.id, kind="raw_output",
            content="synthetic independent verification", filename="verification.txt",
        ),
    )
    verified = intents_router.conclude(
        "p", verifier_intent["id"],
        ConcludeRequest(
            worker="verifier", task_run_id=verifier.id,
            description="synthetic independent result beyond the historical attempt limit",
            verification_result=VerificationResultInput(
                observation_id=verifier_intent["target_observation_id"], outcome="confirmed",
                evidence_refs=[EvidenceRef(artifact_id=verifier_artifact.id)],
            ),
        ),
    )
    assert verified.fact.task_run_id == verifier.id
    assert conn.execute(
        "SELECT fact_id FROM vulnerability_verifications WHERE verifier_task_run_id=?",
        (verifier.id,),
    ).fetchone()["fact_id"] == verified.fact.id
    audit_task_runs.update_task_run_record(
        "p", verifier.id, UpdateTaskRunRequest(status="succeeded", exit_code=0),
        principal=_DISPATCHER,
    )
    assert advance_goal_driven_convergence(conn, "p") is None
    reason = project_reason.claim_project_reason_record(
        "p", ReasonClaimRequest(worker="reasoner", trigger="graph_changed"),
    )
    assert reason.reason is not None
    assert reason.reason.worker == "reasoner"
    project = _project_state(conn)
    assert project["status"] == "active"
    assert project["coverage_stop_reason"] is None
    assert project["explore_budget"] == 40
    assert project["explore_attempts_started"] == 47
    assert project["verification_attempts_started"] == 2
    assert [tuple(row) for row in conn.execute(
        "SELECT status, charged, execution_count FROM assessment_attempts WHERE project_id='p' ORDER BY id"
    )] == [("completed", 1, 1), ("completed", 1, 1)]


def test_historical_budget_stop_does_not_automatically_resume(lifecycle_db):
    conn = lifecycle_db
    _seed_running_goal_driven_explore(conn)
    _remove_seeded_execution(conn)
    conn.execute(
        "UPDATE projects SET status='stopped', coverage_stop_reason='budget_exhausted', "
        "explore_budget=40, explore_attempts_started=45 WHERE id='p'"
    )
    conn.execute(
        "UPDATE intents SET concluded_at=?, conclusion='abandoned', "
        "conclusion_reason='budget_exhausted' WHERE project_id='p' AND id='i1'",
        (utcnow(),),
    )

    advance_goal_driven_convergence(conn, "p")
    with pytest.raises(HTTPException) as stopped:
        project_reason.claim_project_reason_record(
            "p", ReasonClaimRequest(worker="reasoner", trigger="graph_changed"),
        )

    assert stopped.value.detail == "Project is stopped"
    project = _project_state(conn)
    assert project["status"] == "stopped"
    assert project["coverage_stop_reason"] == "budget_exhausted"
    assert project["explore_attempts_started"] == 45
    intent = conn.execute(
        "SELECT conclusion, conclusion_reason FROM intents WHERE project_id='p' AND id='i1'"
    ).fetchone()
    assert dict(intent) == {"conclusion": "abandoned", "conclusion_reason": "budget_exhausted"}


def test_charged_attempt_keeps_bounded_runtime_retry_beyond_legacy_budget(lifecycle_db):
    conn = lifecycle_db
    _seed_running_goal_driven_explore(conn)
    conn.execute(
        "UPDATE projects SET explore_budget=40, explore_attempts_started=45 WHERE id='p'"
    )
    conn.execute(
        "UPDATE intents SET worker=NULL, last_heartbeat_at=NULL WHERE project_id='p' AND id='i1'"
    )
    conn.execute(
        """
        UPDATE task_runs
        SET status='failed', ended_at='2000-01-01T00:00:00Z',
            error_type='heartbeat_lost', error_message='lost heartbeat'
        WHERE project_id='p' AND id='run-1'
        """
    )

    retry = start_explore_attempt(
        conn,
        "p",
        "i1",
        _explore_start_body(),
        principal=_DISPATCHER,
    )

    assert retry.id != "run-1"
    assert retry.assessment_attempt_id == "attempt-1"
    project = _project_state(conn)
    assert project["status"] == "active"
    assert project["coverage_stop_reason"] is None
    assert project["explore_attempts_started"] == 45
    attempt = conn.execute(
        "SELECT status, charged, execution_count, max_runtime_retries "
        "FROM assessment_attempts WHERE id='attempt-1'"
    ).fetchone()
    assert dict(attempt) == {
        "status": "active", "charged": 1, "execution_count": 2, "max_runtime_retries": 2,
    }


def test_deadline_started_result_is_accepted_before_terminal_stop(lifecycle_db):
    conn = lifecycle_db
    _seed_running_goal_driven_explore(conn)
    _remove_seeded_execution(conn)
    conn.execute(
        """
        UPDATE projects
        SET explore_attempts_started=0, explore_deadline='2099-01-01T00:00:00Z'
        WHERE id='p'
        """
    )
    run = start_explore_attempt(
        conn,
        "p",
        "i1",
        _explore_start_body(),
        principal=_DISPATCHER,
    )
    conn.execute(
        "UPDATE projects SET explore_deadline='2000-01-01T00:00:00Z' WHERE id='p'"
    )

    artifact = audit_artifacts.create_artifact_record(
        "p",
        CreateArtifactRequest(
            task_run_id=run.id,
            kind="raw_output",
            content="deadline-crossing observation",
            filename="deadline-observation.txt",
        ),
    )
    evidence = EvidenceRef(artifact_id=artifact.id, pointer="observation")
    accepted = intents_router.conclude(
        "p",
        "i1",
        ConcludeRequest(
            worker="worker",
            description="accepted result from work started before cutoff",
            task_run_id=run.id,
            evidence=[evidence],
            assessment_record={
                "version": 1,
                "kind": "experiment",
                "hypothesis": "The pre-cutoff execution can produce a durable observation",
                "outcome": "observation",
                "observations": ["The execution produced evidence after the cutoff"],
                "evidence_refs": [evidence.model_dump(exclude_none=True)],
            },
        ),
    )
    assert accepted.fact.task_run_id == run.id
    project = _project_state(conn)
    assert project["status"] == "active"
    assert project["coverage_stop_reason"] == "deadline_exceeded"
    assert conn.execute(
        "SELECT status FROM assessment_attempts WHERE id=?",
        (run.assessment_attempt_id,),
    ).fetchone()["status"] == "completed"

    with pytest.raises(HTTPException) as new_work:
        intents_router.create_intent(
            "p",
            CreateIntentRequest(from_=[accepted.fact.id], description="late new work"),
            principal=_USER,
        )
    assert new_work.value.detail == "assessment_stopping"

    audit_task_runs.update_task_run_record(
        "p",
        run.id,
        UpdateTaskRunRequest(
            status="succeeded",
            exit_code=0,
            runtime_context={
                "execution_lifecycle": {
                    "generation": 0,
                    "process_started": True,
                    "termination_confirmed": True,
                    "cleanup_confirmed": True,
                }
            },
        ),
        principal=_DISPATCHER,
    )
    stopped = _project_state(conn)
    assert stopped["status"] == "stopped"
    assert stopped["coverage_stop_reason"] == "deadline_exceeded"


def test_pending_attempt_candidate_and_cleanup_each_block_deadline_stop(lifecycle_db):
    conn = lifecycle_db
    _seed_running_goal_driven_explore(conn)
    conn.execute(
        "UPDATE projects SET explore_deadline='2000-01-01T00:00:00Z' WHERE id='p'"
    )
    assert advance_goal_driven_convergence(conn, "p") == "deadline_exceeded"
    assert _project_state(conn)["status"] == "active"
    assert conn.execute(
        "SELECT status FROM assessment_attempts WHERE id='attempt-1'"
    ).fetchone()["status"] == "active"

    raw = b'{"protocol":"cairn-agent-result-v1"}'
    manifest = _candidate_manifest(raw)
    candidate_store.capture_candidate(
        "p",
        "run-1",
        manifest,
        canonical_json_bytes(manifest.model_dump(mode="json")),
        raw,
        principal=_DISPATCHER,
    )
    conn.execute(
        "UPDATE task_runs SET status='succeeded', ended_at=? WHERE id='run-1'",
        (utcnow(),),
    )
    assert advance_goal_driven_convergence(conn, "p") == "deadline_exceeded"
    assert _project_state(conn)["status"] == "active"
    assert dict(conn.execute(
        "SELECT status, outcome FROM assessment_attempts WHERE id='attempt-1'"
    ).fetchone()) == {"status": "failed", "outcome": "deadline_exceeded"}

    conn.execute("UPDATE task_runs SET pause_requested=1 WHERE id='run-1'")
    outcome = _outcome_manifest()
    candidate_store.capture_candidate_outcome(
        "p",
        "run-1",
        outcome,
        canonical_json_bytes(outcome.model_dump(mode="json")),
        b"",
        principal=_DISPATCHER,
    )
    assert _project_state(conn)["status"] == "active"

    audit_task_runs.update_task_run_record(
        "p",
        "run-1",
        UpdateTaskRunRequest(
            runtime_context={
                "execution_lifecycle": {
                    "generation": 0,
                    "process_started": True,
                    "termination_confirmed": True,
                    "cleanup_confirmed": True,
                }
            },
        ),
        principal=_DISPATCHER,
    )
    stopped = _project_state(conn)
    assert stopped["status"] == "stopped"
    assert stopped["coverage_stop_reason"] == "deadline_exceeded"
