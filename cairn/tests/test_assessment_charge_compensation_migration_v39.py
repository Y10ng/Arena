"""v39 历史误计费冲正迁移回归。"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest
import yaml

from cairn.server import database_admin, db, migrations
from cairn.server.routers.export import _export_timeline, _export_yaml


PREDICATE_VERSION = "pre-execution-missing-structured-cell-v1"
REASON_CODE = "structured_explore_missing_slot_pre_execution"
ERROR_MESSAGE = "default-copy intent is missing slot_key"
NOW = "2026-09-19T05:16:00Z"


@pytest.fixture(autouse=True)
def reset_database_configuration():
    db._reset_for_tests()
    yield
    db._reset_for_tests()


def _sqlite_connection(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate_through_v38(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(
            db,
            "SQLITE_MIGRATIONS",
            tuple(
                migration
                for migration in migrations.SQLITE_MIGRATIONS
                if migration.version <= 38
            ),
        )
        db.run_migrations(conn, "sqlite")
        conn.commit()


def _migrate_through_v39(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(
            db,
            "SQLITE_MIGRATIONS",
            tuple(
                migration
                for migration in migrations.SQLITE_MIGRATIONS
                if migration.version <= 39
            ),
        )
        db.run_migrations(conn, "sqlite")
        conn.commit()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _seed_candidates(
    conn: sqlite3.Connection,
    *,
    project_id: str = "project-history",
    candidate_count: int = 1,
) -> list[dict[str, str]]:
    conn.execute(
        """
        INSERT INTO projects (
            id, title, status, created_at, tenant_id, assessment_protocol,
            scenario_snapshot_json, explore_budget, explore_attempts_started,
            execution_generation
        ) VALUES (?, 'history', 'paused', ?, 'tenant_legacy',
                  'default-copy-v1', '{}', 40, ?, 3)
        """,
        (project_id, NOW, candidate_count),
    )
    conn.execute(
        """
        INSERT INTO facts (
            id, project_id, description, creator, created_at
        ) VALUES ('origin', ?, 'authorized origin', 'system', ?)
        """,
        (project_id, NOW),
    )

    candidates: list[dict[str, str]] = []
    receipts: dict[str, dict[str, str]] = {}
    reason_run_id = f"reason-{project_id}"
    for index in range(candidate_count):
        ordinal = index + 1
        ids = {
            "project_id": project_id,
            "intent_id": f"intent-{ordinal}",
            "attempt_id": f"attempt-{ordinal}",
            "explore_run_id": f"explore-{ordinal}",
            "reason_run_id": reason_run_id,
        }
        candidates.append(ids)
        receipts[str(index)] = {
            "digest": f"{ordinal:x}" * 64,
            "intent_id": ids["intent_id"],
        }

    reason_context = {
        "accepted_intent_submissions": receipts,
        "execution_generation": 3,
        "execution_lifecycle": {
            "generation": 3,
            "process_started": True,
            "termination_confirmed": True,
            "cleanup_confirmed": True,
        },
    }
    conn.execute(
        """
        INSERT INTO task_runs (
            id, project_id, task_type, worker_name, worker_type, status,
            started_at, ended_at, runtime_context_json, execution_generation
        ) VALUES (?, ?, 'reason', 'reason-worker', 'pi', 'succeeded',
                  ?, ?, ?, 3)
        """,
        (
            reason_run_id,
            project_id,
            NOW,
            "2026-09-19T05:15:59Z",
            _canonical_json(reason_context),
        ),
    )

    for index, ids in enumerate(candidates):
        started_at = f"2026-09-19T05:16:{index:02d}Z"
        ended_at = f"2026-09-19T05:16:{index + 10:02d}Z"
        conn.execute(
            """
            INSERT INTO intents (
                id, project_id, description, creator, task_run_id,
                worker_history_json, created_at, concluded_at, conclusion,
                slot_key, strategy_key, assessment_role, conclusion_reason
            ) VALUES (?, ?, 'historical obligation', 'reason-worker', ?,
                      '[]', ?, ?, 'failed', NULL,
                      'authorization.owner-vs-other-v1', 'discovery',
                      'server_write_failed')
            """,
            (
                ids["intent_id"],
                project_id,
                reason_run_id,
                started_at,
                ended_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO intent_sources (
                intent_id, project_id, fact_id, source_order
            ) VALUES (?, ?, 'origin', 0)
            """,
            (ids["intent_id"], project_id),
        )
        conn.execute(
            """
            INSERT INTO assessment_attempts (
                id, project_id, intent_id, slot_key, assessment_role,
                status, charged, execution_count, max_runtime_retries,
                strategy_key, started_at, ended_at, outcome
            ) VALUES (?, ?, ?, NULL, 'discovery', 'failed', 1, 1, 2,
                      'authorization.owner-vs-other-v1', ?, ?,
                      'server_write_failed')
            """,
            (
                ids["attempt_id"],
                project_id,
                ids["intent_id"],
                started_at,
                ended_at,
            ),
        )
        explore_context = {
            "assessment_phase": "baseline",
            "dispatcher_id": "machine-history",
            "execution_container_ids": [],
            "execution_generation": 3,
            "execution_lifecycle": {
                "generation": 3,
                "process_started": False,
                "termination_confirmed": True,
                "cleanup_confirmed": True,
            },
        }
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, intent_id, task_type, worker_name,
                worker_type, status, session_id, started_at, ended_at,
                error_type, error_message, runtime_context_json,
                assessment_attempt_id, execution_generation,
                pause_requested, pause_interrupted
            ) VALUES (?, ?, ?, 'explore', 'explore-worker', 'pi', 'failed',
                      NULL, ?, ?, 'server_write_failed', ?, ?, ?, 3, 0, 0)
            """,
            (
                ids["explore_run_id"],
                project_id,
                ids["intent_id"],
                started_at,
                ended_at,
                ERROR_MESSAGE,
                _canonical_json(explore_context),
                ids["attempt_id"],
            ),
        )
    return candidates


def _proof_material(
    ids: dict[str, str],
    *,
    task_run_ids: list[str],
    counter_before: int,
) -> dict[str, object]:
    return {
        "assessment_attempt_id": ids["attempt_id"],
        "charge_delta": -1,
        "error_message": ERROR_MESSAGE,
        "error_type": "server_write_failed",
        "intent_id": ids["intent_id"],
        "original_charged": 1,
        "predicate_version": PREDICATE_VERSION,
        "project_counter_after": counter_before - 1,
        "project_counter_before": counter_before,
        "project_id": ids["project_id"],
        "reason_code": REASON_CODE,
        "source_reason_task_run_id": ids["reason_run_id"],
        "task_run_ids": task_run_ids,
    }


def test_v39_compensates_three_proven_pre_execution_charges_append_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _sqlite_connection(tmp_path / "assessment-charge-v39.db")
    try:
        _migrate_through_v38(conn, monkeypatch)
        candidates = _seed_candidates(conn, candidate_count=3)
        project_before = dict(
            conn.execute(
                "SELECT * FROM projects WHERE id = 'project-history'"
            ).fetchone()
        )
        intents_before = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM intents WHERE project_id = 'project-history' "
                "ORDER BY id"
            )
        ]
        runs_before = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM task_runs WHERE project_id = 'project-history' "
                "ORDER BY id"
            )
        ]
        attempts_before = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM assessment_attempts "
                "WHERE project_id = 'project-history' ORDER BY id"
            )
        ]
        registry_prefix = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM schema_migrations WHERE version <= 38 "
                "ORDER BY version"
            )
        ]
        conn.commit()

        _migrate_through_v39(conn, monkeypatch)

        assert registry_prefix == [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM schema_migrations WHERE version <= 38 "
                "ORDER BY version"
            )
        ]
        migration_v39 = conn.execute(
            "SELECT version, name, checksum FROM schema_migrations "
            "WHERE version = 39"
        ).fetchone()
        assert dict(migration_v39) == {
            "version": 39,
            "name": "assessment-charge-compensations-v1",
            "checksum": migrations.SQLITE_ASSESSMENT_CHARGE_COMPENSATIONS.checksum,
        }
        assert conn.execute(
            "SELECT explore_attempts_started FROM projects "
            "WHERE id = 'project-history'"
        ).fetchone()["explore_attempts_started"] == 0
        assert [
            row["charged"]
            for row in conn.execute(
                "SELECT charged FROM assessment_attempts "
                "WHERE project_id = 'project-history' ORDER BY id"
            )
        ] == [0, 0, 0]

        project_after = dict(
            conn.execute(
                "SELECT * FROM projects WHERE id = 'project-history'"
            ).fetchone()
        )
        expected_project = dict(project_before)
        expected_project["explore_attempts_started"] = 0
        assert project_after == expected_project
        assert intents_before == [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM intents WHERE project_id = 'project-history' "
                "ORDER BY id"
            )
        ]
        assert runs_before == [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM task_runs WHERE project_id = 'project-history' "
                "ORDER BY id"
            )
        ]
        attempts_after = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM assessment_attempts "
                "WHERE project_id = 'project-history' ORDER BY id"
            )
        ]
        for before, after in zip(attempts_before, attempts_after, strict=True):
            expected_attempt = dict(before)
            expected_attempt["charged"] = 0
            assert after == expected_attempt

        compensations = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM assessment_charge_compensations "
                "WHERE project_id = 'project-history' "
                "ORDER BY project_counter_before DESC, assessment_attempt_id"
            )
        ]
        assert len(compensations) == 3
        for index, (ids, row) in enumerate(
            zip(candidates, compensations, strict=True)
        ):
            counter_before = 3 - index
            assert row == {
                "project_id": "project-history",
                "assessment_attempt_id": ids["attempt_id"],
                "migration_version": 39,
                "predicate_version": PREDICATE_VERSION,
                "reason_code": REASON_CODE,
                "original_charged": 1,
                "charge_delta": -1,
                "project_counter_before": counter_before,
                "project_counter_after": counter_before - 1,
                "source_reason_task_run_id": ids["reason_run_id"],
                "task_run_ids_json": _canonical_json([ids["explore_run_id"]]),
                "proof_sha256": sha256(
                    _canonical_json(
                        _proof_material(
                            ids,
                            task_run_ids=[ids["explore_run_id"]],
                            counter_before=counter_before,
                        )
                    ).encode("utf-8")
                ).hexdigest(),
                "created_at": row["created_at"],
            }
            assert row["created_at"].endswith("Z")

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE assessment_charge_compensations "
                "SET reason_code = 'changed' "
                "WHERE project_id = 'project-history' "
                "AND assessment_attempt_id = 'attempt-1'"
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "DELETE FROM assessment_charge_compensations "
                "WHERE project_id = 'project-history' "
                "AND assessment_attempt_id = 'attempt-1'"
            )
        conn.rollback()

        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        registry = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM schema_migrations ORDER BY version"
            )
        ]
        _migrate_through_v39(conn, monkeypatch)
        assert registry == [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM schema_migrations ORDER BY version"
            )
        ]
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM assessment_charge_compensations"
        ).fetchone()["count"] == 3

        conn.execute("DELETE FROM projects WHERE id = 'project-history'")
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM assessment_charge_compensations"
        ).fetchone()["count"] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def _set_explore_context(
    conn: sqlite3.Connection,
    context: dict[str, object],
) -> None:
    conn.execute(
        "UPDATE task_runs SET runtime_context_json = ? "
        "WHERE id = 'explore-1' AND project_id = 'project-history'",
        (_canonical_json(context),),
    )


def _current_explore_context(conn: sqlite3.Connection) -> dict[str, object]:
    value = conn.execute(
        "SELECT runtime_context_json FROM task_runs "
        "WHERE id = 'explore-1' AND project_id = 'project-history'"
    ).fetchone()["runtime_context_json"]
    return json.loads(value)


def _insert_unrelated_fact(conn: sqlite3.Connection, fact_id: str) -> None:
    conn.execute(
        "INSERT INTO facts (id, project_id, description, creator, created_at) "
        "VALUES (?, 'project-history', 'unrelated', 'system', ?)",
        (fact_id, NOW),
    )


def _apply_disqualifier(conn: sqlite3.Connection, case: str) -> None:
    if case == "project_active":
        conn.execute(
            "UPDATE projects SET status = 'active' WHERE id = 'project-history'"
        )
    elif case == "wrong_protocol":
        conn.execute(
            "UPDATE projects SET assessment_protocol = 'other-v1' "
            "WHERE id = 'project-history'"
        )
    elif case == "verifier_role":
        conn.execute(
            "UPDATE assessment_attempts SET assessment_role = 'verifier' "
            "WHERE id = 'attempt-1'"
        )
    elif case == "attempt_active":
        conn.execute(
            "UPDATE assessment_attempts SET status = 'active', ended_at = NULL "
            "WHERE id = 'attempt-1'"
        )
    elif case == "attempt_uncharged":
        conn.execute(
            "UPDATE assessment_attempts SET charged = 0 WHERE id = 'attempt-1'"
        )
        conn.execute(
            "UPDATE projects SET explore_attempts_started = 0 "
            "WHERE id = 'project-history'"
        )
    elif case == "attempt_has_slot":
        conn.execute(
            "UPDATE assessment_attempts SET slot_key = 'cell-1' "
            "WHERE id = 'attempt-1'"
        )
    elif case == "intent_has_slot":
        conn.execute(
            "UPDATE intents SET slot_key = 'cell-1' WHERE id = 'intent-1'"
        )
    elif case == "intent_not_failed":
        conn.execute(
            "UPDATE intents SET conclusion = 'abandoned' WHERE id = 'intent-1'"
        )
    elif case == "strategy_mismatch":
        conn.execute(
            "UPDATE intents SET strategy_key = 'authorization.different-v1' "
            "WHERE id = 'intent-1'"
        )
    elif case == "execution_count_mismatch":
        conn.execute(
            "UPDATE assessment_attempts SET execution_count = 2 "
            "WHERE id = 'attempt-1'"
        )
    elif case == "intent_has_result":
        _insert_unrelated_fact(conn, "result-1")
        conn.execute(
            "UPDATE intents SET to_fact_id = 'result-1' WHERE id = 'intent-1'"
        )
    elif case == "missing_reason_receipt":
        conn.execute(
            "UPDATE task_runs SET runtime_context_json = '{}' "
            "WHERE id = 'reason-project-history'"
        )
    elif case == "invalid_reason_receipt":
        conn.execute(
            "UPDATE task_runs SET runtime_context_json = ? "
            "WHERE id = 'reason-project-history'",
            (
                _canonical_json(
                    {
                        "accepted_intent_submissions": {
                            "00": {"digest": "g" * 64, "intent_id": "intent-1"}
                        }
                    }
                ),
            ),
        )
    elif case == "duplicate_reason_json_key":
        conn.execute(
            "UPDATE task_runs SET runtime_context_json = ? "
            "WHERE id = 'reason-project-history'",
            (
                '{"accepted_intent_submissions":{},'
                '"accepted_intent_submissions":'
                '{"0":{"digest":"' + "a" * 64 + '","intent_id":"intent-1"}}}',
            ),
        )
    elif case == "missing_explore_run":
        conn.execute(
            "DELETE FROM task_runs WHERE id = 'explore-1' "
            "AND project_id = 'project-history'"
        )
    elif case == "ambiguous_explore_run":
        context = _current_explore_context(conn)
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, intent_id, task_type, worker_name, worker_type,
                status, started_at, ended_at, error_type, error_message,
                runtime_context_json, assessment_attempt_id,
                execution_generation
            ) VALUES ('explore-extra', 'project-history', 'intent-1', 'explore',
                      'explore-worker', 'pi', 'failed', ?, ?,
                      'server_write_failed', ?, ?, 'attempt-1', 3)
            """,
            (NOW, NOW, ERROR_MESSAGE, _canonical_json(context)),
        )
    elif case == "wrong_error":
        conn.execute(
            "UPDATE task_runs SET error_message = 'different failure' "
            "WHERE id = 'explore-1'"
        )
    elif case == "wrong_error_type":
        conn.execute(
            "UPDATE task_runs SET error_type = 'request_contract' "
            "WHERE id = 'explore-1'"
        )
    elif case == "missing_attempt_link":
        conn.execute(
            "UPDATE task_runs SET assessment_attempt_id = NULL "
            "WHERE id = 'explore-1'"
        )
    elif case == "non_explore_attempt_link":
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, intent_id, task_type, worker_name, worker_type,
                status, started_at, ended_at, runtime_context_json,
                assessment_attempt_id, execution_generation
            ) VALUES ('reason-linked', 'project-history', 'intent-1', 'reason',
                      'reason-worker', 'pi', 'succeeded', ?, ?, '{}',
                      'attempt-1', 3)
            """,
            (NOW, NOW),
        )
    elif case == "session_present":
        conn.execute(
            "UPDATE task_runs SET session_id = 'session-1' "
            "WHERE id = 'explore-1'"
        )
    elif case == "execution_delivery_present":
        context = _current_explore_context(conn)
        context["execution_delivery"] = {"session_id": "session-1"}
        _set_explore_context(conn, context)
    elif case == "process_started":
        context = _current_explore_context(conn)
        context["execution_lifecycle"]["process_started"] = True  # type: ignore[index]
        _set_explore_context(conn, context)
    elif case == "generation_mismatch":
        context = _current_explore_context(conn)
        context["execution_lifecycle"]["generation"] = 2  # type: ignore[index]
        _set_explore_context(conn, context)
    elif case == "termination_unconfirmed":
        context = _current_explore_context(conn)
        context["execution_lifecycle"]["termination_confirmed"] = False  # type: ignore[index]
        _set_explore_context(conn, context)
    elif case == "cleanup_unconfirmed":
        context = _current_explore_context(conn)
        context["execution_lifecycle"]["cleanup_confirmed"] = False  # type: ignore[index]
        _set_explore_context(conn, context)
    elif case == "accepted_result_present":
        context = _current_explore_context(conn)
        context["accepted_result_digest"] = "a" * 64
        context["accepted_fact_id"] = "fact-accepted"
        _set_explore_context(conn, context)
    elif case == "accepted_completion_present":
        context = _current_explore_context(conn)
        context["accepted_completion_digest"] = "a" * 64
        context["accepted_completion_intent_id"] = "intent-1"
        _set_explore_context(conn, context)
    elif case == "duplicate_explore_json_key":
        context = _canonical_json(_current_explore_context(conn))
        conn.execute(
            "UPDATE task_runs SET runtime_context_json = ? "
            "WHERE id = 'explore-1'",
            (context[:-1] + ',"execution_generation":3}',),
        )
    elif case == "agent_event_present":
        conn.execute(
            """
            INSERT INTO agent_events (
                id, task_run_id, project_id, ts, kind, payload_json
            ) VALUES ('event-1', 'explore-1', 'project-history', ?,
                      'command_finished', '{}')
            """,
            (NOW,),
        )
    elif case == "execution_event_present":
        conn.execute(
            """
            INSERT INTO execution_events (
                id, project_id, task_run_id, protocol, producer_event_key,
                event_kind, phase, category, producer_recorded_at, received_at,
                verified_dispatcher_id, execution_generation,
                generation_disposition, payload_digest_sha256, payload_json
            ) VALUES (
                'execution-event-1', 'project-history', 'explore-1',
                'cairn-execution-event-v1', 'checkpoint-1', 'checkpoint',
                'explore_execute', 'progress', ?, ?, 'machine-history', 3,
                'current_at_receipt', ?, '{}'
            )
            """,
            (NOW, NOW, "a" * 64),
        )
    elif case == "fact_present":
        conn.execute(
            """
            INSERT INTO facts (
                id, project_id, description, task_run_id, creator, created_at
            ) VALUES ('fact-output', 'project-history', 'output', 'explore-1',
                      'explore-worker', ?)
            """,
            (NOW,),
        )
    elif case == "artifact_present":
        conn.execute(
            """
            INSERT INTO artifacts (
                id, project_id, task_run_id, kind, category, uri, sha256,
                size, created_at, metadata_json
            ) VALUES ('artifact-1', 'project-history', 'explore-1', 'file',
                      'evidence', 'project://artifact-1', ?, 1, ?, '{}')
            """,
            ("a" * 64, NOW),
        )
    elif case == "target_present":
        conn.execute(
            """
            INSERT INTO targets (
                id, project_id, value, type, status, discovered_by_run_id,
                created_at
            ) VALUES ('target-1', 'project-history', 'safe.invalid', 'domain',
                      'discovered', 'explore-1', ?)
            """,
            (NOW,),
        )
    elif case == "coverage_result_present":
        conn.execute(
            """
            INSERT INTO coverage_slots (
                project_id, slot_key, kind, description, status, tier,
                assigned_intent_id, verified_run_id, created_at, updated_at
            ) VALUES ('project-history', 'cell-1', 'endpoint', 'synthetic',
                      'verified', 'core', 'intent-1', 'explore-1', ?, ?)
            """,
            (NOW, NOW),
        )
    elif case == "surface_present":
        conn.execute(
            """
            INSERT INTO surface_manifest_entries (
                entry_id, project_id, surface_id, surface_kind, surface_key,
                source_intent_id, source_task_run_id,
                observation_fingerprint, created_at
            ) VALUES ('surface-1', 'project-history', 'surface-1', 'endpoint',
                      'GET safe.invalid', 'intent-1', 'explore-1', ?, ?)
            """,
            ("a" * 64, NOW),
        )
    elif case == "scope_disposition_present":
        conn.execute(
            """
            INSERT INTO coverage_scope_dispositions (
                disposition_id, project_id, disposition_kind,
                target_snapshot_digest, reason, source_intent_id,
                source_fact_id, source_task_run_id,
                observation_fingerprint, created_at
            ) VALUES ('disp-1', 'project-history', 'not_applicable', ?,
                      'synthetic', 'intent-1', 'origin', 'explore-1', ?, ?)
            """,
            ("a" * 64, "b" * 64, NOW),
        )
    elif case == "vulnerability_present":
        conn.execute(
            """
            INSERT INTO vulnerabilities (
                id, project_id, title, severity, discovered_by_run_id,
                created_at, updated_at
            ) VALUES ('vuln-1', 'project-history', 'synthetic', 'low',
                      'explore-1', ?, ?)
            """,
            (NOW, NOW),
        )
    elif case == "observation_present":
        conn.execute(
            """
            INSERT INTO vulnerabilities (
                id, project_id, title, severity, created_at, updated_at
            ) VALUES ('vuln-1', 'project-history', 'synthetic', 'low', ?, ?)
            """,
            (NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO vulnerability_observations (
                id, project_id, vulnerability_id, task_run_id, severity,
                confidence, observed_at
            ) VALUES ('obs-1', 'project-history', 'vuln-1', 'explore-1',
                      'low', 0.2, ?)
            """,
            (NOW,),
        )
    elif case == "verification_present":
        _insert_unrelated_fact(conn, "fact-verification")
        conn.execute(
            """
            INSERT INTO vulnerabilities (
                id, project_id, title, severity, created_at, updated_at
            ) VALUES ('vuln-1', 'project-history', 'synthetic', 'low', ?, ?)
            """,
            (NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO vulnerability_observations (
                id, project_id, vulnerability_id, severity, confidence,
                observed_at
            ) VALUES ('obs-1', 'project-history', 'vuln-1', 'low', 0.2, ?)
            """,
            (NOW,),
        )
        conn.execute(
            """
            INSERT INTO vulnerability_verifications (
                id, project_id, vulnerability_id, observation_id, intent_id,
                fact_id, verifier_task_run_id, verifier_worker,
                discovery_task_run_id, independence_level, outcome, created_at
            ) VALUES ('verification-1', 'project-history', 'vuln-1', 'obs-1',
                      'intent-1', 'fact-verification',
                      'reason-project-history', 'reason-worker', 'explore-1',
                      'independent_worker', 'confirmed', ?)
            """,
            (NOW,),
        )
    elif case == "fact_evidence_present":
        _insert_unrelated_fact(conn, "fact-evidence")
        conn.execute(
            """
            INSERT INTO artifacts (
                id, project_id, kind, category, uri, sha256,
                size, created_at, metadata_json
            ) VALUES ('artifact-evidence', 'project-history', 'file',
                      'evidence', 'project://artifact-evidence', ?, 1, ?, '{}')
            """,
            ("a" * 64, NOW),
        )
        conn.execute(
            """
            INSERT INTO fact_evidence (
                project_id, fact_id, artifact_id, pointer, verifier_run_id,
                created_at
            ) VALUES ('project-history', 'fact-evidence',
                      'artifact-evidence', '', 'explore-1', ?)
            """,
            (NOW,),
        )
    elif case == "report_receipt_present":
        conn.execute(
            """
            INSERT INTO assessment_report_finish_receipts (
                project_id, task_run_id, protocol, generation_token,
                request_sha256, request_json, response_json, accepted_at
            ) VALUES ('project-history', 'explore-1',
                      'cairn-assessment-report-finish-v1', 'generation-1',
                      ?, '{}', '{}', ?)
            """,
            ("a" * 64, NOW),
        )
    elif case == "payload_present":
        conn.execute(
            """
            INSERT INTO injection_payload_records (
                id, project_id, task_run_id, phase, capture_id,
                injection_type, payload_text, original_format,
                response_status, created_at, updated_at
            ) VALUES ('payload-1', 'project-history', 'explore-1',
                      'explore_execute', 'capture-1', 'prompt_injection',
                      'synthetic', 'text', 'not_sent', ?, ?)
            """,
            (NOW, NOW),
        )
    elif case == "active_project_run":
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, task_type, worker_name, worker_type, status,
                runtime_context_json, execution_generation
            ) VALUES ('run-active', 'project-history', 'bootstrap', 'worker',
                      'pi', 'running', '{}', 3)
            """
        )
    else:  # pragma: no cover - 参数表遗漏必须显式失败。
        raise AssertionError(f"unknown disqualifier: {case}")


@pytest.mark.parametrize(
    "case",
    [
        "project_active",
        "wrong_protocol",
        "verifier_role",
        "attempt_active",
        "attempt_uncharged",
        "attempt_has_slot",
        "intent_has_slot",
        "intent_not_failed",
        "strategy_mismatch",
        "execution_count_mismatch",
        "intent_has_result",
        "missing_reason_receipt",
        "invalid_reason_receipt",
        "duplicate_reason_json_key",
        "missing_explore_run",
        "ambiguous_explore_run",
        "wrong_error",
        "wrong_error_type",
        "missing_attempt_link",
        "non_explore_attempt_link",
        "session_present",
        "execution_delivery_present",
        "process_started",
        "generation_mismatch",
        "termination_unconfirmed",
        "cleanup_unconfirmed",
        "accepted_result_present",
        "accepted_completion_present",
        "duplicate_explore_json_key",
        "agent_event_present",
        "execution_event_present",
        "fact_present",
        "artifact_present",
        "target_present",
        "coverage_result_present",
        "surface_present",
        "scope_disposition_present",
        "vulnerability_present",
        "observation_present",
        "verification_present",
        "fact_evidence_present",
        "report_receipt_present",
        "payload_present",
        "active_project_run",
    ],
)
def test_v39_does_not_compensate_when_any_required_proof_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    conn = _sqlite_connection(tmp_path / f"assessment-charge-v39-{case}.db")
    try:
        _migrate_through_v38(conn, monkeypatch)
        _seed_candidates(conn)
        _apply_disqualifier(conn, case)
        charged_before = conn.execute(
            "SELECT charged FROM assessment_attempts WHERE id = 'attempt-1'"
        ).fetchone()["charged"]
        counter_before = conn.execute(
            "SELECT explore_attempts_started FROM projects "
            "WHERE id = 'project-history'"
        ).fetchone()["explore_attempts_started"]
        conn.commit()

        _migrate_through_v39(conn, monkeypatch)

        assert conn.execute(
            "SELECT charged FROM assessment_attempts WHERE id = 'attempt-1'"
        ).fetchone()["charged"] == charged_before
        assert conn.execute(
            "SELECT explore_attempts_started FROM projects "
            "WHERE id = 'project-history'"
        ).fetchone()["explore_attempts_started"] == counter_before
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM assessment_charge_compensations"
        ).fetchone()["count"] == 0
    finally:
        conn.close()


