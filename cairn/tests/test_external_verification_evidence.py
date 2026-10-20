"""用隔离原件验证复测证据的来源、交付与历史输入边界。"""

from __future__ import annotations

import asyncio
import base64
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from uuid import uuid4

from fastapi import HTTPException
import pytest

from cairn.server import db
from cairn.server.auth import RequestPrincipal, create_user_with_personal_tenant
from cairn.server.models import CreateRawArtifactRequest, CreateVulnerabilityRequest
from cairn.server.schemas.models import CreateTenantModelRequest
from cairn.server.schemas.tasks import CreateTaskRunRequest
from cairn.server.schemas.verification_requests import (
    CreateVerificationRequest, UpdateVerificationConfiguration, VerificationClaimRequest,
    VerificationResultSubmission,
)
from cairn.server.services import model_credentials, tenant_workers
from cairn.server.services.audit_artifacts import create_raw_artifact_record
from cairn.server.services.tenant_models import create_tenant_model
from cairn.server.services.time_utils import utcnow
from cairn.server.services.verification_configuration import update_verification_configuration
from cairn.server.services.verification_requests import (
    claim_verification_request, create_verification_batch, create_verification_request, get_verification_request,
    preview_platform_vulnerability, submit_verification_result, verification_artifact, verification_material,
)
from cairn.server.services.vulnerabilities import create_vulnerability


@pytest.fixture
def execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db._reset_for_tests()
    monkeypatch.setenv("CAIRN_PROJECT_RESOURCE_ROOT", str(tmp_path / "resources"))
    db.configure(tmp_path / "verification.db", artifact_path=tmp_path / "artifacts")
    keyring = tmp_path / "keyring.json"
    keyring.write_text(json.dumps({"active_version": "test", "keys": {"test": base64.b64encode(b"v" * 32).decode()}}))
    keyring.chmod(0o600)
    monkeypatch.setenv(model_credentials.KEYRING_FILE_ENV, str(keyring))
    monkeypatch.setattr(tenant_workers, "dispatcher_executor_available", lambda conn: True)
    user_id, tenant_id = create_user_with_personal_tenant("evidence-admin", "synthetic password", role="super_admin")
    user = RequestPrincipal(kind="user", subject_id=user_id, tenant_id=tenant_id, role="super_admin")
    machine = RequestPrincipal(kind="machine", subject_id="synthetic-dispatcher")
    model = create_tenant_model(CreateTenantModelRequest(
        display_name="Synthetic model", protocol="openai-responses", base_url="https://unused.example.test",
        model_name="unused-test-model", api_key="synthetic-platform-model-secret",
    ), principal=user)
    with db.get_conn() as conn:
        conn.execute("INSERT INTO dispatcher_status(id, status, updated_at, runtime_json) VALUES (?, 'idle', ?, ?)",
                     (machine.subject_id, utcnow(), '{"verification_capacity":1}'))
    configuration = update_verification_configuration(UpdateVerificationConfiguration(model_id=model.id), user, tenant_id=tenant_id)
    detail, claim = _new_execution(user, machine, configuration["tenant_worker_id"])
    yield user, machine, model, configuration["tenant_worker_id"], detail, claim
    db._reset_for_tests()


def _new_execution(user, machine, worker_id, *, report=None):
    detail, replayed = create_verification_request(CreateVerificationRequest(
        schema_version=1, request_id=uuid4(), mode="retest", title="Synthetic verification",
        origin="仅使用本地合成材料，不访问目标。", authorization_expires_at=None,
        report=report or {"type": "text", "content": "合成历史主张"},
    ), user)
    assert not replayed
    claim = claim_verification_request(detail["id"], VerificationClaimRequest(task_run=CreateTaskRunRequest(
        task_type="external_verify", worker_type="pi", worker_name=worker_id,
        session_id=f"pi:{uuid4()}", expected_execution_generation=0,
        runtime_context={"dispatcher_id": machine.subject_id},
    )), machine)
    return detail, claim


def _upload(detail, claim, kind, filename, content, *, metadata=None):
    digest = sha256(content).hexdigest()
    registered = {
        "source_path": f"/home/kali/workspace/{filename}",
        "registered_path": f"/home/kali/workspace/.cairn/artifacts/registered/local-{uuid4()}",
        "registered_handle": f"local-{uuid4()}", "registered_sha256": digest,
        "registered_size_bytes": len(content), "declared_by_worker": True,
    }
    async def chunks():
        yield content[:3]
        yield content[3:]
    return asyncio.run(create_raw_artifact_record(detail["project_id"], CreateRawArtifactRequest(
        task_run_id=claim["task_run"]["id"], kind=kind, filename=filename,
        metadata_json=json.dumps({**registered, **(metadata or {})}),
    ), chunks()))


def _submission(detail, claim, artifacts):
    return VerificationResultSubmission(
        project_id=detail["project_id"], task_run_id=claim["task_run"]["id"],
        execution_generation=claim["execution_generation"], claim_token=claim["claim_token"],
        result={"schema_version": 3, "outcome": "confirmed", "summary": "合成目标响应包含 /root/ 与 /home/，不是平台秘密。",
                "tested_scope": "仅本地合成材料", "key_evidence": [], "test_steps": [],
                "limitations": ["不代表真实任务验收"],
                "evidence_items": [{"id": artifact.id, "kind": "artifact", "source": "capture",
                                    "artifact": {"artifact_id": artifact.id}} for artifact in artifacts]},
    )


