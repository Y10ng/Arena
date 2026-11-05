"""第27节定向实验：真实会话桥、测试后端、采集与精确回收；provider 只给固定调用。"""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import base64
import json
import os
from pathlib import Path
import shlex
import threading
from types import SimpleNamespace
from uuid import uuid4

import docker
from docker.errors import APIError, NotFound
import psycopg
import pytest

from traffic_api_fixture import traffic_api, postgres_instance, empty_postgres_url  # noqa: F401
from test_traffic_formal_execution import wait_for, tool
from cairn.dispatcher.config import ContainerConfig, WorkerConfig
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.chat_workspace import ChatWorkspace
from cairn.dispatcher.runtime.container_options import ContainerRuntimeOptions
from cairn.dispatcher.runtime.containers import ContainerManager, container_instance_id
from cairn.dispatcher.runtime.conversation_cleanup import cleanup_conversation
from cairn.dispatcher.runtime.process import ManagedProcess
from cairn.dispatcher.scheduler.worker_runtime import _pi_model_binding
from cairn.dispatcher.tasks.agent_chat import run_agent_turn
from cairn.server import db
from cairn.server.models import CreateVulnerabilityRequest, UpdateVulnerabilityRequest
from cairn.server.schemas.models import TenantModelExecutionBinding
from cairn.server.services.vulnerabilities import create_vulnerability, update_vulnerability


SITE = r'''
import http.server
from pathlib import Path
root = Path('/site')
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        self.reply(body[::-1])
    def do_GET(self):
        with (root / 'requests').open('ab', buffering=0) as output:
            output.write(self.path.encode() + b'\n')
        self.reply(b'controlled-next-response')
    def reply(self, body):
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
server = http.server.ThreadingHTTPServer(('0.0.0.0', 8989), Handler)
(root / 'ready').touch()
server.serve_forever()
'''

MODEL = r'''
import http.server, json, threading, time
from pathlib import Path
root = Path('/tmp/cairn-prompts')
plans = json.loads((root / 'plans.json').read_text())
count = 0
flow_id = None
lock = threading.Lock()
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        global count, flow_id
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        with lock:
            number = count
            count += 1
        (root / f'call-{number}.json').write_text(json.dumps(body))
        action = plans[number]
        if 'wait' in action:
            while not (root / action['wait']).exists(): time.sleep(.05)
        if action.get('select'):
            result = json.loads(next(m['content'] for m in reversed(body['messages']) if m['role'] == 'tool'))
            selection = action['select']
            if selection == 'request':
                flow_id = next(item['id'] for item in result['items'] if item['method'] == 'POST')
            if selection in ('request', 'response'):
                action = {'tools':[{'name':'read_conversation_research', 'args':{'flow_id':flow_id, 'side':selection}}]}
            else:
                action = {'tools':[{'name':'publish_custom_markdown', 'args':{
                    'title':'受控会话材料', 'content':'# 本地合成实验\n已读取真实采集响应，不代表漏洞确认。',
                    'source_references':[result['source_reference']]}}]}
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        def chunk(delta, finish=None):
            value = {'id':f'controlled-{number}', 'object':'chat.completion.chunk', 'created':1,
                'model':body['model'], 'choices':[{'index':0,'delta':delta,'finish_reason':finish}]}
            self.wfile.write(('data: '+json.dumps(value)+'\n\n').encode())
        chunk({'role':'assistant'})
        for index, item in enumerate(action.get('tools', [])):
            chunk({'tool_calls':[{'index':index,'id':f'call_{number}_{index}','type':'function',
                'function':{'name':item['name'],'arguments':json.dumps(item['args'])}}]})
        if 'text' in action: chunk({'content':action['text']})
        chunk({}, 'tool_calls' if action.get('tools') else 'stop')
        self.wfile.write(b'data: [DONE]\n\n')
        self.wfile.flush()
server = http.server.ThreadingHTTPServer(('127.0.0.1', 8765), Handler)
(root / 'model-ready').touch()
server.serve_forever()
'''


