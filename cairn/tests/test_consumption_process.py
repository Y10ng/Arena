"""真实消费 outbox 配合受控执行器，验证启动前持久化与失败留存。"""
import errno
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.pause_clock import ExecutionDetached
from cairn.dispatcher.tasks.consumption_outbox import ConsumptionOutbox
from cairn.dispatcher.tasks.process_runner import run_worker_process
from cairn.dispatcher.workers.base import DriverResult, WorkerInputFile
from test_worker_input_files import _ContainerManager, _worker


class DurableManager(_ContainerManager):
    def __init__(self, root):
        super().__init__()
        self.prompts = root / 'prompts'
        self.prompts.mkdir()
        self.outbox = ConsumptionOutbox(root / 'consumption')
        self.outbox.finish = Mock(wraps=self.outbox.finish)
        self.consumption_outbox = self.outbox
        self.deliver_consumption = Mock(wraps=self.deliver_consumption)
        self.retained = False

    def write_text_file(self, container_name, path, content, **kwargs):
        super().write_text_file(container_name, path, content, **kwargs)
        (self.prompts / Path(path).name).write_text(content)

    def remove_file(self, container_name, path, **kwargs):
        super().remove_file(container_name, path, **kwargs)
        (self.prompts / Path(path).name).unlink()

    def retain_consumption_spool(self, container_name, identifier, path):
        self.outbox.retain(identifier, self.prompts / Path(path).name)
        self.retained = True

    def deliver_consumption(self, identifier, client, *, completed):
        if completed:
            self.outbox.finish(identifier)
        self.outbox.flush(client)


def _invocation():
    identifier = str(uuid4())
    path = f'/tmp/cairn-prompts/pi-consumption-{identifier}.jsonl'
    return DriverResult(argv=['controlled-worker'], input_files=(WorkerInputFile(path=path, content=''),),
                        consumption_id=identifier, consumption_spool_path=path)


def _client(manager, invocation, *, valid=True):
    def register(identifier, **scope):
        return ApiResult(200, {'accepted': valid, 'id': identifier, **scope})
    return SimpleNamespace(register_consumption=Mock(side_effect=register),
                           submit_consumption=Mock(return_value=ApiResult(503)))


def _run(manager, invocation, client):
    return run_worker_process(manager, 'fixture-container', _worker(), invocation, phase='chat_turn',
                              timeout_seconds=3, client=client, task_run_id='same-run', conversation_id='c1')


def test_chat_driver_always_precreates_independent_consumption_spool():
    from cairn.dispatcher.workers.adapters.pi import PiDriver
    from test_pi_execution import worker
    invocation = PiDriver().build_execute(worker(), '受控请求', None, phase='chat_turn', project_id=None,
        conversation_id='c1', task_run_id='same-run', timeout_seconds=3, session_root='/home/kali/.cairn/pi',
        project_tools_socket_path='/tmp/cairn-prompts/pi-project-' + 'a' * 32 + '.sock',
        project_tools_workspace_path='/home/kali/workspace/chat-fixture', project_tools_allowed_tools=[])
    request_file, spool = invocation.input_files
    capabilities = json.loads(request_file.content)['capabilities']
    assert set(capabilities) == {'project_tools', 'consumption'}
    assert capabilities['consumption'] == {'protocol': 'cairn-consumption-v1', 'id': invocation.consumption_id,
                                          'spool_path': spool.path}
    assert spool.path == invocation.consumption_spool_path and spool.content == ''
    assert spool.mode == 0o600 and invocation.candidate_spool is None


def test_registration_failure_does_not_block_and_replays_after_restart(tmp_path):
    from cairn.server.services.consumption import receipt_digest
    manager, invocation = DurableManager(tmp_path), _invocation()
    client = _client(manager, invocation, valid=False)
    event = {'response_seq': 0, 'revision': 1, 'measurement': 'raw', 'usage': {'input_tokens': 11}}
    source = manager.prompts / Path(invocation.consumption_spool_path).name
    def start():
        client.register_consumption.assert_not_called()
        source.write_text(json.dumps(event) + '\n')
    manager.process.start = Mock(side_effect=start)
    result = _run(manager, invocation, client)
    assert result.returncode == 0
    result.cleanup()
    manager.outbox.finish.assert_called_once_with(invocation.consumption_id)
    pending = manager.outbox.root / f'{invocation.consumption_id}.jsonl'
    assert json.loads(pending.read_text()) == event
    assert pending.with_suffix('.closed').exists()
    client.register_consumption.assert_not_called()
    manager.outbox.flush(client)
    client.register_consumption.assert_called_once()
    client.submit_consumption.assert_not_called()
    client.register_consumption.side_effect = lambda identifier, **scope: ApiResult(200, {
        'accepted': True, 'id': identifier, **scope,
    })
    client.submit_consumption.side_effect = lambda identifier, body: ApiResult(200, {
        'accepted': True, 'id': identifier, 'response_seq': body['response_seq'],
        'revision': body['revision'], 'sha256': receipt_digest(body),
    })
    # 新实例模拟 Dispatcher 重启；仍使用原身份和原回执，不重新运行模型。
    restarted = ConsumptionOutbox(manager.outbox.root)
    restarted.flush(client)
    restarted.flush(client)
    assert pending.with_suffix('.delivered').exists()
    assert client.submit_consumption.call_count == 1
    assert manager.process.start.call_count == 1


