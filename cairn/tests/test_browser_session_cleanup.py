from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from types import SimpleNamespace

import pytest
from pathlib import Path
import socket
import subprocess
import sys
import tempfile


HELPER = (
    Path(__file__).parents[1]
    / "src/cairn/dispatcher/runtime/browser_session_cleanup.py"
)


def invoke(*args, env=None, cwd=None):
    result = subprocess.run(
        [sys.executable, "-c", HELPER.read_text(), *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stderr == ""
    assert len(result.stdout.encode()) <= 16384
    assert len(result.stdout.splitlines()) == 1
    return json.loads(result.stdout)


@pytest.fixture
def installation(tmp_path, monkeypatch):
    package = tmp_path / "npm/@playwright/cli"
    core = package / "node_modules/playwright-core"
    (core / "lib").mkdir(parents=True)
    (core / "lib/coreBundle.js").write_text("// 独立安装 fixture，执行边界由测试替换。\n")
    (package / "package.json").write_text(json.dumps({
        "name": "@playwright/cli", "version": "0.1.15",
        "bin": {"playwright-cli": "playwright-cli.js"},
    }))
    (core / "package.json").write_text(json.dumps({
        "name": "playwright-core", "version": "1.62.0-alpha-2026-06-29",
    }))
    executable = package / "playwright-cli.js"
    executable.write_text("#!/bin/sh\nexit 97\n")
    executable.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "playwright-cli").symlink_to(executable)
    (bin_dir / "node").symlink_to(sys.executable)
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "job"
    cwd.mkdir()
    env = {"HOME": str(home), "PATH": str(bin_dir), "USER": "worker"}
    for key in tuple(os.environ):
        monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(cwd)
    spec = importlib.util.spec_from_file_location("cleanup_helper", HELPER)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return SimpleNamespace(
        package=package, core=core, executable=executable, home=home,
        cwd=cwd, env=env, helper=helper,
    )


def test_probe_reports_installed_versions_and_package_namespace_without_running_cli(installation):
    result = invoke("probe", env=installation.env, cwd=installation.cwd)
    assert result["status"] == "ok"
    context = result["context"]
    assert context["uid"] == os.getuid()
    assert context["home"] == str(installation.home)
    assert context["cache_root"] == str(installation.home / ".cache/ms-playwright/daemon")
    assert context["cli_path"] == str(installation.executable)
    assert context["cli_version"] == "0.1.15"
    assert context["core_version"] == "1.62.0-alpha-2026-06-29"
    assert context["package_root"] == str(installation.core)
    assert context["workspace_dir"] is None
    assert context["cwd"] == str(installation.cwd)
    assert not (installation.home / ".cache").exists()


@pytest.mark.parametrize("depth,found", [(0, True), (9, True), (10, False)])
def test_probe_follows_cli_ten_level_workspace_lookup(installation, depth, found):
    (installation.cwd / ".playwright").touch()
    nested = installation.cwd.joinpath(*["child"] * depth)
    nested.mkdir(parents=True, exist_ok=True)
    result = invoke("probe", env=installation.env, cwd=nested)
    assert result["status"] == "ok"
    assert result["context"]["workspace_dir"] == (str(installation.cwd) if found else None)
    other = installation.cwd.parent / "unmarked"
    other.mkdir()
    global_context = invoke("probe", env=installation.env, cwd=other)["context"]
    assert (result["context"]["namespace_id"] == global_context["namespace_id"]) is not found


@pytest.mark.parametrize("relative_file,field,value", [
    ("package.json", "version", "0.1.16"),
    ("package.json", "name", "unrelated-cli"),
    ("node_modules/playwright-core/package.json", "version", "1.61.0"),
    ("node_modules/playwright-core/package.json", "name", "unrelated-core"),
])
def test_probe_does_not_guess_unsupported_installations(installation, relative_file, field, value):
    path = installation.package / relative_file
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(json.dumps(data))
    result = invoke("probe", env=installation.env, cwd=installation.cwd)
    assert result == {"status": "unsupported", "reason": "unsupported_cli"}


@pytest.mark.parametrize("key,value", [
    ("HOME", "relative/private-secret"),
    ("HOME", ""),
    ("XDG_CACHE_HOME", "relative/private-secret"),
    ("PWTEST_DAEMON_SESSION_DIR", "/private-secret"),
    ("PWTEST_SOCKETS_DIR", "/private-secret"),
    ("PLAYWRIGHT_CLI_VERSION_FOR_TEST", "private-secret"),
    ("NODE_OPTIONS", "--require=private-secret"),
    ("NODE_PATH", "/private-secret"),
])
def test_probe_skips_unconfirmed_environment_without_leaking_values(installation, key, value):
    env = dict(installation.env, **{key: value})
    result = invoke("probe", env=env, cwd=installation.cwd)
    assert result == {"status": "skipped", "reason": "unsafe_environment"}


def test_probe_returns_bounded_failure_for_malformed_metadata(installation):
    (installation.package / "package.json").write_text("cookie=private-secret")
    result = invoke("probe", env=installation.env, cwd=installation.cwd)
    assert result == {"status": "skipped", "reason": "invalid_context"}


OWN = "cairn-task-0123456789abcdef0123456789abcdef"
OTHER = "cairn-task-fedcba9876543210fedcba9876543210"


@pytest.mark.parametrize("name", ["default", "personal", "cairn-task-abc", "../" + OWN, OWN + ";id"])
def test_cleanup_rejects_unmanaged_names_before_inspecting_context(installation, name):
    result = invoke("cleanup", name, "{}", env=installation.env, cwd=installation.cwd)
    assert result == {
        "status": "skipped", "reason": "unmanaged_name",
        "counts": {"matched": 0, "closed": 0, "absent": 0, "skipped": 0, "retry": 0},
    }


def test_cleanup_absent_registry_does_not_create_files_or_execute_cli(installation):
    context = installation.helper.probe()["context"]
    result = invoke("cleanup", OWN, json.dumps(context), env=installation.env, cwd=installation.cwd)
    assert result == {
        "status": "absent", "reason": "no_matching_session",
        "counts": {"matched": 0, "closed": 0, "absent": 0, "skipped": 0, "retry": 0},
    }
    assert not Path(context["cache_root"]).exists()


@pytest.mark.parametrize("field", ["uid", "home", "cache_root", "cli_path", "cli_version", "core_version", "package_root", "namespace_id", "workspace_dir"])
def test_cleanup_skips_mismatched_expected_context(installation, field):
    context = installation.helper.probe()["context"]
    context[field] = "wrong-context"
    result = installation.helper.cleanup(OWN, context)
    assert result["status"] == "skipped"
    assert result["reason"] == "context_mismatch"


@pytest.fixture
def sessions(installation, monkeypatch):
    servers = []
    with tempfile.TemporaryDirectory(prefix="bc") as temp_dir:
        monkeypatch.setenv("TMPDIR", temp_dir)
        installation.env["TMPDIR"] = temp_dir
        socket_dir = Path(temp_dir) / "pw-10f5ebe6/cli"
        socket_dir.mkdir(parents=True)

        def make(name=OWN, workspace=None, **overrides):
            # 此 fixture 按真实 CLI 的记录格式建独立文件，不调用 helper 的解析/构造函数。
            namespace = hashlib.sha1(str(workspace or installation.core).encode()).hexdigest()[:16]
            root = Path(os.environ.get("XDG_CACHE_HOME") or installation.home / ".cache")
            record = root / "ms-playwright/daemon" / namespace / (name + ".session")
            record.parent.mkdir(parents=True, exist_ok=True)
            socket_path = socket_dir / f"{namespace}-{name}.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(socket_path))
            server.listen(32)
            servers.append(server)
            data = {
                "name": name, "version": "1.62.0-alpha-2026-06-29", "timestamp": 1000,
                "socketPath": str(socket_path), "cli": {},
                "browser": {"launchOptions": {"proxy": {"password": "private-secret"}}},
            }
            if workspace is not None:
                data["workspaceDir"] = str(workspace)
            data.update(overrides)
            record.write_text(json.dumps(data))
            return SimpleNamespace(record=record, socket_path=socket_path, server=server, data=data)

        yield make
        for server in servers:
            server.close()


def test_cleanup_closes_only_exact_name_across_workspaces_and_global_namespace(installation, sessions, monkeypatch):
    first = installation.cwd.parent / "workspace-one"
    second = installation.cwd.parent / "workspace-two"
    for workspace in (first, second):
        workspace.mkdir()
        (workspace / ".playwright").mkdir()
    owned = [sessions(workspace=first), sessions(workspace=second), sessions()]
    others = [sessions(name=OTHER, workspace=first), sessions(name="personal", workspace=second)]
    original = {item.record: item.record.read_bytes() for item in others}
    profile = owned[0].record.parent / ("ud-" + OWN + "-chromium")
    profile.mkdir()
    (profile / "keep").write_text("private-secret")
    context = installation.helper.probe()["context"]
    monkeypatch.chdir(second)
    monkeypatch.setenv("PLAYWRIGHT_CLI_SESSION", "personal")
    calls = []

    def official_close(argv, **kwargs):
        assert argv == [str(installation.executable), "-s=" + OWN, "close"]
        assert kwargs["env"]["NO_UPDATE_NOTIFIER"] == "1"
        assert kwargs.get("shell", False) is False
        assert kwargs["timeout"] <= 5
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        calls.append(kwargs["cwd"])
        selected = {str(first): owned[0], str(second): owned[1], str(installation.cwd): owned[2]}[kwargs["cwd"]]
        selected.server.close()
        selected.socket_path.unlink()
        selected.record.unlink()
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", official_close)
    result = installation.helper.cleanup(OWN, context)
    assert result == {
        "status": "closed", "reason": "verified_closed",
        "counts": {"matched": 3, "closed": 3, "absent": 0, "skipped": 0, "retry": 0},
    }
    assert sorted(calls) == sorted([str(first), str(second), str(installation.cwd)])
    for record, contents in original.items():
        assert record.read_bytes() == contents
    assert (profile / "keep").read_text() == "private-secret"
    assert "private-secret" not in json.dumps(result)


@pytest.mark.parametrize("override", [
    {"cli": {"persistent": True}}, {"cli": {"persistent": "false"}},
    {"cli": {"persistent": 0}}, {"cli": None}, {"cli": []},
    {"cli": {"profile": "/private-secret"}},
    {"attached": True}, {"attached": "false"}, {"attached": None},
    {"version": "0.1.15"}, {"version": "1.63.0"}, {"name": OTHER},
    {"timestamp": True}, {"workspaceDir": "relative"}, {"workspaceDir": None},
    {"unknownFutureField": True},
])
def test_cleanup_skips_unknown_persistent_or_attached_records(installation, sessions, monkeypatch, override):
    item = sessions()
    item.data.update(override)
    item.record.write_text(json.dumps(item.data))
    original = item.record.read_bytes()

    def must_not_run(*args, **kwargs):
        pytest.fail("不安全的 session 不得执行 CLI")

    monkeypatch.setattr(subprocess, "run", must_not_run)
    result = installation.helper.cleanup(OWN, installation.helper.probe()["context"])
    assert result["status"] == "skipped"
    assert result["counts"] == {"matched": 1, "closed": 0, "absent": 0, "skipped": 1, "retry": 0}
    assert item.record.read_bytes() == original
    assert item.socket_path.exists()


@pytest.mark.parametrize("kind", ["malformed", "oversize", "array", "duplicate", "symlink", "parent_symlink", "fifo", "hardlink"])
def test_cleanup_rejects_unsafe_session_files_read_only(installation, sessions, monkeypatch, kind):
    item = sessions()
    if kind == "malformed":
        item.record.write_text("private-secret{broken")
    elif kind == "oversize":
        item.record.write_text(json.dumps(dict(item.data, browser={"secret": "x" * 70000})))
    elif kind == "array":
        item.record.write_text("[]")
    elif kind == "duplicate":
        item.record.write_text(item.record.read_text()[:-1] + ',"attached":true,"attached":false}')
    elif kind == "symlink":
        target = item.record.with_suffix(".private")
        item.record.rename(target)
        item.record.symlink_to(target)
    elif kind == "parent_symlink":
        target = item.record.parent.with_name("moved-registry")
        item.record.parent.rename(target)
        item.record.parent.symlink_to(target, target_is_directory=True)
    elif kind == "fifo":
        item.record.unlink()
        os.mkfifo(item.record)
    elif kind == "hardlink":
        os.link(item.record, item.record.with_suffix(".private"))

    def must_not_run(*args, **kwargs):
        pytest.fail("不安全的文件不得执行 CLI")

    context = installation.helper.probe()["context"]
    if kind == "fifo":
        result = invoke("cleanup", OWN, json.dumps(context), env=installation.env, cwd=installation.cwd)
    else:
        monkeypatch.setattr(subprocess, "run", must_not_run)
        result = installation.helper.cleanup(OWN, context)
    assert result["status"] == "skipped"
    assert result["counts"]["closed"] == 0
    assert item.socket_path.exists()
    assert "private-secret" not in json.dumps(result)


@pytest.mark.parametrize("failure", ["exit_zero", "nonzero", "timeout"])
def test_cleanup_retries_boundedly_and_never_trusts_exit_code_without_socket_disappearance(installation, sessions, monkeypatch, failure):
    item = sessions()
    attempts = []

    def close(argv, **kwargs):
        attempts.append(argv)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output="private-secret")
        return subprocess.CompletedProcess(argv, 0 if failure == "exit_zero" else 1)

    monkeypatch.setattr(subprocess, "run", close)
    result = installation.helper.cleanup(OWN, installation.helper.probe()["context"])
    assert result["status"] == "retry"
    assert result["reason"] == "close_unverified"
    assert result["counts"] == {"matched": 1, "closed": 0, "absent": 0, "skipped": 0, "retry": 1}
    assert 1 <= len(attempts) <= 2
    assert item.socket_path.exists()
    assert "private-secret" not in json.dumps(result)