def test_mixed_evidence_is_accepted_unchanged_and_historical_logs_are_input(execution):
    user, machine, model, worker_id, detail, claim = execution
    contents = [
        ("report", "result.md", b"# Synthetic result\n/root/target\n/home/target\n"),
        ("file", "check.py", b"# synthetic proof, not executed\n"),
        ("log", "target.log", b"target /root/rootfile /home/guest/file\n"),
        ("log", "command.out", b"synthetic command stdout\n"),
        ("raw_output", "response.json", b'{"protocol":"session","runtime_context":"target value"}\n'),
        ("raw_output", "sdk-output.txt", b"synthetic SDK saved tool output\n"),
    ]
    artifacts = [_upload(detail, claim, kind, name, content) for kind, name, content in contents]
    accepted = submit_verification_result(detail["id"], _submission(detail, claim, artifacts), machine)
    assert accepted["accepted_at"] and accepted["result"]["outcome"] == "confirmed"
    assert {item["id"] for item in accepted["artifacts"]} == {artifact.id for artifact in artifacts}
    for artifact, (_, _, content) in zip(artifacts, contents):
        downloaded = verification_artifact(detail["id"], artifact.id, user)
        assert downloaded.disk_path.read_bytes() == content
        assert artifact.sha256 == sha256(content).hexdigest()
    with db.get_conn() as conn:
        vulnerability = create_vulnerability(conn, detail["project_id"], CreateVulnerabilityRequest(
            title="合成历史漏洞", severity="low", evidence_items=[{
                "id": "target-log", "kind": "artifact", "source": "manual", "artifact": {"artifact_id": artifacts[2].id},
            }],
        ))
    copied, copied_claim = _new_execution(user, machine, worker_id, report={
        "type": "platform_vulnerability", "project_id": detail["project_id"], "vulnerability_id": vulnerability.id,
    })
    historical = [item for item in copied["materials"] if item["path"].startswith("_verification/historical-evidence/")]
    assert len(historical) == 1 and historical[0]["sha256"] == artifacts[2].sha256
    path = verification_material(copied["id"], historical[0]["id"], user)
    assert path.content == contents[2][2]
    with pytest.raises(HTTPException) as historical_as_new:
        submit_verification_result(copied["id"], _submission(copied, copied_claim, [artifacts[2]]), machine)
    assert historical_as_new.value.status_code == 422
    # 只有已接受的固定版本不依赖模型当前 Header；新交付仍须证明当次秘密边界。
    with db.get_conn() as conn:
        conn.execute("UPDATE tenant_models SET custom_headers_json = ?, revision = revision + 1 WHERE id = ?",
                     ('[{"name":"X-Synthetic","value":"changed-header"}]', model.id))
    assert len(get_verification_request(detail["id"], user)["artifacts"]) == len(artifacts)
    assert verification_artifact(detail["id"], artifacts[2].id, user).disk_path.read_bytes() == contents[2][2]


def test_internal_sources_renamed_copies_and_real_secrets_are_not_deliverable(execution):
    user, machine, model, worker_id, detail, claim = execution
    internal_bytes = b"synthetic Harness stream, not target output\n"
    internal = _upload(detail, claim, "raw_output", "renamed-target.txt", internal_bytes, metadata={"stream": "stderr"})
    session = _upload(detail, claim, "file", "ordinary.json", b"synthetic private session", metadata={
        "source_path": "/home/kali/workspace/.cairn/pi/session/session.jsonl",
    })
    prompt = _upload(detail, claim, "file", "ordinary.md", b"synthetic private instructions", metadata={
        "source_path": "/opt/cairn/AGENTS.md",
    })
    secret = _upload(detail, claim, "log", "response.log", b"synthetic-platform-model-secret")
    for artifact in (internal, session, prompt, secret):
        with pytest.raises(HTTPException) as rejected:
            submit_verification_result(detail["id"], _submission(detail, claim, [artifact]), machine)
        assert rejected.value.status_code == 422
        assert rejected.value.detail == "verification_evidence_not_current_or_deliverable"
        with pytest.raises(HTTPException) as hidden:
            verification_artifact(detail["id"], artifact.id, user)
        assert hidden.value.status_code == 404
        with db.get_conn() as conn:
            vulnerability = create_vulnerability(conn, detail["project_id"], CreateVulnerabilityRequest(
                title="合成不可交付历史材料", severity="low", evidence_items=[{
                    "id": "original", "kind": "artifact", "source": "manual", "artifact": {"artifact_id": artifact.id},
                }],
            ))
        snapshot = preview_platform_vulnerability(detail["project_id"], vulnerability.id, user)
        assert not snapshot["artifacts"][0]["available"]
        assert any(item["code"] in {"source_artifact_not_input", "source_artifact_not_deliverable"}
                   for item in snapshot["missing_materials"])
    clean = _upload(detail, claim, "log", "clean.log", b"synthetic valid evidence")
    leaking_result = _submission(detail, claim, [clean])
    leaking_result.result.summary = "synthetic-platform-model-secret"
    with pytest.raises(HTTPException) as result_secret:
        submit_verification_result(detail["id"], leaking_result, machine)
    assert result_secret.value.detail == "verification_sensitive_result_rejected"


