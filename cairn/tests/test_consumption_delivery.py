"""受控 SDK → 独立宿主 spool → 认证 HTTP → 隔离 PostgreSQL 的定向接缝。"""
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.tasks.consumption_outbox import ConsumptionOutbox
from cairn.server import db
from cairn.server.app import app
from cairn.server.auth import RequestPrincipal, create_machine_principal
from cairn.server.services.consumption import overview_consumption, save_response, estimate, normalize_http_usage
from test_direct_consumption import configured
from test_postgres_bootstrap import empty_postgres_url, postgres_instance

pytestmark = pytest.mark.skipif(os.environ.get('CAIRN_TEST_POSTGRES_BOOTSTRAP') != '1', reason='仅一次性 PostgreSQL')
ROOT = Path(__file__).resolve().parents[2]


class LocalClient:
    def __init__(self, token):
        self.http = TestClient(app, headers={'Authorization': 'Bearer ' + token})
        self.drop_ack = False
        self.drop_registration = False

    def register_consumption(self, identifier, **scope):
        if self.drop_registration:
            return ApiResult(status_code=503)
        response = self.http.put(f'/model-consumption/{identifier}', json=scope)
        return ApiResult(status_code=response.status_code, data=response.json())

    def submit_consumption(self, identifier, event):
        response = self.http.post(f'/model-consumption/{identifier}/responses', json=event)
        return ApiResult(status_code=0 if self.drop_ack else response.status_code, data=response.json())


def bind(user, *, project='fixture-project', register=True):
    owner, token = create_machine_principal('consumption-' + uuid4().hex)
    snapshot = {'model_id': 'model', 'model_revision': 1, 'model_name': 'fixture', 'protocol': 'anthropic-messages',
                'pricing': {'currency': 'USD', 'input': '1.25', 'output': '5', 'cache_read': '0.25', 'cache_write': '2'}}
    with db.get_conn() as conn:
        conn.execute("INSERT INTO projects(id,title,status,created_at,tenant_id) VALUES (?,?,'pending',?,?)",
                     (project, '隔离合成项目', '2026-11-04T00:00:00Z', user.tenant_id))
        conn.execute("INSERT INTO task_runs(id,project_id,task_type,worker_name,worker_type,status,runtime_context_json) VALUES ('same-run',?,'bootstrap','fixture','pi','running',?)",
                     (project, json.dumps({'dispatcher_id': owner, 'execution_binding': snapshot})))
        conn.execute('INSERT INTO model_consumption_rates(tenant_id,model_id,revision,model_json) VALUES (?,?,?,?) ON CONFLICT DO NOTHING',
                     (user.tenant_id,'model',1,json.dumps(snapshot)))
    client, identifier = LocalClient(token), str(uuid4())
    if register:
        response = client.http.put(f'/model-consumption/{identifier}', json={'task_run_id': 'same-run', 'project_id': project})
        assert response.status_code == 200, response.text
    return client, identifier


def test_real_runner_failure_survives_source_deletion_ack_loss_and_price_change(configured, tmp_path):
    client, identifier = bind(configured)
    source = tmp_path / 'project-spool.jsonl'
    source.touch(mode=0o600)
    outbox = ConsumptionOutbox(tmp_path / 'independent-consumption')
    outbox.retain(identifier, source)
    script = """
      import { fixture } from './container/pi-agent/tests/fixtures.mjs';
      import { runRequest } from './container/pi-agent/dist/runner.js';
      const cleanup = [];
      const { request } = await fixture({ after: fn => cleanup.push(fn) });
      request.capabilities.consumption = {protocol:'cairn-consumption-v1', id:process.argv[2], spool_path:process.argv[1]};
      request.capabilities.candidate_capture.max_candidate_bytes = 1;
      try { const result = await runRequest(request); console.log(result.status); }
      finally { for (const fn of cleanup.reverse()) await fn(); }
    """
    result = subprocess.run(['node','--input-type=module','-e',script,str(source),identifier], cwd=ROOT,
                            check=True,capture_output=True,text=True,timeout=30)
    assert result.stdout.strip() == 'failed'
    events = [json.loads(line) for line in source.read_text().splitlines()]
    assert events[0]['measurement'] == 'started'
    assert events[-1]['usage']['input_tokens'] == 10
    assert all('fixture-secret' not in json.dumps(event) for event in events)
    source.unlink()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM projects WHERE id='fixture-project'")
        conn.execute("UPDATE model_consumption_rates SET model_json='{}'")
    client.drop_ack = True
    outbox.finish(identifier)
    outbox.flush(client)
    assert (outbox.root / f'{identifier}.jsonl').exists()
    client.drop_ack = False
    outbox.flush(client)
    assert (outbox.root / f'{identifier}.delivered').exists()
    with db.get_conn() as conn:
        rows = conn.execute('SELECT * FROM model_consumption WHERE binding_id=?', (identifier,)).fetchall()
        assert len(rows) == 1
        assert rows[0]['input_tokens'] == 10 and rows[0]['output_tokens'] == 5
        assert rows[0]['amount'] == Decimal('0.0000375')
        assert rows[0]['unpriced']
        overview = overview_consumption(conn, configured)
        assert overview['total']['tokens'] == 15
        assert overview['total']['partial_responses'] == 1


