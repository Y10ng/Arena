import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, readdir, access } from 'node:fs/promises';
import { join } from 'node:path';
import { runRequest } from '../dist/runner.js';
import { fixture, ok } from './fixtures.mjs';

for (const protocol of ['anthropic-messages', 'openai-chat-completions', 'openai-responses']) test(`${protocol}: interrupted stream cannot be a successful tool submission`, async (t) => {
  const { request, calls } = await fixture(t, protocol, [{ ...ok, interrupt: true }]);
  const audit = [];
  const result = await runRequest(request, { audit: (event) => audit.push(event) });
  assert.equal(result.status, 'failed', JSON.stringify(result));
  assert.equal(result.result, null);
  assert.equal(result.error.code, 'provider_error');
  assert.ok(calls.length <= 2);
  assert.ok(!audit.some((event) => event.event === 'result_repair'));
});

test('max_result_repairs zero means no extra provider round', async (t) => {
  const { request, calls } = await fixture(t, 'openai-chat-completions', [{ text: 'No submit_result.' }]);
  request.max_result_repairs = 0;
  const result = await runRequest(request);
  assert.equal(result.status, 'failed');
  assert.equal(result.error.code, 'submission_missing');
  assert.equal(calls.length, 1);
});

test('oversized tool results fail closed without copying model text into audit', async (t) => {
  const { request, calls } = await fixture(t, 'openai-chat-completions', [{ tools: [{ name: 'submit_result', args: { accepted: false, reason: 'X'.repeat(1024 * 1024 + 10) } }] }]);
  request.max_result_repairs = 0;
  const audit = [];
  const result = await runRequest(request, { audit: (event) => audit.push(event) });
  assert.equal(result.status, 'failed');
  assert.equal(calls.length, 1);
  assert.ok(JSON.stringify(audit).length < 10000);
});

test('business rejection is a structured local candidate, not an empty success', async (t) => {
  const { request } = await fixture(t, 'openai-chat-completions', [{ tools: [{ name: 'submit_result', args: { accepted: false, reason: 'No authorized target is available.' } }] }]);
  const result = await runRequest(request);
  assert.equal(result.status, 'succeeded');
  assert.deepEqual(result.result, { protocol: 'cairn-assessment-v2', accepted: false, reason: 'No authorized target is available.' });
});

test('cancelled candidate and evidence survive for diagnosis, never become terminal result', async (t) => {
  const { request } = await fixture(t);
  const controller = new AbortController();
  const result = await runRequest(request, { signal: controller.signal, audit: (event) => { if (event.event === 'submission_validated') controller.abort(); } });
  assert.equal(result.status, 'cancelled');
  assert.equal(result.result, null);
  const directory = join(request.session_root, request.session.slice(3));
  const candidates = (await readdir(directory)).filter((name) => name.startsWith('candidate-'));
  assert.equal(candidates.length, 1);
  const candidate = JSON.parse(await readFile(join(directory, candidates[0]), 'utf8'));
  assert.equal(candidate.server_accepted, false);
  assert.equal(candidate.task_run_id, request.task_run_id);
  assert.equal(candidate.result.kind, 'explore_result');
  await assert.rejects(access(join(request.session_root, request.session.slice(3) + '.lock')));
});

test('forbidden conclude tools cannot cause unbounded SDK auto-repair loops', async (t) => {
  const { request, calls } = await fixture(t, 'openai-chat-completions', [{ tools: [{ name: 'bash', args: { command: 'touch forbidden-target-action' } }] }]);
  request.phase = 'explore_conclude';
  request.conclude_only = true;
  const result = await runRequest(request);
  assert.equal(result.status, 'failed');
  assert.equal(result.error.code, 'tool_permission');
  assert.equal(result.error.repair_rounds, 0);
  assert.equal(calls.length, 1);
  await assert.rejects(access(join(request.cwd, 'forbidden-target-action')));
});

test('image stages use the same fixed Node base without legacy global agent CLIs', async () => {
  const dockerfile = await readFile(new URL('../../Dockerfile', import.meta.url), 'utf8');
  const ignore = await readFile(new URL('../../.dockerignore', import.meta.url), 'utf8');
  assert.match(dockerfile, /FROM node:24\.16\.0-bookworm-slim AS node-distribution/);
  assert.match(dockerfile, /FROM worker-node-base AS pi-agent-builder/);
  assert.match(dockerfile, /FROM worker-node-base AS worker-runtime/);
  assert.match(dockerfile, /COPY --from=pi-agent-builder .*node_modules/);
  assert.ok(!/npm install -g @(openai\/codex|anthropic-ai\/claude-code|mariozechner\/pi-coding-agent)/.test(dockerfile));
  assert.ok(!dockerfile.includes('CLAUDE_CODE_'));
  assert.ok(!/COPY .*\.codex|COPY .*CLAUDE\.md/.test(dockerfile));
  assert.match(ignore, /\*\*\/node_modules/);
  assert.match(ignore, /\*\*\/dist/);
});
