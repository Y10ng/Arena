"""S1-B opt-in：真实受理、生产准备和 Pi runner，模型仅返回固定工具调用。"""

from concurrent.futures import ThreadPoolExecutor
import base64
import json
import os
from pathlib import Path
import shlex
import tempfile
import threading
import time

import docker
import pytest

from traffic_api_fixture import traffic_api, postgres_instance, empty_postgres_url  # noqa: F401
from test_traffic_execution_integration import certificate
from cairn.dispatcher.config import ContainerConfig, WorkerConfig, PiModelConfig
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.container_options import ContainerRuntimeOptions
from cairn.dispatcher.runtime.containers import ContainerManager, container_instance_id
from cairn.dispatcher.runtime.traffic_execution import TrafficExecutionBridge
from cairn.dispatcher.tasks.candidate_capture_outbox import CandidateCaptureOutbox
from cairn.dispatcher.tasks.process_runner import run_worker_process
from cairn.dispatcher.workers.adapters.pi import PiDriver
from cairn.dispatcher.protocol.client import ApiResult


SITE = r'''
import http.server, ssl, threading, time, socket, os
from pathlib import Path
root = Path('/site')
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        self.reply(body[::-1])
    def reply(self, body):
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        with (root / 'requests').open('ab', buffering=0) as output:
            output.write(self.path.encode() + b'\n')
        if self.path.startswith('/stream'):
            self.send_response(200)
            self.send_header('Content-Length', '1000000')
            self.end_headers()
            self.wfile.write(b'prefix\x00\xff' * 1024)
            self.wfile.flush()
            while not (root / 'append').exists(): time.sleep(.05)
            self.wfile.write(b'next\x00\xff' * 1024)
            self.wfile.flush()
            while not (root / 'release').exists(): time.sleep(.05)
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        self.reply(b'<html><title>formal-traffic</title><body>synthetic</body></html>')
a = http.server.ThreadingHTTPServer(('0.0.0.0', 8989), Handler)
b = http.server.ThreadingHTTPServer(('0.0.0.0', 9443), Handler)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(root / 'server.pem', root / 'server.key')
b.socket = ctx.wrap_socket(b.socket, server_side=True)
threading.Thread(target=a.serve_forever, daemon=True).start()
b.serve_forever()
'''

MODEL = r'''
import http.server, json, threading, time
from pathlib import Path
root = Path('/tmp/cairn-prompts')
plans = json.loads((root / 'plans.json').read_text())
counts = {}
lock = threading.Lock()
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        name = body['model']
        with lock:
            number = counts.get(name, 0)
            counts[name] = number + 1
        (root / f'{name}-{number}.json').write_text(json.dumps(body))
        plan = plans[name]
        action = plan[min(number, len(plan)-1)]
        if 'wait' in action:
            while not (root / action['wait']).exists(): time.sleep(.05)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        def chunk(delta, finish=None):
            value = {'id':f'chat_{name}_{number}', 'object':'chat.completion.chunk', 'created':1,
                'model':name, 'choices':[{'index':0,'delta':delta,'finish_reason':finish}]}
            self.wfile.write(('data: '+json.dumps(value)+'\n\n').encode())
        chunk({'role':'assistant'})
        for index, tool in enumerate(action.get('tools', [])):
            chunk({'tool_calls':[{'index':index,'id':f'call_{number}_{index}','type':'function',
                'function':{'name':tool['name'],'arguments':json.dumps(tool['args'])}}]})
        chunk({}, 'tool_calls' if action.get('tools') else 'stop')
        self.wfile.write(b'data: [DONE]\n\n')
        self.wfile.flush()
server = http.server.ThreadingHTTPServer(('127.0.0.1', 8765), Handler)
(root / 'model-ready').touch()
server.serve_forever()
'''


