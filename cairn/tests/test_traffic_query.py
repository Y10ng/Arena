"""S3 摘要的隔离 PostgreSQL/API 检查，不连接业务库或执行目标请求。"""

import base64
from copy import deepcopy
import json
import os

import pytest
from fastapi import HTTPException

from cairn.server.schemas.traffic import TrafficSummaryQuery
from pydantic import ValidationError
from cairn.server.traffic_schema import traffic_request_location
from traffic_api_fixture import traffic_api, postgres_instance, empty_postgres_url  # noqa: F401


NOW = "2026-10-22T01:00:00Z"


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode()


def _record(scope, flow_id, *, target=b"/api/Item?q=one%25", status=200, method="GET"):
    message = {"http_version": "HTTP/1.1", "headers_base64": [[_b64(b"X-Private"), _b64(b"not-in-summary")],
                                                               [_b64(b"Host"), _b64(b"Synthetic.Invalid:8080")]],
               "body_bytes": 0, "body_complete": False, "body_sealed": False, "body_revision": None, "body_file": "body.bin"}
    return {"id": flow_id, "scope": scope, "revision": 1, "representation": "structured-http-v1",
            "collector_version": "query-test", "observation_point": "task-network-output",
            "original_destination": None, "connection_id": "connection", "started_at": 1.0,
            "finished_at": None, "state": "receiving", "error": None,
            "request": {**message, "method": method, "host": "Synthetic.Invalid", "port": 8080,
                        "scheme": "http", "target_base64": _b64(target), "authority_base64": ""},
            "response": {**message, "status_code": status, "reason_base64": _b64(b"Synthetic")} if status else None}


def test_location_derivation_and_page_validation():
    request = _record({}, "f")["request"]
    original = deepcopy(request)
    assert traffic_request_location(request) == ("http://Synthetic.Invalid:8080/api/Item?q=one%25", "/api/Item")
    assert request == original
    request["target_base64"] = _b64(b"http://target.invalid/A%2fb?q=secret")
    assert traffic_request_location(request) == ("http://target.invalid/A%2fb?q=secret", "/A%2fb")
    request["target_base64"] = _b64(b"/bad\xff\x00")
    assert traffic_request_location(request)[1] == r"/bad\xff\x00"
    request.update(method="CONNECT", target_base64=_b64(b"target.invalid:443"))
    assert traffic_request_location(request)[1] == "target.invalid:443"
    query = TrafficSummaryQuery(method="get", host="SYNTHETIC")
    assert query.method == "GET" and query.host == "synthetic"
    assert query.page == 1 and query.limit == 50
    for invalid in ({"page": 0}, {"page": -1}, {"page": 1.5}, {"limit": 0}, {"limit": 201}, {"cursor": "obsolete"}):
        with pytest.raises(ValidationError):
            TrafficSummaryQuery(**invalid)


