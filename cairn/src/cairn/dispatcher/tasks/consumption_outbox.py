"""消费 spool 复用持久回执与调度恢复；不依赖成功 stdout 或业务对象存活。"""
from __future__ import annotations

import fcntl
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time
from uuid import UUID

LOG = logging.getLogger(__name__)


def _stopping(stop) -> bool:
    return stop is not None and stop.is_set()


def _save_receipt(path: Path, receipt: dict) -> None:
    # 同一执行的终态和交付游标可并发写入；临时文件不能按相同 stem 复用。
    descriptor, temporary = tempfile.mkstemp(prefix=f'.{path.name}-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as output:
            json.dump(receipt, output, separators=(',', ':'))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


class ConsumptionOutbox:
    def __init__(self, root: Path):
        self.root = root
        self._last_attempted = ''
        root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def prepare(self, identifier: str, *, task_run_id: str, project_id: str | None,
                conversation_id: str | None) -> None:
        if str(UUID(identifier)) != identifier:
            raise ValueError('consumption_identity_invalid')
        # 只持久化 Server 验证所需的执行身份；租户与价格仍由 Server 决定。
        _save_receipt(self.root / f'{identifier}.binding.json', {
            'task_run_id': task_run_id, 'project_id': project_id, 'conversation_id': conversation_id,
        })

    def retain(self, identifier: str, source: Path) -> None:
        if str(UUID(identifier)) != identifier:
            raise ValueError('consumption_identity_invalid')
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size != 0:
                raise ValueError('consumption_spool_invalid')
            target = self.root / f'{identifier}.jsonl'
            try:
                # 首选同 inode 的独立链接，项目目录删除后仍可交付。
                os.link(source, target, follow_symlinks=False)
            except FileExistsError:
                raise
            except OSError:
                # 跨文件系统等无法建链接时保留原路径与 inode；终态后复制到 outbox。
                _save_receipt(self.root / f'{identifier}.source.json', {
                    'path': str(source), 'dev': before.st_dev, 'ino': before.st_ino,
                })
                LOG.warning('consumption spool uses source until completion id=%s', identifier)
                return
            after = target.lstat()
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ValueError('consumption_spool_replaced')
            os.fsync(descriptor)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            os.close(descriptor)

    def has_external_source(self, identifier: str) -> bool:
        return (self.root / f'{identifier}.source.json').exists()

    def capture_gaps(self, identifier: str, stream, *, project_id, task_run_id, phase, session) -> None:
        target = self.root / f'{identifier}.gaps.jsonl'
        if target.exists() or target.with_suffix('.delivered').exists():
            return
        descriptor, temporary = tempfile.mkstemp(prefix=f'.{identifier}-gaps-', dir=self.root)
        count = 0
        try:
            with os.fdopen(descriptor, 'wb') as output:
                while line := stream.readline(16385):
                    if len(line) > 16384:
                        while line and not line.endswith(b'\n'):
                            line = stream.readline(16385)
                        continue
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if not isinstance(event, dict) or any(event.get(key) != value for key, value in (
                        ('protocol', 'cairn-agent-audit-v1'), ('event', 'consumption_gap'),
                        ('consumption_id', identifier), ('project_id', project_id),
                        ('task_run_id', task_run_id), ('phase', phase), ('session', session),
                    )):
                        continue
                    receipt = event.get('receipt')
                    if not isinstance(receipt, dict) or receipt.get('missing_responses') != 1:
                        continue
                    output.write(json.dumps(receipt, separators=(',', ':')).encode() + b'\n')
                    count += 1
                if count:
                    output.flush()
                    os.fsync(output.fileno())
            if count:
                os.replace(temporary, target)
                directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _source(self, path: Path) -> tuple[int, Path | None]:
        if path.exists():
            return os.open(path, os.O_RDONLY | os.O_NOFOLLOW), None
        source_marker = self.root / f'{path.stem}.source.json'
        source = json.loads(source_marker.read_text())
        descriptor = os.open(source['path'], os.O_RDONLY | os.O_NOFOLLOW)
        actual = os.fstat(descriptor)
        if not stat.S_ISREG(actual.st_mode) or (actual.st_dev, actual.st_ino) != (source['dev'], source['ino']):
            os.close(descriptor)
            raise ValueError('consumption_spool_replaced')
        return descriptor, source_marker

    def _promote_source(self, path: Path, descriptor: int, marker: Path) -> int:
        # 只有物理终态才能复制可增长的源；复制失败保留源路径供下轮继续恢复。
        temporary = None
        try:
            with os.fdopen(descriptor, 'rb') as source:
                target_fd, temporary = tempfile.mkstemp(prefix=f'.{path.stem}-', dir=self.root)
                with os.fdopen(target_fd, 'wb') as target:
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
            os.replace(temporary, path)
            temporary = None
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            marker.unlink()
            return os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def finish(self, identifier: str) -> None:
        # 物理终态后先尽力复制跨文件系统的源；不能等待可能挂在网络投递中的文件锁。
        path = self.root / f'{identifier}.jsonl'
        if not path.with_suffix('.closed').exists():
            try:
                if self.has_external_source(identifier):
                    descriptor, marker = self._source(path)
                    if marker is not None:
                        descriptor = self._promote_source(path, descriptor, marker)
                    os.close(descriptor)
            except Exception as exc:
                LOG.warning('consumption source copy pending id=%s error=%s', identifier, type(exc).__name__)
        # 崩溃/脱离时不可标记；复制失败仍保留源路径，由恢复周期继续尝试。
        _save_receipt(path.with_suffix('.closed'), {'closed': True})

    def flush(self, client, *, limit: int = 128, stop=None) -> None:
        remaining = limit
        deadline = time.monotonic() + 2
        paths = {path for path in self.root.glob('*.jsonl') if not path.name.endswith('.gaps.jsonl')}
        paths.update(self.root / (path.name.removesuffix('.binding.json') + '.jsonl')
                     for path in self.root.glob('*.binding.json')
                     if not (self.root / (path.name.removesuffix('.binding.json') + '.delivered')).exists())
        paths = sorted(paths)
        # 未确认终态的空 spool 会长期保留，轮转扫描避免它们占满每轮名额。
        paths = [path for path in paths if path.name > self._last_attempted] + [path for path in paths if path.name <= self._last_attempted]
        for index, path in enumerate(paths):
            if remaining <= 0 or index >= limit or time.monotonic() >= deadline or _stopping(stop):
                break
            self._last_attempted = path.name
            try:
                remaining -= max(1, self._flush_file(client, path, remaining, deadline, stop))
            except Exception as exc:
                # 失败可发现且留存，不能用消费交付故障重跑业务模型。
                LOG.warning('consumption delivery pending id=%s error=%s', path.stem, type(exc).__name__)

    def _flush_file(self, client, path: Path, limit: int, deadline: float, stop) -> int:
        lock_fd = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            if _stopping(stop):
                return 0
            binding_path = self.root / f'{path.stem}.binding.json'
            registered_path = self.root / f'{path.stem}.registered.json'
            if binding_path.exists() and not registered_path.exists():
                binding = json.loads(binding_path.read_text())
                if _stopping(stop):
                    return 0
                result = client.register_consumption(path.stem, **binding)
                expected = {'accepted': True, 'id': path.stem, **binding}
                if not result.ok or result.data != expected:
                    raise ValueError('consumption_registration_ack_mismatch')
                _save_receipt(registered_path, {'accepted': True})
            if _stopping(stop):
                return 0
            # 读取前确认终态，避免 EOF 与迟到写入/缺口审计并发时提前归档。
            closed = path.with_suffix('.closed').exists()
            count = 0
            gaps = self.root / f'{path.stem}.gaps.jsonl'
            if gaps.exists():
                descriptor = os.open(gaps, os.O_RDONLY | os.O_NOFOLLOW)
                delivered, complete = self._deliver_lines(
                    client, path.stem, descriptor, gaps.with_suffix('.cursor'), limit, deadline, stop)
                count += delivered
                if complete:
                    gaps.replace(gaps.with_suffix('.delivered'))
                if not complete or count >= limit or time.monotonic() >= deadline or _stopping(stop):
                    return count
            if _stopping(stop):
                return count
            descriptor, external_marker = self._source(path)
            if external_marker is not None and closed:
                descriptor = self._promote_source(path, descriptor, external_marker)
            delivered, complete = self._deliver_lines(
                client, path.stem, descriptor, path.with_suffix('.cursor'), limit - count, deadline, stop)
            count += delivered
            if complete and closed:
                # 仅归档完整且全部精确 ACK 的终态文件，不删原回执材料。
                path.replace(path.with_suffix('.delivered'))
            return count
        finally:
            os.close(lock_fd)

    def _deliver_lines(self, client, identifier: str, descriptor: int, cursor_path: Path,
                       limit: int, deadline: float, stop) -> tuple[int, bool]:
        cursor = json.loads(cursor_path.read_text())['offset'] if cursor_path.exists() else 0
        count = 0
        with os.fdopen(descriptor, 'rb') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError('consumption_spool_invalid')
            source.seek(cursor)
            while count < limit and time.monotonic() < deadline and not _stopping(stop):
                line = source.readline(16385)
                if not line:
                    return count, True
                if len(line) > 16384:
                    raise ValueError('consumption_receipt_too_large')
                if not line.endswith(b'\n'):
                    return count, False
                event = json.loads(line)
                digest = sha256(json.dumps(event, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                if _stopping(stop):
                    return count, False
                result = client.submit_consumption(identifier, event)
                expected = {'accepted': True, 'id': identifier, 'response_seq': event['response_seq'],
                            'revision': event['revision'], 'sha256': digest}
                if not result.ok or result.data != expected:
                    raise ValueError('consumption_ack_mismatch')
                _save_receipt(cursor_path, {'offset': source.tell()})
                count += 1
        return count, False
