"""S2 opt-in：真实 Pi 流量读取、候选持久化、正常停止与 HTTP 业务接受。"""

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
from test_traffic_formal_execution import SITE, MODEL, wait_for
from cairn.dispatcher.config import ContainerConfig, WorkerConfig, PiModelConfig
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.traffic_reader import TrafficReadBinding
from cairn.server.services.assessment_reports import _build_scoped_report_packet
from cairn.report_readable import ReportSelection, selected_original
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.container_options import ContainerRuntimeOptions
from cairn.dispatcher.runtime.containers import ContainerManager, container_instance_id
from cairn.dispatcher.tasks.artifact_records import ProcessArtifactRecord
from cairn.dispatcher.tasks.candidate_capture_outbox import CandidateCaptureOutbox
from cairn.dispatcher.tasks.conclude_writer import build_vulnerability_for_conclude
from cairn.dispatcher.tasks.process_runner import run_worker_process
from cairn.dispatcher.tasks.delivery import register_delivery
from cairn.dispatcher.workers.adapters.pi import PiDriver
from cairn.dispatcher.worker_result_gateway import decode_pi_result
from cairn.dispatcher.contracts import validate_explore_payload


# 仅固定本地模型决策；flow ID、投影和选段身份必须来自 Pi 实际工具回执。
DECISION = r'''
state = {'stage': 0}
def call(name, **args):
    return {'tools': [{'name': name, 'args': args}]}
def next_action(body, number):
    assert number < 60, 'bounded fixture model exhausted'
    plan = plans['success']
    stage = state['stage']
    if stage == 0:
        state['stage'] = 1
        return call('bash', command=plan['command'])
    if stage == 1:
        state['stage'] = 2
        time.sleep(.5)
        return call('list_http_flows', limit=20)
    messages = [m for m in body['messages'] if m['role'] == 'tool']
    try:
        value = json.loads(messages[-1]['content'])
    except (ValueError, TypeError):
        value = {}
    if stage == 2:
        items = [item for item in value.get('items', []) if item['state'] == 'complete']
        pending = [item for item in value.get('items', []) if item['sync']['pending_sides']]
        if not items or not pending:
            time.sleep(.5)
            return call('list_http_flows', limit=20)
        state['flow'] = items[0]['flow_id']
        state['pending'] = pending[0]['flow_id']
        state['stage'] = 'preview'
        return call('read_http_flow', flow_id=state['pending'], side='response', offset=0, limit=32)
    if stage == 'preview':
        assert value['representation'] == 'body-preview-base64' and value['excerpt_eligible'] is False
        assert value['content_base64'] == '' and 'pointer' not in value
        state['stage'] = 3
        return call('read_http_flow', flow_id=state['flow'], side='response', offset=0, limit=32768)
    if stage == 3:
        if value.get('representation') != 'http-evidence-v1' or not value.get('pointer'):
            time.sleep(.5)
            return call('read_http_flow', flow_id=state['flow'], side='response', offset=0, limit=32768)
        assert 'formal-traffic' in value['text']
        state['projection'] = value
        state['stage'] = 4
        return call('bash', command=plan['background'])
    projection = state['projection']
    item = {'id': 'selected-exchange', 'kind': 'http', 'source': 'capture', 'flow_id': state['flow'],
        'excerpts': [{key: projection[key] for key in ('side', 'pointer', 'representation', 'sha256', 'revision')}]}
    result = {'accepted': True, 'data': {
        'fact': {'description': '隔离合成端点返回固定演示正文，不代表真实系统漏洞。', 'vulnerability': {
            'schema_version': 2, 'title': '合成端点读取演示', 'severity': 'low', 'vuln_type': 'synthetic',
            'target': plan['target'], 'confidence': 1.0, 'remediation': '仅用于隔离验收，无真实系统修复建议。',
            'identity': {'vulnerability_class': 'information_disclosure',
                'asset': {'host': plan['host'], 'port': 8989}, 'entrypoint': {'route': '/repeat'}},
            'reproduction_steps': [{'action': '读取同一合成 URL 两次，显式选取其中一次真实流量。',
                'observation': '返回合成 formal-traffic 正文。', 'evidence_item_ids': ['selected-exchange']}],
            'evidence_items': [item, {'id': 'pending-exchange', 'kind': 'http', 'source': 'capture', 'flow_id': state['pending']}]}},
        'assessment_record': {'hypothesis': '仅验证合成 HTTP 证据链', 'outcome': 'finding',
            'observations': ['实际读取固定合成正文'], 'evidence_refs': []}}}
    (root / 'submitted.json').write_text(json.dumps(result))
    return call('submit_result', **result)
'''
SUCCESS_MODEL = MODEL.replace('class Handler(http.server.BaseHTTPRequestHandler):', DECISION + '\nclass Handler(http.server.BaseHTTPRequestHandler):').replace(
    "action = plan[min(number, len(plan)-1)]", "action = next_action(body, number)")