@pytest.mark.skipif(os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1", reason="requires disposable PostgreSQL opt-in")
def test_summary_count_matches_authorized_distinct_flows(empty_postgres_url, tmp_path):
    from cairn.server import db
    from cairn.server.auth import RequestPrincipal
    from cairn.server.schemas.traffic import TrafficSummaryPage
    from cairn.server.services.traffic_query import list_traffic_summaries

    # 仅为管理员页面不能证明的租户隔离、重复引用和深页计数建立一次性合成库。
    db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")
    with db.get_conn() as conn:
        conn.execute("INSERT INTO tenants(id, kind, name, created_at) VALUES ('count-other', 'personal', '隔离', ?)", (NOW,))
        for project, tenant in (("count-a", "tenant_legacy"), ("count-b", "tenant_legacy"), ("count-hidden", "count-other")):
            conn.execute("INSERT INTO projects(id, title, status, created_at, tenant_id) VALUES (?, ?, 'paused', ?, ?)",
                         (project, project, NOW, tenant))
            conn.execute("INSERT INTO task_runs(id, project_id, task_type, worker_name, worker_type, status) "
                         "VALUES ('run', ?, 'explore', 'synthetic', 'pi', 'succeeded')", (project,))
            conn.execute("INSERT INTO http_capture_scopes(id, project_id, task_run_id, generation, container_id, dispatcher_id, created_at) "
                         "VALUES ('scope', ?, 'run', 0, 'synthetic', 'synthetic', ?)", (project, NOW))
            for index in range(3):
                flow_id = f"flow-{index}"
                record = _record({}, flow_id)
                record["request"]["body_bytes"] = 100
                record["response"]["body_bytes"] = 7
                conn.execute("INSERT INTO http_flows(id, project_id, capture_scope_id, revision, record_json, record_sha256, "
                             "state, method, host, status_code, created_cursor, updated_cursor, received_at, updated_at, request_url, request_path) "
                             "VALUES (?, ?, 'scope', 1, ?, ?, 'receiving', 'GET', 'synthetic.invalid', 200, ?, ?, ?, ?, ?, '/api/Item')",
                             (flow_id, project, json.dumps(record), "a" * 64, index + 1, index + 1, NOW, NOW,
                              "http://Synthetic.Invalid:8080/api/Item?q=one%25"))
        evidence = json.dumps([{"id": "a", "kind": "http", "source": "capture", "flow_id": "flow-1"},
                               {"id": "b", "kind": "http", "source": "historical", "flow_id": "flow-1"}])
        conn.execute("INSERT INTO vulnerabilities(id, project_id, title, severity, created_at, updated_at, evidence_items_json) "
                     "VALUES ('v', 'count-a', '合成', 'low', ?, ?, ?)", (NOW, NOW, evidence))
        for observation in ("o1", "o2"):
            conn.execute("INSERT INTO vulnerability_observations(id, project_id, vulnerability_id, severity, confidence, observed_at, evidence_items_json) "
                         "VALUES (?, 'count-a', 'v', 'low', 0.5, ?, ?)", (observation, NOW, evidence))
    principal = RequestPrincipal(kind="user", subject_id="count-reader", tenant_id="tenant_legacy", role="user")

    def read(**filters):
        result = list_traffic_summaries(TrafficSummaryQuery(**filters), principal)
        TrafficSummaryPage.model_validate(result)
        return result

    pages = [read(page=page, limit=2) for page in (1, 2, 3)]
    assert all(result["total"] == 6 and result["total_pages"] == 3 for result in pages)
    keys = [(row["project_id"], row["id"]) for result in pages for row in result["items"]]
    assert keys == sorted(((project, f"flow-{index}") for project in ("count-a", "count-b") for index in range(3)), reverse=True)
    assert read(page=999, limit=2) == pages[-1]
    assert read(host="missing", page=999) == {"items": [], "page": 1, "limit": 50, "total": 0, "total_pages": 0}
    for filters in ({"associated": True}, {"vulnerability_id": "v"},
                    {"observation_id": "o1"}, {"vulnerability_id": "v", "observation_id": "o1", "include_record_evidence": True}):
        result = read(page=999, limit=1, **filters)
        assert result["total"] == result["total_pages"] == result["page"] == 1
        assert result["items"][0]["id"] == "flow-1"
        assert result["items"][0]["associations"]["observation_count"] == 2
    assert read(associated=False)["total"] == 5
    assert read(url="?q=one%25")["total"] == 6
    assert read(path="q=one")["total"] == 0
    assert pages[0]["items"][0]["sync"]["bodies"]["response"]["captured_bytes"] == 7
    with pytest.raises(HTTPException) as denied:
        read(project_id="count-hidden")
    assert denied.value.status_code == 404


@pytest.mark.skipif(os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1", reason="requires disposable PostgreSQL opt-in")
def test_scope_status_filters_terminal_run_beyond_first_page(empty_postgres_url, tmp_path):
    from fastapi import Response
    from cairn.server import db
    from cairn.server.routers.traffic import get_scope_status

    # 只在一次性 PostgreSQL 中写入合成记录，不启动 API、Dispatcher 或业务运行。
    db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")
    status = {"state": "blocked", "code": "upload_failed", "pending": 3, "pending_bytes": 12,
              "control_stopped": False, "test_stopped": True, "collector_stopped": True,
              "upload_complete": False}
    context = {"execution_lifecycle": {"generation": 1, "termination_confirmed": True}}
    with db.get_conn() as conn:
        for project in ("scope-project", "other-project"):
            conn.execute("INSERT INTO projects(id, title, status, created_at, tenant_id, execution_generation) "
                         "VALUES (?, '合成状态查询', 'paused', ?, 'tenant_legacy', 2)", (project, NOW))
        for index in range(102):
            project = "scope-project" if index <= 100 else "other-project"
            run_id = f"run-{index:03d}" if index < 100 else "selected-run"
            conn.execute("INSERT INTO task_runs(id, project_id, task_type, worker_name, worker_type, status, "
                         "runtime_context_json, execution_generation) "
                         "VALUES (?, ?, 'explore', 'synthetic', 'pi', 'failed', ?, 1)",
                         (run_id, project, json.dumps(context)))
            conn.execute("INSERT INTO http_capture_scopes(id, project_id, task_run_id, generation, container_id, "
                         "dispatcher_id, created_at, status_json, status_updated_at) VALUES (?, ?, ?, 1, "
                         "'synthetic-container', 'synthetic-dispatcher', ?, ?, ?)",
                         (f"scope-{index:03d}", project, run_id, NOW, json.dumps(status), NOW))

    def read(project="scope-project", **params):
        response = Response()
        result = get_scope_status(project, response, limit=100, **params)
        assert response.headers["Cache-Control"] == "private, no-store"
        return result

    first = read()
    assert len(first["items"]) == 100 and first["has_more"] is True
    assert first["next_cursor"] == "scope-099"
    assert all(row["task_run_id"] != "selected-run" for row in first["items"])
    selected = read(task_run_id="selected-run")
    assert selected["has_more"] is False and selected["next_cursor"] is None
    assert [row["id"] for row in selected["items"]] == ["scope-100"]
    row = selected["items"][0]
    assert row["project_id"] == "scope-project" and row["generation"] == 1
    assert row["status"] == {**status, "control_stopped": True}
    assert read(task_run_id="selected-run", cursor="scope-100")["items"] == []
    assert read(task_run_id="selected")["items"] == []
    assert read(task_run_id="missing-run")["items"] == []
    assert [row["id"] for row in read(cursor=first["next_cursor"])["items"]] == ["scope-100"]
    with db.get_conn() as conn:
        context["execution_lifecycle"]["generation"] = 2
        conn.execute("UPDATE task_runs SET runtime_context_json = ? WHERE project_id = ? AND id = ?",
                     (json.dumps(context), "scope-project", "selected-run"))
    assert read(task_run_id="selected-run")["items"][0]["status"]["control_stopped"] is False
    with pytest.raises(HTTPException) as failure:
        read("missing-project", task_run_id="selected-run")
    assert failure.value.status_code == 404


@pytest.mark.skipif(os.environ.get("CAIRN_TEST_POSTGRES_BOOTSTRAP") != "1", reason="requires disposable PostgreSQL opt-in")
def test_authorized_summary_pages_filters_and_observation_links(traffic_api):
    api = traffic_api
    conn = api.conn
    p1, p2 = api.project_id, api.other_project_id
    run = api.task_runs[0]
    dispatcher = run["runtime_context"]["dispatcher_id"]
    tenant = conn.execute("SELECT tenant_id FROM projects WHERE id = ?", (p1,)).fetchone()["tenant_id"]
    outsider = conn.execute("SELECT u.id, m.tenant_id FROM users u JOIN tenant_memberships m ON m.user_id = u.id "
                            "WHERE u.login = ?", ("traffic-outsider",)).fetchone()
    p3 = "query-hidden-project"
    conn.execute("INSERT INTO projects(id, title, status, created_at, tenant_id) VALUES (?, ?, 'paused', ?, ?)",
                 (p3, "隔离权限对照", NOW, outsider["tenant_id"]))
    scopes = {}
    for project in (p1, p2, p3):
        if project == p1:
            task_run_id, generation = run["id"], run["execution_generation"]
        else:
            task_run_id, generation = "query-run", 0
            conn.execute("INSERT INTO task_runs(id, project_id, task_type, worker_name, worker_type, status, "
                         "runtime_context_json) VALUES (?, ?, 'explore', 'query', 'pi', 'running', ?)",
                         (task_run_id, project, json.dumps({"dispatcher_id": dispatcher})))
        scopes[project] = {"id": "query-scope", "project_id": project, "task_run_id": task_run_id,
                           "generation": generation, "container_id": "synthetic-container"}
        result = api.machine.register_traffic_scope(project, scopes[project])
        assert result.ok, result.data

    records = {}
    for project in (p1, p2, p3):
        for number in (1, 2, 3):
            flow_id = f"flow-{number}"
            record = _record(scopes[project], flow_id, status=200 if number != 3 else 404,
                             method="GET" if number != 2 else "POST")
            records[project, flow_id] = record
            result = api.machine.sync_traffic_flow(project, "query-scope", record)
            assert result.ok, result.data
    # 合成相同接收时间覆盖完整 tie-breaker；后续重传不得改变该值。
    conn.execute("UPDATE http_flows SET received_at = ?", (NOW,))

    def get(endpoint="/traffic", *, session=None, expected=200, **params):
        response = (session or api.user).get(api.base_url + endpoint, params=params, timeout=15)
        assert response.status_code == expected, response.text
        if expected == 200:
            assert response.headers["Cache-Control"] == "private, no-store"
        return response.json()

    all_rows = get()["items"]
    expected_keys = sorted(((p, f"flow-{n}") for p in (p1, p2) for n in (1, 2, 3)), reverse=True)
    assert [(row["project_id"], row["id"]) for row in all_rows] == expected_keys
    assert len(get(session=api.outsider)["items"]) == 3
    assert get(project_id=p3, expected=404)["detail"] == "Project not found"
    assert get(f"/projects/{p3}/traffic/summary", expected=404)["detail"] == "Project not found"
    assert api.machine._session().get(api.base_url + "/traffic", timeout=15).status_code == 401
    assert all("request" not in row and "record_json" not in row and "headers_base64" not in json.dumps(row) for row in all_rows)
    assert "not-in-summary" not in json.dumps(all_rows)
    project_rows = get(f"/projects/{p1}/traffic/summary")["items"]
    assert project_rows == get(project_id=p1)["items"]
    get(f"/projects/{p1}/traffic/summary", project_id=p2, expected=422)

    first = get(limit=2)
    second = get(limit=2, page=2)
    third = get(limit=2, page=3)
    assert [(r["project_id"], r["id"]) for result in (first, second, third) for r in result["items"]] == expected_keys
    assert set(first) == {"items", "page", "limit", "total", "total_pages"}
    assert [result["page"] for result in (first, second, third)] == [1, 2, 3]
    assert all(result["total"] == 6 and result["total_pages"] == 3 for result in (first, second, third))
    assert get(limit=2, page=99) == third
    empty = get(host="no-matches", page=99)
    assert empty == {"items": [], "page": 1, "limit": 50, "total": 0, "total_pages": 0}
    assert get(limit=2, page=2, session=api.outsider)["total"] == 3
    assert get(f"/projects/{p1}/traffic/summary", limit=2, page=2)["total"] == 3
    get(limit=201, expected=422)
    get(page=0, expected=422)
    get(cursor="obsolete", expected=422)
    for filters, count in (({"host": "SYNTHETIC"}, 6), ({"host": "%"}, 0), ({"method": "post"}, 2),
                           ({"url": "?q=one%25"}, 6), ({"path": "Item"}, 6), ({"path": "q=one"}, 0),
                           ({"path": "item"}, 0), ({"status_family": "4xx"}, 2), ({"status_code": 200}, 4),
                           ({"status_family": "2xx", "status_code": 404}, 0), ({"state": "receiving"}, 6),
                           ({"task_run_id": run["id"]}, 3), ({"flow_id": "flow-1"}, 2)):
        result = get(**filters)
        assert len(result["items"]) == result["total"] == count, filters
    get(status_family="400", expected=422)

    # canonical 补充可关联，但不能冒充 Observation；重复条目不重复计数。
    evidence = [{"id": "one", "kind": "http", "source": "capture", "flow_id": "flow-1"},
                {"id": "two", "kind": "http", "source": "historical", "flow_id": "flow-1"}]
    conn.execute("INSERT INTO vulnerabilities(id, project_id, title, severity, created_at, updated_at, evidence_items_json) "
                 "VALUES ('v1', ?, '合成关联', 'low', ?, ?, ?)", (p1, NOW, NOW, json.dumps(evidence)))
    canonical = get(associated=True)["items"]
    assert len(canonical) == 1
    assert canonical[0]["associations"]["observation_count"] == 0
    assert canonical[0]["associations"]["items"][0]["observation_id"] is None
    for number in range(7):
        conn.execute("INSERT INTO vulnerability_observations(id, project_id, vulnerability_id, severity, confidence, "
                     "observed_at, evidence_items_json) VALUES (?, ?, 'v1', 'low', 0.5, ?, ?)",
                     (f"query-observation-{number}", p1, NOW, json.dumps(evidence)))
    linked = get(associated=True)["items"]
    assert len(linked) == 1 and linked[0]["project_id"] == p1 and linked[0]["id"] == "flow-1"
    relations = linked[0]["associations"]
    assert relations["observation_count"] == 7 and relations["vulnerability_count"] == 1
    assert len(relations["items"]) == 5 and relations["has_more"]
    assert all(item["status"] == "new" and item["vulnerability_id"] == "v1" for item in relations["items"])
    assert len(get(associated=False)["items"]) == 5
    assert get(associated=True, session=api.outsider)["items"] == []
    detail = get(f"/projects/{p1}/traffic/flow-1")
    assert len(detail["observations"]) == 8
    assert len(get(vulnerability_id="v1")["items"]) == 1
    assert len(get(observation_id="query-observation-0")["items"]) == 1
    assert get(vulnerability_id="other", observation_id="query-observation-0")["items"] == []
    assert get(vulnerability_id="v1", observation_id="other", include_record_evidence=True)["items"] == []
    assert len(get(vulnerability_id="v1", observation_id="query-observation-0", include_record_evidence=True)["items"]) == 1
    # 真实人工 PATCH 只变 canonical；可关联尚未封存的同项目索引，不读取正文。
    before = conn.execute("SELECT evidence_items_json FROM vulnerability_observations WHERE project_id=? AND id='query-observation-0'", (p1,)).fetchone()["evidence_items_json"]
    manual = {"id": "manual-flow", "kind": "http", "source": "manual", "flow_id": "flow-2"}
    patch_url = api.base_url + f"/projects/{p1}/vulnerabilities/v1"
    assert api.user.patch(patch_url, json={"evidence_items": [evidence[1], manual]}, timeout=15).status_code == 200
    scoped = get(project_id=p1, vulnerability_id="v1", observation_id="query-observation-0", include_record_evidence=True)["items"]
    assert {row["id"] for row in scoped} == {"flow-1", "flow-2"}
    manual_summary = next(row for row in scoped if row["id"] == "flow-2")["associations"]
    assert manual_summary["observation_count"] == 0 and manual_summary["vulnerability_count"] == 1
    assert get(project_id=p1, observation_id="query-observation-0")["items"][0]["id"] == "flow-1"
    assert len(get(project_id=p1, observation_id="query-observation-0")["items"]) == 1
    assert conn.execute("SELECT evidence_items_json FROM vulnerability_observations WHERE project_id=? AND id='query-observation-0'", (p1,)).fetchone()["evidence_items_json"] == before
    assert api.user.patch(patch_url, json={"evidence_items": [evidence[1], manual, {**manual, "id": "duplicate"}]}, timeout=15).status_code == 422
    assert api.user.patch(patch_url, json={"evidence_items": [evidence[1], {**manual, "flow_id": "not-in-project"}]}, timeout=15).status_code == 422
    assert api.user.patch(patch_url, json={"evidence_items": [evidence[1]]}, timeout=15).status_code == 200
    assert get(project_id=p1, flow_id="flow-2", associated=True)["items"] == []
    assert get(f"/projects/{p1}/traffic/flow-2")["id"] == "flow-2"

    original = records[p1, "flow-1"]
    result = api.machine.sync_traffic_flow(p1, "query-scope", original)
    assert result.ok and result.data["received_at"] == NOW
    # 独立的无响应 flow 覆盖失败/缺侧。
    failed = _record(scopes[p1], "failed-flow", status=None)
    assert api.machine.sync_traffic_flow(p1, "query-scope", failed).ok
    from cairn.server.services.flow_evidence import projection_header
    preview = get(f"/projects/{p1}/traffic/failed-flow/preview/request")
    assert preview["header_text"] == projection_header(failed["request"], "request").decode("utf-8")
    assert preview["stable"] is False and "sha256" not in preview
    get(f"/projects/{p1}/traffic/failed-flow/preview/response", expected=404)
    get(f"/projects/{p1}/traffic/failed-flow/preview/request", session=api.outsider, expected=404)
    failed.update(revision=2, state="failed", error={"code": "transport_error", "message": None})
    failed["request"].update(body_sealed=True, body_revision=2)
    assert api.machine.sync_traffic_flow(p1, "query-scope", failed).ok
    # 源终态但正文尚未封存是 pending，不伪报采集已发布终态。
    pending = get(project_id=p1, flow_id="failed-flow")["items"][0]
    assert pending["source_state"] == "failed" and pending["state"] == "receiving"
    assert pending["sync"]["bodies"]["response"]["status"] == "absent"
    assert get(error_code="transport_error")["items"] == [pending]
    assert get(error_code="collector_stopped")["items"] == []
    assert get(error_code="transport")["items"] == []
    from hashlib import sha256
    sealed = api.machine.seal_traffic_body(p1, "query-scope", "failed-flow", "request",
                                          revision=2, size=0, sha256=sha256(b"").hexdigest())
    assert sealed.ok, sealed.data
    failed_row = get(project_id=p1, state="failed")["items"][0]
    assert failed_row["error_code"] == "transport_error"
    assert failed_row["sync"]["bodies"]["request"]["captured_bytes"] == 0
    assert failed_row["sync"]["bodies"]["request"]["sealed"]
    assert failed_row["received_at"] == pending["received_at"]
    stopped = _record(scopes[p1], "stopped-flow", status=None)
    stopped.update(state="failed", error={"code": "collector_stopped", "message": None})
    stopped["request"].update(body_sealed=True, body_revision=1)
    assert api.machine.sync_traffic_flow(p1, "query-scope", stopped).ok
    assert api.machine.seal_traffic_body(p1, "query-scope", "stopped-flow", "request",
                                        revision=1, size=0, sha256=sha256(b"").hexdigest()).ok
    assert len(get(state="failed")["items"]) == 2
    assert [row["id"] for row in get(state="failed", error_code="transport_error")["items"]] == ["failed-flow"]
    assert [row["id"] for row in get(error_code="collector_stopped")["items"]] == ["stopped-flow"]

    # 显式翻页读取当前集合；原始机器列表仍使用整数项目游标和完整记录。
    current_rows = get()["items"]
    assert get(limit=2, page=2)["items"] == current_rows[2:4]
    legacy = api.machine.list_http_flows(p1, task_run_id=None, limit=2)
    assert legacy.ok and isinstance(legacy.data["next_cursor"], int)
    assert "request" in legacy.data["items"][0] and "update_cursor" in legacy.data
    old = conn.execute("SELECT record_json FROM http_flows WHERE project_id = ? AND id = 'flow-1'", (p1,)).fetchone()
    assert json.loads(old["record_json"]) == original

    # 每次续页重新授权：同 Tenant 的他人复测项目及已隐藏复测不能混入聚合。
    conn.execute("INSERT INTO intents(id, project_id, description, creator, created_at) VALUES ('query-intent', ?, '合成', 'test', ?)", (p2, NOW))
    conn.execute("INSERT INTO project_resources(id, project_id, kind, path, size_bytes, sha256, created_by, created_at) "
                 "VALUES ('query-resource', ?, 'note', 'synthetic.txt', 0, ?, 'test', ?)", (p2, "a" * 64, NOW))
    conn.execute("INSERT INTO verification_requests(id, schema_version, tenant_id, caller_type, caller_id, client_request_id, "
                 "request_digest, mode, project_id, intent_id, report_type, report_resource_id, created_at, updated_at) "
                 "VALUES ('query-verification', 1, ?, 'user', ?, 'query-client', ?, 'retest', ?, 'query-intent', 'text', 'query-resource', ?, ?)",
                 (tenant, outsider["id"], "a" * 64, p2, NOW, NOW))
    assert all(row["project_id"] == p1 for row in get()["items"])
    authorized_page = get(limit=2, page=2)
    assert all(row["project_id"] == p1 for row in authorized_page["items"])
    assert authorized_page["total"] == len(get()["items"])
    get(f"/projects/{p2}/traffic/summary", expected=404)
    reader = conn.execute("SELECT id FROM users WHERE login = 'traffic-reader'").fetchone()["id"]
    conn.execute("UPDATE verification_requests SET caller_id = ?, deleted_at = ? WHERE id = 'query-verification'", (reader, NOW))
    assert all(row["project_id"] == p1 for row in get()["items"])