def test_cross_filesystem_spool_is_preserved_and_promoted_after_exit(tmp_path, monkeypatch):
    manager, invocation = DurableManager(tmp_path), _invocation()
    client = _client(manager, invocation)
    def fail_link(*args, **kwargs):
        raise OSError(errno.EXDEV, 'fixture cross-device link')
    monkeypatch.setattr('cairn.dispatcher.tasks.consumption_outbox.os.link', fail_link)
    event = {'response_seq': 0, 'revision': 1, 'measurement': 'raw', 'usage': {'input_tokens': 9}}
    source = manager.prompts / Path(invocation.consumption_spool_path).name
    manager.process.start = Mock(side_effect=lambda: source.write_text(json.dumps(event) + '\n'))
    result = _run(manager, invocation, client)
    assert result.returncode == 0
    result.cleanup()
    pending = manager.outbox.root / f'{invocation.consumption_id}.jsonl'
    assert json.loads(pending.read_text()) == event
    assert not manager.outbox.has_external_source(invocation.consumption_id)
    assert not source.exists()
    client.register_consumption.assert_not_called()
    client.submit_consumption.assert_not_called()
    manager.outbox.flush(client)
    client.register_consumption.assert_called_once()
    client.submit_consumption.assert_called_once_with(invocation.consumption_id, event)


def test_spool_creation_failure_does_not_block_and_reports_audited_gap(tmp_path):
    from cairn.server.services.consumption import receipt_digest
    manager, invocation = DurableManager(tmp_path), _invocation()
    client = _client(manager, invocation)
    manager.write_text_file = Mock(side_effect=OSError('fixture spool unavailable'))
    receipt = {'response_seq': 0, 'revision': 3, 'measurement': 'sdk_normalized',
               'usage': {'input_tokens': 7}, 'missing_responses': 1}
    audit = {'protocol': 'cairn-agent-audit-v1', 'event': 'consumption_gap',
             'consumption_id': invocation.consumption_id, 'project_id': None,
             'task_run_id': 'same-run', 'phase': 'chat_turn', 'session': None, 'receipt': receipt}
    manager.process.result.stderr = json.dumps({**audit, 'task_run_id': 'other'}) + '\n' + json.dumps(audit) + '\n'
    client.submit_consumption.side_effect = lambda identifier, body: ApiResult(200, {
        'accepted': True, 'id': identifier, 'response_seq': body['response_seq'],
        'revision': body['revision'], 'sha256': receipt_digest(body),
    })
    result = _run(manager, invocation, client)
    assert result.returncode == 0
    result.cleanup()
    client.register_consumption.assert_not_called()
    client.submit_consumption.assert_not_called()
    manager.outbox.flush(client)
    client.submit_consumption.assert_called_once_with(invocation.consumption_id, receipt)
    assert (manager.outbox.root / f'{invocation.consumption_id}.gaps.delivered').exists()
    assert not (manager.outbox.root / f'{invocation.consumption_id}.delivered').exists()


def test_external_source_copy_failure_preserves_original_for_restart(tmp_path, monkeypatch):
    manager, invocation = DurableManager(tmp_path), _invocation()
    client = _client(manager, invocation)
    def fail_link(*args, **kwargs):
        raise OSError(errno.EXDEV, 'fixture cross-device link')
    monkeypatch.setattr('cairn.dispatcher.tasks.consumption_outbox.os.link', fail_link)
    promote = manager.outbox._promote_source
    def fail_copy(*args, **kwargs):
        raise OSError('fixture outbox full')
    monkeypatch.setattr(manager.outbox, '_promote_source', fail_copy)
    source = manager.prompts / Path(invocation.consumption_spool_path).name
    event = {'response_seq': 0, 'revision': 1, 'measurement': 'raw', 'usage': {'input_tokens': 9}}
    manager.process.start = Mock(side_effect=lambda: source.write_text(json.dumps(event) + '\n'))
    result = _run(manager, invocation, client)
    assert result.returncode == 0 and source.exists()
    assert manager.outbox.has_external_source(invocation.consumption_id)
    result.cleanup()
    monkeypatch.setattr(manager.outbox, '_promote_source', promote)
    restarted = ConsumptionOutbox(manager.outbox.root)
    restarted.flush(client)
    pending = restarted.root / f'{invocation.consumption_id}.jsonl'
    assert json.loads(pending.read_text()) == event
    assert not restarted.has_external_source(invocation.consumption_id)


