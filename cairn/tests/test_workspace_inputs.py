"""隔离合成输入；文件读取使用真实 reader，容器归属用替身，不操作正式实例。"""
from contextlib import nullcontext
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import shlex
import tempfile
import threading

import docker
from types import SimpleNamespace

import pytest

from cairn.advisory_contract import selection_sha256, WORKSPACE_INPUTS_PATH
from cairn.dispatcher import advisory_bundle
from cairn.dispatcher.config import ContainerConfig
from cairn.dispatcher.prompting import knowledge_base_runtime_context
from cairn.dispatcher.runtime import workspace_inputs
from cairn.dispatcher.runtime.container_options import ContainerRuntimeOptions
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.server.models import LibraryFile, ProjectLibraryBinding
from cairn.server.services import project_workspace as reader
from cairn.server.services.runtime_context import sanitize_task_run_runtime_context
from test_advisory_bundle import ResourceClient, _resource, _library_digest
from test_project_workspace import scene


@pytest.fixture
def materials(tmp_path, monkeypatch):
    monkeypatch.setenv(advisory_bundle.ADVISORY_ROOT_ENV_VAR, str(tmp_path / "advisory"))
    # Docker source 故意不可供 Dispatcher 读取，证明复制没有反向猜测映射。
    monkeypatch.setenv(advisory_bundle.ADVISORY_DOCKER_SOURCE_ENV_VAR, "/docker-only/advisory")
    files = {"sample.csv": b"name,value\na,2\nb,3\n", "tiny.bin": b"\x00\xff\x01", "notes.txt": b"synthetic input\n"}
    resources = [_resource(path, content, resource_id=f"r{index}", kind="file") for index, (path, content) in enumerate(files.items())]
    library = {"guide.md": b"# Selected guide\n", "unselected.md": b"not selected"}
    digest = _library_digest(library)
    metadata = LibraryFile(path="guide.md", size_bytes=len(library["guide.md"]), sha256=sha256(library["guide.md"]).hexdigest())
    binding = ProjectLibraryBinding(
        library_key="guide", library_name="Guide", version=2, digest_sha256=digest, file_count=1,
        paths=["guide.md"], selection_sha256=selection_sha256("guide", 2, digest, [metadata.model_dump()]),
    )
    client = ResourceClient(resources, {item.id: files[item.path] for item in resources}, bindings=[binding],
        library_files={("guide", 2): [metadata]}, library_contents={("guide", 2, "guide.md"): library["guide.md"]},
        library_selection_explicit=True)
    bundle = advisory_bundle.stage_advisory_bundle(client, "p1", None)
    return bundle, {**{f"project/{path}": content for path, content in files.items()}, "library/guide/guide.md": library["guide.md"]}


def prepare(root, bundle):
    workspace_inputs.prepare_workspace_inputs(root, bundle.local_path, bundle.manifest, bundle.digest)


def test_prepared_inputs_real_reader_and_reuse(scene, materials, monkeypatch):
    bundle, files = materials
    prepare(scene.root, bundle)
    for path, content in files.items():
        target = scene.root / "inputs" / path
        assert target.read_bytes() == content
        assert target.stat().st_nlink == 1 and not target.is_symlink()
        assert target.stat().st_ino != (bundle.local_path / path).stat().st_ino
    assert not (scene.root / "inputs/library/guide/unselected.md").exists()
    manifest = json.loads((scene.root / "inputs/manifest.json").read_bytes())
    assert manifest == bundle.manifest
    assert manifest["libraries"][0]["paths"] == ["guide.md"]
    first = reader.list_workspace("p1")
    wid = first["workspace_id"]
    assert [item["name"] for item in first["entries"]] == ["inputs"]
    assert reader.preview_workspace("p1", "inputs/project/sample.csv", workspace_id=wid)["content"] == files["project/sample.csv"].decode()
    assert reader.search_workspace("p1", "guide.md", workspace_id=wid)["entries"]
    download = reader.download_workspace("p1", "inputs/project/tiny.bin", workspace_id=wid)
    try:
        assert os.read(download.fd, 100) == files["project/tiny.bin"]
    finally:
        os.close(download.fd)
    inode = (scene.root / "inputs/project/sample.csv").stat().st_ino
    (scene.root / "analysis.txt").write_text("keep output")
    monkeypatch.setattr(workspace_inputs, "_publish", lambda *_: pytest.fail("must reuse without copying"))
    prepare(scene.root, bundle)
    assert (scene.root / "inputs/project/sample.csv").stat().st_ino == inode
    assert (scene.root / "analysis.txt").read_text() == "keep output"


