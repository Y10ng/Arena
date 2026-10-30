"""只用合成 spool 与私有 PostgreSQL 验证同步，不访问目标或部署库。"""

from collections import defaultdict, deque
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import time

import pytest

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime import traffic_sync
from cairn.server.schemas.traffic import TRAFFIC_CHUNK_BYTES
from cairn.server.services import traffic
from traffic_api_fixture import traffic_api, postgres_instance, empty_postgres_url  # noqa: F401
from test_traffic_query import _record


def _spool(capture, binding, count, *, size=384, sealed=True, start=0):
    capture.mkdir(parents=True, exist_ok=True)
    (capture / f"{binding['id']}.json").write_text(json.dumps(binding))
    paths = []
    for number in range(start, start + count):
        flow_id = f"{binding['id']}-flow-{number:05d}"
        path = capture / binding['id'] / 'flows' / flow_id / 'flow.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        record = _record(binding, flow_id)
        for side in ('request', 'response'):
            raw = (b'Q' if side == 'request' else b'S') * size
            (path.parent / f'{side}.body').write_bytes(raw)
            record[side].update(body_bytes=size, body_sealed=sealed, body_complete=sealed,
                                body_revision=1 if sealed else None)
        if sealed:
            record.update(state='complete', finished_at=2.0)
        path.write_text(json.dumps(record))
        paths.append(path)
    return paths


