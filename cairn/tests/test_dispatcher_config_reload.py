from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.scheduler.loop import DispatcherBootstrap, DispatcherLoop


class DummyClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.closed = False

    def close(self) -> None:
        self.closed = True


class DummyContainerManager:
    def __init__(self):
        self.configs = []

    def update_config(self, config) -> None:
        self.configs.append(config)


def write_dispatch_yaml(path: Path, *, interval: int = 3, max_workers: int = 1) -> None:
    path.write_text(
        f"""\
server: "http://127.0.0.1:8000"
dispatcher_id: "reload-test"
runtime:
  interval: {interval}
  max_workers: {max_workers}
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
""",
        encoding="utf-8",
    )


def make_reload_loop(config_path: Path) -> DispatcherLoop:
    config = DispatchConfig.load(config_path)
    raw = config_path.read_bytes()
    stat = config_path.stat()
    loop = object.__new__(DispatcherLoop)
    loop.bootstrap = DispatcherBootstrap(config_path=config_path, hard_cap=4)
    loop._config_path = config_path.resolve()
    loop._config_mtime_ns = stat.st_mtime_ns
    loop._config_sha256 = sha256(raw).hexdigest()
    loop._config_error_message = None
    loop.config = config
    loop.client = DummyClient(config.server)
    loop.container_manager = DummyContainerManager()
    loop.dispatcher_id = config.dispatcher_id
    loop._last_config_hash = loop._config_sha256
    loop._settings_checked = True
    loop._startup_healthchecks_checked = True
    loop._worker_capabilities = {"stale-worker": frozenset({"candidate-spool-v1"})}
    loop.candidate_capture_outbox = object()
    return loop


def test_yaml_reload_applies_changed_config(tmp_path: Path) -> None:
    config_path = tmp_path / "dispatch.yaml"
    write_dispatch_yaml(config_path, interval=3)
    loop = make_reload_loop(config_path)

    write_dispatch_yaml(config_path, interval=7)

    assert loop._refresh_config() is True
    assert loop.config.runtime.interval == 7
    assert loop._config_error_message is None
    assert loop._settings_checked is False
    assert loop._startup_healthchecks_checked is False
    assert loop._worker_capabilities == {}
    assert len(loop.container_manager.configs) == 1


def test_yaml_reload_applies_worker_enablement_without_touching_running_tasks(tmp_path: Path) -> None:
    config_path = tmp_path / "dispatch.yaml"
    write_dispatch_yaml(config_path)
    loop = make_reload_loop(config_path)
    running_marker = object()
    loop.futures = {running_marker: object()}

    draft_worker = """workers:
  - id: worker-a
    engine: pi
    task_types: [bootstrap, reason, explore]
    concurrency: 1
    priority: 0
    enabled: false
"""
    content = config_path.read_text(encoding="utf-8").replace("workers: []\n", draft_worker)
    config_path.write_text(content, encoding="utf-8")

    assert loop._refresh_config() is True
    assert loop.config.workers[0].enabled is False
    assert running_marker in loop.futures

    loop._startup_healthchecks_checked = True
    loop._worker_capabilities = {
        "worker-a": frozenset({"candidate-spool-v1"})
    }
    config_path.write_text(content.replace("enabled: false", "enabled: true"), encoding="utf-8")

    assert loop._refresh_config() is True
    assert loop.config.workers[0].enabled is True
    assert loop._startup_healthchecks_checked is False
    assert loop._worker_capabilities == {}
    assert running_marker in loop.futures


def test_yaml_reload_failure_keeps_previous_config(tmp_path: Path) -> None:
    config_path = tmp_path / "dispatch.yaml"
    write_dispatch_yaml(config_path, interval=3)
    loop = make_reload_loop(config_path)

    config_path.write_text("runtime: [", encoding="utf-8")

    assert loop._refresh_config() is False
    assert loop.config.runtime.interval == 3
    assert loop._config_sha256 != sha256(config_path.read_bytes()).hexdigest()
    assert "ParserError" in (loop._config_error_message or "") or "ScannerError" in (loop._config_error_message or "")
