"""首页定向协议测试：只用查询替身，不连接或写入数据库，不启动服务。"""

from contextlib import nullcontext
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from cairn.server.auth import RequestPrincipal, protect_cookie_request, verification_project_access_clause
from cairn.server.routers import overview as router_module
from cairn.server.services import overview as overview_service
from cairn.server.services.overview import get_overview
from cairn.server.services.vulnerabilities import vulnerability_read_scope, vulnerabilities_summary


_NOW = "2026-10-27T00:00:00Z"
_USER = RequestPrincipal(kind="user", subject_id="u1", tenant_id="t1", role="user")
_ADMIN = RequestPrincipal(kind="user", subject_id="admin", tenant_id="ta", role="super_admin")


@pytest.fixture(autouse=True)
def consumption_projection(monkeypatch):
    # 消费 SQL 与权限在隔离 PostgreSQL 接缝测试验证，此处只核对工作台复用同一来源。
    projection = Mock(return_value={'coverage': 'recorded_only'})
    monkeypatch.setattr(overview_service, 'overview_consumption', projection)
    return projection


def _connection(*results):
    conn = Mock()
    conn.execute.side_effect = [Mock(fetchall=Mock(return_value=rows)) for rows in results]
    return conn


def _populated_connection():
    return _connection(
        [{"status": "active", "c": 7}, {"status": "pending", "c": 2}, {"status": "paused", "c": 1}],
        [{"id": f"p{i}", "title": f"合成项目{i}", "status": "active", "created_at": _NOW} for i in range(6)],
        [{"severity": "high", "status": "new", "c": 5},
         {"severity": "high", "status": "remediated", "c": 2},
         {"severity": "info", "status": "dismissed", "c": 1}],
        [{"id": f"v{i}", "project_id": "p0", "project_title": "合成项目0", "title": f"合成漏洞{i}",
          "severity": "high", "status": "new", "created_at": _NOW} for i in range(5)],
    )


@pytest.mark.parametrize("principal", [_USER, _ADMIN])
def test_overview_uses_shared_scope_bounded_projection_and_no_business_writes(principal, consumption_projection):
    conn = _populated_connection()
    result = get_overview(conn, principal)
    consumption_projection.assert_called_once_with(conn, principal)
    assert result.consumption == consumption_projection.return_value
    assert result.projects.total == 10
    assert result.projects.by_status == {"active": 7, "pending": 2, "paused": 1}
    assert result.vulnerabilities.total == 8
    assert result.vulnerabilities.by_severity == {"high": 7, "info": 1}
    assert result.vulnerabilities.by_status == {"new": 5, "remediated": 2, "dismissed": 1}
    assert len(result.project_previews) == 6
    assert len(result.vulnerability_previews) == 5
    assert set(result.project_previews[0].model_dump()) == {"id", "title", "status", "created_at"}
    assert set(result.vulnerability_previews[0].model_dump()) == {
        "id", "title", "project_id", "project_title", "severity", "status", "created_at",
    }
    calls = conn.execute.call_args_list
    assert len(calls) == 4
    assert all(call.args[0].strip().startswith("SELECT") for call in calls)
    assert all("p.*" not in call.args[0] and "v.*" not in call.args[0] for call in calls)
    for call in calls[:2]:
        assert "NOT EXISTS (SELECT 1 FROM verification_requests vr WHERE vr.project_id = p.id)" in call.args[0]
        assert call.args[1] == (() if principal.is_super_admin else (principal.tenant_id,))
    assert "LIMIT 6" in calls[1].args[0]
    assert "WHEN 'active' THEN 0 WHEN 'pending' THEN 1" in calls[1].args[0]
    assert "WHEN 'paused' THEN 2 ELSE 3 END, p.created_at DESC, p.id DESC" in calls[1].args[0]
    scope, params = vulnerability_read_scope(
        tenant_id=None if principal.is_super_admin else principal.tenant_id, principal=principal,
    )
    for call in calls[2:]:
        assert f"WHERE {scope}" in call.args[0]
        assert call.args[1] == params
    assert "LIMIT 5" in calls[3].args[0]
    assert "ORDER BY v.created_at DESC, v.id DESC, v.project_id DESC" in calls[3].args[0]
    assert "updated_at" not in calls[3].args[0]
    conn.commit.assert_not_called()


def test_shared_scope_preserves_caller_tenant_and_hidden_purge_boundaries():
    for principal in (_USER, _ADMIN):
        tenant_id = None if principal.is_super_admin else principal.tenant_id
        scope, params = vulnerability_read_scope(tenant_id=tenant_id, principal=principal)
        access, access_params = verification_project_access_clause(principal)
        assert access in scope
        assert "vr.purge_state IS NOT NULL" in scope
        assert "vr.deleted_at IS NOT NULL" in scope
        if principal.is_super_admin:
            assert params == access_params == ()
            assert "p.tenant_id = ?" not in scope
        else:
            assert "vr.caller_type = ? AND vr.caller_id = ?" in scope
            assert "p.tenant_id = ?" in scope
            assert params == ("t1", "user", "u1", "t1")
    other = RequestPrincipal(kind="user", subject_id="u2", tenant_id="t1", role="user")
    assert vulnerability_read_scope(tenant_id="t1", principal=other)[1] == ("t1", "user", "u2", "t1")


def test_empty_is_real_zero_and_existing_summary_reuses_the_same_count_semantics():
    result = get_overview(_connection([], [], [], []), _USER)
    assert result.projects.total == result.vulnerabilities.total == 0
    assert result.project_previews == result.vulnerability_previews == []
    conn = _connection(
        [{"severity": "high", "status": "new", "c": 2}, {"severity": "high", "status": "confirmed", "c": 3}],
        [{"project_id": "p1", "title": "合成项目", "c": 5}],
    )
    assert vulnerabilities_summary(conn, tenant_id="t1", principal=_USER) == {
        "total": 5, "by_severity": {"high": 5}, "by_status": {"new": 2, "confirmed": 3},
        "per_project": [{"project_id": "p1", "title": "合成项目", "count": 5}],
    }


@pytest.mark.parametrize("principal", [_USER, _ADMIN])
def test_route_preserves_cookie_scope_and_admin_read_audit(monkeypatch, principal):
    app = FastAPI()
    app.include_router(router_module.router)
    app.dependency_overrides[protect_cookie_request] = lambda: principal
    conn = _populated_connection()
    monkeypatch.setattr(router_module, "get_conn", lambda: nullcontext(conn))
    audit = Mock()
    monkeypatch.setattr(router_module, "write_security_audit", audit)
    response = TestClient(app).get("/overview")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["vulnerabilities"]["total"] == 8
    if principal.is_super_admin:
        audit.assert_called_once_with(
            conn, actor_type="user", actor_id="admin", tenant_id="ta", action="admin.overview.read",
            resource_type="admin_read_model", resource_id=None, outcome="success",
        )
    else:
        audit.assert_not_called()


def test_route_rejects_missing_cookie_and_machine_bearer_without_reading_business_data(monkeypatch):
    app = FastAPI()
    app.include_router(router_module.router)
    monkeypatch.setattr("cairn.server.auth.resolve_session", lambda _cookie: None)
    connection = Mock(side_effect=AssertionError("未授权请求不得读取业务数据"))
    monkeypatch.setattr(router_module, "get_conn", connection)
    client = TestClient(app)
    assert client.get("/overview").status_code == 401
    assert client.get("/overview", headers={"Authorization": "Bearer synthetic-machine"}).status_code == 401
    connection.assert_not_called()
