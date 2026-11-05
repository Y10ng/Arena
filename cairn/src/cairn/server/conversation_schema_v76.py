"""会话直接归属迁移；旧运行来源与消息事实不改写，文件另行显式迁移。"""

SQL = """
ALTER TABLE agent_conversations ADD COLUMN tenant_id TEXT REFERENCES tenants(id);
UPDATE agent_conversations c SET tenant_id = p.tenant_id FROM projects p WHERE p.id = c.project_id;
ALTER TABLE agent_conversations ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE agent_conversations DROP CONSTRAINT agent_conversations_project_id_fkey;
ALTER TABLE agent_conversations ALTER COLUMN project_id DROP NOT NULL;
-- 来源标识保留为历史引用，不随项目删除清空或级联。
ALTER TABLE agent_conversations ADD COLUMN unavailable_reason TEXT;
ALTER TABLE agent_conversations ADD COLUMN session_initialized BOOLEAN DEFAULT FALSE;
ALTER TABLE agent_conversations ADD COLUMN session_machine_id TEXT;
UPDATE agent_conversations c SET session_initialized = NULL, unavailable_reason = 'session_verification_required',
    session_machine_id = (SELECT t.runtime_context_json::json->>'dispatcher_id' FROM task_runs t
        JOIN agent_messages m ON m.task_run_id = t.id AND m.project_id = t.project_id
        WHERE m.conversation_id = c.id ORDER BY m.id DESC LIMIT 1)
WHERE EXISTS (SELECT 1 FROM agent_messages m WHERE m.conversation_id = c.id AND m.task_run_id IS NOT NULL);

-- PostgreSQL 外键依赖的是原主键索引，不能仅先建同列 UNIQUE 后直接 DROP 主键。
CREATE TEMP TABLE conversation_owner_fks ON COMMIT DROP AS
SELECT conrelid::regclass::text AS relation, conname, pg_get_constraintdef(oid) AS definition
FROM pg_constraint WHERE contype = 'f' AND confrelid IN ('task_runs'::regclass, 'artifacts'::regclass);
DO $$ DECLARE item RECORD; BEGIN
    FOR item IN SELECT * FROM conversation_owner_fks LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', item.relation, item.conname);
    END LOOP;
END $$;
ALTER TABLE task_runs DROP CONSTRAINT task_runs_pkey;
ALTER TABLE task_runs ADD COLUMN record_id BIGSERIAL PRIMARY KEY;
ALTER TABLE task_runs ALTER COLUMN project_id DROP NOT NULL;
ALTER TABLE task_runs ADD COLUMN conversation_id TEXT REFERENCES agent_conversations(id);
ALTER TABLE task_runs ADD CONSTRAINT task_runs_project_key UNIQUE(id, project_id);
ALTER TABLE task_runs ADD CONSTRAINT task_runs_conversation_key UNIQUE(id, conversation_id);
ALTER TABLE artifacts DROP CONSTRAINT artifacts_pkey;
ALTER TABLE artifacts ADD COLUMN record_id BIGSERIAL PRIMARY KEY;
ALTER TABLE artifacts ALTER COLUMN project_id DROP NOT NULL;
ALTER TABLE artifacts ADD COLUMN conversation_id TEXT REFERENCES agent_conversations(id);
ALTER TABLE artifacts ADD CONSTRAINT artifacts_project_key UNIQUE(id, project_id);
ALTER TABLE artifacts ADD CONSTRAINT artifacts_conversation_key UNIQUE(id, conversation_id);
-- 保存原文件定位，只有完成摘要校验的显式文件迁移才能清除；读取没有旧路径 fallback。
ALTER TABLE artifacts ADD COLUMN migration_source_json TEXT;

ALTER TABLE agent_messages DROP CONSTRAINT agent_messages_conversation_id_project_id_fkey;
ALTER TABLE agent_messages ALTER COLUMN project_id DROP NOT NULL;
ALTER TABLE agent_messages ADD CONSTRAINT agent_messages_conversation_fkey
    FOREIGN KEY(conversation_id) REFERENCES agent_conversations(id) ON DELETE CASCADE;
ALTER TABLE agent_messages ADD CONSTRAINT agent_messages_conversation_run_key UNIQUE(conversation_id, task_run_id);
ALTER TABLE agent_messages ADD CONSTRAINT agent_messages_id_conversation_key UNIQUE(id, conversation_id);
ALTER TABLE agent_attachments DROP CONSTRAINT agent_attachments_message_id_fkey;
ALTER TABLE agent_attachments ADD CONSTRAINT agent_attachments_message_conversation_fkey
    FOREIGN KEY(message_id, conversation_id) REFERENCES agent_messages(id, conversation_id);
ALTER TABLE agent_messages ADD COLUMN claim_request_id TEXT;
ALTER TABLE agent_messages ADD CONSTRAINT agent_messages_claim_key UNIQUE(conversation_id, claim_request_id);
ALTER TABLE agent_attachments DROP CONSTRAINT agent_attachments_conversation_id_project_id_fkey;
ALTER TABLE agent_attachments DROP COLUMN project_id;
ALTER TABLE agent_attachments ADD CONSTRAINT agent_attachments_conversation_fkey
    FOREIGN KEY(conversation_id) REFERENCES agent_conversations(id);

-- 现有聊天 Markdown 发布确实写 agent_events；execution_events 没有聊天消费者，不泛化。
ALTER TABLE agent_events DROP CONSTRAINT agent_events_pkey;
ALTER TABLE agent_events ADD COLUMN record_id BIGSERIAL PRIMARY KEY;
ALTER TABLE agent_events ALTER COLUMN project_id DROP NOT NULL;
ALTER TABLE agent_events ADD COLUMN conversation_id TEXT REFERENCES agent_conversations(id);
ALTER TABLE agent_events ADD CONSTRAINT agent_events_project_key UNIQUE(id, project_id);
ALTER TABLE agent_events ADD CONSTRAINT agent_events_conversation_key UNIQUE(id, conversation_id);
ALTER TABLE agent_events ADD CONSTRAINT agent_events_conversation_run_fkey
    FOREIGN KEY(task_run_id, conversation_id) REFERENCES task_runs(id, conversation_id) ON DELETE CASCADE;

-- 消息是聊天运行唯一的实际消费者；无法明确对应的数据由约束拒绝，不猜测归属。
UPDATE task_runs t SET conversation_id = m.conversation_id
FROM agent_messages m WHERE t.id = m.task_run_id AND t.project_id = m.project_id AND t.task_type = 'agent_chat';
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM task_runs WHERE task_type = 'agent_chat' AND conversation_id IS NULL) THEN
        RAISE EXCEPTION 'conversation ownership migration requires a message for each chat run';
    END IF;
END $$;
UPDATE artifacts a SET conversation_id = t.conversation_id FROM task_runs t
WHERE a.task_run_id = t.id AND a.project_id = t.project_id AND t.conversation_id IS NOT NULL;
UPDATE artifacts a SET conversation_id = c.id FROM agent_attachments x JOIN agent_conversations c ON c.id = x.conversation_id
WHERE a.id = x.artifact_id AND a.project_id = c.project_id AND a.conversation_id IS NULL;
UPDATE artifacts SET migration_source_json = json_build_object('project_id', project_id, 'task_run_id', task_run_id, 'uri', uri)::text,
    uri = 'artifact://conversations/' || conversation_id || '/runs/' || COALESCE(task_run_id, 'unscoped') || '/' || regexp_replace(uri, '^.*/', ''),
    project_id = NULL WHERE conversation_id IS NOT NULL;
UPDATE agent_events e SET conversation_id = t.conversation_id, project_id = NULL FROM task_runs t
WHERE e.task_run_id = t.id AND e.project_id = t.project_id AND t.conversation_id IS NOT NULL;
UPDATE task_runs SET project_id = NULL WHERE conversation_id IS NOT NULL;

-- 消息 project_id 从此仅是不可变的材料上下文；研究外键按原定义逐一恢复并立即验证。
DELETE FROM conversation_owner_fks WHERE relation = 'agent_messages' OR relation = 'agent_attachments';
DO $$ DECLARE item RECORD; BEGIN
    FOR item IN SELECT * FROM conversation_owner_fks LOOP
        EXECUTE format('ALTER TABLE %s ADD CONSTRAINT %I %s', item.relation, item.conname, item.definition);
    END LOOP;
END $$;
ALTER TABLE task_runs ADD CONSTRAINT task_runs_exact_owner CHECK ((project_id IS NULL) <> (conversation_id IS NULL));
ALTER TABLE artifacts ADD CONSTRAINT artifacts_exact_owner CHECK ((project_id IS NULL) <> (conversation_id IS NULL));
ALTER TABLE agent_events ADD CONSTRAINT agent_events_exact_owner CHECK ((project_id IS NULL) <> (conversation_id IS NULL));
ALTER TABLE agent_messages ADD CONSTRAINT agent_messages_run_conversation_fkey
    FOREIGN KEY(task_run_id, conversation_id) REFERENCES task_runs(id, conversation_id);
ALTER TABLE agent_attachments ADD CONSTRAINT agent_attachments_artifact_conversation_fkey
    FOREIGN KEY(artifact_id, conversation_id) REFERENCES artifacts(id, conversation_id);
ALTER TABLE artifacts ADD CONSTRAINT artifacts_conversation_run_fkey
    FOREIGN KEY(task_run_id, conversation_id) REFERENCES task_runs(id, conversation_id) ON DELETE CASCADE;
"""
