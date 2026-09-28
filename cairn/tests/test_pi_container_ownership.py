from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from hashlib import sha256
import json
from types import SimpleNamespace

from docker.errors import APIError, NotFound
import pytest
from requests.exceptions import RequestException

from cairn.dispatcher.config import ContainerConfig, DispatchConfig
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.container_archives import text_file_archive
from cairn.dispatcher.runtime.containers import (
    ContainerManager,
    ContainerOwnershipError,
    canonical_server_address,
    container_instance_id,
)
from cairn.dispatcher.scheduler.cleanup import CleanupMixin
from cairn.dispatcher.scheduler.loop import DispatcherBootstrap, DispatcherLoop


SERVER = "https://server.example.test/api"
INSTANCE_ID = container_instance_id("dispatcher-a", SERVER)
OTHER_ID = container_instance_id("dispatcher-b", SERVER)


class FakeContainer:
    def __init__(self, docker, name, labels, *, state="running"):
        self.client = docker
        self.name = name
        self.id = f"container-{len(docker.objects)}"
        self.attrs = {"Config": {"Labels": dict(labels)}, "State": {"Status": state}}
        self.actions = []
        docker.objects.append(self)

    @property
    def status(self):
        return self.attrs["State"]["Status"]

    def reload(self):
        pass

    def start(self):
        self.actions.append("start")
        self.attrs["State"]["Status"] = "running"

    def stop(self, **kwargs):
        self.actions.append("stop")
        self.attrs["State"]["Status"] = "exited"

    def remove(self, **kwargs):
        self.actions.append("remove")
        self.client.objects.remove(self)

    def exec_run(self, command, **kwargs):
        self.actions.append(("exec", command))
        return SimpleNamespace(exit_code=0, output=b"1000\n")

    def put_archive(self, path, archive):
        self.actions.append(("write", path))
        return True

    def get_archive(self, path):
        self.actions.append(("read", path))
        _, archive = text_file_archive(path, "fixture")
        return iter([archive]), {}


class FakeDocker:
    def __init__(self):
        self.objects = []
        self.runs = []
        self.daemon_id = "daemon-1"
        self.lookups = []
        self.filters = []
        self.closed = False
        self.api = SimpleNamespace()
        self.containers = self

    def info(self):
        return {"ID": self.daemon_id}

    def get(self, identity):
        self.lookups.append(identity)
        for container in self.objects:
            if identity in (container.name, container.id):
                return container
        raise NotFound("missing fake container")

    def list(self, **kwargs):
        self.filters.append(kwargs)
        # 故意忽略Docker过滤器，要求manager仍逐个验证名称和完整标签。
        return list(self.objects)

    def run(self, image, command, **kwargs):
        self.runs.append(kwargs)
        return FakeContainer(self, kwargs["name"], kwargs["labels"])

    def close(self):
        self.closed = True


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    docker = FakeDocker()
    monkeypatch.setattr("cairn.dispatcher.runtime.containers.docker.from_env", lambda *, timeout: docker)
    monkeypatch.setenv("CAIRN_RUNTIME_TEMP_DIR", str(tmp_path / "runtime"))
    config = ContainerConfig(image="worker:test", network_mode="none", completed_action="stop")
    return ContainerManager(config, instance_id=INSTANCE_ID), docker


def owned_container(manager, docker, project="proj_a", **kwargs):
    return FakeContainer(
        docker, manager.container_name(project),
        manager._container_labels(project, manager.default_runtime_options()), **kwargs,
    )


@pytest.mark.parametrize("server", [
    " HTTPS://SERVER.EXAMPLE.TEST:443/api/ ",
    "https://synthetic-user:synthetic-secret@server.example.test/api?token=fixture#secret",
    SERVER,
])
def test_identity_normalizes_server_and_excludes_credentials(server):
    expected = sha256(json.dumps(
        ["dispatcher-a", SERVER], ensure_ascii=True, separators=(",", ":"),
    ).encode()).hexdigest()
    assert container_instance_id(" dispatcher-a ", server) == expected == INSTANCE_ID


@pytest.mark.parametrize(("left", "right"), [
    ("http://SERVER.example.test:80/", "http://server.example.test"),
    ("https://例子.test/api/", "https://xn--fsqu00a.test/api"),
    ("http://[2001:0db8:0:0:0:0:0:1]:80/", "http://[2001:db8::1]"),
    ("https://server.example.test/a/../api/", "https://server.example.test/api"),
    ("https://server.example.test/%61pi/", "https://server.example.test/api"),
    ("https://server.example.test/a/./b/", "https://server.example.test/a/b"),
])
def test_identity_uses_canonical_host(left, right):
    assert container_instance_id("dispatcher-a", left) == container_instance_id("dispatcher-a", right)