def test_runner_gap_registration_recovery_identity_and_lost_ack(configured, tmp_path):
    client, identifier = bind(configured, register=False)
    _, other_token = create_machine_principal('other-consumption-' + uuid4().hex)
    scope = {'task_run_id': 'same-run', 'project_id': 'fixture-project', 'conversation_id': None}
    assert LocalClient(other_token).register_consumption(identifier, **scope).status_code == 403
    source = tmp_path / 'missing-spool.jsonl'
    source.touch(mode=0o600)
    outbox = ConsumptionOutbox(tmp_path / 'outbox')
    outbox.prepare(identifier, **scope)
    outbox.retain(identifier, source)
    source.unlink()
    session = 'pi:' + str(uuid4())
    script = """
      import { fixture } from './container/pi-agent/tests/fixtures.mjs';
      import { runRequest } from './container/pi-agent/dist/runner.js';
      const cleanup = [];
      const { request, calls, freshTaskExecution } = await fixture({ after: fn => cleanup.push(fn) });
      request.project_id = 'fixture-project'; request.task_run_id = 'same-run'; request.session = process.argv[3];
      request.capabilities.task_execution = freshTaskExecution('same-run');
      request.capabilities.consumption = {protocol:'cairn-consumption-v1', id:process.argv[2], spool_path:process.argv[1]};
      try {
        const result = await runRequest(request, {audit: event => process.stderr.write(JSON.stringify(event)+'\\n')});
        console.log(JSON.stringify({status:result.status, calls:calls.length}));
      } finally { for (const fn of cleanup.reverse()) await fn(); }
    """
    result = subprocess.run(['node', '--input-type=module', '-e', script, str(source), identifier, session],
                            cwd=ROOT, check=True, capture_output=True, text=True, timeout=30)
    assert json.loads(result.stdout) == {'status': 'succeeded', 'calls': 1}
    outbox.capture_gaps(identifier, BytesIO(result.stderr.encode()), project_id='fixture-project',
                        task_run_id='same-run', phase='explore_execute', session=session)
    assert (outbox.root / f'{identifier}.gaps.jsonl').exists()
    outbox.finish(identifier)
    client.drop_registration = True
    outbox.flush(client)
    with db.get_conn() as conn:
        # 尚未注册的宿主 outbox 对 Server 不可见，不把零解释为完整覆盖。
        assert overview_consumption(conn, configured)['unobserved_bindings'] == 0
        assert conn.execute('SELECT * FROM model_consumption WHERE binding_id=?', (identifier,)).fetchone() is None
    client.drop_registration = False
    client.drop_ack = True
    restarted = ConsumptionOutbox(outbox.root)
    restarted.flush(client)
    assert (outbox.root / f'{identifier}.gaps.jsonl').exists()
    client.drop_ack = False
    restarted.flush(client)
    restarted.flush(client)
    assert (outbox.root / f'{identifier}.delivered').exists()
    assert LocalClient(other_token).submit_consumption(identifier, {
        'response_seq': 0, 'revision': 1, 'measurement': 'started', 'usage': {},
    }).status_code == 403
    with db.get_conn() as conn:
        rows = conn.execute('SELECT * FROM model_consumption WHERE binding_id=?', (identifier,)).fetchall()
        assert len(rows) == 1
        assert rows[0]['input_tokens'] == 10 and rows[0]['output_tokens'] == 5
        assert rows[0]['missing_responses'] == 1
        summary = overview_consumption(conn, configured)
        assert summary['total']['responses'] == 1 and summary['total']['partial_responses'] == 1
        assert summary['unobserved_bindings'] == 0