def test_bound_session_copies_are_rejected_but_target_session_logs_are_deliverable(execution):
    user, machine, model, worker_id, detail, claim = execution
    # 形状来自当前 SDK SessionHeader 和 Runner Manifest；Server 只持有项目/session 归属。
    header = {
        "type": "session", "version": 3, "id": str(uuid4()), "timestamp": utcnow(),
        "cwd": "/home/kali/workspace",
        "cairn": {"project_id": detail["project_id"], "session": claim["task_run"]["session_id"]},
    }
    transcript = (json.dumps(header) + "\n" + json.dumps({
        "type": "message", "id": "synthetic-entry", "parentId": None, "timestamp": utcnow(),
        "message": {"role": "user", "content": "synthetic internal session"},
    }) + "\n").encode()
    manifest = {
        "protocol": "cairn-pi-session-v1", **header["cairn"], "sdk_session_id": header["id"],
        "cwd": header["cwd"], "phase_family": "external_verify",
        "binding_sha256": sha256(json.dumps({"verification_request_id": detail["id"]}, separators=(",", ":")).encode()).hexdigest(),
        "last_task_run_id": claim["task_run"]["id"], "sha256": sha256(transcript).hexdigest(),
    }
    # 原 session 从未作为内部 Artifact 登记，副本的可信 source_path 也是普通工作区文件。
    copies = [
        _upload(detail, claim, "log", "ordinary-target.log", transcript),
        _upload(detail, claim, "raw_output", "ordinary-output.bin", json.dumps(manifest).encode()),
    ]
    for artifact in copies:
        with pytest.raises(HTTPException) as rejected:
            submit_verification_result(detail["id"], _submission(detail, claim, [artifact]), machine)
        assert rejected.value.detail == "verification_evidence_not_current_or_deliverable"
        with pytest.raises(HTTPException) as hidden:
            verification_artifact(detail["id"], artifact.id, user)
        assert hidden.value.status_code == 404
        with db.get_conn() as conn:
            vulnerability = create_vulnerability(conn, detail["project_id"], CreateVulnerabilityRequest(
                title="合成会话复制材料", severity="low", evidence_items=[{
                    "id": "original", "kind": "artifact", "source": "manual", "artifact": {"artifact_id": artifact.id},
                }],
            ))
        snapshot = preview_platform_vulnerability(detail["project_id"], vulnerability.id, user)
        assert not snapshot["artifacts"][0]["available"]
        assert any(item["code"] == "source_artifact_not_deliverable" for item in snapshot["missing_materials"])
    target_owner = {"project_id": detail["project_id"], "session": f"pi:{uuid4()}"}
    target_contents = [
        ("log", "target-session.log", b"session-fixation prompt-injection: target /root/session /home/guest\n"),
        ("raw_output", "target-session.jsonl", (json.dumps({**header, "cairn": target_owner}) + "\n").encode()),
        ("raw_output", "target-manifest.json", json.dumps({**manifest, **target_owner}).encode()),
    ]
    targets = [_upload(detail, claim, kind, name, content) for kind, name, content in target_contents]
    accepted = submit_verification_result(detail["id"], _submission(detail, claim, targets), machine)
    assert {item["id"] for item in accepted["artifacts"]} == {artifact.id for artifact in targets}
    for artifact, (_, _, content) in zip(targets, target_contents):
        assert verification_artifact(detail["id"], artifact.id, user).disk_path.read_bytes() == content


def _runner_registered_upload(detail, claim, tmp_path, *, copy_instructions):
    from cairn.dispatcher.contracts import extract_declared_artifacts
    from cairn.dispatcher.protocol.client import ApiResult, CairnClient
    from cairn.dispatcher.runtime.process import ProcessResult
    from cairn.dispatcher.tasks.artifact_records import record_declared_artifacts, record_process_artifacts
    from cairn.dispatcher.tasks.hook_records import record_pi_tool_events
    from cairn.dispatcher.tasks.prompt_events import record_prompt_rendered
    from cairn.dispatcher.worker_protocol.submission import build_submit_result_schema
    from cairn.server.models import CreateAgentEventRequest, CreateArtifactRequest
    from cairn.server.services.audit_artifacts import artifact_to_model, create_artifact_record
    from cairn.server.services.audit_events import create_agent_event_record
    from test_pi_execution import worker

    # 私有 mount namespace 使用真实默认加载路径，不在宿主 /opt 或活动容器安装测试指引。
    instructions = tmp_path / "AGENTS.md"
    instructions.write_bytes(b"# Synthetic loaded instructions\nKeep synthetic evidence unchanged.\n")
    script = r"""
import { copyFile, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fixture } from './tests/fixtures.mjs';
import { parseRequest } from './dist/protocol.js';
import { runRequest } from './dist/runner.js';
let input = '';
for await (const chunk of process.stdin) input += chunk;
const production = JSON.parse(input);
const cleanups = [];
try {
  const f = await fixture({ after: (cleanup) => cleanups.push(cleanup) }, 'openai-chat-completions', [
    async () => {
      if (production.copy_instructions) await copyFile('/opt/cairn/AGENTS.md', join(f.cwd, 'proof.txt'));
      else await writeFile(join(f.cwd, 'target.log'), 'OK\n');
      return { tools: [{ name: 'register_artifact', args: { path: production.copy_instructions ? 'proof.txt' : 'target.log', kind: 'log' } }] };
    },
    (call) => {
      const results = call.body.messages.filter((message) => message.role === 'tool');
      if (!results.at(-1).content.startsWith('{')) throw new Error(JSON.stringify(results));
      const artifact = JSON.parse(results.at(-1).content);
      return { tools: [{ name: 'submit_result', args: { accepted: true, data: {
        schema_version: 3, outcome: 'confirmed', summary: '合成观察', tested_scope: '本地合成文件',
        key_evidence: [{ reason: '合成原件记录观察结果', evidence_item_ids: ['observed'] }],
        test_steps: [{ action: '登记本地合成原件', observation: '原件登记成功', evidence_item_ids: ['observed'] }],
        limitations: ['不代表真实任务验收'], evidence_items: [{ id: 'observed', kind: 'artifact', source: 'capture',
          artifact: { artifact_handle: artifact.handle } }],
      } } }] };
    },
  ]);
  const request = parseRequest({ ...f.request, ...production, cwd: f.cwd, session_root: f.request.session_root });
  const audit = [];
  const envelope = await runRequest(request, { audit: (event) => audit.push(event) });
  const materials = {};
  for (const item of envelope.result?.data.artifacts ?? []) materials[item.path] = (await readFile(item.path)).toString('base64');
  process.stdout.write(JSON.stringify({ envelope, audit, materials, cwd: f.cwd, calls: f.calls.length }));
} finally {
  for (const cleanup of cleanups.reverse()) await cleanup();
}
"""
    completed = subprocess.run([
        "bwrap", "--die-with-parent", "--bind", "/", "/", "--tmpfs", "/opt", "--dir", "/opt/cairn",
        "--ro-bind", str(instructions), "/opt/cairn/AGENTS.md", "node", "--input-type=module", "-e", script,
    ], cwd=Path(__file__).resolve().parents[2] / "container" / "pi-agent", input=json.dumps({
        "project_id": detail["project_id"], "task_run_id": claim["task_run"]["id"],
        "session": claim["task_run"]["session_id"], "phase": "external_verify_execute",
        "result_schema": build_submit_result_schema("external_verify_execute"), "copy_instructions": copy_instructions,
    }), text=True, capture_output=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    captured = json.loads(completed.stdout)
    assert captured["envelope"]["status"] == "succeeded", captured["envelope"]
    assert captured["calls"] == 2
    declarations = extract_declared_artifacts(captured["envelope"]["result"])
    assert len(declarations) == 1
    assert declarations[0].source_path == f'{captured["cwd"]}/{"proof.txt" if copy_instructions else "target.log"}'
    assert not Path(captured["cwd"]).exists()

    class Client(CairnClient):
        def _request_json(self, method, path, *, json):
            assert method == "POST"
            if path.endswith("/agent-events"):
                value = create_agent_event_record(detail["project_id"], CreateAgentEventRequest.model_validate(json))
            else:
                assert path.endswith("/artifacts")
                value = create_artifact_record(detail["project_id"], CreateArtifactRequest.model_validate(json))
            return ApiResult(status_code=201, data=value.model_dump(mode="json"))

        def _request_raw_artifact(self, project_id, *, task_run_id, kind, content, filename, metadata, category):
            async def chunks():
                while chunk := content.read(3):
                    yield chunk
            value = asyncio.run(create_raw_artifact_record(project_id, CreateRawArtifactRequest(
                task_run_id=task_run_id, kind=kind, filename=filename, metadata_json=json.dumps(metadata),
            ), chunks()))
            return ApiResult(status_code=201, data=value.model_dump(mode="json"))

    class Materials:
        def read_file(self, container_name, path):
            return base64.b64decode(captured["materials"][path], validate=True)

    client = Client("http://synthetic.invalid", token="synthetic-token")
    try:
        record_prompt_rendered(client, detail["project_id"], claim["task_run"]["id"], phase="external_verify_execute",
                               prompt_group="external_verify", prompt_name="task", prompt="synthetic frozen task prompt",
                               worker=worker("openai-chat-completions"), replacement_keys=[])
        record_pi_tool_events(client, detail["project_id"], claim["task_run"]["id"], phase="external_verify_execute",
                              session=claim["task_run"]["session_id"], result=ProcessResult(
                                  0, json.dumps(captured["envelope"]), "\n".join(json.dumps(event) for event in captured["audit"])))
        streams = record_process_artifacts(client, detail["project_id"], claim["task_run"]["id"],
                                           phase="external_verify_execute", result=ProcessResult(
                                               0, json.dumps(captured["envelope"]), "OK\n"))
        uploaded = record_declared_artifacts(client, Materials(), "synthetic-container", detail["project_id"],
                                            claim["task_run"]["id"], phase="external_verify_execute", artifacts=declarations)
    finally:
        client.close()
    assert not streams.write_failed and not uploaded.collect_failed and not uploaded.write_failed
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM artifacts WHERE project_id = ? AND id = ?",
                           (detail["project_id"], uploaded.refs[0].artifact_id)).fetchone()
        artifact = artifact_to_model(row)
        prompts = [json.loads(row["payload_json"]) for row in conn.execute(
            "SELECT payload_json FROM agent_events WHERE project_id = ? AND kind = 'prompt_rendered'",
            (detail["project_id"],),
        ).fetchall()]
    assert any(event.get("prompt_name") == "AGENTS.md" and event["prompt_hash"] == sha256(instructions.read_bytes()).hexdigest()
               for event in prompts)
    return artifact, instructions.read_bytes()


