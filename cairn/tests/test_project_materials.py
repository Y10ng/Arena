"""材料运维入口的精确归属、原字节流与无覆盖边界；不访问真实 Docker 或数据库。"""
from __future__ import annotations

import base64
from copy import deepcopy
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tarfile

from click.testing import CliRunner
import pytest

from cairn.dispatcher.runtime.materials import MaterialsError, ProjectMaterials


IDENTITY = "a" * 64
DATA = b"\x00C1 original layer\xff\n"
DIR = 1 << 31


def _tar(members):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for member, content in members:
            archive.addfile(member, io.BytesIO(content) if content is not None else None)
    return output.getvalue()


def _member(name, content=DATA, kind=tarfile.REGTYPE, linkname=""):
    member = tarfile.TarInfo(name)
    member.type = kind
    member.linkname = linkname
    member.size = len(content) if kind == tarfile.REGTYPE else 0
    return member, content if kind == tarfile.REGTYPE else None


@pytest.fixture
def scene(tmp_path):
    (tmp_path / "control").mkdir()
    (tmp_path / "prompts").mkdir()
    labels = {
        "cairn.project_id": "p", "cairn.engine": "pi", "cairn.pi.instance_id": "b" * 64,
        "cairn.runtime_signature": "c" * 64, "cairn.scenario_key": "fixture",
    }
    attrs = {"Id": IDENTITY, "Config": {"Labels": labels}, "State": {"Status": "paused"}, "Mounts": [
        {"Type": "bind", "Source": str(tmp_path / "control"), "Destination": "/home/kali/.cairn"},
        {"Type": "bind", "Source": str(tmp_path / "prompts"), "Destination": "/tmp/cairn-prompts"},
    ]}
    state = SimpleNamespace(heads=[], archives=[], stats={}, attrs=attrs, closed=False, client_closed=False,
                            target="/tmp/c1-control-layer", raw=None, fail_stream=False)

    def path_stat(path):
        return deepcopy(state.stats.get(path, {"name": Path(path).name, "size": len(DATA) if path == state.target else 0,
                                               "mode": 0o644 if path == state.target else DIR | 0o755,
                                               "mtime": "2026-11-02T00:00:00Z", "linkTarget": ""}))

    class Response:
        def __init__(self, path):
            self.headers = {"x-docker-container-path-stat": base64.b64encode(json.dumps(path_stat(path)).encode()).decode()}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def head(url, *, params, timeout):
        assert url == IDENTITY
        state.heads.append(params["path"])
        return Response(params["path"])

    def get_archive(path, *, chunk_size):
        state.archives.append(path)
        raw = state.raw if state.raw is not None else _tar([_member(Path(path).name)])

        def stream():
            try:
                for offset in range(0, len(raw), 1024):
                    yield raw[offset:offset + 1024]
                    if state.fail_stream:
                        raise OSError("private stream exception canary")
            finally:
                state.closed = True
        return stream(), path_stat(path)

    container = SimpleNamespace(id=IDENTITY, attrs=attrs, reload=lambda: None, get_archive=get_archive)

    class Client:
        api = SimpleNamespace(head=head, _url=lambda pattern, identity: identity, timeout=10, _raise_for_status=lambda response: None)
        containers = SimpleNamespace(get=lambda identity: container, list=lambda **kwargs: [container])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            state.client_closed = True

    state.client = Client()
    state.container = container
    state.reader = ProjectMaterials(state.client)
    state.output = tmp_path / "saved.bin"
    return state


@pytest.mark.parametrize("status", ["paused", "exited"])
def test_layer_materials_save_exact_bytes_without_exec_or_state_changes(scene, status):
    scene.attrs["State"]["Status"] = status
    info = scene.reader.save("p", IDENTITY, scene.target, scene.output)
    assert scene.output.read_bytes() == DATA
    assert scene.output.stat().st_mode & 0o777 == 0o600
    assert info["hardlink_check"] == "tar_only"
    assert info["container_id"] == IDENTITY and info["state"] == status
    assert scene.attrs["State"]["Status"] == status
    assert scene.heads[:3] == ["/", "/tmp", scene.target]
    assert scene.closed and not list(scene.output.parent.glob(".cairn-material-*"))