def test_cleanup_reports_absent_when_valid_record_has_no_socket_without_running_cli(installation, sessions, monkeypatch):
    item = sessions()
    item.server.close()
    item.socket_path.unlink()
    original = item.record.read_bytes()

    def must_not_run(*args, **kwargs):
        pytest.fail("已消失的 daemon 不需要 CLI")

    monkeypatch.setattr(subprocess, "run", must_not_run)
    result = installation.helper.cleanup(OWN, installation.helper.probe()["context"])
    assert result["status"] == "absent"
    assert result["counts"]["absent"] == 1
    assert item.record.read_bytes() == original


@pytest.mark.parametrize("kind", ["other_socket", "outside", "symlink", "regular_file"])
def test_cleanup_rejects_socket_redirection_and_non_socket_paths(installation, sessions, monkeypatch, kind):
    item = sessions()
    other = sessions(name=OTHER)
    if kind == "other_socket":
        item.data["socketPath"] = str(other.socket_path)
    elif kind == "outside":
        item.data["socketPath"] = "/tmp/private-secret.sock"
    elif kind == "symlink":
        item.socket_path.unlink()
        item.socket_path.symlink_to(other.socket_path)
    else:
        item.server.close()
        item.socket_path.unlink()
        item.socket_path.write_text("private-secret")
    item.record.write_text(json.dumps(item.data))

    def must_not_run(*args, **kwargs):
        pytest.fail("未确认归属的 socket 不得传给官方 close")

    monkeypatch.setattr(subprocess, "run", must_not_run)
    result = installation.helper.cleanup(OWN, installation.helper.probe()["context"])
    assert result["status"] == "skipped"
    assert result["counts"]["closed"] == 0
    assert other.socket_path.exists()