def test_runner_independent_log_with_same_bytes_as_harness_stderr_is_deliverable(execution, tmp_path):
    user, machine, model, worker_id, detail, claim = execution
    artifact, _ = _runner_registered_upload(detail, claim, tmp_path, copy_instructions=False)
    assert artifact.sha256 == sha256(b"OK\n").hexdigest()
    assert [item["id"] for item in get_verification_request(detail["id"], user)["artifacts"]] == [artifact.id]
    assert verification_artifact(detail["id"], artifact.id, user).disk_path.read_bytes() == b"OK\n"
    with db.get_conn() as conn:
        vulnerability = create_vulnerability(conn, detail["project_id"], CreateVulnerabilityRequest(
            title="合成独立日志", severity="low", evidence_items=[{
                    "id": "original", "kind": "artifact", "source": "manual", "artifact": {"artifact_id": artifact.id},
                }],
        ))
    copied, _ = _new_execution(user, machine, worker_id, report={
        "type": "platform_vulnerability", "project_id": detail["project_id"], "vulnerability_id": vulnerability.id,
    })
    historical = next(item for item in copied["materials"] if item["path"].startswith("_verification/historical-evidence/"))
    assert verification_material(copied["id"], historical["id"], user).content == b"OK\n"
    accepted = submit_verification_result(detail["id"], _submission(detail, claim, [artifact]), machine)
    assert accepted["accepted_at"] and accepted["result"]["evidence_refs"][0]["artifact_id"] == artifact.id


def test_runner_copy_of_loaded_unregistered_instructions_is_not_deliverable(execution, tmp_path):
    user, machine, model, worker_id, detail, claim = execution
    artifact, loaded = _runner_registered_upload(detail, claim, tmp_path, copy_instructions=True)
    assert artifact.sha256 == sha256(loaded).hexdigest()
    with db.get_conn() as conn:
        assert not any(json.loads(row["metadata_json"]).get("source_path") == "/opt/cairn/AGENTS.md" for row in conn.execute(
            "SELECT metadata_json FROM artifacts WHERE project_id = ?", (detail["project_id"],),
        ).fetchall())
        vulnerability = create_vulnerability(conn, detail["project_id"], CreateVulnerabilityRequest(
            title="合成指引副本", severity="low", evidence_items=[{
                    "id": "original", "kind": "artifact", "source": "manual", "artifact": {"artifact_id": artifact.id},
                }],
        ))
    assert not get_verification_request(detail["id"], user)["artifacts"]
    with pytest.raises(HTTPException) as hidden:
        verification_artifact(detail["id"], artifact.id, user)
    assert hidden.value.status_code == 404
    with pytest.raises(HTTPException) as rejected:
        submit_verification_result(detail["id"], _submission(detail, claim, [artifact]), machine)
    assert rejected.value.detail == "verification_evidence_not_current_or_deliverable"
    copied, _ = _new_execution(user, machine, worker_id, report={
        "type": "platform_vulnerability", "project_id": detail["project_id"], "vulnerability_id": vulnerability.id,
    })
    assert not any(item["path"].startswith("_verification/historical-evidence/") for item in copied["materials"])
    manifest = next(item for item in copied["materials"] if item["path"] == "_verification/source-manifest.json")
    snapshot = json.loads(verification_material(copied["id"], manifest["id"], user).content)
    assert any(item["code"] == "source_artifact_not_deliverable" for item in snapshot["missing_materials"])


