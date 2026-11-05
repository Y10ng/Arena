"""计划的保存、管理和到期处理；业务副作用在单个计划事务内完成。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from uuid import uuid4

from fastapi import HTTPException

from cairn.project_authorization_contract import scope_source_from_descriptor
from cairn.server.auth import RequestPrincipal
from cairn.server.db import PostgresConnection, get_conn, schedule_transaction
from cairn.server.schemas.project_schedules import CreateScheduleRequest, ScheduleActionRequest, ScheduleTiming, UpdateScheduleRequest
from cairn.server.schemas.projects import CreateProjectRequest
from cairn.server.schemas.libraries import SelectableLibraryManifest
from cairn.server.schemas.test_egress import TestEgressInput, TestEgressView, egress_request_identity
from .test_egress import inherited_input, public_project_egress, save_project_egress, stored_settings

from .creation_requests import begin_creation_request, complete_creation_request
from .libraries import (
    bind_schedule_library_revision, get_bound_library_revision, get_library,
    list_library_bindings, list_library_revision_files, list_schedule_library_revisions,
    resolve_library_selections, validate_library_revision_content, safety_file_path,
)
from .project_authorization import authorization_scope_projection, project_authorization_block_reason
from .project_records import _project_library_bindings, create_project_record, project_pause_draining
from .project_resources import list_project_resource_records, validate_project_resource_records
from .schedule_resources import (
    copy_schedule_resources_to_project, list_schedule_resource_records,
    update_schedule_resources,
)
from .schedule_times import effective_boundary, end_boundary, iso, next_times, timestamp
from .scenarios import get_scenario_or_404, resolve_scenario_key, validate_scenario_values
from .targets import normalize_target_value
from .tenant_workers import resolve_general_workers

logger = logging.getLogger(__name__)
EMPTY_SCOPE_REASON = "当前执行环境无法隔离空授权范围，仅资料任务暂不能自动启动；请保留资料并调整为已明确授权的范围，或等待离线执行支持。"


def _now(value: datetime | None = None) -> datetime:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _timing(row) -> ScheduleTiming:
    return ScheduleTiming(
        mode=row["mode"], timezone=row["timezone"], start_at=row["start_at"],
        local_time=row["local_time"], weekdays=json.loads(row["weekdays_json"]),
        end_date=row["end_date"], max_runs=row["max_runs"],
    )


def _lock_schedule(conn, schedule_id: str):
    suffix = " FOR UPDATE" if isinstance(conn, PostgresConnection) else ""
    row = conn.execute("SELECT * FROM project_schedules WHERE id=?" + suffix, (schedule_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "计划不存在")
    return row


def _access(row, principal: RequestPrincipal):
    if principal.kind != "user" or (not principal.is_super_admin and row["tenant_id"] != principal.tenant_id):
        raise HTTPException(404, "计划不存在")


def _version(row, version: int):
    if row["version"] != version:
        raise HTTPException(409, "计划已变化，请刷新后重试")


def _creator_available(conn, row):
    available = conn.execute(
        """SELECT 1 FROM users u JOIN tenant_memberships tm ON tm.user_id=u.id
           WHERE u.id=? AND u.disabled=0 AND tm.tenant_id=?""",
        (row["creator_id"], row["tenant_id"]),
    ).fetchone()
    if not available:
        raise HTTPException(409, "计划发起用户已失去访问权，自动启动已停止")


def _validate_input(conn, project: CreateProjectRequest, tenant_id: str, now: datetime):
    scenario = get_scenario_or_404(conn, resolve_scenario_key(conn, project.scenario))
    validate_scenario_values(conn, [scenario])
    resolve_general_workers(conn, tenant_id=tenant_id)
    seen = set()
    for target in project.targets or []:
        key = (target.type, normalize_target_value(target.type, target.value))
        if key in seen:
            raise HTTPException(422, "授权目标重复")
        seen.add(key)
    for value, label in ((project.authorization_expires_at, "授权期限"), (project.explore_deadline, "执行停止时间")):
        if value and timestamp(value) <= now:
            raise HTTPException(422, f"{label}已到期，需显式更新")


def _stored_project(project: CreateProjectRequest, egress: TestEgressView, revisions) -> str:
    public = project.model_dump(mode="json", exclude={"request_id", "resource_batch_id", "resources", "test_egress"})
    public["library_selections"] = [
        {"library_key": item.library_key, "source": "saved", "paths": item.paths, "expected_files": None}
        for item in revisions
    ]
    return json.dumps(public | {"test_egress": egress.model_dump()}, ensure_ascii=False)


def _project_input(row, *, with_auth=False) -> CreateProjectRequest:
    values = json.loads(row["project_json"])
    egress = TestEgressView.model_validate(values.pop("test_egress"))
    body = TestEgressInput(mode=egress.mode, host=egress.host, port=egress.port)
    if with_auth:
        body = inherited_input(egress, row["test_egress_auth"], tenant_id=row["tenant_id"], owner_kind="schedule", owner_id=row["id"])
    return CreateProjectRequest(**values, test_egress=body)


def _check_saved_choices(project, revisions):
    saved = {item.library_key: item.paths for item in project.library_selections}
    frozen = {item.library_key: item.paths for item in revisions}
    if saved != frozen or any(item.source != "saved" for item in project.library_selections):
        raise HTTPException(409, "计划资料配置与已保存绑定不一致")


def _replace_library_bindings(conn, schedule_id: str, selections, *, tenant_id: str, user_id: str):
    existing = list_schedule_library_revisions(conn, schedule_id)
    revisions = resolve_library_selections(
        conn, selections, tenant_id=tenant_id, user_id=user_id, existing=existing,
    )
    conn.execute("DELETE FROM project_schedule_libraries WHERE schedule_id=?", (schedule_id,))
    for revision in revisions:
        bind_schedule_library_revision(conn, schedule_id, revision)
    return revisions


def preview_schedule(rule: ScheduleTiming, *, project: CreateProjectRequest | None = None, now: datetime | None = None) -> dict:
    boundary = effective_boundary(rule, project)
    count = min(3, rule.max_runs) if rule.max_runs is not None else 3
    return {"times": next_times(rule, after=_now(now), count=count, boundary=boundary), "boundary": iso(boundary) if boundary else None}


def _next(rule, project, now, *, exclusive=False):
    values = next_times(rule, after=now, count=1, inclusive=not exclusive, boundary=effective_boundary(rule, project))
    return values[0]["at"] if values else None


def _future_state(rule, project, after, generated):
    if rule.max_runs is not None and generated >= rule.max_runs:
        return None, "completed", None
    next_run = _next(rule, project, after, exclusive=True)
    if next_run:
        return next_run, "active", None
    boundary = effective_boundary(rule, project)
    ending = end_boundary(rule)
    if boundary and (ending is None or boundary < ending):
        return None, "blocked", "授权或执行停止边界前已无后续有效启动时间，请显式更新未来配置后恢复"
    return None, "completed", None


def create_schedule(body: CreateScheduleRequest, principal: RequestPrincipal, *, now: datetime | None = None) -> dict:
    current = _now(now)
    if not principal.tenant_id:
        raise HTTPException(403, "缺少 Tenant")
    with schedule_transaction() as conn:
        payload = body.model_dump(mode="json", exclude={"request_id"})
        if "test_egress" in body.project.model_fields_set:
            payload["project"]["test_egress"] = egress_request_identity(body.project.test_egress)
        else:
            payload["project"].pop("test_egress")
        request = begin_creation_request(
            conn, tenant_id=principal.tenant_id, user_id=principal.subject_id, operation="schedule",
            request_id=str(body.request_id), payload=payload,
        )
        if request["schedule_id"]:
            schedule_id = request["schedule_id"]
        else:
            if timestamp(body.start_at) <= current:
                raise HTTPException(422, "首次启动边界必须晚于当前时间")
            _validate_input(conn, body.project, principal.tenant_id, current)
            next_run = _next(body, body.project, current)
            if next_run is None:
                raise HTTPException(422, "在结束、授权与执行期限内没有有效的未来启动时间")
            schedule_id = str(uuid4())
            egress, encrypted_auth = (None, None) if body.mode == "once" else stored_settings(
                body.project.test_egress, tenant_id=principal.tenant_id, owner_kind="schedule", owner_id=schedule_id,
            )
            revisions = [] if body.mode == "once" else resolve_library_selections(
                conn, body.project.library_selections,
                tenant_id=principal.tenant_id, user_id=principal.subject_id,
            )
            conn.execute(
                """INSERT INTO project_schedules(id,tenant_id,creator_id,mode,status,version,timezone,start_at,
                   local_time,weekdays_json,end_date,max_runs,next_run_at,project_json,created_at,updated_at,test_egress_auth,
                   library_selection_explicit)
                   VALUES(?,?,?,?,'active',1,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (schedule_id, principal.tenant_id, principal.subject_id, body.mode, body.timezone, body.start_at,
                 body.local_time, json.dumps(body.weekdays), body.end_date, body.max_runs, next_run,
                 None if body.mode == "once" else _stored_project(body.project, egress, revisions), iso(current), iso(current), encrypted_auth),
            )
            project_id = None
            if body.mode == "once":
                project_id = create_project_record(body.project, conn=conn, tenant_id=principal.tenant_id, creator=principal.subject_id, initial_status="pending").project.id
                conn.execute("UPDATE project_schedules SET project_id=? WHERE id=?", (project_id, schedule_id))
            else:
                for revision in revisions:
                    bind_schedule_library_revision(conn, schedule_id, revision)
                update_schedule_resources(conn, schedule_id, batch_id=body.project.resource_batch_id, tenant_id=principal.tenant_id, user_id=principal.subject_id, created_by=principal.subject_id)
            complete_creation_request(conn, tenant_id=principal.tenant_id, user_id=principal.subject_id, operation="schedule", request_id=str(body.request_id), project_id=project_id, schedule_id=schedule_id)
    return get_schedule(schedule_id, principal)


