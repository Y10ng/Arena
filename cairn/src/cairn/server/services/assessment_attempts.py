"""结构化评估尝试的运行重试与收敛规则。"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from fastapi import HTTPException

from cairn.assessment_contracts import GOAL_DRIVEN_PROTOCOL
from .assessment_policy import parse_scenario_snapshot, row_value
from .time_utils import parse_utc_timestamp, utcnow


DEFAULT_MAX_RUNTIME_RETRIES = 2
EXTENDED_MAX_RUNTIME_RETRIES = 10
MAX_RETRY_BACKOFF_SECONDS = 300

# 交付失败只允许原会话续交付，不能加入可重新探索的通用白名单。
DELIVERY_RETRY_ERROR_TYPES = {"submission_missing", "submission_invalid"}
ACCEPTED_RESULT_CONTEXT_FIELDS = (
    "accepted_result_digest", "accepted_fact_id", "accepted_completion_digest",
    "accepted_completion_intent_id", "accepted_intent_submissions",
)


def validate_delivery_registration(conn, project, run, body, snapshot):
    """仅当前 running owner 能一次登记；不接受失败后补造来源或任意上下文扩展。"""
    from re import fullmatch
    from uuid import UUID

    context = parse_scenario_snapshot(run["runtime_context_json"])
    if (
        run["status"] != "running"
        or body.status not in {None, "running"}
        or run["execution_generation"] != project["execution_generation"]
        or run["pause_requested"]
    ):
        raise HTTPException(409, "execution_delivery_registration_closed")
    if any(context.get(key) for key in (
        "accepted_result_digest", "accepted_fact_id",
        "accepted_completion_digest", "accepted_intent_submissions",
    )):
        raise HTTPException(409, "execution_delivery_registration_closed")
    fields = {
        "session_id",
        "phase",
        "deadline_at",
        "container_id",
        "model_binding_sha256",
        "result_schema_sha256",
        "binding",
        "inputs",
    }
    if not isinstance(snapshot, dict) or set(snapshot) != fields:
        raise HTTPException(422, "execution_delivery_invalid")
    session = snapshot["session_id"]
    try:
        if not isinstance(session, str) or not session.startswith("pi:") or str(UUID(session[3:])) != session[3:]:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, "execution_delivery_invalid") from None
    if session != (body.session_id or run["session_id"]) or (run["session_id"] and run["session_id"] != session):
        raise HTTPException(422, "execution_delivery_invalid")
    previous_session_runs = conn.execute(
        "SELECT * FROM task_runs WHERE session_id = ? AND NOT (id = ? AND project_id = ?)",
        (session, run["id"], project["id"]),
    ).fetchall()
    continuation = context.get("continuation")
    if continuation:
        activation = conn.execute(
            "SELECT container_id, state FROM project_container_activations WHERE project_id = ? AND dispatcher_id = ?",
            (project["id"], context.get("dispatcher_id")),
        ).fetchone()
        if (not isinstance(continuation, dict)
                or snapshot.get("container_id") != continuation.get("container_id")
                or activation is None or activation["state"] != "running"
                or activation["container_id"] != continuation.get("container_id")):
            raise HTTPException(409, "execution_continuation_workspace_unavailable")
    if previous_session_runs and not (
        isinstance(continuation, dict)
        and any(previous["id"] == continuation.get("source_task_run_id") for previous in previous_session_runs)
        and all(
            previous["project_id"] == project["id"]
            and previous["task_type"] == run["task_type"]
            and previous["intent_id"] == run["intent_id"]
            and previous["status"] not in {"queued", "running"}
            for previous in previous_session_runs
        )
    ):
        raise HTTPException(409, "execution_delivery_session_already_bound")
    phases = {run["task_type"] + "_execute"}
    intent = None
    if run["task_type"] == "explore":
        intent = conn.execute(
            """SELECT assessment_role, target_observation_id
               FROM intents WHERE project_id = ? AND id = ?""",
            (project["id"], run["intent_id"]),
        ).fetchone()
        if intent is not None and intent["assessment_role"] == "verifier":
            phases = {"verifier_execute"}
    deadline = parse_utc_timestamp(snapshot["deadline_at"])
    # Report 在探索截止后正常 finalization，只受原报告执行 deadline 约束。
    project_deadline = parse_utc_timestamp(project["explore_deadline"]) if run["task_type"] != "report" else None
    if (
        not isinstance(snapshot["phase"], str)
        or snapshot["phase"] not in phases
        or not deadline
        or deadline <= parse_utc_timestamp(utcnow())
        or (project_deadline and deadline > project_deadline)
    ):
        raise HTTPException(422, "execution_delivery_invalid")
    if not isinstance(snapshot["container_id"], str) or not snapshot["container_id"] or snapshot["container_id"] == "unknown" or len(snapshot["container_id"]) > 128:
        raise HTTPException(422, "execution_delivery_invalid")
    for digest_field in ("model_binding_sha256", "result_schema_sha256"):
        digest = snapshot[digest_field]
        if not isinstance(digest, str) or not fullmatch(r"[a-f0-9]{64}", digest):
            raise HTTPException(422, "execution_delivery_invalid")
    allowed_binding = {
        "reason": {
            "allowed_fact_ids",
            "allowed_intent_ids",
            "max_intents",
            "review_input_digest",
            "structured_intent_binding",
            "decision_input_digest",
        },
        "explore": {
            "slot_key",
            "surface_key",
            "test_family",
            "verification_observation_id",
            "assessment_protocol",
        },
        "bootstrap": {"assessment_protocol"},
    }.get(run["task_type"], set())
    allowed_inputs = {
        "reason": {"open_intent_ids"},
        "report": {
            "snapshot_path", "input_snapshot_artifact_id",
            "input_snapshot_sha256", "generation_token",
        },
    }.get(run["task_type"], set())
    binding = snapshot["binding"]
    inputs = snapshot["inputs"]
    if (
        (binding is not None and (not isinstance(binding, dict) or not set(binding) <= allowed_binding))
        or not isinstance(inputs, dict)
        or not set(inputs) <= allowed_inputs
    ):
        raise HTTPException(422, "execution_delivery_invalid")
    if run["task_type"] == "reason":
        if (
            not isinstance(binding, dict)
            or not {"allowed_fact_ids", "max_intents"} <= set(binding)
            or type(binding["max_intents"]) is not int
            or binding["max_intents"] < 1
            or set(inputs) != {"open_intent_ids"}
        ):
            raise HTTPException(422, "execution_delivery_invalid")
        for table, values in (("facts", binding["allowed_fact_ids"]), ("intents", inputs["open_intent_ids"])):
            if (
                not isinstance(values, list)
                or any(not isinstance(value, str) or not value or len(value) > 256 for value in values)
                or len(set(values)) != len(values)
            ):
                raise HTTPException(422, "execution_delivery_invalid")
            for value in values:
                if conn.execute(f"SELECT 1 FROM {table} WHERE project_id = ? AND id = ?", (project["id"], value)).fetchone() is None:
                    raise HTTPException(422, "execution_delivery_invalid")
        if "review_input_digest" in binding and (not isinstance(binding["review_input_digest"], str) or not fullmatch(r"[a-f0-9]{64}", binding["review_input_digest"])):
            raise HTTPException(422, "execution_delivery_invalid")
        goal_driven = (
            row_value(project, "assessment_protocol") == GOAL_DRIVEN_PROTOCOL
        )
        decision_digest = binding.get("decision_input_digest")
        if goal_driven:
            if (
                set(binding)
                != {"allowed_fact_ids", "allowed_intent_ids", "max_intents", "decision_input_digest"}
                or not isinstance(decision_digest, str)
                or fullmatch(r"[a-f0-9]{64}", decision_digest) is None
            ):
                raise HTTPException(422, "execution_delivery_invalid")
            intent_ids = binding["allowed_intent_ids"]
            if (
                not isinstance(intent_ids, list)
                or any(not isinstance(value, str) or not value or len(value) > 256 for value in intent_ids)
                or len(set(intent_ids)) != len(intent_ids)
            ):
                raise HTTPException(422, "execution_delivery_invalid")
            for intent_id in intent_ids:
                if conn.execute(
                    "SELECT 1 FROM intents WHERE project_id = ? AND id = ?",
                    (project["id"], intent_id),
                ).fetchone() is None:
                    raise HTTPException(422, "execution_delivery_invalid")
        elif decision_digest is not None:
            raise HTTPException(422, "execution_delivery_invalid")
        expected_structured = context.get("structured_intent_binding")
        supplied_structured = binding.get("structured_intent_binding")
        if supplied_structured != expected_structured:
            raise HTTPException(422, "execution_delivery_invalid")
        if expected_structured is not None:
            from .structured_intent_bindings import parse_structured_intent_binding

            try:
                parsed = parse_structured_intent_binding(supplied_structured)
            except HTTPException as exc:
                raise HTTPException(422, "execution_delivery_invalid") from exc
            if parsed.model_dump(mode="json", exclude_none=True) != expected_structured:
                raise HTTPException(422, "execution_delivery_invalid")
    elif run["task_type"] == "explore":
        if binding is not None and any(not isinstance(value, str) or not value or len(value) > 256 for value in binding.values()):
            raise HTTPException(422, "execution_delivery_invalid")
        expected_structured = context.get("structured_explore_binding")
        if expected_structured is not None:
            from .structured_intent_bindings import parse_structured_explore_binding

            try:
                parsed = parse_structured_explore_binding(expected_structured)
            except HTTPException as exc:
                raise HTTPException(422, "execution_delivery_invalid") from exc
            expected_binding = {
                "slot_key": parsed.cell.slot_key,
                "surface_key": parsed.cell.surface_key,
                "test_family": parsed.cell.test_family,
            }
            if binding != expected_binding:
                raise HTTPException(422, "execution_delivery_invalid")
        elif intent is not None and intent["assessment_role"] == "verifier":
            if binding != {
                "verification_observation_id": intent["target_observation_id"]
            }:
                raise HTTPException(422, "execution_delivery_invalid")
        elif row_value(project, "assessment_protocol") == GOAL_DRIVEN_PROTOCOL:
            if binding != {"assessment_protocol": GOAL_DRIVEN_PROTOCOL}:
                raise HTTPException(422, "execution_delivery_invalid")
        elif binding is not None:
            raise HTTPException(422, "execution_delivery_invalid")
    elif run["task_type"] == "bootstrap":
        goal_driven = (
            row_value(project, "assessment_protocol") == GOAL_DRIVEN_PROTOCOL
        )
        expected_binding = (
            {"assessment_protocol": GOAL_DRIVEN_PROTOCOL}
            if goal_driven
            else None
        )
        if binding != expected_binding:
            raise HTTPException(422, "execution_delivery_invalid")
    elif binding is not None:
        raise HTTPException(422, "execution_delivery_invalid")
    if run["task_type"] == "report":
        finalization = conn.execute("SELECT * FROM assessment_report_finalizations WHERE project_id = ?", (project["id"],)).fetchone()
        if (
            set(inputs) != allowed_inputs
            or any(not isinstance(value, str) or not value for value in inputs.values())
            or not fullmatch(r"[a-f0-9]{64}", inputs.get("input_snapshot_sha256", ""))
        ):
            raise HTTPException(422, "execution_delivery_invalid")
        # 仅引用当前 claim 已校验归属的 artifact，容器文件路径由原 TaskRun 固定派生。
        if (
            finalization is None
            or finalization["status"] != "running"
            or finalization["claimed_by"] != run["worker_name"]
            or inputs["snapshot_path"] != f"/tmp/cairn-prompts/report-delivery-{run['id']}.json"
            or inputs["generation_token"] != finalization["generation_token"]
            or inputs["generation_token"] != context.get("report_claim", {}).get("generation_token")
            or inputs["input_snapshot_artifact_id"] != finalization["input_snapshot_artifact_id"]
            or inputs["input_snapshot_sha256"] != finalization["input_snapshot_sha256"]
        ):
            raise HTTPException(422, "execution_delivery_invalid")

    from cairn.dispatcher.worker_protocol.submission import build_submit_result_schema
    from cairn.server.schemas.candidates import canonical_value_sha256

    try:
        expected_schema_sha256 = canonical_value_sha256(
            build_submit_result_schema(snapshot["phase"], binding=binding, inputs=inputs)
        )
    except (KeyError, TypeError, ValueError):
        raise HTTPException(422, "execution_delivery_invalid") from None
    if snapshot["result_schema_sha256"] != expected_schema_sha256:
        raise HTTPException(422, "execution_delivery_invalid")


def _started_pause_source(conn, project, newest):
    """仅沿 Server 记录的未启动续作来源回溯，绝不跨工作或阶段复用会话。"""
    phase = parse_scenario_snapshot(project["assessment_progress_json"]).get("phase")
    row = newest
    seen = set()
    while row["id"] not in seen:
        seen.add(row["id"])
        context = parse_scenario_snapshot(row["runtime_context_json"])
        if (
            not row["pause_interrupted"]
            or row["execution_generation"] >= project["execution_generation"]
            or ("assessment_phase" in context and context["assessment_phase"] != phase)
        ):
            return None
        lifecycle = context.get("execution_lifecycle")
        if not isinstance(lifecycle, dict) or lifecycle.get("process_started") is True:
            return row
        predecessor = context.get("continuation")
        if predecessor is None:
            return row
        source_id = predecessor.get("source_task_run_id") if isinstance(predecessor, dict) else None
        if not isinstance(source_id, str) or not source_id:
            return row
        source = conn.execute(
            "SELECT * FROM task_runs WHERE project_id = ? AND id = ?",
            (project["id"], source_id),
        ).fetchone()
        if (
            source is None or source["execution_generation"] >= row["execution_generation"]
            or source["task_type"] != row["task_type"] or source["intent_id"] != row["intent_id"]
        ):
            return row
        row = source
    return row


def _prestart_continuation_source(conn, project, run):
    """仅对尚未登记本次 session 执行、且旧来源仍有效的失败续作开放有界重试。"""
    context = parse_scenario_snapshot(run["runtime_context_json"])
    continuation = context.get("continuation")
    lifecycle = context.get("execution_lifecycle")
    if (
        run["status"] not in {"failed", "timed_out"}
        or run["pause_interrupted"]
        or run["execution_generation"] != project["execution_generation"]
        or not isinstance(continuation, dict)
        or continuation.get("execution_generation") != project["execution_generation"]
        or not isinstance(lifecycle, dict)
        or lifecycle.get("process_started") is not False
        or lifecycle.get("termination_confirmed") is not True
        or context.get("execution_delivery") is not None
        or any(context.get(key) for key in ACCEPTED_RESULT_CONTEXT_FIELDS)
    ):
        return None
    source_id = continuation.get("source_task_run_id")
    if not isinstance(source_id, str) or not source_id:
        return None
    source = conn.execute(
        "SELECT * FROM task_runs WHERE project_id = ? AND id = ?", (project["id"], source_id),
    ).fetchone()
    if source is None or source["execution_generation"] >= run["execution_generation"] or not source["pause_interrupted"]:
        return None
    previous = parse_scenario_snapshot(source["runtime_context_json"])
    prior_lifecycle = previous.get("execution_lifecycle")
    snapshot = previous.get("execution_delivery")
    if (
        source["status"] not in {"cancelled", "failed", "timed_out"}
        or source["task_type"] != run["task_type"]
        or source["intent_id"] != run["intent_id"]
        or source["assessment_attempt_id"] != run["assessment_attempt_id"]
        or source["worker_name"] != run["worker_name"]
        or previous.get("dispatcher_id") != context.get("dispatcher_id")
        or any(previous.get(key) for key in ACCEPTED_RESULT_CONTEXT_FIELDS)
        or not isinstance(prior_lifecycle, dict)
        or prior_lifecycle.get("generation") != source["execution_generation"]
        or prior_lifecycle.get("process_started") is not True
        or prior_lifecycle.get("termination_confirmed") is not True
        or prior_lifecycle.get("cleanup_confirmed") is not True
        or not isinstance(snapshot, dict)
        or not source["session_id"]
        or snapshot.get("session_id") != source["session_id"]
        or run["session_id"] != source["session_id"]
        or continuation.get("container_id") != snapshot.get("container_id")
    ):
        return None
    activation = conn.execute(
        "SELECT * FROM project_container_activations WHERE project_id = ? AND dispatcher_id = ?",
        (project["id"], context.get("dispatcher_id")),
    ).fetchone()
    if (
        activation is None or activation["container_id"] != snapshot["container_id"]
        or activation["state"] not in {"stopped", "running"}
        or (activation["state"] == "running" and activation["activation_generation"] != project["execution_generation"])
    ):
        return None
    return source


def delivery_retry_routes(conn, project, *, executions=None):
    """一次分组派生当前逻辑工作的路由提示；admission 仍在写锁内重新校验。"""
    if executions is None:
        executions = conn.execute("SELECT * FROM task_runs WHERE project_id = ? ORDER BY COALESCE(started_at, ended_at), LENGTH(id), id", (project["id"],)).fetchall()
    phase = parse_scenario_snapshot(project["assessment_progress_json"]).get("phase")
    report = conn.execute("SELECT generation_token FROM assessment_report_finalizations WHERE project_id = ?", (project["id"],)).fetchone()
    token = report["generation_token"] if report else None
    groups = {}
    latest_by_work = {}
    for run in executions:
        if run["execution_generation"] > project["execution_generation"]:
            continue
        key = (run["task_type"], run["intent_id"])
        previous = latest_by_work.get(key)
        if previous is None or (run["execution_generation"], len(run["id"]), run["id"]) > (
            previous["execution_generation"], len(previous["id"]), previous["id"]
        ):
            latest_by_work[key] = run
        if run["execution_generation"] != project["execution_generation"] or run["pause_interrupted"]:
            continue
        context = parse_scenario_snapshot(run["runtime_context_json"])
        if run["task_type"] == "report":
            claim = context.get("report_claim", {})
            if not token or not isinstance(claim, dict) or claim.get("generation_token") != token:
                continue
        elif "assessment_phase" in context and context["assessment_phase"] != phase:
            continue
        groups.setdefault((run["task_type"], run["intent_id"]), []).append(run)
    routes = {}
    for (task_type, intent_id), runs in groups.items():
        runs = _current_execution_chain(conn, project, task_type, runs)
        if not runs:
            continue
        source = runs[-1]
        context = parse_scenario_snapshot(source["runtime_context_json"])
        delivery_retry = _is_delivery_execution(source)
        eligibility = execution_retry_eligibility(conn, project, task_type, intent_id=intent_id, report_generation_token=token, executions=runs)
        continuation_source = _prestart_continuation_source(conn, project, source) if context.get("continuation") and not delivery_retry else None
        if context.get("continuation") and source["status"] != "succeeded" and not delivery_retry and continuation_source is None:
            eligibility = {"allowed": False, "reason": "execution_continuation_unavailable", "retry_at": None}
        # 普通失败仍走 fresh；仅已证明启动前失败的 continuation 沿原来源重试。
        if not delivery_retry and eligibility["allowed"] and continuation_source is None:
            continue
        if delivery_retry and (source["status"] == "succeeded" or any(run["status"] in {"queued", "running"} for run in runs)):
            eligibility = {"allowed": False, "reason": "assessment_execution_not_quiescent", "retry_at": None}
        snapshot = context.get("execution_delivery") if delivery_retry else None
        routes[source["id"]] = {
            **eligibility, "source_task_run_id": continuation_source["id"] if continuation_source is not None else source["id"],
            "task_type": task_type, "intent_id": intent_id,
            "dispatcher_id": context.get("dispatcher_id"), "worker_name": source["worker_name"],
            "container_id": (context["continuation"]["container_id"] if continuation_source is not None
                             else snapshot.get("container_id") if isinstance(snapshot, dict) else None),
            "continuation": continuation_source is not None,
        }
    # 已被较新物理执行取代的旧会话不能在切换评估阶段后重新成为路由。
    for key, newest in latest_by_work.items():
        if key in groups or key[0] not in {"bootstrap", "explore", "reason"}:
            continue
        if newest["execution_generation"] >= project["execution_generation"] or not newest["pause_interrupted"]:
            continue
        run = _started_pause_source(conn, project, newest)
        if run is None:
            continue
        context = parse_scenario_snapshot(run["runtime_context_json"])
        lifecycle = context.get("execution_lifecycle", {})
        snapshot = context.get("execution_delivery", {})
        if not isinstance(lifecycle, dict):
            lifecycle = {}
        if not isinstance(snapshot, dict):
            snapshot = {}
        if lifecycle.get("process_started") is not True:
            continue
        ready = (
            run["status"] in {"cancelled", "failed", "timed_out"}
            and lifecycle.get("generation") == run["execution_generation"]
            and lifecycle.get("termination_confirmed") is True
            and lifecycle.get("cleanup_confirmed") is True
            and snapshot.get("session_id") == run["session_id"]
            and bool(run["session_id"])
            and not any(context.get(field) for field in ACCEPTED_RESULT_CONTEXT_FIELDS)
        )
        routes[run["id"]] = {
            "allowed": ready,
            "reason": None if ready else "execution_continuation_unavailable",
            "retry_at": None,
            "source_task_run_id": run["id"],
            "task_type": run["task_type"],
            "intent_id": run["intent_id"],
            "dispatcher_id": context.get("dispatcher_id"),
            "worker_name": run["worker_name"],
            "container_id": snapshot.get("container_id"),
            "continuation": True,
        }
    return routes


def _fresh_reason_decision_digest(conn, project, runs):
    """仅已结算的受控完成拒绝可因权威输入变化开启新决策，不重置普通失败链。"""
    if not runs or project["assessment_protocol"] != GOAL_DRIVEN_PROTOCOL:
        return None
    source = runs[-1]
    context = parse_scenario_snapshot(source["runtime_context_json"])
    rejection = context.get("completion_rejection")
    delivery = context.get("execution_delivery", {})
    binding = delivery.get("binding", {}) if isinstance(delivery, dict) else {}
    if (
        source["status"] != "failed"
        or source["error_type"] not in {"business_acceptance", "stale_business_state"}
        or not isinstance(rejection, dict)
        or not isinstance(binding, dict)
        or not isinstance(rejection.get("decision_input_digest"), str)
        or rejection["decision_input_digest"] != binding.get("decision_input_digest")
    ):
        return None
    for run in runs:
        prior = parse_scenario_snapshot(run["runtime_context_json"])
        if (
            run["status"] not in {"failed", "timed_out", "cancelled"}
            or _is_delivery_execution(run)
            or prior.get("continuation")
            or any(prior.get(key) for key in ACCEPTED_RESULT_CONTEXT_FIELDS)
            or conn.execute(
                "SELECT 1 FROM facts WHERE project_id = ? AND task_run_id = ? LIMIT 1",
                (project["id"], run["id"]),
            ).fetchone() is not None
            or conn.execute(
                "SELECT 1 FROM task_run_candidates AS c WHERE c.project_id = ? AND c.task_run_id = ? "
                "AND NOT EXISTS (SELECT 1 FROM task_run_candidate_outcomes AS o "
                "WHERE o.project_id = c.project_id AND o.candidate_id = c.id) LIMIT 1",
                (project["id"], run["id"]),
            ).fetchone() is not None
        ):
            return None
    # 终态执行及新事实生产者须完成物理结算；未交付结果的 Explore 仍可与 Reason 并行。
    for producer in conn.execute(
        "SELECT r.* FROM task_runs AS r WHERE r.project_id = ? AND r.task_type <> 'report' "
        "AND (r.status NOT IN ('queued', 'running') OR EXISTS "
        "(SELECT 1 FROM facts AS f WHERE f.project_id = r.project_id AND f.task_run_id = r.id))",
        (project["id"],),
    ):
        lifecycle = parse_scenario_snapshot(producer["runtime_context_json"]).get("execution_lifecycle", {})
        if (
            producer["status"] in {"queued", "running"}
            or not isinstance(lifecycle, dict)
            or lifecycle.get("generation") != producer["execution_generation"]
            or lifecycle.get("termination_confirmed") is not True
            or lifecycle.get("cleanup_confirmed") is not True
        ):
            return None
    from cairn.assessment_contracts import goal_driven_decision_input_digest
    from .project_completion import _goal_driven_completion_detail

    digest = goal_driven_decision_input_digest(_goal_driven_completion_detail(conn, project))
    boundary = context.get("reason_decision_boundary", {})
    # fresh 准入之后没有新业务输入时，旧快照过时也不能形成同输入的循环调用。
    if digest == rejection["decision_input_digest"] or digest == boundary.get("decision_input_digest"):
        return None
    return digest


def _current_execution_chain(conn, project, task_type, runs, *, allow_new_decision=True):
    """成功或 Server 已授予的新决策结束旧链；未决拒绝仍按原链阻断。"""
    if task_type == "reason":
        for index in range(len(runs) - 1, -1, -1):
            if runs[index]["status"] == "succeeded":
                runs = runs[index + 1:]
                break
            if parse_scenario_snapshot(runs[index]["runtime_context_json"]).get("reason_decision_boundary"):
                runs = runs[index:]
                break
        if allow_new_decision and _fresh_reason_decision_digest(conn, project, runs) is not None:
            return []
    return runs


def _is_delivery_execution(run):
    context = parse_scenario_snapshot(run["runtime_context_json"])
    return run["error_type"] in DELIVERY_RETRY_ERROR_TYPES or bool(context.get("delivery_retry"))


def _delivery_predecessor_matches(runs, run):
    """续交付只能引用当前链内真实失败来源，并完整沿用其冻结归属与输入。"""
    context = parse_scenario_snapshot(run["runtime_context_json"])
    retry = context.get("delivery_retry")
    if retry is None:
        return True
    if not isinstance(retry, dict) or set(retry) != {"source_task_run_id"}:
        return False
    for source in runs:
        if source["id"] == run["id"]:
            break
        if source["id"] != retry["source_task_run_id"]:
            continue
        previous = parse_scenario_snapshot(source["runtime_context_json"])
        return bool(
            source["status"] in {"failed", "timed_out"}
            and _is_delivery_execution(source)
            and all(source[key] == run[key] for key in (
                "project_id", "execution_generation", "task_type", "intent_id",
                "assessment_attempt_id", "worker_name", "session_id",
            ))
            and previous.get("dispatcher_id") == context.get("dispatcher_id")
            and isinstance(context.get("execution_delivery"), dict)
            and previous.get("execution_delivery") == context["execution_delivery"]
        )
    return False


def _logical_executions(conn, project, task_type, intent_id, report_generation_token=None, *, allow_new_decision=True):
    phase = parse_scenario_snapshot(project["assessment_progress_json"]).get("phase")
    runs = []
    for run in conn.execute(
        """SELECT * FROM task_runs WHERE project_id = ? AND execution_generation = ?
           AND task_type = ? AND pause_interrupted = 0
           ORDER BY COALESCE(started_at, ended_at), LENGTH(id), id""",
        (project["id"], project["execution_generation"], task_type),
    ):
        if run["intent_id"] != intent_id:
            continue
        context = parse_scenario_snapshot(run["runtime_context_json"])
        if task_type == "report":
            claim = context.get("report_claim", {})
            if not isinstance(claim, dict) or claim.get("generation_token") != report_generation_token:
                continue
        elif "assessment_phase" in context and context["assessment_phase"] != phase:
            continue
        runs.append(run)
    return _current_execution_chain(conn, project, task_type, runs, allow_new_decision=allow_new_decision)


def _delivery_source_usable(project, run, now):
    context = parse_scenario_snapshot(run["runtime_context_json"])
    snapshot = context.get("execution_delivery")
    lifecycle = context.get("execution_lifecycle", {})
    if not isinstance(snapshot, dict) or not isinstance(lifecycle, dict):
        return False
    deadline = parse_utc_timestamp(snapshot.get("deadline_at"))
    # Report 在探索截止后正常 finalization，只受原报告执行 deadline 约束。
    project_deadline = parse_utc_timestamp(project["explore_deadline"]) if run["task_type"] != "report" else None
    current = parse_utc_timestamp(now)
    # cleanup 证明不等于 session 保留；本地原 owner 还须验证原容器及 manifest。
    return bool(
        run["status"] in {"failed", "timed_out"}
        and not run["pause_requested"]
        and type(lifecycle.get("generation")) is int
        and lifecycle["generation"] == project["execution_generation"]
        and lifecycle.get("termination_confirmed") is True
        and context.get("dispatcher_id")
        and run["session_id"] and snapshot.get("session_id") == run["session_id"]
        and deadline and current and deadline > current
        and (not project_deadline or project_deadline > current)
        and not any(context.get(key) for key in (
            "accepted_result_digest", "accepted_fact_id", "accepted_completion_digest", "accepted_intent_submissions",
        ))
    )


def _pause_continuation_source(conn, project, body, assessment_attempt_id):
    if body.task_type not in {"bootstrap", "explore", "reason"} or project["execution_generation"] == 0:
        return None
    intent_filter = "intent_id IS NULL" if body.intent_id is None else "intent_id = ?"
    params = (project["id"], body.task_type, project["execution_generation"])
    if body.intent_id is not None:
        params += (body.intent_id,)
    newest = conn.execute(
        f"""SELECT * FROM task_runs
            WHERE project_id = ? AND task_type = ? AND execution_generation <= ?
              AND {intent_filter}
            ORDER BY execution_generation DESC, LENGTH(id) DESC, id DESC LIMIT 1""",
        params,
    ).fetchone()
    if newest is None or not newest["pause_interrupted"]:
        return None
    source = _started_pause_source(conn, project, newest)
    if source is None:
        return None
    context = parse_scenario_snapshot(source["runtime_context_json"])
    lifecycle = context.get("execution_lifecycle")
    snapshot = context.get("execution_delivery")
    if not isinstance(lifecycle, dict) or (
        lifecycle.get("generation") != source["execution_generation"]
        or lifecycle.get("termination_confirmed") is not True
        or lifecycle.get("cleanup_confirmed") is not True
    ):
        raise HTTPException(409, "execution_continuation_not_confirmed")
    if source["status"] not in {"cancelled", "failed", "timed_out"} or any(
        context.get(key) for key in ACCEPTED_RESULT_CONTEXT_FIELDS
    ):
        raise HTTPException(409, "execution_continuation_source_invalid")
    if source["assessment_attempt_id"] != assessment_attempt_id:
        raise HTTPException(409, "execution_continuation_source_mismatch")
    if lifecycle.get("process_started") is not True:
        # 未启动的 fresh 执行没有会话；带来源的续作若无法回溯，则必须显式阻断。
        if context.get("continuation") is not None:
            raise HTTPException(409, "execution_continuation_source_invalid")
        return None
    if (context.get("dispatcher_id") != body.runtime_context.get("dispatcher_id")
            or source["worker_name"] != body.worker_name):
        raise HTTPException(409, "execution_continuation_owner_mismatch")
    if not isinstance(snapshot, dict) or snapshot.get("session_id") != source["session_id"] or not source["session_id"]:
        raise HTTPException(409, "execution_continuation_session_missing")
    activation = conn.execute(
        """SELECT container_id, state FROM project_container_activations
           WHERE project_id = ? AND dispatcher_id = ?""",
        (project["id"], context["dispatcher_id"]),
    ).fetchone()
    if activation is None or activation["container_id"] != snapshot.get("container_id") or activation["state"] not in {"stopped", "running"}:
        raise HTTPException(409, "execution_continuation_workspace_unavailable")
    if body.session_id is not None and body.session_id != source["session_id"]:
        raise HTTPException(422, "execution_continuation_is_server_owned")
    return source


def derive_delivery_retry_context(conn, project, body, *, assessment_attempt_id=None):
    """在 admission 的项目写锁内派生原交付或已确认暂停的续作来源。"""
    token = body.runtime_context.get("report_claim", {}).get("generation_token") if body.task_type == "report" else None
    runs = _logical_executions(conn, project, body.task_type, body.intent_id, token, allow_new_decision=False)
    if body.task_type == "reason":
        digest = _fresh_reason_decision_digest(conn, project, runs)
        if digest is not None:
            if body.session_id is not None and conn.execute(
                "SELECT 1 FROM task_runs WHERE project_id = ? AND session_id = ? LIMIT 1",
                (project["id"], body.session_id),
            ).fetchone():
                raise HTTPException(409, "execution_delivery_session_already_bound")
            return {"reason_decision_boundary": {
                "source_task_run_id": runs[-1]["id"], "decision_input_digest": digest,
            }}
    if not runs:
        source = _pause_continuation_source(conn, project, body, assessment_attempt_id)
        if source is None:
            if body.session_id is not None and conn.execute(
                "SELECT 1 FROM task_runs WHERE project_id = ? AND session_id = ? LIMIT 1",
                (project["id"], body.session_id),
            ).fetchone():
                raise HTTPException(409, "execution_delivery_session_already_bound")
            return {}
        snapshot = parse_scenario_snapshot(source["runtime_context_json"])["execution_delivery"]
        return {"continuation": {
            "source_task_run_id": source["id"],
            "execution_generation": project["execution_generation"],
            "container_id": snapshot["container_id"],
        }}
    source = runs[-1]
    context = parse_scenario_snapshot(source["runtime_context_json"])
    if any(_is_delivery_execution(run) for run in runs) and any(run["status"] in {"queued", "running"} for run in runs):
        raise HTTPException(409, "assessment_execution_not_quiescent")
    if not _is_delivery_execution(source):
        if context.get("continuation"):
            original = _prestart_continuation_source(conn, project, source)
            if original is None:
                raise HTTPException(409, "execution_continuation_unavailable")
            require_execution_retry_eligible(conn, project, body.task_type, intent_id=body.intent_id, report_generation_token=token)
            if (source["assessment_attempt_id"] != assessment_attempt_id
                    or context.get("dispatcher_id") != body.runtime_context.get("dispatcher_id")
                    or source["worker_name"] != body.worker_name):
                raise HTTPException(409, "execution_continuation_owner_mismatch")
            if body.session_id is not None and body.session_id != original["session_id"]:
                raise HTTPException(422, "execution_continuation_is_server_owned")
            return {"continuation": {
                "source_task_run_id": original["id"], "execution_generation": project["execution_generation"],
                "container_id": context["continuation"]["container_id"],
            }}
        if body.session_id is not None and conn.execute(
            "SELECT 1 FROM task_runs WHERE project_id = ? AND session_id = ? LIMIT 1",
            (project["id"], body.session_id),
        ).fetchone():
            # fresh 执行不能把历史 session 冒充新会话；delivery 才允许原会话续交付。
            raise HTTPException(409, "execution_delivery_session_already_bound")
        return {}
    require_execution_retry_eligible(conn, project, body.task_type, intent_id=body.intent_id, report_generation_token=token)
    if source["assessment_attempt_id"] != assessment_attempt_id:
        raise HTTPException(409, "execution_delivery_source_mismatch")
    if context.get("dispatcher_id") != body.runtime_context.get("dispatcher_id") or source["worker_name"] != body.worker_name:
        raise HTTPException(409, "execution_delivery_owner_mismatch")
    if body.session_id is not None and body.session_id != source["session_id"]:
        raise HTTPException(422, "execution_delivery_is_server_owned")
    if any(run["status"] in {"queued", "running"} for run in runs):
        raise HTTPException(409, "assessment_execution_not_quiescent")
    if body.intent_id is not None:
        intent = conn.execute("SELECT to_fact_id, concluded_at FROM intents WHERE project_id = ? AND id = ?", (project["id"], body.intent_id)).fetchone()
        if intent is None or intent["to_fact_id"] or intent["concluded_at"]:
            raise HTTPException(409, "execution_retry_blocked")
    return {"delivery_retry": {"source_task_run_id": source["id"]}, "execution_delivery": context["execution_delivery"]}

RUNTIME_RETRYABLE_ERROR_TYPES = {
    "api_unreachable",
    "conclude_timeout",
    "container_failed",
    "healthcheck_timeout",
    "heartbeat_lost",
    "network_blocked",
    "worker_timeout",
    "worker_unhealthy",
    "worker_oom",
}

# 只对稳定的 provider/transport 分类放宽上限；candidate 与 session 失败不能盲目重启。
EXTENDED_PROJECT_RETRYABLE_ERROR_TYPES = {
    "provider_error", "provider_rate_limit", "transport_error",
}
EXTENDED_EXPLORE_RETRYABLE_ERROR_TYPES = {
    "provider_error", "provider_rate_limit",
}
PROJECT_RETRYABLE_ERROR_TYPES = RUNTIME_RETRYABLE_ERROR_TYPES | EXTENDED_PROJECT_RETRYABLE_ERROR_TYPES | {
    "report_packet_read_failed",
}
REPORT_RETRYABLE_ERROR_TYPES = PROJECT_RETRYABLE_ERROR_TYPES | {"candidate_spool_unavailable"}


def attempt_has_accepted_result(conn, project_id: str, attempt_id: str) -> bool:
    """只认 Server 接受回执与 Fact；candidate 捕获和校验不是业务接受。"""
    if conn.execute(
        "SELECT 1 FROM facts AS f JOIN task_runs AS r "
        "ON f.project_id = r.project_id AND f.task_run_id = r.id "
        "WHERE r.project_id = ? AND r.assessment_attempt_id = ? LIMIT 1",
        (project_id, attempt_id),
    ).fetchone() is not None:
        return True
    for row in conn.execute(
        "SELECT runtime_context_json FROM task_runs "
        "WHERE project_id = ? AND assessment_attempt_id = ?",
        (project_id, attempt_id),
    ):
        context = parse_scenario_snapshot(row["runtime_context_json"])
        if any(context.get(key) for key in ACCEPTED_RESULT_CONTEXT_FIELDS):
            return True
    return False


def execution_retry_eligibility(
    conn, project, task_type: str, *, intent_id: str | None = None,
    report_generation_token: str | None = None, now: str | None = None, executions=None,
    max_runtime_retries: int | None = None, ignore_cooldown: bool = False,
    check_delivery_source: bool = True,
) -> dict:
    """以同代持久失败统一裁决准入、路由和 Explore 收尾；收尾不受暂时冷却影响。"""
    runs = (_current_execution_chain(conn, project, task_type, executions) if executions is not None
            else _logical_executions(conn, project, task_type, intent_id, report_generation_token))
    if task_type == "explore" and max_runtime_retries is None and intent_id is not None:
        attempt = conn.execute(
            "SELECT id, max_runtime_retries FROM assessment_attempts "
            "WHERE project_id = ? AND intent_id = ? AND status = 'active'",
            (project["id"], intent_id),
        ).fetchone()
        if attempt is not None:
            if attempt_has_accepted_result(conn, project["id"], attempt["id"]):
                return {"allowed": False, "reason": "execution_retry_blocked", "retry_at": None}
            max_runtime_retries = int(attempt["max_runtime_retries"])
    extended = (
        EXTENDED_EXPLORE_RETRYABLE_ERROR_TYPES if task_type == "explore"
        else EXTENDED_PROJECT_RETRYABLE_ERROR_TYPES if task_type in {"bootstrap", "reason"}
        else set()
    )
    retryable = (
        RUNTIME_RETRYABLE_ERROR_TYPES | extended if task_type == "explore"
        else REPORT_RETRYABLE_ERROR_TYPES if task_type == "report"
        else PROJECT_RETRYABLE_ERROR_TYPES
    )
    failures = []
    extended_only = task_type != "report"
    delivery_chain = False
    for run in runs:
        if run["status"] not in {"failed", "timed_out"} or run["error_type"] == "cancelled":
            continue
        context = parse_scenario_snapshot(run["runtime_context_json"])
        if any(context.get(key) for key in ACCEPTED_RESULT_CONTEXT_FIELDS):
            return {"allowed": False, "reason": "execution_retry_blocked", "retry_at": None}
        delivery_error = run["error_type"] in DELIVERY_RETRY_ERROR_TYPES
        delivery_chain = delivery_chain or delivery_error or bool(context.get("delivery_retry"))
        if context.get("continuation") and not delivery_error:
            if delivery_chain or _prestart_continuation_source(conn, project, run) is None:
                return {"allowed": False, "reason": "execution_continuation_unavailable", "retry_at": None}
            extended_only = False
            failures.append(run)
            continue
        # 交付模式不随服务故障切回 fresh；瞬态错误只能沿真实冻结来源使用原额度。
        if delivery_chain:
            if not delivery_error and (
                run["error_type"] not in EXTENDED_PROJECT_RETRYABLE_ERROR_TYPES
                or not context.get("delivery_retry")
            ):
                return {"allowed": False, "reason": "execution_retry_blocked", "retry_at": None}
            if not _delivery_predecessor_matches(runs, run):
                return {"allowed": False, "reason": "execution_retry_blocked", "retry_at": None}
            if check_delivery_source and not _delivery_source_usable(project, run, now or utcnow()):
                return {"allowed": False, "reason": "execution_delivery_unavailable", "retry_at": None}
            if not isinstance(context.get("execution_delivery"), dict):
                return {"allowed": False, "reason": "execution_delivery_unavailable", "retry_at": None}
            extended_only = False
        elif run["error_type"] not in retryable:
            return {"allowed": False, "reason": "execution_retry_blocked", "retry_at": None}
        extended_only = extended_only and run["error_type"] in extended
        failures.append(run)
    limit = EXTENDED_MAX_RUNTIME_RETRIES if extended_only else DEFAULT_MAX_RUNTIME_RETRIES
    if max_runtime_retries is not None:
        limit = min(limit, max_runtime_retries)
    if len(failures) > limit:
        return {"allowed": False, "reason": "execution_retry_exhausted", "retry_at": None}
    if failures and not ignore_cooldown:
        if task_type == "report":
            # 报告沿用 finalization 已持久化的退避窗口，不再计算第二份报告策略。
            report = conn.execute("SELECT next_attempt_at FROM assessment_report_finalizations WHERE project_id = ? AND generation_token = ?", (project["id"], report_generation_token)).fetchone()
            retry_at = parse_utc_timestamp(report["next_attempt_at"]) if report else None
        else:
            ended = [parse_utc_timestamp(run["ended_at"]) for run in failures]
            if any(value is None for value in ended):
                return {"allowed": False, "reason": "execution_retry_blocked", "retry_at": None}
            retry_at = max(ended) + timedelta(
                seconds=min(MAX_RETRY_BACKOFF_SECONDS, 5 * 2 ** (len(failures) - 1))
            )
        if retry_at and parse_utc_timestamp(now or utcnow()) < retry_at:
            return {"allowed": False, "reason": "execution_retry_cooldown", "retry_at": retry_at.strftime("%Y-%m-%dT%H:%M:%SZ")}
    return {"allowed": True, "reason": None, "retry_at": None}


def require_execution_retry_eligible(conn, project, task_type: str, **kwargs) -> None:
    eligibility = execution_retry_eligibility(conn, project, task_type, **kwargs)
    if not eligibility["allowed"]:
        raise HTTPException(409, eligibility["reason"])


def close_assessment_attempt_for_task_run(
    conn: sqlite3.Connection,
    project_id: str,
    intent_id: str,
    task_run_id: str | None,
    *,
    outcome: str,
    require_linked_task_run: bool = False,
) -> None:
    """结构化结论与逻辑评估尝试在同一事务内收敛。"""
    if task_run_id is None:
        active = conn.execute(
            """
            SELECT 1 FROM assessment_attempts
            WHERE project_id = ? AND intent_id = ? AND status = 'active'
            LIMIT 1
            """,
            (project_id, intent_id),
        ).fetchone()
        if active is not None:
            raise HTTPException(422, "assessment_task_run_required")
        return
    task_run = conn.execute(
        """
        SELECT intent_id, assessment_attempt_id
        FROM task_runs
        WHERE id = ? AND project_id = ?
        """,
        (task_run_id, project_id),
    ).fetchone()
    if task_run is None:
        raise HTTPException(404, f"Task run {task_run_id} not found")
    assessment_attempt_id = task_run["assessment_attempt_id"]
    if task_run["intent_id"] != intent_id and (
        assessment_attempt_id is not None or require_linked_task_run
    ):
        raise HTTPException(422, "task_run_id does not belong to the concluded intent")
    if assessment_attempt_id is None:
        # v10 之前的 v1 项目没有逻辑 attempt 关联，保持只读兼容。
        return
    current_execution = conn.execute(
        """
        SELECT id FROM task_runs
        WHERE project_id = ? AND assessment_attempt_id = ?
        ORDER BY COALESCE(started_at, ended_at) DESC, LENGTH(id) DESC, id DESC
        LIMIT 1
        """,
        (project_id, assessment_attempt_id),
    ).fetchone()
    if current_execution is None or current_execution["id"] != task_run_id:
        raise HTTPException(409, "assessment_execution_is_not_current")
    closed = conn.execute(
        """
        UPDATE assessment_attempts
        SET status = 'completed', ended_at = ?, outcome = ?
        WHERE id = ? AND project_id = ? AND intent_id = ? AND status = 'active'
        RETURNING id
        """,
        (utcnow(), outcome, assessment_attempt_id, project_id, intent_id),
    ).fetchone()
    if closed is None:
        raise HTTPException(409, "assessment_attempt_is_not_active")


def finalize_non_retryable_assessment_execution(
    conn: sqlite3.Connection,
    project_id: str,
    assessment_attempt_id: str | None,
    *,
    task_run_id: str,
    status: str,
    error_type: str | None,
) -> None:
    """仅由当前 physical execution 结束逻辑 attempt。"""
    if assessment_attempt_id is None or status not in (
        "failed",
        "timed_out",
        "cancelled",
        "succeeded",
    ):
        return
    if status == "succeeded":
        # 只有 conclude 已关闭 attempt 才是成功；进程退出成功不等于接受业务结果。
        error_type = "assessment_result_missing"
    current_execution = conn.execute(
        """
        SELECT id FROM task_runs
        WHERE project_id = ? AND assessment_attempt_id = ?
        ORDER BY COALESCE(started_at, ended_at) DESC, LENGTH(id) DESC, id DESC
        LIMIT 1
        """,
        (project_id, assessment_attempt_id),
    ).fetchone()
    if current_execution is None or current_execution["id"] != task_run_id:
        # 旧 execution 的迟到状态更新只修正自身审计记录，不能关闭当前 retry。
        return
    from .assessment_policy import policy_from_project_row
    from .ids import get_project_or_404

    project = get_project_or_404(conn, project_id)
    execution = conn.execute("SELECT pause_interrupted FROM task_runs WHERE project_id = ? AND id = ?", (project_id, task_run_id)).fetchone()
    if execution is not None and execution["pause_interrupted"] and project["status"] != "stopped":
        # 暂停归属只能来自服务端；取消本次物理执行不结束逻辑义务。
        return
    policy = policy_from_project_row(project)
    retry_allowed = project["status"] == "active" and (
        not policy.explore_deadline or utcnow() < policy.explore_deadline
    )
    goal_driven = policy.protocol == GOAL_DRIVEN_PROTOCOL
    if not retry_allowed and (policy.phased or goal_driven):
        error_type = (
            project["coverage_stop_reason"]
            if policy.phased
            else None
        ) or (
            "deadline_exceeded" if policy.explore_deadline and utcnow() >= policy.explore_deadline
            else "assessment_stopped"
        )
    if retry_allowed and status in {"failed", "timed_out"}:
        attempt = conn.execute(
            "SELECT max_runtime_retries FROM assessment_attempts "
            "WHERE id = ? AND project_id = ? AND status = 'active'",
            (assessment_attempt_id, project_id),
        ).fetchone()
        if attempt is None or attempt_has_accepted_result(conn, project_id, assessment_attempt_id):
            return
        execution = conn.execute(
            "SELECT task_type, intent_id FROM task_runs WHERE project_id = ? AND id = ?",
            (project_id, task_run_id),
        ).fetchone()
        if execution is not None:
            runs = conn.execute(
                "SELECT * FROM task_runs WHERE project_id = ? AND assessment_attempt_id = ? "
                "AND execution_generation = ? AND pause_interrupted = 0 "
                "ORDER BY COALESCE(started_at, ended_at), LENGTH(id), id",
                (project_id, assessment_attempt_id, project["execution_generation"]),
            ).fetchall()
            eligibility = execution_retry_eligibility(
                conn, project, execution["task_type"], intent_id=execution["intent_id"],
                executions=runs, max_runtime_retries=int(attempt["max_runtime_retries"]),
                ignore_cooldown=True, check_delivery_source=False,
            )
            if eligibility["allowed"]:
                return
            if eligibility["reason"] == "execution_retry_exhausted":
                error_type = f"runtime_retry_exhausted:{error_type}"
    closed = conn.execute(
        """
        UPDATE assessment_attempts
        SET status = 'failed', ended_at = ?, outcome = ?
        WHERE id = ? AND project_id = ? AND status = 'active'
        """,
        (utcnow(), error_type or status, assessment_attempt_id, project_id),
    )
    if closed.rowcount:
        if policy.phased or goal_driven:
            conn.execute(
                """UPDATE intents SET concluded_at = ?, conclusion = 'failed',
                       conclusion_reason = ?, worker = NULL
                   WHERE project_id = ? AND concluded_at IS NULL AND id = (
                       SELECT intent_id FROM assessment_attempts WHERE project_id = ? AND id = ?
                   )""",
                (utcnow(), error_type or status, project_id, project_id, assessment_attempt_id),
            )
