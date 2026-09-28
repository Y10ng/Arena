from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from cairn.dispatcher import advisory_bundle
from cairn.dispatcher.advisory_bundle import stage_advisory_bundle
from cairn.dispatcher.prompting import knowledge_base_runtime_context
from cairn.dispatcher.runtime.container_options import ContainerRuntimeOptions
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.server.models import LibraryFile, ProjectLibraryBinding, ProjectResource
from cairn.server.services.runtime_context import sanitize_task_run_runtime_context


class ResourceClient:
    def __init__(
        self,
        resources: list[ProjectResource],
        contents: dict[str, bytes],
        *,
        bindings: list[ProjectLibraryBinding] | None = None,
        library_files: dict[tuple[str, int], list[LibraryFile]] | None = None,
        library_contents: dict[tuple[str, int, str], bytes] | None = None,
    ) -> None:
        self.resources = resources
        self.contents = contents
        self.bindings = bindings or []
        self.library_files = library_files or {}
        self.library_contents = library_contents or {}

    def list_project_library_bindings(
        self,
        project_id: str,
    ) -> list[ProjectLibraryBinding]:
        return self.bindings

    def list_project_library_files(
        self,
        project_id: str,
        key: str,
        version: int,
    ) -> list[LibraryFile]:
        return self.library_files[(key, version)]

    def read_project_library_file(
        self,
        project_id: str,
        key: str,
        version: int,
        path: str,
    ) -> bytes:
        return self.library_contents[(key, version, path)]

    def list_project_resources(self, project_id: str) -> list[ProjectResource]:
        return self.resources

    def read_project_resource(self, project_id: str, resource_id: str) -> bytes:
        return self.contents[resource_id]


def _library_digest(files: dict[str, bytes]) -> str:
    digest = sha256()
    for path, content in sorted(files.items()):
        path_bytes = path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _resource(
    path: str,
    content: bytes,
    *,
    resource_id: str = "r001",
    kind: str = "script",
) -> ProjectResource:
    return ProjectResource(
        id=resource_id,
        project_id="p1",
        kind=kind,
        path=path,
        size_bytes=len(content),
        sha256=sha256(content).hexdigest(),
        active=True,
        created_by="u1",
        created_at="2026-08-19T00:00:00Z",
    )


def _runtime(bundle=None) -> ContainerRuntimeOptions:
    return ContainerRuntimeOptions.build(
        image="cairn:test",
        network_mode="bridge",
        dns=[],
        cap_add=[],
        features={},
        scenario_key="default-copy-v2",
        advisory_bundle=bundle,
    )