def test_v39_conflicting_existing_compensation_aborts_without_rewriting_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _sqlite_connection(tmp_path / "assessment-charge-v39-conflict.db")
    try:
        _migrate_through_v38(conn, monkeypatch)
        _seed_candidates(conn)
        conn.executescript(migrations.SQLITE_ASSESSMENT_CHARGE_COMPENSATIONS_V39)
        conn.execute(
            """
            INSERT INTO assessment_charge_compensations (
                project_id, assessment_attempt_id, migration_version,
                predicate_version, reason_code, original_charged,
                charge_delta, project_counter_before, project_counter_after,
                source_reason_task_run_id, task_run_ids_json, proof_sha256,
                created_at
            ) VALUES (
                'project-history', 'attempt-1', 39, ?, ?, 1, -1, 1, 0,
                'reason-project-history', '["explore-1"]', ?, ?
            )
            """,
            (PREDICATE_VERSION, REASON_CODE, "0" * 64, NOW),
        )
        conn.commit()

        with pytest.raises(RuntimeError, match="Conflicting assessment charge"):
            _migrate_through_v39(conn, monkeypatch)
        conn.rollback()

        assert conn.execute(
            "SELECT charged FROM assessment_attempts WHERE id = 'attempt-1'"
        ).fetchone()["charged"] == 1
        assert conn.execute(
            "SELECT explore_attempts_started FROM projects "
            "WHERE id = 'project-history'"
        ).fetchone()["explore_attempts_started"] == 1
        assert conn.execute(
            "SELECT proof_sha256 FROM assessment_charge_compensations"
        ).fetchone()["proof_sha256"] == "0" * 64
        assert conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 39"
        ).fetchone() is None
    finally:
        conn.close()


