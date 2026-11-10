"""两镜像本地验收：真实执行、采集与停止，不使用模型或业务数据库。"""

import base64
import json
import os
from pathlib import Path
import shlex
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import docker
import pytest

from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.traffic import ProjectTrafficRuntime, WORKSPACE
from cairn.dispatcher.runtime.traffic_execution import TaskTrafficExecution, TrafficExecutionBridge
from test_traffic_egress_integration import PROXY, execute, wait_for
from test_traffic_execution_integration import SITE, certificate


def test_selected_research_image_is_used_by_executor(tmp_path, monkeypatch):
    client = Mock()
    client.resolve_task_test_egress.return_value = {'mode': 'direct'}
    client.register_traffic_scope.return_value = SimpleNamespace(ok=True)
    bridge = TrafficExecutionBridge(
        None, client, root=tmp_path, dispatcher_id='synthetic',
        config=SimpleNamespace(image='default-research', traffic_gateway_image='gateway'),
    )
    project = SimpleNamespace(id='synthetic-project', scenario_snapshot={
        'features': {'http_traffic_execution_version': 1},
    })
    # 恢复建立的默认 runtime 不得覆盖随后各 TaskRun 的画像选图。
    runtime = bridge._runtime(project.id)
    runtime.manifest['scopes'] = {}
    monkeypatch.setattr(runtime, 'prepare', lambda **kwargs: None)
    monkeypatch.setattr(runtime, 'check_scope', lambda scope_id: None)
    monkeypatch.setattr(bridge, '_start_sync', lambda runtime: None)
    selected = []

    def prepare_scope(task_run_id, generation, *, test_egress, executor_image):
        selected.append(executor_image)
        runtime.manifest['scopes'][task_run_id] = {'test_egress': test_egress}
        return SimpleNamespace(
            id=task_run_id, task_run_id=task_run_id, project_id=project.id,
            generation=generation, container_id='synthetic-container', capability=lambda: {},
        )

    monkeypatch.setattr(runtime, 'prepare_scope', prepare_scope)
    for image in ('custom-research', 'other-research'):
        bridge.prepare_task(project, image, TaskCancellation(generation=0), image=image)
    assert selected == ['custom-research', 'other-research']
    assert runtime.executor_image == 'default-research'
    assert runtime.gateway_image == 'gateway'


