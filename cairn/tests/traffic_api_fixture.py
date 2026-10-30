"""S1-A opt-in 实验使用的真实 HTTP/私有 PostgreSQL fixture，不连接部署数据库。"""

from __future__ import annotations

import base64
import json
import secrets
import socket
import threading
import time
from types import SimpleNamespace

import psycopg
from psycopg.rows import dict_row
import pytest
import requests
import uvicorn

from cairn.dispatcher.protocol.client import CairnClient
from cairn.server import db
from cairn.server.app import app
from cairn.server.auth import create_user_with_personal_tenant, ensure_local_runtime_principals
from cairn.server.services.model_credentials import KEYRING_FILE_ENV

# 仅复用本次新建容器和空库的 fixture；不导入带业务清库或认证 mock 的 fixture。
from test_postgres_bootstrap import empty_postgres_url, postgres_instance  # noqa: F401


def _json_request(session: requests.Session, base_url: str, method: str, path: str, *, expected: int, **kwargs):
    response = session.request(method, base_url + path, timeout=15, **kwargs)
    assert response.status_code == expected, f"{method} {path}: HTTP {response.status_code}: {response.text}"
    return response.json()


def _login(base_url: str, login: str, password: str) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers["Origin"] = base_url
    try:
        result = _json_request(
            session, base_url, "POST", "/auth/login", expected=200,
            json={"login": login, "password": password},
        )
        assert result["must_change_password"] is False
        session.headers["X-Cairn-CSRF-Token"] = result["csrf_token"]
        return session
    except BaseException:
        session.close()
        raise