def test_missing_changed_originals_and_stale_execution_are_rejected(execution):
    user, machine, model, worker_id, detail, claim = execution
    missing = _upload(detail, claim, "log", "missing.log", b"synthetic missing original")
    changed = _upload(detail, claim, "raw_output", "changed.txt", b"synthetic original")
    missing_path = verification_artifact(detail["id"], missing.id, user).disk_path
    changed_path = verification_artifact(detail["id"], changed.id, user).disk_path
    missing_path.unlink()
    changed_path.write_bytes(b"different bytes")
    for artifact in (missing, changed):
        with pytest.raises(HTTPException) as rejected:
            submit_verification_result(detail["id"], _submission(detail, claim, [artifact]), machine)
        assert rejected.value.status_code in {409, 422}
    valid = _upload(detail, claim, "log", "valid.log", b"synthetic valid original")
    with pytest.raises(HTTPException) as changed_registration:
        _upload(detail, claim, "log", "bad-registration.log", b"synthetic bytes", metadata={"registered_sha256": "0" * 64})
    assert changed_registration.value.detail == "registered_content_version_mismatch"
    stale = _submission(detail, claim, [valid])
    stale.execution_generation += 1
    with pytest.raises(HTTPException) as rejected:
        submit_verification_result(detail["id"], stale, machine)
    assert rejected.value.status_code == 409
    accepted = submit_verification_result(detail["id"], _submission(detail, claim, [valid]), machine)
    assert [ref["artifact_id"] for ref in accepted["result"]["evidence_refs"]] == [valid.id]
    verification_artifact(detail["id"], valid.id, user).disk_path.write_bytes(b"changed after acceptance")
    with pytest.raises(HTTPException) as rejected:
        get_verification_request(detail["id"], user)
    assert rejected.value.status_code == 409