@pytest.mark.skipif(
    os.environ.get('CAIRN_TRAFFIC_INTEGRATION') != '1' or os.environ.get('CAIRN_TEST_POSTGRES_BOOTSTRAP') != '1',
    reason='需明确启用隔离 Docker 与私有 PostgreSQL 实验',
)
def test_real_flow_candidate_conclude_and_normal_scope_stop(traffic_api, monkeypatch):
    api = traffic_api
    run = api.task_runs[0]
    dispatcher_id = run['runtime_context']['dispatcher_id']
    owner = container_instance_id(dispatcher_id, api.base_url)
    config = ContainerConfig(image='arena-worker:s2', network_mode='none', completed_action='remove')
    manager = ContainerManager(config, instance_id=owner)
    docker_client = docker.from_env(timeout=10)
    target = control = result = None
    cancellation = TaskCancellation(generation=run['execution_generation'])
    with tempfile.TemporaryDirectory(prefix='s2-flow-') as temporary:
        root = Path(temporary)
        site = root / 'site'
        site.mkdir()
        ca = certificate(site)
        (site / 'server.py').write_text(SITE)
        manager.configure_traffic_execution(api.machine, root=root / 'runtime', dispatcher_id=dispatcher_id,
                                            isolated=True, upstream_ca=site / 'ca.pem')
        released = threading.Event()
        held = {}
        upload = api.machine.sync_traffic_body
        def hold_one_response(project_id, scope_id, flow_id, side, **kwargs):
            # 只注入传输未送达；释放后调用真实上传，不伪造 Server 成功回执。
            if side == 'response' and kwargs['size'] > 0:
                held.setdefault('flow_id', flow_id)
                if held['flow_id'] == flow_id and not released.is_set():
                    return ApiResult(status_code=0, error_code='controlled_upload_hold')
            return upload(project_id, scope_id, flow_id, side, **kwargs)
        monkeypatch.setattr(api.machine, 'sync_traffic_body', hold_one_response)
        bridge = manager.traffic_execution
        bridge.recover()
        project_view = api.machine.get_project(api.project_id)
        project = project_view.project
        model_response = api.user.get(api.base_url+'/models', timeout=15)
        assert model_response.status_code == 200
        model = model_response.json()[0]
        configured = api.user.put(api.base_url+'/models/'+model['id'], timeout=15, json={
            'display_name': model['display_name'], 'protocol': 'openai-chat-completions',
            'base_url': 'http://127.0.0.1:8765', 'inference_endpoint': 'http://127.0.0.1:8765/chat/completions',
            'model_name': 'success', 'expected_revision': model['revision'],
        })
        assert configured.status_code == 200, configured.text
        tenant_worker_id = api.conn.execute('SELECT tenant_worker_id FROM project_tenant_workers WHERE project_id = ?',
                                            (project.id,)).fetchone()['tenant_worker_id']
        execution = api.machine.resolve_execution_binding(project.id, tenant_worker_id, 'explore')
        provenance = api.machine.update_task_run(project.id, run['id'], status='running',
            runtime_context={'execution_binding': execution.execution_provenance()})
        assert provenance.ok, provenance.data
        options = ContainerRuntimeOptions.build(image=config.image, network_mode='none', dns=[], cap_add=[],
                    features=project.scenario_snapshot['features'], scenario_key='goal-driven-v1')
        prefix = f'{api.base_url}/projects/{project.id}/traffic'
        def get(path='', **params):
            response = api.user.get(prefix + path, params=params, timeout=15)
            assert response.status_code == 200, response.text
            return response.json()
        try:
            name = manager.ensure_running(project.id, options)
            control = docker_client.containers.get(name)
            runtime = bridge._runtimes[project.id]
            target = docker_client.containers.create('arena-traffic-gateway-dependencies:s0',
                ['sh', '-c', 'while ! test -f /site/server.key; do sleep .1; done; exec python /site/server.py'],
                network=runtime.egress.name, labels={'io.cairn.s2.fixture': owner},
                volumes={str(site): {'bind': '/site', 'mode': 'rw'}})
            target.start()
            target.reload()
            address = target.attrs['NetworkSettings']['Networks'][runtime.egress.name]['IPAddress']
            certificate(site, issuer=ca, address=address)
            url = f'http://{address}:8989/repeat'
            plans = {'success': {
                'host': address, 'target': url,
                'command': 'test ! -e /tmp/cairn-prompts/plans.json && '
                    + f'curl --retry 10 --retry-connrefused --retry-delay 1 -fsS {shlex.quote(url)} && curl -fsS {shlex.quote(url)}',
                'background': f"setsid sh -c 'while :; do curl -fsS http://{address}:8989/background >/dev/null; sleep .2; done' </dev/null >/dev/null 2>&1 & sleep 1",
            }}
            for filename, content in [('plans.json', json.dumps(plans)), ('model.py', SUCCESS_MODEL)]:
                path = runtime.control_prompts / filename
                path.write_text(content)
                os.chown(path, 1000, 1000)
            control.exec_run(['python3', '/tmp/cairn-prompts/model.py'], detach=True)
            wait_for(lambda: (runtime.control_prompts / 'model-ready').exists())
            capability = manager.prepare_task_execution(project, run['id'], cancellation, options)
            scope_id = bridge.task(project.id, run['id']).scope_id
            scope = next(scope for scope in runtime.scopes if scope.id == scope_id)
            assert len(runtime.manifest['scopes']) == 1
            worker = WorkerConfig(id=execution.worker_id, name='traffic-source-1', model_id=execution.model_id,
                engine='pi', task_types=['explore'], concurrency=1, priority=0,
                model_binding=PiModelConfig(protocol=execution.protocol, model_name=execution.model_name,
                    base_url=execution.base_url, inference_endpoint=execution.inference_endpoint,
                    auth_header=execution.auth_header, api_key=execution.api_key))
            driver = PiDriver()
            session = driver.prepare_session()
            register_delivery(api.machine, manager, name, project_view, run['id'], worker,
                phase='explore_execute', session=session, timeout_seconds=180,
                binding={'assessment_protocol': 'goal-driven-v1'})
            invocation = driver.build_execute(worker, '仅执行隔离合成 HTTP 证据链验收。', session,
                phase='explore_execute', project_id=project.id, task_run_id=run['id'], timeout_seconds=180,
                task_execution=capability, session_root='/home/kali/.cairn/pi',
                binding={'assessment_protocol': 'goal-driven-v1'})
            result = run_worker_process(manager, name, worker, invocation, phase='explore_execute',
                timeout_seconds=180, cancellation=cancellation, client=api.machine, project_id=project.id,
                task_run_id=run['id'], candidate_outbox=CandidateCaptureOutbox(root/'outbox', owner_id=owner))
            assert result.returncode == 0 and not result.cancelled and not result.timed_out, (result, result.read_stderr())
            # 一个候选加一次本地校验 outcome，两帧均走正式持久化入口。
            assert result.candidate_host_durable and result.candidate_capture_frames == 2
            assert not result.candidate_capture_pending and result.candidate_capture_integrity_error is None
            assert runtime.manifest['scopes'][scope.id]['termination_confirmed']
            with pytest.raises(docker.errors.NotFound):
                docker_client.containers.get(scope.container_id)
            before = (site/'requests').read_bytes().count(b'/background\n')
            assert before > 0
            time.sleep(1)
            assert (site/'requests').read_bytes().count(b'/background\n') == before
            assert (site/'requests').read_bytes().count(b'/repeat\n') == 2
            candidates = api.conn.execute('SELECT * FROM task_run_candidates WHERE project_id = ? AND task_run_id = ?',
                                         (project.id, run['id'])).fetchall()
            assert len(candidates) == 1 and candidates[0]['artifact_count'] == 0
            candidate = json.loads(bytes(candidates[0]['raw_bytes']))
            assert candidate == json.loads((runtime.control_prompts/'submitted.json').read_text())
            outcome = decode_pi_result(result.read_stdout(), phase='explore_execute', validator=validate_explore_payload,
                project_id=project.id, task_run_id=run['id'], session=session, assessment_protocol='goal-driven-v1')
            assert outcome.succeeded, outcome.failure
            wire = build_vulnerability_for_conclude(outcome.payload, ProcessArtifactRecord(refs=[]), require_current_run_reproduction=True)
            assert wire is not None
            item = wire['evidence_items'][0]
            flow_id = item['flow_id']
            flow = get('/'+flow_id)
            assert flow['scope']['id'] == scope.id and flow['scope']['task_run_id'] == run['id']
            assert base64.b64decode(flow['request']['target_base64']) == b'/repeat'
            assert not flow['observations']
            projection = get(f'/{flow_id}/projection/response', offset=0, limit=32768)
            excerpt = item['excerpts'][0]
            assert excerpt['sha256'] == projection['sha256'] and excerpt['revision'] == projection['revision']
            def conclude():
                return api.machine.conclude_with_evidence(project.id, run['intent_id'], 'traffic-source-1',
                    candidate['data']['fact']['description'], evidence=[], task_run_id=run['id'], vulnerability=wire,
                    assessment_record=candidate['data']['assessment_record'])
            pending = conclude()
            assert pending.status_code == 409 and pending.data['detail'] == 'traffic_evidence_pending', pending
            assert api.conn.execute('SELECT count(*) AS count FROM vulnerability_observations WHERE project_id = ?',
                                    (project.id,)).fetchone()['count'] == 0
            assert not get('/'+flow_id)['observations']
            assert wire['evidence_items'][1]['flow_id'] == held['flow_id']
            released.set()
            wait_for(lambda: not get('/'+held['flow_id'])['sync']['pending_sides'], timeout=40)
            accepted = conclude()
            assert accepted.ok, (accepted.status_code, accepted.data)
            observation = api.conn.execute('SELECT * FROM vulnerability_observations WHERE project_id = ? AND task_run_id = ?',
                                           (project.id, run['id'])).fetchone()
            assert observation is not None
            assert json.loads(observation['evidence_items_json']) == wire['evidence_items']
            assert json.loads(observation['reproduction_steps_json'])[0]['evidence_item_ids'] == ['selected-exchange']
            assert observation['id'] in json.dumps(get('/'+flow_id)['observations'])
            request_count = (site/'requests').read_bytes()
            vuln_id = observation['vulnerability_id']
            detail_url = f'{api.base_url}/projects/{project.id}/vulnerabilities/{vuln_id}'
            detail = api.user.get(detail_url, params={'observation_id': observation['id']}, timeout=15)
            assert detail.status_code == 200, detail.text
            assert detail.json()['readable']['observation_id'] == observation['id']
            assert 'formal-traffic' in detail.json()['readable']['key_evidence_markdown']
            exported = api.user.get(detail_url+'/report', params={'observation_id': observation['id']}, timeout=15)
            assert exported.status_code == 200 and 'formal-traffic' in exported.text, exported.text
            source = api.user.get(f'{api.base_url}/api/v1/verification-sources/projects/{project.id}/vulnerabilities/{vuln_id}/preview',
                                  params={'observation_id': observation['id']}, timeout=15)
            assert source.status_code == 200, source.text
            historical = source.json()['flow_evidence'][flow_id]
            assert historical['material_role'] == 'historical_input' and historical['scope']['task_run_id'] == run['id']
            assert historical['projections']['response']['sha256'] == projection['sha256']
            packet = _build_scoped_report_packet(api.conn, project.id, vuln_id, observation['id'])
            frozen = json.loads(packet.model_dump_json())
            selection = ReportSelection(side='response', flow_id=flow_id, byte_start=0,
                byte_end=projection['next_offset'], sha256=excerpt['sha256'], representation=excerpt['representation'],
                revision=excerpt['revision'])
            # 消费者只接收序列化后的 Packet，不拿 client/conn，也不回退查询 live flow。
            assert selected_original(selection, frozen)[0] == projection['text']
            second_run = api.task_runs[1]['id']
            reader = TrafficReadBinding(api.machine, project.id, second_run)
            with pytest.raises(ValueError, match='traffic_historical_required'):
                reader.dispatch({'method': 'read_http_flow', 'params': {'flow_id': flow_id, 'side': 'response'}})
            past = reader.dispatch({'method': 'read_http_flow', 'params': {'flow_id': flow_id, 'side': 'response', 'historical': True}})
            assert past['scope']['task_run_id'] == run['id'] and past['sha256'] == projection['sha256']
            from cairn.evidence_contracts import EvidenceItem
            from cairn.server.services.vulnerabilities import has_current_evidence
            historical_item = EvidenceItem.model_validate({**item, 'source': 'historical'})
            assert not has_current_evidence(api.conn, project.id, second_run, [historical_item])
            assert api.user.get(f'{api.base_url}/projects/{api.other_project_id}/traffic/{flow_id}', timeout=15).status_code == 404
            assert api.outsider.get(prefix+'/'+flow_id, timeout=15).status_code == 404
            assert (site/'requests').read_bytes() == request_count
            assert len(runtime.manifest['scopes']) == 1
            assert api.conn.execute('SELECT count(*) AS count FROM vulnerability_observations WHERE project_id = ?',
                                    (project.id,)).fetchone()['count'] == 1
            print(json.dumps({'task_run_executors': 1, 'durable_candidates': 1, 'same_url_requests': 2,
                'normal_scope_stop': True, 'http_conclude': True, 'upload_pending_then_accept': True,
                'export': True, 'frozen_packet_consumer': True, 'historical_retest_preview': True,
                'observation_id': observation['id']}))
        finally:
            cancellation.cancel('fixture_cleanup')
            if result is not None:
                result.cleanup()
            if target is not None:
                target.remove(force=True)
            if control is not None:
                manager.remove_container(control.name)
            manager.close()
            docker_client.close()