@pytest.mark.parametrize('failure', ['business', 'communicate', 'detached'])
def test_failure_retains_known_receipts_without_false_terminal_or_model_retry(tmp_path, failure):
    manager, invocation = DurableManager(tmp_path), _invocation()
    client = _client(manager, invocation)
    source = manager.prompts / Path(invocation.consumption_spool_path).name
    event = {'response_seq': 0, 'revision': 1, 'measurement': 'sdk_normalized', 'usage': {'input_tokens': 11}}
    def start():
        assert client.register_consumption.call_count == 0
        source.write_text(json.dumps(event) + '\n')
    manager.process.start = Mock(side_effect=start)
    if failure == 'business':
        manager.process.result.returncode = 1
        result = _run(manager, invocation, client)
        assert result.returncode == 1
        result.cleanup()
    else:
        exception = ExecutionDetached() if failure == 'detached' else RuntimeError('fixture communicate failure')
        manager.process.execution_completed = failure != 'detached'
        manager.process.communicate = Mock(side_effect=exception)
        with pytest.raises(type(exception)):
            _run(manager, invocation, client)
    assert manager.process.start.call_count == 1
    assert manager.outbox.finish.call_count == (failure != 'detached')
    retained = manager.outbox.root / f'{invocation.consumption_id}.jsonl'
    assert json.loads(retained.read_text()) == event
    assert retained.with_suffix('.closed').exists() is (failure != 'detached')
    client.register_consumption.assert_not_called()
    manager.outbox.flush(client)
    client.submit_consumption.assert_called_once_with(invocation.consumption_id, event)


def test_unconfirmed_start_retains_append_path_until_physical_exit(tmp_path):
    manager, invocation = DurableManager(tmp_path), _invocation()
    client = _client(manager, invocation)
    source = manager.prompts / Path(invocation.consumption_spool_path).name
    manager.process.execution_completed = False
    manager.process.start = Mock(side_effect=RuntimeError('fixture lost exec_start ACK'))
    with pytest.raises(RuntimeError, match='lost exec_start ACK'):
        _run(manager, invocation, client)
    manager.outbox.finish.assert_not_called()
    client.register_consumption.assert_not_called()
    assert source.exists()
    event = {'response_seq': 0, 'revision': 1, 'measurement': 'raw', 'usage': {'output_tokens': 9}}
    # 原执行迟到写入仍经相同 inode 留存，不因启动调用抛错而失去路径。
    source.write_text(json.dumps(event) + '\n')
    retained = manager.outbox.root / f'{invocation.consumption_id}.jsonl'
    assert json.loads(retained.read_text()) == event
    assert not retained.with_suffix('.closed').exists()


def test_outbox_rotates_past_unclosed_empty_files_and_waits_for_complete_lines(tmp_path):
    from cairn.server.services.consumption import receipt_digest
    outbox = ConsumptionOutbox(tmp_path / 'outbox')
    first, second = str(uuid4()), str(uuid4())
    first, second = sorted((first, second))
    (outbox.root / f'{first}.jsonl').write_text('')
    pending = outbox.root / f'{second}.jsonl'
    event = {'response_seq': 0, 'revision': 1, 'measurement': 'raw', 'usage': {'input_tokens': 11}}
    pending.write_text(json.dumps(event))
    def ack(identifier, body):
        return ApiResult(200, {'accepted': True, 'id': identifier, 'response_seq': body['response_seq'],
                              'revision': body['revision'], 'sha256': receipt_digest(body)})
    client = SimpleNamespace(submit_consumption=Mock(side_effect=ack))
    outbox.flush(client, limit=1)
    outbox.flush(client, limit=1)
    client.submit_consumption.assert_not_called()
    with pending.open('a') as output:
        output.write('\n')
    outbox.finish(second)
    for _ in range(4):
        outbox.flush(client, limit=1)
    client.submit_consumption.assert_called_once_with(second, event)
    assert pending.with_suffix('.delivered').exists()
    assert (outbox.root / f'{first}.jsonl').exists()


