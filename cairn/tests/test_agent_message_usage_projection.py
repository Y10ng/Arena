"""隔离 PostgreSQL 验证历史导入与统一消费消息投影，不读取正式业务库。"""

import json
import os
from uuid import uuid4

import pytest

from cairn.server import db
from cairn.server.consumption_schema import import_chat_usage
from cairn.server.services import agent_conversations as service
from cairn.server.services.consumption import FIELDS, message_consumption, overview_consumption, save_response
from test_direct_consumption import configured
from test_global_conversation_ownership import owner, new_turn, finish_body
from test_postgres_bootstrap import empty_postgres_url, postgres_instance

pytestmark = pytest.mark.skipif(os.environ.get('CAIRN_TEST_POSTGRES_BOOTSTRAP') != '1', reason='仅一次性 PostgreSQL')


def _conversation(conn, principal, identifier):
    conn.execute('''INSERT INTO agent_conversations(id,tenant_id,created_by,session_id,created_at)
        VALUES (?,?,?,?,?)''', (identifier, principal.tenant_id, principal.subject_id, 'pi:' + str(uuid4()), '2026-11-01T00:00:00Z'))


def _turn(conn, conversation, run, context, *, status='failed'):
    conn.execute('''INSERT INTO task_runs(id,conversation_id,task_type,worker_name,worker_type,status,runtime_context_json)
        VALUES (?,?,'agent_chat','fixture','pi',?,?)''', (run, conversation, status, json.dumps(context)))
    conn.execute('''INSERT INTO agent_messages(conversation_id,client_message_id,text,created_at,task_run_id,answer)
        VALUES (?,?,'原始提问','2026-11-01T00:00:00Z',?,'原始回复')''', (conversation, run, run))


def test_history_import_is_bounded_idempotent_and_message_projection_is_scoped(configured):
    old = {'usage': dict(zip(FIELDS, (12, 0, None, 3))), 'private_runtime_marker': 'synthetic-only',
           'execution_binding': {'model_name': 'old-fixture', 'pricing': {'currency': 'USD', 'input': '99'}}}
    with db.get_conn() as conn:
        for identifier in ('old-a', 'old-b', 'new'):
            _conversation(conn, configured, identifier)
        # 同一 run id 在不同会话合法存在，导入键和消息查询不能串账。
        _turn(conn, 'old-a', 'same-run', old)
        _turn(conn, 'old-b', 'same-run', {'usage': {'output_tokens': 9}})
        _turn(conn, 'old-a', 'active', {'usage': {'input_tokens': 111}}, status='running')
        _turn(conn, 'old-a', 'null-usage', {'usage': None})
        _turn(conn, 'new', 'same-run', {'usage': {'input_tokens': 777}, 'consumption_source': 'receipts-v1'})
        _turn(conn, 'new', 'already-bound', {'usage': {'input_tokens': 888}})
        conn.execute('''INSERT INTO model_consumption_bindings
            (id,tenant_id,owner_id,source,task_run_id,conversation_id,model_json)
            VALUES ('receipt',?,'machine','agent_chat','same-run','new','{}'),
                   ('other-receipt',?,'machine','agent_chat','already-bound','new','{}')''',
            (configured.tenant_id, configured.tenant_id))
        save_response(conn, 'receipt', {'response_seq': 0, 'revision': 1, 'measurement': 'raw',
            'occurred_at': '2026-11-04T00:00:00Z', 'usage': dict(zip(FIELDS, (20, 5, 0, 0)))})
        import_chat_usage(conn)
        import_chat_usage(conn)
        historical = conn.execute('SELECT b.id,c.* FROM model_consumption_bindings b JOIN model_consumption c ON b.id=c.binding_id WHERE b.historical').fetchall()
        assert len(historical) == 2
        assert len({row['id'] for row in historical}) == 2
        assert all(row['occurred_at'] is None and row['amount'] is None and row['currency'] is None for row in historical)
        assert all(row['time_basis'] == 'unknown_historical' for row in historical)
        unchanged = conn.execute("SELECT runtime_context_json FROM task_runs WHERE conversation_id='old-a' AND id='same-run'").fetchone()
        assert json.loads(unchanged['runtime_context_json']) == old
        projected = service.messages(conn, 'old-a')
        assert len(projected) == 3
        recorded = next(item for item in projected if item['task_run_id'] == 'same-run')
        assert recorded['usage'] == old['usage']
        assert recorded['consumption']['tokens'] == 15
        assert recorded['consumption']['partial_responses'] == 1
        assert recorded['consumption']['amounts'] == []
        assert recorded['text'] == '原始提问' and recorded['answer'] == '原始回复'
        assert 'synthetic-only' not in json.dumps(recorded)
        assert all(item['usage'] == dict.fromkeys(FIELDS) for item in projected if item['task_run_id'] != 'same-run')
        assert message_consumption(conn, ['same-run'], conversation_id='old-b')['same-run']['tokens'] == 9
        assert message_consumption(conn, ['same-run'], conversation_id='new')['same-run']['tokens'] == 25
        unobserved = next(item for item in service.messages(conn, 'new') if item['task_run_id'] == 'already-bound')
        assert unobserved['unobserved_consumption_bindings'] == 1
        assert unobserved['consumption'] is None and unobserved['usage_missing_responses'] is None
        assert unobserved['usage'] == dict.fromkeys(FIELDS)
        summary = overview_consumption(conn, configured)
        assert summary['unobserved_bindings'] == 1
        assert summary['total']['tokens'] == 49
        assert summary['undated']['tokens'] == 24
        assert summary['undated']['responses'] == 2
        old_revision = conn.execute("SELECT revision FROM agent_messages WHERE conversation_id='old-a' AND task_run_id='same-run'").fetchone()['revision']
        save_response(conn, 'receipt', {'response_seq': 0, 'revision': 2, 'measurement': 'raw',
            'occurred_at': '2026-11-04T00:00:00Z', 'usage': dict(zip(FIELDS, (20, 10, 0, 0)))})
        assert conn.execute("SELECT revision FROM agent_messages WHERE conversation_id='old-a' AND task_run_id='same-run'").fetchone()['revision'] == old_revision
        assert service.messages(conn, 'new')[0]['usage']['output_tokens'] == 10