@pytest.mark.skipif(os.environ.get('CAIRN_TRAFFIC_INTEGRATION') != '1', reason='需显式启用本地隔离 Docker 验收')
def test_shared_research_image_capture_and_cancellation():
    client = docker.from_env()
    # 解析本地身份，不自动拉取，也不修改默认镜像标签。
    image = client.images.get(os.environ['CAIRN_TEST_BROWSER_IMAGE']).id
    gateway = client.images.get('arena-traffic-gateway:local').id
    with tempfile.TemporaryDirectory(prefix='research-') as temporary:
        root = Path(temporary)
        site = root / 'site'
        site.mkdir()
        ca = certificate(site)
        (site / 'server.py').write_text(SITE)
        proxy_files = root / 'proxy'
        proxy_files.mkdir(mode=0o700)
        auth = {'username': 'synthetic-user', 'password': 'synthetic-secret'}
        (proxy_files / 'auth.json').write_text(json.dumps(auth))
        runtime = ProjectTrafficRuntime(
            client, project_id='synthetic-research', root=root / 'runtime',
            gateway_image=gateway, executor_image='unused-default-research', isolated=True,
        )
        bridge = TrafficExecutionBridge(client, None, root=root, dispatcher_id='synthetic', config=None)
        watcher = threading.Thread(target=bridge._watch, daemon=True)
        watcher.start()
        resources = []
        try:
            # 原 Worker 启动方式不变；无目标网络、模型凭据或模型调用。
            worker = client.containers.run(image, ['sleep', 'infinity'], detach=True,
                                           network_mode='none', environment={'CONTROL_ONLY_SECRET': 'synthetic'})
            resources.append(worker)
            assert worker.exec_run(['cairn-agent', '--self-check']).exit_code == 0
            runtime.prepare(upstream_ca=site / 'ca.pem')
            target = client.containers.run(
                gateway, ['sh', '-c', 'while ! test -f /site/server.key; do sleep .1; done; exec python /site/server.py'],
                entrypoint=[], detach=True, network=runtime.egress.name,
                volumes={str(site): {'bind': '/site', 'mode': 'ro'}},
            )
            resources.append(target)
            target.reload()
            address = target.attrs['NetworkSettings']['Networks'][runtime.egress.name]['IPAddress']
            certificate(site, issuer=ca, address=address)
            proxy = client.containers.run(
                gateway, ['python', '-c', PROXY], entrypoint=[], detach=True,
                network=runtime.egress.name, volumes={str(proxy_files): {'bind': '/proxy', 'mode': 'ro'}},
            )
            resources.append(proxy)
            proxy.reload()
            proxy_ip = proxy.attrs['NetworkSettings']['Networks'][runtime.egress.name]['IPAddress']
            wait_for(lambda: proxy.exec_run(['python', '-c',
                "import socket; socket.create_connection(('127.0.0.1',9567),1).close()"]).exit_code == 0)
            for mode in ('direct', 'http', 'socks5'):
                egress = {'mode': mode}
                if mode != 'direct':
                    egress.update(host=proxy_ip, port=9567, **auth)
                scope = runtime.prepare_scope(
                    f'synthetic-{mode}', 0, test_egress=egress, executor_image=image,
                )
                cancellation = TaskCancellation(generation=0)
                task = TaskTrafficExecution(runtime, scope.id, cancellation)
                cancellation.register_resource(task)
                with bridge._lock:
                    bridge._tasks[(runtime.project_id, scope.task_run_id)] = task
                executor = client.containers.get(scope.container_id)
                assert executor.image.id == worker.image.id
                assert executor.attrs['Config']['Entrypoint'] == ['python3', '-u', '/opt/cairn/task-executor/server.py']
                assert {mount['Destination'] for mount in executor.attrs['Mounts']} == {
                    WORKSPACE, '/run/cairn-exec', '/run/cairn-ca/project.pem',
                }
                collector = next(item for item in runtime.containers
                                 if item.labels.get('io.cairn.traffic.role') == 'collector'
                                 and item.labels.get('io.cairn.traffic.scope') == scope.id)
                assert collector.image.id == gateway
                assert not any('task-executor' in part for part in collector.attrs['Config']['Entrypoint'])
                assert not any(mount['Destination'] == WORKSPACE for mount in collector.attrs['Mounts'])
                commands = ['test -z "$CONTROL_ONLY_SECRET"']
                for scheme, port in (('http', 8989), ('https', 9443)):
                    commands.append(f"curl -fsS --max-time 8 '{scheme}://{address}:{port}/{mode}-{scheme}'")
                code, text = execute(scope, ' && '.join(commands))
                assert code == 0 and text.count('traffic-synthetic') == 2, text
                code, text = execute(scope, f"playwright-cli open 'https://{address}:9443/{mode}-browser'")
                assert code == 0 and 'traffic-synthetic' in text, text
                code, text = execute(scope, "playwright-cli eval 'document.title'")
                assert code == 0 and 'traffic-synthetic' in text, text
                def records():
                    return [json.loads(path.read_text()) for path in (runtime.capture / scope.id).glob('flows/*/flow.json')]
                paths = {f'/{mode}-http', f'/{mode}-https', f'/{mode}-browser'}
                observed = wait_for(lambda: records() if paths <= {
                    base64.b64decode(flow['request']['target_base64']).decode()
                    for flow in records() if flow['state'] == 'complete'
                } else None)
                assert all(flow['scope']['task_run_id'] == scope.task_run_id for flow in observed)
                # 保留浏览器和脱离会话的后台进程，取消必须停止整个执行容器。
                marker = runtime.workspace / f'{mode}-ticks'
                script = f"import subprocess; subprocess.Popen(['sh','-c','while :; do printf x >> {mode}-ticks; sleep .1; done'], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
                assert execute(scope, 'python3 -c ' + shlex.quote(script))[0] == 0
                wait_for(marker.exists)
                cancellation.cancel('synthetic_cancel')
                wait_for(lambda: task.execution_completed)
                with pytest.raises(docker.errors.NotFound):
                    executor.reload()
                size = marker.stat().st_size
                time.sleep(.3)
                assert marker.stat().st_size == size
                assert not (runtime.root / 'egress' / scope.id / 'private.json').exists()
            worker.reload()
            assert worker.status == 'running'
        finally:
            bridge._stop.set()
            watcher.join(timeout=5)
            for resource in reversed(resources):
                resource.remove(force=True)
            runtime.close()
            client.close()