def test_source_state_failure_preserves_process_result_and_original(tmp_path, monkeypatch):
    manager, invocation = DurableManager(tmp_path), _invocation()
    client = _client(manager, invocation)
    source = manager.prompts / Path(invocation.consumption_spool_path).name
    monkeypatch.setattr(manager.outbox, 'has_external_source', Mock(side_effect=OSError('fixture state failure')))
    result = _run(manager, invocation, client)
    assert result.returncode == 0 and source.exists()
    result.cleanup()


def test_terminal_start_error_keeps_external_source_until_delivery(tmp_path, monkeypatch):
    manager, invocation = DurableManager(tmp_path), _invocation()
    client = _client(manager, invocation)
    monkeypatch.setattr('cairn.dispatcher.tasks.consumption_outbox.os.link',
                        Mock(side_effect=OSError(errno.EXDEV, 'fixture cross-device link')))
    source = manager.prompts / Path(invocation.consumption_spool_path).name
    event = {'response_seq': 0, 'revision': 1, 'measurement': 'raw', 'usage': {'input_tokens': 9}}
    def failed_start():
        source.write_text(json.dumps(event) + '\n')
        raise RuntimeError('fixture start error after terminal')
    manager.process.start = Mock(side_effect=failed_start)
    with pytest.raises(RuntimeError, match='fixture start error after terminal'):
        _run(manager, invocation, client)
    assert source.exists()
    assert json.loads((manager.outbox.root / f'{invocation.consumption_id}.jsonl').read_text()) == event
    manager.outbox.finish.assert_called_once_with(invocation.consumption_id)
    client.register_consumption.assert_not_called()


def test_concurrent_terminal_and_cursor_saves_have_distinct_temporaries(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import os
    from threading import Barrier, local
    from cairn.dispatcher.tasks.consumption_outbox import _save_receipt

    barrier, state = Barrier(2), local()
    original_fsync = os.fsync
    def synchronized_fsync(descriptor):
        if not getattr(state, 'first_fsync', False):
            state.first_fsync = True
            barrier.wait(timeout=5)
        original_fsync(descriptor)
    monkeypatch.setattr('cairn.dispatcher.tasks.consumption_outbox.os.fsync', synchronized_fsync)
    identifier = str(uuid4())
    closed, cursor = tmp_path / f'{identifier}.closed', tmp_path / f'{identifier}.cursor'
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_save_receipt, closed, {'closed': True}),
                   pool.submit(_save_receipt, cursor, {'offset': 42})]
        for future in futures:
            future.result(timeout=10)
    assert json.loads(closed.read_text()) == {'closed': True}
    assert json.loads(cursor.read_text()) == {'offset': 42}


def test_outbox_time_budget_is_checked_between_receipts(tmp_path, monkeypatch):
    from cairn.server.services.consumption import receipt_digest
    outbox = ConsumptionOutbox(tmp_path / 'outbox')
    identifier = str(uuid4())
    event = {'response_seq': 0, 'revision': 1, 'measurement': 'raw', 'usage': {'input_tokens': 11}}
    (outbox.root / f'{identifier}.jsonl').write_text(json.dumps(event) + '\n' + json.dumps({**event, 'revision': 2}) + '\n')
    clock = [0]
    monkeypatch.setattr('cairn.dispatcher.tasks.consumption_outbox.time.monotonic', lambda: clock[0])
    def ack(identifier, body):
        clock[0] += 3
        return ApiResult(200, {'accepted': True, 'id': identifier, 'response_seq': body['response_seq'],
                              'revision': body['revision'], 'sha256': receipt_digest(body)})
    client = SimpleNamespace(submit_consumption=Mock(side_effect=ack))
    outbox.flush(client, limit=16)
    assert client.submit_consumption.call_count == 1


def test_slow_registration_does_not_delay_process_return(tmp_path):
    import threading

    manager, invocation = DurableManager(tmp_path), _invocation()
    client = _client(manager, invocation)
    release, finished = threading.Event(), threading.Event()
    outcome = []
    def blocked_registration(identifier, **scope):
        release.wait(5)
        return ApiResult(200, {'accepted': True, 'id': identifier, **scope})
    client.register_consumption.side_effect = blocked_registration
    def execute():
        try:
            outcome.append(_run(manager, invocation, client))
        except BaseException as exc:
            outcome.append(exc)
        finally:
            finished.set()
    thread = threading.Thread(target=execute, daemon=True)
    thread.start()
    try:
        assert finished.wait(1), '计量注册阻塞了真实业务结果'
        assert len(outcome) == 1 and outcome[0].returncode == 0
        manager.deliver_consumption.assert_not_called()
        client.register_consumption.assert_not_called()
        client.submit_consumption.assert_not_called()
        assert (manager.outbox.root / f'{invocation.consumption_id}.closed').exists()
        outcome[0].cleanup()
    finally:
        release.set()
        thread.join(timeout=2)


