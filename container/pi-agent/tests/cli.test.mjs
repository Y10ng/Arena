import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { access, writeFile, chmod } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
import { fixture } from './fixtures.mjs';

const cli = fileURLToPath(new URL('../dist/cli.js', import.meta.url));
function invoke(args, cwd, onChild) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [cli, ...args], { cwd, env: { PATH: process.env.PATH, HOME: cwd }, stdio: ['ignore', 'pipe', 'pipe'] });
    onChild?.(child);
    let stdout = '', stderr = '';
    child.stdout.on('data', (chunk) => { stdout += chunk; });
    child.stderr.on('data', (chunk) => { stderr += chunk; });
    child.on('error', reject);
    child.on('close', (code, signal) => resolve({ code, signal, stdout, stderr }));
  });
}

test('CLI emits exactly one program envelope and structured secret-free stderr', async (t) => {
  const { request, cwd } = await fixture(t);
  const path = join(cwd, 'request.json');
  await writeFile(path, JSON.stringify(request), { mode: 0o600 });
  const output = await invoke(['--request', path], cwd);
  assert.equal(output.code, 0, output.stdout + output.stderr);
  const lines = output.stdout.trim().split('\n');
  assert.equal(lines.length, 1);
  const result = JSON.parse(lines[0]);
  assert.equal(result.status, 'succeeded');
  for (const line of output.stderr.trim().split('\n')) assert.equal(JSON.parse(line).protocol, 'cairn-agent-audit-v1');
  assert.ok(!output.stdout.includes('fixture-secret-key'));
  assert.ok(!output.stderr.includes('fixture-secret-key'));
  assert.ok(!output.stderr.includes('fixture-header-value'));
});

test('CLI offline modes do not contact provider or create session', async (t) => {
  const { cwd, calls } = await fixture(t);
  let output = await invoke(['--version'], cwd);
  assert.equal(output.code, 0);
  assert.match(output.stdout, /pi-coding-agent 0\.73\.0/);
  output = await invoke(['--self-check'], cwd);
  assert.equal(output.code, 0);
  assert.deepEqual(JSON.parse(output.stdout), { status: 'ok', engine: 'pi', sdk_version: '0.73.0', offline: true, capabilities: ['candidate-spool-v1', 'execution-checkpoint-spool-v1', 'session-continuation-v1'] });
  assert.equal(calls.length, 0);
});

test('CLI rejects request phase healthcheck before provider or session activity', async (t) => {
  const { request, cwd, calls } = await fixture(t);
  request.phase = 'healthcheck';
  const path = join(cwd, 'request.json');
  await writeFile(path, JSON.stringify(request), { mode: 0o600 });
  const output = await invoke(['--request', path], cwd);
  assert.equal(output.code, 1, output.stdout + output.stderr);
  const lines = output.stdout.trim().split('\n');
  assert.equal(lines.length, 1);
  const result = JSON.parse(lines[0]);
  assert.equal(result.project_id, request.project_id);
  assert.equal(result.task_run_id, request.task_run_id);
  assert.equal(result.phase, 'healthcheck');
  assert.equal(result.session, request.session);
  assert.deepEqual(result.error, {
    code: 'invalid_request',
    message: 'Invalid request field: phase',
    path: 'phase',
  });
  assert.equal(calls.length, 0);
  await assert.rejects(access(request.session_root));
});

test('CLI rejects world-readable request without exposing its contents', async (t) => {
  const { request, cwd, calls } = await fixture(t);
  const path = join(cwd, 'request.json');
  await writeFile(path, JSON.stringify(request));
  await chmod(path, 0o644);
  const output = await invoke(['--request', path], cwd);
  assert.equal(output.code, 1);
  assert.equal(JSON.parse(output.stdout).error.code, 'invalid_request');
  assert.equal(calls.length, 0);
  assert.ok(!output.stdout.includes('fixture-secret-key'));
});

test('CLI invalid binding exposes only the fixed contract path before any provider call', async (t) => {
  const { request, cwd, calls } = await fixture(t);
  request.binding = { review_input_digest: null, ignored: 'private-payload' };
  const path = join(cwd, 'request.json');
  await writeFile(path, JSON.stringify(request), { mode: 0o600 });
  const output = await invoke(['--request', path], cwd);
  assert.equal(output.code, 1);
  const result = JSON.parse(output.stdout);
  assert.deepEqual(result.error, {
    code: 'invalid_request', message: 'Invalid request field: binding.review_input_digest',
    path: 'binding.review_input_digest',
  });
  assert.equal(result.phase, request.phase);
  assert.equal(calls.length, 0);
  assert.ok(!output.stdout.includes('private-payload'));
  assert.ok(!output.stdout.includes('fixture-secret-key'));
});

test('CLI SIGTERM yields cancelled, nonzero and no repair call', async (t) => {
  let child;
  const { request, cwd, calls } = await fixture(t, 'openai-chat-completions', [() => { setTimeout(() => child.kill('SIGTERM'), 25); return { hang: true }; }]);
  const path = join(cwd, 'request.json');
  await writeFile(path, JSON.stringify(request), { mode: 0o600 });
  const output = await invoke(['--request', path], cwd, (process) => { child = process; });
  assert.equal(output.code, 130, output.stdout + output.stderr);
  assert.equal(JSON.parse(output.stdout).status, 'cancelled');
  assert.equal(calls.length, 1);
});