def _library_views(conn, schedule_id):
    results = []
    for revision in list_schedule_library_revisions(conn, schedule_id):
        library = get_library(conn, revision.library_key, include_archived=True)
        files = list_library_revision_files(conn, revision)
        results.append({"library_key": revision.library_key, "library_name": library.name,
                        "version": revision.version, "digest_sha256": revision.digest_sha256,
                        "file_count": len(files), "paths": revision.paths,
                        "selection_sha256": revision.selection_sha256})
    return results


def _view(conn, row):
    result = dict(row)
    result.pop("test_egress_auth")
    result["library_selection_explicit"] = bool(result["library_selection_explicit"])
    result["weekdays"] = json.loads(result.pop("weekdays_json"))
    result["project"] = json.loads(result.pop("project_json")) if row["project_json"] else None
    result["resources"] = [item.model_dump() if hasattr(item, "model_dump") else dict(item) for item in list_schedule_resource_records(conn, row["id"])]
    result["library_bindings"] = _library_views(conn, row["id"])
    if row["mode"] == "once":
        project = conn.execute("SELECT explore_deadline,authorization_expires_at,test_egress_json FROM projects WHERE id=?", (row["project_id"],)).fetchone()
        result["test_egress"] = public_project_egress(project).model_dump() if project else None
        result["explore_deadline"] = project["explore_deadline"] if project else None
        result["authorization_expires_at"] = project["authorization_expires_at"] if project else None
        result["once_project_materials_available"] = project is not None
        if project:
            # 单次计划的批次与库绑定已由待启动项目消费，不能从周期计划的表读取。
            result["resources"] = [
                {"id": item.id, "path": item.path, "kind": item.kind,
                 "size_bytes": item.size_bytes, "digest_sha256": item.sha256,
                 "source_name": None}
                for item in list_project_resource_records(row["project_id"], conn=conn)
            ]
            result["library_bindings"] = [
                item.model_dump() for item in _project_library_bindings(conn, row["project_id"])
            ]
    last = conn.execute("SELECT * FROM project_schedule_occurrences WHERE schedule_id=? ORDER BY processed_at DESC,id DESC LIMIT 1", (row["id"],)).fetchone()
    result["last_result"] = dict(last) if last else None
    return result