def test_cleanup_does_not_claim_closed_when_socket_is_unlinked_but_daemon_connection_survives(installation, sessions, monkeypatch):
    item = sessions()

    def unlink_only(argv, **kwargs):
        item.socket_path.unlink(missing_ok=True)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", unlink_only)
    result = installation.helper.cleanup(OWN, installation.helper.probe()["context"])
    assert result["status"] == "retry"
    assert result["counts"]["closed"] == 0
    assert item.record.exists()


@pytest.mark.parametrize("alias", ["other_filename", "wrong_namespace", "linked_namespace"])
def test_cleanup_refuses_registry_aliases_that_official_cli_could_select_instead(installation, sessions, monkeypatch, alias):
    workspace = installation.cwd
    (workspace / ".playwright").mkdir()
    item = sessions(workspace=workspace)
    if alias == "other_filename":
        misleading = item.record.with_name("personal.session")
        misleading.write_text(item.record.read_text())
    elif alias == "wrong_namespace":
        misleading = item.record.parent.parent / "0000000000000000" / item.record.name
        misleading.parent.mkdir()
        misleading.write_text(item.record.read_text())
    else:
        misleading = item.record.parent.with_name("0000000000000000")
        misleading.symlink_to(item.record.parent, target_is_directory=True)

    def must_not_run(*args, **kwargs):
        pytest.fail("官方 Registry.load 有歧义时不得 close")

    monkeypatch.setattr(subprocess, "run", must_not_run)
    result = installation.helper.cleanup(OWN, installation.helper.probe()["context"])
    assert result["status"] == "skipped"
    assert result["reason"] == "unsafe_registry"
    assert item.socket_path.exists()


