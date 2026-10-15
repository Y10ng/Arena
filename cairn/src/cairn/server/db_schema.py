"""新建 Cairn 数据库使用的 SQLite schema 基线。"""

from __future__ import annotations

# SCHEMA 是已发布的 v1 基线，内容参与 checksum，禁止原地修改。
SCHEMA = """\
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    intent_timeout INTEGER NOT NULL DEFAULT 60,
    reason_timeout INTEGER NOT NULL DEFAULT 180,
    coverage_idle_rounds INTEGER NOT NULL DEFAULT 3
);

INSERT INTO settings (id, intent_timeout, reason_timeout) VALUES (1, 60, 180)
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS digest_worker (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 1,
    name TEXT NOT NULL DEFAULT 'digest-analyzer',
    type TEXT NOT NULL DEFAULT 'claudecode',
    model TEXT NOT NULL DEFAULT 'deepseek-v4-flash',
    base_url TEXT NOT NULL DEFAULT 'http://127.0.0.1:8317',
    api_key TEXT NOT NULL DEFAULT '',
    provider_api TEXT NOT NULL DEFAULT 'openai-completions',
    max_running INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 1
);

INSERT INTO digest_worker
    (id, enabled, name, type, model, base_url, api_key, provider_api, max_running, priority)
VALUES
    (1, 1, 'digest-analyzer', 'claudecode', 'deepseek-v4-flash',
     'http://127.0.0.1:8317', '', 'openai-completions', 1, 1)
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS scenarios (
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    profile_id TEXT NOT NULL DEFAULT '',
    worker_ids_json TEXT NOT NULL DEFAULT '[]',
    image TEXT NOT NULL DEFAULT '',
    prompt_group TEXT NOT NULL DEFAULT 'default',
    knowledge TEXT NOT NULL DEFAULT 'knowledge_base',
    cap_add_json TEXT NOT NULL DEFAULT '[]',
    features_json TEXT NOT NULL DEFAULT '{}',
    enabled_workers_json TEXT NOT NULL DEFAULT '[]',
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    scenario_key TEXT NOT NULL DEFAULT 'default',
    preferred_workers_json TEXT NOT NULL DEFAULT '[]',
    group_id TEXT REFERENCES project_groups(id) ON DELETE SET NULL,
    reason_worker TEXT,
    reason_trigger TEXT,
    reason_started_at TEXT,
    reason_last_heartbeat_at TEXT,
    -- Phase 4 收敛机制：reason idle streak（连续无新 fact 轮数）+ 上次 fact 快照；
    -- explore_budget/deadline 为预算兜底（NULL=不限）。仅 coverage_gate 场景读。
    reason_idle_streak INTEGER NOT NULL DEFAULT 0,
    reason_last_fact_count INTEGER NOT NULL DEFAULT 0,
    explore_budget INTEGER,
    explore_deadline TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    task_run_id TEXT,
    creator TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00Z',
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intents (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    to_fact_id TEXT,
    description TEXT NOT NULL,
    creator TEXT NOT NULL,
    task_run_id TEXT,
    worker TEXT,
    worker_history_json TEXT NOT NULL DEFAULT '[]',
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    -- conclusion 收敛方式：NULL=未收敛(open)；completed=正常结出 fact；
    -- abandoned=被 reason 主动放弃或 complete 时强制收尾；failed=探索失败收尾。
    conclusion TEXT,
    -- depends_on 引用同项目另一 intent id，表示执行顺序约束（B 等 A 收敛后才可 claim）。
    -- NULL=无依赖，立即可派发。与 from（因果来源 fact）语义不同。
    depends_on TEXT,
    -- slot_key 把该 intent 绑定到 coverage_slots 的一条待覆盖槽位（pentest 场景）。
    -- NULL=非槽位驱动的普通 intent（default/logic-vuln 场景始终为 NULL，向后兼容）。
    slot_key TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intent_sources (
    intent_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    source_order INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (intent_id, project_id, fact_id),
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS hints (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    fact_id TEXT,
    intent_id TEXT,
    content TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id),
    FOREIGN KEY (fact_id, project_id) REFERENCES facts(id, project_id) ON DELETE SET NULL,
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS targets (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    value TEXT NOT NULL,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    source_fact_id TEXT,
    discovered_by_run_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id),
    FOREIGN KEY (source_fact_id, project_id) REFERENCES facts(id, project_id) ON DELETE SET NULL,
    FOREIGN KEY (discovered_by_run_id, project_id) REFERENCES task_runs(id, project_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_targets_project_created
ON targets (project_id, created_at);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

INSERT INTO counters (name, value) VALUES ('project', 0) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS scoped_counters (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, kind)
);

CREATE TABLE IF NOT EXISTS task_runs (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    intent_id TEXT,
    task_type TEXT NOT NULL,
    worker_name TEXT NOT NULL,
    worker_type TEXT NOT NULL,
    status TEXT NOT NULL,
    session_id TEXT,
    prompt_group TEXT,
    prompt_version TEXT,
    execution_profile TEXT,
    started_at TEXT,
    ended_at TEXT,
    exit_code INTEGER,
    error_type TEXT,
    error_message TEXT,
    runtime_context_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_task_runs_project_started
ON task_runs (project_id, started_at);

CREATE INDEX IF NOT EXISTS idx_task_runs_status_time
ON task_runs (status, ended_at, started_at);

CREATE INDEX IF NOT EXISTS idx_task_runs_worker_status_time
ON task_runs (worker_name, worker_type, status, ended_at, started_at);

CREATE TABLE IF NOT EXISTS agent_events (
    id TEXT NOT NULL,
    task_run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (id, project_id),
    FOREIGN KEY (task_run_id, project_id) REFERENCES task_runs(id, project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_agent_events_task_ts
ON agent_events (project_id, task_run_id, ts);

CREATE INDEX IF NOT EXISTS idx_agent_events_kind_ts
ON agent_events (kind, ts);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_run_id TEXT,
    kind TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'evidence',
    uri TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (id, project_id),
    FOREIGN KEY (task_run_id, project_id) REFERENCES task_runs(id, project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_artifacts_project_task
ON artifacts (project_id, task_run_id, created_at);

CREATE TABLE IF NOT EXISTS dispatcher_status (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    started_at TEXT,
    config_hash TEXT,
    host TEXT,
    config_path TEXT,
    config_mtime INTEGER,
    config_sha256 TEXT,
    runtime_json TEXT NOT NULL DEFAULT '{}',
    container_json TEXT NOT NULL DEFAULT '{}',
    server_reachable_path TEXT,
    last_loop_started_at TEXT,
    last_loop_finished_at TEXT,
    last_loop_duration_ms INTEGER,
    running_task_count INTEGER NOT NULL DEFAULT 0,
    active_project_count INTEGER NOT NULL DEFAULT 0,
    worker_cooldowns_json TEXT NOT NULL DEFAULT '[]',
    worker_rejections_json TEXT NOT NULL DEFAULT '[]',
    worker_pool_json TEXT NOT NULL DEFAULT '[]',
    container_images_json TEXT NOT NULL DEFAULT '[]',
    cap_baseline_json TEXT,
    declared_inventory_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dispatcher_connectivity_tests (
    id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    target_key TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    model_id TEXT,
    scope TEXT NOT NULL,
    status TEXT NOT NULL,
    category TEXT,
    detail TEXT NOT NULL DEFAULT '',
    http_status INTEGER,
    latency_ms INTEGER,
    config_revision TEXT NOT NULL,
    credential_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_dispatcher_connectivity_target_created
ON dispatcher_connectivity_tests (target_key, created_at DESC);

CREATE TABLE IF NOT EXISTS fact_evidence (
    project_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    pointer TEXT NOT NULL DEFAULT '',
    verifier_run_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, fact_id, artifact_id, pointer),
    FOREIGN KEY (fact_id, project_id) REFERENCES facts(id, project_id) ON DELETE CASCADE,
    FOREIGN KEY (artifact_id, project_id) REFERENCES artifacts(id, project_id) ON DELETE CASCADE
);

-- 项目组：多人共用时按 owner（localActor 软身份）隔离，组内管理项目。
-- 无认证下 owner 为约定级隔离；删组时组内项目 group_id 置 NULL（降级为未分组），不删项目。
CREATE TABLE IF NOT EXISTS project_groups (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner TEXT NOT NULL,
    created_at TEXT NOT NULL
);

INSERT INTO counters (name, value) VALUES ('project_group', 0) ON CONFLICT DO NOTHING;

-- worker 模板：保存一组 worker name 的选择预设，按 owner（localActor）隔离。
-- 模板只引用 worker 名，API key 仍只在 dispatch.yaml；建项目时选模板即拷贝为 preferred_workers。
CREATE TABLE IF NOT EXISTS worker_templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner TEXT NOT NULL,
    worker_names_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

INSERT INTO counters (name, value) VALUES ('worker_template', 0) ON CONFLICT DO NOTHING;

-- 覆盖槽位：渗透测试场景（pentest）的客观完成门数据。
-- bootstrap 枚举攻击面时登记 pending 槽位；explore 跑过一轮置 explored；
-- Phase 3 verifier 复现成功置 verified。complete 时若仍有未收敛槽位则 409。
-- 仅 pentest 场景读写；default/logic-vuln 场景不触碰，加性表无副作用。
CREATE TABLE IF NOT EXISTS coverage_slots (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    slot_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    -- status: pending=待探索；exploring=已派 intent 正在跑；explored=已产出 fact；
    -- verified=已复现确认（Phase 3）；failed=探索/复现失败；skipped=人工/B/D 收尾跳过。
    status TEXT NOT NULL DEFAULT 'pending',
    -- tier: core=bootstrap 首轮枚举（完成门必覆盖）；extended=过程发现（不卡完成候选）。
    tier TEXT NOT NULL DEFAULT 'extended',
    source_intent_id TEXT,
    assigned_intent_id TEXT,
    resulting_fact_id TEXT,
    verified_run_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, slot_key)
);

CREATE INDEX IF NOT EXISTS idx_coverage_slots_project_status
ON coverage_slots (project_id, status);

-- 漏洞管理：所有场景通用。worker 输出 fact 带 vulnerability 字段时自动确认入库；
-- pentest slot verified 时升级 confidence=1.0/status=confirmed；支持人工独立创建。
-- 加性表，不输出 vulnerability 字段的场景无人读写，无副作用。
CREATE TABLE IF NOT EXISTS vulnerabilities (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    severity TEXT NOT NULL,
    vuln_type TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '',
    -- confidence 0-1 可信度；status: new/confirmed/dismissed/remediated。
    confidence REAL NOT NULL DEFAULT 0.5,
    status TEXT NOT NULL DEFAULT 'new',
    -- source_fact_id 来源 fact（NULL=人工独立创建）；packet_artifact_id 复现数据包 artifact。
    source_fact_id TEXT,
    packet_artifact_id TEXT,
    remediation TEXT NOT NULL DEFAULT '',
    -- repro_request/repro_response: worker 内联返回的原始 HTTP 请求/响应包（固定格式文本），
    -- 不依赖文件 artifact；packet_artifact_id 为空时仍可展示完整复现。
    repro_request TEXT NOT NULL DEFAULT '',
    repro_response TEXT NOT NULL DEFAULT '',
    -- repro_steps: 报告阶段生成的编号复现步骤（markdown），供人工按步复现。
    repro_steps TEXT NOT NULL DEFAULT '',
    discovered_by_run_id TEXT,
    discovered_by_worker TEXT,
    -- slot_key: pentest 场景关联的 coverage_slot（NULL=非 slot 驱动）。
    slot_key TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id),
    FOREIGN KEY (source_fact_id, project_id) REFERENCES facts(id, project_id) ON DELETE SET NULL,
    FOREIGN KEY (packet_artifact_id, project_id) REFERENCES artifacts(id, project_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_vulnerabilities_project_status
ON vulnerabilities (project_id, status);

CREATE INDEX IF NOT EXISTS idx_vulnerabilities_project_severity
ON vulnerabilities (project_id, severity);

CREATE INDEX IF NOT EXISTS idx_vulnerabilities_fact
ON vulnerabilities (project_id, source_fact_id);

INSERT INTO counters (name, value) VALUES ('vulnerability', 0) ON CONFLICT DO NOTHING;
"""