@pytest.mark.skipif(os.environ.get('CAIRN_TRAFFIC_INTEGRATION') != '1', reason='需显式启用本地隔离 Docker 验收')
def test_owner_connection_pause_capture_and_orphan_cleanup(monkeypatch):
    """真实镜像、网络采集、热暂停与脱离进程组的后台任务，无模型或业务数据库。"""
    client = docker.from_env()
    image = client.images.get(os.environ['CAIRN_TEST_BROWSER_IMAGE']).id
    gateway = client.images.get('arena-traffic-gateway:local').id
    with tempfile.TemporaryDirectory(prefix='owner-docker-') as temporary:
        root = Path(temporary)
        site = root / 'site'
        site.mkdir()
        ca = certificate(site)
        (site / 'server.py').write_text(SITE)
        runtime = ProjectTrafficRuntime(
            client, project_id='synthetic-owner', root=root / ('dispatcher-traffic-v1-' + 'a' * 64) / 'project',
            gateway_image=gateway, executor_image=image, isolated=True,
        )
        target = None
        command_thread = None
        try:
            runtime.prepare(upstream_ca=site / 'ca.pem')
            target = client.containers.run(
                gateway, ['sh', '-c', 'while ! test -f /site/server.key; do sleep .1; done; exec python /site/server.py'],
                entrypoint=[], detach=True, network=runtime.egress.name,
                volumes={str(site): {'bind': '/site', 'mode': 'ro'}},
            )
            target.reload()
            address = target.attrs['NetworkSettings']['Networks'][runtime.egress.name]['IPAddress']
            certificate(site, issuer=ca, address=address)
            wait_for(lambda: target.exec_run(['python', '-c',
                "import socket; socket.create_connection(('127.0.0.1',8989),1).close()"]).exit_code == 0)
            monkeypatch.setattr(runtime, '_start_egress', Mock(side_effect=AssertionError('direct 不应解析代理 DNS')))
            scope = runtime.prepare_scope('synthetic-owner-run', 0, test_egress={'mode': 'direct'})
            owner = runtime._owners[scope.id]
            assert not owner.get_inheritable()
            executor = client.containers.get(scope.container_id)
            assert execute(scope, f"curl -fsS --max-time 5 http://{address}:8989/owner-before")[0] == 0

            outcomes = []
            command_thread = threading.Thread(target=lambda: outcomes.append(execute(
                scope, 'touch command-started; sleep 2; printf resumed-original-command',
            )))
            command_thread.start()
            wait_for(lambda: (runtime.workspace / 'command-started').exists())
            runtime.hot_control(True)
            executor.reload()
            assert executor.status == 'paused'
            time.sleep(11)
            assert runtime._owners[scope.id] is owner
            runtime.hot_control(False)
            runtime.hot_resume_completed()
            runtime.pause_clock.resume()
            command_thread.join(10)
            assert outcomes == [(0, 'resumed-original-command')]
            assert runtime._owners[scope.id] is owner
            assert execute(scope, f"curl -fsS --max-time 5 http://{address}:8989/owner-after")[0] == 0

            def captured_paths():
                return {base64.b64decode(record['request']['target_base64']).decode()
                        for path in (runtime.capture / scope.id).glob('flows/*/flow.json')
                        if (record := json.loads(path.read_text()))['state'] == 'complete'}

            wait_for(lambda: {'/owner-before', '/owner-after'} <= captured_paths())
            # 命令已返回但其 setsid 后代仍运行；仅断开 owner，不借助 Docker stop 证明清理。
            script = "import subprocess; subprocess.Popen(['sh','-c','while :; do printf x >> orphan-ticks; sleep .1; done'], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
            assert execute(scope, 'python3 -c ' + shlex.quote(script))[0] == 0
            marker = runtime.workspace / 'orphan-ticks'
            wait_for(marker.exists)
            runtime._owners.pop(scope.id).close()

            def exited():
                executor.reload()
                return executor.status == 'exited'

            wait_for(exited)
            assert executor.attrs['State']['ExitCode'] == 75
            size = marker.stat().st_size
            time.sleep(.3)
            assert marker.stat().st_size == size
            assert runtime.check_scope(scope.id) == 'traffic_executor_stopped'
        finally:
            if target is not None:
                target.remove(force=True)
            runtime.destroy()
            if command_thread is not None:
                command_thread.join(5)
            client.close()