def test_server_path_canonicalization_matches_requests_ordering():
    base = "https://server.example.test"
    assert canonical_server_address(base + "/%2e%2e/api") == base + "/../api"
    assert canonical_server_address(base + "/a%2Fb/") == base + "/a%2Fb"
    assert canonical_server_address(base + "/a//b/") == base + "/a//b"
    assert container_instance_id(
        "dispatcher-a", base + "/a//b"
    ) != container_instance_id("dispatcher-a", base + "/a/b")


@pytest.mark.parametrize(("dispatcher", "server"), [
    ("dispatcher-b", SERVER),
    ("dispatcher-a", "https://other.example.test/api"),
    ("dispatcher-a", "https://server.example.test:8443/api"),
    ("dispatcher-a", "https://server.example.test/other"),
    ("dispatcher-a", "http://server.example.test/api"),
])
def test_identity_separates_dispatcher_and_server(dispatcher, server):
    assert container_instance_id(dispatcher, server) != INSTANCE_ID


@pytest.mark.parametrize("server", [
    "not-a-url", "ftp://fixture:synthetic-secret@server.example.test", "http:///api",
    "https://fixture:synthetic-secret@server.example.test:bad", "http://[invalid",
])
def test_invalid_identity_errors_do_not_echo_credentials(server):
    with pytest.raises(ValueError) as error:
        container_instance_id("dispatcher-a", server)
    assert str(error.value) == "invalid dispatcher container ownership configuration"


def test_manager_requires_identity_before_docker_access(monkeypatch):
    def unexpected_docker():
        pytest.fail("身份无效时不得访问Docker")

    monkeypatch.setattr("cairn.dispatcher.runtime.containers.docker.from_env", unexpected_docker)
    config = ContainerConfig(image="worker:test", network_mode="none", completed_action="stop")
    with pytest.raises(TypeError, match="instance_id"):
        ContainerManager(config)
    for invalid in ("", "dispatcher-a", "../other", "A" * 64):
        with pytest.raises(ValueError, match="instance_id"):
            ContainerManager(config, instance_id=invalid)
    with pytest.raises(ValueError):
        container_instance_id(" ", SERVER)


def test_creation_uses_separate_owner_namespaces_and_labels(runtime):
    manager, docker = runtime
    old = FakeContainer(docker, "cairn-dispatch-proj_a", {})
    old_health = FakeContainer(docker, "cairn-startup-healthcheck-0123456789ab", {})
    project = manager.ensure_running("proj_a")
    health = manager.create_startup_container()
    assert project == f"cairn-pi-dispatch-{INSTANCE_ID[:16]}-proj_a"
    assert health.startswith(f"cairn-pi-healthcheck-{INSTANCE_ID[:16]}-")
    for call in docker.runs:
        assert call["init"] is True
        assert call["labels"]["cairn.pi.instance_id"] == INSTANCE_ID
        assert call["labels"]["cairn.engine"] == "pi"
        assert "synthetic-secret" not in repr(call)
    assert manager.managed_container_names() == [project]
    assert old.actions == old_health.actions == []
    assert old.name not in docker.lookups and old_health.name not in docker.lookups
    assert manager.build_exec_process(health, {}, ["true"]) is not None
    manager.remove_container(health)
    assert manager.inspect_state(health) is None


@pytest.mark.parametrize("state", ["running", "exited"])
def test_same_identity_reuses_only_owned_container(runtime, state):
    manager, docker = runtime
    existing = owned_container(manager, docker, state=state)
    restarted = ContainerManager(manager._config, instance_id=INSTANCE_ID)
    assert restarted.ensure_running("proj_a") == existing.name
    assert existing.actions == ([] if state == "running" else ["start"])
    assert docker.runs == []
    other = ContainerManager(manager._config, instance_id=OTHER_ID)
    assert other.ensure_running("proj_a") != existing.name
    assert len(docker.runs) == 1


def test_continuation_activates_only_the_original_stopped_container(runtime):
    manager, docker = runtime
    retained = owned_container(manager, docker, state="exited")
    events = []

    def register(daemon_id):
        events.append(("register", daemon_id, retained.status))
        return "activation-token"

    def finish(token, container_id):
        events.append(("finish", token, container_id, retained.status))

    assert manager.activate_existing_for_continuation(
        "proj_a", manager.default_runtime_options(), retained.id,
        register=register, finish=finish, reuse=lambda *_args: False,
    ) == retained.name
    assert events == [
        ("register", docker.daemon_id, "exited"),
        ("finish", "activation-token", retained.id, "running"),
    ]
    assert retained.actions == ["start"]
    assert docker.runs == []


def test_continuation_reuses_running_container_only_with_confirmed_activation(runtime):
    manager, docker = runtime
    retained = owned_container(manager, docker, state="running")
    checked = []

    def reuse(daemon_id, container_id):
        checked.append((daemon_id, container_id))
        return True

    assert manager.activate_existing_for_continuation(
        "proj_a", manager.default_runtime_options(), retained.id,
        register=lambda *_args: pytest.fail("running container must not register again"),
        finish=lambda *_args: pytest.fail("running container must not finish again"),
        reuse=reuse,
    ) == retained.name
    assert checked == [(docker.daemon_id, retained.id)]
    assert retained.actions == [] and docker.runs == []