EXECUTION_EVENTS_TABLE_SCHEMA = """\
CREATE TABLE IF NOT EXISTS execution_events (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    task_run_id TEXT NOT NULL,
    protocol TEXT NOT NULL CHECK (protocol = 'cairn-execution-event-v1'),
    producer_event_key TEXT NOT NULL,
    event_kind TEXT NOT NULL CHECK (event_kind = 'checkpoint'),
    phase TEXT NOT NULL,
    category TEXT NOT NULL CHECK (
        category IN ('progress', 'observation', 'hypothesis', 'limitation', 'handoff')
    ),
    producer_recorded_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    verified_dispatcher_id TEXT NOT NULL,
    execution_generation INTEGER NOT NULL CHECK (execution_generation >= 0),
    generation_disposition TEXT NOT NULL CHECK (
        generation_disposition IN ('current_at_receipt', 'historical_at_receipt')
    ),
    payload_digest_sha256 TEXT NOT NULL CHECK (length(payload_digest_sha256) = 64),
    payload_json TEXT NOT NULL,
    PRIMARY KEY (id, project_id),
    UNIQUE (project_id, task_run_id, producer_event_key),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (task_run_id, project_id) REFERENCES task_runs(id, project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_execution_events_project_received
ON execution_events (project_id, received_at, id);

CREATE INDEX IF NOT EXISTS idx_execution_events_task_received
ON execution_events (project_id, task_run_id, received_at, id);

CREATE INDEX IF NOT EXISTS idx_execution_events_generation
ON execution_events (project_id, execution_generation, received_at, id);
"""


