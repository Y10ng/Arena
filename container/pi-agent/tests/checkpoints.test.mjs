import test from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { access, mkdir, open, readFile, rm, symlink, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { createCheckpointTool, createCheckpointWriter } from '../dist/checkpoints.js';
import { parseRequest } from '../dist/protocol.js';
import { runRequest } from '../dist/runner.js';
import { fixture, ok } from './fixtures.mjs';

const spoolRoot = '/tmp/cairn-prompts';
function spoolPath() { return join(spoolRoot, `pi-checkpoints-${randomUUID()}.jsonl`); }
function capability(path, overrides = {}) {
  return {
    protocol: 'cairn-execution-event-v1',
    spool_path: path,
    max_events: 64,
    max_event_bytes: 16 * 1024,
    max_spool_bytes: 512 * 1024,
    ...overrides,
  };
}

async function removeSpool(path) { await rm(path, { force: true }); }

test('checkpoint capability is optional and enforces compiled path and size bounds', async (t) => {
  const { request } = await fixture(t);
  assert.ok(parseRequest(request).capabilities.candidate_capture);
  const path = spoolPath();
  const parsed = parseRequest({ ...request, capabilities: { ...request.capabilities, execution_checkpoints: capability(path) } });
  assert.deepEqual(parsed.capabilities.execution_checkpoints, capability(path));
  for (const [field, value] of [['max_events', 65], ['max_event_bytes', 16 * 1024 + 1], ['max_spool_bytes', 512 * 1024 + 1], ['max_events', 0]]) {
    assert.throws(() => parseRequest({ ...request, capabilities: { ...request.capabilities, execution_checkpoints: capability(path, { [field]: value }) } }), { code: 'invalid_request', path: `capabilities.execution_checkpoints.${field}` });
  }
  assert.throws(() => parseRequest({ ...request, capabilities: { ...request.capabilities, execution_checkpoints: capability(join(request.cwd, 'checkpoints.jsonl')) } }), { code: 'invalid_request', path: 'capabilities.execution_checkpoints.spool_path' });
  assert.throws(() => parseRequest({ ...request, capabilities: { ...request.capabilities, execution_checkpoints: capability(path), unknown: true } }), { code: 'invalid_request', path: 'capabilities' });
  assert.throws(() => parseRequest({ ...request, capabilities: { ...request.capabilities, execution_checkpoints: capability(path, { unknown: true }) } }), { code: 'invalid_request', path: 'capabilities.execution_checkpoints' });
});

test('record_checkpoint is absent by default and capability adds runner-generated JSONL only', async (t) => {
  const without = await fixture(t, 'openai-chat-completions', [ok]);
  without.request.phase = 'explore_execute';
  assert.equal((await runRequest(without.request)).status, 'succeeded');
  assert.ok(!without.calls[0].body.tools.some((tool) => tool.function.name === 'record_checkpoint'));

  const report = await fixture(t, 'openai-chat-completions', [ok]);
  const unusedPath = spoolPath();
  report.request.phase = 'report_execute';
  report.request.capabilities = { ...report.request.capabilities, execution_checkpoints: capability(unusedPath) };
  assert.equal((await runRequest(parseRequest(report.request))).status, 'succeeded');
  assert.ok(!report.calls[0].body.tools.some((tool) => tool.function.name === 'record_checkpoint'));

  const canary = 'CHECKPOINT_BODY_CANARY_7db1';
  const path = spoolPath();
  t.after(() => removeSpool(path));
  const withCheckpoint = await fixture(t, 'openai-chat-completions', [
    { tools: [{ name: 'record_checkpoint', args: { category: 'observation', summary: canary, details: { outcome: 'bounded' } } }] },
    ok,
  ]);
  withCheckpoint.request.phase = 'explore_execute';
  withCheckpoint.request.capabilities = { ...withCheckpoint.request.capabilities, execution_checkpoints: capability(path) };
  const audit = [];
  const result = await runRequest(parseRequest(withCheckpoint.request), { audit: (event) => audit.push(event) });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.equal(withCheckpoint.calls.length, 2);
  assert.ok(withCheckpoint.calls[0].body.tools.some((tool) => tool.function.name === 'record_checkpoint'));
  const toolMessage = withCheckpoint.calls[1].body.messages.find((message) => message.role === 'tool');
  const localResult = JSON.parse(toolMessage.content);
  assert.equal(localResult.locally_spooled, true);
  assert.equal(localResult.scope, 'local_checkpoint_spool_only');
  assert.equal(localResult.server_received, false);
  assert.equal(localResult.business_accepted, false);
  const lines = (await readFile(path, 'utf8')).trimEnd().split('\n');
  assert.equal(lines.length, 1);
  const event = JSON.parse(lines[0]);
  assert.deepEqual(Object.keys(event).sort(), ['checkpoint', 'kind', 'phase', 'producer_event_key', 'protocol', 'recorded_at']);
  assert.equal(event.protocol, 'cairn-execution-event-v1');
  assert.equal(event.kind, 'checkpoint');
  assert.equal(event.phase, 'explore_execute');
  assert.match(event.producer_event_key, /^[0-9a-f-]{36}$/);
  assert.equal(new Date(event.recorded_at).toISOString(), event.recorded_at);
  assert.deepEqual(event.checkpoint, { category: 'observation', summary: canary, details: { outcome: 'bounded' } });
  const start = audit.find((entry) => entry.event === 'tool_execution_start' && entry.tool_name === 'record_checkpoint');
  const end = audit.find((entry) => entry.event === 'tool_execution_end' && entry.tool_name === 'record_checkpoint');
  assert.ok(start.tool_call_id);
  assert.equal(end.tool_call_id, start.tool_call_id);
  assert.ok(!JSON.stringify(audit).includes(canary));
});

test('automatic completion stays metadata-only, survives cancel-after-completion, and creates no candidate frames', async (t) => {
  const path = spoolPath();
  t.after(() => removeSpool(path));
  const commandCanary = 'COMMAND_CANARY_1d8668';
  const outputCanary = 'OUTPUT_CANARY_907ca1';
  const errorCanary = 'ERROR_CANARY_6090fb';
  const pathCanary = '/private/PATH_CANARY_57e4d0';
  const urlCanary = 'https://URL_CANARY.invalid/private';
  const modelCanary = 'MODEL_CANARY_0f045e';
  const command = `printf '${outputCanary} ${pathCanary} ${urlCanary}'; printf '${errorCanary}' >&2; test '${commandCanary}' = never`;
  const { request, calls, candidateSpoolPath } = await fixture(t, 'openai-chat-completions', [
    { tools: [{ name: 'bash', args: { command } }] },
    ok,
  ]);
  request.phase = 'explore_execute';
  request.model.model_name = modelCanary;
  request.capabilities = { ...request.capabilities, execution_checkpoints: capability(path) };
  const controller = new AbortController();
  const result = await runRequest(parseRequest(request), {
    signal: controller.signal,
    audit: (event) => {
      if (event.event === 'tool_execution_end' && event.tool_name === 'bash') controller.abort();
    },
  });
  assert.equal(result.status, 'cancelled', JSON.stringify(result));
  assert.equal(calls.length, 1);
  const lines = (await readFile(path, 'utf8')).trimEnd().split('\n');
  assert.equal(lines.length, 1);
  const event = JSON.parse(lines[0]);
  assert.deepEqual(Object.keys(event).sort(), ['checkpoint', 'kind', 'phase', 'producer_event_key', 'protocol', 'recorded_at']);
  assert.equal(event.kind, 'checkpoint');
  assert.match(event.producer_event_key, /^[0-9a-f-]{36}$/);
  assert.equal(event.checkpoint.category, 'progress');
  assert.equal(event.checkpoint.summary, 'Exploration tool completed');
  assert.deepEqual(Object.keys(event.checkpoint.details).sort(), ['event_code', 'event_version', 'input_bytes', 'is_error', 'output_bytes', 'sequence', 'source', 'tool_name']);
  assert.equal(event.checkpoint.details.event_version, 1);
  assert.equal(event.checkpoint.details.source, 'pi-agent');
  assert.equal(event.checkpoint.details.event_code, 'tool_execution_end');
  assert.equal(event.checkpoint.details.tool_name, 'bash');
  assert.equal(event.checkpoint.details.sequence, 1);
  assert.equal(event.checkpoint.details.is_error, true);
  assert.equal(event.checkpoint.details.input_bytes, Buffer.byteLength(JSON.stringify({ command })));
  assert.ok(Number.isInteger(event.checkpoint.details.output_bytes) && event.checkpoint.details.output_bytes > 0);
  const serialized = JSON.stringify(event);
  for (const canary of [commandCanary, outputCanary, errorCanary, pathCanary, urlCanary, modelCanary, request.model.inference_endpoint]) assert.ok(!serialized.includes(canary));
  await assert.rejects(access(candidateSpoolPath), { code: 'ENOENT' });
});

test('shared checkpoint writer fsyncs before explicit append succeeds', async (t) => {
  const path = spoolPath();
  await mkdir(spoolRoot, { recursive: true, mode: 0o700 });
  const probe = await open(path, 'a', 0o600);
  const fileHandlePrototype = Object.getPrototypeOf(probe);
  await probe.close();
  t.after(() => removeSpool(path));
  const originalSync = fileHandlePrototype.sync;
  let syncCalls = 0;
  fileHandlePrototype.sync = async function syncCheckpointAppend() {
    syncCalls += 1;
    return originalSync.call(this);
  };
  try {
    const writer = createCheckpointWriter(capability(path), 'explore_execute');
    const tool = createCheckpointTool(capability(path), 'explore_execute', () => {}, writer);
    await tool.execute('fsync', { category: 'progress', summary: 'durable', details: {} });
    assert.equal(syncCalls, 1);
  } finally {
    fileHandlePrototype.sync = originalSync;
  }
});

test('automatic quota serializes 47 completions plus one marker while preserving 16 explicit checkpoints', async (t) => {
  const path = spoolPath();
  t.after(() => removeSpool(path));
  const limits = capability(path);
  const writer = createCheckpointWriter(limits, 'explore_execute');
  await Promise.all(Array.from({ length: 60 }, (_, index) => writer.recordToolCompletion({
    tool_name: index % 2 === 0 ? 'bash' : 'read',
    sequence: index + 1,
    is_error: index % 3 === 0,
    input_bytes: index,
    output_bytes: index + 10,
  })));
  const tool = createCheckpointTool(limits, 'explore_execute', () => {}, writer);
  for (let index = 0; index < 16; index += 1) {
    await tool.execute(`explicit-${index}`, { category: 'progress', summary: `explicit ${index}`, details: {} });
  }
  await assert.rejects(tool.execute('explicit-overflow', { category: 'progress', summary: 'overflow', details: {} }), { code: 'checkpoint_limit' });

  const events = (await readFile(path, 'utf8')).trimEnd().split('\n').map((line) => JSON.parse(line));
  assert.equal(events.length, 64);
  const completions = events.filter((event) => event.checkpoint.details.event_code === 'tool_execution_end');
  const markers = events.filter((event) => event.checkpoint.details.event_code === 'tool_execution_truncated');
  const explicit = events.filter((event) => !Object.hasOwn(event.checkpoint.details, 'event_code'));
  assert.equal(completions.length, 47);
  assert.equal(markers.length, 1);
  assert.deepEqual(markers[0].checkpoint, {
    category: 'limitation',
    summary: 'Exploration tool completion observations truncated',
    details: { event_version: 1, source: 'pi-agent', event_code: 'tool_execution_truncated' },
  });
  assert.equal(explicit.length, 16);
  assert.deepEqual(explicit.map((event) => event.checkpoint.summary), Array.from({ length: 16 }, (_, index) => `explicit ${index}`));
});

test('checkpoint writer enforces summary, depth, event, spool and artifact-reference limits', async (t) => {
  const paths = [];
  t.after(async () => { for (const path of paths) await removeSpool(path); });
  const makeTool = (overrides = {}) => {
    const path = spoolPath();
    paths.push(path);
    return { path, tool: createCheckpointTool(capability(path, overrides), 'explore_execute', () => {}) };
  };
  let current = makeTool();
  await assert.rejects(current.tool.execute('summary', { category: 'progress', summary: 'x'.repeat(1001), details: {} }), { code: 'checkpoint_limit' });

  current = makeTool();
  await assert.rejects(current.tool.execute('blank-summary', { category: 'progress', summary: ' \t\n ', details: {} }), { code: 'checkpoint_invalid' });

  current = makeTool();
  await assert.rejects(current.tool.execute('detail-string', { category: 'observation', summary: 'bounded', details: { note: 'x'.repeat(4001) } }), {
    code: 'checkpoint_limit',
    message: 'Checkpoint detail string exceeds the limit',
  });

  current = makeTool();
  await assert.rejects(current.tool.execute('detail-nul', { category: 'observation', summary: 'bounded', details: { note: 'unsafe\0value' } }), {
    code: 'checkpoint_invalid',
    message: 'Checkpoint detail strings must not contain NUL',
  });

  current = makeTool();
  await assert.rejects(current.tool.execute('array-items', { category: 'observation', summary: 'bounded', details: { values: Array.from({ length: 65 }, () => 1) } }), {
    code: 'checkpoint_limit',
    message: 'Checkpoint detail collection exceeds the item limit',
  });

  current = makeTool();
  await assert.rejects(current.tool.execute('object-items', {
    category: 'observation',
    summary: 'bounded',
    details: Object.fromEntries(Array.from({ length: 65 }, (_, index) => [`key${index}`, index])),
  }), {
    code: 'checkpoint_limit',
    message: 'Checkpoint detail collection exceeds the item limit',
  });

  current = makeTool();
  await assert.rejects(current.tool.execute('nodes', {
    category: 'observation',
    summary: 'bounded',
    details: { groups: Array.from({ length: 64 }, () => [1, 2, 3]) },
  }), {
    code: 'checkpoint_limit',
    message: 'Checkpoint details exceed the value limit',
  });

  current = makeTool();
  let nested = {};
  for (let index = 0; index < 6; index += 1) nested = { nested };
  await assert.rejects(current.tool.execute('depth', { category: 'progress', summary: 'deep', details: nested }), { code: 'checkpoint_limit' });

  current = makeTool();
  await assert.rejects(current.tool.execute('event', { category: 'observation', summary: 'large', details: { body: 'x'.repeat(16 * 1024) } }), { code: 'checkpoint_limit' });

  current = makeTool({ max_events: 1 });
  await current.tool.execute('first', { category: 'progress', summary: 'first', details: {} });
  await assert.rejects(current.tool.execute('second', { category: 'progress', summary: 'second', details: {} }), { code: 'checkpoint_limit' });
  assert.equal((await readFile(current.path, 'utf8')).trimEnd().split('\n').length, 1);

  current = makeTool({ max_spool_bytes: 128 });
  await assert.rejects(current.tool.execute('spool', { category: 'handoff', summary: 'bounded', details: {} }), { code: 'checkpoint_limit' });

  for (const details of [
    { evidence_refs: [{ artifact_id: 'a1' }] },
    { nested: { filePath: '/tmp/private' } },
    { projectid: 'spoofed' },
    { businessAcceptance: 'accepted' },
  ]) {
    current = makeTool();
    await assert.rejects(current.tool.execute('reserved', { category: 'observation', summary: 'reference', details }), { code: 'checkpoint_invalid' });
  }
});

test('checkpoint append rejects symlink spool targets', async (t) => {
  await mkdir(spoolRoot, { recursive: true, mode: 0o700 });
  const path = spoolPath();
  const target = join('/tmp', `cairn-checkpoint-target-${randomUUID()}`);
  await writeFile(target, 'unchanged');
  await symlink(target, path);
  t.after(async () => { await removeSpool(path); await removeSpool(target); });
  const tool = createCheckpointTool(capability(path), 'explore_execute', () => {});
  await assert.rejects(tool.execute('symlink', { category: 'progress', summary: 'blocked', details: {} }), { code: 'checkpoint_spool_invalid' });
  assert.equal(await readFile(target, 'utf8'), 'unchanged');
});
