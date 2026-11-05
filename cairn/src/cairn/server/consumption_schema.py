"""消费事实独立于业务对象寿命；仅用于 PostgreSQL。"""

POSTGRES_CONSUMPTION_V83 = """
ALTER TABLE tenant_models ADD COLUMN pricing_json TEXT NOT NULL DEFAULT '{}';
CREATE TABLE model_consumption_rates (
    tenant_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    model_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, model_id, revision)
);
CREATE TABLE model_consumption_bindings (
    id TEXT PRIMARY KEY,
    tenant_id TEXT,
    owner_id TEXT NOT NULL,
    source TEXT NOT NULL,
    task_run_id TEXT,
    project_id TEXT,
    conversation_id TEXT,
    model_json TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    historical BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX consumption_binding_run ON model_consumption_bindings(task_run_id);
CREATE TABLE model_consumption (
    binding_id TEXT NOT NULL REFERENCES model_consumption_bindings(id),
    response_seq INTEGER NOT NULL CHECK (response_seq >= 0),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    receipt_sha256 TEXT NOT NULL,
    occurred_at TIMESTAMPTZ,
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    time_basis TEXT NOT NULL,
    input_tokens BIGINT CHECK (input_tokens >= 0),
    output_tokens BIGINT CHECK (output_tokens >= 0),
    cache_read_tokens BIGINT CHECK (cache_read_tokens >= 0),
    cache_write_tokens BIGINT CHECK (cache_write_tokens >= 0),
    measurement TEXT NOT NULL,
    missing_responses INTEGER NOT NULL DEFAULT 0,
    currency TEXT,
    amount NUMERIC,
    unpriced BOOLEAN NOT NULL,
    PRIMARY KEY (binding_id, response_seq)
);
CREATE INDEX consumption_received ON model_consumption(received_at);
CREATE INDEX consumption_occurred ON model_consumption(occurred_at);
CREATE INDEX consumption_tenant_time ON model_consumption_bindings(tenant_id, created_at);
"""


def import_chat_usage(conn):
    # 只导入切换前已终态的结构化原件；不扫描 stdout，不把汇总结束日冒充响应日。
    rows = conn.execute("""SELECT t.id, t.record_id, t.project_id, t.conversation_id, t.runtime_context_json, c.tenant_id,
        c.created_by FROM task_runs t JOIN agent_conversations c ON c.id=t.conversation_id
        WHERE t.task_type='agent_chat' AND t.status NOT IN ('queued','running')
        AND jsonb_typeof(t.runtime_context_json::jsonb -> 'usage') = 'object'
        AND t.runtime_context_json::jsonb ->> 'consumption_source' IS DISTINCT FROM 'receipts-v1'
        AND NOT EXISTS (SELECT 1 FROM model_consumption_bindings b
            WHERE b.task_run_id=t.id AND b.project_id IS NOT DISTINCT FROM t.project_id
            AND b.conversation_id IS NOT DISTINCT FROM t.conversation_id AND NOT b.historical)""").fetchall()
    from json import loads, dumps
    from cairn.server.services.consumption import save_response
    for row in rows:
        context = loads(row['runtime_context_json'])
        identifier = 'historical:' + str(row['record_id'])
        model = context.get('execution_binding', {})
        model = {key: model[key] for key in ('model_id', 'model_revision', 'model_name', 'protocol', 'display_name') if key in model}
        model['pricing'] = {}
        conn.execute("""INSERT INTO model_consumption_bindings
            (id,tenant_id,owner_id,source,task_run_id,project_id,conversation_id,model_json,historical)
            VALUES (?,?,?,?,?,?,?,?,TRUE) ON CONFLICT DO NOTHING""",
            (identifier,row['tenant_id'],row['created_by'],'agent_chat',row['id'],row['project_id'],row['conversation_id'],dumps(model)))
        save_response(conn, identifier, {
            'response_seq': 0, 'revision': 1, 'occurred_at': None, 'time_basis': 'unknown_historical',
            'usage': context['usage'], 'measurement': 'historical_summary',
            'missing_responses': context.get('usage_missing_responses') or 0,
        })