SQLITE_EXECUTION_EVENTS_APPEND_ONLY_SCHEMA = """\
CREATE TRIGGER IF NOT EXISTS trg_execution_events_no_update
BEFORE UPDATE ON execution_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'execution_events are append-only');
END;
"""


ASSESSMENT_REPORT_FINISH_RECEIPTS_TABLE_SCHEMA = """\
CREATE TABLE IF NOT EXISTS assessment_report_finish_receipts (
    project_id TEXT NOT NULL,
    task_run_id TEXT NOT NULL,
    protocol TEXT NOT NULL CHECK (protocol = 'cairn-assessment-report-finish-v1'),
    generation_token TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    request_json TEXT NOT NULL,
    response_json TEXT NOT NULL,
    artifact_id TEXT,
    artifact_sha256 TEXT CHECK (
        artifact_sha256 IS NULL OR length(artifact_sha256) = 64
    ),
    input_snapshot_artifact_id TEXT,
    input_snapshot_sha256 TEXT CHECK (
        input_snapshot_sha256 IS NULL OR length(input_snapshot_sha256) = 64
    ),
    accepted_at TEXT NOT NULL,
    PRIMARY KEY (project_id, task_run_id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (task_run_id, project_id)
        REFERENCES task_runs(id, project_id) ON DELETE CASCADE,
    CHECK ((artifact_id IS NULL) = (artifact_sha256 IS NULL)),
    CHECK (
        (input_snapshot_artifact_id IS NULL) =
        (input_snapshot_sha256 IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_assessment_report_finish_receipts_generation
ON assessment_report_finish_receipts(project_id, generation_token, accepted_at);
"""


