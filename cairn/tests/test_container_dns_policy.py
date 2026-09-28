from __future__ import annotations

from types import SimpleNamespace

import pytest
from docker.errors import NotFound
from pydantic import ValidationError

from cairn.dispatcher.config import ContainerConfig
from cairn.dispatcher.runtime.container_options import ContainerRuntimeOptions
from cairn.dispatcher.runtime.containers import ContainerManager, container_instance_id


INSTANCE_ID = container_instance_id("dns-policy-test", "https://server.example.test")


def recording_docker_client(calls: list[dict[str, object]]) -> SimpleNamespace:
    class FakeContainer:
        def __init__(self, name: str, labels: dict[str, str]) -> None:
            self.name = name
            self.id = f"container-{len(containers.objects)}"
            self.attrs = {
                "Config": {"Labels": dict(labels)},
                "State": {"Status": "running"},
            }

        def reload(self) -> None:
            return None

    class FakeContainers:
        def __init__(self) -> None:
            self.objects: dict[str, FakeContainer] = {}

        def get(self, name: str) -> FakeContainer:
            try:
                return self.objects[name]
            except KeyError:
                raise NotFound("container not found") from None

        def run(self, _image, _command, **kwargs):
            calls.append(kwargs)
            container = FakeContainer(kwargs["name"], kwargs["labels"])
            self.objects[container.name] = container
            return container

    containers = FakeContainers()
    return SimpleNamespace(containers=containers, close=lambda: None)


def container_config(**overrides) -> ContainerConfig:
    values = {
        "image": "worker:test",
        "network_mode": "host",
        "completed_action": "stop",
        "dns": [],
    }
    values.update(overrides)
    return ContainerConfig(**values)


def runtime_options(
    *,
    dns: list[str],
    mem_limit: int | None = None,
    memswap_limit: int | None = None,
    pids_limit: int | None = None,
) -> ContainerRuntimeOptions:
    return ContainerRuntimeOptions.build(
        image="worker:test",
        network_mode="host",
        dns=dns,
        cap_add=[],
        features={},
        scenario_key="default-copy-v2",
        mem_limit=mem_limit,
        memswap_limit=memswap_limit,
        pids_limit=pids_limit,
    )


def test_container_dns_is_normalized_and_deduplicated() -> None:
    config = container_config(dns=[" 192.168.19.2 ", "2001:0db8::1", "192.168.19.2"])

    assert config.dns == ["192.168.19.2", "2001:db8::1"]


@pytest.mark.parametrize(
    "dns",
    [
        [""],
        ["resolver.example.test"],
        ["https://192.168.19.2/dns-query"],
        ["0.0.0.0"],
        ["::"],
        ["224.0.0.1"],
        ["ff02::1"],
    ],
)
def test_container_dns_rejects_invalid_entries(dns: list[str]) -> None:
    with pytest.raises(ValidationError):
        container_config(dns=dns)


def test_dns_changes_container_runtime_signature() -> None:
    default = runtime_options(dns=[])
    configured = runtime_options(dns=["192.168.19.2"])

    assert default.dns == ()
    assert configured.dns == ("192.168.19.2",)
    assert default.runtime_signature != configured.runtime_signature


def test_container_manager_propagates_dns_to_project_and_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    fake_client = recording_docker_client(calls)
    monkeypatch.setattr("cairn.dispatcher.runtime.containers.docker.from_env", lambda *, timeout: fake_client)

    manager = ContainerManager(
        container_config(dns=["192.168.19.2", "2001:db8::1"]), instance_id=INSTANCE_ID,
    )
    monkeypatch.setattr(manager, "_advisory_volume_bind", lambda _options: {})

    manager.ensure_running("proj_dns", runtime_options(dns=["192.168.19.2", "2001:db8::1"]))
    manager.create_startup_container()

    assert calls[0]["dns"] == ["192.168.19.2", "2001:db8::1"]
    assert calls[1]["dns"] == ["192.168.19.2", "2001:db8::1"]


def test_running_container_with_stale_runtime_signature_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeContainer:
        name = f"cairn-pi-dispatch-{INSTANCE_ID[:16]}-proj_bounded"
        attrs = {
            "State": {"Status": "running"},
            "Config": {"Labels": {
                "cairn.pi.instance_id": INSTANCE_ID,
                "cairn.engine": "pi",
                "cairn.project_id": "proj_bounded",
            }},
        }

        def reload(self) -> None:
            return None

    fake_client = SimpleNamespace(
        containers=SimpleNamespace(),
        close=lambda: None,
    )
    monkeypatch.setattr(
        "cairn.dispatcher.runtime.containers.docker.from_env",
        lambda *, timeout: fake_client,
    )
    manager = ContainerManager(container_config(), instance_id=INSTANCE_ID)
    monkeypatch.setattr(manager, "_get_container", lambda _name: FakeContainer())

    with pytest.raises(RuntimeError, match="runtime differs from scenario"):
        manager.ensure_running(
            "proj_bounded",
            runtime_options(
                dns=[],
                mem_limit=1536 * 1024 * 1024,
                memswap_limit=1536 * 1024 * 1024,
                pids_limit=256,
            ),
        )


def test_project_limits_do_not_apply_to_startup_container(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    fake_client = recording_docker_client(calls)
    monkeypatch.setattr("cairn.dispatcher.runtime.containers.docker.from_env", lambda *, timeout: fake_client)

    manager = ContainerManager(container_config(), instance_id=INSTANCE_ID)
    monkeypatch.setattr(manager, "_advisory_volume_bind", lambda _options: {})
    options = runtime_options(
        dns=[],
        mem_limit=1536 * 1024 * 1024,
        memswap_limit=1536 * 1024 * 1024,
        pids_limit=256,
    )

    manager.ensure_running("proj_bounded", options)
    manager.create_startup_container()

    assert calls[0]["mem_limit"] == 1536 * 1024 * 1024
    assert calls[0]["memswap_limit"] == 1536 * 1024 * 1024
    assert calls[0]["pids_limit"] == 256
    assert "mem_limit" not in calls[1]
    assert "memswap_limit" not in calls[1]
    assert "pids_limit" not in calls[1]


def test_empty_dns_preserves_docker_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    fake_client = recording_docker_client(calls)
    monkeypatch.setattr("cairn.dispatcher.runtime.containers.docker.from_env", lambda *, timeout: fake_client)

    manager = ContainerManager(container_config(), instance_id=INSTANCE_ID)
    monkeypatch.setattr(manager, "_advisory_volume_bind", lambda _options: {})

    manager.ensure_running("proj_default")
    manager.create_startup_container()

    assert calls[0]["dns"] is None
    assert calls[1]["dns"] is None
