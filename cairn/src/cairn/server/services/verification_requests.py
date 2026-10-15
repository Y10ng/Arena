"""外部复测的原子受理、唯一终结和集中查询投影。"""

from __future__ import annotations

from hashlib import sha256
import hmac
import json
from pathlib import Path
import re
from uuid import uuid4

from fastapi import HTTPException

from cairn.server.auth import RequestPrincipal
from cairn.server.db import get_conn, schedule_transaction
from cairn.server.schemas.verification_requests import (
    CreateVerificationRequest, VerificationClaimRequest, VerificationExecutionBinding,
    VerificationFailureSubmission, VerificationResultSubmission,
)

from .audit_artifacts import _artifact_disk_path_from_row, artifact_content_file
from .audit_task_runs import authenticated_execution_owner, insert_task_run_record
from .ids import get_project_or_404, next_intent_id
from .payload_values import dump_json
from .project_authorization import project_authorization_block_reason, require_project_authorization_current
from .project_records import create_verification_project_record
from .project_resources import (
    _batch_member_disk_path, _check_batch_additions,
    _insert_prepared_project_resources, _list_resource_batch_members, _lock_owned_open_batch,
    _project_resource_disk_path, consume_project_resource_batch, copy_verified_resource_content,
    create_project_resource_batch, get_project_resource_batch, infer_project_resource_kind,
    read_project_resource_content, validate_resource_content,
)
from .projects import lease_clock, task_run_to_model
from .runtime_context import raw_task_run_runtime_context
from .tenant_workers import project_worker_bindings
from .time_utils import parse_utc_timestamp, utcnow
from .verification_configuration import resolve_verification_configuration
from .verification_sources import (
    SOURCE_ARTIFACT_ROOT, SOURCE_MANIFEST_PATH, artifact_is_verification_material,
    read_platform_vulnerability_source, require_platform_source_user,
    save_platform_vulnerability_source, source_project,
)

TERMINAL_RUN_STATUSES = {"succeeded", "failed", "cancelled", "timed_out"}
PUBLIC_SECRET_PATTERN = re.compile(
    r"(?i)(?:\b(?:bearer|basic)\s+[a-z0-9+/_.=-]{8,}|-----BEGIN [^-]*PRIVATE KEY-----|"
    r"\b(?:password|passwd|api[_-]?key|access[_-]?token|client[_-]?secret)\s*[=:]\s*[^\s,;]{4,})"
)


def _digest(value: dict) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def caller_scope(principal: RequestPrincipal) -> dict:
    if principal.kind not in {"user", "integration"} or not principal.tenant_id:
        raise HTTPException(403, "复测需要租户用户或集成身份")
    return {"tenant_id": principal.tenant_id, "user_id": principal.subject_id if principal.kind == "user" else None,
            "integration_id": principal.subject_id if principal.kind == "integration" else None}


def _owned_request(conn, request_id: str, principal: RequestPrincipal):
    sql = "SELECT * FROM verification_requests WHERE id = ?"
    params = [request_id]
    if principal.kind == "integration":
        sql += " AND tenant_id = ? AND caller_type = 'integration' AND caller_id = ?"
        params.extend([principal.tenant_id, principal.subject_id])
    elif principal.kind == "user" and not principal.is_super_admin:
        sql += " AND tenant_id = ?"
        params.append(principal.tenant_id)
    elif principal.kind not in {"user", "machine"}:
        raise HTTPException(403, "复测身份无权访问")
    row = conn.execute(sql, tuple(params)).fetchone()
    if row is None:
        raise HTTPException(404, "复测请求不存在")
    return row


def _locked_request(conn, request_id: str, principal: RequestPrincipal):
    row = _owned_request(conn, request_id, principal)
    # 所有终结竞争固定项目→请求锁序，与 TaskRun 更新和 Artifact 写入一致。
    conn.execute("UPDATE projects SET id = id WHERE id = ?", (row["project_id"],))
    return _owned_request(conn, request_id, principal)


def _run_ended(run) -> bool:
    lifecycle = raw_task_run_runtime_context(run).get("execution_lifecycle", {})
    return (run["status"] in TERMINAL_RUN_STATUSES and lifecycle.get("generation") == run["execution_generation"]
            and lifecycle.get("termination_confirmed") is True)


def _execution_status(row, runs: list) -> str:
    ended = all(_run_ended(run) for run in runs)
    if row["terminal_kind"] is not None:
        if not ended:
            return "cancelling" if row["terminal_kind"] == "cancelled" or row["cancel_requested_at"] else "running"
        return {"result": "completed", "failure": "failed", "cancelled": "cancelled"}[row["terminal_kind"]]
    if row["cancel_requested_at"]:
        return "cancelling"
    return "running" if runs else "queued"


def _source(conn, row) -> dict:
    if row["caller_type"] == "integration":
        name = conn.execute("SELECT name FROM verification_integrations WHERE id = ?", (row["caller_id"],)).fetchone()
        display = name["name"] if name else "集成"
    else:
        name = conn.execute("SELECT login FROM users WHERE id = ?", (row["caller_id"],)).fetchone()
        display = name["login"] if name else "用户"
    return {"type": row["caller_type"], "id": row["caller_id"], "name": display}


def _input_source(row) -> dict | None:
    if row["source_project_id"] is None:
        return None
    return {"type": "platform_vulnerability", "project_id": row["source_project_id"],
            "vulnerability_id": row["source_vulnerability_id"],
            "input_mode": "current_vulnerability" if row["report_type"] == "platform_vulnerability" else "copied_snapshot"}


