import test from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { parseRequest } from '../dist/protocol.js';
import { runRequest } from '../dist/runner.js';
import { PROJECT_TOOL_NAMES } from '../dist/project-tools.js';
import { fixture } from './fixtures.mjs';

test('legacy request followed by metered request preserves a temporary session', async t => {
  const { request, calls } = await fixture(t, 'openai-chat-completions', [
    { text: '受控首轮回复' }, { text: '受控续轮回复' },
  ]);
  const projectTools = {
    protocol: 'cairn-project-tools-v1',
    socket_path: `/tmp/cairn-prompts/pi-project-${'a'.repeat(32)}.sock`,
    workspace_path: join(request.cwd, 'conversation'),
    research_execution: { protocol: 'cairn-conversation-research-v1', conversation_id: 'conversation', task_run_id: request.task_run_id },
    forbidden_literals: ['fixture-worker-secret'], allowed_tools: PROJECT_TOOL_NAMES,
  };
  const first = parseRequest({ ...request, conversation_id: 'conversation', project_id: null,
    phase: 'chat_turn', binding: null, max_result_repairs: 0, result_schema: {},
    capabilities: { project_tools: projectTools } });
  const initial = await runRequest(first);
  assert.equal(initial.status, 'succeeded');
  const sessionPath = join(first.session_root, first.session.slice(3), 'session.jsonl');
  const before = await readFile(sessionPath, 'utf8');
  const id = randomUUID();
  const resumed = parseRequest({ ...first, task_run_id: 'task-next', resume: true, prompt: '受控续轮',
    capabilities: {
      project_tools: { ...projectTools, research_execution: { ...projectTools.research_execution, task_run_id: 'task-next' } },
      consumption: { protocol: 'cairn-consumption-v1', id, spool_path: `/tmp/cairn-prompts/pi-consumption-${id}.jsonl` },
    },
  });
  // 正式 parser 接受规范路径后，仅测试将 spool 替换为临时目录内的文件。
  const spool = join(first.session_root, 'controlled-usage.jsonl');
  await writeFile(spool, '', { flag: 'wx', mode: 0o600 });
  resumed.capabilities.consumption.spool_path = spool;
  const audit = [];
  const next = await runRequest(resumed, { audit: event => audit.push(event) });
  assert.equal(next.status, 'succeeded');
  assert.deepEqual(next.result, { text: '受控续轮回复' });
  assert.equal(calls.length, 2);
  assert.match(JSON.stringify(calls[1].body.messages), /受控首轮回复/);
  const after = await readFile(sessionPath, 'utf8');
  assert.ok(after.startsWith(before));
  const entries = after.trimEnd().split('\n').map(JSON.parse);
  assert.equal(entries.filter(entry => entry.type === 'session').length, 1);
  const receipts = (await readFile(spool, 'utf8')).trimEnd().split('\n').map(JSON.parse);
  assert.ok(receipts.some(row => row.usage.input_tokens === 10 && row.usage.output_tokens === 5));
  assert.equal(audit.filter(event => event.event === 'consumption_gap').length, 0);
});
