"""HTTP 采集入站、双层幂等及授权路由使用的只读投影。"""

from __future__ import annotations

import base64
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import AsyncIterable

from starlette.concurrency import run_in_threadpool

from fastapi import HTTPException
from pydantic import ValidationError

from cairn.server.auth import RequestPrincipal
from cairn.server.db import artifact_root, get_conn
from cairn.server.models import CreateRawArtifactRequest
from cairn.server.traffic_schema import traffic_request_address
from cairn.server.schemas.traffic import TRAFFIC_CHUNK_BYTES, TrafficBodySeal, TrafficRecord, TrafficScope, TrafficScopeStatus
from .audit_artifacts import StagedRawArtifact, artifact_content_file, publish_raw_artifact
from .ids import get_project_or_404
from .time_utils import utcnow

SIDES = ("request", "response")
PREVIEW_BYTES = 64 * 1024


def _lock_project(conn, project_id: str):
    # 所有游标分配都持有同一项目行锁直至提交，避免 sequence 的提交乱序漏更新。
    conn.execute("SELECT id FROM projects WHERE id = ? FOR UPDATE", (project_id,))
    return get_project_or_404(conn, project_id)


def _next_cursor(conn, project_id: str) -> int:
    row = conn.execute(
        "INSERT INTO scoped_counters(project_id, kind, value) VALUES (?, 'http_flow', 1) "
        "ON CONFLICT (project_id, kind) DO UPDATE SET value = scoped_counters.value + 1 RETURNING value",
        (project_id,),
    ).fetchone()
    return row["value"]


