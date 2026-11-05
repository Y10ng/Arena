"""会话研究沿用现有流量表；删除回收仅由新的显式删除动作建立。"""

SQL = """
ALTER TABLE agent_messages ADD COLUMN cancel_requested_at TEXT;
ALTER TABLE agent_conversations ADD COLUMN cleanup_request_json TEXT;
ALTER TABLE agent_conversations ADD COLUMN traffic_cursor BIGINT NOT NULL DEFAULT 0;

-- 保留原项目外键，先暂存依赖主键的约束，再为同一组表增加互斥的会话 owner。
CREATE TEMP TABLE conversation_traffic_fks ON COMMIT DROP AS
SELECT conrelid::regclass::text AS table_name, conname, pg_get_constraintdef(oid) AS definition
FROM pg_constraint WHERE contype = 'f' AND confrelid IN (
    'http_capture_scopes'::regclass, 'http_flows'::regclass,
    'http_flow_body_streams'::regclass
);
DO $$ DECLARE item RECORD; BEGIN
    FOR item IN SELECT * FROM conversation_traffic_fks LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', item.table_name, item.conname);
    END LOOP;
END $$;
DO $$ DECLARE item RECORD; keys TEXT; BEGIN
    FOR item IN SELECT * FROM (VALUES
        ('http_capture_scopes', 'id, project_id', 'owner_key, id'),
        ('http_flows', 'id, project_id', 'owner_key, id'),
        ('http_flow_receipts', 'project_id, flow_id, revision', 'owner_key, flow_id, revision'),
        ('http_flow_bodies', 'project_id, flow_id, side, revision', 'owner_key, flow_id, side, revision'),
        ('http_flow_body_streams', 'project_id, flow_id, side', 'owner_key, flow_id, side'),
        ('http_flow_body_chunks', 'project_id, flow_id, side, byte_offset', 'owner_key, flow_id, side, byte_offset')
    ) AS tables(name, old_key, new_key) LOOP
        EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', item.name, item.name || '_pkey');
        EXECUTE format('ALTER TABLE %I ALTER COLUMN project_id DROP NOT NULL', item.name);
        EXECUTE format('ALTER TABLE %I ADD COLUMN conversation_id TEXT REFERENCES agent_conversations(id)', item.name);
        EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I CHECK ((project_id IS NULL) <> (conversation_id IS NULL))', item.name, item.name || '_exact_owner');
        EXECUTE format('ALTER TABLE %I ADD COLUMN owner_key TEXT GENERATED ALWAYS AS (CASE WHEN project_id IS NOT NULL THEN ''p:'' || project_id ELSE ''c:'' || conversation_id END) STORED', item.name);
        EXECUTE format('ALTER TABLE %I ADD PRIMARY KEY (%s)', item.name, item.new_key);
        EXECUTE format('ALTER TABLE %I ADD UNIQUE (%s)', item.name, item.old_key);
    END LOOP;
END $$;
ALTER TABLE http_capture_scopes ADD UNIQUE (id, conversation_id);
ALTER TABLE http_flows ADD UNIQUE (id, conversation_id);
ALTER TABLE http_flow_body_streams ADD UNIQUE (conversation_id, flow_id, side);
DO $$ DECLARE item RECORD; BEGIN
    FOR item IN SELECT * FROM conversation_traffic_fks LOOP
        EXECUTE format('ALTER TABLE %s ADD CONSTRAINT %I %s', item.table_name, item.conname, item.definition);
    END LOOP;
END $$;
ALTER TABLE http_capture_scopes ADD FOREIGN KEY (task_run_id, conversation_id) REFERENCES task_runs(id, conversation_id);
ALTER TABLE http_flows ADD FOREIGN KEY (capture_scope_id, conversation_id) REFERENCES http_capture_scopes(id, conversation_id);
ALTER TABLE http_flow_receipts ADD FOREIGN KEY (flow_id, conversation_id) REFERENCES http_flows(id, conversation_id);
ALTER TABLE http_flow_bodies ADD FOREIGN KEY (flow_id, conversation_id) REFERENCES http_flows(id, conversation_id);
ALTER TABLE http_flow_bodies ADD FOREIGN KEY (artifact_id, conversation_id) REFERENCES artifacts(id, conversation_id);
ALTER TABLE http_flow_body_streams ADD FOREIGN KEY (flow_id, conversation_id) REFERENCES http_flows(id, conversation_id);
ALTER TABLE http_flow_body_chunks ADD FOREIGN KEY (conversation_id, flow_id, side) REFERENCES http_flow_body_streams(conversation_id, flow_id, side);
CREATE INDEX idx_http_flows_owner_created ON http_flows(owner_key, created_cursor);
CREATE INDEX idx_http_flows_owner_updated ON http_flows(owner_key, updated_cursor);
CREATE INDEX idx_http_capture_scopes_conversation ON http_capture_scopes(conversation_id, task_run_id);
"""
