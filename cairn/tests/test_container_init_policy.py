from __future__ import annotations

from types import SimpleNamespace

import pytest
from docker.errors import NotFound

from cairn.dispatcher.config import ContainerConfig
from cairn.dispatcher.runtime.containers import ContainerManager, container_instance_id


INSTANCE_ID = container_instance_id("init-policy-test", "https://server.example.test")


def test_new_project_and_startup_containers_enable_init(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

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

        def get(self, name):
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
    fake_client = SimpleNamespace(containers=containers, close=lambda: None)
    monkeypatch.setattr("cairn.dispatcher.runtime.containers.docker.from_env", lambda *, timeout: fake_client)
    manager = ContainerManager(
        ContainerConfig(image="worker:test", network_mode="host", completed_action="stop", cap_add=[], dns=[]),
        instance_id=INSTANCE_ID,
    )

    project_name = manager.ensure_running("proj_init")
    startup_name = manager.create_startup_container()

    assert len(calls) == 2
    assert calls[0]["name"] == project_name
    assert calls[1]["name"] == startup_name
    assert [call.get("init") for call in calls] == [True, True]


@pytest.mark.parametrize(("state", "expected_starts"), [("running", 0), ("exited", 1)])
def test_owned_container_without_init_keeps_runtime_signature_reuse(
    monkeypatch: pytest.MonkeyPatch, state: str, expected_starts: int
) -> None:
    calls: list[dict[str, object]] = []

    class FakeContainer:
        name = f"cairn-pi-dispatch-{INSTANCE_ID[:16]}-proj_init"

        def __init__(self):
            self.id = "existing-container"
            self.starts = 0
            self.removals: list[bool] = []
            self.attrs = {
                "State": {"Status": state},
                "HostConfig": {"Init": False},
                "Config": {
                    "Labels": {
                        "cairn.pi.instance_id": INSTANCE_ID,
                        "cairn.engine": "pi",
                        "cairn.project_id": "proj_init",
                        # 仅本owner容器可复用；固定旧签名验证init不触发重建。
                        "cairn.runtime_signature": "54f159ce611e7e024e8dd4e0773d09b879fd73248d6487ccf0cacbdf09c39e09",
                    },
                },
            }

        def reload(self) -> None:
            return None

        def start(self) -> None:
            self.starts += 1
            self.attrs["State"]["Status"] = "running"

        def remove(self, *, force: bool) -> None:
            self.removals.append(force)

    existing = FakeContainer()

    class FakeContainers:
        def get(self, name):
            assert name == existing.name
            return existing

        def run(self, _image, _command, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace()

    fake_client = SimpleNamespace(containers=FakeContainers(), close=lambda: None)
    monkeypatch.setattr("cairn.dispatcher.runtime.containers.docker.from_env", lambda *, timeout: fake_client)
    manager = ContainerManager(
        ContainerConfig(image="worker:test", network_mode="host", completed_action="stop", cap_add=[], dns=[]),
        instance_id=INSTANCE_ID,
    )

    assert manager.ensure_running("proj_init") == existing.name
    assert existing.starts == expected_starts
    assert existing.attrs["State"]["Status"] == "running"
    assert existing.attrs["HostConfig"]["Init"] is False
    assert existing.removals == []
    assert calls == []