def test_continuation_rejects_missing_replaced_or_running_container(runtime):
    manager, docker = runtime
    retained = owned_container(manager, docker, state="exited")
    register = lambda _daemon: pytest.fail("invalid container must not register")
    finish = lambda *_args: pytest.fail("invalid container must not finish")
    for expected_id in ("wrong-container", ""):
        with pytest.raises((ContainerOwnershipError, ValueError)):
            manager.activate_existing_for_continuation(
                "proj_a", manager.default_runtime_options(), expected_id,
                register=register, finish=finish, reuse=lambda *_args: False,
            )
    retained.attrs["Config"]["Labels"]["cairn.runtime_signature"] = "different"
    with pytest.raises(ContainerOwnershipError):
        manager.activate_existing_for_continuation(
            "proj_a", manager.default_runtime_options(), retained.id,
            register=register, finish=finish, reuse=lambda *_args: False,
        )
    retained.attrs["Config"]["Labels"] = manager._container_labels(
        "proj_a", manager.default_runtime_options(),
    )
    retained.start()
    with pytest.raises(RuntimeError, match="not confirmed for this generation"):
        manager.activate_existing_for_continuation(
            "proj_a", manager.default_runtime_options(), retained.id,
            register=register, finish=finish, reuse=lambda *_args: False,
        )
    assert docker.runs == []


def test_continuation_refuses_replacement_after_register(runtime):
    manager, docker = runtime
    retained = owned_container(manager, docker, state="exited")
    finished = []

    def register(_daemon):
        docker.objects.remove(retained)
        replacement = owned_container(manager, docker, state="exited")
        replacement.id = "replacement-id"
        return "activation-token"

    with pytest.raises(ContainerOwnershipError, match="changed before start"):
        manager.activate_existing_for_continuation(
            "proj_a", manager.default_runtime_options(), retained.id,
            register=register, finish=lambda *_args: finished.append(True), reuse=lambda *_args: False,
        )
    assert finished == []
    assert retained.actions == docker.objects[0].actions == []
    assert docker.runs == []


def test_continuation_refuses_daemon_switch_after_start(runtime, monkeypatch):
    manager, docker = runtime
    retained = owned_container(manager, docker, state="exited")
    original_start = retained.start
    finished = []

    def start_and_switch():
        original_start()
        docker.daemon_id = "different-daemon"

    monkeypatch.setattr(retained, "start", start_and_switch)
    with pytest.raises(RuntimeError, match="Docker daemon changed after start"):
        manager.activate_existing_for_continuation(
            "proj_a", manager.default_runtime_options(), retained.id,
            register=lambda _daemon: "activation-token",
            finish=lambda *_args: finished.append(True), reuse=lambda *_args: False,
        )
    assert retained.actions == ["start"]
    assert finished == []
    assert docker.runs == []


OPERATIONS = [
    "reuse", "exec", "read", "materialize", "write", "remove_file", "stop",
    "completed_stop", "completed_remove", "remove", "orphan", "start",
]


def operate(manager, name, operation, *, project="proj_a"):
    if operation == "reuse":
        return manager.ensure_running(project)
    if operation == "exec":
        return manager.build_exec_process(name, {}, ["true"])
    if operation == "read":
        return manager.read_file(name, "/tmp/fixture")
    if operation == "materialize":
        with manager.materialize_file(name, "/tmp/fixture") as materialized:
            return materialized.path.read_bytes()
    if operation == "write":
        return manager.write_text_file(name, "/tmp/fixture", "fixture", owner="kali")
    if operation == "remove_file":
        return manager.remove_file(name, "/tmp/fixture", allowed_root="/tmp")
    if operation == "stop":
        return manager.cleanup_stopped(project)
    if operation.startswith("completed_"):
        manager.update_config(manager._config.model_copy(update={
            "completed_action": "remove" if operation.endswith("remove") else "stop",
        }))
        return manager.cleanup_completed(project)
    if operation == "remove":
        return manager.remove_container(name)
    if operation == "orphan":
        return manager.cleanup_orphan(name)
    if operation == "start":
        return manager._start_existing(name)
    raise AssertionError(operation)


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("corruption", ["owner", "owner_suffix", "engine", "missing", "project"])
def test_operations_refuse_unowned_name_even_if_prefix_matches(runtime, operation, corruption):
    manager, docker = runtime
    container = owned_container(manager, docker)
    labels = container.attrs["Config"]["Labels"]
    if corruption == "owner":
        labels["cairn.pi.instance_id"] = OTHER_ID
    elif corruption == "owner_suffix":
        labels["cairn.pi.instance_id"] = INSTANCE_ID[:16] + "0" * 48
    elif corruption == "engine":
        labels["cairn.engine"] = "legacy"
    elif corruption == "project":
        labels["cairn.project_id"] = "proj_other"
    else:
        labels.clear()
    with pytest.raises(ContainerOwnershipError):
        operate(manager, container.name, operation)
    assert container.actions == docker.runs == []


