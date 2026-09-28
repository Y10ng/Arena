"""显式 opt-in 的本地浏览器验收；仅创建并清理本测试的无网络、无挂载容器。"""

from __future__ import annotations

import io
import json
import re
import tarfile
import time
import uuid
from pathlib import Path

import pytest

from browser_integration_support import local_docker_client, resolve_browser_image
from cairn.dispatcher.config import ContainerConfig
from cairn.dispatcher.runtime.containers import ContainerManager, container_instance_id
from cairn.dispatcher.runtime.process import ManagedProcess


MEASURE = r'''
import json, pathlib
processes = {}
for directory in pathlib.Path('/proc').iterdir():
    if not directory.name.isdigit():
        continue
    try:
        fields = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
        args = (directory / 'cmdline').read_bytes().split(b'\0')
        root = args[0].endswith(b'node') and any(arg.endswith(b'/cliDaemon.js') for arg in args)
        processes[int(directory.name)] = (int(fields[1]), root, fields[0])
    except (OSError, IndexError):
        pass
roots = {pid for pid, (_, root, _) in processes.items() if root}
family = set(roots)
while True:
    added = {pid for pid, (parent, _, _) in processes.items() if parent in family} - family
    if not added:
        break
    family.update(added)
pss = 0
for pid in family:
    try:
        pss += sum(int(line.split()[1]) for line in pathlib.Path(f'/proc/{pid}/smaps_rollup').read_text().splitlines() if line.startswith('Pss:'))
    except OSError:
        pass
families = {}
for root in roots:
    members = {root}
    while True:
        added = {pid for pid, (parent, _, _) in processes.items() if parent in members} - members
        if not added:
            break
        members.update(added)
    families[str(root)] = sorted(members)
print(json.dumps({'roots': sorted(roots), 'families': families, 'browser_pss_kib': pss, 'zombies': sum(state == 'Z' for _, _, state in processes.values()), 'cgroup_bytes': int(pathlib.Path('/sys/fs/cgroup/memory.current').read_text())}))
'''