def test_containers_accept_only_exact_project_and_existing_owner_labels(scene):
    assert [item["kind"] for item in scene.reader.containers("p")] == ["research"]
    scene.attrs["Config"]["Labels"].update({"cairn.container_role": "report", "cairn.report_task_run_id": "report-original"})
    assert scene.reader.containers("p")[0]["task_run_id"] == "report-original"
    scene.attrs["Config"]["Labels"] = {
        "io.cairn.traffic.project": "p", "io.cairn.traffic.role": "executor", "io.cairn.traffic.owner": "owner",
        "io.cairn.traffic.dispatcher": "dispatcher", "io.cairn.traffic.resource": "resource",
        "io.cairn.traffic.scope": "scope", "io.cairn.traffic.task-run": "task", "io.cairn.traffic.generation": "1",
    }
    assert scene.reader.containers("p")[0]["kind"] == "executor"
    scene.attrs["Config"]["Labels"]["io.cairn.traffic.role"] = "collector"
    assert scene.reader.containers("p") == []
    assert scene.reader.containers("foreign") == []


@pytest.mark.parametrize("identity,project", [("cairn-pi-dispatch-fake", "p"), (IDENTITY[:12], "p"), (IDENTITY, "foreign")])
def test_names_prefixes_and_foreign_projects_cannot_select_materials(scene, identity, project):
    with pytest.raises(MaterialsError):
        scene.reader.save(project, identity, scene.target, scene.output)
    assert scene.archives == [] and not scene.output.exists()


@pytest.mark.parametrize("path", [
    "/tmp/../home/kali/.cairn/auth.json", "/tmp//file", "/etc/passwd", "/run/cairn-exec/gate.json",
    "/home/kali/.cairn/pi/auth.json", "/tmp/cairn-prompts/request.json", "/tmp/egress/private.json",
    "/home/kali/.config/unknown/auth.json", "/home/kali/.ssh/id_ed25519",
])
def test_private_unknown_and_traversing_paths_are_never_archived(scene, path):
    with pytest.raises(MaterialsError):
        scene.reader.save("p", IDENTITY, path, scene.output)
    assert scene.archives == [] and not scene.output.exists()


@pytest.mark.parametrize("path,override", [
    ("/tmp", {"linkTarget": "/home/kali/.cairn"}),
    ("/tmp/c1-control-layer", {"linkTarget": "/home/kali/.cairn/auth.json"}),
    ("/tmp/c1-control-layer", {"mode": 1 << 27}),
    ("/tmp/c1-control-layer", {"mode": DIR}),
])
def test_parent_and_target_symlink_or_directory_spoof_are_rejected(scene, path, override):
    scene.stats[path] = {"name": Path(path).name, "size": len(DATA), "mode": DIR if path == "/tmp" else 0o644,
                         "mtime": "now", "linkTarget": "", **override}
    with pytest.raises(MaterialsError):
        scene.reader.save("p", IDENTITY, scene.target, scene.output)
    assert scene.archives == [] and not scene.output.exists()


def test_layer_export_requires_private_mount_isolation_and_refuses_running(scene):
    scene.attrs["Mounts"] = []
    with pytest.raises(MaterialsError, match="isolation_required"):
        scene.reader.save("p", IDENTITY, scene.target, scene.output)
    scene.attrs["State"]["Status"] = "running"
    with pytest.raises(MaterialsError, match="quiescent"):
        scene.reader.save("p", IDENTITY, scene.target, scene.output)
    assert scene.archives == []


@pytest.mark.parametrize("kind,name,linkname", [
    (tarfile.SYMTYPE, "c1-control-layer", "/private/auth"),
    (tarfile.LNKTYPE, "c1-control-layer", "auth"),
    (tarfile.DIRTYPE, "c1-control-layer", ""),
    (tarfile.REGTYPE, "../c1-control-layer", ""),
    (tarfile.REGTYPE, "wrong-file", ""),
])
def test_archive_links_spoofs_and_unexpected_names_leave_no_output(scene, kind, name, linkname):
    scene.raw = _tar([_member(name, kind=kind, linkname=linkname)])
    with pytest.raises(MaterialsError):
        scene.reader.save("p", IDENTITY, scene.target, scene.output)
    assert not scene.output.exists() and scene.closed
    assert not list(scene.output.parent.glob(".cairn-material-*"))