@pytest.mark.skipif(
    os.environ.get('CAIRN_TRAFFIC_INTEGRATION') != '1' or os.environ.get('CAIRN_TEST_POSTGRES_BOOTSTRAP') != '1',
    reason='仅显式启用本次隔离 Docker、HTTP 与 PostgreSQL 实验',
)
def test_conversation_research_stop_and_persistent_exact_cleanup(traffic_api, tmp_path, monkeypatch):
    api = traffic_api
    # 只改变批次预算，让真实小正文经过分块与 seal API，覆盖会话 owner 回执。
    monkeypatch.setattr('cairn.dispatcher.runtime.traffic_sync.TRAFFIC_BATCH_BODY_BYTES', 0)

    def request(method, path, expected=200, **kwargs):
        response = api.user.request(method, api.base_url + path, timeout=15, **kwargs)
        assert response.status_code == expected, (method, path, response.status_code, response.text)
        return response.json() if response.content else None

    model = request('POST', '/models', 201, json={
        'display_name': '会话确定性工具验证', 'protocol': 'openai-chat-completions',
        'base_url': 'http://127.0.0.1:8765', 'model_name': 'controlled-conversation',
        'api_key': 'synthetic-conversation-provider-key',
    })
    request('PUT', '/project-agent/configuration', json={'model_id': model['id']})
    with db.get_conn() as conn:
        # 手工样本不冒充未执行过的模型产出，来源 run 保持为空。
        vulnerability = create_vulnerability(conn, api.project_id, CreateVulnerabilityRequest(
            title='仅本地合成来源', severity='info', source_fact_id='origin',
        ))
        update_vulnerability(conn, api.project_id, vulnerability.id, UpdateVulnerabilityRequest(evidence_items=[{
            'id': 'prior-note', 'kind': 'text', 'source': 'manual', 'text': '合成来源说明：不是采集报文或漏洞确认。',
        }]))
        observation = conn.execute('SELECT id FROM vulnerability_observations WHERE project_id = ? AND vulnerability_id = ?',
                                   (api.project_id, vulnerability.id)).fetchone()['id']
        conn.execute("UPDATE projects SET status = 'paused' WHERE id = ?", (api.project_id,))
    source = {'project_id': api.project_id, 'vulnerability_id': vulnerability.id, 'observation_id': observation}
    dispatcher = api.task_runs[0]['runtime_context']['dispatcher_id']
    identity = container_instance_id(dispatcher, api.base_url)
    config = ContainerConfig(image='arena-worker:conversation-research', network_mode='none', completed_action='remove')
    options = ContainerRuntimeOptions.build(image=config.image, network_mode='none', dns=[], cap_add=[],
                                            features={'conversation_chat': 1}, scenario_key='conversation-chat')
    manager = ContainerManager(config, instance_id=identity)
    manager.configure_traffic_execution(api.machine, root=tmp_path / 'runtime', dispatcher_id=dispatcher, isolated=True)
    bridge = manager.traffic_execution
    client = manager._client
    cancellations, controls, futures = [], [], []
    registrations = []
    register_scope = api.machine.register_traffic_scope

    def observed_registration(owner, binding):
        response = register_scope(owner, binding)
        registrations.append(response)
        return response

    monkeypatch.setattr(api.machine, 'register_traffic_scope', observed_registration)
    command_results = []
    execute = ChatWorkspace.execute

    def observed_command(workspace, params):
        try:
            result = execute(workspace, params)
        except Exception as exc:
            command_results.append({'exception': type(exc).__name__, 'code': str(exc)})
            raise
        command_results.append(result)
        return result

    monkeypatch.setattr(ChatWorkspace, 'execute', observed_command)
    process_results = []
    communicate = ManagedProcess.communicate

    def observed_process(process, timeout):
        result = communicate(process, timeout)
        process_results.append({'returncode': result.returncode, 'completed': process.execution_completed})
        return result

    monkeypatch.setattr(ManagedProcess, 'communicate', observed_process)
    target = None
    runtime = None
    pool = ThreadPoolExecutor(max_workers=1)

    def conversation():
        return request('POST', '/agent-conversations', 201, json={})['id']

    def claim(cid):
        request('POST', f'/agent-conversations/{cid}/messages', json={
            'client_message_id': str(uuid4()), 'text': '仅继续研究隔离本地合成来源。', 'project_id': api.project_id,
        })
        response = api.machine.claim_agent_turn(cid, str(uuid4()))
        assert response.ok, response.data
        return response.data

    def run(cid, accepted):
        cancellation = TaskCancellation()
        cancellations.append(cancellation)
        binding = TenantModelExecutionBinding.model_validate(accepted['execution_binding'])
        worker = WorkerConfig(id='project-agent', engine='pi', task_types=['agent_chat'], concurrency=1,
                              priority=0, model_binding=_pi_model_binding(binding),
                              env={'CONTROL_ONLY_SECRET': 'synthetic-control-not-for-test'})
        settings = SimpleNamespace(runtime=SimpleNamespace(interval=1), tasks=SimpleNamespace(reason=SimpleNamespace(timeout=180)))
        future = pool.submit(run_agent_turn, settings, api.machine, manager, worker, cancellation, accepted, options)
        futures.append(future)
        return future, cancellation

    def finish(cid, accepted, result):
        assert result is not None
        response = api.machine.agent_turn_action(cid, accepted['message_id'], 'finish',
                                                 {'task_run_id': accepted['task_run_id'], **result})
        assert response.ok, response.data
        return response.data

    def api_tool(cid, accepted, method, params):
        return api.machine.agent_turn_action(cid, accepted['message_id'], 'tools', {
            'task_run_id': accepted['task_run_id'], 'tool_call_id': uuid4().hex, 'method': method, 'params': params,
        })

    try:
        empty = conversation()
        assert manager._get_container(manager.chat_container_name(empty)) is None
        assert request('DELETE', f'/agent-conversations/{empty}', 202)['cleanup']['state'] == 'completed'
        cid = conversation()
        other = conversation()
        other_name = manager.ensure_chat_running(other, options)
        other_control = client.containers.get(other_name)
        controls.append(other_control.id)
        accepted = claim(cid)
        assert accepted['project_id'] == api.project_id
        assert request('DELETE', f'/agent-conversations/{cid}', 409)['detail']
        name = manager.ensure_chat_running(cid, options)
        control = client.containers.get(name)
        controls.append(control.id)
        prompts = manager.chat_directory(cid) / 'prompts'
        local_server = ('import http.server; from pathlib import Path; '
                        'server = http.server.ThreadingHTTPServer(("127.0.0.1", 8990), http.server.SimpleHTTPRequestHandler); '
                        'Path("local-ready").touch(); server.serve_forever()')
        first_command = (
            'test -z "$CONTROL_ONLY_SECRET" && test ! -e /home/kali/.cairn/pi && test ! -e /var/run/docker.sock && '
            'test ! -e /tmp/cairn-prompts/model.py && '
            'curl -fsS --data-binary "sample body" "$(cat target.url)/reverse" > response.txt && '
            'printf shared-proof > shared.txt && '
            f'(setsid python3 -c {shlex.quote(local_server)} </dev/null >local.log 2>&1 &)'
        )
        # 后台命令已提交不代表监听已就绪；有界等到本地服务绑定端口后再验证跨命令现场。
        second_command = ('for attempt in $(seq 1 40); do test -f local-ready && break; sleep .05; done; '
                          'test -f local-ready && test "$(cat response.txt)" = "ydob elpmas" && '
                          'curl -fsS http://127.0.0.1:8990/shared.txt && '
                          'curl -fsS "$(cat target.url)/next?value=$(cat response.txt | tr " " _)"')
        plans = [tool('list_vulnerabilities', project_id=api.project_id), tool('read_vulnerability', **source),
                 tool('start_conversation_research', **source), {'wait': 'target-ready', **tool('bash', command=first_command)},
                 tool('bash', command=second_command), {'wait': 'capture-ready', **tool('read_conversation_research')},
                 {'select': 'request'}, {'select': 'response'}, {'select': 'publish'}, {'text': '本地确定性研究材料已保存；没有漏洞结论。'},
                 tool('start_conversation_research', **source), tool('bash', command=(
                     'test "$(cat shared.txt)" = shared-proof && '
                     '(setsid sh -c \'while :; do curl -fsS "$(cat target.url)/background" >/dev/null; sleep .2; done\' '
                     '</dev/null >background.log 2>&1 &)')), {'wait': 'never'}]
        for filename, data in [('plans.json', json.dumps(plans)), ('model.py', MODEL)]:
            path = prompts / filename
            path.write_text(data)
            os.chown(path, 1000, 1000)
        control.exec_run(['python3', str(Path('/tmp/cairn-prompts/model.py'))], detach=True)
        wait_for(lambda: (prompts / 'model-ready').exists())
        future, cancellation = run(cid, accepted)

        def reached(number):
            if future.done():
                raise AssertionError(f'controlled runner ended early: {future.result()}')
            return (prompts / f'call-{number}.json').exists()

        wait_for(lambda: reached(3), timeout=80)
        # 同步器可幂等重登同一个 scope；检查真实归属，不把后台请求次数当业务合同。
        assert registrations and all(item.ok for item in registrations), registrations
        assert len({item.data['id'] for item in registrations}) == 1
        prior_messages = json.loads((prompts / 'call-3.json').read_text())['messages']
        source_tool = next(item for item in prior_messages if item.get('tool_call_id') == 'call_1_0')
        source_data = json.loads(source_tool['content'])
        assert source_data.get('observation_id') == observation, source_data
        admitted_tool = prior_messages[-1]
        assert admitted_tool['role'] == 'tool'
        assert json.loads(admitted_tool['content'])['state'] == 'ready'
        runtime = bridge.conversation_runtime(cid)
        assert runtime.manifest['isolated'] is True
        site = tmp_path / 'site'
        site.mkdir()
        (site / 'server.py').write_text(SITE)
        target = client.containers.run('arena-traffic-gateway-dependencies:s0', ['python', '/site/server.py'],
            detach=True, network=runtime.egress.name, labels={'io.cairn.conversation.fixture': identity},
            volumes={str(site): {'bind': '/site', 'mode': 'rw'}})
        target.reload()
        address = target.attrs['NetworkSettings']['Networks'][runtime.egress.name]['IPAddress']
        directory = manager.chat_directory(cid) / 'workspace' / cid
        (directory / 'target.url').write_text(f'http://{address}:8989')
        wait_for(lambda: (site / 'ready').exists())
        (prompts / 'target-ready').touch()
        wait_for(lambda: reached(5))
        assert len(command_results) == 2 and all(item.get('exit_code') == 0 for item in command_results), command_results
        traffic_path = f'/agent-conversations/{cid}/traffic'

        def captured():
            rows = request('GET', traffic_path, params={'limit': 100})['items']
            posts = [row for row in rows if row['request']['method'] == 'POST']
            return next((row for row in posts if all(row['sync']['bodies'][side]['artifact_id'] for side in ('request', 'response'))), None)

        post = wait_for(captured, timeout=45)
        assert post['scope']['conversation_id'] == cid and 'project_id' not in post['scope']
        assert post['scope']['task_run_id'] == accepted['task_run_id']
        (prompts / 'capture-ready').touch()
        result = future.result(timeout=40)
        assert result and result['status'] == 'succeeded', result
        first = finish(cid, accepted, result)
        assert len(first['artifacts']) == 1
        assert cancellation.execution_lifecycle(task_finished=True)['termination_confirmed']
        history = json.loads((prompts / 'call-9.json').read_text())['messages']
        values = [json.loads(m['content']) for m in history
                  if m['role'] == 'tool' and m['tool_call_id'] not in {'call_3_0', 'call_4_0'}]
        assert any(value.get('observation_id') == observation for value in values)
        assert any('合成来源说明' in json.dumps(value, ensure_ascii=False) for value in values)
        assert any(value.get('content') == 'sample body' for value in values)
        response_material = next(value for value in values if value.get('content') == 'ydob elpmas')
        assert response_material['source_reference']['params']['conversation_id'] == cid
        body = request('GET', traffic_path + f"/{post['id']}/bodies/response", params={'preview': 'true'})
        assert base64.b64decode(body['content_base64']) == b'ydob elpmas'
        assert body['sha256'] == sha256(b'ydob elpmas').hexdigest()
        row = api.conn.execute('SELECT project_id, conversation_id FROM task_runs WHERE id = ?', (accepted['task_run_id'],)).fetchone()
        assert row['project_id'] is None and row['conversation_id'] == cid
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            api.conn.execute('UPDATE http_capture_scopes SET conversation_id = ? WHERE conversation_id = ?', (other, cid))
        assert api.outsider.get(api.base_url + traffic_path, timeout=10).status_code == 404
        assert api.user.get(api.base_url + f'/projects/{api.project_id}/traffic/{post["id"]}', timeout=10).status_code == 404

        second = claim(cid)
        assert second['resume'] is True and second['session_id'] == accepted['session_id']
        assert client.containers.get(manager.ensure_chat_running(cid, options)).id == control.id
        future, cancellation = run(cid, second)
        wait_for(lambda: reached(12), timeout=80)
        wait_for(lambda: (site / 'requests').exists() and b'/background' in (site / 'requests').read_bytes())
        stopped = request('POST', f'/agent-conversations/{cid}/messages/{second["message_id"]}/stop', 202)
        assert stopped['status'] == 'running' and stopped['cancel_requested_at']
        result = future.result(timeout=40)
        assert result and result['status'] == 'cancelled', (result, process_results, cancellation.execution_lifecycle(task_finished=True))
        finish(cid, second, result)
        assert cancellation.execution_lifecycle(task_finished=True)['termination_confirmed']
        assert (directory / 'shared.txt').read_text() == 'shared-proof'
        assert all(scope['termination_confirmed'] for scope in runtime.manifest['scopes'].values())
        counts = len((site / 'requests').read_bytes())
        # Docker 停止证明是主断言；短窗口仅检查目标没有继续收到后台请求。
        threading.Event().wait(.6)
        assert len((site / 'requests').read_bytes()) == counts
        assert api.conn.execute('SELECT status FROM projects WHERE id = ?', (api.project_id,)).fetchone()['status'] == 'paused'
        assert all(api.conn.execute('SELECT status FROM task_runs WHERE id = ?', (run['id'],)).fetchone()['status'] == 'running' for run in api.task_runs)
        # 合成目标仅为实验接入私有 egress，不属于会话资源；断言完成后由测试自行移除，清理器不代删未知端点。
        target.remove(force=True)
        target = None

        deleted = request('DELETE', f'/agent-conversations/{cid}', 202)
        assert deleted['cleanup']['state'] == 'requested' and 'targets' not in deleted['cleanup']
        pending = next(item for item in api.machine.pending_conversation_cleanups().data if item['conversation_id'] == cid)
        original_remove = docker.models.containers.Container.remove
        failed = threading.Event()

        def fail_once(obj, *args, **kwargs):
            if obj.id == control.id and not failed.is_set():
                failed.set()
                raise APIError('synthetic temporary delete failure')
            return original_remove(obj, *args, **kwargs)

        with monkeypatch.context() as injected:
            injected.setattr(docker.models.containers.Container, 'remove', fail_once)
            assert cleanup_conversation(manager, api.machine, pending) is False
        status = request('GET', f'/agent-conversations/{cid}/cleanup')['cleanup']
        assert failed.is_set() and status['state'] == 'failed'
        # 只推进本次隔离请求的重试时点；新管理器从 Server 取同一持久请求和精确 ID。
        api.conn.execute("UPDATE agent_conversations SET cleanup_request_json = jsonb_set(cleanup_request_json::jsonb, '{retry_at}', '0')::text WHERE id = ?", (cid,))
        retry = next(item for item in api.machine.pending_conversation_cleanups().data if item['conversation_id'] == cid)
        assert retry['request_id'] == pending['request_id'] and any(item['id'] == control.id for item in retry['targets'])
        replacement = ContainerManager(config, instance_id=identity)
        replacement.configure_traffic_execution(api.machine, root=tmp_path / 'runtime', dispatcher_id=dispatcher, isolated=True)
        try:
            assert cleanup_conversation(replacement, api.machine, retry)
        finally:
            replacement.traffic_execution.close()
        assert request('GET', f'/agent-conversations/{cid}/cleanup')['cleanup']['state'] == 'completed'
        with pytest.raises(NotFound):
            client.containers.get(control.id)
        assert client.containers.get(other_control.id).status == 'running'
        assert (directory / 'shared.txt').read_text() == 'shared-proof'
        assert (manager.chat_directory(cid) / 'control' / 'pi' / accepted['session_id'][3:] / 'session.jsonl').is_file()
        assert runtime.capture.is_dir()

        reader = conversation()
        reader_claim = claim(reader)
        indexed = api_tool(reader, reader_claim, 'list_conversation_research', source)
        assert indexed.ok and any(item['conversation_id'] == cid and item['conversation_deleted'] for item in indexed.data['items'])
        material = api_tool(reader, reader_claim, 'read_conversation_research', {'conversation_id': cid, 'flow_id': post['id'], 'side': 'response'})
        assert material.ok and material.data['content'] == 'ydob elpmas'
        published = api_tool(reader, reader_claim, 'publish_custom_markdown', {
            'title': '前序留存材料', 'content': '# 合成材料引用', 'source_references': [material.data['source_reference']],
        })
        assert published.ok
        stale = {**material.data['source_reference'], 'expected_version': '0' * 64}
        assert api_tool(reader, reader_claim, 'publish_custom_markdown', {
            'title': '拒绝过期来源', 'content': '# 不应发布', 'source_references': [stale],
        }).status_code == 409
        original = api.user.get(api.base_url + material.data['artifact_url'], timeout=10)
        assert original.status_code == 200 and original.content == b'ydob elpmas'
        finish(reader, reader_claim, {'status': 'succeeded', 'text': '仅受控材料核验。', 'error_code': None,
                                     'usage': None, 'session_initialized': False})
    finally:
        for cancellation in cancellations:
            cancellation.cancel('controlled_fixture_cleanup')
        for future in futures:
            try:
                future.result(timeout=40)
            except Exception:
                pass
        pool.shutdown(wait=True)
        bridge.close()
        if target is not None:
            target.remove(force=True)
        for identifier in controls:
            try:
                client.containers.get(identifier).remove(force=True)
            except NotFound:
                pass
        for owned in bridge._conversation_runtimes.values():
            for key, entry in sorted(owned.manifest.get('resources', {}).items(), key=lambda item: item[1]['kind'] == 'network'):
                if not entry.get('removed'):
                    owned._remove(key)