def test_cross_filesystem_copy_does_not_wait_for_hung_delivery_lock(tmp_path, monkeypatch):
    import threading
    from cairn.server.services.consumption import receipt_digest

    manager, invocation = DurableManager(tmp_path), _invocation()
    client = _client(manager, invocation)
    monkeypatch.setattr('cairn.dispatcher.tasks.consumption_outbox.os.link',
                        Mock(side_effect=OSError(errno.EXDEV, 'fixture cross-device link')))
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    source = manager.prompts / Path(invocation.consumption_spool_path).name
    event = {'response_seq': 0, 'revision': 1, 'measurement': 'raw', 'usage': {'input_tokens': 9}}
    def blocked_registration(identifier, **scope):
        entered.set()
        assert release.wait(5)
        return ApiResult(200, {'accepted': True, 'id': identifier, **scope})
    client.register_consumption.side_effect = blocked_registration
    client.submit_consumption.side_effect = lambda identifier, body: ApiResult(200, {
        'accepted': True, 'id': identifier, 'response_seq': body['response_seq'],
        'revision': body['revision'], 'sha256': receipt_digest(body),
    })
    recovery = threading.Thread(target=lambda: manager.outbox.flush(client), daemon=True)
    def start():
        source.write_text(json.dumps(event) + '\n')
        recovery.start()
        assert entered.wait(2)
    manager.process.start = Mock(side_effect=start)
    outcome = []
    def execute():
        try:
            outcome.append(_run(manager, invocation, client))
        except BaseException as exc:
            outcome.append(exc)
        finally:
            finished.set()
    worker = threading.Thread(target=execute, daemon=True)
    worker.start()
    try:
        assert entered.wait(2)
        assert finished.wait(1), '外部源的本地留存等待了投递锁'
        assert len(outcome) == 1 and outcome[0].returncode == 0
        pending = manager.outbox.root / f'{invocation.consumption_id}.jsonl'
        assert json.loads(pending.read_text()) == event
        assert not manager.outbox.has_external_source(invocation.consumption_id)
        assert not source.exists()
        manager.deliver_consumption.assert_not_called()
        outcome[0].cleanup()
    finally:
        release.set()
        worker.join(timeout=2)
        if recovery.ident is not None:
            recovery.join(timeout=2)
    assert not recovery.is_alive()
    ConsumptionOutbox(manager.outbox.root).flush(client)
    assert (manager.outbox.root / f'{invocation.consumption_id}.delivered').exists()
    client.submit_consumption.assert_called_once_with(invocation.consumption_id, event)


def test_concurrent_terminal_does_not_archive_unread_spool_or_gap(tmp_path, monkeypatch):
    from cairn.server.services.consumption import receipt_digest

    outbox = ConsumptionOutbox(tmp_path / 'outbox')
    identifier = str(uuid4())
    path = outbox.root / f'{identifier}.jsonl'
    first = {'response_seq': 0, 'revision': 1, 'measurement': 'raw', 'usage': {'input_tokens': 1}}
    later = {'response_seq': 1, 'revision': 1, 'measurement': 'raw', 'usage': {'input_tokens': 2}}
    gap = {'response_seq': 2, 'revision': 1, 'measurement': 'raw', 'missing_responses': 1}
    path.write_text(json.dumps(first) + '\n')
    client = SimpleNamespace(submit_consumption=Mock(side_effect=lambda identifier, body: ApiResult(200, {
        'accepted': True, 'id': identifier, 'response_seq': body['response_seq'],
        'revision': body['revision'], 'sha256': receipt_digest(body),
    })))
    original = outbox._deliver_lines
    def at_eof(*args):
        count, complete = original(*args)
        if complete and not path.with_suffix('.closed').exists():
            with path.open('a') as output:
                output.write(json.dumps(later) + '\n')
            (outbox.root / f'{identifier}.gaps.jsonl').write_text(json.dumps(gap) + '\n')
            outbox.finish(identifier)
        return count, complete
    monkeypatch.setattr(outbox, '_deliver_lines', at_eof)
    outbox.flush(client)
    assert path.exists() and path.with_suffix('.closed').exists()
    assert not path.with_suffix('.delivered').exists()
    outbox.flush(client)
    assert path.with_suffix('.delivered').exists()
    assert (outbox.root / f'{identifier}.gaps.delivered').exists()
    assert [call.args[1] for call in client.submit_consumption.call_args_list] == [first, gap, later]