def test_new_chat_finish_uses_receipts_and_never_reintroduces_stdout_usage(owner):
    from cairn.server.services.consumption import register_binding
    conversation, _, machine, claim = new_turn(owner)
    identifier = str(uuid4())
    with db.get_conn() as conn:
        revision = conn.execute('SELECT revision FROM agent_messages WHERE conversation_id=? AND task_run_id=?',
                                (conversation, claim['task_run_id'])).fetchone()['revision']
        register_binding(conn, identifier, principal=machine, task_run_id=claim['task_run_id'], conversation_id=conversation)
        pending, = service.messages(conn, conversation, revision=revision)
        assert pending['revision'] > revision
        assert pending['unobserved_consumption_bindings'] == 1 and pending['consumption'] is None
        assert pending['usage_missing_responses'] is None and pending['usage'] == dict.fromkeys(FIELDS)
        save_response(conn, identifier, {'response_seq': 0, 'revision': 1, 'measurement': 'raw',
            'usage': dict(zip(FIELDS, (12, 4, 0, 0)))})
        observed, = service.messages(conn, conversation, revision=pending['revision'])
        assert observed['revision'] > pending['revision']
        assert observed['unobserved_consumption_bindings'] == 0
        assert observed['usage'] == dict(zip(FIELDS, (12, 4, 0, 0)))
        body = finish_body(claim, usage={'input_tokens': 999999})
        result = service.finish_turn(conn, conversation, claim['message_id'], body, machine)
        assert result['usage'] == dict(zip(FIELDS, (12, 4, 0, 0)))
        context = json.loads(conn.execute('SELECT runtime_context_json FROM task_runs WHERE conversation_id=? AND id=?',
            (conversation, claim['task_run_id'])).fetchone()['runtime_context_json'])
        assert context['consumption_source'] == 'receipts-v1'
        assert 'usage' not in context and 'usage_missing_responses' not in context
        body.usage = {'input_tokens': 1}
        assert service.finish_turn(conn, conversation, claim['message_id'], body, machine)['consumption'] == result['consumption']


def test_chat_finish_and_message_reads_survive_consumption_query_failure(owner, monkeypatch):
    from cairn.server.services import consumption
    conversation, _, machine, claim = new_turn(owner)
    original = consumption.message_consumption
    def broken_query(conn, *args, **kwargs):
        return conn.execute('SELECT 1 / 0').fetchone()
    monkeypatch.setattr(consumption, 'message_consumption', broken_query)
    with db.get_conn() as conn:
        body = finish_body(claim)
        result = service.finish_turn(conn, conversation, claim['message_id'], body, machine)
        assert result['status'] == 'succeeded' and result['answer'] == body.text
        assert result['consumption_error'] is True and result['consumption'] is None
        assert result['unobserved_consumption_bindings'] is None
        assert result['usage_missing_responses'] is None and result['usage'] == dict.fromkeys(FIELDS)
        assert conn.execute('SELECT 1 AS n').fetchone()['n'] == 1
    with db.get_conn() as conn:
        row, = service.messages(conn, conversation)
        assert row['status'] == 'succeeded' and row['consumption_error'] is True
        assert row['unobserved_consumption_bindings'] is None
        assert conn.execute('SELECT 1 AS n').fetchone()['n'] == 1
    monkeypatch.setattr(consumption, 'message_consumption', original)
    with db.get_conn() as conn:
        row, = service.messages(conn, conversation)
        assert row['status'] == 'succeeded' and row['consumption_error'] is False
        assert row['unobserved_consumption_bindings'] == 0


def test_chat_finish_bounds_consumption_table_lock_and_restores_timeout(owner):
    import time
    conversation, _, machine, claim = new_turn(owner)
    with db.get_conn() as blocker:
        blocker.execute('LOCK TABLE model_consumption IN ACCESS EXCLUSIVE MODE')
        with db.get_conn() as conn:
            original = conn.execute('SHOW statement_timeout').fetchone()['statement_timeout']
            started = time.monotonic()
            result = service.finish_turn(conn, conversation, claim['message_id'], finish_body(claim), machine)
            assert time.monotonic() - started < 8
            assert result['status'] == 'succeeded' and result['consumption_error'] is True
            assert conn.execute('SHOW statement_timeout').fetchone()['statement_timeout'] == original
    with db.get_conn() as conn:
        row, = service.messages(conn, conversation)
        assert row['status'] == 'succeeded' and row['consumption_error'] is False