SQLITE_ASSESSMENT_REPORT_FINISH_RECEIPTS_IMMUTABLE_SCHEMA = """\
CREATE TRIGGER IF NOT EXISTS trg_assessment_report_finish_receipts_no_update
BEFORE UPDATE ON assessment_report_finish_receipts
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'assessment_report_finish_receipts are immutable');
END;
"""


ASSESSMENT_CHARGE_COMPENSATIONS_TABLE_SCHEMA = """\
CREATE TABLE IF NOT EXISTS assessment_charge_compensations (
    project_id TEXT NOT NULL,
    assessment_attempt_id TEXT NOT NULL,
    migration_version INTEGER NOT NULL CHECK (migration_version = 39),
    predicate_version TEXT NOT NULL CHECK (
        predicate_version = 'pre-execution-missing-structured-cell-v1'
    ),
    reason_code TEXT NOT NULL CHECK (
        reason_code = 'structured_explore_missing_slot_pre_execution'
    ),
    original_charged INTEGER NOT NULL CHECK (original_charged = 1),
    charge_delta INTEGER NOT NULL CHECK (charge_delta = -1),
    project_counter_before INTEGER NOT NULL CHECK (project_counter_before > 0),
    project_counter_after INTEGER NOT NULL CHECK (
        project_counter_after >= 0
        AND project_counter_after = project_counter_before - 1
    ),
    source_reason_task_run_id TEXT NOT NULL,
    task_run_ids_json TEXT NOT NULL,
    proof_sha256 TEXT NOT NULL CHECK (length(proof_sha256) = 64),
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, assessment_attempt_id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_assessment_charge_compensations_created
ON assessment_charge_compensations(project_id, created_at, assessment_attempt_id);
"""


SQLITE_ASSESSMENT_CHARGE_COMPENSATIONS_IMMUTABLE_SCHEMA = """\
CREATE TRIGGER IF NOT EXISTS trg_assessment_charge_compensations_no_update
BEFORE UPDATE ON assessment_charge_compensations
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'assessment_charge_compensations are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_assessment_charge_compensations_no_delete
BEFORE DELETE ON assessment_charge_compensations
FOR EACH ROW
WHEN EXISTS (SELECT 1 FROM projects WHERE id = OLD.project_id)
BEGIN
    SELECT RAISE(ABORT, 'assessment_charge_compensations are immutable');
END;
"""


