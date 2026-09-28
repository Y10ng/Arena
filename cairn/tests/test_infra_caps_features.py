from __future__ import annotations

from contextlib import contextmanager
from json import dumps
import sqlite3

import pytest
from fastapi import HTTPException

from cairn.dispatcher.config import (
    BootstrapTaskConfig,
    ContainerConfig,
    DispatchConfig,
    ExploreTaskConfig,
    ExecutionProfileConfig,
    ImageConfig,
    ModelConfig,
    ProviderConfig,
    ReasonTaskConfig,
    RuntimeConfig,
    TasksConfig,
    WorkerConfig,
)
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server import db
from cairn.server.models import DispatcherHeartbeatRequest, Scenario
from cairn.server.services import (
    read_dispatcher_config,
    read_dispatcher_yaml,
    replace_scenarios,
    scenario_allowed_caps_with_source,
    write_dispatcher_config,
    write_dispatcher_yaml,
)
from cairn.server.services.monitoring import record_dispatcher_heartbeat


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(db.SCHEMA)
    db._ensure_schema_migrations(conn)
    return conn


def insert_dispatcher_status(
    conn: sqlite3.Connection,
    *,
    worker_pool: list[dict[str, object]] | None = None,
    images: list[str] | None = None,
    caps: list[str] | None = None,
    config_path: str | None = None,
    server_reachable_path: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO dispatcher_status
            (id, status, worker_pool_json, container_images_json, cap_baseline_json,
             config_path, server_reachable_path, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "dispatcher-test",
            "running",
            dumps(worker_pool or [worker_report("fixture-pi")]),
            dumps(images or []),
            dumps(caps) if caps is not None else None,
            config_path,
            server_reachable_path,
            "2026-01-01T00:00:00Z",
        ),
    )


def worker_report(name: str, worker_type: str = "pi", env_keys: list[str] | None = None) -> dict[str, object]:
    return {
        "worker_name": name,
        "worker_type": worker_type,
        "task_types": ["bootstrap", "reason", "explore"],
        "priority": 0,
        "max_running": 1,
        "running_count": 0,
        "env_keys": env_keys or [],
    }


def scenario(**overrides: object) -> Scenario:
    payload = {
        "key": "custom",
        "label": "Custom",
        "prompt_group": "default",
        "knowledge": "knowledge_base",
    }
    payload.update(overrides)
    return Scenario(**payload)


def valid_dispatch_yaml(interval: int = 3) -> str:
    return f"""\
server: "http://127.0.0.1:8000"
dispatcher_id: "dispatcher-test"
runtime:
  interval: {interval}
  max_workers: 1
  max_running_projects: 1
  max_project_workers: 1
  healthcheck_timeout: 1
  prompt_group: "default"
tasks:
  bootstrap:
    timeout: 1
    conclude_timeout: 1
  reason:
    timeout: 1
    max_intents: 1
  explore:
    timeout: 1
    conclude_timeout: 1
container:
  image: "cairn/default:latest"
  allowed_images: []
  network_mode: "none"
  completed_action: "stop"
  execution_profile: "shared_local"
  cap_add: []
common_env: {{}}
workers: []
"""


def test_cap_allowed_set_uses_dispatcher_report() -> None:
    conn = make_conn()
    try:
        insert_dispatcher_status(conn, caps=["NET_RAW"])

        allowed, source = scenario_allowed_caps_with_source(conn)
        assert allowed == {"NET_RAW"}
        assert source == "declared_inventory"

        with pytest.raises(HTTPException) as exc:
            replace_scenarios(conn, [scenario(cap_add=["SYS_ADMIN"])])

        assert exc.value.status_code == 422
        assert "SYS_ADMIN" in str(exc.value.detail)
    finally:
        conn.close()


def test_cap_allowed_set_falls_back_without_dispatcher_report() -> None:
    conn = make_conn()
    try:
        allowed, source = scenario_allowed_caps_with_source(conn)

        assert allowed == set()
        assert source == "unavailable"
    finally:
        conn.close()


def test_image_is_validated_at_save_time_from_dispatcher_report() -> None:
    conn = make_conn()
    try:
        insert_dispatcher_status(conn, images=["cairn/default:latest"], caps=["NET_RAW"])

        with pytest.raises(HTTPException) as exc:
            replace_scenarios(
                conn,
                [scenario(image="cairn/untrusted:latest", cap_add=["NET_RAW"])],
            )

        assert exc.value.status_code == 422
        assert "cairn/untrusted:latest" in str(exc.value.detail)
    finally:
        conn.close()