def _recovery_loop(tmp_path, outbox, client):
    from cairn.dispatcher.scheduler.loop import DispatcherBootstrap, DispatcherLoop
    from test_pi_container_ownership import dispatch_config

    config = dispatch_config()
    client.base_url = config.server
    client.close = Mock()
    client.consume_project_schedules = Mock(return_value={'processed': 0, 'errors': 0})
    client.expire_paused_projects = Mock(return_value=0)
    client.list_projects = Mock(return_value=[])
    loop = DispatcherLoop(DispatcherBootstrap(config_path=tmp_path / 'fixture.json', hard_cap=8),
                          client=client, config=config)
    loop.container_manager = SimpleNamespace(
        consumption_outbox=outbox, traffic_execution=SimpleNamespace(recover=Mock()), close=Mock())
    loop._task_run_finish_futures = {}
    loop._settings_checked = loop._startup_healthchecks_checked = loop._startup_reconcile_checked = True
    for name in ('_ensure_config_loaded', 'run_startup_healthchecks', '_begin_project_decisions',
                 '_record_dispatcher_heartbeat', '_cancel_inactive_tasks', '_refresh_config', '_reap_futures',
                 '_flush_pending_report_finishes', '_flush_pending_task_run_finishes', '_reap_cleanup_futures',
                 '_initialize_reason_checkpoints', '_refresh_runtime_projects', '_queue_container_cleanups',
                 '_dispatch_available'):
        setattr(loop, name, Mock())
    loop._refresh_candidate_recovery = Mock(return_value=True)
    return loop


def _pending_receipt(outbox):
    identifier = str(uuid4())
    event = {'response_seq': 0, 'revision': 1, 'measurement': 'raw', 'usage': {'input_tokens': 17}}
    (outbox.root / f'{identifier}.jsonl').write_text(json.dumps(event) + '\n')
    outbox.prepare(identifier, task_run_id='same-run', project_id=None, conversation_id='c1')
    outbox.finish(identifier)
    return identifier, event


def _ack_client():
    from cairn.server.services.consumption import receipt_digest
    return SimpleNamespace(
        register_consumption=Mock(),
        submit_consumption=Mock(side_effect=lambda identifier, body: ApiResult(200, {
            'accepted': True, 'id': identifier, 'response_seq': body['response_seq'],
            'revision': body['revision'], 'sha256': receipt_digest(body),
        })),
    )


@pytest.mark.parametrize('blocked', ['registration', 'receipt'])
def test_hung_recovery_does_not_block_dispatch_or_accumulate_jobs(tmp_path, monkeypatch, blocked):
    import threading

    outbox = ConsumptionOutbox(tmp_path / 'outbox')
    identifier, event = _pending_receipt(outbox)
    client = _ack_client()
    entered, release, flushed = threading.Event(), threading.Event(), threading.Event()
    ack = client.submit_consumption.side_effect
    def blocked_registration(identifier, **scope):
        if blocked == 'registration':
            entered.set()
            assert release.wait(5)
        return ApiResult(200, {'accepted': True, 'id': identifier, **scope})
    def blocked_receipt(identifier, body):
        if blocked == 'receipt':
            entered.set()
            assert release.wait(5)
        return ack(identifier, body)
    client.register_consumption.side_effect = blocked_registration
    client.submit_consumption.side_effect = blocked_receipt
    flush = outbox.flush
    def tracked_flush(*args, **kwargs):
        try:
            return flush(*args, **kwargs)
        finally:
            flushed.set()
    outbox.flush = Mock(side_effect=tracked_flush)
    loop = _recovery_loop(tmp_path, outbox, client)
    monkeypatch.setattr('cairn.dispatcher.scheduler.loop.ExecutionControlObserver', Mock())
    close = loop.close
    loop.close = Mock()
    errors = []
    threads = []
    def run_once(done):
        try:
            loop.run(once=True)
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()
    try:
        first_done = threading.Event()
        threads.append(threading.Thread(target=run_once, args=(first_done,), daemon=True))
        threads[-1].start()
        assert entered.wait(2)
        assert first_done.wait(1), '慢注册占住了本轮调度'
        second_done = threading.Event()
        threads.append(threading.Thread(target=run_once, args=(second_done,), daemon=True))
        threads[-1].start()
        assert second_done.wait(1), '慢注册占住了后续派发'
        for _ in range(10):
            loop._schedule_consumption_recovery()
        assert not errors
        assert loop._dispatch_available.call_count == 2
        assert loop.container_manager.traffic_execution.recover.call_count == 2
        assert outbox.flush.call_count == client.register_consumption.call_count == 1
        assert client.submit_consumption.call_count == (blocked == 'receipt')
        release.set()
        assert flushed.wait(2)
        # 即使首轮的两秒预算已耗尽，重启后仍以原身份和精确 ACK 恢复。
        ConsumptionOutbox(outbox.root).flush(client)
        assert (outbox.root / f'{identifier}.delivered').exists()
        client.submit_consumption.assert_called_once_with(identifier, event)
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=2)
        loop.close = close
        loop.close()


