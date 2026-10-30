import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:net';
import { mkdir, rm } from 'node:fs/promises';
import { runRequest } from '../dist/runner.js';
import { parseRequest } from '../dist/protocol.js';
import { readHttpTraffic } from '../dist/http-flows.js';
import { canonicalFixtureRequest, fixture, ok } from './fixtures.mjs';

async function bindReader(t, request, handler) {
  const socket_path = `/tmp/cairn-prompts/pi-traffic-${request.capabilities.candidate_capture.capture_id}.sock`;
  await mkdir('/tmp/cairn-prompts', { recursive: true, mode: 0o700 });
  const received = [];
  const server = createServer((socket) => {
    let raw = '';
    socket.on('error', () => {});
    socket.on('data', (chunk) => {
      raw += chunk.toString();
      if (!raw.endsWith('\n')) return;
      const call = JSON.parse(raw);
      received.push(call);
      socket.end(JSON.stringify(handler(call)) + '\n');
    });
  });
  await new Promise((resolve) => server.listen(socket_path, resolve));
  t.after(async () => { await new Promise((resolve) => server.close(resolve)); await rm(socket_path, { force: true }); });
  request.capabilities.http_traffic = { protocol: 'cairn-traffic-read-v1', project_id: request.project_id, task_run_id: request.task_run_id, socket_path };
  return received;
}

test('readonly delivery lists and reads bounded flow projections without target tools or artifacts', async (t) => {
  const projection = { flow_id: 'flow-1', project_id: 'project-test', scope: { project_id: 'project-test', task_run_id: 'task-test' }, revision: 3, state: 'partial', side: 'response', representation: 'http-evidence-v1', sha256: 'a'.repeat(64), size: 99, offset: 9, next_offset: 15, text: '证据', body_artifact_id: 'a1', body_sha256: 'b'.repeat(64), body_display: 'utf8' };
  const { request, calls } = await fixture(t, 'openai-chat-completions', [
    { tools: [{ name: 'list_http_flows', args: { limit: 1 } }] },
    { tools: [{ name: 'read_http_flow', args: { flow_id: 'flow-1', side: 'response', offset: 9, limit: 6 } }] },
    ok,
  ]);
  request.conclude_only = true;
  delete request.capabilities.task_execution;
  const received = await bindReader(t, request, (call) => ({ ok: true, data: call.method === 'list_http_flows' ? { items: [{ flow_id: 'flow-1' }], next_cursor: 1, has_more: true } : projection }));
  const audit = [];
  const result = await runRequest(parseRequest(request), { audit: (event) => audit.push(event) });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.deepEqual(received, [
    { method: 'list_http_flows', params: { limit: 1 } },
    { method: 'read_http_flow', params: { flow_id: 'flow-1', side: 'response', offset: 9, limit: 6 } },
  ]);
  const names = calls[0].body.tools.map((tool) => tool.function.name);
  assert.ok(names.includes('list_http_flows') && names.includes('read_http_flow'));
  assert.ok(names.every((name) => !['bash', 'capture_http_evidence', 'write'].includes(name)));
  const read = JSON.parse(calls[2].body.messages.filter((message) => message.role === 'tool').at(-1).content);
  assert.deepEqual(read, { ...projection, pointer: 'utf8-bytes=9:15' });
  assert.equal(result.result.data.artifacts, undefined);
  assert.ok(!JSON.stringify(audit).includes('证据'));
});

test('optional screenshot rejection does not change a valid candidate or its required artifacts', async (t) => {
  const { request, calls } = await fixture(t, 'openai-chat-completions', [
    { tools: [{ name: 'render_http_evidence', args: { source: { kind: 'flow', flow_id: 'synthetic-denied' } } }] },
    ok,
  ]);
  request.conclude_only = true;
  delete request.capabilities.task_execution;
  const received = await bindReader(t, request, () => ({ ok: false, error: 'traffic_read_http_403' }));
  const result = await runRequest(parseRequest(request));
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.equal(result.result.accepted, true);
  assert.equal(result.result.data.artifacts, undefined);
  assert.deepEqual(received.map((call) => call.method), ['prepare_http_evidence_image']);
  const feedback = JSON.parse(calls[1].body.messages.filter((message) => message.role === 'tool').at(-1).content);
  assert.equal(feedback.status, 'unavailable');
  assert.equal(feedback.optional, true);
});

test('private traffic capability binds project/run/socket and is forbidden in frozen reports', async (t) => {
  const { request } = await fixture(t);
  await bindReader(t, request, () => ({ ok: false, error: 'traffic_historical_required' }));
  const canonical = canonicalFixtureRequest(request);
  assert.ok(parseRequest(canonical).capabilities.http_traffic);
  for (const changed of [{ project_id: 'other-project' }, { task_run_id: 'other-run' }, { socket_path: `${request.cwd}/traffic.sock` }, { token: 'not-permitted' }]) {
    assert.throws(() => parseRequest({ ...canonical, capabilities: { ...canonical.capabilities, http_traffic: { ...request.capabilities.http_traffic, ...changed } } }), { code: 'invalid_request' });
  }
  assert.throws(() => parseRequest({ ...canonical, phase: 'report_execute' }), { code: 'invalid_request', path: 'capabilities.http_traffic' });
  await assert.rejects(readHttpTraffic(request.capabilities.http_traffic, 'read_http_flow', { flow_id: 'old-flow' }), { code: 'traffic_historical_required' });
});