PROJECT_AUTHORIZATION_GRANTS_TABLE_SCHEMA = """\
CREATE TABLE IF NOT EXISTS project_authorization_grants (
    project_id TEXT NOT NULL,
    authorization_epoch INTEGER NOT NULL CHECK (authorization_epoch >= 1),
    protocol TEXT NOT NULL CHECK (protocol = 'cairn-project-authorization-v1'),
    source TEXT NOT NULL CHECK (
        source IN ('project_creation', 'explicit_reauthorization', 'legacy_backfill')
    ),
    actor_type TEXT NOT NULL CHECK (actor_type IN ('user', 'system')),
    actor_id TEXT NOT NULL,
    scope_digest TEXT NOT NULL CHECK (
        length(scope_digest) = 84
        AND scope_digest LIKE 'authorization-scope:%'
    ),
    scope_descriptor_json TEXT NOT NULL,
    window_seconds INTEGER NOT NULL CHECK (window_seconds > 0),
    previous_deadline TEXT,
    new_deadline TEXT NOT NULL,
    execution_generation INTEGER NOT NULL CHECK (execution_generation >= 0),
    authorized_at TEXT NOT NULL,
    PRIMARY KEY (project_id, authorization_epoch),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_project_authorization_grants_authorized
ON project_authorization_grants(project_id, authorized_at, authorization_epoch);
"""


SQLITE_PROJECT_AUTHORIZATION_GRANTS_IMMUTABLE_SCHEMA = """\
CREATE TRIGGER IF NOT EXISTS trg_project_authorization_grants_no_update
BEFORE UPDATE ON project_authorization_grants
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'project_authorization_grants are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_project_authorization_grants_no_delete
BEFORE DELETE ON project_authorization_grants
FOR EACH ROW
WHEN EXISTS (SELECT 1 FROM projects WHERE id = OLD.project_id)
BEGIN
    SELECT RAISE(ABORT, 'project_authorization_grants are immutable');
END;
"""


PROJECT_AUTHORIZATION_GRANTS_V2_TABLE_SCHEMA = """\
CREATE TABLE IF NOT EXISTS project_authorization_grants_v2 (
    project_id TEXT NOT NULL,
    authorization_epoch INTEGER NOT NULL CHECK (authorization_epoch >= 1),
    protocol TEXT NOT NULL CHECK (protocol = 'cairn-project-authorization-v2'),
    source TEXT NOT NULL CHECK (
        source IN ('project_creation', 'explicit_reauthorization')
    ),
    actor_type TEXT NOT NULL CHECK (actor_type IN ('user', 'system')),
    actor_id TEXT NOT NULL,
    scope_digest TEXT NOT NULL CHECK (
        length(scope_digest) = 84
        AND scope_digest LIKE 'authorization-scope:%'
    ),
    scope_descriptor_json TEXT NOT NULL,
    authorization_expires_at TEXT,
    execution_generation INTEGER NOT NULL CHECK (execution_generation >= 0),
    authorized_at TEXT NOT NULL,
    PRIMARY KEY (project_id, authorization_epoch),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_project_authorization_grants_v2_authorized
ON project_authorization_grants_v2(project_id, authorized_at, authorization_epoch);
"""


SQLITE_PROJECT_AUTHORIZATION_GRANTS_V2_IMMUTABLE_SCHEMA = """\
CREATE TRIGGER IF NOT EXISTS trg_project_authorization_grants_v2_no_update
BEFORE UPDATE ON project_authorization_grants_v2
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'project_authorization_grants_v2 are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_project_authorization_grants_v2_no_delete
BEFORE DELETE ON project_authorization_grants_v2
FOR EACH ROW
WHEN EXISTS (SELECT 1 FROM projects WHERE id = OLD.project_id)
BEGIN
    SELECT RAISE(ABORT, 'project_authorization_grants_v2 are immutable');
END;
"""