def _scope(conn, project_id: str, scope_id: str, principal: RequestPrincipal):
    row = conn.execute(
        "SELECT * FROM http_capture_scopes WHERE project_id = ? AND id = ?", (project_id, scope_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "traffic_scope_not_found")
    if row["dispatcher_id"] != principal.subject_id:
        raise HTTPException(403, "traffic_dispatcher_mismatch")
    return row


def _binding(row) -> dict:
    return {key: row[key] for key in ("id", "project_id", "task_run_id", "generation", "container_id")} | {
        "test_egress": json.loads(row["test_egress_json"]),
    }


def _validate_run(conn, project, binding: dict, dispatcher_id: str) -> None:
    run = conn.execute(
        "SELECT execution_generation, runtime_context_json FROM task_runs WHERE project_id = ? AND id = ?",
        (binding["project_id"], binding["task_run_id"]),
    ).fetchone()
    if run is None:
        raise HTTPException(404, "traffic_task_run_not_found")
    if json.loads(run["runtime_context_json"]).get("dispatcher_id") != dispatcher_id:
        raise HTTPException(403, "traffic_dispatcher_mismatch")
    if run["execution_generation"] != binding["generation"]:
        raise HTTPException(409, "traffic_generation_mismatch")
    # 历史执行已结束或项目已进入下一代，不阻止恢复上传既有材料。
    if binding["generation"] > project["execution_generation"]:
        raise HTTPException(409, "traffic_generation_future")


def register_traffic_scope(project_id: str, body: TrafficScope, principal: RequestPrincipal) -> dict:
    binding = body.model_dump()
    if body.project_id != project_id:
        raise HTTPException(409, "traffic_project_mismatch")
    with get_conn() as conn:
        project = _lock_project(conn, project_id)
        _validate_run(conn, project, binding, principal.subject_id)
        row = conn.execute(
            "SELECT * FROM http_capture_scopes WHERE project_id = ? AND id = ?", (project_id, body.id),
        ).fetchone()
        if row is not None:
            if row["dispatcher_id"] != principal.subject_id:
                raise HTTPException(403, "traffic_dispatcher_mismatch")
            if _binding(row) != binding:
                raise HTTPException(409, "traffic_scope_conflict")
            return _binding(row)
        conn.execute(
            "INSERT INTO http_capture_scopes "
            "(id, project_id, task_run_id, generation, container_id, dispatcher_id, created_at, test_egress_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (body.id, project_id, body.task_run_id, body.generation, body.container_id, principal.subject_id, utcnow(), body.test_egress.model_dump_json()),
        )
        return binding


def _current_scope_status(status: dict | None, generation: int, runtime_context: str) -> dict | None:
    lifecycle = json.loads(runtime_context).get("execution_lifecycle", {})
    if status is not None and isinstance(lifecycle, dict) and lifecycle.get("generation") == generation and lifecycle.get("termination_confirmed") is True:
        # 只投影已接受的执行证明；collector 停止和正文排空仍由各自事实决定。
        return {**status, "control_stopped": True}
    return status


def update_traffic_scope_status(project_id: str, scope_id: str, body: TrafficScopeStatus, principal: RequestPrincipal) -> dict:
    with get_conn() as conn:
        project = _lock_project(conn, project_id)
        scope = _scope(conn, project_id, scope_id, principal)
        _validate_run(conn, project, _binding(scope), principal.subject_id)
        now = utcnow()
        status = body.model_dump()
        conn.execute(
            "UPDATE http_capture_scopes SET status_json = ?, status_updated_at = ? WHERE project_id = ? AND id = ?",
            (json.dumps(status, separators=(",", ":")), now, project_id, scope_id),
        )
        run = conn.execute("SELECT runtime_context_json FROM task_runs WHERE project_id = ? AND id = ?",
                           (project_id, scope["task_run_id"])).fetchone()
        return {**_binding(scope), "dispatcher_id": scope["dispatcher_id"],
                "status": _current_scope_status(status, scope["generation"], run["runtime_context_json"]),
                "status_updated_at": now}


def list_traffic_scope_status(
    project_id: str, *, cursor: str = "", limit: int = 100, task_run_id: str | None = None,
) -> dict:
    filters = ["s.project_id = ?", "s.id > ?"]
    params = [project_id, cursor]
    if task_run_id is not None:
        filters.append("s.task_run_id = ?")
        params.append(task_run_id)
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        rows = conn.execute(
            "SELECT s.*, r.runtime_context_json FROM http_capture_scopes s JOIN task_runs r "
            "ON r.project_id = s.project_id AND r.id = s.task_run_id "
            f"WHERE {' AND '.join(filters)} ORDER BY s.id LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
        return {
            "items": [{**_binding(row), "dispatcher_id": row["dispatcher_id"],
                       "status": _current_scope_status(json.loads(row["status_json"]) if row["status_json"] else None,
                                                       row["generation"], row["runtime_context_json"]),
                       "status_updated_at": row["status_updated_at"]} for row in rows[:limit]],
            "has_more": len(rows) > limit,
            "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
        }


def _flow(conn, project_id: str, flow_id: str):
    row = conn.execute(
        "SELECT * FROM http_flows WHERE project_id = ? AND id = ?", (project_id, flow_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "traffic_flow_not_found")
    return row


def _body_rows(conn, project_id: str, flow_id: str) -> list:
    return conn.execute(
        "SELECT * FROM http_flow_bodies WHERE project_id = ? AND flow_id = ?", (project_id, flow_id),
    ).fetchall()


def _stream_rows(conn, project_id: str, flow_id: str) -> list:
    return conn.execute(
        "SELECT * FROM http_flow_body_streams WHERE project_id = ? AND flow_id = ?", (project_id, flow_id),
    ).fetchall()


def _sync(record: dict, bodies: list, streams: list | None = None) -> dict:
    available = {(row["side"], row["revision"]): row for row in bodies}
    accepted = {row["side"]: row["accepted_bytes"] for row in streams or ()}
    sides = {}
    pending = []
    for side in SIDES:
        message = record[side]
        if message is None:
            sides[side] = {"status": "absent", "artifact_id": None}
            continue
        receipt = available.get((side, message["body_revision"]))
        if receipt is not None:
            sides[side] = {
                "status": "available", "artifact_id": receipt["artifact_id"],
                "revision": receipt["revision"], "size": receipt["size"], "sha256": receipt["sha256"],
                "captured_bytes": message["body_bytes"], "accepted_bytes": receipt["size"],
                "sealed_bytes": receipt["size"], "sealed": True,
            }
        else:
            sides[side] = {
                "status": "pending" if message["body_sealed"] else "receiving", "artifact_id": None,
                "revision": message["body_revision"], "size": accepted.get(side, 0), "sha256": None,
                "captured_bytes": message["body_bytes"], "accepted_bytes": accepted.get(side, 0),
                "sealed_bytes": None, "sealed": False,
            }
            if message["body_sealed"]:
                pending.append(side)
    return {"metadata_revision": record["revision"], "pending_sides": pending, "bodies": sides}


def _view(conn, row, bodies: list | None = None, streams: list | None = None) -> dict:
    record = json.loads(row["record_json"])
    if bodies is None:
        bodies = _body_rows(conn, row["project_id"], row["id"])
    if streams is None:
        streams = _stream_rows(conn, row["project_id"], row["id"])
    return {
        **record,
        "source_state": record["state"], "state": row["state"],
        "project_id": row["project_id"], "capture_scope_id": row["capture_scope_id"],
        "record_sha256": row["record_sha256"],
        "created_cursor": row["created_cursor"], "updated_cursor": row["updated_cursor"],
        "received_at": row["received_at"], "updated_at": row["updated_at"],
        "sync": _sync(record, bodies, streams),
    }


def _published_state(record: dict, bodies: list) -> str:
    return "receiving" if _sync(record, bodies)["pending_sides"] else record["state"]


def _validate_progress(previous: dict, record: dict) -> None:
    if previous["state"] != "receiving":
        raise HTTPException(409, "traffic_flow_terminal")
    for key in ("scope", "representation", "collector_version", "observation_point", "connection_id", "started_at", "original_destination"):
        if previous[key] != record[key]:
            raise HTTPException(409, "traffic_flow_identity_conflict")
    recovery = record.get("recovery")
    if recovery and recovery["last_capture_revision"] < previous["revision"]:
        raise HTTPException(409, "traffic_recovery_revision_conflict")
    for side in SIDES:
        before, after = previous[side], record[side]
        if before is None:
            continue
        if after is None or after["body_bytes"] < before["body_bytes"]:
            raise HTTPException(409, "traffic_body_progress_regressed")
        if before["body_sealed"] and before != after:
            raise HTTPException(409, "traffic_sealed_body_conflict")
        if recovery and recovery["last_capture_revision"] == previous["revision"] and not before["body_sealed"] and after["body_complete"]:
            raise HTTPException(409, "traffic_recovery_complete_conflict")
        # 正文接收进度可增长，但已观察到的协议字段和头不是可编辑草稿。
        progress_keys = {"body_bytes", "body_complete", "body_sealed", "body_revision"}
        if {key: value for key, value in before.items() if key not in progress_keys} != {
            key: value for key, value in after.items() if key not in progress_keys
        }:
            raise HTTPException(409, "traffic_message_conflict")


def accept_traffic_record(
    project_id: str, scope_id: str, flow_id: str, record: dict, *, principal: RequestPrincipal,
) -> dict:
    with get_conn() as conn:
        project = _lock_project(conn, project_id)
        return _accept_record(conn, project, project_id, scope_id, flow_id, record, principal=principal)


def _accept_record(
    conn, project, project_id: str, scope_id: str, flow_id: str, record: dict, *, principal: RequestPrincipal,
) -> dict:
    try:
        parsed = TrafficRecord.model_validate(record)
        serialized = json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValidationError, ValueError):
        # Pydantic 错误中的 input 可能含认证头；入站错误只返回稳定诊断码。
        raise HTTPException(422, "traffic_record_invalid") from None
    if parsed.id != flow_id or parsed.scope.id != scope_id or parsed.scope.project_id != project_id:
        raise HTTPException(409, "traffic_record_binding_mismatch")
    digest = sha256(serialized.encode("utf-8")).hexdigest()
    scope = _scope(conn, project_id, scope_id, principal)
    if parsed.scope.model_dump() != _binding(scope):
        raise HTTPException(409, "traffic_scope_conflict")
    _validate_run(conn, project, _binding(scope), principal.subject_id)
    row = conn.execute(
        "SELECT * FROM http_flows WHERE project_id = ? AND id = ?", (project_id, flow_id),
    ).fetchone()
    if row is not None:
        if row["capture_scope_id"] != scope_id:
            raise HTTPException(409, "traffic_flow_scope_conflict")
        receipt = conn.execute(
            "SELECT record_sha256 FROM http_flow_receipts WHERE project_id = ? AND flow_id = ? AND revision = ?",
            (project_id, flow_id, parsed.revision),
        ).fetchone()
        if receipt is not None:
            if receipt["record_sha256"] != digest:
                raise HTTPException(409, "traffic_revision_conflict")
            return _view(conn, row)
        if parsed.revision > row["revision"]:
            _validate_progress(json.loads(row["record_json"]), record)
    if row is None or parsed.revision > row["revision"]:
        cursor = _next_cursor(conn, project_id)
        now = utcnow()
        bodies = [] if row is None else _body_rows(conn, project_id, flow_id)
        state = _published_state(record, bodies)
        status = parsed.response.status_code if parsed.response else None
        address = traffic_request_address(record["request"])
        if row is None:
            conn.execute(
                "INSERT INTO http_flows "
                "(id, project_id, capture_scope_id, revision, record_json, record_sha256, state, "
                "method, host, status_code, created_cursor, updated_cursor, received_at, updated_at, request_url, request_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (flow_id, project_id, scope_id, parsed.revision, serialized, digest, state,
                 parsed.request.method, address["host"], status, cursor, cursor, now, now,
                 address["request_url"], address["request_path"]),
            )
        else:
            conn.execute(
                "UPDATE http_flows SET revision = ?, record_json = ?, record_sha256 = ?, state = ?, "
                "method = ?, host = ?, status_code = ?, updated_cursor = ?, updated_at = ?, "
                "request_url = ?, request_path = ? WHERE project_id = ? AND id = ?",
                (parsed.revision, serialized, digest, state, parsed.request.method, address["host"],
                 status, cursor, now, address["request_url"], address["request_path"], project_id, flow_id),
            )
    # 即使首次遇到的是旧版本，也持久记录其 hash，之后的同版本冲突不可绕过。
    conn.execute(
        "INSERT INTO http_flow_receipts(project_id, flow_id, revision, record_sha256) VALUES (?, ?, ?, ?)",
        (project_id, flow_id, parsed.revision, digest),
    )
    return _view(conn, _flow(conn, project_id, flow_id))


def _body_target(conn, project_id: str, scope_id: str, flow_id: str, side: str, principal: RequestPrincipal):
    if side not in SIDES:
        raise HTTPException(422, "traffic_side_invalid")
    scope = _scope(conn, project_id, scope_id, principal)
    row = _flow(conn, project_id, flow_id)
    if row["capture_scope_id"] != scope_id:
        raise HTTPException(409, "traffic_flow_scope_conflict")
    message = json.loads(row["record_json"])[side]
    if message is None:
        raise HTTPException(404, "traffic_side_absent")
    return scope, row, message


def _staging_path(project_id: str, flow_id: str, side: str) -> Path:
    # 不把采集器传入的 body_file 当作服务器路径；staging 随项目 Artifact 根一起删除。
    return artifact_root() / "projects" / project_id / ".traffic" / f"{sha256(flow_id.encode()).hexdigest()}-{side}.body"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_prefix(path: Path, accepted: int):
    # DB 只确认 fsync 后的前缀；崩溃遗留的未提交尾部在下一次写入前丢弃。
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        _fsync_directory(directory.parent)
    if not path.exists() and accepted:
        raise HTTPException(503, "traffic_durable_prefix_missing")
    output = path.open("r+b" if path.exists() else "w+b")
    if os.fstat(output.fileno()).st_size < accepted:
        output.close()
        raise HTTPException(503, "traffic_durable_prefix_missing")
    output.truncate(accepted)
    output.seek(accepted)
    return output


def _accepted_bytes(conn, project_id: str, flow_id: str, side: str) -> int:
    row = conn.execute(
        "SELECT accepted_bytes FROM http_flow_body_streams WHERE project_id = ? AND flow_id = ? AND side = ?",
        (project_id, flow_id, side),
    ).fetchone()
    return row["accepted_bytes"] if row else 0


def _touch_body(conn, project_id: str, flow_id: str, record: dict) -> None:
    conn.execute(
        "UPDATE http_flows SET state = ?, updated_cursor = ?, updated_at = ? WHERE project_id = ? AND id = ?",
        (_published_state(record, _body_rows(conn, project_id, flow_id)),
         _next_cursor(conn, project_id), utcnow(), project_id, flow_id),
    )


def _accept_body_chunk(project_id, scope_id, flow_id, side, offset, size, digest, raw, principal) -> dict:
    with get_conn() as conn:
        project = _lock_project(conn, project_id)
        return _accept_chunk(conn, project, project_id, scope_id, flow_id, side, offset, size, digest, raw, principal)


def _accept_chunk(conn, project, project_id, scope_id, flow_id, side, offset, size, digest, raw, principal) -> dict:
    scope, row, message = _body_target(conn, project_id, scope_id, flow_id, side, principal)
    _validate_run(conn, project, _binding(scope), principal.subject_id)
    receipt = conn.execute(
        "SELECT size, sha256 FROM http_flow_body_chunks "
        "WHERE project_id = ? AND flow_id = ? AND side = ? AND byte_offset = ?",
        (project_id, flow_id, side, offset),
    ).fetchone()
    result = {"project_id": project_id, "flow_id": flow_id, "side": side,
              "offset": offset, "size": size, "sha256": digest, "next_offset": offset + size}
    if receipt is not None:
        if receipt["size"] != size or receipt["sha256"] != digest:
            raise HTTPException(409, "traffic_chunk_conflict")
        return result
    if any(body["side"] == side for body in _body_rows(conn, project_id, flow_id)):
        raise HTTPException(409, "traffic_body_immutable")
    accepted = _accepted_bytes(conn, project_id, flow_id, side)
    if offset != accepted:
        raise HTTPException(409, "traffic_chunk_offset_conflict")
    if offset + size > message["body_bytes"]:
        raise HTTPException(409, "traffic_chunk_beyond_capture")
    path = _staging_path(project_id, flow_id, side)
    with _open_prefix(path, accepted) as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    _fsync_directory(path.parent)
    # 文件与目录均持久化之后才提交接受长度和幂等回执；响应在事务提交之后发送。
    conn.execute(
        "INSERT INTO http_flow_body_streams(project_id, flow_id, side, accepted_bytes) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (project_id, flow_id, side) DO UPDATE SET accepted_bytes = EXCLUDED.accepted_bytes",
        (project_id, flow_id, side, offset + size),
    )
    conn.execute(
        "INSERT INTO http_flow_body_chunks(project_id, flow_id, side, byte_offset, size, sha256) VALUES (?, ?, ?, ?, ?, ?)",
        (project_id, flow_id, side, offset, size, digest),
    )
    _touch_body(conn, project_id, flow_id, json.loads(row["record_json"]))
    return result


async def accept_traffic_body(
    project_id: str, scope_id: str, flow_id: str, side: str,
    offset: int, size: int, digest: str, chunks: AsyncIterable[bytes], *, principal: RequestPrincipal,
) -> dict:
    if offset < 0 or not 0 < size <= TRAFFIC_CHUNK_BYTES:
        raise HTTPException(422, "traffic_chunk_range_invalid")
    # 只缓冲一个有界范围，接收网络时不持有项目锁，也不重复复制整个前缀。
    raw = bytearray()
    async for chunk in chunks:
        if len(raw) + len(chunk) > size:
            raise HTTPException(422, "traffic_chunk_size_mismatch")
        raw.extend(chunk)
    if len(raw) != size:
        raise HTTPException(422, "traffic_chunk_size_mismatch")
    if sha256(raw).hexdigest() != digest:
        raise HTTPException(422, "traffic_body_digest_mismatch")
    return await run_in_threadpool(
        _accept_body_chunk, project_id, scope_id, flow_id, side, offset, size, digest, raw, principal,
    )


def seal_traffic_body(
    project_id: str, scope_id: str, flow_id: str, side: str, body: TrafficBodySeal, *, principal: RequestPrincipal,
) -> dict:
    with get_conn() as conn:
        project = _lock_project(conn, project_id)
        return _seal_body(conn, project, project_id, scope_id, flow_id, side, body, principal=principal)


def _seal_body(
    conn, project, project_id: str, scope_id: str, flow_id: str, side: str, body: TrafficBodySeal, *, principal: RequestPrincipal,
) -> dict:
    scope, row, message = _body_target(conn, project_id, scope_id, flow_id, side, principal)
    _validate_run(conn, project, _binding(scope), principal.subject_id)
    if not message["body_sealed"]:
        raise HTTPException(409, "traffic_body_not_sealed")
    if message["body_revision"] != body.revision or message["body_bytes"] != body.size:
        raise HTTPException(409, "traffic_body_version_conflict")
    receipt = conn.execute(
        "SELECT * FROM http_flow_bodies WHERE project_id = ? AND flow_id = ? AND side = ? AND revision = ?",
        (project_id, flow_id, side, body.revision),
    ).fetchone()
    if receipt is not None:
        if receipt["sha256"] != body.sha256 or receipt["size"] != body.size:
            raise HTTPException(409, "traffic_body_conflict")
        return dict(receipt)
    accepted = _accepted_bytes(conn, project_id, flow_id, side)
    if accepted != body.size:
        raise HTTPException(409, "traffic_body_prefix_incomplete")
    path = _staging_path(project_id, flow_id, side)
    with _open_prefix(path, accepted) as source:
        source.flush()
        os.fsync(source.fileno())
        source.seek(0)
        digest = sha256()
        while chunk := source.read(TRAFFIC_CHUNK_BYTES):
            digest.update(chunk)
    _fsync_directory(path.parent)
    if digest.hexdigest() != body.sha256:
        raise HTTPException(409, "traffic_body_digest_mismatch")
    # publish 会 rename；保留同 inode 的 staging 链接，使提交前崩溃/回滚仍能恢复。
    # 封存后禁止任何写入，链接不复制正文，且与原件同属项目生命周期。
    publishing = path.with_suffix(".publish")
    publishing.unlink(missing_ok=True)
    os.link(path, publishing)
    upload = StagedRawArtifact(publishing, body.size, body.sha256)
    artifact = publish_raw_artifact(
        conn, project_id,
        CreateRawArtifactRequest(
            task_run_id=scope["task_run_id"], kind="file", category="evidence", filename=f"traffic-{side}.bin",
            metadata_json=json.dumps({"capture_scope_id": scope_id, "flow_id": flow_id,
                                      "side": side, "body_revision": body.revision}),
        ),
        upload, emit_event=False,
    )
    # publish 已 fsync 最末目录；补齐本次可能新建的 runs 等祖先目录后才确认封存。
    directory = upload.final_path.parent
    root = artifact_root()
    while directory != root.parent:
        _fsync_directory(directory)
        directory = directory.parent
    conn.execute(
        "INSERT INTO http_flow_bodies(project_id, flow_id, side, revision, artifact_id, size, sha256) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (project_id, flow_id, side, body.revision, artifact.id, body.size, body.sha256),
    )
    _touch_body(conn, project_id, flow_id, json.loads(row["record_json"]))
    return {"project_id": project_id, "flow_id": flow_id, "side": side, "revision": body.revision,
            "artifact_id": artifact.id, "size": body.size, "sha256": body.sha256}


def accept_traffic_batch(project_id: str, scope_id: str, payload: dict, *, principal: RequestPrincipal) -> dict:
    """合并小包往返，每条 flow 独立提交；单条冲突不回滚其它已持久回执。"""
    from cairn.server.schemas.traffic import TRAFFIC_BATCH_BODY_BYTES, TRAFFIC_BATCH_FLOWS

    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not 0 < len(items) <= TRAFFIC_BATCH_FLOWS:
        raise HTTPException(422, "traffic_batch_invalid")
    decoded = []
    total = 0
    for item in items:
        try:
            record, bodies = item["record"], item["bodies"]
            if not isinstance(record, dict) or not isinstance(bodies, dict) or set(bodies) - set(SIDES):
                raise ValueError
            parts = {}
            for side, part in bodies.items():
                seal = TrafficBodySeal.model_validate({key: part[key] for key in ("revision", "size", "sha256")})
                raw = base64.b64decode(part["content_base64"], validate=True)
                total += len(raw)
                if len(raw) != seal.size or sha256(raw).hexdigest() != seal.sha256:
                    raise ValueError
                parts[side] = (seal, raw)
            decoded.append((record, parts))
        except (KeyError, TypeError, ValueError):
            raise HTTPException(422, "traffic_batch_invalid") from None
    if total > TRAFFIC_BATCH_BODY_BYTES:
        raise HTTPException(413, "traffic_batch_too_large")
    results = []
    for record, parts in decoded:
        flow_id = record.get("id")
        try:
            with get_conn() as conn:
                project = _lock_project(conn, project_id)
                view = _accept_record(conn, project, project_id, scope_id, flow_id, record, principal=principal)
                for side, (seal, raw) in parts.items():
                    remote = view["sync"]["bodies"][side]
                    accepted = remote.get("accepted_bytes", 0)
                    if accepted < len(raw):
                        chunk = raw[accepted:]
                        _accept_chunk(conn, project, project_id, scope_id, flow_id, side, accepted,
                                      len(chunk), sha256(chunk).hexdigest(), chunk, principal)
                    _seal_body(conn, project, project_id, scope_id, flow_id, side, seal, principal=principal)
                view = _view(conn, _flow(conn, project_id, flow_id))
                result = {"flow_id": flow_id, "status_code": 200,
                          "data": {"id": flow_id, "revision": view["revision"], "sync": view["sync"]}}
            # 仅在事务提交及 Artifact 回调成功后生成成功项。
            results.append(result)
        except HTTPException as exc:
            results.append({"flow_id": flow_id, "status_code": exc.status_code, "error_code": exc.detail})
    return {"items": results}


def read_traffic_record(project_id: str, flow_id: str) -> dict:
    with get_conn() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        get_project_or_404(conn, project_id)
        view = _view(conn, _flow(conn, project_id, flow_id))
        # 关系只从已接受的 Observation 派生，不将候选留存冒充漏洞确认。
        rows = conn.execute(
            "SELECT o.id AS observation_id, o.vulnerability_id, v.status, o.task_run_id "
            "FROM vulnerability_observations o JOIN vulnerabilities v "
            "ON v.project_id = o.project_id AND v.id = o.vulnerability_id "
            "WHERE o.project_id = ? AND EXISTS (SELECT 1 FROM "
            "jsonb_array_elements(o.evidence_items_json::jsonb) item WHERE item->>'flow_id' = ?) "
            "ORDER BY o.observed_at, o.id",
            (project_id, flow_id),
        ).fetchall()
        view["observations"] = [dict(row) for row in rows]
        view["location"] = traffic_request_address(view["request"])
        # 头部派生不读取正文，避免阅读器为显示头部下载大包或复制 v1 转义规则。
        from .flow_evidence import projection_header

        view["header_text"] = {
            side: projection_header(view[side], side).decode("utf-8") if view[side] is not None else None
            for side in SIDES
        }
        return view


def list_traffic_records(
    project_id: str, *, limit: int = 50, cursor: int = 0, after_update: int | None = None,
    scope_id: str | None = None, task_run_id: str | None = None,
    method: str | None = None, host: str | None = None, state: str | None = None,
    status_code: int | None = None,
) -> dict:
    with get_conn() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        get_project_or_404(conn, project_id)
        counter = conn.execute(
            "SELECT value FROM scoped_counters WHERE project_id = ? AND kind = 'http_flow'", (project_id,),
        ).fetchone()
        watermark = counter["value"] if counter else 0
        if after_update is not None and after_update > watermark:
            raise HTTPException(422, "traffic_update_cursor_future")
        column = "updated_cursor" if after_update is not None else "created_cursor"
        params = [project_id, after_update if after_update is not None else cursor, watermark]
        clauses = ["f.project_id = ?", f"f.{column} > ?", f"f.{column} <= ?"]
        for name, value in (("capture_scope_id", scope_id), ("method", method), ("host", host), ("state", state), ("status_code", status_code)):
            if value is not None:
                clauses.append(f"f.{name} = ?")
                params.append(value)
        if task_run_id is not None:
            clauses.append("s.task_run_id = ?")
            params.append(task_run_id)
        rows = conn.execute(
            "SELECT f.* FROM http_flows f JOIN http_capture_scopes s "
            "ON s.id = f.capture_scope_id AND s.project_id = f.project_id WHERE "
            + " AND ".join(clauses) + f" ORDER BY f.{column} LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        bodies_by_flow = {}
        streams_by_flow = {}
        if rows:
            placeholders = ",".join("?" for _ in rows)
            bodies = conn.execute(
                f"SELECT * FROM http_flow_bodies WHERE project_id = ? AND flow_id IN ({placeholders})",
                (project_id, *(row["id"] for row in rows)),
            ).fetchall()
            for body in bodies:
                bodies_by_flow.setdefault(body["flow_id"], []).append(body)
            streams = conn.execute(
                f"SELECT * FROM http_flow_body_streams WHERE project_id = ? AND flow_id IN ({placeholders})",
                (project_id, *(row["id"] for row in rows)),
            ).fetchall()
            for stream in streams:
                streams_by_flow.setdefault(stream["flow_id"], []).append(stream)
        return {
            "items": [_view(conn, row, bodies_by_flow.get(row["id"], []), streams_by_flow.get(row["id"], [])) for row in rows],
            "next_cursor": rows[-1]["created_cursor"] if rows and after_update is None else None,
            "has_more": has_more,
            "update_cursor": rows[-1]["updated_cursor"] if has_more and after_update is not None else watermark,
        }


def read_traffic_body(project_id: str, flow_id: str, side: str, *, preview: bool, offset: int = 0, limit: int = PREVIEW_BYTES):
    with get_conn() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        get_project_or_404(conn, project_id)
        row = _flow(conn, project_id, flow_id)
        record = json.loads(row["record_json"])
        message = record[side]
        if message is None:
            raise HTTPException(404, "traffic_side_absent")
        receipt = conn.execute(
            "SELECT * FROM http_flow_bodies WHERE project_id = ? AND flow_id = ? AND side = ? AND revision = ?",
            (project_id, flow_id, side, message["body_revision"]),
        ).fetchone()
        if not preview and receipt is None:
            raise HTTPException(409, "traffic_body_pending" if message["body_sealed"] else "traffic_body_receiving")
        accepted = receipt["size"] if receipt else _accepted_bytes(conn, project_id, flow_id, side)
    content = artifact_content_file(project_id, receipt["artifact_id"]) if receipt else None
    if not preview:
        return content
    # 一次 DB 快照确定上限；即使文件随后增长，也不读取未确认或快照之后的字节。
    offset = min(max(0, offset), accepted)
    length = min(max(0, limit), PREVIEW_BYTES, accepted - offset)
    raw = b""
    if length:
        path = content.disk_path if content else _staging_path(project_id, flow_id, side)
        try:
            with path.open("rb") as source:
                source.seek(offset)
                raw = source.read(length)
        except FileNotFoundError:
            raise HTTPException(503, "traffic_durable_prefix_missing") from None
        if len(raw) != length:
            raise HTTPException(503, "traffic_durable_prefix_missing")
    return {
        "artifact_id": receipt["artifact_id"] if receipt else None, "side": side,
        "revision": message["body_revision"], "body_complete": message["body_complete"],
        "sealed": receipt is not None, "source_sealed": message["body_sealed"],
        "captured_bytes": message["body_bytes"], "accepted_bytes": accepted,
        "sealed_bytes": receipt["size"] if receipt else None,
        "size": accepted, "sha256": receipt["sha256"] if receipt else None,
        "encoding": "base64", "content_base64": base64.b64encode(raw).decode("ascii"),
        "offset": offset, "bytes_read": len(raw), "next_offset": offset + len(raw),
        "has_more": offset + len(raw) < accepted,
    }