@pytest.mark.parametrize("change", ["home", "record", "workspace", "alias"])
def test_cleanup_revalidates_context_and_registry_after_close_before_retry(installation, sessions, monkeypatch, change):
    item = sessions()
    context = installation.helper.probe()["context"]
    calls = []

    def change_during_close(argv, **kwargs):
        calls.append(argv)
        if change == "home":
            monkeypatch.setenv("HOME", str(installation.home / "changed"))
        elif change == "record":
            item.record.write_text(json.dumps(dict(item.data, attached=True)))
        elif change == "workspace":
            (installation.cwd / ".playwright").mkdir(exist_ok=True)
        else:
            item.record.with_name("personal.session").write_text(item.record.read_text())
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", change_during_close)
    result = installation.helper.cleanup(OWN, context)
    assert result["status"] == "skipped"
    assert result["counts"]["closed"] == 0
    assert len(calls) == 1
    assert item.socket_path.exists()


def test_cleanup_bounds_registry_scan_before_any_cli_command(installation, sessions, monkeypatch):
    item = sessions()
    for index in range(300):
        (item.record.parent.parent / f"{index:016x}").mkdir(exist_ok=True)

    def must_not_run(*args, **kwargs):
        pytest.fail("超大 registry 应保守跳过")

    monkeypatch.setattr(subprocess, "run", must_not_run)
    result = installation.helper.cleanup(OWN, installation.helper.probe()["context"])
    assert result["status"] == "skipped"
    assert result["reason"] == "unsafe_registry"
    assert result["counts"]["closed"] == 0