def test_platform_source_selects_one_observation_and_preserves_project_context(execution):
    from cairn.server.services.project_resources import _insert_prepared_project_resources, _project_resource_disk_path, read_project_resource_content
    from cairn.server.services.verification_sources import (
        SOURCE_MANIFEST_PATH, SOURCE_REPORT_PATH, read_platform_vulnerability_source, save_platform_vulnerability_source,
    )

    user, machine, model, worker_id, detail, claim = execution
    project_id = detail["project_id"]
    request = _upload(detail, claim, "file", "request.http", b"GET /first HTTP/1.1\r\n\r\n ")
    response = _upload(detail, claim, "file", "response.http", b"HTTP/1.1 200 OK\r\n\r\nsecond\r\n")
    unrelated = _upload(detail, claim, "file", "old-verification.txt", b"unrelated verification")
    blocked = _upload(detail, claim, "file", "blocked.txt", b"synthetic disallowed input")
    origin = "Exact Origin\r\n仅允许合成范围，见 `scope.md`、`blocked-scope.md`、`missing-scope.md`；排除示例已登记。  \r\n"
    goal = "Exact Goal\r\n保留真实缺侧。 \t"
    first_items = [{"id": "first", "kind": "http", "source": "capture", "request": {"artifact_id": request.id}}]
    second_items = [
        {"id": "second", "kind": "http", "source": "capture", "response": {"artifact_id": response.id}},
        {"id": "blocked", "kind": "artifact", "source": "capture", "artifact": {"artifact_id": blocked.id}},
    ]
    with db.get_conn() as conn:
        conn.execute("UPDATE facts SET description = ? WHERE project_id = ? AND id = 'origin'", (origin, project_id))
        conn.execute("UPDATE facts SET description = ? WHERE project_id = ? AND id = 'goal'", (goal, project_id))
        for fact_id in ("finding-a", "finding-b"):
            conn.execute("INSERT INTO facts (id, project_id, description, task_run_id, creator, created_at) VALUES (?, ?, ?, ?, 'synthetic', ?)",
                         (fact_id, project_id, fact_id + " original\r\n ", claim["task_run"]["id"], utcnow()))
        vulnerability = create_vulnerability(conn, project_id, CreateVulnerabilityRequest(
            title="two observations", severity="low", source_fact_id="finding-a", evidence_items=first_items,
        ), discovered_by_run_id=claim["task_run"]["id"], discovered_by_worker=worker_id)
        first_id = conn.execute("SELECT id FROM vulnerability_observations WHERE project_id = ? AND vulnerability_id = ?",
                                (project_id, vulnerability.id)).fetchone()["id"]
        conn.execute("UPDATE vulnerability_observations SET observed_at = '2026-01-01T00:00:00Z' WHERE project_id = ? AND id = ?", (project_id, first_id))
        conn.execute(
            "INSERT INTO vulnerability_observations (id, project_id, vulnerability_id, source_fact_id, task_run_id, severity, confidence, evidence_items_json, observed_at) "
            "VALUES ('selected-b', ?, ?, 'finding-b', ?, 'low', 0.5, ?, '2026-01-02T00:00:00Z')",
            (project_id, vulnerability.id, claim["task_run"]["id"], json.dumps(second_items)),
        )
        conn.execute(
            "INSERT INTO vulnerability_verifications (id, project_id, vulnerability_id, observation_id, intent_id, fact_id, "
            "verifier_task_run_id, verifier_worker, discovery_task_run_id, independence_level, outcome, evidence_json, notes, created_at) "
            "VALUES ('old-check', ?, ?, ?, ?, 'finding-a', ?, ?, ?, 'same_worker', 'confirmed', ?, 'historical only', ?)",
            (project_id, vulnerability.id, first_id, claim["task_run"]["intent_id"], claim["task_run"]["id"], worker_id,
             claim["task_run"]["id"], json.dumps([{"artifact_id": unrelated.id}]), utcnow()),
        )
        conn.execute("UPDATE vulnerabilities SET packet_artifact_id = ?, repro_request = 'archived canonical request', repro_response = 'archived canonical response' WHERE project_id = ? AND id = ?",
                     (unrelated.id, project_id, vulnerability.id))
        conn.execute("INSERT INTO targets (id, project_id, value, type, status, created_at) VALUES ('excluded', ?, 'excluded.example.test', 'domain', 'excluded', ?)", (project_id, utcnow()))
        source_resources = _insert_prepared_project_resources(conn, project_id, [
            ("note", "scope.md", b"exact scope original\r\n \t"), ("note", "unmentioned.md", b"not related"),
            ("note", "blocked-scope.md", b"synthetic forbidden context"), ("note", "missing-scope.md", b"synthetic missing context"),
        ], created_by=user.subject_id)
        source_resource_ids = {item.path: item.id for item in source_resources}
        _project_resource_disk_path(project_id, source_resource_ids["missing-scope.md"]).unlink()
        snapshot, originals = read_platform_vulnerability_source(
            conn, project_id, vulnerability.id, user,
            artifact_allowed=lambda connection, artifact: artifact["id"] != blocked.id,
            resource_allowed=lambda connection, resource: resource["path"] != "blocked-scope.md",
        )
        assert snapshot["evidence"]["observation_id"] == "selected-b"
        assert snapshot["evidence"]["evidence_items"][0]["response"]["artifact_id"] == response.id
        assert "request" not in snapshot["evidence"]["evidence_items"][0]
        assert [item["id"] for item in snapshot["facts"]] == ["finding-b"]
        assert [item["id"] for item in originals] == [response.id]
        assert {item["artifact_id"] for item in snapshot["artifacts"]} == {response.id, blocked.id}
        assert any(item["code"] == "source_artifact_not_deliverable" for item in snapshot["missing_materials"])
        assert len(snapshot["observations"]) == 2 and snapshot["verifications"][0]["id"] == "old-check"
        assert "provenance" in snapshot["history_path"]
        assert not {"packet_artifact_id", "repro_request", "repro_response"}.intersection(snapshot["vulnerability"])
        context = snapshot["project_context"]
        assert context["origin"]["description"] == origin and context["goal"]["description"] == goal
        assert context["targets"][0]["status"] == "excluded"
        assert [item["path"] for item in context["resource_refs"]] == ["blocked-scope.md", "missing-scope.md", "scope.md"]
        resource_refs = {item["path"]: item for item in context["resource_refs"]}
        assert resource_refs["scope.md"]["available"]
        assert resource_refs["scope.md"]["id"] == source_resource_ids["scope.md"]
        assert resource_refs["scope.md"]["sha256"] == sha256(b"exact scope original\r\n \t").hexdigest()
        assert not resource_refs["blocked-scope.md"]["available"] and not resource_refs["missing-scope.md"]["available"]
        assert {"project_context_resource_not_input", "project_context_resource_unreadable"}.issubset({item["code"] for item in snapshot["missing_materials"]})
        selected_a, selected_originals = read_platform_vulnerability_source(
            conn, project_id, vulnerability.id, user, observation_id=first_id,
            artifact_allowed=lambda connection, artifact: True, resource_allowed=lambda *_: True,
        )
        assert [item["id"] for item in selected_a["facts"]] == ["finding-a"]
        assert [item["id"] for item in selected_originals] == [request.id]
        assert "response" not in selected_a["evidence"]["evidence_items"][0]
        with pytest.raises(HTTPException) as invalid_observation:
            read_platform_vulnerability_source(conn, project_id, vulnerability.id, user,
                                               observation_id="not-a-member", artifact_allowed=lambda *_: True, resource_allowed=lambda *_: True)
        assert invalid_observation.value.status_code == 404
        _, resources = save_platform_vulnerability_source(conn, project_id, snapshot, originals, created_by=user.subject_id)
        ids = {item.path: item.id for item in resources}
    stored = json.loads(read_project_resource_content(project_id, ids[SOURCE_REPORT_PATH]).content)
    manifest = json.loads(read_project_resource_content(project_id, ids[SOURCE_MANIFEST_PATH]).content)
    assert stored["project_context"]["origin"]["description"] == origin
    assert manifest["source"]["observation_id"] == "selected-b"
    assert manifest["evidence"]["item_ids"] == ["second", "blocked"]
    assert manifest["project_context"] == {"snapshot_path": SOURCE_REPORT_PATH, "pointer": "/project_context"}
    copied_path = resource_refs["scope.md"]["input_path"]
    assert read_project_resource_content(project_id, ids[copied_path]).content == b"exact scope original\r\n \t"
    assert manifest["resources"] == context["resource_refs"]
    assert sum(path.startswith("_verification/source-resources/") for path in ids) == 1


def test_platform_source_copy_rechecks_context_resource_bytes(execution):
    from cairn.server.services.project_resources import _insert_prepared_project_resources, _project_resource_disk_path
    from cairn.server.services.verification_sources import read_platform_vulnerability_source, save_platform_vulnerability_source

    user, machine, model, worker_id, detail, claim = execution
    with db.get_conn() as conn:
        conn.execute("UPDATE facts SET description = '明确资料 `scope.txt`' WHERE project_id = ? AND id = 'origin'", (detail["project_id"],))
        resource = _insert_prepared_project_resources(conn, detail["project_id"], [("note", "scope.txt", b"captured original\r\n")], created_by=user.subject_id)[0]
        vulnerability = create_vulnerability(conn, detail["project_id"], CreateVulnerabilityRequest(title="context copy", severity="low"))
        snapshot, artifacts = read_platform_vulnerability_source(
            conn, detail["project_id"], vulnerability.id, user,
            artifact_allowed=lambda *_: True, resource_allowed=lambda *_: True,
        )
    target, _ = _new_execution(user, machine, worker_id)
    _project_resource_disk_path(detail["project_id"], resource.id).write_bytes(b"changed original")
    with pytest.raises(HTTPException) as changed:
        with db.get_conn() as conn:
            save_platform_vulnerability_source(conn, target["project_id"], snapshot, artifacts, created_by=user.subject_id)
    assert changed.value.status_code == 409
    with db.get_conn() as conn:
        assert conn.execute("SELECT count(*) FROM project_resources WHERE project_id = ? AND path LIKE '_verification/source-resources/%'", (target["project_id"],)).fetchone()[0] == 0


