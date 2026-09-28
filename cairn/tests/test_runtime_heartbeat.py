from __future__ import annotations

from collections.abc import Iterator

import pytest

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime import heartbeat as heartbeat_module
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease


class FakeStopEvent:
    def __init__(self, waits: Iterator[bool]) -> None:
        self._waits = waits

    def wait(self, _interval: int) -> bool:
        return next(self._waits)


class FakeProcess:
    def __init__(self) -> None:
        self.kill_calls = 0
        self.cancel_reasons = []

    def kill(self) -> None:
        self.kill_calls += 1

    def cancel(self, reason) -> None:
        self.cancel_reasons.append(reason)


def test_transient_timeout_does_not_abandon_recoverable_lease(monkeypatch) -> None:
    results = iter(
        [
            ApiResult(status_code=0, text="read timeout"),
            ApiResult(status_code=200),
        ]
    )
    monotonic_values = iter([0.0, 20.0, 21.0])
    monkeypatch.setattr(heartbeat_module.time, "monotonic", lambda: next(monotonic_values))
    lease = HeartbeatLease(
        heartbeat=lambda: next(results),
        scope="project=<REDACTED> intent=<REDACTED>",
        worker_name="worker-1",
        interval=3,
    )
    lease._stop = FakeStopEvent(iter([False, False, True]))
    process = FakeProcess()
    lease.attach_process(process)

    lease._run()

    assert lease.failure is None
    assert process.kill_calls == 0


@pytest.mark.parametrize("status_code", (403, 409))
@pytest.mark.parametrize("reject_before_attach", [False, True])
def test_authoritative_lease_rejection_cancels_attached_or_late_process(status_code: int, reject_before_attach) -> None:
    lease = HeartbeatLease(
        heartbeat=lambda: ApiResult(status_code=status_code, text="lease rejected"),
        scope="project=<REDACTED> intent=<REDACTED>",
        worker_name="worker-1",
        interval=3,
    )
    lease._stop = FakeStopEvent(iter([False]))
    process = FakeProcess()
    if not reject_before_attach:
        lease.attach_process(process)
    lease._run()
    if reject_before_attach:
        lease.attach_process(process)

    assert lease.failure is not None
    assert lease.failure.status_code == status_code
    assert process.cancel_reasons == ["heartbeat_lost"]
    assert process.kill_calls == 0