def test_cleanup_has_one_total_time_budget_across_workspaces(installation, sessions, monkeypatch):
    sessions()
    workspace = installation.cwd.parent / "workspace-budget"
    workspace.mkdir()
    (workspace / ".playwright").mkdir()
    sessions(workspace=workspace)
    clock = [0.0]
    calls = []
    import time

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def timeout(argv, **kwargs):
        calls.append(kwargs["timeout"])
        clock[0] += kwargs["timeout"]
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    result = installation.helper.cleanup(OWN, installation.helper.probe()["context"])
    assert result["status"] == "retry"
    assert result["counts"]["retry"] == 2
    assert 0 < clock[0] <= 8.1
    assert sum(calls) <= 8


@pytest.mark.parametrize("kind", ["oversize_metadata", "linked_metadata", "missing_core", "missing_node", "relative_tmp", "linked_cache", "linked_marker"])
def test_probe_rejects_unconfirmed_runtime_paths_read_only(installation, kind):
    if kind == "oversize_metadata":
        path = installation.package / "package.json"
        path.write_text(path.read_text() + " " * 70000)
    elif kind == "linked_metadata":
        path = installation.package / "package.json"
        target = path.with_suffix(".private")
        path.rename(target)
        path.symlink_to(target)
    elif kind == "missing_core":
        (installation.core / "lib/coreBundle.js").unlink()
    elif kind == "missing_node":
        (Path(installation.env["PATH"]) / "node").unlink()
    elif kind == "relative_tmp":
        installation.env["TMPDIR"] = "relative"
    elif kind == "linked_cache":
        (installation.home / ".cache").symlink_to(installation.cwd, target_is_directory=True)
    else:
        (installation.cwd / ".playwright").symlink_to(installation.home, target_is_directory=True)
    result = invoke("probe", env=installation.env, cwd=installation.cwd)
    assert result["status"] in {"skipped", "unsupported"}
    assert "context" not in result


