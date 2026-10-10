import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, rm, writeFile, readFile, truncate, symlink, chmod } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { runRequest } from '../dist/runner.js';
import { ArtifactRegistry, evidencePath } from '../dist/artifacts.js';
import { fixture, ok } from './fixtures.mjs';

function schema(properties) {
  return { type: 'object', properties: { accepted: { const: true }, data: { type: 'object', properties, additionalProperties: false } }, required: ['accepted', 'data'], additionalProperties: false };
}

test('register evidence via actual SDK; inject exact artifact declaration and authoritative bindings', async (t) => {
  const { request, cwd, calls } = await fixture(t, 'openai-chat-completions', [
    { tools: [{ name: 'register_artifact', args: { path: 'evidence.txt', kind: 'raw_output' } }] },
    (call) => {
      const toolMessage = call.body.messages.find((message) => message.role === 'tool');
      const registered = JSON.parse(toolMessage.content);
      return { tools: [{ name: 'submit_result', args: { accepted: true, data: { evidence_refs: [{ artifact_id: registered.handle }], coverage_result: {}, verification_result: {}, depth_review: {} } } }] };
    },
  ]);
  await writeFile(join(cwd, 'evidence.txt'), 'local fixture evidence');
  request.phase = 'explore_execute';
  request.result_schema = schema({ evidence_refs: { type: 'array', items: { type: 'object', properties: { artifact_id: { type: 'string' } }, required: ['artifact_id'], additionalProperties: false } }, coverage_result: { type: 'object', additionalProperties: false }, verification_result: { type: 'object', additionalProperties: false }, depth_review: { type: 'object', additionalProperties: false } });
  request.binding = { slot_key: 'cell-1', surface_key: 'surface-1', test_family: 'authorization', verification_observation_id: 'obs-1', review_input_digest: 'd'.repeat(64), max_intents: 3, allowed_fact_ids: ['fact-1'] };
  const audit = [];
  const result = await runRequest(request, { audit: (e) => audit.push(e) });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.equal(calls.length, 2);
  const data = result.result.data;
  assert.equal(data.artifacts.length, 1);
  assert.deepEqual(Object.keys(data.artifacts[0]).sort(), ['handle', 'kind', 'path', 'sha256', 'size_bytes', 'source_path']);
  assert.equal(data.artifacts[0].source_path, join(cwd, 'evidence.txt'));
  assert.equal(await readFile(data.artifacts[0].path, 'utf8'), 'local fixture evidence');
  assert.match(data.artifacts[0].handle, /^local-/);
  assert.equal(data.evidence_refs[0].artifact_id, data.artifacts[0].handle);
  assert.deepEqual(data.coverage_result, { slot_key: 'cell-1', surface_key: 'surface-1', test_family: 'authorization' });
  assert.deepEqual(data.verification_result, { observation_id: 'obs-1' });
  assert.deepEqual(data.depth_review, { input_digest: 'd'.repeat(64) });
  const registered = audit.find((e) => e.event === 'artifact_registered');
  assert.equal(registered.size_bytes, 22);
  assert.equal(registered.metadata, undefined);
  assert.ok(!JSON.stringify(audit).includes('evidence.txt'));
});

test('bindings never create semantic objects missing from model result', async (t) => {
  const { request } = await fixture(t);
  request.phase = 'verifier_execute';
  request.binding = { slot_key: 'slot', surface_key: 'surface', test_family: 'auth', verification_observation_id: 'obs', review_input_digest: 'digest' };
  const result = await runRequest(request);
  assert.equal(result.status, 'succeeded');
  assert.deepEqual(result.result.data, { status: 'ok' });
});

test('unknown evidence handles fail after bounded repairs', async (t) => {
  const { request, calls } = await fixture(t, 'openai-chat-completions', [{ tools: [{ name: 'submit_result', args: { accepted: true, data: { evidence_refs: [{ artifact_id: 'made-up-handle' }] } } }] }]);
  request.phase = 'explore_execute';
  request.result_schema = schema({ evidence_refs: { type: 'array', items: { type: 'object' } } });
  const result = await runRequest(request);
  assert.equal(result.status, 'failed');
  assert.equal(result.error.code, 'artifact_unknown');
  assert.equal(calls.length, 3);
});

test('registry preserves fixed versions and provenance; changed copies and wrong references still reject', async (t) => {
  const { cwd, request } = await fixture(t);
  const file = join(cwd, 'evidence.txt');
  await writeFile(file, 'before');
  await symlink(file, join(cwd, 'evidence-link'));
  const registry = new ArtifactRegistry(cwd, request.session_root);
  await assert.rejects(registry.register('evidence-link', 'file'), { code: 'artifact_invalid' });
  const first = await registry.register('evidence.txt', 'file');
  await writeFile(file, 'after');
  const data = { evidence_refs: [{ artifact_id: first.handle }] };
  await registry.inject(data);
  assert.equal(await readFile(first.path, 'utf8'), 'before');
  assert.equal(first.source_path, file);
  const second = await registry.register('evidence.txt', 'file');
  assert.notEqual(first.handle, second.handle);
  assert.notEqual(first.path, second.path);
  assert.equal(await readFile(second.path, 'utf8'), 'after');
  await registry.inject(data);
  assert.equal(data.artifacts.length, 2);
  assert.equal(data.evidence_refs[0].artifact_id, first.handle);
  await assert.rejects(registry.inject({ evidence_refs: [{ artifact_id: 'local-missing' }] }), { code: 'artifact_unknown' });
  await assert.rejects(registry.inject({ artifacts: [{ handle: first.handle, path: file, kind: first.kind }] }), { code: 'artifact_unknown' });
  await chmod(first.path, 0o600);
  await writeFile(first.path, 'tampered');
  await assert.rejects(registry.inject(data), { code: 'artifact_changed' });
  assert.equal(registry.items[0].sha256, first.sha256);
  await rm(first.path);
  await assert.rejects(registry.inject(data), { code: 'ENOENT' });
});