@pytest.mark.parametrize("name", [
    "cairn-dispatch-proj_a", "cairn-startup-healthcheck-0123456789ab",
    f"cairn-pi-dispatch-{OTHER_ID[:16]}-proj_a", "unrelated-container",
])
@pytest.mark.parametrize("operation", ["exec", "read", "write", "remove_file", "remove", "orphan", "start"])
def test_matching_labels_do_not_claim_legacy_or_foreign_namespace(runtime, name, operation):
    manager, docker = runtime
    container = owned_container(manager, docker)
    container.name = name
    with pytest.raises(ContainerOwnershipError):
        operate(manager, name, operation)
    assert manager.project_id_from_container_name(name) is None
    assert manager.managed_container_names() == []
    assert container.actions == []


@pytest.mark.parametrize("operation", OPERATIONS)
def test_owned_operations_remain_available(runtime, operation):
    manager, docker = runtime
    container = owned_container(manager, docker)
    result = operate(manager, container.name, operation)
    if operation in {"read", "materialize"}:
        assert result == b"fixture"
    elif operation in {"stop", "completed_stop"}:
        assert container.actions == ["stop"]
    elif operation in {"completed_remove", "remove", "orphan"}:
        assert container.actions == ["remove"]


@pytest.mark.parametrize("operation", ["reuse", "stop", "completed_stop", "completed_remove"])
def test_project_name_sanitization_cannot_claim_another_project(runtime, operation):
    manager, docker = runtime
    container = owned_container(manager, docker, "proj/a")
    with pytest.raises(ContainerOwnershipError):
        operate(manager, container.name, operation, project="proj-a")
    assert container.actions == docker.runs == []


@pytest.mark.parametrize("conflict", ["owned", "foreign", "runtime", "missing_signature"])
def test_name_conflict_rechecks_owner_and_runtime(runtime, monkeypatch, conflict):
    manager, docker = runtime

    def concurrent_creation(*args, **kwargs):
        container = owned_container(manager, docker, state="exited")
        labels = container.attrs["Config"]["Labels"]
        if conflict == "foreign":
            labels["cairn.pi.instance_id"] = OTHER_ID
        elif conflict == "runtime":
            labels["cairn.runtime_signature"] = "different"
        elif conflict == "missing_signature":
            labels.pop("cairn.runtime_signature")
        raise APIError("conflict", response=SimpleNamespace(status_code=409), explanation="name is already in use")

    monkeypatch.setattr(docker, "run", concurrent_creation)
    if conflict == "owned":
        assert manager.ensure_running("proj_a") == manager.container_name("proj_a")
        assert docker.objects[0].actions == ["start"]
    else:
        with pytest.raises(ContainerOwnershipError):
            manager.ensure_running("proj_a")
        assert docker.objects[0].actions == []


def test_concurrent_project_creation_preserves_worker_concurrency(runtime):
    manager, docker = runtime
    with ThreadPoolExecutor(max_workers=8) as executor:
        names = list(executor.map(lambda _: manager.ensure_running("proj_a"), range(16)))
    assert len(set(names)) == len(docker.runs) == 1


@pytest.mark.parametrize("operation", ["project", "startup"])
def test_container_creation_requires_exact_running_container(
    runtime,
    monkeypatch,
    operation,
):
    manager, docker = runtime
    monkeypatch.setattr(
        docker,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )

    with pytest.raises(RuntimeError, match="not running"):
        if operation == "project":
            manager.ensure_running("proj_a")
        else:
            manager.create_startup_container()


def test_start_existing_rechecks_exact_running_container(runtime, monkeypatch):
    manager, docker = runtime
    container = owned_container(manager, docker, state="exited")

    def no_op_start():
        container.actions.append("start")

    monkeypatch.setattr(container, "start", no_op_start)

    with pytest.raises(RuntimeError, match="did not reach running state"):
        manager.ensure_running("proj_a")
    assert container.actions == ["start"]


