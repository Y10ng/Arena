from __future__ import annotations

import pytest

from types import SimpleNamespace

from cairn.dispatcher.runtime.browser_sessions import BrowserSessions, notify_browser_task


def test_task_session_survives_phases_until_local_completion_and_confirmed_finish():
    sessions = BrowserSessions()
    first = sessions.prepare("proj_a", "run_1", "container_a", env={}, context={"uid": 1000})
    assert first is not None
    sessions.register_exec(first, ("container_a", "exec_explore"))

    conclude = sessions.prepare("proj_a", "run_1", "container_a", env={}, context={"uid": 1000})
    assert conclude == first
    sessions.register_exec(conclude, ("container_a", "exec_conclude"))
    assert sessions.claim_ready() == []

    sessions.local_finished("proj_a", "run_1")
    assert sessions.claim_ready() == []
    sessions.terminal_confirmed("proj_a", "run_1")
    assert sessions.claim_ready() == [first]
    assert sessions.execution_ids(first) == ("exec_explore", "exec_conclude")
    assert sessions.claim_ready() == []


def test_late_cleanup_cannot_close_another_execution_or_reopen_finished_session():
    sessions = BrowserSessions()
    old = sessions.prepare("proj_a", "run_1", "container_a", env={}, context={})
    current = sessions.prepare("proj_a", "run_2", "container_a", env={}, context={})
    other = sessions.prepare("proj_b", "run_1", "container_b", env={}, context={})
    assert old is not None and current is not None and other is not None
    assert len({old.name, current.name, other.name}) == 3
    sessions.local_finished("proj_a", "run_1")
    sessions.terminal_confirmed("proj_a", "run_1")
    assert sessions.claim_ready() == [old]

    sessions.finish_cleanup(old, "closed")
    sessions.finish_cleanup(old, "closed")
    assert sessions.prepare("proj_a", "run_1", "container_a", env={}, context={}) is None
    assert sessions.prepare("proj_a", "run_2", "container_a", env={}, context={}) == current
    assert sessions.prepare("proj_b", "run_1", "container_b", env={}, context={}) == other
    assert sessions.claim_ready() == []


@pytest.mark.parametrize("identity", [None, ("replacement_container", "exec_1")])
def test_uncertain_exec_identity_never_authorizes_browser_cleanup(identity):
    sessions = BrowserSessions()
    session = sessions.prepare("proj_a", "run_1", "container_a", env={}, context={})
    assert session is not None
    sessions.register_exec(session, identity)
    sessions.local_finished("proj_a", "run_1")
    sessions.terminal_confirmed("proj_a", "run_1")
    assert sessions.claim_ready() == [session]
    assert sessions.execution_ids(session) is None


def test_cleanup_retries_are_delayed_and_bounded():
    now = [10.0]
    sessions = BrowserSessions(clock=lambda: now[0])
    session = sessions.prepare("proj_a", "run_1", "container_a", env={}, context={})
    assert session is not None
    sessions.local_finished("proj_a", "run_1")
    sessions.terminal_confirmed("proj_a", "run_1")
    for _ in range(3):
        assert sessions.claim_ready() == [session]
        sessions.finish_cleanup(session, "retry")
        assert sessions.claim_ready() == []
        now[0] += 60
    assert sessions.claim_ready() == []
    assert sessions.prepare("proj_a", "run_1", "container_a", env={}, context={}) is None


def test_pending_metadata_is_bounded_without_evicting_active_tasks():
    sessions = BrowserSessions(max_sessions=2)
    first = sessions.prepare("proj_a", "run_1", "container_a", env={}, context={})
    second = sessions.prepare("proj_a", "run_2", "container_a", env={}, context={})
    assert first is not None and second is not None
    assert sessions.prepare("proj_a", "run_3", "container_a", env={}, context={}) is None
    assert sessions.prepare("proj_a", "run_1", "container_a", env={}, context={}) == first
    sessions.local_finished("proj_a", "run_1")
    sessions.terminal_confirmed("proj_a", "run_1")
    assert sessions.claim_ready(limit=0) == []
    assert sessions.claim_ready() == [first]
    sessions.finish_cleanup(first, "absent")
    assert sessions.prepare("proj_a", "run_3", "container_a", env={}, context={}) is not None
    assert sessions.prepare("proj_a", "run_2", "container_a", env={}, context={}) == second


def test_optional_completion_notifications_do_not_create_or_close_unowned_sessions():
    notify_browser_task(object(), "proj_a", "run_1", local_finished=True)
    sessions = BrowserSessions()
    manager = SimpleNamespace(browser_sessions=sessions)
    notify_browser_task(manager, "proj_a", None, local_finished=True, terminal_confirmed=True)
    assert sessions.claim_ready() == []
    session = sessions.prepare("proj_a", "run_1", "container_a", env={}, context={})
    notify_browser_task(manager, "proj_a", "run_1", terminal_confirmed=True)
    assert sessions.claim_ready() == []
    notify_browser_task(manager, "proj_a", "run_1", local_finished=True)
    assert sessions.claim_ready() == [session]


def test_failed_preparation_freezes_unmanaged_even_before_initial_registration():
    sessions = BrowserSessions(max_sessions=2)
    sessions.keep_unmanaged("proj_a", "failed")
    assert sessions.policy_for_task("proj_a", "failed", "task") == "legacy"
    assert sessions.policy_for_task("proj_a", "active", "task") == "task"
    assert sessions.policy_for_task("proj_a", "overflow", "task") == "legacy"
    sessions.local_finished("proj_a", "failed")
    assert sessions.policy_for_task("proj_a", "overflow", "task") == "legacy"
    assert sessions.policy_for_task("proj_a", "active", "legacy") == "task"


def test_excessive_phase_metadata_fences_cleanup_without_growing_forever():
    sessions = BrowserSessions()
    session = sessions.prepare("proj_a", "run_1", "container_a", env={}, context={})
    for index in range(257):
        sessions.register_exec(session, ("container_a", f"exec_{index}"))
    assert sessions.execution_ids(session) is None
