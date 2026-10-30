"""定向 opt-in：真实 API、执行 socket、代理侧连接和落库原件，不调用模型。"""

import base64
import json
import os
from pathlib import Path
import shlex
import socket
import tempfile
import time
from types import SimpleNamespace

import docker
import pytest

from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.traffic import WORKSPACE
from cairn.dispatcher.runtime.traffic_execution import TrafficExecutionBridge
from test_traffic_execution_integration import SITE, certificate
from traffic_api_fixture import traffic_api, postgres_instance, empty_postgres_url  # noqa: F401


PROXY = '''
import asyncio, json, pproxy
from pathlib import Path
async def main():
    auth = json.loads(Path('/proxy/auth.json').read_text())
    server = await pproxy.Server('http+socks5://0.0.0.0:9567#' + auth['username'] + ':' + auth['password']).start_server({'rserver': []})
    async with server: await server.serve_forever()
asyncio.run(main())
'''


def execute(scope, command):
    body = json.dumps({'scope_id': scope.id, 'task_run_id': scope.task_run_id,
                       'command': command, 'cwd': WORKSPACE, 'timeout_seconds': 25}).encode()
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(30)
        connection.connect(str(scope.socket_directory / 'execute.sock'))
        connection.sendall(b'POST /execute HTTP/1.0\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
        response = b''
        while chunk := connection.recv(65536):
            response += chunk
    header, body = response.split(b'\r\n\r\n', 1)
    assert b' 200 ' in header
    events = [json.loads(line) for line in body.splitlines()]
    text = ''.join(base64.b64decode(event.get('stdout', event.get('stderr', ''))).decode() for event in events)
    return events[-1].get('exit_code'), text


def wait_for(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.1)
    raise AssertionError('controlled egress condition timed out')


@pytest.mark.skipif(
    os.environ.get('CAIRN_TRAFFIC_INTEGRATION') != '1' or os.environ.get('CAIRN_TEST_POSTGRES_BOOTSTRAP') != '1',
    reason='需显式启用隔离 Docker 与私有 PostgreSQL 实验',
)
def test_http_and_socks_upstream_capture(traffic_api):
    api = traffic_api
    client = docker.from_env()
    image = 'arena-traffic-gateway:egress-prototype'
    with tempfile.TemporaryDirectory(prefix='egress-') as temporary:
        root = Path(temporary)
        site = root / 'site'
        site.mkdir()
        ca = certificate(site)
        site_code = SITE.replace('    def do_GET(self):', "    def do_GET(self):\n        with (site / 'requests').open('a') as out: out.write(self.client_address[0] + ' ' + self.path + '\\n')")
        site_code = site_code.replace('b.serve_forever()', '''
def echo_tcp():
    listener = socket.socket()
    listener.bind(('0.0.0.0', 9777))
    listener.listen()
    while True:
        connection, peer = listener.accept()
        with connection:
            value = connection.recv(1024)
            with (site / 'tcp-requests').open('a') as out: out.write(peer[0] + '\\n')
            connection.sendall(value)
def echo_udp():
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(('0.0.0.0', 9778))
    while True:
        value, peer = listener.recvfrom(1024)
        (site / 'udp-received').touch()
        listener.sendto(value, peer)
threading.Thread(target=echo_tcp, daemon=True).start()
threading.Thread(target=echo_udp, daemon=True).start()
b.serve_forever()
''')
        (site / 'server.py').write_text(site_code)
        auth = {'username': 'synthetic-egress-user', 'password': 'synthetic-egress-secret'}
        proxy_files = root / 'proxy'
        proxy_files.mkdir(mode=0o700)
        (proxy_files / 'auth.json').write_text(json.dumps(auth))
        (proxy_files / 'auth.json').chmod(0o600)
        bridge = TrafficExecutionBridge(
            client, api.machine, root=root / 'runtime',
            dispatcher_id=api.task_runs[0]['runtime_context']['dispatcher_id'],
            config=SimpleNamespace(traffic_gateway_image=image, task_executor_image='arena-task-executor:local'),
            isolated=True, upstream_ca=site / 'ca.pem',
        )
        runtime = bridge._runtime(api.project_id)
        resources, scopes, paths = [], [], []
        prefix = f'{api.base_url}/projects/{api.project_id}'
        def get(path):
            response = api.user.get(prefix + path, timeout=10)
            assert response.status_code == 200
            return response.json()
        def flows():
            return get('/traffic?limit=200')['items']
        def run(scope, command):
            code, text = execute(scope, command)
            assert code == 0, text
            return text
        def curl(scope, path, *, scheme='http', host=None, flags='--noproxy "*"'):
            paths.append(path)
            return run(scope, f'curl -fsS --max-time 8 {flags} {scheme}://{host or address}:{9443 if scheme == "https" else 8989}{path}')
        def configure(mode, proxy_ip, **credentials):
            response = api.user.put(prefix + '/test-egress', json={
                'mode': mode, 'host': proxy_ip, 'port': 9567,
                'auth_action': 'replace', **credentials,
            }, timeout=10)
            assert response.status_code == 200
            return response.json()
        try:
            runtime.prepare(upstream_ca=site / 'ca.pem')
            target = client.containers.run(image, ['sh', '-c', 'while ! test -f /site/server.key; do sleep .1; done; exec python /site/server.py'],
                                          entrypoint=[], detach=True, network=runtime.egress.name,
                                          volumes={str(site): {'bind': '/site', 'mode': 'rw'}})
            resources.append(target)
            target.reload()
            address = target.attrs['NetworkSettings']['Networks'][runtime.egress.name]['IPAddress']
            certificate(site, issuer=ca, address=address)
            proxies = []
            for _ in range(2):
                proxy = client.containers.run(image, ['python', '-c', PROXY], entrypoint=[], detach=True,
                                             network=runtime.egress.name, extra_hosts={'controlled.test': address}, dns=['127.0.0.1'],
                                             volumes={str(proxy_files): {'bind': '/proxy', 'mode': 'ro'}})
                resources.append(proxy)
                proxy.reload()
                proxies.append((proxy, proxy.attrs['NetworkSettings']['Networks'][runtime.egress.name]['IPAddress']))
            project = api.machine.get_project(api.project_id).project
            expected_peers = {}
            for mode, task_run, (_, proxy_ip) in zip(('http', 'socks5'), api.task_runs, proxies):
                public = configure(mode, proxy_ip, **auth)
                bridge.prepare_task(project, task_run['id'], TaskCancellation(generation=task_run['execution_generation']))
                scope = bridge.task(project.id, task_run['id']).scope
                scopes.append(scope)
                assert runtime.manifest['scopes'][scope.id]['test_egress'] == public
                start = len(paths)
                for scheme in ('http', 'https'):
                    curl(scope, f'/{mode}-{scheme}', scheme=scheme)
                # 两种工具显式入口与选定上游协议独立，认证仍只在私有出口消费。
                for incoming in ('http', 'socks5h'):
                    for scheme in ('http', 'https'):
                        curl(scope, f'/{mode}-{incoming}-{scheme}-explicit', scheme=scheme,
                             host='controlled.test' if scheme == 'http' else address,
                             flags=f'--noproxy "" --proxy {incoming}://{proxy_ip}:9567')
                curl(scope, f'/{mode}-dns', host='controlled.test')
                expected_peers.update({path: proxy_ip for path in paths[start:]})
            # 同一项目已有 runtime：修改设置只影响新 scope，不改变旧 scope 的出口。
            curl(scopes[0], '/http-after-update')
            expected_peers[paths[-1]] = proxies[0][1]
            command = "import urllib.request; print(urllib.request.urlopen('http://controlled.test:8989/python').read().decode())"
            run(scopes[1], 'python3 -c ' + shlex.quote(command))
            paths.append('/python')
            expected_peers['/python'] = proxies[1][1]
            for scope, (_, proxy_ip) in zip(scopes, proxies):
                command = f"import socket; s=socket.create_connection(('{address}',9777),5); s.sendall(b'\\x01egress-proof'); assert s.recv(128)==b'\\x01egress-proof'; s.close()"
                run(scope, 'python3 -c ' + shlex.quote(command))
                command = f"import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(1); s.connect(('{address}',9778)); s.send(b'blocked'); s.recv(128)"
                code, _ = execute(scope, 'python3 -c ' + shlex.quote(command))
                assert code != 0 and not (site / 'udp-received').exists()
                # 未配置的代理握手不能被当成目标 HTTP，更不能保存逐跳认证。
                code, _ = execute(scope, f'curl -fsS --max-time 3 --noproxy "" -x http://{address}:8989 http://controlled.test:8989/conflicting-proxy')
                assert code != 0
            fragmented = f'''import socket, time
for method, target in [('GET', 'http://controlled.test:8989/fragmented-proxy'), ('CONNECT', 'controlled.test:8989')]:
    with socket.create_connection(('{address}', 8989), 5) as connection:
        connection.sendall((method + ' ').encode())
        time.sleep(.1)
        connection.sendall((target + ' HTTP/1.1\\r\\nHost: controlled.test\\r\\nProxy-Authorization: Basic c3ludGhldGljLW1hbnVhbC1hdXRo\\r\\n\\r\\n').encode())
        reply = connection.recv(4096).split(b'\\r\\n', 1)[0]
        assert b'502' in reply, (method, reply)
'''
            run(scopes[1], 'python3 -c ' + shlex.quote(fragmented))
            assert (site / 'tcp-requests').read_text().splitlines() == [proxy[1] for proxy in proxies]
            run(scopes[1], f"playwright-cli open 'https://{address}:9443/browser'")
            assert 'traffic-synthetic' in run(scopes[1], "playwright-cli eval 'document.title'")
            assert 'traffic-synthetic' in run(scopes[1], "playwright-cli eval 'async () => await (await fetch(\"/browser-fetch\")).text()'")
            run(scopes[1], 'playwright-cli close')
            browser_observed = (site / 'requests').read_text().splitlines()[len(paths):]
            browser_paths = [line.split(' ', 1)[1] for line in browser_observed]
            assert '/browser' in browser_paths and '/browser-fetch' in browser_paths
            assert all(path in ('/browser', '/browser-fetch', '/favicon.ico') for path in browser_paths)
            paths.extend(browser_paths)
            expected_peers.update({path: proxies[1][1] for path in browser_paths})
            observed = (site / 'requests').read_text().splitlines()
            assert sorted(observed) == sorted(f'{expected_peers[path]} {path}' for path in paths)
            def target_flows():
                return [row for row in flows() if base64.b64decode(row['request']['target_base64']).decode() in paths]
            wait_for(lambda: len(target_flows()) == len(paths) and all(row['state'] == 'complete' for row in target_flows()))
            records = target_flows()
            # Chromium 自身后台请求不属于目标样本，隔离拓扑中不得有成功公网连接。
            assert all(row['state'] == 'failed' for row in flows() if row['id'] not in {item['id'] for item in records})
            assert all(row['request']['method'] == 'GET' and row['response']['status_code'] == 200 for row in records)
            assert len({row['id'] for row in records}) == len(paths)
            for row in records:
                view = get(f"/traffic/{row['id']}")
                assert view['scope']['test_egress'] == runtime.manifest['scopes'][view['scope']['id']]['test_egress']
                wait_for(lambda: get(f"/traffic/{row['id']}/bodies/response?preview=true")['sealed'])
                response = api.user.get(prefix + f"/traffic/{row['id']}/bodies/response", timeout=10)
                assert response.status_code == 200 and b'traffic-synthetic' in response.content
            secrets = [value.encode() for value in auth.values()]
            secrets.extend([base64.b64encode((auth['username'] + ':' + auth['password']).encode()),
                            b'c3ludGhldGljLW1hbnVhbC1hdXRo', b'synthetic-manual-auth'])
            def assert_secrets_private():
                public_bytes = json.dumps([get(''), flows(), api.task_runs, runtime.manifest]).encode()
                for resource in runtime.containers:
                    public_bytes += json.dumps(resource.attrs).encode() + resource.logs()
                for path in runtime.root.rglob('*'):
                    if path.is_file() and path.suffix != '.sock' and path.name != 'private.json':
                        public_bytes += path.read_bytes()
                assert not any(secret in public_bytes for secret in secrets)
            assert_secrets_private()
            # 代理拒绝认证时也失败闭合；随后再验证连接不可用，不改 scope 私有快照。
            (proxy_files / 'auth.json').write_text(json.dumps({**auth, 'password': 'different-controlled-secret'}))
            for scope, (proxy, _) in zip(scopes, proxies):
                proxy.restart(timeout=1)
                wait_for(lambda: proxy.exec_run(['python', '-c', "from pathlib import Path; assert any(row.split()[1].endswith(':255F') and row.split()[3]=='0A' for row in Path('/proc/net/tcp').read_text().splitlines()[1:])"]).exit_code == 0)
                code, _ = execute(scope, f'curl -fsS --max-time 3 --noproxy "*" http://{address}:8989/bad-auth')
                assert code != 0
            assert (site / 'requests').read_text().splitlines() == observed
            # 两个出口各自失败闭合，不能偷偷沿原有 project gateway 直连。
            for scope, (proxy, _) in zip(scopes, proxies):
                proxy.stop(timeout=1)
                code, _ = execute(scope, f'curl -fsS --max-time 15 --noproxy "*" http://{address}:8989/no-fallback')
                assert code != 0
            assert (site / 'requests').read_text().splitlines() == observed
            wait_for(lambda: {(row['error'] or {}).get('code') for row in flows() if base64.b64decode(row['request']['target_base64']) == b'/bad-auth'} == {'traffic_proxy_auth_failed', 'traffic_proxy_handshake_failed'})
            wait_for(lambda: sum(base64.b64decode(row['request']['target_base64']) == b'/no-fallback' and (row['error'] or {}).get('code') == 'traffic_proxy_connect_failed' for row in flows()) == 2)
            response = api.user.put(prefix + '/test-egress', json={'mode': 'direct'}, timeout=10)
            assert response.status_code == 200
            intent = api.user.post(prefix + '/intents', json={'from': ['origin'], 'description': '合成直连回归'}, timeout=10)
            assert intent.status_code == 201
            admitted = api.machine.start_explore_attempt(project.id, intent.json()['id'], 'traffic-source-direct',
                worker_type='pi', prompt_group='goal-driven-v1', expected_execution_generation=project.execution_generation)
            assert admitted.status_code == 201
            task_run = admitted.data
            bridge.prepare_task(project, task_run['id'], TaskCancellation(generation=task_run['execution_generation']))
            direct = bridge.task(project.id, task_run['id']).scope
            for scheme in ('http', 'https'):
                curl(direct, f'/direct-{scheme}', scheme=scheme)
            wait_for(lambda: sum(base64.b64decode(row['request']['target_base64']).startswith(b'/direct-') and row['state'] == 'complete' for row in flows()) == 2)
            assert all(line.split()[0] not in {proxy[1] for proxy in proxies} for line in (site / 'requests').read_text().splitlines()[-2:])
            assert_secrets_private()
            for scope in scopes:
                assert runtime.stop_scope(scope.id)['termination_confirmed']
                assert not (runtime.root / 'egress' / scope.id / 'private.json').exists()
        finally:
            for resource in reversed(resources):
                resource.stop(timeout=1)
                resource.remove()
            bridge.close()


@pytest.mark.skipif(
    os.environ.get('CAIRN_TRAFFIC_INTEGRATION') != '1' or os.environ.get('CAIRN_TEST_POSTGRES_BOOTSTRAP') != '1',
    reason='需显式启用隔离 Docker 与私有 PostgreSQL 实验',
)
def test_loopback_capture_keeps_namespace_and_selected_egress(traffic_api):
    api = traffic_api
    client = docker.from_env()
    image = 'arena-traffic-gateway:local'
    with tempfile.TemporaryDirectory(prefix='loopback-') as temporary:
        root = Path(temporary)
        site = root / 'site'
        site.mkdir()
        ca = certificate(site)
        (site / 'server.py').write_text(SITE.replace('    def do_GET(self):',
            "    def do_GET(self):\n        with (site / 'requests').open('a') as out: out.write(self.client_address[0] + ' ' + self.path + '\\n')"))
        auth = {'username': 'synthetic-egress-user', 'password': 'synthetic-egress-secret'}
        proxy_files = root / 'proxy'
        proxy_files.mkdir(mode=0o700)
        (proxy_files / 'auth.json').write_text(json.dumps(auth))
        bridge = TrafficExecutionBridge(
            client, api.machine, root=root / 'runtime',
            dispatcher_id=api.task_runs[0]['runtime_context']['dispatcher_id'],
            config=SimpleNamespace(traffic_gateway_image=image, task_executor_image='arena-task-executor:local'),
            isolated=True, upstream_ca=site / 'ca.pem',
        )
        runtime = bridge._runtime(api.project_id)
        resources, scopes, proxies = [], [], []
        expected, expected_peers = {}, []
        prefix = f'{api.base_url}/projects/{api.project_id}'

        def get(path):
            response = api.user.get(prefix + path, timeout=10)
            assert response.status_code == 200
            return response.json()

        def run(scope, command):
            code, text = execute(scope, command)
            assert code == 0, text
            return text

        def request(scope, host, port, path, *, scheme='http', flags='--noproxy "*"', local=False):
            body = f'shared-input:{scope.id}' if local else '<html><title>traffic-synthetic</title><body>synthetic</body></html>'
            url = shlex.quote(f'{scheme}://{host}:{port}{path}')
            assert run(scope, f'curl -fsS --max-time 8 {flags} {url}') == body
            expected[scope.id, path] = (scope, host, port, scheme, body.encode())

        try:
            runtime.prepare(upstream_ca=site / 'ca.pem')
            target = client.containers.run(image,
                ['sh', '-c', 'while ! test -f /site/server.key; do sleep .1; done; exec python /site/server.py'],
                entrypoint=[], detach=True, network=runtime.egress.name,
                volumes={str(site): {'bind': '/site', 'mode': 'rw'}})
            resources.append(target)
            target.reload()
            address = target.attrs['NetworkSettings']['Networks'][runtime.egress.name]['IPAddress']
            certificate(site, issuer=ca, address=address)
            gateway_peer = runtime.gateway.attrs['NetworkSettings']['Networks'][runtime.egress.name]['IPAddress']
            (runtime.workspace / 'control-file').write_text('shared-input')
            project = api.machine.get_project(api.project_id).project
            task_runs = list(api.task_runs)
            intent = api.user.post(prefix + '/intents', json={'from': ['origin'], 'description': '合成回环采集源'}, timeout=10)
            assert intent.status_code == 201
            admitted = api.machine.start_explore_attempt(project.id, intent.json()['id'], 'loopback-source',
                worker_type='pi', prompt_group='goal-driven-v1', expected_execution_generation=project.execution_generation)
            assert admitted.status_code == 201
            task_runs.append(admitted.data)
            for mode, task_run in zip(('direct', 'http', 'socks5'), task_runs):
                config = {'mode': mode}
                peer = gateway_peer
                if mode != 'direct':
                    proxy = client.containers.run(image, ['python', '-c', PROXY], entrypoint=[], detach=True,
                        network=runtime.egress.name, extra_hosts={'controlled.test': address}, dns=['127.0.0.1'],
                        volumes={str(proxy_files): {'bind': '/proxy', 'mode': 'ro'}})
                    resources.append(proxy)
                    proxy.reload()
                    peer = proxy.attrs['NetworkSettings']['Networks'][runtime.egress.name]['IPAddress']
                    config.update(host=peer, port=9567, auth_action='replace', **auth)
                response = api.user.put(prefix + '/test-egress', json=config, timeout=10)
                assert response.status_code == 200
                bridge.prepare_task(project, task_run['id'], TaskCancellation(generation=task_run['execution_generation']))
                scope = bridge.task(project.id, task_run['id']).scope
                scopes.append(scope)
                if mode != 'direct':
                    proxies.append((proxy, scope))
                local_site = runtime.workspace / scope.id
                local_site.mkdir()
                os.chown(local_site, 1000, 1000)
                certificate(local_site, issuer=ca, address=['127.0.0.1', '127.0.0.2'])
                (local_site / 'server.py').write_text(SITE)
                command = ("import subprocess; subprocess.Popen(['python3', "
                    f"'{WORKSPACE}/{scope.id}/server.py', '{WORKSPACE}/{scope.id}', '{scope.id}'], "
                    "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)")
                run(scope, 'python3 -c ' + shlex.quote(command))
                wait_for(lambda: (local_site / 'ready').exists())
                # 同一回环地址、端口和路径必须返回各自 namespace 的正文，且只采一次。
                for host in ('127.0.0.1', '127.0.0.2'):
                    for scheme, port in (('http', 8990), ('https', 9444)):
                        request(scope, host, port, f'/control-file?host={host}&scheme={scheme}', scheme=scheme, local=True)
                for scheme, port in (('http', 8989), ('https', 9443)):
                    path = f'/{mode}-{scheme}'
                    request(scope, address, port, path, scheme=scheme)
                    expected_peers.append(f'{peer} {path}')
                # 实际 regular / SOCKS listener 保持可用；它们不能再次进入透明采集。
                for protocol, port in (('http', 8081), ('socks5h', 1080)):
                    path = f'/{mode}-listener-{port}'
                    request(scope, address, 8989, path,
                        flags=f'--noproxy "" --proxy {protocol}://127.0.0.1:{port}')
                    expected_peers.append(f'{peer} {path}')
                if mode != 'direct':
                    path = f'/{mode}-dns'
                    request(scope, 'controlled.test', 8989, path)
                    expected_peers.append(f'{peer} {path}')
                    dns_tcp = r'''import socket, struct
query = b'\x00\x01\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x0acontrolled\x04test\x00\x00\x01\x00\x01'
with socket.create_connection(('127.0.0.1', 5353), 3) as connection:
    connection.sendall(struct.pack('!H', len(query)) + query)
    reply = connection.recv(512)
    assert len(reply) > 14 and reply[4] & 0x80
'''
                    run(scope, 'python3 -c ' + shlex.quote(dns_tcp))
                    # 传输桥只接收 collector socket；工具不能直连透明采集入口。
                    for port in (8080, 9091):
                        code, _ = execute(scope, f'curl -fsS --max-time 2 --noproxy "*" http://127.0.0.1:{port}/reserved')
                        assert code != 0
            assert sorted((site / 'requests').read_text().splitlines()) == sorted(expected_peers)
            for proxy, scope in proxies:
                proxy.stop(timeout=1)
                code, _ = execute(scope, f'curl -fsS --max-time 12 --noproxy "*" http://{address}:8989/no-fallback')
                assert code != 0
                # 本地语义不依赖上游健康，不是外部失败后的 DIRECT fallback。
                request(scope, '127.0.0.1', 8990, '/control-file?after-proxy-stop', local=True)
            assert sorted((site / 'requests').read_text().splitlines()) == sorted(expected_peers)

            def captured():
                rows = get('/traffic?limit=200')['items']
                return rows if len(rows) == len(expected) + len(proxies) and all(row['state'] != 'receiving' for row in rows) else None

            rows = wait_for(captured)
            grouped = {}
            for row in rows:
                key = (row['scope']['id'], base64.b64decode(row['request']['target_base64']).decode())
                grouped.setdefault(key, []).append(row)
            assert set(grouped) == set(expected) | {(scope.id, '/no-fallback') for _, scope in proxies}
            assert all(len(items) == 1 for items in grouped.values())
            for key, (scope, host, port, scheme, body) in expected.items():
                row = grouped[key][0]
                assert row['state'] == 'complete' and row['response']['status_code'] == 200
                assert (row['request']['host'], row['request']['port'], row['request']['scheme']) == (host, port, scheme)
                view = get(f"/traffic/{row['id']}")
                assert view['scope'] == {'id': scope.id, 'project_id': scope.project_id,
                    'task_run_id': scope.task_run_id, 'generation': scope.generation, 'container_id': scope.container_id,
                    'test_egress': runtime.manifest['scopes'][scope.id]['test_egress']}
                if host.startswith('127.'):
                    assert view['original_destination'] == [host, port]
                wait_for(lambda: get(f"/traffic/{row['id']}/bodies/response?preview=true")['sealed'])
                response = api.user.get(prefix + f"/traffic/{row['id']}/bodies/response", timeout=10)
                assert response.status_code == 200 and response.content == body
            for _, scope in proxies:
                row = grouped[scope.id, '/no-fallback'][0]
                assert row['state'] == 'failed' and row['error']['code'] == 'traffic_proxy_connect_failed'
        finally:
            for resource in reversed(resources):
                resource.stop(timeout=1)
                resource.remove()
            bridge.close()