def test_orphan_reconciliation_ignores_legacy_foreign_and_healthcheck(runtime):
    manager, docker = runtime
    known = owned_container(manager, docker, "proj/known")
    orphan = owned_container(manager, docker, "proj_deleted")
    legacy = FakeContainer(docker, "cairn-dispatch-old", {})
    foreign_manager = ContainerManager(manager._config, instance_id=OTHER_ID)
    foreign = owned_container(foreign_manager, docker, "proj_deleted")
    health = docker.get(manager.create_startup_container())
    submitted = []

    def submit(function, *args):
        submitted.append((function, args))
        return Future()

    loop = object.__new__(DispatcherLoop)
    loop.container_manager = manager
    loop._cleanup_pending = set()
    loop.cleanup_futures = {}
    loop.cleanup_executor = SimpleNamespace(submit=submit)
    CleanupMixin._reconcile_orphan_containers(loop, [SimpleNamespace(id="proj/known")])
    assert len(submitted) == 1
    function, args = submitted[0]
    assert function == loop._run_candidate_protected_cleanup
    assert args[0] == orphan.name
    assert args[1].__self__ is manager
    assert args[1].__func__ is manager.cleanup_orphan.__func__
    assert args[2:] == (orphan.name,)
    assert docker.filters[-1]["filters"]["label"] == [
        f"cairn.pi.instance_id={INSTANCE_ID}", "cairn.engine=pi",
    ]
    # 排队后同名容器归属变化必须在真正执行remove前再次拒绝。
    orphan.attrs["Config"]["Labels"]["cairn.pi.instance_id"] = OTHER_ID
    function, args = submitted[0]
    with pytest.raises(ContainerOwnershipError):
        function(*args)
    assert all(container.actions == [] for container in (known, orphan, legacy, foreign, health))


def test_lookup_must_match_exact_name_or_full_container_id(runtime, monkeypatch):
    manager, docker = runtime
    container = owned_container(manager, docker)
    monkeypatch.setattr(docker, "get", lambda _: container)
    assert manager.build_exec_process(container.id, {}, ["true"]) is not None
    with pytest.raises(ContainerOwnershipError, match="lookup identity"):
        manager.remove_container("wrong-name")
    assert container.actions == []


def dispatch_config(**overrides):
    return DispatchConfig.model_validate({
        "server": SERVER, "dispatcher_id": "dispatcher-a",
        "runtime": {
            "max_workers": 8, "max_running_projects": 4, "max_project_workers": 4,
            "interval": 3, "healthcheck_timeout": 1, "prompt_group": "default",
        },
        "tasks": {
            "bootstrap": {"timeout": 1, "conclude_timeout": 1},
            "reason": {"timeout": 1},
            "explore": {"timeout": 1, "conclude_timeout": 1},
        },
        "container": {"image": "worker:test", "network_mode": "none", "completed_action": "stop"},
        "workers": [], **overrides,
    })


@pytest.fixture
def loop(runtime, tmp_path):
    manager, docker = runtime
    client = SimpleNamespace(
        base_url=SERVER, close=lambda: None,
        dispatcher_heartbeat=lambda **kwargs: ApiResult(
            status_code=200, data={"dispatcher_id": "dispatcher-a"},
        ),
        get_candidate_capabilities=lambda **_kwargs: {
            "required": True,
            "spool_protocol": "cairn-candidate-spool-v1",
            "frame_protocol": "cairn-candidate-frame-v1",
            "receipt_protocol": "cairn-candidate-capture-receipt-v1",
            "manifest_header": "X-Cairn-Candidate-Manifest",
            "manifest_encoding": "base64url-utf8-canonical-json-no-padding",
            "content_type": "application/octet-stream",
            "source_format": "pi-toolcall-delta-utf8-v1",
            "max_candidates_per_task_run": 16,
            "max_raw_bytes": 2 * 1024 * 1024,
            "max_manifest_bytes": 4 * 1024,
            "max_spool_bytes": 34 * 1024 * 1024,
            "max_outcomes_per_candidate": 64,
            "max_artifacts_per_candidate": 16,
        },
    )
    loop = DispatcherLoop(
        DispatcherBootstrap(config_path=tmp_path / "synthetic-config.json", hard_cap=8),
        config=dispatch_config(dispatcher_id="synthetic-display"), client=client,
    )
    assert loop.dispatcher_id is None and loop.container_manager is None
    loop.run_startup_healthchecks()
    assert loop.dispatcher_id == "dispatcher-a"
    yield loop
    loop.futures.clear()
    loop.close()


def test_loop_binds_owner_without_reducing_concurrency(loop):
    assert loop.container_manager.instance_id == INSTANCE_ID
    assert loop.executor._max_workers == 8
    assert loop.cleanup_executor._max_workers == 8
    assert loop.config.runtime.max_workers == 8


@pytest.mark.parametrize("force", [False, True])
def test_display_id_reload_preserves_authenticated_owner_and_namespace(loop, force):
    manager = loop.container_manager
    client = loop.client
    browser_sessions = manager.browser_sessions
    task = object()
    loop.futures[task] = SimpleNamespace(project_id="proj_a")
    changed = loop.config.model_copy(update={"dispatcher_id": "dispatcher-b"})

    loop._apply_config(changed, force=force, config_sha="changed")

    assert loop.config is changed
    assert loop.config.dispatcher_id == "dispatcher-b"
    assert loop.dispatcher_id == "dispatcher-a"
    assert loop.container_manager is manager and manager.instance_id == INSTANCE_ID
    assert manager.browser_sessions is browser_sessions
    assert loop.client is client and task in loop.futures
    assert loop._last_config_hash == "changed"