@pytest.mark.skipif(os.environ.get('CAIRN_TEST_POSTGRES_BOOTSTRAP') != '1', reason='仅使用一次性 PostgreSQL 和本地 HTTP')
def test_registered_file_bytes_cross_conversation_and_cleanup_discovery(traffic_api, tmp_path):
    from cairn.dispatcher.runtime.project_tools import ProjectToolBinding

    api = traffic_api

    def request(method, path, expected=200, **kwargs):
        response = api.user.request(method, api.base_url + path, timeout=15, **kwargs)
        assert response.status_code == expected, (response.status_code, response.text)
        return response.json() if response.content else None

    model = request('POST', '/models', 201, json={
        'display_name': '非HTTP材料验证', 'protocol': 'openai-chat-completions',
        'base_url': 'http://127.0.0.1:1', 'model_name': 'not-called', 'api_key': 'synthetic-file-secret',
    })
    request('PUT', '/project-agent/configuration', json={'model_id': model['id']})
    manager = SimpleNamespace(chat_directory=lambda cid: tmp_path / 'chats' / cid)

    def claim():
        cid = request('POST', '/agent-conversations', 201, json={})['id']
        request('POST', f'/agent-conversations/{cid}/messages', json={
            'client_message_id': str(uuid4()), 'text': '仅登记可控文件，不运行模型或访问目标。', 'project_id': api.project_id,
        })
        accepted = api.machine.claim_agent_turn(cid, str(uuid4()))
        assert accepted.ok
        turn = accepted.data
        binding = ProjectToolBinding(api.machine, api.project_id, cid, turn['message_id'], turn['task_run_id'], [])
        binding.workspace = ChatWorkspace(manager, cid)
        return cid, turn, binding

    def call(binding, method, params, call_id=None):
        return binding.dispatch({'method': method, 'params': params, 'tool_call_id': call_id or uuid4().hex})

    cid, turn, producer = claim()
    raw = b'\x00\xffcontrolled-file\r\n' + '原字节'.encode() + bytes(range(16))
    producer.workspace.write('chosen/result.bin', raw)
    producer.workspace.write('not-registered.bin', b'not published')
    registered = call(producer, 'register_conversation_material', {'workspace_path': 'chosen/result.bin'}, 'register-file')
    artifact = registered['artifact']
    assert registered['status'] == 'registered' and artifact['sha256'] == sha256(raw).hexdigest()
    assert call(producer, 'register_conversation_material', {'workspace_path': 'chosen/result.bin'}, 'register-file')['artifact'] == artifact
    row = api.conn.execute('SELECT * FROM artifacts WHERE id = ?', (artifact['id'],)).fetchone()
    assert row['conversation_id'] == cid and row['task_run_id'] == turn['task_run_id'] and row['project_id'] is None
    assert row['size'] == len(raw) and row['sha256'] == sha256(raw).hexdigest()
    assert api.user.get(api.base_url + artifact['download_url'], timeout=15).content == raw
    producer.workspace.write('blocked.bin', b'synthetic-file-secret')
    assert 'error' in call(producer, 'register_conversation_material', {'workspace_path': 'blocked.bin'})
    header = {'type': 'session', 'version': 1, 'id': 'synthetic', 'timestamp': '2026-11-03',
              'cwd': '/controlled', 'cairn': {'session': 'pi:synthetic'}}
    producer.workspace.write('copied.jsonl', json.dumps(header).encode())
    assert 'error' in call(producer, 'register_conversation_material', {'workspace_path': 'copied.jsonl'})
    assert api.conn.execute('SELECT count(*) AS n FROM artifacts WHERE conversation_id = ?', (cid,)).fetchone()['n'] == 1
    with pytest.raises(ValueError, match='chat_workspace_path_invalid'):
        call(producer, 'register_conversation_material', {'workspace_path': '../outside.bin'})

    reader, reader_turn, consumer = claim()

    def consume(deleted):
        indexed = call(consumer, 'list_conversation_research', {'project_id': api.project_id})
        entry = next(item for item in indexed['items'] if item['conversation_id'] == cid)
        assert entry['task_run_id'] == turn['task_run_id'] and entry['conversation_deleted'] is deleted
        params = {'conversation_id': cid, 'task_run_id': turn['task_run_id'], 'source': 'artifacts'}
        page = call(consumer, 'read_conversation_research', params)
        assert page['items'] == [artifact]
        view = call(consumer, 'read_conversation_research', {**params, 'artifact_id': artifact['id'], 'offset': 1, 'chunk_size': 8})
        assert view['content'] == raw[1:9].decode('utf-8', errors='backslashreplace') and view['bytes_read'] == 8
        copied = call(consumer, 'read_conversation_research', {**params, 'artifact_id': artifact['id'], 'workspace_path': 'retrieved/original.bin'})
        assert copied['artifact']['sha256'] == sha256(raw).hexdigest()
        assert consumer.workspace.read_bytes('retrieved/original.bin') == raw
        assert 'content_base64' not in copied
        return copied['source_reference']

    source = consume(False)
    response = api.machine.agent_turn_action(cid, turn['message_id'], 'finish', {
        'task_run_id': turn['task_run_id'], 'status': 'succeeded', 'text': '隔离工具路径验证，无模型执行。',
        'session_initialized': False,
    })
    assert response.ok
    deletion = request('DELETE', f'/agent-conversations/{cid}', 202)
    assert deletion['cleanup']['state'] == 'requested'
    assert consume(True) == source
    published = call(consumer, 'publish_custom_markdown', {
        'title': '文件来源复核', 'content': '# 原件读取记录\n不代表漏洞确认。', 'source_references': [source],
    })
    assert published['status'] == 'succeeded'
    stale = call(consumer, 'publish_custom_markdown', {
        'title': '过期来源拒绝', 'content': '# 不应发布', 'source_references': [{**source, 'expected_version': '0' * 64}],
    })
    assert stale['error']['code'] == 'agent_source_changed'
    assert api.outsider.get(api.base_url + artifact['download_url'], timeout=15).status_code == 404
    assert api.outsider.get(api.base_url + f'/agent-conversations/{cid}/cleanup', timeout=15).status_code == 404
    # 在隔离库改为另一操作者同租户的身份，验证工具鉴权不是只检查tenant。
    with db.get_conn() as conn:
        original = conn.execute('SELECT created_by FROM agent_conversations WHERE id = ?', (cid,)).fetchone()['created_by']
        stranger = conn.execute("SELECT id FROM users WHERE login = 'traffic-outsider'").fetchone()['id']
        conn.execute('UPDATE agent_conversations SET created_by = ? WHERE id = ?', (stranger, cid))
    denied = call(consumer, 'read_conversation_research', {'conversation_id': cid, 'artifact_id': artifact['id']})
    assert 'error' in denied
    with db.get_conn() as conn:
        conn.execute('UPDATE agent_conversations SET created_by = ? WHERE id = ?', (original, cid))
    # 发现接口不依赖页面内存，既有失败用尽请求仍能找到；不补建历史tombstone。
    history = request('POST', '/agent-conversations', 201, json={})['id']
    with db.get_conn() as conn:
        conn.execute("UPDATE agent_conversations SET deleted_at = '2026-11-03T00:00:00Z' WHERE id = ?", (history,))
        value = json.loads(conn.execute('SELECT cleanup_request_json FROM agent_conversations WHERE id = ?', (cid,)).fetchone()['cleanup_request_json'])
        value.update(state='failed', attempts=6, error_code='controlled_cleanup_failure')
        conn.execute('UPDATE agent_conversations SET cleanup_request_json = ? WHERE id = ?', (json.dumps(value), cid))
    requests = request('GET', '/agent-conversations/cleanup-requests')
    assert len(requests) == 1 and requests[0]['conversation_id'] == cid
    assert requests[0] == request('GET', f'/agent-conversations/{cid}/cleanup')
    assert requests[0]['cleanup']['attempts'] == 6 and 'targets' not in requests[0]['cleanup']
    assert api.outsider.get(api.base_url + '/agent-conversations/cleanup-requests', timeout=15).json() == []
    assert request('GET', f'/agent-conversations/{history}/cleanup')['cleanup'] is None
    assert api.user.get(api.base_url + artifact['download_url'], timeout=15).content == raw