def test_none_knowledge_and_no_resources_produce_no_mount(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(advisory_bundle.ADVISORY_ROOT_ENV_VAR, str(tmp_path / "stage"))
    client = ResourceClient([], {})

    assert stage_advisory_bundle(client, "p1", None) is None
    context = knowledge_base_runtime_context(None, advisory=_runtime())

    assert context["enabled"] is False
    assert context["path"] is None
    assert context["resources"] == []


def test_project_resource_is_staged_exactly_and_changes_runtime_signature(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = b"username=tester\npassword=allowed-secret\n"
    resource = _resource("auth/login.py", content)
    monkeypatch.setenv(advisory_bundle.ADVISORY_ROOT_ENV_VAR, str(tmp_path / "stage"))

    bundle = stage_advisory_bundle(ResourceClient([resource], {resource.id: content}), "p1", None)

    assert bundle is not None
    stage = Path(bundle.source_path)
    assert (stage / "project/auth/login.py").read_bytes() == content
    assert stage.stat().st_mode & 0o222 == 0
    assert (stage / "project/auth/login.py").stat().st_mode & 0o222 == 0
    manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == 3
    assert manifest["project_resources"][0]["sha256"] == sha256(content).hexdigest()
    assert "content" not in manifest["project_resources"][0]
    runtime = _runtime(bundle)
    assert runtime.runtime_signature != _runtime().runtime_signature
    assert ContainerManager._advisory_volume_bind(runtime) == {
        str(stage): {"bind": "/cairn/advisory", "mode": "ro"}
    }
    assert ContainerManager._advisory_volume_bind(_runtime()) is None


def test_binary_project_resource_is_staged_without_text_decoding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = b"\x00\xff\x89PNG\r\n\x1a\n"
    resource = _resource("images/diagram.png", content, kind="file")
    monkeypatch.setenv(advisory_bundle.ADVISORY_ROOT_ENV_VAR, str(tmp_path / "stage"))

    bundle = stage_advisory_bundle(ResourceClient([resource], {resource.id: content}), "p1", None)

    assert bundle is not None
    stage = Path(bundle.source_path)
    assert (stage / "project/images/diagram.png").read_bytes() == content
    manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == 3
    assert manifest["project_resources"][0]["kind"] == "file"
    assert (stage / "project/images/diagram.png").stat().st_mode & 0o222 == 0


def test_frozen_library_is_staged_and_disables_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = b"\xef\xbb\xbfuser=admin\npassword=allowed-secret\n"
    files = {"auth/login.txt": content}
    binding = ProjectLibraryBinding(
        library_key="runbook",
        library_name="Runbook",
        version=3,
        digest_sha256=_library_digest(files),
        file_count=1,
    )
    metadata = LibraryFile(
        path="auth/login.txt",
        size_bytes=len(content),
        sha256=sha256(content).hexdigest(),
    )
    root = tmp_path / "knowledge"
    (root / "legacy").mkdir(parents=True)
    (root / "legacy/old.txt").write_text("old", encoding="utf-8")
    monkeypatch.setattr(advisory_bundle, "resolved_knowledge_base_path", lambda: str(root))
    monkeypatch.setenv(advisory_bundle.ADVISORY_ROOT_ENV_VAR, str(tmp_path / "stage"))
    client = ResourceClient(
        [],
        {},
        bindings=[binding],
        library_files={("runbook", 3): [metadata]},
        library_contents={("runbook", 3, "auth/login.txt"): content},
    )

    bundle = stage_advisory_bundle(client, "p1", "legacy")

    assert bundle is not None
    stage = Path(bundle.source_path)
    assert (stage / "library/runbook/auth/login.txt").read_bytes() == content
    assert not (stage / "legacy").exists()
    assert bundle.legacy_key is None
    assert bundle.manifest["libraries"][0]["version"] == 3
    assert "content" not in json.dumps(bundle.manifest)
    context = knowledge_base_runtime_context(None, advisory=_runtime(bundle))
    assert context["resources"] == [
        {
            "source": "advisory_library",
            "kind": "note",
            "library_key": "runbook",
            "library_version": 3,
            "library_digest": binding.digest_sha256,
            "path": "auth/login.txt",
            "container_path": "/cairn/advisory/library/runbook/auth/login.txt",
            "size_bytes": len(content),
            "sha256": metadata.sha256,
        }
    ]


def test_selected_legacy_set_does_not_stage_siblings_or_nested_root_sets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "knowledge"
    (root / "chosen").mkdir(parents=True)
    (root / "sibling").mkdir()
    (root / "root.md").write_text("root", encoding="utf-8")
    (root / "chosen/only.md").write_text("chosen", encoding="utf-8")
    (root / "sibling/private.md").write_text("sibling", encoding="utf-8")
    monkeypatch.setattr(advisory_bundle, "resolved_knowledge_base_path", lambda: str(root))
    monkeypatch.setenv(advisory_bundle.ADVISORY_ROOT_ENV_VAR, str(tmp_path / "stage"))
    client = ResourceClient([], {})

    chosen = stage_advisory_bundle(client, "p1", "chosen")
    root_bundle = stage_advisory_bundle(client, "p2", "knowledge_base")

    assert chosen is not None
    assert (Path(chosen.source_path) / "legacy/chosen/only.md").is_file()
    assert not (Path(chosen.source_path) / "legacy/sibling").exists()
    assert root_bundle is not None
    assert (Path(root_bundle.source_path) / "legacy/knowledge_base/root.md").is_file()
    assert not (Path(root_bundle.source_path) / "legacy/knowledge_base/chosen").exists()


def test_legacy_symlink_and_resource_hash_mismatch_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "knowledge"
    (root / "chosen").mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (root / "chosen/link.txt").symlink_to(outside)
    monkeypatch.setattr(advisory_bundle, "resolved_knowledge_base_path", lambda: str(root))
    monkeypatch.setenv(advisory_bundle.ADVISORY_ROOT_ENV_VAR, str(tmp_path / "stage"))

    with pytest.raises(RuntimeError, match="symlink"):
        stage_advisory_bundle(ResourceClient([], {}), "p1", "chosen")

    expected = b"expected"
    resource = _resource("note.txt", expected)
    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        stage_advisory_bundle(
            ResourceClient([resource], {resource.id: b"tampered"}),
            "p2",
            None,
        )

    staged = stage_advisory_bundle(
        ResourceClient([resource], {resource.id: expected}),
        "p3",
        None,
    )
    assert staged is not None
    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        # 已存在同 digest bundle 时仍必须从协议读取并验证当前字节。
        stage_advisory_bundle(
            ResourceClient([resource], {resource.id: b"tampered"}),
            "p3",
            None,
        )

    with pytest.raises(RuntimeError, match="size mismatch"):
        stage_advisory_bundle(
            ResourceClient([resource], {resource.id: b"x"}),
            "p4",
            None,
        )


def test_runtime_context_sanitizer_keeps_only_advisory_metadata() -> None:
    digest = "a" * 64
    resource_digest = "b" * 64
    context = sanitize_task_run_runtime_context(
        {
            "knowledge_base": {
                "enabled": True,
                "injected": True,
                "path": "/cairn/advisory",
                "manifest_path": "/cairn/advisory/manifest.json",
                "bundle_digest": digest,
                "mode": "project_advisory_bundle",
                "read_only": True,
                "content": "must-not-survive",
                "resources": [
                    {
                        "source": "project_resource",
                        "id": "r001",
                        "kind": "script",
                        "path": "auth/login.py",
                        "container_path": "/cairn/advisory/project/auth/login.py",
                        "size_bytes": 12,
                        "sha256": resource_digest,
                        "preview": "must-not-survive",
                    },
                    {
                        "source": "project_resource",
                        "id": "r002",
                        "kind": "note",
                        "path": "bad.txt",
                        "container_path": "/tmp/bad.txt",
                        "size_bytes": 1,
                        "sha256": resource_digest,
                    },
                    {
                        "source": "advisory_library",
                        "kind": "note",
                        "library_key": "runbook",
                        "library_version": 2,
                        "library_digest": digest,
                        "path": "auth/login.txt",
                        "container_path": "/cairn/advisory/library/runbook/auth/login.txt",
                        "size_bytes": 8,
                        "sha256": resource_digest,
                        "content": "must-not-survive",
                    },
                ],
            },
            "project_resources": {
                "bundle_digest": digest,
                "manifest_path": "/cairn/advisory/manifest.json",
                "read_only": True,
                "host_path": "/tmp/cairn-advisory/p1",
                "resources": [],
            },
            "custom_runtime_field": {"preserved": True},
        }
    )

    knowledge = context["knowledge_base"]
    assert isinstance(knowledge, dict)
    assert "content" not in knowledge
    assert knowledge["resources"] == [
        {
            "source": "project_resource",
            "id": "r001",
            "kind": "script",
            "path": "auth/login.py",
            "container_path": "/cairn/advisory/project/auth/login.py",
            "size_bytes": 12,
            "sha256": resource_digest,
        },
        {
            "source": "advisory_library",
            "kind": "note",
            "library_key": "runbook",
            "library_version": 2,
            "library_digest": digest,
            "path": "auth/login.txt",
            "container_path": "/cairn/advisory/library/runbook/auth/login.txt",
            "size_bytes": 8,
            "sha256": resource_digest,
        },
    ]
    assert context["project_resources"] == {
        "bundle_digest": digest,
        "manifest_path": "/cairn/advisory/manifest.json",
        "read_only": True,
        "resources": [],
    }
    assert context["custom_runtime_field"] == {"preserved": True}


def test_runtime_context_sanitizer_disables_host_mount_path() -> None:
    context = sanitize_task_run_runtime_context(
        {
            "knowledge_base": {
                "enabled": True,
                "path": "/tmp/cairn-advisory/project/digest",
                "manifest_path": "/tmp/cairn-advisory/project/digest/manifest.json",
                "bundle_digest": "a" * 64,
                "read_only": True,
                "resources": [],
            }
        }
    )

    assert context["knowledge_base"]["enabled"] is False
    assert "path" not in context["knowledge_base"]
    assert "bundle_digest" not in context["knowledge_base"]