@pytest.mark.parametrize("force", [False, True])
def test_server_identity_reload_requires_user_restart_without_side_effects(loop, force, monkeypatch):
    change = {"server": "https://other.example.test/api"}
    previous = loop.config
    manager = loop.container_manager
    client = loop.client
    digest = loop._last_config_hash
    task = object()
    loop.futures[task] = SimpleNamespace(project_id="proj_a")
    calls = []
    monkeypatch.setattr(manager, "update_config", lambda config: calls.append("update"))
    monkeypatch.setattr(manager, "close", lambda: calls.append("close"))
    monkeypatch.setattr(client, "close", lambda: calls.append("client-close"))
    with pytest.raises(RuntimeError, match="restart the dispatcher explicitly"):
        loop._apply_config(previous.model_copy(update=change), force=force, config_sha="changed")
    assert loop.config is previous and loop.container_manager is manager and loop.client is client
    assert loop._last_config_hash == digest and loop.dispatcher_id == "dispatcher-a"
    assert task in loop.futures
    assert calls == []


def test_refresh_rejects_identity_change_but_keeps_running_config(loop, monkeypatch):
    previous = loop.config
    loop._config_path.write_text('{"synthetic": true}', encoding="utf-8")
    changed = previous.model_copy(update={"server": "https://other.example.test/api"})
    monkeypatch.setattr(DispatchConfig, "load", classmethod(lambda cls, path: changed))
    assert loop._refresh_config() is False
    assert loop.config is previous
    assert "restart the dispatcher explicitly" in loop._config_error_message
    assert loop.container_manager.instance_id == INSTANCE_ID


def test_nonidentity_reload_preserves_manager_and_browser_bindings(loop, monkeypatch):
    manager = loop.container_manager
    marker = manager.browser_sessions
    changed = loop.config.model_copy(update={
        "runtime": loop.config.runtime.model_copy(update={"interval": 7, "playwright_session_policy": "task"}),
        "common_env": {"API_KEY": "rotated-synthetic-secret"},
    })
    loop._apply_config(changed, force=False, config_sha="changed")
    assert loop.container_manager is manager and manager.browser_sessions is marker
    assert manager.instance_id == INSTANCE_ID
    assert manager._browser_session_policy == "task" and loop.config.runtime.interval == 7
    assert loop.config.runtime.max_workers == 8
    # 等价Server写法和URL凭据轮换不会切换容器owner。
    monkeypatch.setattr("cairn.dispatcher.scheduler.config_runtime.CairnClient", lambda url: SimpleNamespace(
        base_url=url.rstrip("/"), close=lambda: None,
    ))
    equivalent = changed.model_copy(update={"server": "https://fixture:rotated@SERVER.example.test:443/api/"})
    loop._apply_config(equivalent, force=False, config_sha="equivalent")
    assert loop.container_manager is manager and manager.instance_id == INSTANCE_ID


def test_candidate_execution_stopped_requires_exact_exec_and_process_group_proof(
    runtime,
    monkeypatch,
):
    from cairn.dispatcher.runtime.exec_control import EXEC_VERIFY_EXIT

    manager, docker = runtime
    container = owned_container(manager, docker)
    docker.api.exec_inspect = lambda exec_id: {
        "ContainerID": container.id,
        "Running": False,
        "ExitCode": 137,
    }
    controls = []

    def verify(container_id, command):
        controls.append((container_id, command))
        return 0

    monkeypatch.setattr(manager, "_run_candidate_control", verify)

    result = manager.candidate_execution_status(
        docker_daemon_id="daemon-1",
        container_id=container.id,
        container_name=container.name,
        project_id="proj_a",
        exec_id="exec-id",
        control_path=(
            "/tmp/cairn-exec-"
            "0123456789abcdef0123456789abcdef.json"
        ),
    )
    assert result.status == "stopped"
    assert result.reason == "exec_terminal_process_group_drained"
    assert result.exit_code == 137
    assert controls == [
        (
            container.id,
            [
                "python3",
                "-c",
                EXEC_VERIFY_EXIT,
                (
                    "/tmp/cairn-exec-"
                    "0123456789abcdef0123456789abcdef.json"
                ),
                "137",
            ],
        )
    ]


@pytest.mark.parametrize(
    "state",
    [
        {"ContainerID": "other", "Running": False, "ExitCode": 0},
        {"ContainerID": "container-0", "Running": True, "ExitCode": 0},
        {"ContainerID": "container-0", "Running": False, "ExitCode": None},
        {"ContainerID": "container-0", "Running": False, "ExitCode": True},
        {"ContainerID": "container-0", "Running": False, "ExitCode": -1},
        {"ContainerID": "container-0", "Running": False, "ExitCode": "0"},
    ],
)
def test_candidate_execution_stopped_rejects_ambiguous_exec_state(
    runtime,
    monkeypatch,
    state,
):
    manager, docker = runtime
    container = owned_container(manager, docker)
    docker.api.exec_inspect = lambda exec_id: state
    monkeypatch.setattr(
        manager,
        "_run_candidate_control",
        lambda *_args: pytest.fail("ambiguous exec state must block proof"),
    )

    result = manager.candidate_execution_status(
        docker_daemon_id="daemon-1",
        container_id=container.id,
        container_name=container.name,
        project_id="proj_a",
        exec_id="exec-id",
        control_path=(
            "/tmp/cairn-exec-"
            "0123456789abcdef0123456789abcdef.json"
        ),
    )
    assert result.status == (
        "running"
        if state.get("ContainerID") == container.id and state.get("Running") is True
        else "unknown"
    )