# v41 SQL 参与双后端历史 checksum，容量升级不能原地修改。
TASK_RUN_CANDIDATES_V41_TABLE_SCHEMA = """\
CREATE TABLE IF NOT EXISTS task_run_candidates (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    task_run_id TEXT NOT NULL,
    protocol TEXT NOT NULL CHECK (protocol = 'cairn-candidate-frame-v1'),
    source_format TEXT NOT NULL CHECK (source_format = 'pi-toolcall-delta-utf8-v1'),
    capture_id TEXT NOT NULL,
    producer_candidate_key TEXT NOT NULL,
    parent_candidate_id TEXT,
    assistant_turn INTEGER NOT NULL CHECK (assistant_turn BETWEEN 1 AND 1000000),
    content_index INTEGER NOT NULL CHECK (content_index BETWEEN 0 AND 1000000),
    phase TEXT NOT NULL,
    session_id TEXT NOT NULL,
    binding_sha256 TEXT NOT NULL CHECK (
        length(binding_sha256) = 64
        AND binding_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    result_schema_sha256 TEXT NOT NULL CHECK (
        length(result_schema_sha256) = 64
        AND result_schema_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    delivery_sha256 TEXT NOT NULL CHECK (
        length(delivery_sha256) = 64
        AND delivery_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    artifact_identities_json TEXT NOT NULL,
    artifact_count INTEGER NOT NULL CHECK (artifact_count BETWEEN 0 AND 16),
    raw_bytes BLOB NOT NULL,
    raw_size_bytes INTEGER NOT NULL CHECK (
        raw_size_bytes BETWEEN 0 AND 2097152
        AND length(raw_bytes) = raw_size_bytes
    ),
    raw_sha256 TEXT NOT NULL CHECK (
        length(raw_sha256) = 64
        AND raw_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    request_sha256 TEXT NOT NULL CHECK (
        length(request_sha256) = 64
        AND request_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    manifest_json TEXT NOT NULL CHECK (
        length(CAST(manifest_json AS BLOB)) BETWEEN 2 AND 4096
    ),
    verified_dispatcher_id TEXT NOT NULL,
    execution_generation INTEGER NOT NULL CHECK (execution_generation >= 0),
    generation_disposition TEXT NOT NULL CHECK (
        generation_disposition IN ('current_at_receipt', 'historical_at_receipt')
    ),
    received_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id),
    UNIQUE (id, project_id, task_run_id),
    UNIQUE (project_id, task_run_id, producer_candidate_key),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (task_run_id, project_id)
        REFERENCES task_runs(id, project_id) ON DELETE CASCADE,
    FOREIGN KEY (parent_candidate_id, project_id, task_run_id)
        REFERENCES task_run_candidates(id, project_id, task_run_id)
);

CREATE INDEX IF NOT EXISTS idx_task_run_candidates_task_received
ON task_run_candidates(project_id, task_run_id, received_at, id);

CREATE INDEX IF NOT EXISTS idx_task_run_candidates_generation
ON task_run_candidates(project_id, execution_generation, received_at, id);

CREATE TABLE IF NOT EXISTS task_run_candidate_outcomes (
    id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    task_run_id TEXT NOT NULL,
    protocol TEXT NOT NULL CHECK (protocol = 'cairn-candidate-frame-v1'),
    producer_candidate_key TEXT NOT NULL,
    outcome_sequence INTEGER NOT NULL CHECK (outcome_sequence BETWEEN 1 AND 64),
    code TEXT NOT NULL,
    diagnostic_path TEXT,
    diagnostic_keyword TEXT,
    diagnostic_count INTEGER CHECK (
        diagnostic_count IS NULL OR diagnostic_count BETWEEN 0 AND 1000000
    ),
    artifact_status TEXT NOT NULL CHECK (
        artifact_status IN ('not_evaluated', 'validated', 'rejected')
    ),
    binding_status TEXT NOT NULL CHECK (
        binding_status IN ('not_evaluated', 'not_required', 'injected', 'rejected')
    ),
    request_sha256 TEXT NOT NULL CHECK (
        length(request_sha256) = 64
        AND request_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    manifest_json TEXT NOT NULL CHECK (
        length(CAST(manifest_json AS BLOB)) BETWEEN 2 AND 4096
    ),
    verified_dispatcher_id TEXT NOT NULL,
    execution_generation INTEGER NOT NULL CHECK (execution_generation >= 0),
    generation_disposition TEXT NOT NULL CHECK (
        generation_disposition IN ('current_at_receipt', 'historical_at_receipt')
    ),
    received_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id),
    UNIQUE (candidate_id, outcome_sequence),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (task_run_id, project_id)
        REFERENCES task_runs(id, project_id) ON DELETE CASCADE,
    FOREIGN KEY (candidate_id, project_id, task_run_id)
        REFERENCES task_run_candidates(id, project_id, task_run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_task_run_candidate_outcomes_candidate
ON task_run_candidate_outcomes(candidate_id, outcome_sequence);
"""


TASK_RUN_CANDIDATES_V47_TABLE_SCHEMA = (
    TASK_RUN_CANDIDATES_V41_TABLE_SCHEMA
    .replace("artifact_count BETWEEN 0 AND 16", "artifact_count BETWEEN 0 AND 32")
    .replace("BETWEEN 2 AND 4096", "BETWEEN 2 AND 16384")
)
TASK_RUN_CANDIDATES_TABLE_SCHEMA = TASK_RUN_CANDIDATES_V47_TABLE_SCHEMA