def get_schedule(schedule_id, principal):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM project_schedules WHERE id=?", (schedule_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "计划不存在")
        _access(row, principal)
        return _view(conn, row)


def get_schedule_library_manifest(schedule_id: str, key: str, principal: RequestPrincipal) -> SelectableLibraryManifest:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM project_schedules WHERE id=?", (schedule_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "计划不存在")
        _access(row, principal)
        if row["mode"] == "once":
            bindings = list_library_bindings(conn, row["project_id"]) if row["project_id"] else []
            binding = next((item for item in bindings if item.library_key == key), None)
            revision = (get_bound_library_revision(conn, row["project_id"], key, binding.version)
                        if binding else None)
        else:
            revision = next((item for item in list_schedule_library_revisions(conn, schedule_id)
                             if item.library_key == key), None)
        if revision is None:
            raise HTTPException(404, "计划未绑定该资料库")
        library = get_library(conn, key, include_archived=True)
        files = list_library_revision_files(conn, revision)
        return SelectableLibraryManifest(
            library_key=key, library_name=library.name, file_count=len(files), files=files,
            safety_path=safety_file_path(key),
        )


def list_schedules(principal):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM project_schedules" + ("" if principal.is_super_admin else " WHERE tenant_id=?") + " ORDER BY created_at DESC,id", () if principal.is_super_admin else (principal.tenant_id,)).fetchall()
        return [_view(conn, row) for row in rows]