def test_features_are_schema_validated() -> None:
    conn = make_conn()
    try:
        with pytest.raises(HTTPException) as exc:
            replace_scenarios(conn, [scenario(features={"sandbox": "yes"})])
        assert exc.value.status_code == 422
        assert "sandbox" in str(exc.value.detail)

        with pytest.raises(HTTPException) as exc:
            replace_scenarios(conn, [scenario(features={"unknown": True})])
        assert exc.value.status_code == 422
        assert "unknown" in str(exc.value.detail)
    finally:
        conn.close()


def test_network_flow_feature_drives_container_network_mode() -> None:
    loop = dispatcher_loop_for_policy(network_mode="host")

    disabled = loop._container_runtime_options(scenario(features={}))
    enabled = loop._container_runtime_options(scenario(features={"network_flow": True}))

    assert disabled.network_mode == "none"
    assert enabled.network_mode == "host"


def test_execution_profiles_drive_distinct_container_runtime() -> None:
    loop = dispatcher_loop_for_policy(network_mode="host")

    default_runtime = loop._container_runtime_options(scenario(profile_id="default"))
    network_runtime = loop._container_runtime_options(scenario(profile_id="network"))
    v2_runtime = loop._container_runtime_options(
        scenario(key="default-copy-v2", profile_id="default-copy-v2")
    )

    assert default_runtime.network_mode == "host"
    assert network_runtime.network_mode == "host"
    assert default_runtime.features["profile_id"] == "default"
    assert network_runtime.features["profile_id"] == "network"
    assert default_runtime.runtime_signature != network_runtime.runtime_signature
    assert default_runtime.mem_limit is None
    assert v2_runtime.mem_limit == 1536 * 1024 * 1024
    assert v2_runtime.memswap_limit == 1536 * 1024 * 1024
    assert v2_runtime.pids_limit == 256
    assert v2_runtime.runtime_signature != default_runtime.runtime_signature


def test_default_copy_v2_profile_requires_resource_boundaries() -> None:
    with pytest.raises(ValueError, match="requires mem_limit"):
        ExecutionProfileConfig(id="default-copy-v2")

    with pytest.raises(ValueError, match="must equal mem_limit"):
        ExecutionProfileConfig(
            id="default-copy-v2",
            mem_limit="1g",
            memswap_limit="2g",
            pids_limit=128,
        )


def test_default_copy_v2_missing_profile_fails_closed() -> None:
    loop = dispatcher_loop_for_policy(network_mode="host")
    loop.config.execution_profiles = [
        profile
        for profile in loop.config.execution_profiles
        if profile.id != "default-copy-v2"
    ]

    with pytest.raises(ValueError, match="requires the default-copy-v2 execution profile"):
        loop._container_runtime_options(
            scenario(key="default-copy-v2", profile_id="default-copy-v2")
        )


def test_execution_profile_memory_limits_are_validated() -> None:
    profile = ExecutionProfileConfig(
        id="bounded",
        mem_limit="1536m",
        memswap_limit="2g",
        pids_limit=128,
    )
    assert profile.mem_limit == 1536 * 1024 * 1024
    assert profile.memswap_limit == 2 * 1024 * 1024 * 1024

    with pytest.raises(ValueError, match="cannot be smaller"):
        ExecutionProfileConfig(
            id="invalid",
            mem_limit="2g",
            memswap_limit="1536m",
        )


def test_dispatch_config_resolves_structured_pi_model_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_API_KEY", "secret-token")

    config = DispatchConfig(
        server="http://127.0.0.1:8000",
        runtime=RuntimeConfig(
            max_workers=2,
            max_running_projects=1,
            max_project_workers=2,
            interval=1,
            healthcheck_timeout=1,
            prompt_group="default",
        ),
        tasks=TasksConfig(
            bootstrap=BootstrapTaskConfig(timeout=1, conclude_timeout=1),
            reason=ReasonTaskConfig(timeout=1, max_intents=1),
            explore=ExploreTaskConfig(timeout=1, conclude_timeout=1),
        ),
        container=ContainerConfig(
            image="cairn/default:latest",
            allowed_images=[],
            network_mode="host",
            completed_action="stop",
            cap_add=["NET_RAW"],
        ),
        providers=[
            ProviderConfig(
                id="local",
                base_url="http://127.0.0.1:8317",
                inference_endpoint="http://127.0.0.1:8317/v1/messages",
                provider_api="anthropic-messages",
                credential_ref="LOCAL_API_KEY",
            )
        ],
        models=[ModelConfig(id="deepseek", provider_id="local", upstream_model="deepseek-v4-flash")],
        images=[ImageConfig(id="default", image="cairn/default:latest")],
        execution_profiles=[
            ExecutionProfileConfig(
                id="default",
                image_ref="default",
                network_policy="none",
                cap_add=["NET_RAW"],
                completed_action="stop",
            )
        ],
        workers=[
            WorkerConfig(
                id="pi-deepseek",
                engine="pi",
                model_id="deepseek",
                task_types=["reason"],
                concurrency=1,
                priority=1,
            )
        ],
    )

    worker = config.workers[0]
    assert worker.name == "pi-deepseek"
    assert worker.type == "pi"
    assert worker.max_running == 1
    binding = worker.model_binding
    assert binding is not None
    assert binding.model_name == "deepseek-v4-flash"
    assert binding.protocol == "anthropic-messages"
    assert binding.base_url == "http://127.0.0.1:8317"
    assert binding.inference_endpoint == "http://127.0.0.1:8317/v1/messages"
    assert binding.api_key == "secret-token"
    assert worker.env == {}
    inventory = config.declared_inventory()
    assert inventory["workers"][0]["id"] == "pi-deepseek"
    assert "secret-token" not in dumps(inventory)
    assert "secret-token" not in repr(worker)