def test_cumulative_receipts_permissions_scope_and_business_dates(configured, monkeypatch):
    client, identifier = bind(configured)
    other_owner, other_token = create_machine_principal('other-consumption')
    event = {'response_seq': 0, 'revision': 1, 'occurred_at': '2026-11-01T05:30:00Z', 'time_basis': 'observed',
             'measurement': 'raw', 'usage': {'input_tokens': 80,'output_tokens': 10,'cache_read_tokens': 20,'cache_write_tokens': 0}}
    assert client.submit_consumption(identifier,event).ok
    assert client.submit_consumption(identifier,event).ok
    assert LocalClient(other_token).submit_consumption(identifier,event).status_code == 403
    assert TestClient(app).post(f'/model-consumption/{identifier}/responses',json=event).status_code in (401,403)
    conflict = {**event,'measurement':'started'}
    assert client.submit_consumption(identifier,conflict).status_code == 409
    updated = {**event,'revision':2,'usage':{**event['usage'],'output_tokens':30}}
    assert client.submit_consumption(identifier,updated).ok
    assert client.submit_consumption(identifier,event).ok
    assert client.submit_consumption(identifier,{**event,'revision':3}).status_code == 409
    assert client.submit_consumption(identifier,{**event,'response_seq':1,'occurred_at':'2026-11-01T06:30:00Z'}).ok
    monkeypatch.setenv('CAIRN_BUSINESS_TIMEZONE','America/New_York')
    with db.get_conn() as conn:
        conn.execute("UPDATE schema_migrations SET applied_at='2026-10-31T12:00:00Z' WHERE version=83")
        summary = overview_consumption(conn,configured,now=datetime(2026,11,1,20,tzinfo=timezone.utc))
        assert summary['today']['tokens'] == 240
        assert summary['total']['responses'] == 2
        assert len(summary['total']['amounts']) == 1
        assert summary['total']['amounts'][0]['currency'] == 'USD'
        assert Decimal(summary['total']['amounts'][0]['amount']) == Decimal('0.000410')
        assert summary['start'].endswith('04:00:00+00:00') and summary['end'].endswith('05:00:00+00:00')
        other = RequestPrincipal('user','different',tenant_id='different',role='user')
        assert overview_consumption(conn,other)['total']['responses'] == 0
        platform = str(uuid4())
        conn.execute("INSERT INTO model_consumption_bindings(id,owner_id,source,model_json) VALUES (?,'server','connectivity_test','{}')", (platform,))
        save_response(conn,platform,event)
        admin = RequestPrincipal('user','admin',tenant_id='different',role='super_admin')
        assert overview_consumption(conn,admin)['total']['responses'] == 3


def test_duplicate_run_ids_keep_project_owner_and_registration_scope(configured):
    first, first_id = bind(configured, project='first-project')
    second, second_id = bind(configured, project='second-project')
    event = {'response_seq': 0, 'revision': 1, 'measurement': 'raw', 'usage': {'input_tokens': 7}}
    assert first.submit_consumption(first_id, event).ok
    assert second.submit_consumption(second_id, {**event, 'usage': {'input_tokens': 99}}).ok
    assert first.submit_consumption(second_id, event).status_code == 403
    wrong_scope = first.http.put(f'/model-consumption/{first_id}', json={'task_run_id': 'same-run', 'project_id': 'second-project'})
    assert wrong_scope.status_code == 403
    new_wrong_scope = first.http.put(f'/model-consumption/{uuid4()}', json={'task_run_id': 'same-run', 'project_id': 'second-project'})
    assert new_wrong_scope.status_code == 403
    with db.get_conn() as conn:
        assert conn.execute('SELECT input_tokens FROM model_consumption WHERE binding_id=?', (first_id,)).fetchone()['input_tokens'] == 7
        assert conn.execute('SELECT input_tokens FROM model_consumption WHERE binding_id=?', (second_id,)).fetchone()['input_tokens'] == 99


def test_receipt_rejects_coercion_overflow_and_ambiguous_dates_without_database_write(configured):
    client, identifier = bind(configured)
    event = {'response_seq': 0, 'revision': 1, 'measurement': 'raw', 'usage': {'input_tokens': 7}}
    invalid = [
        {'response_seq': '0'}, {'response_seq': True}, {'revision': 1.0}, {'revision': 2**31},
        {'missing_responses': '1'}, {'missing_responses': True},
        {'occurred_at': '2026-11-04T00:00:00'}, {'occurred_at': 1793750400}, {'occurred_at': '1793750400'},
        {'usage': {'input_tokens': True}}, {'usage': {'input_tokens': 2**63}},
    ]
    for change in invalid:
        assert client.submit_consumption(identifier, {**event, **change}).status_code == 422, change
    with db.get_conn() as conn:
        assert conn.execute('SELECT COUNT(*) AS n FROM model_consumption WHERE binding_id=?', (identifier,)).fetchone()['n'] == 0
    event['occurred_at'] = '2026-11-04T08:00:00+08:00'
    result = client.submit_consumption(identifier, event)
    assert result.ok
    from cairn.server.services.consumption import receipt_digest
    assert result.data['sha256'] == receipt_digest(event)


def test_decimal_and_raw_usage_do_not_double_count_reasoning_or_cache():
    usage = normalize_http_usage('openai-chat-completions', {'usage': {'prompt_tokens':100,'completion_tokens':30,
        'prompt_tokens_details':{'cached_tokens':20},'completion_tokens_details':{'reasoning_tokens':25}}})
    assert usage == {'input_tokens':80,'output_tokens':30,'cache_read_tokens':20,'cache_write_tokens':0}
    assert estimate(usage,{'currency':'USD','input':'1.234567890123','output':'0','cache_read':'0'}) == (
        'USD',Decimal('0.00009876543120984'),False)
    assert estimate(usage,{}) == (None,None,True)
    unknown = normalize_http_usage('openai-responses', {'usage':{'input_tokens':100,'output_tokens':30}})
    assert unknown['input_tokens'] is None and unknown['cache_read_tokens'] is None