def schedule_history(schedule_id, principal, *, offset=0, limit=20):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM project_schedules WHERE id=?", (schedule_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "计划不存在")
        _access(row, principal)
        count = conn.execute("SELECT COUNT(*) AS count FROM project_schedule_occurrences WHERE schedule_id=?", (schedule_id,)).fetchone()["count"]
        items = conn.execute("SELECT * FROM project_schedule_occurrences WHERE schedule_id=? ORDER BY scheduled_at DESC,id DESC LIMIT ? OFFSET ?", (schedule_id, limit, offset)).fetchall()
        return {"items": [dict(item) for item in items], "total": count}


def update_schedule(schedule_id, body: UpdateScheduleRequest, principal, *, now=None):
    current = _now(now)
    with schedule_transaction() as conn:
        row = _lock_schedule(conn, schedule_id)
        _access(row, principal)
        _version(row, body.version)
        if row["status"] in {"completed", "disabled"}:
            raise HTTPException(409, "已完成或停用的计划不能编辑")
        if (row["mode"] == "once") != (body.mode == "once"):
            raise HTTPException(422, "单次与周期计划不能互相转换")
        if body.mode == "once":
            project = _lock_project(conn, row["project_id"])
            if project["status"] != "pending":
                raise HTTPException(409, "预约已经触发，不能重新排期")
            if timestamp(body.start_at) <= current:
                raise HTTPException(422, "新启动时间必须晚于当前时间")
            if "explore_deadline" in body.model_fields_set:
                if body.explore_deadline and timestamp(body.explore_deadline) <= current:
                    raise HTTPException(422, "执行停止时间必须晚于当前时间")
                conn.execute("UPDATE projects SET explore_deadline=? WHERE id=?", (body.explore_deadline, project["id"]))
                project = _lock_project(conn, project["id"])
            if body.test_egress is not None:
                save_project_egress(conn, project["id"], body.test_egress)
            _validate_pending(conn, project, current, require_scope=False)
            boundary = min((timestamp(project[key]) for key in ("authorization_expires_at", "explore_deadline") if project[key]), default=None)
            if boundary and timestamp(body.start_at) >= boundary:
                raise HTTPException(422, "启动时间必须早于授权和执行停止边界")
            project_json = None
            next_run = iso(timestamp(body.start_at))
        else:
            stored_input = _project_input(row)
            project = body.project
            revisions = list_schedule_library_revisions(conn, schedule_id)
            old_choices = {item.library_key: (item.version, item.paths) for item in revisions}
            choices_supplied = "library_selections" in project.model_fields_set
            if not choices_supplied:
                _check_saved_choices(stored_input, revisions)
                project = project.model_copy(update={"library_selections": stored_input.library_selections})
            if stored_input.targets is not None and project.targets is None:
                raise HTTPException(422, "已有显式范围计划必须保留 targets，不能在编辑中改为 Origin 来源")
            _validate_input(conn, project, row["tenant_id"], current)
            egress, encrypted_auth = stored_settings(
                project.test_egress, tenant_id=row["tenant_id"], owner_kind="schedule", owner_id=schedule_id,
                current=TestEgressView.model_validate(json.loads(row["project_json"])["test_egress"]),
                encrypted_auth=row["test_egress_auth"],
            )
            conn.execute("UPDATE project_schedules SET test_egress_auth=? WHERE id=?", (encrypted_auth, schedule_id))
            if choices_supplied:
                revisions = _replace_library_bindings(conn, schedule_id, project.library_selections,
                                                      tenant_id=row["tenant_id"], user_id=principal.subject_id)
            project_json = _stored_project(project, egress, revisions)
            new_choices = {item.library_key: (item.version, item.paths) for item in revisions}
            # 未改变旧计划的选择时保留 legacy；空旧计划需显式表示改为空选择。
            explicit = bool(row["library_selection_explicit"] or body.library_selection_explicit
                            or old_choices != new_choices)
            conn.execute("UPDATE project_schedules SET library_selection_explicit=? WHERE id=?",
                         (int(explicit), schedule_id))
            update_schedule_resources(conn, schedule_id, resource_ids=body.resource_ids, batch_id=project.resource_batch_id, tenant_id=row["tenant_id"], user_id=principal.subject_id, created_by=principal.subject_id)
            next_run, status, reason = _future_state(body, project, current, row["generated_count"])
        if body.mode == "once":
            status, reason = "active", None
        elif row["status"] == "paused" and status == "active":
            status = "paused"
        conn.execute(
            """UPDATE project_schedules SET mode=?,timezone=?,start_at=?,local_time=?,weekdays_json=?,end_date=?,max_runs=?,
               next_run_at=?,project_json=?,status=?,block_reason=?,version=version+1,updated_at=? WHERE id=?""",
            (body.mode, body.timezone, body.start_at, body.local_time, json.dumps(body.weekdays), body.end_date, body.max_runs,
             next_run, project_json, status, reason, iso(current), schedule_id),
        )
    return get_schedule(schedule_id, principal)