def wait_for(check, *, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        assert time.monotonic() < deadline, 'controlled condition did not become true'
        time.sleep(.1)


def tool(name, **args):
    return {'tools': [{'name': name, 'args': args}]}


@pytest.mark.skipif(
    os.environ.get('CAIRN_TRAFFIC_INTEGRATION') != '1' or os.environ.get('CAIRN_TEST_POSTGRES_BOOTSTRAP') != '1',
    reason='需明确启用隔离 Docker 与私有 PostgreSQL 实验',
)
def test_formal_runner_streaming_cancellation_and_recovery(monkeypatch, traffic_api):
    api = traffic_api
    dispatcher_id = api.task_runs[0]['runtime_context']['dispatcher_id']
    owner = container_instance_id(dispatcher_id, api.base_url)
    config = ContainerConfig(image='arena-worker:s1-b', network_mode='none', completed_action='remove')
    manager = ContainerManager(config, instance_id=owner)
    docker_client = docker.from_env(timeout=5)
    target = control = None
    cancellations = []
    futures = []
    pool = ThreadPoolExecutor(max_workers=2)
    with tempfile.TemporaryDirectory(prefix='s1b-') as temporary:
        root = Path(temporary)
        site = root / 'site'
        site.mkdir()
        ca = certificate(site)
        (site / 'server.py').write_text(SITE)
        manager.configure_traffic_execution(api.machine, root=root / 'runtime', dispatcher_id=dispatcher_id,
                                            isolated=True, upstream_ca=site / 'ca.pem')
        bridge = manager.traffic_execution
        bridge.recover()
        project = api.machine.get_project(api.project_id).project
        assert project.scenario_snapshot['features']['http_traffic_execution_version'] == 1
        options = ContainerRuntimeOptions.build(image=config.image, network_mode='none', dns=[], cap_add=[],
                    features=project.scenario_snapshot['features'], scenario_key='goal-driven-v1')
        lost = threading.Event()
        upload = api.machine.sync_traffic_body
        def lose_receipt(*args, **kwargs):
            result = upload(*args, **kwargs)
            if result.ok and not lost.is_set():
                lost.set()
                return ApiResult(status_code=0, error_code='injected_receipt_loss')
            return result
        monkeypatch.setattr(api.machine, 'sync_traffic_body', lose_receipt)
        prefix = f'{api.base_url}/projects/{api.project_id}/traffic'
        def get(path='', **params):
            response = api.user.get(prefix + path, params=params, timeout=10)
            assert response.status_code == 200, response.text
            return response.json()
        def flows():
            return get(limit=200)['items']
        def find(path):
            return next((item for item in flows() if base64.b64decode(item['request']['target_base64']).decode() == path), None)
        def preview(flow):
            return get(f"/{flow['id']}/bodies/response", preview='true')
        try:
            name = manager.ensure_running(project.id, options)
            control = docker_client.containers.get(name)
            runtime = bridge._runtimes[project.id]
            target = docker_client.containers.create('arena-traffic-gateway-dependencies:s0',
                ['sh', '-c', 'while ! test -f /site/server.key; do sleep .1; done; exec python /site/server.py'],
                network=runtime.egress.name, labels={'io.cairn.s1b.fixture': owner},
                volumes={str(site): {'bind': '/site', 'mode': 'rw'}})
            target.start()
            target.reload()
            address = target.attrs['NetworkSettings']['Networks'][runtime.egress.name]['IPAddress']
            certificate(site, issuer=ca, address=address)
            http = f'http://{address}:8989'
            https = f'https://{address}:9443'
            binary = bytes(range(256)) * 9
            commands = [
                "test -z \"$S1B_CONTROL_SECRET\" && test ! -e /home/kali/.cairn/pi && test ! -e /tmp/cairn-prompts/plans.json && "
                + f"printf '%s' {shlex.quote(base64.b64encode(binary).decode())} | base64 -d > binary.dat && "
                + f"curl -fsS --data-binary @binary.dat '{https}/binary' > reversed.dat && printf 'shared-proof' > shared.txt && "
                + "(setsid python3 -m http.server 8990 --bind 0.0.0.0 </dev/null >/dev/null 2>&1 &) && sleep .3 && "
                + "curl -fsS http://127.0.0.1:8990/shared.txt && curl -fsS http://$(hostname -i | tr -d ' '):8990/shared.txt",
                f"playwright-cli open '{https}/browser'",
                "playwright-cli eval '() => { setInterval(() => fetch(\"/browser-background\"), 400); return document.title; }'",
                f"setsid sh -c 'while :; do curl -fsS {http}/background-one >/dev/null; sleep .3; done' </dev/null >/dev/null 2>&1 &",
            ]
            plans = {
                'one': [tool('bash', command=commands[0]), tool('capture_http_evidence', url=http+'/helper-http'),
                        tool('capture_http_evidence', url=https+'/helper-https'), tool('read', path='shared.txt'),
                        tool('register_artifact', path='shared.txt', kind='file'),
                        *[tool('bash', command=command) for command in commands[1:]],
                        {'wait':'never-one'}],
                'two': [tool('bash', command=f"setsid curl -sS '{http}/stream' -o stream.dat </dev/null >/dev/null 2>&1 &"),
                        {'wait':'continue-two', **tool('bash', command=f"curl -fsS '{http}/still-running'")},
                        {'wait':'never-two'}],
            }
            for filename, content in [('plans.json', json.dumps(plans)), ('model.py', MODEL)]:
                path = runtime.control_prompts / filename
                path.write_text(content)
                os.chown(path, 1000, 1000)
            control.exec_run(['python3', '/tmp/cairn-prompts/model.py'], detach=True)
            wait_for(lambda: (runtime.control_prompts / 'model-ready').exists())
            outbox = CandidateCaptureOutbox(root / 'outbox', owner_id=owner)
            scopes = []
            for index, run in enumerate(api.task_runs):
                cancellation = TaskCancellation(generation=run['execution_generation'])
                cancellations.append(cancellation)
                capability = manager.prepare_task_execution(project, run['id'], cancellation, options)
                scopes.append(bridge.task(project.id, run['id']).scope)
                model_name = ('one', 'two')[index]
                worker = WorkerConfig(id=f'traffic-source-{index+1}', engine='pi', task_types=['explore'], concurrency=2, priority=0,
                    env={'S1B_CONTROL_SECRET':'synthetic-private-control-value'},
                    model_binding=PiModelConfig(protocol='openai-chat-completions', model_name=model_name,
                        base_url='http://127.0.0.1:8765', inference_endpoint='http://127.0.0.1:8765/chat/completions',
                        auth_header='authorization-bearer', api_key='synthetic-model-secret'))
                invocation = PiDriver().build_execute(worker, '仅执行本地合成验证；没有漏洞发现结论。', None,
                    phase='explore_execute', project_id=project.id, task_run_id=run['id'], timeout_seconds=240,
                    task_execution=capability, session_root='/home/kali/.cairn/pi')
                futures.append(pool.submit(run_worker_process, manager, name, worker, invocation,
                    phase='explore_execute', timeout_seconds=240, cancellation=cancellation, client=api.machine,
                    project_id=project.id, task_run_id=run['id'], candidate_outbox=outbox))
            def reached_tools():
                for future in futures:
                    if future.done():
                        raise AssertionError(f'runner ended before tools: {future.result()}')
                return (runtime.control_prompts / 'one-8.json').exists()
            wait_for(reached_tools, timeout=90)
            wait_for(lambda: (runtime.control_prompts / 'two-1.json').exists())
            assert not any(future.done() for future in futures)
            history = json.loads((runtime.control_prompts / 'one-8.json').read_text())['messages']
            results = [str(item.get('content')) for item in history if item['role'] == 'tool']
            assert any('shared-proof' in value for value in results), results
            assert any('formal-traffic' in value for value in results), results
            with manager.materialize_file(name, '/home/kali/workspace/reversed.dat') as materialized:
                assert materialized.path.read_bytes() == binary[::-1]
            complete = wait_for(lambda: find('/helper-https'))
            wait_for(lambda: preview(complete)['sealed'])
            assert get(f"/{complete['id']}")['scope']['id'] == scopes[0].id
            stream = wait_for(lambda: find('/stream'))
            wait_for(lambda: preview(stream)['accepted_bytes'] == 8 * 1024)
            first = preview(stream)
            assert base64.b64decode(first['content_base64']) == b'prefix\x00\xff' * 1024
            assert not first['sealed'] and not first['source_sealed']
            (site / 'append').touch()
            wait_for(lambda: preview(stream)['accepted_bytes'] == 14 * 1024)
            assert base64.b64decode(preview(stream)['content_base64']) == b'prefix\x00\xff' * 1024 + b'next\x00\xff' * 1024
            assert lost.is_set()
            cancellations[0].cancel('controlled_cancel')
            result = futures[0].result(timeout=30)
            assert result.cancelled
            assert runtime.manifest['scopes'][scopes[0].id]['termination_confirmed']
            with pytest.raises(docker.errors.NotFound):
                docker_client.containers.get(scopes[0].container_id)
            before = [(site / 'requests').read_bytes().count(path) for path in (b'/background-one\n', b'/browser-background\n')]
            assert all(count > 0 for count in before)
            time.sleep(1)
            assert [(site / 'requests').read_bytes().count(path) for path in (b'/background-one\n', b'/browser-background\n')] == before
            (runtime.control_prompts / 'continue-two').touch()
            wait_for(lambda: find('/still-running'))
            assert not futures[1].done()
            assert not bridge._syncs[project.id].stop(timeout=5)['running']
            collector_entry = runtime.manifest['resources'][f'collector:{scopes[1].id}']
            docker_client.containers.get(collector_entry['id']).kill()
            futures[1].result(timeout=30)
            assert cancellations[1].is_cancelled
            assert runtime.manifest['scopes'][scopes[1].id]['termination_confirmed']
            assert not preview(stream)['sealed']
            snapshot = {flow['id']: flow['sync']['bodies'] for flow in flows() if flow['state'] == 'complete'}
            request_count = (site / 'requests').read_bytes().count(b'/stream\n')
            # 停止隔离实例的后台循环，再用同一持久目录运行生产恢复入口；不重新准备执行。
            bridge._stop.set()
            for thread in bridge._threads:
                thread.join(timeout=5)
            for sync in bridge._syncs.values():
                assert not sync.stop(timeout=5)['running']
            recovered = TrafficExecutionBridge(manager._client, api.machine, root=root/'runtime',
                dispatcher_id=dispatcher_id, config=config, isolated=True, upstream_ca=site/'ca.pem')
            manager.traffic_execution = bridge = recovered
            bridge.recover()
            restored = bridge._runtimes[project.id]
            assert restored.isolated and len(restored.manifest['scopes']) == 2
            wait_for(lambda: preview(stream)['sealed'])
            recovered_flow = get(f"/{stream['id']}")
            assert recovered_flow['state'] == 'partial' and recovered_flow['recovery']['source'] == 'runtime'
            assert api.user.get(prefix+f"/{stream['id']}/bodies/response", timeout=10).content == b'prefix\x00\xff'*1024+b'next\x00\xff'*1024
            assert api.outsider.get(prefix+f"/{stream['id']}/bodies/response", timeout=10).status_code == 404
            wait_for(lambda: all((item['status'] or {}).get('upload_complete') is True for item in get('/scopes/status')['items']))
            assert (site / 'requests').read_bytes().count(b'/stream\n') == request_count == 1
            assert {flow['id']: flow['sync']['bodies'] for flow in flows() if flow['id'] in snapshot} == snapshot
            targets = [base64.b64decode(flow['request']['target_base64']) for flow in flows()]
            assert not any(b'chat/completions' in value for value in targets)
            assert targets.count(b'/shared.txt') == 2
            assert {flow['request']['host'] for flow in flows() if base64.b64decode(flow['request']['target_base64']) == b'/shared.txt'} == {'127.0.0.1', scopes[0].address}
            assert set(flow['scope']['id'] for flow in flows()) == {scope.id for scope in scopes}
            print(json.dumps({'formal_task_runs':2, 'flows':len(snapshot), 'prefix_bytes':14*1024,
                              'lost_receipt':lost.is_set(), 'cancelled_background':True, 'recovery_no_replay':True}))
        finally:
            (site/'append').touch()
            (site/'release').touch()
            for cancellation in cancellations:
                cancellation.cancel('fixture_cleanup')
            for future in futures:
                try:
                    future.result(timeout=30)
                except Exception:
                    pass
            pool.shutdown(wait=True)
            if target is not None:
                target.remove(force=True)
            if control is not None:
                manager.remove_container(control.name)
            manager.close()
            docker_client.close()