SQLITE_TASK_RUN_CANDIDATES_IMMUTABLE_SCHEMA = """\
CREATE TRIGGER IF NOT EXISTS trg_task_run_candidates_no_update
BEFORE UPDATE ON task_run_candidates
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'task_run_candidates are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_task_run_candidates_no_delete
BEFORE DELETE ON task_run_candidates
FOR EACH ROW
WHEN EXISTS (SELECT 1 FROM projects WHERE id = OLD.project_id)
BEGIN
    SELECT RAISE(ABORT, 'task_run_candidates are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_task_run_candidate_outcomes_no_update
BEFORE UPDATE ON task_run_candidate_outcomes
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'task_run_candidate_outcomes are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_task_run_candidate_outcomes_no_delete
BEFORE DELETE ON task_run_candidate_outcomes
FOR EACH ROW
WHEN EXISTS (SELECT 1 FROM projects WHERE id = OLD.project_id)
BEGIN
    SELECT RAISE(ABORT, 'task_run_candidate_outcomes are immutable');
END;
"""


VERIFICATION_REQUESTS_V50_TABLE_SCHEMA = """\
CREATE TABLE verification_integrations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    group_id TEXT REFERENCES project_groups(id) ON DELETE SET NULL,
    worker_template_id TEXT REFERENCES worker_templates(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT
);
CREATE INDEX ix_verification_integrations_tenant ON verification_integrations(tenant_id, created_at, id);
CREATE TABLE verification_requests (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    caller_type TEXT NOT NULL CHECK (caller_type IN ('user', 'integration')),
    caller_id TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    external_id TEXT,
    mode TEXT NOT NULL CHECK (mode IN ('reproduce', 'retest')),
    project_id TEXT NOT NULL UNIQUE REFERENCES projects(id),
    intent_id TEXT NOT NULL,
    report_type TEXT NOT NULL CHECK (report_type IN ('markdown', 'text', 'file')),
    report_resource_id TEXT NOT NULL,
    previous_request_id TEXT REFERENCES verification_requests(id),
    worker_template_id TEXT NOT NULL,
    current_task_run_id TEXT,
    claim_token TEXT,
    cancel_requested_at TEXT,
    terminal_kind TEXT CHECK (terminal_kind IN ('result', 'failure', 'cancelled')),
    terminal_digest TEXT,
    result_json TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    accepted_at TEXT,
    terminal_at TEXT,
    UNIQUE (tenant_id, caller_type, caller_id, client_request_id),
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id),
    FOREIGN KEY (report_resource_id, project_id) REFERENCES project_resources(id, project_id),
    FOREIGN KEY (current_task_run_id, project_id) REFERENCES task_runs(id, project_id),
    CHECK (
        (terminal_kind IS NULL AND result_json IS NULL AND error_json IS NULL AND terminal_at IS NULL)
        OR (terminal_kind = 'result' AND result_json IS NOT NULL AND error_json IS NULL AND terminal_at IS NOT NULL)
        OR (terminal_kind = 'failure' AND result_json IS NULL AND error_json IS NOT NULL AND terminal_at IS NOT NULL)
        OR (terminal_kind = 'cancelled' AND result_json IS NULL AND error_json IS NULL AND terminal_at IS NOT NULL)
    )
);
CREATE INDEX ix_verification_requests_owner ON verification_requests(tenant_id, caller_type, caller_id, created_at, id);
CREATE INDEX ix_verification_requests_external ON verification_requests(tenant_id, caller_type, caller_id, external_id, created_at);
ALTER TABLE project_resources ADD COLUMN derived_from_resource_id TEXT;
ALTER TABLE project_resource_batch_members ADD COLUMN source_resource_id TEXT;
"""


VERIFICATION_CONFIGURATION_V51_TABLE_SCHEMA = """\
CREATE TABLE verification_configuration (
    tenant_id TEXT PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    group_id TEXT REFERENCES project_groups(id) ON DELETE SET NULL,
    tenant_worker_id TEXT REFERENCES tenant_workers(id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL
);
"""


# 已发布 v50/v51 SQL 保持冻结，专用 Worker 与平台来源只由 v52 增量变更。
VERIFICATION_CONFIGURATION_V52_TABLE_SCHEMA = """\
CREATE TABLE verification_configuration (
    tenant_id TEXT PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    tenant_worker_id TEXT REFERENCES tenant_workers(id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL
);
"""
VERIFICATION_REQUESTS_V52_TABLE_SCHEMA = (
    ("CREATE TABLE verification_requests" + VERIFICATION_REQUESTS_V50_TABLE_SCHEMA.split("CREATE TABLE verification_requests", 1)[1])
    .split("ALTER TABLE project_resources", 1)[0]
    .replace("worker_template_id TEXT NOT NULL", "worker_template_id TEXT")
    .replace("report_type IN ('markdown', 'text', 'file')", "report_type IN ('markdown', 'text', 'file', 'platform_vulnerability')")
    .replace("    terminal_at TEXT,", "    terminal_at TEXT,\n    source_project_id TEXT,\n    source_vulnerability_id TEXT,")
    + "CREATE INDEX ix_verification_requests_source ON verification_requests(tenant_id, source_project_id, source_vulnerability_id, created_at);\n"
)