@pytest.mark.parametrize("source", ["platform", "text"])
def test_request_without_additional_origin_preserves_source_in_claim(execution, source):
    user, machine, model, worker_id, detail, claim = execution
    original = "真实合成背景与范围\r\n仅允许本地合成材料。  \t"
    title = "真实合成漏洞主张"
    if source == "platform":
        from cairn.server.services.project_resources import _insert_prepared_project_resources

        original += "\r\n明确关联资料：`identity.txt` 与 `platform-secret.txt`。"
        identity = b"Authorization: Bearer synthetic-target-context\r\n \t"
        with db.get_conn() as conn:
            _insert_prepared_project_resources(conn, detail["project_id"], [
                ("note", "identity.txt", identity), ("note", "platform-secret.txt", b"synthetic-platform-model-secret"),
            ], created_by=user.subject_id)
            conn.execute("UPDATE facts SET description = ? WHERE project_id = ? AND id = 'origin'", (original, detail["project_id"]))
            vulnerability = create_vulnerability(conn, detail["project_id"], CreateVulnerabilityRequest(
                title=title, severity="low", target="synthetic://local-only", evidence_items=[{
                    "id": "manual", "kind": "text", "source": "manual", "text": "历史主张原文\r\n ",
                }],
            ))
        report = {"type": "platform_vulnerability", "project_id": detail["project_id"], "vulnerability_id": vulnerability.id}
    else:
        report = {"type": "text", "content": original}
    body = CreateVerificationRequest(schema_version=1, request_id=uuid4(), mode="retest", title=title, report=report)
    assert "origin" not in body.model_fields_set
    created, replayed = create_verification_request(body, user)
    assert not replayed
    claimed = claim_verification_request(created["id"], VerificationClaimRequest(task_run=CreateTaskRunRequest(
        task_type="external_verify", worker_type="pi", worker_name=worker_id,
        session_id=f"pi:{uuid4()}", expected_execution_generation=0,
        runtime_context={"dispatcher_id": machine.subject_id},
    )), machine)
    context = json.loads(claimed["input"]["origin"])
    assert "additional_context" not in context
    if source == "platform":
        assert context["project_context"]["origin"]["description"] == original
        assert context["vulnerability"]["title"] == title
        assert context["vulnerability"]["target"] == "synthetic://local-only"
        snapshot_material = next(item for item in created["materials"] if item["path"] == "_verification/platform-vulnerability.json")
        snapshot = json.loads(verification_material(created["id"], snapshot_material["id"], user).content)
        assert snapshot["evidence"]["evidence_items"][0]["text"] == "历史主张原文\r\n "
        refs = {item["path"]: item for item in snapshot["project_context"]["resource_refs"]}
        assert refs["identity.txt"]["available"] and not refs["platform-secret.txt"]["available"]
        identity_material = next(item for item in created["materials"] if item["path"] == refs["identity.txt"]["input_path"])
        assert verification_material(created["id"], identity_material["id"], user).content == identity
        assert sum(item["path"].startswith("_verification/source-resources/") for item in created["materials"]) == 1
    else:
        assert context["report"]["content"] == original


def _saved_platform_input(execution):
    from cairn.server.services.ids import next_project_id
    from cairn.server.services.project_resources import _insert_prepared_project_resources

    user, machine, model, worker_id, detail, claim = execution
    with db.get_conn() as conn:
        source_id = next_project_id(conn)
        # 普通来源项目只存在于隔离库；复制实际场景配置，不创建可执行任务。
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, tenant_id, scenario_key, scenario_snapshot_json, "
            "assessment_protocol, authorization_expires_at) "
            "SELECT ?, '合成来源项目', 'paused', ?, tenant_id, scenario_key, scenario_snapshot_json, assessment_protocol, ? "
            "FROM projects WHERE id = ?",
            (source_id, utcnow(), "2099-05-01T00:00:00Z", detail["project_id"]),
        )
        for fact_id, description in (("origin", "只读合成范围，关联 `scope.txt`；不访问网络。  \r\n"), ("goal", "验证合成主张")):
            conn.execute("INSERT INTO facts (id, project_id, description, creator, created_at) VALUES (?, ?, ?, ?, ?)",
                         (fact_id, source_id, description, user.subject_id, utcnow()))
        _insert_prepared_project_resources(conn, source_id, [("note", "scope.txt", b"exact saved scope\r\n \t")], created_by=user.subject_id)
        vulnerability = create_vulnerability(conn, source_id, CreateVulnerabilityRequest(
            title="合成旧快照", severity="low", evidence_items=[{
                "id": "request", "kind": "http", "source": "manual", "request_text": "GET /saved HTTP/1.1\r\n\r\n ",
            }],
        ))
    body = CreateVerificationRequest(
        schema_version=1, request_id=uuid4(), mode="retest", title="保存平台输入",
        authorization_expires_at="2099-04-01T00:00:00Z",
        report={"type": "platform_vulnerability", "project_id": source_id, "vulnerability_id": vulnerability.id},
    )
    previous, replayed = create_verification_request(body, user)
    assert not replayed
    return source_id, vulnerability.id, previous


def _copied_snapshot_body(previous, user, *, expires_at=None):
    batch = create_verification_batch(previous["id"], user)
    main = next(item for item in batch.members if item.source_resource_id == previous["report"]["resource_id"])
    return CreateVerificationRequest(
        schema_version=1, request_id=uuid4(), mode="retest", title="复制已保存平台输入",
        previous_request_id=previous["id"], resource_batch_id=batch.id,
        authorization_expires_at=expires_at, report={"type": "file", "batch_member_id": main.id},
    ), batch


