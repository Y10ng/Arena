from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
import requests

from cairn.dispatcher.protocol.client import CairnClient

from cairn.advisory_contract import selection_sha256
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
        library_selection_explicit: bool = False,
        selection_protocol: bool = True,
    ) -> None:
        self.resources = resources
        self.contents = contents
        self.bindings = bindings or []
        self.library_files = library_files or {}
        self.library_contents = library_contents or {}
        self.library_selection_explicit = library_selection_explicit
        self.selection_protocol = selection_protocol
        self.library_reads: list[tuple[str, int, str]] = []

    def get_project(self, project_id: str):
        return SimpleNamespace(
            library_bindings=self.bindings,
            library_selection_explicit=self.library_selection_explicit,
            model_fields_set={"library_selection_explicit"} if self.selection_protocol else set(),
        )

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
        self.library_reads.append((key, version, path))
        return self.library_contents[(key, version, path)]

    def list_project_resources(self, project_id: str) -> list[ProjectResource]:
        return self.resources

    def download_project_resource(self, project_id: str, resource: ProjectResource, output) -> None:
        content = self.contents[resource.id]
        advisory_bundle._verify_resource(resource, content)
        output.write(content)


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


@pytest.mark.parametrize("failure", ["short", "long", "digest", "disconnect"])
def test_stream_download_failure_does_not_publish_partial_bundle(monkeypatch, tmp_path, failure):
    expected = b"valid-data"
    first = _resource("first.bin", expected)
    second = _resource("second.bin", expected, resource_id="r002")
    responses = []
    class StreamResponse:
        def __init__(self, broken):
            self.broken = broken
            self.closed = False
        def __enter__(self):
            return self
        def __exit__(self, *_):
            self.closed = True
        @property
        def content(self):
            pytest.fail("下载不得读取完整 response.content")
        def raise_for_status(self):
            pass
        def iter_content(self, chunk_size):
            assert chunk_size == 256 * 1024
            data = expected
            if self.broken:
                data = {"short": expected[:-1], "long": expected + b"x",
                        "digest": b"wrong-data", "disconnect": expected}[failure]
            yield data[:3]
            if self.broken and failure == "disconnect":
                raise requests.ConnectionError("isolated interrupted stream")
            yield data[3:]
    def get(url, *, timeout, stream):
        assert stream is True
        response = StreamResponse(url.endswith("/r002"))
        responses.append(response)
        return response
    downloader = CairnClient("http://fixture.invalid", token="synthetic-token")
    monkeypatch.setattr(downloader, "_session", lambda: SimpleNamespace(get=get))
    client = ResourceClient([first, second], {})
    monkeypatch.setattr(client, "download_project_resource", downloader.download_project_resource)
    root = tmp_path / "stage"
    monkeypatch.setenv(advisory_bundle.ADVISORY_ROOT_ENV_VAR, str(root))
    try:
        with pytest.raises((RuntimeError, requests.ConnectionError)):
            stage_advisory_bundle(client, "p1", None)
        assert len(responses) == 2 and all(response.closed for response in responses)
        assert not list(root.rglob(".staging-*"))
        assert not list(root.rglob("manifest.json"))
        assert not list(root.rglob("*.bin"))
    finally:
        downloader.close()


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
        paths=None,
        selection_sha256=selection_sha256(
            "runbook", 3, _library_digest(files),
            [{"path": "auth/login.txt", "size_bytes": len(content), "sha256": sha256(content).hexdigest()}],
        ),
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


def test_library_subset_is_materialized_and_has_distinct_cache_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    all_files = {"shared/readme.md": b"public", "private.md": b"private"}
    revision_digest = _library_digest(all_files)
    monkeypatch.setenv(advisory_bundle.ADVISORY_ROOT_ENV_VAR, str(tmp_path / "stage"))

    def selected(path: str) -> ResourceClient:
        content = all_files[path]
        metadata = LibraryFile(path=path, size_bytes=len(content), sha256=sha256(content).hexdigest())
        binding = ProjectLibraryBinding(
            library_key="runbook", library_name="Runbook", version=3,
            digest_sha256=revision_digest, file_count=1, paths=[path],
            selection_sha256=selection_sha256("runbook", 3, revision_digest, [metadata.model_dump()]),
        )
        return ResourceClient(
            [], {}, bindings=[binding], library_files={("runbook", 3): [metadata]},
            library_contents={("runbook", 3, path): content}, library_selection_explicit=True,
        )

    first_client = selected("shared/readme.md")
    first = stage_advisory_bundle(first_client, "p1", "legacy")
    assert first is not None
    root = Path(first.source_path)
    assert (root / "library/runbook/shared/readme.md").read_bytes() == b"public"
    assert not (root / "library/runbook/private.md").exists()
    assert first_client.library_reads == [("runbook", 3, "shared/readme.md")]
    assert first.manifest["libraries"][0]["paths"] == ["shared/readme.md"]
    assert first.manifest["libraries"][0]["digest_sha256"] == revision_digest
    assert first.manifest["libraries"][0]["selection_sha256"] != revision_digest
    context = knowledge_base_runtime_context("legacy", advisory=_runtime(first))
    assert context["key"] is None
    assert [item["path"] for item in context["resources"]] == ["shared/readme.md"]

    second = stage_advisory_bundle(selected("private.md"), "p1", "legacy")
    assert second is not None and second.digest != first.digest
    assert not (Path(second.source_path) / "library/runbook/shared/readme.md").exists()