PROJECT_SCHEDULES_V48_TABLE_SCHEMA = """\
CREATE TABLE project_schedules (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    creator_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    timezone TEXT NOT NULL,
    start_at TEXT NOT NULL,
    local_time TEXT,
    weekdays_json TEXT NOT NULL DEFAULT '[]',
    end_date TEXT,
    max_runs INTEGER,
    next_run_at TEXT,
    project_id TEXT UNIQUE REFERENCES projects(id) ON DELETE SET NULL,
    last_project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    generated_count INTEGER NOT NULL DEFAULT 0,
    block_reason TEXT,
    project_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX ix_project_schedules_due ON project_schedules(status, next_run_at, id);
CREATE INDEX ix_project_schedules_tenant ON project_schedules(tenant_id, created_at, id);
CREATE INDEX ix_project_schedules_last_project ON project_schedules(last_project_id);

CREATE TABLE project_schedule_occurrences (
    id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL REFERENCES project_schedules(id),
    occurrence_key TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    project_id TEXT UNIQUE REFERENCES projects(id) ON DELETE SET NULL,
    config_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    UNIQUE (schedule_id, occurrence_key)
);
CREATE INDEX ix_project_schedule_occurrences_history
ON project_schedule_occurrences(schedule_id, processed_at, id);

CREATE TABLE creation_requests (
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    schedule_id TEXT REFERENCES project_schedules(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, user_id, operation, request_id)
);

CREATE TABLE schedule_resources (
    id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL REFERENCES project_schedules(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('note', 'script', 'file')),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    digest_sha256 TEXT NOT NULL,
    source_name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (schedule_id, path)
);

CREATE TABLE project_schedule_libraries (
    schedule_id TEXT NOT NULL REFERENCES project_schedules(id) ON DELETE CASCADE,
    library_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    digest_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (schedule_id, library_key),
    FOREIGN KEY (library_key, version)
        REFERENCES advisory_library_versions(library_key, version)
);
"""


# 轻量隔离测试直接执行 db.SCHEMA；迁移基线仍单独引用上面的冻结 SCHEMA。
_CURRENT_BASE_SCHEMA = (
    SCHEMA.replace(
        "    explore_budget INTEGER,\n    explore_deadline TEXT\n);",
        "    explore_budget INTEGER,\n    explore_deadline TEXT,\n"
        "    authorization_expires_at TEXT\n);",
        1,
    ).replace(
        "    depends_on TEXT,\n    -- slot_key",
        "    depends_on TEXT,\n"
        "    priority INTEGER NOT NULL DEFAULT 1 CHECK (priority IN (0, 1, 2)),\n"
        "    -- slot_key",
        1,
    )
)
FRESH_SCHEMA = (
    _CURRENT_BASE_SCHEMA
    + EXECUTION_EVENTS_TABLE_SCHEMA
    + SQLITE_EXECUTION_EVENTS_APPEND_ONLY_SCHEMA
    + ASSESSMENT_REPORT_FINISH_RECEIPTS_TABLE_SCHEMA
    + SQLITE_ASSESSMENT_REPORT_FINISH_RECEIPTS_IMMUTABLE_SCHEMA
    + ASSESSMENT_CHARGE_COMPENSATIONS_TABLE_SCHEMA
    + SQLITE_ASSESSMENT_CHARGE_COMPENSATIONS_IMMUTABLE_SCHEMA
    + PROJECT_AUTHORIZATION_GRANTS_TABLE_SCHEMA
    + SQLITE_PROJECT_AUTHORIZATION_GRANTS_IMMUTABLE_SCHEMA
    + PROJECT_AUTHORIZATION_GRANTS_V2_TABLE_SCHEMA
    + SQLITE_PROJECT_AUTHORIZATION_GRANTS_V2_IMMUTABLE_SCHEMA
    + TASK_RUN_CANDIDATES_TABLE_SCHEMA
    + SQLITE_TASK_RUN_CANDIDATES_IMMUTABLE_SCHEMA
    + PROJECT_SCHEDULES_V48_TABLE_SCHEMA
)