test('registration rejects capacity before capture without truncating or discarding versions', async (t) => {
  const { cwd, request } = await fixture(t);
  const file = join(cwd, 'evidence.txt');
  await writeFile(file, 'one');
  const registry = new ArtifactRegistry(cwd, request.session_root);
  for (let index = 0; index < 32; index += 1) await registry.register(file, 'file');
  await assert.rejects(registry.register(file, 'file'), { code: 'artifact_invalid' });
  assert.equal(registry.items.length, 32);
  await registry.inject({});
  const large = join(cwd, 'large.txt');
  await writeFile(large, '');
  await truncate(large, 32 * 1024 * 1024 + 1);
  const bounded = new ArtifactRegistry(cwd, request.session_root);
  await assert.rejects(bounded.register(large, 'file'), { code: 'artifact_invalid' });
  assert.equal(bounded.items.length, 0);
});

test('readonly inputs remain readable but cannot be registered as artifacts', async (t) => {
  const { request, cwd, calls } = await fixture(t, 'openai-chat-completions', [
    { tools: [{ name: 'read_evidence', args: { path: '.cairn/readonly-inputs/request.txt' } }] },
    ok,
  ]);
  const directory = join(cwd, '.cairn', 'readonly-inputs');
  await mkdir(directory, { recursive: true });
  await writeFile(join(directory, 'request.txt'), 'readonly request body');
  request.phase = 'explore_execute';
  const result = await runRequest(request);
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  const toolMessage = calls[1].body.messages.find((message) => message.role === 'tool');
  assert.equal(JSON.parse(toolMessage.content).text, 'readonly request body');
  const registry = new ArtifactRegistry(cwd, request.session_root);
  await assert.rejects(registry.register('.cairn/readonly-inputs/request.txt', 'file'), { code: 'artifact_invalid' });
});

test('managed readonly inputs outside workspace are readable but never registrable', async (t) => {
  const { request, cwd } = await fixture(t);
  const managedRoot = await mkdtemp(join(tmpdir(), 'cairn-readonly-inputs-'));
  const unrelatedRoot = await mkdtemp(join(tmpdir(), 'cairn-unrelated-inputs-'));
  t.after(async () => {
    await rm(managedRoot, { recursive: true, force: true });
    await rm(unrelatedRoot, { recursive: true, force: true });
  });
  const managedFile = join(managedRoot, 'report-packet.json');
  const unrelatedFile = join(unrelatedRoot, 'untrusted.json');
  await writeFile(managedFile, '{"version":1}');
  await writeFile(unrelatedFile, '{}');

  assert.equal(
    await evidencePath(cwd, managedFile, request.session_root, managedRoot),
    managedFile,
  );
  await assert.rejects(
    evidencePath(cwd, unrelatedFile, request.session_root, managedRoot),
    { code: 'artifact_invalid' },
  );
  const registry = new ArtifactRegistry(cwd, request.session_root, managedRoot);
  await assert.rejects(
    registry.register(managedFile, 'file'),
    { code: 'artifact_invalid' },
  );
});

test('malformed tool JSON is not accepted by SDK permissive partial parser', async (t) => {
  const { request, calls } = await fixture(t, 'openai-chat-completions', [{ tools: [{ name: 'submit_result', rawArgs: '{"accepted":true,"data":{"status":"ok"}', args: {} }] }, ok]);
  const result = await runRequest(request);
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.equal(calls.length, 2);
});

test('duplicate identical submissions produce one candidate', async (t) => {
  const { request, calls } = await fixture(t, 'openai-chat-completions', [{ tools: [ok.tools[0], ok.tools[0]] }]);
  const result = await runRequest(request);
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.equal(calls.length, 1);
});

test('real SDK bash large output is bounded and absent from structured audit', async (t) => {
  const { request } = await fixture(t, 'openai-chat-completions', [{ tools: [{ name: 'bash', args: { command: `node -e "process.stdout.write(('fixture-output-'+String.fromCharCode(10)).repeat(30000))"` } }] }, ok]);
  request.phase = 'explore_execute';
  const audit = [];
  const result = await runRequest(request, { audit: (e) => audit.push(e) });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.ok(audit.some((e) => e.event === 'tool_execution_end' && e.tool_name === 'bash' && e.output_bytes > 1000));
  assert.ok(!JSON.stringify(audit).includes('fixture-output-'));
  assert.ok(JSON.stringify(audit).length < 10000);
});

test('a cancellation synchronously observed after local submission beats success', async (t) => {
  const { request } = await fixture(t);
  const controller = new AbortController();
  const result = await runRequest(request, { signal: controller.signal, audit: (event) => { if (event.event === 'submission_validated') controller.abort(); } });
  assert.equal(result.status, 'cancelled');
  assert.equal(result.result, null);
  assert.equal(result.submission_id, null);
});