@pytest.mark.parametrize(
    ("container_name", "project_id", "control_path"),
    [
        ("wrong-name", "proj_a", "/tmp/cairn-exec-0123456789abcdef0123456789abcdef.json"),
        ("unused", "other-project", "/tmp/cairn-exec-0123456789abcdef0123456789abcdef.json"),
        ("unused", "proj_a", "/tmp/not-controlled.json"),
    ],
)
def test_candidate_execution_stopped_rejects_wrong_authority(
    runtime,
    container_name,
    project_id,
    control_path,
):
    manager, docker = runtime
    container = owned_container(manager, docker)
    if container_name == "unused":
        container_name = container.name
    docker.api.exec_inspect = lambda exec_id: {
        "ContainerID": container.id,
        "Running": False,
        "ExitCode": 0,
    }

    result = manager.candidate_execution_status(
        docker_daemon_id="daemon-1",
        container_id=container.id,
        container_name=container_name,
        project_id=project_id,
        exec_id="exec-id",
        control_path=control_path,
    )
    assert result.status == "unknown"


def test_candidate_execution_status_distinguishes_missing_container_and_daemon_change(
    runtime,
    monkeypatch,
):
    manager, docker = runtime

    gone = manager.candidate_execution_status(
        docker_daemon_id="daemon-1",
        container_id="missing-container",
        container_name=manager.container_name("proj_a"),
        project_id="proj_a",
        exec_id="exec-id",
        control_path="/tmp/cairn-exec-0123456789abcdef0123456789abcdef.json",
    )
    assert gone.status == "gone"
    assert gone.reason == "container_missing_on_same_daemon"

    docker.daemon_id = "daemon-2"
    changed = manager.candidate_execution_status(
        docker_daemon_id="daemon-1",
        container_id="missing-container",
        container_name=manager.container_name("proj_a"),
        project_id="proj_a",
        exec_id="exec-id",
        control_path="/tmp/cairn-exec-0123456789abcdef0123456789abcdef.json",
    )
    assert changed.status == "unknown"
    assert changed.reason == "docker_daemon_changed"

    def unavailable():
        raise RequestException("injected daemon outage")

    monkeypatch.setattr(docker, "info", unavailable)
    unavailable_result = manager.candidate_execution_status(
        docker_daemon_id="daemon-1",
        container_id="missing-container",
        container_name=manager.container_name("proj_a"),
        project_id="proj_a",
        exec_id="exec-id",
        control_path="/tmp/cairn-exec-0123456789abcdef0123456789abcdef.json",
    )
    assert unavailable_result.status == "unknown"
    assert unavailable_result.reason == "docker_daemon_unavailable"


@pytest.mark.parametrize(
    ("container_state", "proof_exit", "expected_status", "expected_reason"),
    [
        ("running", 0, "stopped", "exec_missing_process_group_drained"),
        ("running", 1, "unknown", "exec_missing_process_unconfirmed"),
        ("exited", None, "stopped", "container_not_running"),
    ],
)
def test_candidate_execution_status_handles_missing_exec_record(
    runtime,
    monkeypatch,
    container_state,
    proof_exit,
    expected_status,
    expected_reason,
):
    manager, docker = runtime
    container = owned_container(manager, docker, state=container_state)

    def missing_exec(_exec_id):
        raise NotFound("missing fake exec")

    docker.api.exec_inspect = missing_exec
    if proof_exit is None:
        monkeypatch.setattr(
            manager,
            "_run_candidate_control",
            lambda *_args: pytest.fail("stopped container needs no live proof exec"),
        )
    else:
        monkeypatch.setattr(manager, "_run_candidate_control", lambda *_args: proof_exit)

    result = manager.candidate_execution_status(
        docker_daemon_id="daemon-1",
        container_id=container.id,
        container_name=container.name,
        project_id="proj_a",
        exec_id="exec-id",
        control_path="/tmp/cairn-exec-0123456789abcdef0123456789abcdef.json",
    )

    assert result.status == expected_status
    assert result.reason == expected_reason