def test_subset_metadata_and_bytes_are_verified_before_mount(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = b"valid"
    metadata = LibraryFile(path="readme.md", size_bytes=len(content), sha256=sha256(content).hexdigest())
    revision_digest = _library_digest({"readme.md": content, "other.md": b"not selected"})
    binding = ProjectLibraryBinding(
        library_key="runbook", library_name="Runbook", version=1,
        digest_sha256=revision_digest, file_count=1, paths=["readme.md"],
        selection_sha256=selection_sha256("runbook", 1, revision_digest, [metadata.model_dump()]),
    )
    monkeypatch.setenv(advisory_bundle.ADVISORY_ROOT_ENV_VAR, str(tmp_path / "stage"))
    client = ResourceClient([], {}, bindings=[binding], library_files={("runbook", 1): [metadata]},
                            library_contents={("runbook", 1, "readme.md"): b"wrong"})
    with pytest.raises(RuntimeError, match="file readme.md sha256 mismatch"):
        stage_advisory_bundle(client, "p1", None)

    bad_metadata = metadata.model_copy(update={"sha256": sha256(b"wrong").hexdigest()})
    client.library_files[("runbook", 1)] = [bad_metadata]
    client.library_reads.clear()
    with pytest.raises(RuntimeError, match="selection sha256 mismatch"):
        stage_advisory_bundle(client, "p1", None)
    assert client.library_reads == []

    client.library_files[("runbook", 1)] = [metadata.model_copy(update={"path": "other.md"})]
    with pytest.raises(RuntimeError, match="selection paths mismatch"):
        stage_advisory_bundle(client, "p1", None)
    assert client.library_reads == []


def test_explicit_empty_selection_does_not_mount_or_advertise_legacy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "knowledge"
    (root / "legacy").mkdir(parents=True)
    (root / "legacy/old.txt").write_text("old", encoding="utf-8")
    monkeypatch.setattr(advisory_bundle, "resolved_knowledge_base_path", lambda: str(root))
    monkeypatch.setenv(advisory_bundle.ADVISORY_ROOT_ENV_VAR, str(tmp_path / "stage"))
    assert stage_advisory_bundle(ResourceClient([], {}, library_selection_explicit=True), "new", "legacy") is None
    context = knowledge_base_runtime_context("legacy", advisory=_runtime())
    assert context["key"] is None and context["enabled"] is False
    historical = stage_advisory_bundle(ResourceClient([], {}), "old", "legacy")
    assert historical is not None and (Path(historical.source_path) / "legacy/legacy/old.txt").is_file()
    assert knowledge_base_runtime_context("legacy", advisory=_runtime(historical))["key"] == "legacy"


def test_missing_selection_protocol_cannot_assume_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(advisory_bundle.ADVISORY_ROOT_ENV_VAR, str(tmp_path / "stage"))
    with pytest.raises(RuntimeError, match="server does not provide advisory selection state"):
        stage_advisory_bundle(ResourceClient([], {}, selection_protocol=False), "p1", "legacy")


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
    # 相同内容身份复用已验证的本地缓存，不为每次启动重新下载大文件。
    cached = stage_advisory_bundle(
        ResourceClient([resource], {}),
        "p3",
        None,
    )
    assert cached is not None and cached.source_path == staged.source_path

    with pytest.raises(RuntimeError, match="size mismatch"):
        stage_advisory_bundle(
            ResourceClient([resource], {resource.id: b"x"}),
            "p4",
            None,
        )


@pytest.mark.parametrize(
    "tamper",
    ["content", "manifest", "extra_file", "extra_directory", "missing", "symlink", "writable"],
)
def test_cached_bundle_is_revalidated_without_replacing_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tamper: str,
) -> None:
    content = b"valid-data"
    resource = _resource("notes/example.txt", content)
    client = ResourceClient([resource], {resource.id: content})
    monkeypatch.setenv(advisory_bundle.ADVISORY_ROOT_ENV_VAR, str(tmp_path / "stage"))
    first = stage_advisory_bundle(client, "p1", None)
    assert first is not None
    second = stage_advisory_bundle(client, "p1", None)
    assert second is not None and second.digest == first.digest
    root = Path(first.source_path)
    target = root / "project/notes/example.txt"
    if tamper in {"content", "manifest"}:
        changed = target if tamper == "content" else root / "manifest.json"
        changed.chmod(0o644)
        original = changed.read_bytes()
        changed.write_bytes(b"X" + original[1:])
        changed.chmod(0o444)
    elif tamper == "writable":
        target.chmod(0o644)
    else:
        parent = target.parent
        parent.chmod(0o755)
        if tamper == "extra_file":
            (parent / "extra.txt").write_bytes(b"extra")
            (parent / "extra.txt").chmod(0o444)
        elif tamper == "extra_directory":
            (parent / "extra").mkdir(mode=0o555)
        else:
            target.unlink()
            if tamper == "symlink":
                target.symlink_to(tmp_path / "outside")
        parent.chmod(0o555)

    with pytest.raises(RuntimeError, match="advisory bundle"):
        stage_advisory_bundle(client, "p1", None)

    assert root.exists()
    if tamper == "symlink":
        assert target.is_symlink()
    elif tamper == "missing":
        assert not target.exists()
    elif tamper == "extra_file":
        assert (target.parent / "extra.txt").read_bytes() == b"extra"
    elif tamper == "extra_directory":
        assert (target.parent / "extra").is_dir()
    elif tamper in {"content", "manifest"}:
        assert changed.read_bytes() == b"X" + original[1:]
    else:
        assert target.stat().st_mode & 0o222