@pytest.mark.parametrize("conflict", ["directory", "bytes", "manifest", "link"])
def test_inputs_conflicts_never_overwrite(scene, materials, conflict):
    bundle, _files = materials
    if conflict == "directory":
        (scene.root / "inputs").mkdir()
        (scene.root / "inputs/own.txt").write_text("keep")
    else:
        prepare(scene.root, bundle)
        target = scene.root / "inputs" / ("manifest.json" if conflict == "manifest" else "project/sample.csv")
        target.chmod(0o644)
        if conflict == "link":
            target.parent.chmod(0o755)
            target.unlink()
            target.symlink_to(bundle.local_path / "project/sample.csv")
        else:
            target.write_bytes(b"changed")
            target.chmod(0o444)
    before = (bundle.local_path / "project/sample.csv").read_bytes()
    with pytest.raises(RuntimeError, match="workspace inputs conflict"):
        prepare(scene.root, bundle)
    assert (bundle.local_path / "project/sample.csv").read_bytes() == before
    if conflict == "directory":
        assert (scene.root / "inputs/own.txt").read_text() == "keep"
    elif conflict != "link":
        assert target.read_bytes() == b"changed"


@pytest.mark.parametrize("failure", ["copy", "digest", "size", "publish_race"])
def test_failure_does_not_publish_or_delete(scene, materials, monkeypatch, failure):
    bundle, _files = materials
    if failure in {"digest", "size"}:
        target = bundle.local_path / "project/sample.csv"
        target.chmod(0o644)
        original = target.read_bytes()
        target.write_bytes(b"x" * len(original) if failure == "digest" else b"x")
        target.chmod(0o444)
    elif failure == "copy":
        original_open = Path.open
        def fail_write(path, mode="r", *args, **kwargs):
            if mode == "xb" and ".inputs-staging-" in str(path):
                raise OSError("synthetic disk failure")
            return original_open(path, mode, *args, **kwargs)
        monkeypatch.setattr(Path, "open", fail_write)
    else:
        publish = workspace_inputs._publish
        def race(temporary, destination):
            assert not list(scene.root.iterdir())
            destination.mkdir()
            publish(temporary, destination)
        monkeypatch.setattr(workspace_inputs, "_publish", race)
    with pytest.raises((OSError, RuntimeError)):
        prepare(scene.root, bundle)
    assert (scene.root / "inputs").exists() == (failure == "publish_race")
    assert not list(scene.root.parent.glob(".inputs-staging-*"))


def test_only_prepared_research_context_has_working_paths(scene, materials):
    bundle, _files = materials
    options = ContainerRuntimeOptions.build(image="fixture", network_mode="none", dns=[], cap_add=[],
        features={"http_traffic_execution_version": 1}, scenario_key="fixture", advisory_bundle=bundle)
    assert options.advisory_local_path == bundle.local_path
    assert not Path(options.advisory_mount_source).exists()
    assert "workspace_path" not in knowledge_base_runtime_context(advisory=options)
    prepare(scene.root, bundle)
    context = knowledge_base_runtime_context(advisory=options, workspace_inputs_prepared=True)
    projected = sanitize_task_run_runtime_context({"knowledge_base": context, "project_resources": context})
    for item in projected["knowledge_base"]["resources"]:
        assert item["workspace_path"].startswith(WORKSPACE_INPUTS_PATH + "/")
        assert item["container_path"].startswith("/cairn/advisory/")
        assert item["sha256"] == sha256((scene.root / "inputs" / item["workspace_path"].removeprefix(WORKSPACE_INPUTS_PATH + "/")).read_bytes()).hexdigest()
    assert projected["project_resources"]["workspace_path"] == WORKSPACE_INPUTS_PATH
    independent = replace(options, features={})
    assert "workspace_path" not in knowledge_base_runtime_context(advisory=independent, workspace_inputs_prepared=True)
    bad = {**context, "resources": [{**context["resources"][0], "workspace_path": "/private/secret"}]}
    assert "workspace_path" not in sanitize_task_run_runtime_context({"knowledge_base": bad})["knowledge_base"]["resources"][0]