def test_candidate_execution_status_uses_exact_stopped_container_even_with_retained_exec(runtime):
    manager, docker = runtime
    container = owned_container(manager, docker, state="exited")
    docker.api.exec_inspect = lambda _exec_id: pytest.fail("stopped container must not start a verifier")

    result = manager.candidate_execution_status(
        docker_daemon_id="daemon-1",
        container_id=container.id,
        container_name=container.name,
        project_id="proj_a",
        exec_id="retained-exec-id",
        control_path="/tmp/cairn-exec-0123456789abcdef0123456789abcdef.json",
    )

    assert result.status == "stopped"
    assert result.reason == "container_not_running"
    assert container.actions == []
    assert manager.read_file(container.id, "/tmp/fixture") == b"fixture"
    assert container.actions == [("read", "/tmp/fixture")]
    assert docker.runs == []


def test_candidate_execution_stopped_requires_successful_verifier(
    runtime,
    monkeypatch,
):
    manager, docker = runtime
    container = owned_container(manager, docker)
    docker.api.exec_inspect = lambda exec_id: {
        "ContainerID": container.id,
        "Running": False,
        "ExitCode": 0,
    }
    monkeypatch.setattr(manager, "_run_candidate_control", lambda *_args: 1)

    result = manager.candidate_execution_status(
        docker_daemon_id="daemon-1",
        container_id=container.id,
        container_name=container.name,
        project_id="proj_a",
        exec_id="exec-id",
        control_path=(
            "/tmp/cairn-exec-"
            "0123456789abcdef0123456789abcdef.json"
        ),
    )
    assert result.status == "unknown"
    assert result.reason == "termination_proof_unavailable"


def test_candidate_control_cleanup_uses_exact_owned_container(
    runtime,
    monkeypatch,
):
    from cairn.dispatcher.runtime.exec_control import EXEC_CLEANUP

    manager, docker = runtime
    container = owned_container(manager, docker)
    controls = []

    def cleanup(container_id, command):
        controls.append((container_id, command))
        return 0

    monkeypatch.setattr(manager, "_run_candidate_control", cleanup)
    control_path = (
        "/tmp/cairn-exec-0123456789abcdef0123456789abcdef.json"
    )

    manager.cleanup_candidate_execution_control(
        container_id=container.id,
        container_name=container.name,
        project_id="proj_a",
        control_path=control_path,
    )

    assert controls == [
        (container.id, ["python3", "-c", EXEC_CLEANUP, control_path])
    ]


@pytest.mark.parametrize("status", [None, 1])
def test_candidate_control_cleanup_fails_when_helper_is_not_successful(
    runtime,
    monkeypatch,
    status,
):
    manager, docker = runtime
    container = owned_container(manager, docker)
    monkeypatch.setattr(
        manager,
        "_run_candidate_control",
        lambda *_args: status,
    )

    with pytest.raises(RuntimeError, match="failed to clean"):
        manager.cleanup_candidate_execution_control(
            container_id=container.id,
            container_name=container.name,
            project_id="proj_a",
            control_path=(
                "/tmp/cairn-exec-"
                "0123456789abcdef0123456789abcdef.json"
            ),
        )


def test_candidate_control_helper_rejects_invalid_identity_and_exit_code(
    runtime,
):
    manager, docker = runtime
    docker.api.exec_create = lambda *_args, **_kwargs: {}

    with pytest.raises(RuntimeError, match="identity is unavailable"):
        manager._run_candidate_control("container-0", ["true"])

    docker.api.exec_create = lambda *_args, **_kwargs: {"Id": "control-id"}
    docker.api.exec_start = lambda *_args, **_kwargs: None
    docker.api.exec_inspect = lambda *_args, **_kwargs: {
        "ContainerID": "container-0",
        "Running": False,
        "ExitCode": False,
    }
    with pytest.raises(RuntimeError, match="exit code is invalid"):
        manager._run_candidate_control("container-0", ["true"])


def test_candidate_control_helper_rejects_foreign_container(runtime):
    manager, docker = runtime
    docker.api.exec_create = lambda *_args, **_kwargs: {"Id": "control-id"}
    docker.api.exec_start = lambda *_args, **_kwargs: None
    docker.api.exec_inspect = lambda *_args, **_kwargs: {
        "ContainerID": "foreign-container",
        "Running": False,
        "ExitCode": 0,
    }

    with pytest.raises(RuntimeError, match="container identity"):
        manager._run_candidate_control("container-0", ["true"])


def test_candidate_control_helper_times_out_without_terminal_state(
    runtime,
    monkeypatch,
):
    manager, docker = runtime
    docker.api.exec_create = lambda *_args, **_kwargs: {"Id": "control-id"}
    docker.api.exec_start = lambda *_args, **_kwargs: None
    docker.api.exec_inspect = lambda *_args, **_kwargs: {
        "ContainerID": "container-0",
        "Running": True,
        "ExitCode": None,
    }
    times = iter([0.0, 0.0, 3.0])
    monkeypatch.setattr(
        "cairn.dispatcher.runtime.containers.time.monotonic",
        lambda: next(times),
    )
    monkeypatch.setattr(
        "cairn.dispatcher.runtime.containers.time.sleep",
        lambda _seconds: None,
    )

    assert manager._run_candidate_control("container-0", ["true"]) is None