@pytest.fixture
def traffic_api(empty_postgres_url, tmp_path, monkeypatch):
    """返回真实客户端和仅供计数的连接；先停 HTTP，再让上游删除私有空库。"""
    # 所有可写文件均限定在本次临时目录，既有部署密钥和凭据不会被读取。
    keyring = tmp_path / "traffic-keyring.json"
    with keyring.open("x", encoding="utf-8") as output:
        json.dump({"active_version": "test-v1", "keys": {
            "test-v1": base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
        }}, output)
    keyring.chmod(0o600)
    monkeypatch.setenv(KEYRING_FILE_ENV, str(keyring))
    monkeypatch.setenv("CAIRN_PROJECT_RESOURCE_ROOT", str(tmp_path / "project-resources"))
    monkeypatch.setenv("CAIRN_ADVISORY_LIBRARY_ROOT", str(tmp_path / "library-content"))
    monkeypatch.setenv("CAIRN_KNOWLEDGE_ROOT", str(tmp_path / "knowledge"))
    monkeypatch.setenv("CAIRN_PROMPTS_ROOT", str(tmp_path / "prompts"))
    monkeypatch.setenv("CAIRN_SESSION_COOKIE_SECURE", "0")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    db.configure(database_url=empty_postgres_url, artifact_path=tmp_path / "artifacts")
    assert db.database_config().database_url == empty_postgres_url
    assert not app.dependency_overrides, "traffic fixture requires unmodified authentication and business dependencies"

    token = secrets.token_urlsafe(32)
    identity = ensure_local_runtime_principals(
        admin_login="traffic-bootstrap", admin_password=secrets.token_urlsafe(32),
        dispatcher_name="traffic-fixture", dispatcher_token=token,
    )
    password = secrets.token_urlsafe(32)
    # 读取测试使用普通用户，而非会绕过 Tenant 限制的超级管理员。
    _, reader_tenant = create_user_with_personal_tenant("traffic-reader", password)
    outsider_password = secrets.token_urlsafe(32)
    _, outsider_tenant = create_user_with_personal_tenant("traffic-outsider", outsider_password)
    assert outsider_tenant != reader_tenant

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    monkeypatch.setenv("CAIRN_TRUSTED_ORIGINS", base_url)
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=0, lifespan="on", access_log=False,
        log_config=None, log_level="warning", timeout_graceful_shutdown=5,
    ))
    startup_errors = []

    def serve():
        try:
            server.run(sockets=[listener])
        except BaseException as exc:
            startup_errors.append(exc)

    thread = threading.Thread(target=serve, name="traffic-test-api", daemon=True)
    user = None
    outsider = None
    machine = None
    connection = None
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started:
            if not thread.is_alive() or startup_errors:
                raise AssertionError("isolated traffic API failed to start") from (startup_errors[0] if startup_errors else None)
            if time.monotonic() >= deadline:
                raise AssertionError("isolated traffic API startup timed out")
            time.sleep(0.05)
        user = _login(base_url, "traffic-reader", password)
        outsider = _login(base_url, "traffic-outsider", outsider_password)
        assert _json_request(user, base_url, "GET", "/health", expected=200) == {"status": "ok"}
        machine = CairnClient(base_url, timeout=15, token=token)
        machine._session().trust_env = False
        heartbeat = machine.dispatcher_heartbeat(
            dispatcher_id=identity["dispatcher_principal_id"], status="running",
            config_hash="traffic-isolated-fixture", running_task_count=0, active_project_count=0,
        )
        assert heartbeat.ok, heartbeat.data

        # 这里只保存合成配置，不调用发现模型或测试模型的接口，也不启动 Dispatcher。
        model = _json_request(user, base_url, "POST", "/models", expected=201, json={
            "display_name": "流量隔离实验模型", "protocol": "openai-chat-completions",
            "base_url": base_url + "/unused-model", "model_name": "traffic-not-executed",
            "api_key": secrets.token_urlsafe(24),
        })
        worker = _json_request(user, base_url, "POST", "/tenant-workers", expected=201, json={
            "display_name": "流量隔离实验 Worker", "model_id": model["id"], "engine": "pi",
            "task_types": ["bootstrap", "reason", "explore"], "max_running": 2,
        })
        assert worker["available"] is True

        def create_project(title: str):
            return _json_request(user, base_url, "POST", "/projects", expected=201, json={
                "title": title, "origin": "仅测试本次隔离 Docker 拓扑中的合成 HTTP 服务",
                "goal": "验证采集保存、重传幂等和原件读取，不执行模型研究",
                "scenario": "goal-driven-v1", "tenant_worker_ids": [worker["id"]],
                "authorization_expires_at": None,
            })

        project = create_project("HTTP 流量隔离实验")
        project_id = project["project"]["id"]
        generation = project["project"]["execution_generation"]
        task_runs = []
        for number in (1, 2):
            intent = _json_request(
                user, base_url, "POST", f"/projects/{project_id}/intents", expected=201,
                json={"from": ["origin"], "description": f"合成 HTTP 流量源 {number}"},
            )
            admitted = machine.start_explore_attempt(
                project_id, intent["id"], f"traffic-source-{number}", worker_type="pi",
                prompt_group="goal-driven-v1", expected_execution_generation=generation,
            )
            assert admitted.status_code == 201, admitted.data
            assert admitted.data["status"] == "running"
            assert admitted.data["runtime_context"]["dispatcher_id"] == identity["dispatcher_principal_id"]
            task_runs.append(admitted.data)
        other = create_project("HTTP 流量跨项目拒绝对照")
        connection = db.PostgresConnection(psycopg.connect(
            empty_postgres_url, autocommit=True, row_factory=dict_row,
        ))
        yield SimpleNamespace(
            base_url=base_url, user=user, outsider=outsider, machine=machine, project_id=project_id,
            task_runs=task_runs, other_project_id=other["project"]["id"], conn=connection,
        )
    finally:
        if machine is not None:
            machine.close()
        if user is not None:
            user.close()
        if outsider is not None:
            outsider.close()
        server.should_exit = True
        thread.join(timeout=15)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=10)
        listener.close()
        if connection is not None:
            connection.close()
        assert not thread.is_alive(), "isolated traffic API did not stop before PostgreSQL cleanup"