def _lock_project(conn, project_id):
    if not project_id:
        raise HTTPException(409, "关联项目已删除")
    conn.execute("UPDATE projects SET id=id WHERE id=?", (project_id,))
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if project is None:
        raise HTTPException(409, "关联项目已删除")
    return project


def _validate_pending(conn, project, current, *, require_scope=True):
    get_scenario_or_404(conn, project["scenario_key"])
    from cairn.server.schemas.scenarios import Scenario
    from .assessment_policy import parse_scenario_snapshot, require_execution_contract
    from .scenarios import scenario_feature_schema

    require_execution_contract(project)
    snapshot = parse_scenario_snapshot(project["scenario_snapshot_json"])
    # 执行使用冻结引用；只从冻结特性中取场景可配置项作校验，不改写项目原件。
    scenario = Scenario.model_validate(snapshot)
    allowed = {item.key for item in scenario_feature_schema()}
    scenario = scenario.model_copy(update={"features": {key: value for key, value in scenario.features.items() if key in allowed}})
    validate_scenario_values(conn, [scenario])
    resolve_general_workers(conn, tenant_id=project["tenant_id"])
    for key, label in (("authorization_expires_at", "授权期限"), ("explore_deadline", "执行停止时间")):
        if project[key] and timestamp(project[key]) <= current:
            raise HTTPException(409, f"{label}已到期，请显式更新")
    reason = project_authorization_block_reason(conn, project)
    if reason:
        raise HTTPException(409, "项目需要重新授权：" + reason)
    scope_source = scope_source_from_descriptor(authorization_scope_projection(conn, project).descriptor)
    if require_scope and scope_source == "targets" and not conn.execute("SELECT 1 FROM targets WHERE project_id=? AND status IN ('seed','approved') LIMIT 1", (project["id"],)).fetchone():
        raise HTTPException(409, EMPTY_SCOPE_REASON)
    validate_project_resource_records(conn, project["id"])
    for binding in list_library_bindings(conn, project["id"]):
        revision = get_bound_library_revision(conn, project["id"], binding.library_key, binding.version)
        validate_library_revision_content(conn, revision)