def test_v39_counter_mismatch_rolls_back_the_whole_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _sqlite_connection(tmp_path / "assessment-charge-v39-counter.db")
    try:
        _migrate_through_v38(conn, monkeypatch)
        _seed_candidates(conn)
        conn.execute(
            "UPDATE projects SET explore_attempts_started = 2 "
            "WHERE id = 'project-history'"
        )
        conn.commit()

        with pytest.raises(RuntimeError, match="counter mismatch"):
            _migrate_through_v39(conn, monkeypatch)
        conn.rollback()

        assert conn.execute(
            "SELECT charged FROM assessment_attempts WHERE id = 'attempt-1'"
        ).fetchone()["charged"] == 1
        assert conn.execute(
            "SELECT explore_attempts_started FROM projects "
            "WHERE id = 'project-history'"
        ).fetchone()["explore_attempts_started"] == 2
        assert conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 39"
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'assessment_charge_compensations'"
        ).fetchone() is None
    finally:
        conn.close()


def test_v39_compensations_are_visible_in_yaml_timeline_and_database_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "assessment-charge-v39-export.db"
    conn = _sqlite_connection(path)
    try:
        _migrate_through_v38(conn, monkeypatch)
        _seed_candidates(conn)
        _seed_candidates(conn, project_id="project-other")
        conn.commit()
        db.run_migrations(conn, "sqlite")
        conn.commit()

        exported = yaml.safe_load(_export_yaml(conn, "project-history"))
        compensations = exported["assessment_charge_compensations"]
        assert len(compensations) == 1
        assert compensations[0] == {
            "assessment_attempt_id": "attempt-1",
            "migration_version": 39,
            "predicate_version": PREDICATE_VERSION,
            "reason_code": REASON_CODE,
            "original_charged": 1,
            "charge_delta": -1,
            "project_counter_before": 1,
            "project_counter_after": 0,
            "source_reason_task_run_id": "reason-project-history",
            "task_run_ids": ["explore-1"],
            "proof_sha256": compensations[0]["proof_sha256"],
            "created_at": compensations[0]["created_at"],
        }
        assert len(compensations[0]["proof_sha256"]) == 64

        timeline = _export_timeline(conn, "project-history")
        assert timeline.count("ASSESSMENT CHARGE COMPENSATED") == 1
        assert "ASSESSMENT CHARGE COMPENSATED attempt-1" in timeline
        assert f"reason: {REASON_CODE}" in timeline
        assert "delta: -1 (1 -> 0)" in timeline
        assert "task_runs: explore-1" in timeline
        assert "reason-project-other" not in timeline
    finally:
        conn.close()

    status = database_admin.status(db_path=path)
    assert status.counts["assessment_charge_compensations"] == 2