@pytest.mark.parametrize("state", ["running", "exited"])
def test_existing_container_and_continuation_prepare_inputs(scene, materials, monkeypatch, state):
    bundle, _files = materials
    options = ContainerRuntimeOptions.build(image="fixture", network_mode="none", dns=[], cap_add=[],
        features={"http_traffic_execution_version": 1}, scenario_key="fixture", advisory_bundle=bundle)
    manager = object.__new__(ContainerManager)
    manager.traffic_execution = SimpleNamespace(workspace_directory=lambda *_args, **_kwargs: scene.root)
    container = scene.container
    container.attrs["Mounts"][0]["RW"] = True
    monkeypatch.setattr(manager, "_get_container", lambda *_: container)
    monkeypatch.setattr(manager, "_assert_owned", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(manager, "_container_runtime_mismatch", lambda *_: False)
    monkeypatch.setattr(manager, "_container_state", lambda *_: state)
    starts = []
    monkeypatch.setattr(manager, "_start_existing", lambda *_args, **_kwargs: starts.append(True))
    assert manager._ensure_running_locked("p1", "fixture", options=options) == "fixture"
    assert (scene.root / "inputs/project/sample.csv").is_file()
    assert bool(starts) == (state == "exited")
    monkeypatch.setattr(manager, "container_name", lambda *_: "fixture")
    monkeypatch.setattr(manager, "_research_preparation", lambda *_: nullcontext())
    monkeypatch.setattr(manager, "docker_daemon_id", lambda: "fixture-daemon")
    monkeypatch.setattr(manager, "_running_or_hot_paused", lambda *_: True)
    assert manager.activate_existing_for_continuation("p1", options, container.id,
        register=lambda *_: "token", finish=lambda *_: None, reuse=lambda *_: True) == "fixture"
    container.attrs["Mounts"][0]["Source"] = str(scene.root.parent / "wrong")
    with pytest.raises(RuntimeError, match="confirmed shared workspace bind"):
        manager._ensure_running_locked("p1", "fixture", options=options)


@pytest.mark.skipif(os.environ.get("CAIRN_WORKSPACE_INPUTS_DOCKER") != "1", reason="仅显式启用隔离 Docker 检查")
def test_real_control_and_task_executor_share_prepared_inputs(materials, monkeypatch):
    bundle, files = materials
    bundle = replace(bundle, source_path=str(bundle.local_path))
    image = os.environ.get("CAIRN_WORKSPACE_INPUTS_IMAGE", "arena-worker:local")
    client = docker.from_env()
    with tempfile.TemporaryDirectory(prefix="cairn-inp-") as temporary:
        root = Path(temporary)
        manager = ContainerManager(ContainerConfig(image=image, network_mode="none", completed_action="stop"), instance_id=sha256(temporary.encode()).hexdigest())
        audit = []
        api = SimpleNamespace(
            update_task_run=lambda *_args, **kwargs: (audit.append(kwargs["runtime_context"]) or SimpleNamespace(ok=True)),
            resolve_task_test_egress=lambda *_: {"mode": "direct"},
            register_traffic_scope=lambda *_: SimpleNamespace(ok=True),
        )
        manager.configure_traffic_execution(api, root=root / "traffic", dispatcher_id="input-fixture", isolated=True)
        bridge = manager.traffic_execution
        # 不启动上传和状态发布；保留真实健康线程刷新执行许可，避免慢速启动被误判失联。
        monkeypatch.setattr(bridge, "_start_sync", lambda *_: None)
        health = threading.Thread(target=bridge._watch, daemon=True)
        options = ContainerRuntimeOptions.build(image=image, network_mode="none", dns=[], cap_add=[],
            features={"http_traffic_execution_version": 1}, scenario_key="fixture", advisory_bundle=bundle)
        control = None
        runtime = bridge._runtime("p1", isolated=True)
        health.start()
        try:
            name = manager.ensure_running("p1", options)
            control = client.containers.get(name)
            inode = (runtime.workspace / "inputs/project/sample.csv").stat().st_ino
            assert manager.ensure_running("p1", options) == name
            assert (runtime.workspace / "inputs/project/sample.csv").stat().st_ino == inode
            binding = manager.prepare_task_execution(SimpleNamespace(id="p1", scenario_snapshot={"features": options.features}),
                "input-fixture-run", TaskCancellation(), options)
            assert sanitize_task_run_runtime_context(audit[0])["knowledge_base"]["workspace_path"] == WORKSPACE_INPUTS_PATH
            scope = runtime.scopes[0]
            executor = client.containers.get(scope.container_id)
            for instance in (control, executor):
                assert instance.attrs["Image"] == client.images.get(image).id
                mounts = {item["Destination"]: item for item in instance.attrs["Mounts"]}
                assert mounts["/home/kali/workspace"]["Source"] == str(runtime.workspace)
            assert "/cairn/advisory" not in {item["Destination"] for item in executor.attrs["Mounts"]}
            analysis = "import csv,json,sys; from pathlib import Path; rows=list(csv.DictReader(Path(sys.argv[1]).open())); result={'rows':len(rows),'sum':sum(int(row['value']) for row in rows)}; Path(sys.argv[2]).write_text(json.dumps(result)); print(json.dumps(result))"
            command = shlex.join(["python3", "-I", "-c", analysis, f"{WORKSPACE_INPUTS_PATH}/project/sample.csv", "/home/kali/workspace/analysis.json"])
            node = r'''
import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { executeTaskCommand } from '/opt/cairn/pi-agent/dist/task-execution.js';
import { evidencePath, readEvidence, ArtifactRegistry } from '/opt/cairn/pi-agent/dist/artifacts.js';
const input = JSON.parse(process.argv[1]);
const cwd = '/home/kali/workspace';
const path = await evidencePath(cwd, input.path, '/home/kali/.cairn/pi');
const text = (await readEvidence(path, {})).text;
const sha256 = createHash('sha256').update(await readFile(path)).digest('hex');
const registry = new ArtifactRegistry(cwd, '/home/kali/.cairn/pi');
let rejected = false;
try { await registry.register(path, 'file'); } catch (error) { if (error.code === 'artifact_invalid') rejected = true; else throw error; }
if (!rejected) throw Error('input registered as output');
let stdout = '';
const result = await executeTaskCommand(input.binding, input.command, cwd, {timeout: 10, onData(data, side) { if (side === 'stdout') stdout += data.toString(); }});
console.log(JSON.stringify({text, sha256, result, analysis: JSON.parse(stdout)}));
'''
            result = control.exec_run(["node", "--input-type=module", "-e", node, json.dumps({
                "path": f"{WORKSPACE_INPUTS_PATH}/project/sample.csv", "binding": binding, "command": command,
            })])
            assert result.exit_code == 0, result.output.decode()
            outcome = json.loads(result.output)
            assert outcome["text"].encode() == files["project/sample.csv"]
            assert outcome["sha256"] == sha256(files["project/sample.csv"]).hexdigest()
            assert outcome["result"]["exitCode"] == 0
            assert outcome["analysis"] == {"rows": 2, "sum": 5}
            assert json.loads((runtime.workspace / "analysis.json").read_bytes()) == outcome["analysis"]
            for path, content in files.items():
                assert (runtime.workspace / "inputs" / path).read_bytes() == content
            assert (runtime.workspace / "inputs/manifest.json").read_bytes() == (bundle.local_path / "manifest.json").read_bytes()
            assert (bundle.local_path / "project/sample.csv").read_bytes() == files["project/sample.csv"]
            record = ({"scenario_key": "fixture"}, [{"dispatcher_id": "input-fixture", "container_id": control.id,
                "docker_daemon_id": client.info()["ID"]}], True)
            monkeypatch.setattr(reader, "_record", lambda *_: record)
            wid = reader.list_workspace("p1")["workspace_id"]
            assert reader.preview_workspace("p1", "inputs/project/sample.csv", workspace_id=wid)["content"] == outcome["text"]
            print("real Docker: shared CSV rows=2 sum=5; binary/library/manifest bytes exact; reader and input boundary passed")
        finally:
            bridge._stop.set()
            health.join(timeout=5)
            if control is None:
                try:
                    control = client.containers.get(manager.container_name("p1"))
                except docker.errors.NotFound:
                    pass
            if control is not None:
                control.remove(force=True)
            runtime.destroy()
            client.close()