def _occurrence(conn, row, key, scheduled_at, current, status, *, project_id=None, reason=None):
    existing = conn.execute("SELECT * FROM project_schedule_occurrences WHERE schedule_id=? AND occurrence_key=?", (row["id"], key)).fetchone()
    if existing and existing["status"] != "blocked":
        return False
    if existing:
        # 同一单次记录可以从受阻恢复为首次启动，但不更换逻辑身份。
        previous = existing["reason"]
        if previous and previous != reason:
            reason = "先前受阻：" + previous + ("；" + reason if reason else "")
        conn.execute("UPDATE project_schedule_occurrences SET scheduled_at=?,processed_at=?,project_id=?,config_version=?,status=?,reason=? WHERE id=?", (scheduled_at, iso(current), project_id, row["version"], status, reason, existing["id"]))
    else:
        conn.execute("INSERT INTO project_schedule_occurrences(id,schedule_id,occurrence_key,scheduled_at,processed_at,project_id,config_version,status,reason) VALUES(?,?,?,?,?,?,?,?,?)", (str(uuid4()), row["id"], key, scheduled_at, iso(current), project_id, row["version"], status, reason))
    return True


def _consume_locked(conn, row, current, *, force=False):
    if not force and (row["status"] != "active" or not row["next_run_at"] or timestamp(row["next_run_at"]) > current):
        return False
    _creator_available(conn, row)
    if row["mode"] == "once":
        project = _lock_project(conn, row["project_id"])
        if project["status"] != "pending":
            raise HTTPException(409, "预约已经触发，不能再次启动")
        _validate_pending(conn, project, current)
        scheduled_at = row["next_run_at"] or iso(timestamp(row["start_at"]))
        delay = max(0, int((current - timestamp(scheduled_at)).total_seconds()))
        if not _occurrence(conn, row, "once", scheduled_at, current, "started", project_id=project["id"], reason=f"延迟 {delay} 秒启动" if delay else None):
            return False
        conn.execute("UPDATE projects SET status='active' WHERE id=? AND status='pending'", (project["id"],))
        conn.execute("UPDATE project_schedules SET status='completed',next_run_at=NULL,block_reason=NULL,last_project_id=?,generated_count=1,updated_at=? WHERE id=?", (project["id"], iso(current), row["id"]))
        return True
    rule = _timing(row)
    project_input = _project_input(row)
    if rule.max_runs is not None and row["generated_count"] >= rule.max_runs:
        conn.execute("UPDATE project_schedules SET status='completed',next_run_at=NULL,updated_at=? WHERE id=?", (iso(current), row["id"]))
        return True
    # 正常结束优先，避免停机后已结束计划又被较晚失效的授权标成受阻。
    ending = end_boundary(rule)
    stop = effective_boundary(rule, project_input)
    if ending and current >= ending and stop == ending:
        due = row["next_run_at"]
        _occurrence(conn, row, due, due, current, "missed", reason=f"结束边界前错过：{due} 至 {iso(ending)}；计划已结束，历史轮次不补跑")
        conn.execute("UPDATE project_schedules SET status='completed',next_run_at=NULL,updated_at=? WHERE id=?", (iso(current), row["id"]))
        return True
    _validate_input(conn, project_input, row["tenant_id"], current)
    due = row["next_run_at"]
    if timestamp(due) < current - timedelta(seconds=60):
        future = _next(rule, project_input, current - timedelta(seconds=60))
        _occurrence(conn, row, due, due, current, "missed", reason=f"停机或延迟错过：{due} 至 {iso(current - timedelta(seconds=60))}；历史轮次不补跑")
        due = future
        status, reason = "active", None
        if future is None:
            _, status, reason = _future_state(rule, project_input, current, row["generated_count"])
        conn.execute("UPDATE project_schedules SET next_run_at=?,status=?,block_reason=?,updated_at=? WHERE id=?", (future, status, reason, iso(current), row["id"]))
        if due is None or timestamp(due) > current:
            return True
    if project_input.targets == []:
        raise HTTPException(409, EMPTY_SCOPE_REASON)
    revisions = list_schedule_library_revisions(conn, row["id"])
    _check_saved_choices(project_input, revisions)
    for revision in revisions:
        validate_library_revision_content(conn, revision)
    last_project = _lock_project(conn, row["last_project_id"]) if row["last_project_id"] else None
    if last_project is not None and (last_project["status"] in {"active", "paused", "pending"} or project_pause_draining(conn, last_project)):
        _occurrence(conn, row, due, due, current, "skipped", reason="上一轮项目尚未结束或物理清理未确认，不生成重叠项目")
        generated = row["generated_count"]
        project_id = row["last_project_id"]
    else:
        existing = conn.execute("SELECT status FROM project_schedule_occurrences WHERE schedule_id=? AND occurrence_key=?", (row["id"], due)).fetchone()
        if existing and existing["status"] != "blocked":
            generated = row["generated_count"]
            project_id = row["last_project_id"]
        else:
            detail = create_project_record(
                _project_input(row, with_auth=True), conn=conn, tenant_id=row["tenant_id"],
                creator=row["creator_id"], library_revisions=revisions,
                library_selection_explicit=bool(row["library_selection_explicit"]),
            )
            project_id = detail.project.id
            copy_schedule_resources_to_project(conn, row["id"], project_id, created_by=row["creator_id"])
            _occurrence(conn, row, due, due, current, "started", project_id=project_id)
            generated = row["generated_count"] + 1
    next_run, status, reason = _future_state(rule, project_input, timestamp(due), generated)
    conn.execute("UPDATE project_schedules SET generated_count=?,last_project_id=?,next_run_at=?,status=?,block_reason=?,updated_at=? WHERE id=?", (generated, project_id, next_run, status, reason, iso(current), row["id"]))
    return True