@pytest.mark.parametrize("expires_at, expected", [
    (None, "2099-04-01T00:00:00Z"),
    ("2099-06-01T00:00:00Z", "2099-04-01T00:00:00Z"),
    ("2099-03-01T00:00:00Z", "2099-03-01T00:00:00Z"),
])
def test_copied_platform_snapshot_survives_source_deletion_and_keeps_expiry(execution, expires_at, expected):
    from cairn.server.services.project_records import delete_project_record

    user, machine, model, worker_id, detail, claim = execution
    source_id, vulnerability_id, previous = _saved_platform_input(execution)
    originals = {item["path"]: verification_material(previous["id"], item["id"], user).content for item in previous["materials"]}
    delete_project_record(source_id)
    body, _ = _copied_snapshot_body(previous, user, expires_at=expires_at)
    copied, replayed = create_verification_request(body, user)
    assert not replayed and copied["id"] != previous["id"]
    assert copied["previous_request_id"] == previous["id"]
    assert copied["authorization_expires_at"] == expected
    assert copied["input_source"] == {
        "type": "platform_vulnerability", "project_id": source_id,
        "vulnerability_id": vulnerability_id, "input_mode": "copied_snapshot",
    }
    assert {item["path"]: verification_material(copied["id"], item["id"], user).content for item in copied["materials"]} == originals
    replay, replayed = create_verification_request(body, user)
    assert replayed and replay["id"] == copied["id"]
    assert get_verification_request(previous["id"], user)["authorization_expires_at"] == "2099-04-01T00:00:00Z"
    with pytest.raises(HTTPException) as deleted_source:
        create_verification_request(CreateVerificationRequest(
            schema_version=1, request_id=uuid4(), mode="retest", title="重新选择不存在来源",
            report={"type": "platform_vulnerability", "project_id": source_id, "vulnerability_id": vulnerability_id},
        ), user)
    assert deleted_source.value.status_code == 404


@pytest.mark.parametrize("damage", ["incomplete", "missing", "changed", "expired"])
def test_copied_platform_snapshot_rejects_invalid_saved_input_and_expired_boundary(execution, damage):
    from cairn.server.services.project_records import delete_project_record
    from cairn.server.services.project_resources import _batch_member_disk_path
    from cairn.server.services.verification_sources import SOURCE_MANIFEST_PATH

    user, machine, model, worker_id, detail, claim = execution
    source_id, _, previous = _saved_platform_input(execution)
    delete_project_record(source_id)
    body, batch = _copied_snapshot_body(previous, user, expires_at="2099-06-01T00:00:00Z")
    manifest = next(item for item in batch.members if item.path == SOURCE_MANIFEST_PATH)
    with db.get_conn() as conn:
        original_count = conn.execute("SELECT count(*) FROM verification_requests").fetchone()[0]
        if damage == "incomplete":
            conn.execute("DELETE FROM project_resource_batch_members WHERE batch_id = ? AND id = ?", (batch.id, manifest.id))
        elif damage == "expired":
            conn.execute("UPDATE projects SET authorization_expires_at = '2000-01-01T00:00:00Z' WHERE id = ?", (previous["project_id"],))
    if damage == "missing":
        _batch_member_disk_path(batch.id, manifest.id).unlink()
    elif damage == "changed":
        _batch_member_disk_path(batch.id, manifest.id).write_bytes(b"replaced source manifest")
    with pytest.raises(HTTPException) as rejected:
        create_verification_request(body, user)
    assert rejected.value.status_code == (422 if damage in {"incomplete", "expired"} else 409)
    if damage == "expired":
        assert rejected.value.detail == "authorization_expires_at_not_future"
    with db.get_conn() as conn:
        assert conn.execute("SELECT count(*) FROM verification_requests").fetchone()[0] == original_count


def test_target_authorization_is_historical_input_but_not_public_delivery(execution):
    user, machine, model, worker_id, detail, claim = execution
    original = b"GET /synthetic HTTP/1.1\r\nAuthorization: Bearer synthetic-target-credential\r\n\r\n  "
    artifact = _upload(detail, claim, "file", "authorized-request.http", original)
    with db.get_conn() as conn:
        vulnerability = create_vulnerability(conn, detail["project_id"], CreateVulnerabilityRequest(
            title="synthetic authorized request", severity="low", evidence_items=[{
                "id": "request", "kind": "http", "source": "manual", "request": {"artifact_id": artifact.id},
            }],
        ))
    snapshot = preview_platform_vulnerability(detail["project_id"], vulnerability.id, user)
    assert snapshot["artifacts"][0]["available"]
    copied, _ = _new_execution(user, machine, worker_id, report={
        "type": "platform_vulnerability", "project_id": detail["project_id"], "vulnerability_id": vulnerability.id,
    })
    historical = next(item for item in copied["materials"] if item["path"].startswith("_verification/historical-evidence/"))
    assert verification_material(copied["id"], historical["id"], user).content == original
    from cairn.server.services.verification_requests import _artifact_deliverable, _artifact_input_allowed
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM artifacts WHERE project_id = ? AND id = ?", (detail["project_id"], artifact.id)).fetchone()
        assert _artifact_input_allowed(conn, row)
        assert not _artifact_deliverable(conn, row)
    leaking = _submission(detail, claim, [artifact])
    leaking.result.summary = "Authorization: Bearer synthetic-target-credential"
    with pytest.raises(HTTPException) as public_text:
        submit_verification_result(detail["id"], leaking, machine)
    assert public_text.value.detail == "verification_sensitive_result_rejected"
    accepted = submit_verification_result(detail["id"], _submission(detail, claim, [artifact]), machine)
    assert accepted["result"]["evidence_items"][0]["artifact"]["artifact_id"] == artifact.id
    assert verification_artifact(detail["id"], artifact.id, user).disk_path.read_bytes() == original