# 单浏览器功能验收与三浏览器并发验收分开报告；前者不能替代后者。
@pytest.mark.parametrize("sidecars", [False, True], ids=["single-browser-workflow", "concurrent-lifecycle"])
def test_task_cleanup_preserves_other_browsers_and_reaps_orphans(monkeypatch, sidecars):
    image = resolve_browser_image()
    # 仅在测试内约束管理器的连接工厂，不改变生产环境的 Docker 配置方式。
    monkeypatch.setattr("cairn.dispatcher.runtime.containers.docker.from_env", local_docker_client)
    project = "browser_test_" + uuid.uuid4().hex
    # 每次验收使用独立owner，既满足归属校验，也不会进入任何线上实例的orphan集合。
    manager = ContainerManager(
        ContainerConfig(image=image, network_mode="none", cap_add=[], completed_action="remove"),
        instance_id=container_instance_id("browser-validation-" + uuid.uuid4().hex, "http://browser-validation.invalid"),
    )
    client = local_docker_client()
    container = None
    measurements = []
    try:
        # 不使用会在 ImageNotFound 时自动 pull 的 run；镜像消失时直接失败。
        container = client.containers.create(
            image, ["sleep", "infinity"], detach=True, init=True,
            name=manager.container_name(project), network_mode="none",
            labels={
                **manager._container_labels(project, manager.default_runtime_options()),
                "cairn.test": "browser-session-integration",
            },
        )
        container.start()
        manager.configure_browser_sessions("task")

        def execute(command, env=None, session=None):
            process = ManagedProcess(container, command, env or {})
            process.start()
            if session is not None:
                manager.browser_sessions.register_exec(session, process.execution_identity)
            result = process.communicate(timeout=30)
            if session is not None:
                manager.browser_sessions.finish_exec(
                    session, process.execution_identity, completed=process.execution_completed,
                )
            try:
                assert result.returncode == 0 and not result.timed_out, result.stdout + result.stderr
                return result.read_stdout()
            finally:
                result.cleanup()

        def cli(env, session, *arguments, expected_error=None):
            output = execute(["playwright-cli", *arguments], {**env, "NO_UPDATE_NOTIFIER": "1"}, session)
            if expected_error is None:
                assert "### Error" not in output, output
            else:
                assert "### Error" in output and expected_error in output, output
            return output

        def cli_result(env, session, *arguments):
            output = cli(env, session, *arguments)
            assert "### Result\n" in output, output
            return json.JSONDecoder().raw_decode(output.split("### Result\n", 1)[1].lstrip())[0]

        def measure(label):
            data = json.loads(execute(["python3", "-c", MEASURE]))
            measurements.append({"label": label, **data})
            return data

        execute(["python3", "-c", "import os; assert os.environ['PLAYWRIGHT_MCP_BROWSER'] == 'chromium'; assert os.environ['PLAYWRIGHT_MCP_EXECUTABLE_PATH'] == '/usr/bin/chromium'"])
        # 通过归档注入纯合成站点，不挂载宿主文件；浏览器使用镜像默认配置，不覆写启动选项。
        fixture = Path(__file__).parent / "fixtures" / "browser_session_site.py"
        with io.BytesIO() as buffer:
            with tarfile.open(fileobj=buffer, mode="w") as archive:
                archive.add(fixture, arcname="cairn-browser-site.py", recursive=False)
            assert container.put_archive("/tmp", buffer.getvalue())
        execute(["python3", "-c", "import subprocess; subprocess.Popen(['python3','/tmp/cairn-browser-site.py'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)"])
        execute(["tmux", "new-session", "-d", "-s", "cairn-test-persistent", "sleep infinity"])
        execute(["python3", "-c", """
import time, urllib.error, urllib.request
for attempt in range(50):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8765', timeout=1) as response:
            assert response.status == 200
        break
    except urllib.error.URLError:
        time.sleep(0.1)
else:
    raise AssertionError('synthetic browser site did not start')
"""])
        url = "http://127.0.0.1:8765"
        if sidecars:
            b_env, b_session = manager.prepare_browser_session(
                container.name, {}, project_id=project, task_run_id="browser_b",
            )
            assert b_session is not None
            cli(b_env, b_session, "open", url)
            cli(b_env, b_session, "eval", "window.cairnB = 'B-still-alive'")
            long_env = {"PLAYWRIGHT_CLI_SESSION": "cairn-test-long-lived"}
            cli(long_env, None, "open", url)
            cli(long_env, None, "eval", "window.cairnLong = 'long-still-alive'")
        baseline = measure("baseline")
        assert len(baseline["roots"]) == (2 if sidecars else 0)
        for index in range(5 if sidecars else 1):
            task = f"browser_a_{index}"
            env, session = manager.prepare_browser_session(
                container.name, {}, project_id=project, task_run_id=task,
            )
            assert session is not None
            cli(env, session, "open", url)
            if index == 0:
                # 同一个请求先只读取响应头，再消费正文；不能用第二次重放掩盖采集等待。
                assert cli_result(env, session, "eval", "async () => { window.cairnPendingResponse = await fetch('/api/private'); return window.cairnPendingResponse.status; }") == 401
                pending = cli(env, session, "requests", "--filter=/api/private$")
                pending_indexes = re.findall(r"(?m)^(\d+)\. \[GET\] " + re.escape(url + "/api/private") + r" => \[401\]", pending)
                assert len(pending_indexes) == 1, pending
                cli(env, session, "response-body", pending_indexes[0], expected_error="response_body_incomplete")
                consumed = cli_result(env, session, "eval", "async () => await window.cairnPendingResponse.json()")
                assert consumed == {"error": "unauthorized"}
                assert '"error": "unauthorized"' in cli(env, session, "response-body", pending_indexes[0])
            assert cli_result(env, session, "eval", "async () => (await fetch('/api/private')).status") == 401
            if index == 0:
                # 先证明错误的合成凭据不能获得会话，避免只验证前端表单外观。
                cli(env, session, "run-code", """async (page) => {
                    await page.locator('#username').fill('synthetic-user');
                    await page.locator('#password').fill('wrong-password');
                    await page.locator('#login button').click();
                    await page.waitForFunction(() => document.getElementById('login-state').textContent === 'invalid-credentials');
                }""")
                assert cli_result(env, session, "eval", "async () => (await fetch('/api/private')).status") == 401
            cookie = cli_result(env, session, "run-code", """async (page) => {
                await page.locator('#username').fill('synthetic-user');
                await page.locator('#password').fill('synthetic-password');
                await page.locator('#login button').click();
                await page.waitForFunction(() => document.getElementById('login-state').textContent === 'signed-in');
                return (await page.context().cookies()).find(cookie => cookie.name === 'cairn_fixture_session');
            }""")
            assert cookie["httpOnly"] is True
            assert cookie["value"] == "synthetic-session"
            # 使用 CLI 已有请求记录，不为普通采集自建事件监听器；正文必须单独读取。
            requests = cli(env, session, "requests", "--filter=/login$")
            indexes = re.findall(r"(?m)^(\d+)\. \[POST\] " + re.escape(url + "/login") + r" => \[200\]", requests)
            assert len(indexes) == 1, requests
            login = {part: cli(env, session, part, indexes[0]) for part in ("request", "request-body", "response-body")}
            assert url + "/login" in login["request"] and "[POST]" in login["request"]
            assert "content-type: application/x-www-form-urlencoded" in login["request"]
            assert "username=synthetic-user&password=synthetic-password" in login["request-body"]
            assert '"user": "synthetic-user"' in login["response-body"]
            marker = f"state-{index}"
            cli(env, session, "eval", f"(() => {{ document.cookie='cairn={marker}'; document.getElementById('state').textContent='{marker}'; window.cairnState='{marker}'; return true; }})()")
            next_env, next_session = manager.prepare_browser_session(
                container.name, {}, project_id=project, task_run_id=task,
            )
            assert next_session == session
            state = cli(next_env, next_session, "eval", "JSON.stringify([document.cookie, document.getElementById('state').textContent, window.cairnState])")
            assert state.count(marker) == 3
            # 再次获取同一 TaskRun 会话后，服务端仍能识别 HttpOnly 登录态。
            authenticated = cli_result(next_env, next_session, "eval", """async () => {
                const response = await fetch('/api/private');
                return {status: response.status, body: await response.json(), visibleCookie: document.cookie};
            }""")
            assert authenticated["status"] == 200
            assert authenticated["body"] == {"user": "synthetic-user"}
            assert "cairn_fixture_session" not in authenticated["visibleCookie"]
            cli(next_env, next_session, "run-code", """async (page) => {
                await page.locator('#load-data').click();
                await page.waitForFunction(() => document.getElementById('dynamic-data').textContent === 'synthetic-dynamic-data');
            }""")
            resources = cli_result(next_env, next_session, "eval", """() => ({
                scripts: Array.from(document.scripts).map(script => script.src).filter(Boolean),
                requests: performance.getEntriesByType('resource').map(entry => entry.name),
                marker: window.cairnExternalScript
            })""")
            assert resources["marker"] == "external-js-loaded"
            assert resources["scripts"] == [url + "/app.js"]
            assert url + "/app.js" in resources["requests"]
            assert url + "/api/data" in resources["requests"]
            evidence_dir = f"/tmp/cairn-browser-evidence/cycle-{index}"
            # 下载浏览器实际提取的脚本 URL；所有证据仅保留在本次隔离容器内。
            execute(["python3", "-c", """
import json, pathlib, sys, urllib.request
root = pathlib.Path(sys.argv[2])
root.mkdir(parents=True)
(root / 'login-request-response.json').write_text(sys.argv[3])
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for index, source in enumerate(json.loads(sys.argv[1])):
    with opener.open(source, timeout=5) as response:
        assert response.status == 200
        assert response.headers.get_content_type() == 'application/javascript'
        body = response.read()
    assert b'external-js-loaded' in body and b'/api/data' in body
    (root / f'script-{index}.js').write_bytes(body)
""", json.dumps(resources["scripts"]), evidence_dir, json.dumps(login)])
            cli(next_env, next_session, "screenshot", f"--filename={evidence_dir}/page.png")
            execute(["python3", "-c", """
import pathlib, struct, sys
root = pathlib.Path(sys.argv[1])
script = (root / 'script-0.js').read_bytes()
assert b'external-js-loaded' in script and b'/api/data' in script
image = (root / 'page.png').read_bytes()
assert len(image) > 100 and image[:8] == b'\\x89PNG\\r\\n\\x1a\\n'
width, height = struct.unpack('>II', image[16:24])
assert width > 0 and height > 0
""", evidence_dir])
            active = measure(f"cycle_{index}_active")
            assert len(active["roots"]) == (3 if sidecars else 1)
            owned_roots = set(active["roots"]) - set(baseline["roots"])
            owned_pids = {pid for root in owned_roots for pid in active["families"][str(root)]}
            manager.browser_sessions.local_finished(project, task)
            assert manager.browser_sessions.claim_ready() == []
            manager.browser_sessions.terminal_confirmed(project, task)
            assert manager.browser_sessions.claim_ready() == [session]
            status = manager.cleanup_browser_session(session)
            assert status == "closed", status
            manager.browser_sessions.finish_cleanup(session, status)
            time.sleep(0.3)
            after = measure(f"cycle_{index}_cleaned")
            assert after["roots"] == baseline["roots"]
            execute(["python3", "-c", f"import pathlib; assert not any(pathlib.Path('/proc/' + str(pid)).exists() for pid in {sorted(owned_pids)!r})"])
            if sidecars:
                assert "B-still-alive" in cli(b_env, b_session, "eval", "window.cairnB")
                assert "long-still-alive" in cli(long_env, None, "eval", "window.cairnLong")
            execute(["tmux", "has-session", "-t", "cairn-test-persistent"])
        execute(["python3", "-c", "import os,time,pathlib; pid=os.fork();\nif pid == 0:\n pathlib.Path('/tmp/cairn-orphan-pid').write_text(str(os.getpid())); time.sleep(0.3); os._exit(0)\nos._exit(0)"])
        time.sleep(0.6)
        execute(["python3", "-c", "import pathlib; pid=pathlib.Path('/tmp/cairn-orphan-pid').read_text(); assert not pathlib.Path('/proc/'+pid).exists()"])
        assert measure("after_orphan_exit")["zombies"] == 0
        print("BROWSER_PROFILE=" + ("concurrent-lifecycle" if sidecars else "single-browser-workflow"))
        print("BROWSER_INTEGRATION=" + json.dumps(measurements))
    finally:
        if container is not None:
            container.remove(force=True)
        client.close()
        manager.close()