def test_bind_hardlinks_are_rejected_even_when_tar_would_be_regular(scene, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = workspace / "original"
    original.write_bytes(DATA)
    os.link(original, workspace / "linked")
    scene.target = "/home/kali/workspace/linked"
    scene.attrs["Mounts"].append({"Type": "bind", "Source": str(workspace), "Destination": "/home/kali/workspace"})
    with pytest.raises(MaterialsError, match="unlinked_file"):
        scene.reader.save("p", IDENTITY, scene.target, scene.output)
    assert scene.archives == [] and not scene.output.exists()
    original.unlink()
    assert scene.reader.save("p", IDENTITY, scene.target, scene.output)["hardlink_check"] == "host_nlink_and_tar"
    assert scene.output.read_bytes() == DATA


def test_private_bind_source_alias_is_not_a_material_mount(scene):
    source = scene.attrs["Mounts"][0]["Source"]
    scene.attrs["Mounts"].append({"Type": "bind", "Source": source, "Destination": "/tmp/alias"})
    scene.target = "/tmp/alias/auth.json"
    with pytest.raises(MaterialsError, match="private_mount_alias"):
        scene.reader.save("p", IDENTITY, scene.target, scene.output)
    assert scene.archives == []


@pytest.mark.parametrize("existing", ["file", "symlink"])
def test_existing_outputs_are_never_overwritten(scene, existing):
    if existing == "file":
        scene.output.write_bytes(b"original")
    else:
        scene.output.symlink_to(scene.output.parent / "absent")
    with pytest.raises(MaterialsError, match="output_exists"):
        scene.reader.save("p", IDENTITY, scene.target, scene.output)
    assert scene.archives == []
    if existing == "file":
        assert scene.output.read_bytes() == b"original"
    else:
        assert scene.output.is_symlink()


@pytest.mark.parametrize("failure", ["stream", "extra_file", "truncated", "output_race"])
def test_failed_save_removes_temporary_bytes_and_preserves_racing_output(scene, monkeypatch, failure):
    if failure == "stream":
        scene.fail_stream = True
    elif failure == "extra_file":
        scene.raw = _tar([_member("c1-control-layer"), _member("extra")])
    elif failure == "truncated":
        scene.raw = _tar([_member("c1-control-layer")])[:520]
    else:
        original_link = os.link
        def competing_link(source, output, **kwargs):
            Path(output).write_bytes(b"winner")
            original_link(source, output, **kwargs)
        monkeypatch.setattr(os, "link", competing_link)
    with pytest.raises((MaterialsError, OSError, tarfile.TarError)):
        scene.reader.save("p", IDENTITY, scene.target, scene.output)
    assert scene.closed and not list(scene.output.parent.glob(".cairn-material-*"))
    assert scene.output.read_bytes() == b"winner" if failure == "output_race" else not scene.output.exists()


def test_list_is_bounded_and_does_not_archive_private_parent_roots(scene):
    assert "/tmp" in scene.reader.list("p", IDENTITY)["roots"]
    assert scene.archives == []
    with pytest.raises(MaterialsError):
        scene.reader.list("p", IDENTITY, "/tmp")
    huge, _ = _member("work/huge")
    huge.size = 9 * 1024 * 1024
    scene.raw = _tar([_member("work", kind=tarfile.DIRTYPE)])[:512] + huge.tobuf() + b"\x00" * 1024
    result = scene.reader.list("p", IDENTITY, "/tmp/work")
    assert result["truncated"] and result["limit_reason"] == "listing_limit"
    assert [item["path"] for item in result["entries"]] == ["/tmp/work"]
    assert scene.closed


def test_list_stops_before_private_subtrees_and_does_not_publish_their_names(scene):
    scene.raw = _tar([_member("work", kind=tarfile.DIRTYPE), _member("work/.cairn/auth.json")])
    result = scene.reader.list("p", IDENTITY, "/tmp/work")
    assert result["truncated"] and result["limit_reason"] == "private_subtree"
    assert ".cairn" not in json.dumps(result)


def test_cli_uses_local_docker_only_and_reports_fixed_errors(scene, monkeypatch):
    import docker
    from cairn import cli

    monkeypatch.setattr(docker, "from_env", lambda **kwargs: scene.client)
    monkeypatch.setattr(cli.db, "configure", lambda *args, **kwargs: pytest.fail("materials must not configure a business DB"))
    runner = CliRunner()
    listed = runner.invoke(cli.main, ["materials", "containers", "--project", "p"])
    assert listed.exit_code == 0 and json.loads(listed.output)[0]["container_id"] == IDENTITY
    result = runner.invoke(cli.main, ["materials", "save", "--project", "p", "--container", IDENTITY,
                                      "--path", scene.target, "--output", str(scene.output)])
    assert result.exit_code == 0 and scene.output.read_bytes() == DATA and scene.client_closed
    scene.output.unlink()
    scene.fail_stream = True
    failed = runner.invoke(cli.main, ["materials", "save", "--project", "p", "--container", IDENTITY,
                                      "--path", scene.target, "--output", str(scene.output)])
    assert failed.exit_code == 1 and "materials_read_failed" in failed.output
    assert "canary" not in failed.output and not scene.output.exists()