def _input_report(conn, row) -> dict:
    report = {"type": row["report_type"], "resource_id": row["report_resource_id"]}
    source = _input_source(row)
    if source is not None:
        manifest = conn.execute("SELECT * FROM project_resources WHERE project_id = ? AND path = ?",
                                (row["project_id"], SOURCE_MANIFEST_PATH)).fetchone()
        if manifest is None:
            raise HTTPException(409, "已受理的平台漏洞输入缺少来源清单")
        report.update(source=source, material_role="historical_input", manifest_resource_id=manifest["id"],
                      manifest_path=manifest["path"],
                      notice="主资料与历史证据是受理时保存的旧输入，不代表本轮观察；缺项见来源清单，不扩大本轮授权。")
    return report


def _summary(conn, row, *, runs=None) -> dict:
    if runs is None:
        runs = conn.execute("SELECT * FROM task_runs WHERE project_id = ? ORDER BY started_at, id", (row["project_id"],)).fetchall()
    project = get_project_or_404(conn, row["project_id"])
    return {"schema_version": 1, "id": row["id"], "request_id": row["client_request_id"],
            "external_id": row["external_id"], "mode": row["mode"], "title": project["title"],
            "source": _source(conn, row), "input_source": _input_source(row), "execution_status": _execution_status(row, runs),
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error": json.loads(row["error_json"]) if row["error_json"] else None,
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def _materials(conn, project_id: str) -> list[dict]:
    return [{key: row[key] for key in ("id", "path", "kind", "size_bytes", "sha256", "derived_from_resource_id")}
            for row in conn.execute("SELECT * FROM project_resources WHERE project_id = ? ORDER BY created_at, id", (project_id,)).fetchall()]


def _platform_secrets(conn, project_id: str, task_run_id: str | None = None) -> list[bytes]:
    from .model_credentials import (
        CredentialConfigurationError, CredentialDecryptionError, decrypt_credential_version,
    )
    project = get_project_or_404(conn, project_id)
    secrets = []
    runs = conn.execute(
        "SELECT * FROM task_runs WHERE project_id = ? AND " + ("id = ?" if task_run_id else "task_type = 'external_verify'"),
        (project_id, task_run_id) if task_run_id else (project_id,),
    ).fetchall()
    if task_run_id and not runs:
        raise HTTPException(409, "verification_execution_secrets_unavailable")
    for run in runs:
        binding = raw_task_run_runtime_context(run).get("execution_binding") or {}
        model = conn.execute("SELECT * FROM tenant_models WHERE tenant_id = ? AND id = ?",
                             (project["tenant_id"], binding.get("model_id"))).fetchone()
        # 无法还原当次来源时拒绝新交付，不假称已检查历史凭据，也不改写已有档案。
        if model is None or not binding.get("credential_version_id"):
            raise HTTPException(409, "verification_execution_secrets_unavailable")
        try:
            secret, credential = decrypt_credential_version(
                conn, tenant_id=project["tenant_id"], model_id=binding["model_id"],
                version_id=binding["credential_version_id"],
            )
        except (HTTPException, CredentialConfigurationError, CredentialDecryptionError) as exc:
            raise HTTPException(409, "verification_execution_secrets_unavailable") from exc
        if credential["credential_fingerprint"] != binding.get("credential_fingerprint"):
            raise HTTPException(409, "verification_execution_secrets_unavailable")
        headers = json.loads(model["custom_headers_json"] or "[]")
        headers_digest = sha256(json.dumps(headers, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        expected_headers = binding.get("custom_headers_sha256")
        if (expected_headers is not None and headers_digest != expected_headers) or (
                expected_headers is None and model["revision"] != binding.get("model_revision")):
            raise HTTPException(409, "verification_execution_headers_unavailable")
        secrets.append(secret.encode("utf-8"))
        secrets.extend(header["value"].encode("utf-8") for header in headers if header.get("value"))
    return secrets


def _artifact_deliverable(conn, artifact) -> bool:
    if not artifact_is_verification_material(artifact):
        return False
    # 二进制证据按字节检查，不把截图当成 UTF-8 文本，也不修改权威文件内容。
    pattern = re.compile(PUBLIC_SECRET_PATTERN.pattern.encode("ascii"), re.IGNORECASE)
    private_markers = (b"/root/", b"/home/", b"/var/lib/cairn", b".pi/agent", b"CAIRN_MACHINE_TOKEN", b"credential_version_id", b"\"runtime_context\"", b"\"apiKey\"")
    try:
        secrets = _platform_secrets(conn, artifact["project_id"], artifact["task_run_id"])
    except HTTPException:
        return False
    overlap = b""
    try:
        with _artifact_disk_path_from_row(artifact).open("rb") as stream:
            while chunk := stream.read(64 * 1024):
                content = overlap + chunk
                if pattern.search(content) or any(marker in content for marker in (*private_markers, *secrets)):
                    return False
                overlap = content[-max([8192, *(len(secret) for secret in secrets)]):]
    except OSError:
        return False
    return True


def _public_artifacts(conn, row) -> list:
    accepted_ids = {ref["artifact_id"] for ref in json.loads(row["result_json"])["evidence_refs"]} if row["terminal_kind"] == "result" else set()
    artifacts = []
    for artifact in conn.execute(
        "SELECT * FROM artifacts WHERE project_id = ? AND task_run_id = ? ORDER BY created_at, id",
        (row["project_id"], row["current_task_run_id"]),
    ).fetchall():
        if artifact["id"] in accepted_ids:
            # 接受事实来自终结结果；只核对原版本，不让后来配置撤回已接受的证据。
            validate_resource_content(_artifact_disk_path_from_row(artifact), path=artifact["id"],
                                      size_bytes=artifact["size"], digest_sha256=artifact["sha256"])
        elif not _artifact_deliverable(conn, artifact):
            continue
        artifacts.append(artifact)
    return artifacts


def _cleanup(runs: list) -> dict:
    if not runs:
        return {"status": "not_allocated", "error": None}
    cleanup = [raw_task_run_runtime_context(run).get("external_verification_cleanup") for run in runs]
    if all(isinstance(item, dict) and item.get("status") == "removed" for item in cleanup):
        return {"status": "removed", "error": None}
    error = next((item.get("error") for item in reversed(cleanup) if isinstance(item, dict) and item.get("error")), None)
    return {"status": "pending", "error": error}


def _execution_message(conn, row, project, runs: list, bindings: list) -> str | None:
    if runs or row["terminal_kind"] is not None or row["cancel_requested_at"]:
        return None
    authorization_reason = project_authorization_block_reason(conn, project)
    if authorization_reason:
        return "本次复测授权已过期，无法执行。" if authorization_reason == "project_authorization_expired" else "本次复测授权已失效，无法执行。"
    deadline = parse_utc_timestamp(project["explore_deadline"])
    if deadline and deadline <= parse_utc_timestamp(utcnow()):
        return "本次复测已到执行截止时间。"
    if not bindings:
        return "本次复测缺少专用 Worker 绑定，无法执行。"
    try:
        worker_id = resolve_verification_configuration(conn, row["tenant_id"])
    except HTTPException as exc:
        return str(exc.detail)
    if len(bindings) != 1 or bindings[0]["id"] != worker_id:
        return "本次属于旧执行绑定，不能自动替换为新专用 Worker；请明确发起新一轮复测。"
    return "尚未开始，将使用首次认领时的专用 Worker 与模型配置；暂时满载时排队。"


def _detail(conn, row, principal: RequestPrincipal) -> dict:
    project = get_project_or_404(conn, row["project_id"])
    runs = conn.execute("SELECT * FROM task_runs WHERE project_id = ? ORDER BY started_at, id", (row["project_id"],)).fetchall()
    bindings = project_worker_bindings(conn, row["project_id"])
    origin = conn.execute("SELECT description FROM facts WHERE project_id = ? AND id = 'origin'", (row["project_id"],)).fetchone()
    history = []
    candidates = conn.execute(
        "SELECT * FROM verification_requests WHERE tenant_id = ? AND "
        "((caller_type = ? AND caller_id = ? AND external_id = ? AND external_id IS NOT NULL) OR previous_request_id = ? OR id = ? "
        "OR (source_project_id = ? AND source_vulnerability_id = ? AND source_project_id IS NOT NULL))",
        (row["tenant_id"], row["caller_type"], row["caller_id"], row["external_id"], row["id"], row["previous_request_id"],
         row["source_project_id"], row["source_vulnerability_id"]),
    ).fetchall()
    pending = [row, *candidates]
    linked_ids = set()
    while pending:
        item = pending.pop()
        if item["id"] in linked_ids:
            continue
        linked_ids.add(item["id"])
        if principal.kind == "integration" and (item["caller_type"] != "integration" or item["caller_id"] != principal.subject_id):
            continue
        if item["id"] != row["id"]:
            history.append(_summary(conn, item))
        pending.extend(conn.execute("SELECT * FROM verification_requests WHERE tenant_id = ? AND (id = ? OR previous_request_id = ?)",
                                    (row["tenant_id"], item["previous_request_id"], item["id"])).fetchall())
    history.sort(key=lambda item: (item["created_at"], item["id"]), reverse=True)
    current_run = next((run for run in runs if run["id"] == row["current_task_run_id"]), None)
    provenance = raw_task_run_runtime_context(current_run).get("execution_binding", {}) if current_run else {}
    execution_binding = {key: provenance.get(key) for key in (
        "worker_id", "display_name", "worker_revision", "model_id", "model_name", "model_revision",
    )} if provenance else None
    return _summary(conn, row, runs=runs) | {
        "project_id": row["project_id"], "origin": origin["description"],
        "report": _input_report(conn, row),
        "previous_request_id": row["previous_request_id"], "group_id": project["group_id"],
        "worker_template_id": row["worker_template_id"],
        "tenant_workers": ([{"id": execution_binding["worker_id"],
                             "display_name": execution_binding["display_name"] or execution_binding["worker_id"],
                             "revision": execution_binding["worker_revision"]}] if execution_binding else
                           [{"id": binding["id"], "display_name": binding["id"] if runs else binding["display_name"],
                             "revision": None if not runs else binding["revision_at_bind"]} for binding in bindings]),
        "execution_binding": execution_binding,
        "execution_message": _execution_message(conn, row, project, runs, bindings),
        "authorization_expires_at": project["authorization_expires_at"],
        "execution_deadline": project["explore_deadline"], "materials": _materials(conn, row["project_id"]),
        "artifacts": [{"id": item["id"], "filename": _artifact_disk_path_from_row(item).name,
                       "kind": item["kind"], "size": item["size"], "sha256": item["sha256"], "task_run_id": item["task_run_id"]}
                      for item in _public_artifacts(conn, row)],
        # 对外只给执行简表，内部 session、配置、路径与 runtime_context 不进入此视图。
        "runs": [{"id": run["id"], "worker_name": run["worker_name"], "status": run["status"],
                  "started_at": run["started_at"], "ended_at": run["ended_at"], "error_type": run["error_type"],
                  "error_message": "执行失败，详见本轮终止原因。" if run["error_type"] else None,
                  "execution_generation": run["execution_generation"]} for run in runs],
        "history": history, "cleanup": _cleanup(runs), "accepted_at": row["accepted_at"], "terminal_at": row["terminal_at"],
    }


def get_verification_request(request_id: str, principal: RequestPrincipal) -> dict:
    with get_conn() as conn:
        return _detail(conn, _owned_request(conn, request_id, principal), principal)


def list_verification_requests(principal: RequestPrincipal, *, limit: int, offset: int,
                               external_id=None, mode=None, execution_status=None, outcome=None,
                               source_project_id=None, source_vulnerability_id=None) -> dict:
    caller_scope(principal)
    if (source_project_id is None) != (source_vulnerability_id is None):
        raise HTTPException(422, "来源筛选必须同时指定 source_project_id 和 source_vulnerability_id")
    if source_project_id is not None:
        require_platform_source_user(principal)
    sql = "SELECT * FROM verification_requests WHERE 1 = 1"
    params = []
    if not principal.is_super_admin or source_project_id is not None:
        sql += " AND tenant_id = ?"
        params.append(principal.tenant_id)
    if principal.kind == "integration":
        sql += " AND caller_type = 'integration' AND caller_id = ?"
        params.append(principal.subject_id)
    for column, value in (("external_id", external_id), ("mode", mode),
                          ("source_project_id", source_project_id), ("source_vulnerability_id", source_vulnerability_id)):
        if value is not None:
            sql += f" AND {column} = ?"
            params.append(value)
    with get_conn() as conn:
        items = [_summary(conn, row) for row in conn.execute(sql + " ORDER BY created_at DESC, id DESC", tuple(params)).fetchall()]
    if execution_status is not None:
        items = [item for item in items if item["execution_status"] == execution_status]
    if outcome is not None:
        items = [item for item in items if item["result"] and item["result"]["outcome"] == outcome]
    return {"items": items[offset:offset + limit], "total": len(items)}


def preview_platform_vulnerability(project_id: str, vulnerability_id: str, principal: RequestPrincipal) -> dict:
    require_platform_source_user(principal)
    with get_conn() as conn:
        snapshot, _artifacts = read_platform_vulnerability_source(
            conn, project_id, vulnerability_id, principal, artifact_allowed=_artifact_deliverable,
        )
        return snapshot


def platform_vulnerability_history(project_id: str, vulnerability_id: str, principal: RequestPrincipal,
                                   *, limit: int, offset: int) -> dict:
    require_platform_source_user(principal)
    with get_conn() as conn:
        source_project(conn, project_id, principal)
        if conn.execute("SELECT 1 FROM vulnerabilities WHERE project_id = ? AND id = ?",
                        (project_id, vulnerability_id)).fetchone() is None:
            raise HTTPException(404, "来源漏洞不存在")
    return list_verification_requests(principal, limit=limit, offset=offset,
                                      source_project_id=project_id, source_vulnerability_id=vulnerability_id)


def _write_terminal(conn, row, *, kind: str, digest: str, result=None, error=None) -> None:
    now = utcnow()
    accepted = conn.execute(
        "UPDATE verification_requests SET terminal_kind = ?, terminal_digest = ?, result_json = ?, error_json = ?, "
        "terminal_at = ?, accepted_at = ?, updated_at = ? WHERE id = ? AND terminal_kind IS NULL",
        (kind, digest, dump_json(result) if result is not None else None, dump_json(error) if error is not None else None,
         now, now if kind == "result" else None, now, row["id"]),
    )
    if accepted.rowcount != 1:
        raise HTTPException(409, "verification_terminal_conflict")
    # 独立复测不进入普通 Reason/报告的完成条件；执行终止仍由 TaskRun 事实单独确认。
    conn.execute("UPDATE projects SET status = 'completed' WHERE id = ?", (row["project_id"],))
    conn.execute("UPDATE intents SET concluded_at = ?, conclusion = ?, worker = NULL WHERE project_id = ? AND id = ?",
                 (now, "completed" if kind == "result" else "failed" if kind == "failure" else "abandoned", row["project_id"], row["intent_id"]))


def _copied_platform_source(conn, previous, main, members: list, principal: RequestPrincipal):
    if previous is None or previous["source_project_id"] is None or main["source_resource_id"] != previous["report_resource_id"]:
        return None
    require_platform_source_user(principal)
    _input_report(conn, previous)
    originals = conn.execute(
        "SELECT * FROM project_resources WHERE project_id = ? AND derived_from_resource_id IS NULL ORDER BY path, id",
        (previous["project_id"],),
    ).fetchall()
    # 只有服务端复制映射和原字节元数据都完整时，旧档案才是来源权威；绝不重读当前漏洞。
    for original in originals:
        copied = [item for item in members if item["source_resource_id"] == original["id"]]
        if len(copied) != 1 or any(copied[0][key] != original[key] for key in ("path", "kind", "size_bytes", "sha256")):
            raise HTTPException(422, "复制平台漏洞输入必须保留上次主资料、来源清单和历史资料原件；如需更新来源，请重新选择当前漏洞")
    return previous["source_project_id"], previous["source_vulnerability_id"]


def create_verification_request(body: CreateVerificationRequest, principal: RequestPrincipal) -> tuple[dict, bool]:
    scope = caller_scope(principal)
    # 保留旧协议未显式选择资源时的缺省键，避免历史同输入重放仅因删字段而冲突。
    payload_digest = _digest(body.model_dump(mode="json", exclude={"request_id"})
                             | {"group_id": None, "worker_template_id": None})
    with schedule_transaction() as conn:
        # 同租户的受理先串行化幂等查找；重放不能重新解析默认或已消费的批次。
        conn.execute("UPDATE tenants SET id = id WHERE id = ?", (principal.tenant_id,))
        existing = conn.execute(
            "SELECT * FROM verification_requests WHERE tenant_id = ? AND caller_type = ? AND caller_id = ? AND client_request_id = ?",
            (principal.tenant_id, principal.kind, principal.subject_id, str(body.request_id)),
        ).fetchone()
        if existing is not None:
            if existing["request_digest"] != payload_digest:
                raise HTTPException(409, "同一 request_id 的资料或参数已改变，请使用新的请求 ID")
            return _detail(conn, existing, principal), True
        if body.report.type == "platform_vulnerability":
            require_platform_source_user(principal)
        worker_id = resolve_verification_configuration(conn, principal.tenant_id)
        previous = None
        if body.previous_request_id is not None:
            previous = _owned_request(conn, body.previous_request_id, principal)
            if previous["tenant_id"] != principal.tenant_id:
                raise HTTPException(404, "历史复测请求不存在")
        members = []
        if body.resource_batch_id:
            _lock_owned_open_batch(conn, body.resource_batch_id, **scope)
            members = _list_resource_batch_members(conn, body.resource_batch_id)
        platform_source = None
        source_snapshot, source_artifacts = None, []
        if body.report.type == "file":
            main = next((item for item in members if item["id"] == body.report.batch_member_id), None)
            if main is None:
                raise HTTPException(422, "主资料成员不属于本请求的上传批次")
            platform_source = _copied_platform_source(conn, previous, main, members, principal)
            if platform_source is None and Path(main["path"]).suffix.casefold() not in {".md", ".txt", ".docx", ".png", ".jpg", ".jpeg"}:
                raise HTTPException(422, "主资料仅支持 MD、TXT、DOCX、PNG 或 JPEG；平台快照必须通过上次请求复制")
        elif body.report.type == "platform_vulnerability":
            source_snapshot, source_artifacts = read_platform_vulnerability_source(
                conn, body.report.project_id, body.report.vulnerability_id, principal,
                artifact_allowed=_artifact_deliverable,
            )
            platform_source = body.report.project_id, body.report.vulnerability_id
        project_id = create_verification_project_record(
            conn, title=body.title, origin=body.origin, tenant_id=principal.tenant_id, tenant_worker_id=worker_id,
            creator=principal.subject_id, actor_type=principal.kind, execution_deadline=body.execution_deadline,
            authorization_expires_at=body.authorization_expires_at,
        ).project.id
        resources = consume_project_resource_batch(conn, project_id, body.resource_batch_id, **scope,
                                                    created_by=principal.subject_id, require_stopped=False) if body.resource_batch_id else []
        if body.report.type == "file":
            report_resource = next(item for item in resources if item.path == main["path"])
        elif body.report.type == "platform_vulnerability":
            report_resource, source_resources = save_platform_vulnerability_source(
                conn, project_id, source_snapshot, source_artifacts, created_by=principal.subject_id,
            )
            resources.extend(source_resources)
        else:
            report_resource = _insert_prepared_project_resources(
                conn, project_id, [("note", "_verification/report.md" if body.report.type == "markdown" else "_verification/report.txt", body.report.content.encode("utf-8"))],
                created_by=principal.subject_id,
            )[0]
            resources.append(report_resource)
        intent_id = next_intent_id(conn, project_id)
        now = utcnow()
        conn.execute("INSERT INTO intents(id, project_id, description, creator, created_at) VALUES (?, ?, ?, ?, ?)",
                     (intent_id, project_id, "定向验证本次提交的漏洞资料，历史输入不代表本轮观察，结果仅交付至关联复测请求。", principal.subject_id, now))
        request_id = "vr_" + uuid4().hex
        conn.execute(
            "INSERT INTO verification_requests(id, schema_version, tenant_id, caller_type, caller_id, client_request_id, request_digest, "
            "external_id, mode, project_id, intent_id, report_type, report_resource_id, previous_request_id, worker_template_id, "
            "source_project_id, source_vulnerability_id, created_at, updated_at) "
            "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (request_id, principal.tenant_id, principal.kind, principal.subject_id, str(body.request_id), payload_digest,
             body.external_id, body.mode, project_id, intent_id, body.report.type, report_resource.id, body.previous_request_id, None,
             platform_source[0] if platform_source else None, platform_source[1] if platform_source else None, now, now),
        )
        # DOCX 原件先归档；解析错误是平台失败，不冒充环境变化或阴性结论。
        parse_error = None
        for resource in resources:
            if platform_source and resource.path.startswith(SOURCE_ARTIFACT_ROOT + "/"):
                continue
            if Path(resource.path).suffix.casefold() != ".docx":
                continue
            from .verification_documents import extract_docx
            try:
                derived = extract_docx(_project_resource_disk_path(project_id, resource.id).read_bytes(), resource.path)
            except ValueError:
                parse_error = {"code": "document_parse_failed", "message": "DOCX 资料无法解析；原件已保留，请检查文件格式或改用 MD／文本。"}
                break
            created = _insert_prepared_project_resources(conn, project_id,
                [(infer_project_resource_kind(item.path), item.path, item.content) for item in derived], created_by=principal.subject_id)
            for item in created:
                conn.execute("UPDATE project_resources SET derived_from_resource_id = ? WHERE project_id = ? AND id = ?", (resource.id, project_id, item.id))
        row = _owned_request(conn, request_id, principal)
        if parse_error:
            _write_terminal(conn, row, kind="failure", digest=_digest(parse_error), error=parse_error)
        else:
            conn.execute("UPDATE projects SET status = 'active' WHERE id = ?", (project_id,))
        return _detail(conn, _owned_request(conn, request_id, principal), principal), False


def create_verification_batch(source_request_id: str | None, principal: RequestPrincipal):
    scope = caller_scope(principal)
    source_resources = []
    if source_request_id:
        with get_conn() as conn:
            source = _owned_request(conn, source_request_id, principal)
            if source["tenant_id"] != principal.tenant_id:
                raise HTTPException(404, "历史复测请求不属于当前租户")
            if source["source_project_id"] is not None:
                require_platform_source_user(principal)
                _input_report(conn, source)
            source_project_id = source["project_id"]
            source_resources = conn.execute("SELECT * FROM project_resources WHERE project_id = ? AND derived_from_resource_id IS NULL ORDER BY path, id", (source_project_id,)).fetchall()
    batch = create_project_resource_batch(**scope)
    if source_resources:
        with schedule_transaction() as conn:
            _lock_owned_open_batch(conn, batch.id, **scope)
            _check_batch_additions(conn, batch.id, [(item["path"], item["size_bytes"]) for item in source_resources])
            for item in source_resources:
                member_id = "prm_" + uuid4().hex
                target = _batch_member_disk_path(batch.id, member_id, create_parent=True)
                copy_verified_resource_content(conn, _project_resource_disk_path(source_project_id, item["id"]), target,
                                               path=item["path"], size_bytes=item["size_bytes"], digest_sha256=item["sha256"])
                conn.execute("INSERT INTO project_resource_batch_members(id, batch_id, kind, path, size_bytes, sha256, source, created_at, source_resource_id) VALUES (?, ?, ?, ?, ?, ?, 'file', ?, ?)",
                             (member_id, batch.id, item["kind"], item["path"], item["size_bytes"], item["sha256"], utcnow(), item["id"]))
    return get_project_resource_batch(batch.id, **scope)


def verification_input_in_transaction(conn, row) -> dict:
    project = get_project_or_404(conn, row["project_id"])
    origin = conn.execute("SELECT description FROM facts WHERE project_id = ? AND id = 'origin'", (row["project_id"],)).fetchone()
    return {"id": row["id"], "project_id": row["project_id"], "intent_id": row["intent_id"], "mode": row["mode"],
            "title": project["title"], "origin": origin["description"],
            "report": _input_report(conn, row), "input_source": _input_source(row),
            "materials": _materials(conn, row["project_id"]), "previous_request_id": row["previous_request_id"],
            "execution_generation": project["execution_generation"], "cancel_requested": row["cancel_requested_at"] is not None,
            "execution_deadline": project["explore_deadline"], "authorization_expires_at": project["authorization_expires_at"]}


def verification_input(request_id: str, principal: RequestPrincipal) -> dict:
    with get_conn() as conn:
        return verification_input_in_transaction(conn, _owned_request(conn, request_id, principal))


def _execution_open(conn, row):
    project = get_project_or_404(conn, row["project_id"])
    if row["terminal_kind"] is not None or row["cancel_requested_at"] is not None or project["status"] != "active":
        raise HTTPException(409, "verification_execution_closed")
    require_project_authorization_current(conn, project)
    deadline = parse_utc_timestamp(project["explore_deadline"])
    if deadline and deadline <= parse_utc_timestamp(utcnow()):
        raise HTTPException(409, "verification_execution_deadline_expired")
    return project


def claim_verification_request(request_id: str, body: VerificationClaimRequest, principal: RequestPrincipal) -> dict:
    owner = authenticated_execution_owner(principal, body.task_run.runtime_context.get("dispatcher_id"))
    execution_binding = None
    with schedule_transaction() as conn:
        row = _owned_request(conn, request_id, principal)
        # 配置保存、首次认领统一租户→项目锁序，候选读取不决定实际模型。
        conn.execute("UPDATE tenants SET id = id WHERE id = ?", (row["tenant_id"],))
        row = _locked_request(conn, request_id, principal)
        project = _execution_open(conn, row)
        if body.task_run.task_type != "external_verify" or body.task_run.worker_type != "pi" or body.task_run.status not in {"queued", "running"}:
            raise HTTPException(422, "verification_claim_task_invalid")
        if body.task_run.expected_execution_generation != project["execution_generation"]:
            raise HTTPException(409, "execution_generation_mismatch")
        if body.task_run.intent_id not in {None, row["intent_id"]}:
            raise HTTPException(409, "verification_intent_mismatch")
        replayed = row["current_task_run_id"] is not None
        if replayed:
            run = conn.execute("SELECT * FROM task_runs WHERE project_id = ? AND id = ?", (row["project_id"], row["current_task_run_id"])).fetchone()
            if raw_task_run_runtime_context(run).get("dispatcher_id") != owner or run["worker_name"] != body.task_run.worker_name:
                raise HTTPException(409, "verification_execution_already_claimed")
            task_run = task_run_to_model(run)
            token = row["claim_token"]
        else:
            from .tenant_workers import refresh_verification_worker_execution_binding
            execution_binding = refresh_verification_worker_execution_binding(conn, row["project_id"])
            if execution_binding.worker_id != body.task_run.worker_name:
                raise HTTPException(409, "verification_worker_binding_mismatch")
            token = uuid4().hex
            boot_id, now_ns = lease_clock()
            conn.execute("UPDATE intents SET worker = ?, last_heartbeat_at = ?, heartbeat_boot_id = ?, heartbeat_boottime_ns = ? WHERE project_id = ? AND id = ?",
                         (execution_binding.worker_id, utcnow(), boot_id, now_ns, row["project_id"], row["intent_id"]))
            runtime_context = {**body.task_run.runtime_context,
                               "execution_binding": execution_binding.execution_provenance(),
                               "model": execution_binding.model_name}
            task_run = insert_task_run_record(conn, row["project_id"], body.task_run.model_copy(update={
                "intent_id": row["intent_id"], "runtime_context": runtime_context,
            }), principal=principal)
            conn.execute("UPDATE verification_requests SET current_task_run_id = ?, claim_token = ?, updated_at = ? WHERE id = ?", (task_run.id, token, utcnow(), request_id))
        input_data = verification_input_in_transaction(conn, row)
    # 完整配置仅通过受控 machine 回执到执行进程；重放不是再次启动许可。
    return {"request_id": request_id, "project_id": row["project_id"], "intent_id": row["intent_id"],
            "claim_token": token, "execution_generation": project["execution_generation"],
            "replayed": replayed,
            "execution_binding": execution_binding.model_dump(mode="json") if execution_binding else None,
            "task_run": task_run.model_dump(mode="json"), "input": input_data}


def _validate_binding(conn, row, body: VerificationExecutionBinding, principal):
    owner = authenticated_execution_owner(principal)
    if body.project_id != row["project_id"] or body.task_run_id != row["current_task_run_id"]:
        raise HTTPException(409, "verification_execution_binding_mismatch")
    if not row["claim_token"] or not hmac.compare_digest(body.claim_token, row["claim_token"]):
        raise HTTPException(409, "verification_execution_binding_mismatch")
    project = get_project_or_404(conn, row["project_id"])
    run = conn.execute("SELECT * FROM task_runs WHERE project_id = ? AND id = ?", (row["project_id"], body.task_run_id)).fetchone()
    if (run is None or run["task_type"] != "external_verify" or run["intent_id"] != row["intent_id"]
            or body.execution_generation != run["execution_generation"] or body.execution_generation != project["execution_generation"]):
        raise HTTPException(409, "execution_generation_mismatch")
    if raw_task_run_runtime_context(run).get("dispatcher_id") != owner:
        raise HTTPException(403, "execution_owner_mismatch")
    return run


def heartbeat_verification_request(request_id: str, body: VerificationExecutionBinding, principal) -> dict:
    with schedule_transaction() as conn:
        row = _locked_request(conn, request_id, principal)
        _validate_binding(conn, row, body, principal)
        _execution_open(conn, row)
        boot_id, now_ns = lease_clock()
        conn.execute("UPDATE intents SET last_heartbeat_at = ?, heartbeat_boot_id = ?, heartbeat_boottime_ns = ? WHERE project_id = ? AND id = ?",
                     (utcnow(), boot_id, now_ns, row["project_id"], row["intent_id"]))
    return {"ok": True}


def _terminal_replay(row, kind: str, digest: str) -> bool:
    if row["terminal_kind"] is None:
        return False
    if row["terminal_kind"] == kind and row["terminal_digest"] == digest:
        return True
    raise HTTPException(409, "verification_terminal_conflict")


def _validate_public_text(value: dict) -> None:
    text = json.dumps(value, ensure_ascii=False)
    if PUBLIC_SECRET_PATTERN.search(text) or any(marker in text for marker in ("/root/", "/home/", "/var/lib/cairn", ".pi/agent", "CAIRN_MACHINE_TOKEN", "credential_version_id", '"runtime_context"')):
        # 在输出接受边界拒绝凭据，不改写、脱敏或摘要权威结果。
        raise HTTPException(422, "verification_sensitive_result_rejected")


def submit_verification_result(request_id: str, body: VerificationResultSubmission, principal) -> dict:
    result = body.result.model_dump(mode="json")
    digest = _digest({"task_run_id": body.task_run_id, "result": result})
    with schedule_transaction() as conn:
        row = _locked_request(conn, request_id, principal)
        run = _validate_binding(conn, row, body, principal)
        if _terminal_replay(row, "result", digest):
            return _detail(conn, row, principal)
        if row["cancel_requested_at"] or run["pause_requested"] or run["status"] in {"failed", "timed_out", "cancelled"}:
            raise HTTPException(409, "verification_execution_closed")
        _validate_public_text(result)
        result_bytes = json.dumps(result, ensure_ascii=False).encode("utf-8")
        if any(secret in result_bytes for secret in _platform_secrets(conn, row["project_id"])):
            raise HTTPException(422, "verification_sensitive_result_rejected")
        seen = set()
        for evidence in body.result.evidence_refs:
            if evidence.artifact_id in seen:
                continue
            seen.add(evidence.artifact_id)
            artifact = conn.execute("SELECT * FROM artifacts WHERE project_id = ? AND id = ? AND task_run_id = ?",
                                    (row["project_id"], evidence.artifact_id, body.task_run_id)).fetchone()
            if artifact is None or not _artifact_deliverable(conn, artifact):
                raise HTTPException(422, "verification_evidence_not_current_or_deliverable")
            validate_resource_content(_artifact_disk_path_from_row(artifact), path=artifact["id"], size_bytes=artifact["size"], digest_sha256=artifact["sha256"])
        _write_terminal(conn, row, kind="result", digest=digest, result=result)
        return _detail(conn, _owned_request(conn, request_id, principal), principal)


def submit_verification_failure(request_id: str, body: VerificationFailureSubmission, principal) -> dict:
    error = body.error.model_dump(mode="json")
    _validate_public_text(error)
    digest = _digest({"task_run_id": body.task_run_id, "error": error})
    with schedule_transaction() as conn:
        row = _locked_request(conn, request_id, principal)
        _validate_binding(conn, row, body, principal)
        if any(secret in json.dumps(error, ensure_ascii=False).encode("utf-8") for secret in _platform_secrets(conn, row["project_id"])):
            raise HTTPException(422, "verification_sensitive_result_rejected")
        if not _terminal_replay(row, "failure", digest):
            if row["cancel_requested_at"]:
                raise HTTPException(409, "verification_cancellation_pending")
            _write_terminal(conn, row, kind="failure", digest=digest, error=error)
        return _detail(conn, _owned_request(conn, request_id, principal), principal)


def cancel_verification_request(request_id: str, principal) -> dict:
    with schedule_transaction() as conn:
        row = _locked_request(conn, request_id, principal)
        if row["terminal_kind"] is not None:
            return _detail(conn, row, principal)
        if row["cancel_requested_at"] is None:
            now = utcnow()
            conn.execute("UPDATE verification_requests SET cancel_requested_at = ?, updated_at = ? WHERE id = ?", (now, now, request_id))
            conn.execute("UPDATE projects SET status = 'paused' WHERE id = ?", (row["project_id"],))
            conn.execute("UPDATE task_runs SET pause_requested = 1, pause_interrupted = CASE WHEN status IN ('queued', 'running') THEN 1 ELSE pause_interrupted END WHERE project_id = ?", (row["project_id"],))
        refreshed = _owned_request(conn, request_id, principal)
        runs = conn.execute("SELECT * FROM task_runs WHERE project_id = ?", (row["project_id"],)).fetchall()
        if all(_run_ended(run) for run in runs):
            _write_terminal(conn, refreshed, kind="cancelled", digest=_digest({"cancelled": True}))
        return _detail(conn, _owned_request(conn, request_id, principal), principal)


def acknowledge_verification_cancel(request_id: str, body: VerificationExecutionBinding, principal) -> dict:
    with schedule_transaction() as conn:
        row = _locked_request(conn, request_id, principal)
        run = _validate_binding(conn, row, body, principal)
        if row["terminal_kind"] == "cancelled":
            return _detail(conn, row, principal)
        if row["terminal_kind"] is not None:
            raise HTTPException(409, "verification_terminal_conflict")
        if not row["cancel_requested_at"] or not _run_ended(run):
            raise HTTPException(409, "verification_cancellation_not_confirmed")
        _write_terminal(conn, row, kind="cancelled", digest=_digest({"cancelled": True}))
        return _detail(conn, _owned_request(conn, request_id, principal), principal)


def expire_pending_verification_requests(conn) -> None:
    """尚未认领且已失去授权或执行窗口的请求无需创建容器即可失败收口。"""
    rows = conn.execute("SELECT id, project_id FROM verification_requests WHERE terminal_kind IS NULL AND current_task_run_id IS NULL AND cancel_requested_at IS NULL").fetchall()
    now = parse_utc_timestamp(utcnow())
    for candidate in rows:
        conn.execute("UPDATE projects SET id = id WHERE id = ?", (candidate["project_id"],))
        row = conn.execute("SELECT * FROM verification_requests WHERE id = ?", (candidate["id"],)).fetchone()
        if row["terminal_kind"] or row["current_task_run_id"] or row["cancel_requested_at"]:
            continue
        project = get_project_or_404(conn, row["project_id"])
        expiry = parse_utc_timestamp(project["authorization_expires_at"])
        deadline = parse_utc_timestamp(project["explore_deadline"])
        code = "authorization_expired" if expiry and expiry <= now else "execution_deadline_expired" if deadline and deadline <= now else None
        if code:
            error = {"code": code, "message": "本轮在认领前已超出授权或执行期限，未执行目标操作。"}
            _write_terminal(conn, row, kind="failure", digest=_digest(error), error=error)


def finalize_verification_run(conn, project_id: str, run) -> None:
    """终止证明补报也会收口；API 短暂失败不能让未交付请求永久假运行。"""
    row = conn.execute("SELECT * FROM verification_requests WHERE project_id = ? AND current_task_run_id = ?", (project_id, run["id"])).fetchone()
    if row is None or row["terminal_kind"] is not None or not _run_ended(run):
        return
    if run["execution_generation"] != get_project_or_404(conn, project_id)["execution_generation"]:
        return
    if row["cancel_requested_at"]:
        _write_terminal(conn, row, kind="cancelled", digest=_digest({"cancelled": True}))
        return
    error = {"code": "delivery_failed" if run["status"] == "succeeded" else "execution_failed",
             "message": "执行已结束但没有接受合法的复测结果。" if run["status"] == "succeeded" else "本次复测执行失败，已保留服务端原件和已上传证据。"}
    _write_terminal(conn, row, kind="failure", digest=_digest({"task_run_id": run["id"], "error": error}), error=error)


def verification_report(request_id: str, principal) -> str:
    detail = get_verification_request(request_id, principal)
    if detail["result"] is None:
        raise HTTPException(409, "复测报告尚未就绪：本轮没有已接受的结果")
    result = detail["result"]
    labels = {"confirmed": "已复现", "not_reproduced": "本轮未复现", "environment_changed": "环境或条件变化", "insufficient_evidence": "证据不足"}
    lines = [f"# {detail['title']}", "", f"- 请求：{detail['id']}", f"- 模式：{detail['mode']}",
             f"- 结论：{labels[result['outcome']]}", "", "## 结论", "", result["summary"], "", "## 实际测试范围", "", result["tested_scope"], "", "## 前提、操作与观察", "", result["notes"], "", "## 限制与未测试事项", ""]
    lines.extend(f"- {item}" for item in result["limitations"])
    lines.extend(["", "## 本轮证据", ""])
    lines.extend(f"- {item['artifact_id']} {item.get('pointer', '')}" for item in result["evidence_refs"])
    lines.extend(["", "本轮未复现不等于证明安全或确认所有相关路径已修复。", ""])
    return "\n".join(lines)


def verification_material(request_id: str, resource_id: str, principal):
    with get_conn() as conn:
        row = _owned_request(conn, request_id, principal)
        if conn.execute("SELECT 1 FROM project_resources WHERE project_id = ? AND id = ?", (row["project_id"], resource_id)).fetchone() is None:
            raise HTTPException(404, "复测原件不存在")
        project_id = row["project_id"]
    return read_project_resource_content(project_id, resource_id)


def verification_artifact(request_id: str, artifact_id: str, principal):
    with get_conn() as conn:
        row = _owned_request(conn, request_id, principal)
        artifact = next((item for item in _public_artifacts(conn, row) if item["id"] == artifact_id), None)
        if artifact is None:
            raise HTTPException(404, "复测证据不存在或不允许交付")
        validate_resource_content(_artifact_disk_path_from_row(artifact), path=artifact["id"],
                                  size_bytes=artifact["size"], digest_sha256=artifact["sha256"])
        project_id = row["project_id"]
    return artifact_content_file(project_id, artifact_id)