def test_fresh_schema_and_postgres_v39_contracts_are_append_only() -> None:
    conn = _sqlite_connection(":memory:")
    try:
        conn.executescript(db.SCHEMA)
        table = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'assessment_charge_compensations'"
        ).fetchone()
        triggers = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND name LIKE 'trg_assessment_charge_compensations_%'"
            )
        }
    finally:
        conn.close()

    assert table is not None
    assert triggers == {
        "trg_assessment_charge_compensations_no_update",
        "trg_assessment_charge_compensations_no_delete",
    }
    assert "assessment_charge_compensations" not in migrations.SQLITE_V1_SCHEMA
    assert "assessment_charge_compensations" in db.SCHEMA
    assert migrations.SQLITE_MIGRATIONS[-1].version == 41
    assert migrations.POSTGRES_MIGRATIONS[-1].version == 41

    postgres_sql = " ".join(
        migrations.POSTGRES_ASSESSMENT_CHARGE_COMPENSATIONS_V39.split()
    )
    assert "PRIMARY KEY (project_id, assessment_attempt_id)" in postgres_sql
    assert "charge_delta = -1" in postgres_sql
    assert "BEFORE UPDATE OR DELETE ON assessment_charge_compensations" in postgres_sql
    assert "FOR UPDATE" in migrations.POSTGRES_ASSESSMENT_CHARGE_COMPENSATIONS_LOCK_SQL
    assert (
        migrations.POSTGRES_ASSESSMENT_CHARGE_COMPENSATIONS.checksum
        != migrations.SQLITE_ASSESSMENT_CHARGE_COMPENSATIONS.checksum
    )