@pytest.mark.parametrize('blocked', ['registration', 'receipt'])
def test_hung_recovery_shutdown_is_bounded_and_preserves_receipts(tmp_path, caplog, blocked):
    import threading

    outbox = ConsumptionOutbox(tmp_path / 'outbox')
    identifier, event = _pending_receipt(outbox)
    client = _ack_client()
    entered, release, flushed, closed = (threading.Event() for _ in range(4))
    ack = client.submit_consumption.side_effect
    def blocked_registration(identifier, **scope):
        if blocked == 'registration':
            entered.set()
            assert release.wait(5)
        return ApiResult(200, {'accepted': True, 'id': identifier, **scope})
    def blocked_receipt(identifier, body):
        if blocked == 'receipt':
            entered.set()
            assert release.wait(5)
        return ack(identifier, body)
    client.register_consumption.side_effect = blocked_registration
    client.submit_consumption.side_effect = blocked_receipt
    flush = outbox.flush
    def tracked_flush(*args, **kwargs):
        try:
            return flush(*args, **kwargs)
        finally:
            flushed.set()
    outbox.flush = Mock(side_effect=tracked_flush)
    loop = _recovery_loop(tmp_path, outbox, client)
    errors = []
    def close():
        try:
            loop.close()
        except BaseException as exc:
            errors.append(exc)
        finally:
            closed.set()
    closing = threading.Thread(target=close, daemon=True)
    try:
        loop._schedule_consumption_recovery()
        assert entered.wait(2)
        closing.start()
        assert closed.wait(1.5), '挂起的计量请求拖住了关闭'
        assert not errors
        assert 'consumption recovery still in flight during shutdown' in caplog.text
        loop._schedule_consumption_recovery()
        assert outbox.flush.call_count == 1
        assert client.submit_consumption.call_count == (blocked == 'receipt')
        release.set()
        assert flushed.wait(2)
        loop._consumption_thread.join(timeout=2)
        assert not loop._consumption_thread.is_alive()
        assert client.submit_consumption.call_count == (blocked == 'receipt')
        assert (outbox.root / f'{identifier}.jsonl').exists()
        ConsumptionOutbox(outbox.root).flush(client)
        assert (outbox.root / f'{identifier}.delivered').exists()
        assert client.register_consumption.call_count == 1
        client.submit_consumption.assert_called_once_with(identifier, event)
    finally:
        release.set()
        if closing.ident is not None:
            closing.join(timeout=2)
        if not closed.is_set():
            loop.close()


