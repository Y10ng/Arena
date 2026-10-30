import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:net';
import { join } from 'node:path';
import { mkdir, readFile, symlink, writeFile } from 'node:fs/promises';
import { createChatFileTools } from '../dist/chat-workspace.js';
import { runRequest } from '../dist/runner.js';
import { parseRequest } from '../dist/protocol.js';
import { CHAT_SYSTEM_PROMPT, createProjectTools } from '../dist/project-tools.js';
import { fixture } from './fixtures.mjs';

function chatRequest(request) {
  return parseRequest({ ...request, phase: 'chat_turn', binding: null, max_result_repairs: 0, result_schema: {}, capabilities: {
    project_tools: { protocol: 'cairn-project-tools-v1', socket_path: `/tmp/cairn-prompts/pi-project-${'a'.repeat(32)}.sock`, workspace_path: join(request.cwd, 'conversation'), forbidden_literals: ['fixture-worker-secret'] },
  } });
}

test('chat naturally completes then resumes the same session through a real private project tool', async (t) => {
  const { request, calls } = await fixture(t, 'openai-chat-completions', [
    { text: '你好，请问需要查询什么？', thinking: 'private-thinking-marker' },
    { text: '内部工具调用旁白', tools: [{ name: 'query_project_progress', args: {} }] },
    { text: '当前项目已完成，已确认一个漏洞。', thinking: 'private-thinking-marker' },
  ]);
  const first = chatRequest(request);
  const audit = [];
  const answer = await runRequest(first, { audit: (event) => audit.push(event) });
  assert.equal(answer.status, 'succeeded', JSON.stringify(answer));
  assert.deepEqual(answer.result, { text: '你好，请问需要查询什么？' });
  assert.equal(answer.submission_id, null);
  assert.equal(calls.length, 1);
  const names = calls[0].body.tools.map((tool) => tool.function.name);
  assert.ok(['bash', 'read', 'write', 'edit', 'list_conversation_attachments', 'publish_custom_markdown', 'create_project', 'append_project_intent'].every((name) => names.includes(name)));
  assert.ok(!names.includes('submit_result'));
  const received = [];
  const path = join(request.session_root, 'project.sock');
  const server = createServer((socket) => {
    socket.once('data', (data) => {
      received.push(JSON.parse(data.toString()));
      socket.end(JSON.stringify({ ok: true, data: { status: 'completed', vulnerability_count: 1 } }) + '\n');
    });
  });
  await new Promise((resolve) => server.listen(path, resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const resumed = chatRequest({ ...request, task_run_id: 'task-next', resume: true, prompt: '查询进展' });
  // 正式parser校验私有路径后，仅测试传输改用临时Unix socket。
  resumed.capabilities.project_tools.socket_path = path;
  const next = await runRequest(resumed, { audit: (event) => audit.push(event) });
  assert.equal(next.status, 'succeeded', JSON.stringify(next));
  assert.deepEqual(next.result, { text: '当前项目已完成，已确认一个漏洞。' });
  assert.deepEqual(received, [{ method: 'query_project_progress', params: {}, tool_call_id: 'call_2_0' }]);
  assert.equal(calls.length, 3);
  assert.match(JSON.stringify(calls[1].body.messages), /你好/);
  assert.ok(!JSON.stringify([answer, next, audit]).includes('private-thinking-marker'));
  assert.ok(!JSON.stringify([answer, next, audit]).includes('内部工具调用旁白'));
  assert.equal(audit.filter((event) => event.event === 'result_repair').length, 0);
});

test('R2a tools preserve action identity and expose public business errors through the private socket', async (t) => {
  const { request } = await fixture(t);
  const path = join(request.session_root, 'r2a.sock');
  const received = [];
  const businessError = { code: 'verification_config_missing', message: '请先配置复测模型。', action_url: '/settings/verification' };
  const server = createServer((socket) => {
    socket.once('data', (raw) => {
      const call = JSON.parse(raw.toString());
      received.push(call);
      const data = call.params.action_id === 'missing'
        ? { error: businessError }
        : { action_id: 'action-1', work: { status: 'queued' } };
      socket.end(JSON.stringify({ ok: true, data }) + '\n');
    });
  });
  await new Promise((resolve) => server.listen(path, resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const tools = createProjectTools({ protocol: 'cairn-project-tools-v1', socket_path: path, forbidden_literals: [] }, () => {});
  const cases = [
    ['request_vulnerability_retest', { vulnerability_id: 'v001', observation_id: 'o002' }],
    ['request_vulnerability_retest', { vulnerability_id: 'v001', observation_id: 'o002', target_url: 'https://new.example.test/instance' }],
    ['request_project_report', {}],
    ['query_agent_work', { action_id: 'action-1' }],
  ];
  for (const [name, params] of cases) {
    const tool = tools.find((item) => item.name === name);
    const args = tool.prepareArguments(params);
    const result = await tool.execute('sdk-call-1', args);
    assert.deepEqual(JSON.parse(result.content[0].text), { action_id: 'action-1', work: { status: 'queued' } });
    assert.deepEqual(received.at(-1), { method: name, params, tool_call_id: 'sdk-call-1' });
  }
  const query = tools.find((item) => item.name === 'query_agent_work');
  await assert.rejects(query.execute('sdk-error', { action_id: 'missing' }), (error) => {
    assert.equal(error.code, businessError.code);
    assert.deepEqual(JSON.parse(error.message), businessError);
    return true;
  });
});

test('chat blocks complete protected output and does not retry the model', async (t) => {
  for (const text of ['fixture-secret-key', 'fixture-header-value', 'fixture-worker-secret', CHAT_SYSTEM_PROMPT]) {
    await t.test(text === CHAT_SYSTEM_PROMPT ? 'system prompt' : 'synthetic secret', async (t) => {
      const { request, calls } = await fixture(t, 'openai-chat-completions', [{ text }]);
      const result = await runRequest(chatRequest(request));
      assert.equal(result.status, 'failed');
      assert.equal(result.error.code, 'public_output_forbidden');
      assert.equal(result.result, null);
      assert.equal(calls.length, 1);
      assert.ok(!JSON.stringify(result).includes(text));
    });
  }
});

test('chat runner uses conversation files and bridges bash under its real SDK call id', async (t) => {
  const { request } = await fixture(t, 'openai-chat-completions', [
    { tools: [{ name: 'write', args: { path: 'report.md', content: '# 用户报告\n' } }] },
    { tools: [{ name: 'bash', args: { command: 'wc -c report.md' } }] },
    { tools: [{ name: 'read', args: { path: 'report.md' } }] },
    { text: '工作区文件已生成，尚未发布。' },
  ]);
  const chat = chatRequest(request);
  await mkdir(chat.capabilities.project_tools.workspace_path);
  const path = join(request.session_root, 'workspace.sock');
  const received = [];
  const server = createServer((socket) => socket.once('data', (raw) => {
    received.push(JSON.parse(raw.toString()));
    socket.end(JSON.stringify({ ok: true, data: { output: '15 report.md\n', exit_code: 0 } }) + '\n');
  }));
  await new Promise((resolve) => server.listen(path, resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  chat.capabilities.project_tools.socket_path = path;
  const result = await runRequest(chat);
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.equal(await readFile(join(chat.capabilities.project_tools.workspace_path, 'report.md'), 'utf8'), '# 用户报告\n');
  assert.equal(received.length, 1);
  assert.equal(received[0].method, '_workspace_bash');
  assert.equal(received[0].tool_call_id, 'call_2_0');
  assert.equal(received[0].params.cwd, chat.capabilities.project_tools.workspace_path);
});

test('workspace files reject outside paths and dangling symlinks while preserving real Markdown', async (t) => {
  const { request } = await fixture(t);
  const cwd = join(request.cwd, 'conversation');
  await mkdir(cwd);
  const binding = { protocol: 'cairn-project-tools-v1', workspace_path: cwd, forbidden_literals: ['protected-fixture'], socket_path: '/unused' };
  const tools = createChatFileTools(cwd, binding, () => {});
  const write = tools.find((tool) => tool.name === 'write');
  const read = tools.find((tool) => tool.name === 'read');
  await write.execute('write-real', { path: 'report.md', content: '# 合成报告\n\n原始内容\n' });
  assert.equal(await readFile(join(cwd, 'report.md'), 'utf8'), '# 合成报告\n\n原始内容\n');
  assert.match(JSON.stringify(await read.execute('read-real', { path: 'report.md' })), /合成报告/);
  await symlink(join(request.session_root, 'absent'), join(cwd, 'escape'));
  await assert.rejects(write.execute('escape', { path: 'escape', content: 'bad' }), { code: 'workspace_path_invalid' });
  await assert.rejects(read.execute('outside', { path: request.session_root }), { code: 'workspace_path_invalid' });
  await assert.rejects(read.execute('at-prefix', { path: `@${request.session_root}` }), { code: 'workspace_path_invalid' });
  await assert.rejects(write.execute('home-prefix', { path: '~/must-not-write', content: 'bad' }), { code: 'workspace_path_invalid' });
  await writeFile(join(cwd, 'secret.txt'), 'protected-fixture');
  await assert.rejects(read.execute('secret', { path: 'secret.txt' }), { code: 'public_output_forbidden' });
});

test('chat cannot repurpose an existing batch session or register target tools', async (t) => {
  const { request } = await fixture(t);
  assert.equal((await runRequest(request)).status, 'succeeded');
  const resumed = chatRequest({ ...request, task_run_id: 'next', resume: true });
  assert.equal((await runRequest(resumed)).error.code, 'session_ownership');
  assert.throws(() => parseRequest({ ...resumed, capabilities: { ...resumed.capabilities, task_execution: request.capabilities.task_execution } }), { code: 'invalid_request' });
});