def _record_blocked(schedule_id, version, current, reason):
    with schedule_transaction() as conn:
        row = _lock_schedule(conn, schedule_id)
        if row["version"] != version or row["status"] not in {"active", "blocked", "cancelled"}:
            return
        if row["mode"] == "once":
            project = _lock_project(conn, row["project_id"])
            if project["status"] != "pending":
                return
        due = row["next_run_at"] or iso(timestamp(row["start_at"]))
        if row["mode"] != "once" and timestamp(due) < current - timedelta(seconds=60):
            # 失败事务回滚后仍保留错过范围，不将受阻期间的历史轮次当作待补任务。
            reason = f"停机或延迟错过：{due} 至 {iso(current - timedelta(seconds=60))}；历史轮次不补跑；" + reason
        _occurrence(conn, row, "once" if row["mode"] == "once" else due, due, current, "blocked", reason=reason)
        conn.execute("UPDATE project_schedules SET status='blocked',block_reason=?,updated_at=? WHERE id=?", (reason, iso(current), schedule_id))


def schedule_action(schedule_id, body: ScheduleActionRequest, principal, *, now=None):
    current = _now(now)
    failed_version = None
    try:
        with schedule_transaction() as conn:
            row = _lock_schedule(conn, schedule_id)
            _access(row, principal)
            _version(row, body.version)
            if body.action == "start_now":
                if row["mode"] != "once" or row["status"] not in {"active", "blocked", "cancelled"}:
                    raise HTTPException(409, "只有尚未触发的单次预约可立即启动")
                failed_version = row["version"]
                _consume_locked(conn, row, current, force=True)
            else:
                states = {"cancel": {"active", "blocked", "cancelled"}, "pause": {"active", "blocked"}, "resume": {"paused", "blocked"}, "disable": {"active", "paused", "blocked"}}
                if row["status"] not in states[body.action]:
                    raise HTTPException(409, "当前计划状态不支持该操作；若已触发，请使用项目停止动作")
                if (body.action == "cancel") != (row["mode"] == "once"):
                    raise HTTPException(422, "单次使用取消／立即启动，周期使用暂停／恢复／停用")
                next_run = row["next_run_at"]
                reason = None
                status = {"cancel": "cancelled", "pause": "paused", "resume": "active", "disable": "disabled"}[body.action]
                if body.action in {"cancel", "disable"}:
                    next_run = None
                elif body.action == "resume":
                    _creator_available(conn, row)
                    project_input = _project_input(row)
                    _validate_input(conn, project_input, row["tenant_id"], current)
                    next_run, status, reason = _future_state(_timing(row), project_input, current, row["generated_count"])
                conn.execute("UPDATE project_schedules SET status=?,next_run_at=?,block_reason=?,version=version+1,updated_at=? WHERE id=?", (status, next_run, reason, iso(current), schedule_id))
    except HTTPException as exc:
        if failed_version is None:
            raise
        _record_blocked(schedule_id, failed_version, current, str(exc.detail))
    return get_schedule(schedule_id, principal)


def consume_due_schedules(*, now: datetime | None = None, limit: int = 50) -> dict:
    current = _now(now)
    with get_conn() as conn:
        ids = [row["id"] for row in conn.execute("SELECT id FROM project_schedules WHERE status='active' AND next_run_at<=? ORDER BY next_run_at,id LIMIT ?", (iso(current), limit)).fetchall()]
    processed = errors = 0
    for schedule_id in ids:
        version = None
        try:
            with schedule_transaction() as conn:
                row = _lock_schedule(conn, schedule_id)
                version = row["version"]
                processed += int(_consume_locked(conn, row, current))
        except HTTPException as exc:
            if version is None:
                raise
            _record_blocked(schedule_id, version, current, str(exc.detail))
            processed += 1
        except Exception as exc:
            # 每个计划独立回滚；未知错误持久化类型并报告计数，不把失败冒充成功。
            logger.error("schedule processing failed schedule_id=%s error_type=%s", schedule_id, type(exc).__name__)
            if version is not None:
                _record_blocked(schedule_id, version, current, f"计划处理失败（{type(exc).__name__}），请查看服务诊断并修正后恢复")
            errors += 1
    return {"processed": processed, "errors": errors}