@pytest.mark.parametrize('action', ['reload', 'shutdown'])
@pytest.mark.parametrize('blocked', ['registration', 'receipt'])
def test_recovery_client_close_keeps_inflight_session_owned(tmp_path, monkeypatch, action, blocked):
    import threading

    import requests
    from requests.adapters import HTTPAdapter

    from cairn.dispatcher.protocol.client import CairnClient
    from cairn.server.services.consumption import receipt_digest

    monkeypatch.setenv('CAIRN_DISPATCHER_TOKEN', 'synthetic-test-only')
    outbox = ConsumptionOutbox(tmp_path / 'outbox')
    identifier, event = _pending_receipt(outbox)
    loop = _recovery_loop(tmp_path, outbox, _ack_client())
    client = CairnClient(loop.config.server)
    loop.client = client
    entered, release, flushed = (threading.Event() for _ in range(3))
    sessions, sent, completed, close_during_send = [], [], [], []
    session_factory = requests.Session

    def session():
        result = session_factory()
        result.trust_env = False
        close = result.close
        def tracked_close():
            close_during_send.append(entered.is_set() and not release.is_set())
            close()
        result.close = Mock(side_effect=tracked_close)
        sessions.append(result)
        return result

    def send(adapter, request, **_kwargs):
        body = json.loads(request.body)
        sent.append(request.method)
        if not entered.is_set() and request.method == ('PUT' if blocked == 'registration' else 'POST'):
            entered.set()
            assert release.wait(5)
        # 保留真实连接池创建/清理；在 transport 边界返回精确 ACK，不打开 socket。
        adapter.poolmanager.connection_from_url(request.url)
        ack = {'accepted': True, 'id': identifier}
        if request.method == 'PUT':
            ack.update(body)
        else:
            ack.update(response_seq=body['response_seq'], revision=body['revision'], sha256=receipt_digest(body))
        response = requests.Response()
        response.status_code = 200
        response.headers['content-type'] = 'application/json'
        response._content = json.dumps(ack).encode()
        completed.append(request.method)
        return response

    monkeypatch.setattr('cairn.dispatcher.protocol.client.requests.Session', session)
    monkeypatch.setattr(HTTPAdapter, 'send', send)
    flush = outbox.flush
    def tracked_flush(*args, **kwargs):
        try:
            return flush(*args, **kwargs)
        finally:
            flushed.set()
    outbox.flush = Mock(side_effect=tracked_flush)
    try:
        loop._schedule_consumption_recovery()
        assert entered.wait(2)
        if action == 'shutdown':
            loop.close()
            assert loop._consumption_thread.is_alive()
        else:
            # 等价地址热加载不能关闭恢复线程及业务任务仍持有的 client。
            loop.dispatcher_id = 'synthetic-dispatcher'
            loop.candidate_capture_outbox = object()
            loop.container_manager.update_config = Mock()
            loop.container_manager.configure_traffic_execution = Mock()
            config = loop.config.model_copy(update={
                'server': loop.config.server.replace('server.example.test', 'SERVER.EXAMPLE.TEST'),
            })
            loop._apply_config(config, force=False)
            assert loop.client is client
            assert loop.config.server == config.server
            with pytest.raises(RuntimeError, match='identity changed'):
                loop._apply_config(config.model_copy(update={'server': 'https://other.invalid/api'}), force=False)
            assert loop.client is client
        sessions[0].close.assert_not_called()
        release.set()
        assert flushed.wait(2)
        loop.close()
        assert not loop._consumption_thread.is_alive()
        original_session = sessions[0]
        observation = {
            'closed_during_send': any(close_during_send),
            'remaining_pools': sum(len(adapter.poolmanager.pools) for adapter in set(original_session.adapters.values())),
            'requests': sent,
            'registered': (outbox.root / f'{identifier}.registered.json').exists(),
        }
        assert observation == {
            'closed_during_send': False, 'remaining_pools': 0,
            'requests': ['PUT'] if action == 'shutdown' and blocked == 'registration' else ['PUT', 'POST'],
            'registered': True,
        }, observation
        assert client._sessions == {}
        original_session.close.assert_called_once()
        # 已完成的精确 ACK 保留；未投递部分由新 client 接续，不重复发送已确认回执。
        replacement = CairnClient(loop.config.server)
        try:
            ConsumptionOutbox(outbox.root).flush(replacement)
            assert (outbox.root / f'{identifier}.delivered').exists()
            assert completed == ['PUT', 'POST']
            assert json.loads((outbox.root / f'{identifier}.delivered').read_text()) == event
        finally:
            replacement.close()
    finally:
        release.set()
        loop.close()
        if loop._consumption_thread is not None:
            loop._consumption_thread.join(timeout=2)
        client.close()
        for item in sessions:
            item.close()


def test_recovery_exception_is_logged_and_next_cycle_can_retry(tmp_path, caplog):
    import threading
    import time

    failed, retried = threading.Event(), threading.Event()
    def flush(*_args, **_kwargs):
        if not failed.is_set():
            failed.set()
            raise RuntimeError('fixture-private-message')
        retried.set()
    outbox = SimpleNamespace(flush=Mock(side_effect=flush))
    loop = _recovery_loop(tmp_path, outbox, _ack_client())
    try:
        loop._schedule_consumption_recovery()
        assert failed.wait(2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with loop._consumption_lock:
                if loop._consumption_job is None:
                    break
            threading.Event().wait(.005)
        else:
            pytest.fail('异常任务未释放恢复槽位')
        loop._schedule_consumption_recovery()
        assert retried.wait(2)
        assert outbox.flush.call_count == 2
        assert 'consumption recovery failed error=RuntimeError' in caplog.text
        assert 'fixture-private-message' not in caplog.text
    finally:
        loop.close()