def test_dispatcher_heartbeat_persists_declared_inventory() -> None:
    conn = make_conn()
    try:
        inventory = {
            "workers": [{"id": "worker-a", "engine": "pi", "task_types": ["reason"], "concurrency": 1, "priority": 1}],
            "execution_profiles": [{"id": "default", "image_ref": "default", "network_policy": "none", "cap_add": []}],
        }
        status = record_dispatcher_heartbeat(
            conn,
            DispatcherHeartbeatRequest(
                dispatcher_id="dispatcher-test",
                status="running",
                config_hash="hash",
                running_task_count=0,
                active_project_count=0,
                declared_inventory=inventory,
            ),
        )

        assert status.declared_inventory == inventory
        row = conn.execute("SELECT declared_inventory_json FROM dispatcher_status WHERE id = ?", ("dispatcher-test",)).fetchone()
        assert "worker-a" in row["declared_inventory_json"]
    finally:
        conn.close()


def test_replace_scenarios_rejects_unknown_profile_from_declared_inventory() -> None:
    conn = make_conn()
    try:
        inventory = {
            "workers": [{"id": "worker-a", "engine": "pi", "task_types": ["reason"], "concurrency": 1, "priority": 1}],
            "execution_profiles": [{"id": "default", "image_ref": "default", "network_policy": "none", "cap_add": []}],
            "capabilities": [],
            "images": [{"id": "default", "image": "cairn/default:latest"}],
        }
        conn.execute(
            """
            INSERT INTO dispatcher_status
                (id, status, declared_inventory_json, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            ("dispatcher-test", "running", dumps(inventory), "2026-01-01T00:00:00Z"),
        )

        with pytest.raises(HTTPException) as exc:
            replace_scenarios(
                conn,
                [
                    scenario(
                        profile_id="missing",
                    )
                ],
            )

        assert exc.value.status_code == 422
        assert "missing" in str(exc.value.detail)
    finally:
        conn.close()


def test_inventory_routes_read_dispatcher_report_without_env_values(monkeypatch: pytest.MonkeyPatch) -> None:
    from cairn.server.routers import infra

    conn = make_conn()

    @contextmanager
    def fake_get_conn():
        yield conn

    try:
        insert_dispatcher_status(
            conn,
            worker_pool=[worker_report("secret_worker", "pi", ["OPENAI_API_KEY"])],
            images=["cairn/default:latest"],
            caps=["NET_RAW"],
        )
        monkeypatch.setattr(infra, "get_conn", fake_get_conn)

        workers = infra.list_workers()
        images = infra.list_images()
        caps = infra.list_capabilities()

        assert workers[0].worker_name == "secret_worker"
        assert workers[0].env_keys == ["OPENAI_API_KEY"]
        assert "secret-token" not in str(workers[0].model_dump())
        assert workers[0].source == "dispatcher_report"
        assert images[0].image == "cairn/default:latest"
        assert images[0].source == "dispatcher_report"
        assert caps[0].name == "NET_RAW"
        assert caps[0].source == "declared_inventory"
    finally:
        conn.close()


def test_dispatcher_yaml_read_write_validates_and_replaces(tmp_path) -> None:
    conn = make_conn()
    config_path = tmp_path / "dispatch.yaml"
    config_path.write_text(valid_dispatch_yaml(), encoding="utf-8")
    try:
        insert_dispatcher_status(conn, server_reachable_path=str(config_path))

        document = read_dispatcher_yaml(conn)
        assert document.path == str(config_path.resolve())
        assert "dispatcher_id" in document.content

        with pytest.raises(HTTPException) as exc:
            write_dispatcher_yaml(conn, "runtime: [")
        assert exc.value.status_code == 422
        assert config_path.read_text(encoding="utf-8") == valid_dispatch_yaml()

        updated = write_dispatcher_yaml(conn, valid_dispatch_yaml(interval=5))
        assert "interval: 5" in updated.content
        assert "interval: 5" in config_path.read_text(encoding="utf-8")
    finally:
        conn.close()


def test_dispatcher_yaml_uses_fixed_server_config_without_heartbeat(tmp_path, monkeypatch) -> None:
    conn = make_conn()
    config_path = tmp_path / "dispatch.yaml"
    config_path.write_text(valid_dispatch_yaml(), encoding="utf-8")
    monkeypatch.setenv("CAIRN_DISPATCH_CONFIG", str(config_path))
    monkeypatch.setenv("CAIRN_DISPATCH_YAML_ALLOWED_ROOTS", str(tmp_path))
    try:
        document = read_dispatcher_yaml(conn)

        assert document.path == str(config_path.resolve())
        assert "dispatcher_id" in document.content
    finally:
        conn.close()


def test_fixed_server_config_still_enforces_allowed_roots(tmp_path, monkeypatch) -> None:
    conn = make_conn()
    config_path = tmp_path / "dispatch.yaml"
    config_path.write_text(valid_dispatch_yaml(), encoding="utf-8")
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    monkeypatch.setenv("CAIRN_DISPATCH_CONFIG", str(config_path))
    monkeypatch.setenv("CAIRN_DISPATCH_YAML_ALLOWED_ROOTS", str(allowed_root))
    try:
        with pytest.raises(HTTPException) as exc:
            read_dispatcher_yaml(conn)

        assert exc.value.status_code == 409
        assert "outside allowed roots" in str(exc.value.detail)
    finally:
        conn.close()


def test_dispatcher_yaml_uses_reported_startup_config_path(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CAIRN_DISPATCH_CONFIG", raising=False)
    conn = make_conn()
    config_path = tmp_path / "dispatch.yaml"
    config_path.write_text(valid_dispatch_yaml(), encoding="utf-8")
    try:
        insert_dispatcher_status(conn, config_path=str(config_path))

        document = read_dispatcher_yaml(conn)
        assert document.path == str(config_path.resolve())

        updated = write_dispatcher_yaml(conn, valid_dispatch_yaml(interval=9))
        assert "interval: 9" in updated.content
        assert "interval: 9" in config_path.read_text(encoding="utf-8")
    finally:
        conn.close()


def test_dispatcher_config_read_write_serializes_to_yaml(tmp_path) -> None:
    conn = make_conn()
    config_path = tmp_path / "dispatch.yaml"
    config_path.write_text(valid_dispatch_yaml(), encoding="utf-8")
    try:
        insert_dispatcher_status(conn, server_reachable_path=str(config_path))

        document = read_dispatcher_config(conn)
        assert document.config["runtime"]["interval"] == 3

        updated_config = dict(document.config)
        updated_config["runtime"] = dict(updated_config["runtime"], interval=6)
        updated = write_dispatcher_config(conn, updated_config)

        assert updated.config["runtime"]["interval"] == 6
        assert "interval: 6" in config_path.read_text(encoding="utf-8")
    finally:
        conn.close()


def dispatcher_loop_for_policy(*, network_mode: str = "none") -> DispatcherLoop:
    loop = object.__new__(DispatcherLoop)
    loop._log_state = {}
    loop.dispatcher_id = "dispatcher-test"
    loop._config_path = PathLike("dispatch.yaml")
    loop._config_sha256 = "hash"
    loop.config = DispatchConfig(
        server="http://127.0.0.1:8000",
        runtime=RuntimeConfig(
            max_workers=1,
            max_running_projects=1,
            max_project_workers=1,
            interval=1,
            healthcheck_timeout=1,
            prompt_group="default",
        ),
        tasks=TasksConfig(
            bootstrap=BootstrapTaskConfig(timeout=1, conclude_timeout=1),
            reason=ReasonTaskConfig(timeout=1, max_intents=1),
            explore=ExploreTaskConfig(timeout=1, conclude_timeout=1),
        ),
        container=ContainerConfig(
            image="cairn/default:latest",
            allowed_images=["cairn/web:latest"],
            network_mode=network_mode,
            completed_action="stop",
            cap_add=["NET_RAW"],
        ),
        workers=[
            WorkerConfig(
                name="fixture-pi",
                engine="pi",
                task_types=["bootstrap", "reason", "explore"],
                max_running=1,
                priority=1,
            )
        ],
    )
    return loop


class PathLike(str):
    def __str__(self) -> str:
        return str.__str__(self)