def _wait(check, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError('synthetic traffic sync timed out')


@pytest.mark.skipif(os.environ.get('CAIRN_TEST_POSTGRES_BOOTSTRAP') != '1', reason='requires disposable PostgreSQL opt-in')
def test_synthetic_scope_upload_costs(traffic_api, tmp_path, monkeypatch):
    api = traffic_api
    capture, receipts = tmp_path / 'capture', tmp_path / 'receipts'
    scopes = []
    for name, run, count in zip(('a-large', 'z-small'), api.task_runs, (32, 2)):
        binding = dict(id=name, project_id=api.project_id, task_run_id=run['id'],
                       generation=run['execution_generation'], container_id='synthetic-container')
        scopes.append(binding)
        _spool(capture, binding, count)
    timings = defaultdict(float)
    calls = defaultdict(int)

    def instrument(owner, name, label):
        original = getattr(owner, name)
        def measured(*args, **kwargs):
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                timings[label] += time.perf_counter() - started
                calls[label] += 1
        monkeypatch.setattr(owner, name, measured)

    for name, label in (('sync_traffic_flow', 'metadata_http'), ('sync_traffic_body', 'chunk_http'),
                        ('seal_traffic_body', 'seal_http'), ('register_traffic_scope', 'scope_http')):
        instrument(api.machine, name, label)
    if hasattr(api.machine, 'sync_traffic_batch'):
        instrument(api.machine, 'sync_traffic_batch', 'batch_http')
    instrument(traffic_sync, '_save_receipt', 'receipt_fsync')
    instrument(traffic, '_lock_project', 'project_lock')
    instrument(traffic, '_accept_record', 'metadata_server')
    instrument(traffic, '_accept_chunk', 'chunk_server')
    instrument(traffic, '_seal_body', 'seal_server')
    instrument(traffic, '_fsync_directory', 'server_directory_fsync')
    original_conn = traffic.get_conn
    @contextmanager
    def measured_conn():
        start = time.perf_counter()
        with original_conn() as conn:
            try:
                yield conn
            finally:
                timings['transaction_inside'] += time.perf_counter() - start
        timings['transaction_total'] += time.perf_counter() - start
        calls['transaction_total'] += 1
    monkeypatch.setattr(traffic, 'get_conn', measured_conn)
    sync = traffic_sync.TrafficSync(api.machine, capture=capture, receipts=receipts, interval=0.01)
    sync._discover_scopes()
    # 固定大 scope 先取得服务，不能依赖文件系统恰好先返回小 scope 来证明公平。
    sync._scope_queue = deque(sorted(sync._scope_queue))
    started = time.perf_counter()
    sync.start()
    try:
        _wait(lambda: (receipts / 'z-small-z-small-flow-00001.json').exists() and
              json.loads((receipts / 'z-small-z-small-flow-00001.json').read_text()).get('bodies', {}).get('response', {}).get('artifact_id'))
        small_seconds = time.perf_counter() - started
        large_at_small = len([p for p in receipts.glob('a-large-*.json')
                              if json.loads(p.read_text()).get('bodies', {}).get('response', {}).get('artifact_id')])
        assert 0 < large_at_small < 32
        _wait(lambda: len(sync.status()['scopes']) == 2 and all(s['upload_complete'] for s in sync.status()['scopes'].values()))
        elapsed = time.perf_counter() - started
        assert sync.status()['pending'] == sync.status()['pending_bytes'] == 0
        rows = api.conn.execute('SELECT state FROM http_flows WHERE project_id = ?', (api.project_id,)).fetchall()
        assert len(rows) == 34 and all(row['state'] == 'complete' for row in rows)
        print(json.dumps({'flows': 34, 'body_bytes': 34 * 384 * 2, 'elapsed_seconds': round(elapsed, 4),
                          'small_scope_seconds': round(small_seconds, 4), 'large_complete_when_small': large_at_small,
                          'calls': dict(calls), 'seconds': {k: round(v, 4) for k, v in timings.items()}}, sort_keys=True))
    finally:
        sync.stop(timeout=15)
    _exercise_resume(api, tmp_path, scopes[0], monkeypatch)


def _exercise_resume(api, tmp_path, original_binding, monkeypatch):
    binding = {**original_binding, 'id': 'resume'}
    capture, receipts = tmp_path / 'resume-capture', tmp_path / 'resume-receipts'
    paths = _spool(capture, binding, 2, size=TRAFFIC_CHUNK_BYTES + 17, sealed=False)
    first_record = json.loads(paths[0].read_text())
    receipts.mkdir()
    sync = traffic_sync.TrafficSync(api.machine, capture=capture, receipts=receipts, interval=0.01)
    assert sync.status()['pending'] is None and sync.status()['pending_bytes'] is None
    assert sync._step()
    sync._publish_inventory()
    assert sync.status()['scopes']['resume']['pending'] == 2
    assert sync.status()['pending_bytes'] == 4 * (TRAFFIC_CHUNK_BYTES + 17)
    upload = api.machine.sync_traffic_body
    lost_chunks = []
    def lose_chunk(*args, **kwargs):
        result = upload(*args, **kwargs)
        if not lost_chunks:
            assert result.ok
            lost_chunks.append((args, kwargs['offset'], kwargs['size'], kwargs['sha256']))
            return ApiResult(status_code=503, error_code='synthetic_receipt_loss')
        return result
    monkeypatch.setattr(api.machine, 'sync_traffic_body', lose_chunk)
    sync._step()
    sync._step()
    # 一个未完成大 flow 不得独占小于一个批次的 pending 队列。
    local = [json.loads((receipts / f"resume-{p.parent.name}.json").read_text()) for p in paths]
    assert all(item['bodies']['response']['accepted_bytes'] > 0 for item in local)
    assert any(item['bodies']['request'].get('pending') for item in local)
    sync.stop()
    sync = traffic_sync.TrafficSync(api.machine, capture=capture, receipts=receipts, interval=0.01)
    sync.start()
    try:
        _wait(lambda: sync.status()['pending_bytes'] == 0)
        assert sync.status()['pending'] == 2 and not sync.status()['scopes']['resume']['upload_complete']
        assert len(lost_chunks) == 1
        chunk_count = api.conn.execute('SELECT count(*) AS n FROM http_flow_body_chunks WHERE project_id = ? AND flow_id LIKE ?',
                                      (api.project_id, 'resume-%')).fetchone()['n']
        assert chunk_count == 8
        for path in paths:
            record = json.loads(path.read_text())
            record.update(revision=2, state='complete', finished_at=2.0)
            for side in ('request', 'response'):
                record[side].update(body_revision=2, body_sealed=True, body_complete=True)
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(record))
            temporary.replace(path)
        sync.request_reconcile('resume')
        assert sync.status()['pending'] is None
        _wait(lambda: sync.status()['scopes'].get('resume', {}).get('upload_complete'))
        assert sync.status()['pending'] == sync.status()['pending_bytes'] == 0
    finally:
        sync.stop(timeout=15)
    # 已确认 revision 重启后不重发；新 flow 在同一旧 scope 中仍能被核对发现。
    before = api.conn.execute('SELECT count(*) AS n FROM http_flow_receipts WHERE project_id = ? AND flow_id LIKE ?',
                              (api.project_id, 'resume-%')).fetchone()['n']
    assert before == 4
    batch_upload = api.machine.sync_traffic_batch
    lost_batches = []
    def lose_batch(*args, **kwargs):
        result = batch_upload(*args, **kwargs)
        if not lost_batches:
            assert result.ok
            lost_batches.append(result.data)
            return ApiResult(status_code=503, error_code='synthetic_batch_receipt_loss')
        return result
    monkeypatch.setattr(api.machine, 'sync_traffic_batch', lose_batch)
    sync = traffic_sync.TrafficSync(api.machine, capture=capture, receipts=receipts, interval=0.01)
    sync.reconcile_interval = 0.05
    sync.start()
    try:
        _wait(lambda: sync.status()['scopes'].get('resume', {}).get('upload_complete'))
        assert not lost_batches
        # 不显式通知新增，验证周期核对能发现迟到的目录。
        _spool(capture, binding, 1, size=23, start=2)
        _wait(lambda: lost_batches)
        _wait(lambda: sync.status()['errors'])
    finally:
        sync.stop(timeout=15)
    sync = traffic_sync.TrafficSync(api.machine, capture=capture, receipts=receipts, interval=0.01)
    sync.start()
    try:
        _wait(lambda: sync.status()['scopes'].get('resume', {}).get('upload_complete'))
        loaded = []
        load = sync._load_flow
        def tracked(*args):
            loaded.append(args[1])
            return load(*args)
        monkeypatch.setattr(sync, '_load_flow', tracked)
        sync.request_reconcile('resume')
        _wait(lambda: sync.status()['scopes'].get('resume', {}).get('upload_complete'))
        assert not loaded, 'unchanged acknowledged flows must stay out of the hot scan'
    finally:
        sync.stop(timeout=15)
    bodies = api.conn.execute('SELECT flow_id, side, sha256, size FROM http_flow_bodies WHERE project_id = ? AND flow_id LIKE ?',
                             (api.project_id, 'resume-%')).fetchall()
    assert len(bodies) == 6
    for body in bodies:
        expected = (b'Q' if body['side'] == 'request' else b'S') * body['size']
        assert body['sha256'] == sha256(expected).hexdigest()
    assert api.conn.execute('SELECT count(*) AS n FROM http_flow_receipts WHERE project_id = ? AND flow_id LIKE ?',
                            (api.project_id, 'resume-%')).fetchone()['n'] == 5
    conflict = deepcopy(first_record)
    conflict['request']['host'] = 'conflict.invalid'
    valid = deepcopy(first_record)
    valid['id'] = 'resume-isolated-valid'
    result = batch_upload(api.project_id, 'resume', [{'record': conflict, 'bodies': {}}, {'record': valid, 'bodies': {}}])
    assert result.ok and [item['status_code'] for item in result.data['items']] == [409, 200]
    assert result.data['items'][0]['error_code'] == 'traffic_revision_conflict'
    assert api.conn.execute('SELECT state FROM http_flows WHERE project_id = ? AND id = ?',
                            (api.project_id, valid['id'])).fetchone()['state'] == 'receiving'
    status = dict(state='stopping', pending=None, pending_bytes=None, control_stopped=False,
                  collector_stopped=False, upload_complete=False)
    assert api.machine.update_traffic_scope_status(api.project_id, 'resume', status).ok
    context = json.loads(api.conn.execute('SELECT runtime_context_json FROM task_runs WHERE project_id = ? AND id = ?',
                                         (api.project_id, binding['task_run_id'])).fetchone()['runtime_context_json'])
    for generation, stopped in ((binding['generation'] + 1, False), (binding['generation'], True)):
        context['execution_lifecycle'] = dict(generation=generation, termination_confirmed=True)
        api.conn.execute('UPDATE task_runs SET runtime_context_json = ? WHERE project_id = ? AND id = ?',
                         (json.dumps(context), api.project_id, binding['task_run_id']))
        result = api.machine.list_traffic_scope_status(api.project_id)
        actual = next(item['status'] for item in result.data['items'] if item['id'] == 'resume')
        assert actual['control_stopped'] is stopped and actual['collector_stopped'] is False
        assert actual['pending'] is None and actual['pending_bytes'] is None and actual['upload_complete'] is False


def test_unknown_inventory_and_unsealed_body_cannot_complete(tmp_path):
    capture, receipts = tmp_path / 'capture', tmp_path / 'receipts'
    binding = dict(id='large', project_id='p', task_run_id='r', generation=1, container_id='synthetic')
    _spool(capture, binding, 129, size=1, sealed=False)
    receipts.mkdir()
    sync = traffic_sync.TrafficSync(None, capture=capture, receipts=receipts)
    sync._discover_scopes()
    scope = sync._scopes['large']
    scope['binding'] = binding
    sync._scan_scope('large', scope)
    sync._publish_inventory()
    assert sync.status()['pending'] is None and sync.status()['pending_bytes'] is None
    assert not sync.status()['scopes']['large']['upload_complete']
    sync._scan_scope('large', scope)
    sync._publish_inventory()
    assert sync.status()['pending'] == 129 and sync.status()['pending_bytes'] == 258
    record = _record(binding, 'f')
    record['state'] = 'partial'
    assert traffic_sync.TrafficSync._remaining(record, {'bodies': {}}) == (False, 0)