def test_bundle_is_read_only_before_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = b"content"
    resource = _resource("nested/example.txt", content)
    client = ResourceClient([resource], {resource.id: content})
    monkeypatch.setenv(advisory_bundle.ADVISORY_ROOT_ENV_VAR, str(tmp_path / "stage"))
    rename = Path.rename

    def check_before_rename(source: Path, destination: Path) -> Path:
        assert source.stat().st_mode & 0o222 == 0
        assert all(path.stat().st_mode & 0o222 == 0 for path in source.rglob("*"))
        return rename(source, destination)

    monkeypatch.setattr(Path, "rename", check_before_rename)
    assert stage_advisory_bundle(client, "p1", None) is not None


def test_concurrent_bundle_staging_reuses_one_complete_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = b"content"
    resource = _resource("nested/example.txt", content)
    client = ResourceClient([resource], {resource.id: content})
    monkeypatch.setenv(advisory_bundle.ADVISORY_ROOT_ENV_VAR, str(tmp_path / "stage"))
    rename = Path.rename
    remove_tree = advisory_bundle.shutil.rmtree
    barrier = Barrier(2)
    removed: list[Path] = []

    def rename_together(source: Path, destination: Path) -> Path:
        barrier.wait(timeout=10)
        return rename(source, destination)

    def remove_writable_staging(path: Path) -> None:
        assert path.name.startswith(".staging-")
        assert all(item.stat().st_mode & 0o700 == 0o700 for item in [path, *path.rglob("*")] if item.is_dir())
        removed.append(path)
        remove_tree(path)

    monkeypatch.setattr(Path, "rename", rename_together)
    monkeypatch.setattr(advisory_bundle.shutil, "rmtree", remove_writable_staging)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(stage_advisory_bundle, client, "p1", None) for _ in range(2)]
        bundles = [future.result(timeout=15) for future in futures]

    assert bundles[0] is not None and bundles[1] is not None
    assert bundles[0].source_path == bundles[1].source_path
    assert bundles[0].digest == bundles[1].digest
    assert len(removed) == 1
    assert list(Path(bundles[0].source_path).parent.glob(".staging-*")) == []


def test_cached_bundle_root_symlink_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    content = b"valid-data"
    resource = _resource("example.txt", content)
    client = ResourceClient([resource], {resource.id: content})
    monkeypatch.setenv(advisory_bundle.ADVISORY_ROOT_ENV_VAR, str(tmp_path / "stage"))
    bundle = stage_advisory_bundle(client, "p1", None)
    assert bundle is not None
    root = Path(bundle.source_path)
    original = root.with_name("original")
    root.rename(original)
    root.symlink_to(original, target_is_directory=True)

    with pytest.raises(RuntimeError, match="invalid advisory bundle directory"):
        stage_advisory_bundle(client, "p1", None)
    assert root.is_symlink() and original.is_dir()


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