def test_probe_records_runtime_identity_and_skips_replaced_executable(installation):
    context = installation.helper.probe()["context"]
    assert context["node_path"] == str(Path(sys.executable).resolve())
    replacement = installation.executable.with_suffix(".new")
    replacement.write_text("#!/bin/sh\nexit 0\n")
    replacement.chmod(0o755)
    replacement.replace(installation.executable)
    result = installation.helper.cleanup(OWN, context)
    assert result["status"] == "skipped"
    assert result["reason"] == "context_mismatch"


@pytest.mark.parametrize("context", [None, [], {"cwd": None}, True])
def test_cleanup_public_boundary_returns_json_for_invalid_context_types(installation, context):
    result = installation.helper.cleanup(OWN, context)
    assert result["status"] == "skipped"
    assert "private-secret" not in json.dumps(result)


def test_cleanup_rejects_oversize_expected_context_before_parsing(installation):
    result = invoke("cleanup", OWN, " " * 20000 + "{}", env=installation.env, cwd=installation.cwd)
    assert result["status"] == "skipped"
    assert result["reason"] == "invalid_arguments"


def test_probe_rejects_dangling_daemon_root_link(installation):
    cache = installation.home / ".cache/ms-playwright"
    cache.mkdir(parents=True)
    (cache / "daemon").symlink_to(installation.home / "missing")
    result = installation.helper.probe()
    assert result["status"] == "skipped"


def test_cleanup_rejects_context_with_wrong_cwd_type_without_throwing(installation):
    context = installation.helper.probe()["context"]
    context["cwd"] = ["private-secret"]
    result = installation.helper.cleanup(OWN, context)
    assert result["status"] == "skipped"
    assert "private-secret" not in json.dumps(result)


def test_probe_bounds_even_valid_but_very_long_context_output(installation):
    deep = installation.cwd.joinpath(*(["d" * 190] * 18))
    deep.mkdir(parents=True)
    moved = deep / "cli"
    installation.package.rename(moved)
    binary = Path(installation.env["PATH"]) / "playwright-cli"
    binary.unlink()
    binary.symlink_to(moved / "playwright-cli.js")
    installation.env["HOME"] = str(deep / "home")
    installation.env["TMPDIR"] = str(deep / "temp")
    result = invoke("probe", env=installation.env, cwd=installation.cwd)
    assert result == {"status": "skipped", "reason": "context_too_large"}


def test_cleanup_rejects_workspace_alias_even_if_its_record_hash_matches(installation, sessions, monkeypatch):
    real = installation.cwd.parent / "real-workspace"
    real.mkdir()
    (real / ".playwright").mkdir()
    alias = installation.cwd.parent / "linked-workspace"
    alias.symlink_to(real, target_is_directory=True)
    sessions(workspace=alias)
    sessions(workspace=real, cli={"persistent": True})

    def must_not_run(*args, **kwargs):
        pytest.fail("cwd 的 realpath 与记录 workspace 不同，不得关闭其他 namespace")

    monkeypatch.setattr(subprocess, "run", must_not_run)
    result = installation.helper.cleanup(OWN, installation.helper.probe()["context"])
    assert result["status"] == "skipped"
    assert result["counts"]["closed"] == 0


def test_probe_does_not_apply_linux_namespace_rules_on_other_platforms(installation, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert installation.helper.probe() == {"status": "unsupported", "reason": "unsupported_platform"}


def test_cleanup_context_comparison_is_type_strict(installation):
    context = installation.helper.probe()["context"]
    context["schema"] = True
    result = installation.helper.cleanup(OWN, context)
    assert result["status"] == "skipped"
    assert result["reason"] == "context_mismatch"


def test_probe_without_cli_is_read_only_and_unsupported(tmp_path):
    home = tmp_path / "not-created"
    result = invoke("probe", env={"HOME": str(home), "PATH": ""}, cwd=tmp_path)
    assert result == {"status": "unsupported", "reason": "cli_not_found"}
    assert not home.exists()


def test_standalone_entry_rejects_invalid_arguments_without_echoing_secrets():
    result = invoke("unknown", "cookie=private-secret")
    assert result["status"] == "skipped"
    assert result["reason"] == "invalid_arguments"
    assert "private-secret" not in json.dumps(result)
