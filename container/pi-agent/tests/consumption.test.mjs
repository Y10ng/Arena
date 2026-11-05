import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, truncateSync, writeFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ConsumptionSpool } from '../dist/consumption.js';
import { runRequest } from '../dist/runner.js';
import { fixture } from './fixtures.mjs';

function spool(t) {
  const directory = mkdtempSync(join(tmpdir(), 'cairn-consumption-test-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const path = join(directory, 'usage.jsonl');
  writeFileSync(path, '', { mode: 0o600 });
  return { path, meter: new ConsumptionSpool(path), events: () => readFileSync(path, 'utf8').trim().split('\n').map(JSON.parse) };
}

test('cumulative observations retain known counts, keep unknown zeros and separate new responses', t => {
  const { meter, events } = spool(t);
  meter.begin();
  meter.observe({ input: 10, output: 2, cacheRead: 3, cacheWrite: 0 });
  meter.observe({ input: 10, output: 2, cacheRead: 3, cacheWrite: 0 });
  meter.observe({ input: 0, output: 5, cacheRead: 0, cacheWrite: 0 });
  meter.observe(undefined, true);
  meter.begin();
  meter.observe({ input: 4, output: 1, cacheRead: 0, cacheWrite: 0 }, true);
  const rows = events();
  assert.deepEqual(rows.map(row => [row.response_seq, row.revision]), [[0, 0], [0, 1], [0, 2], [0, 3], [1, 0], [1, 1]]);
  assert.equal(rows[0].measurement, 'started');
  assert.equal(rows[0].time_basis, 'observed');
  assert.deepEqual(rows[3].usage, { input_tokens: 10, output_tokens: 5, cache_read_tokens: 3 });
  assert.deepEqual(rows[5].usage, { input_tokens: 4, output_tokens: 1 });
  assert.ok(rows.slice(0, 4).every(row => row.occurred_at === rows[0].occurred_at));
});

test('missing spool emits real gap observations without throwing', t => {
  const directory = mkdtempSync(join(tmpdir(), 'cairn-consumption-test-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const gaps = [];
  const meter = new ConsumptionSpool(join(directory, 'missing.jsonl'), event => gaps.push(event));
  meter.begin();
  meter.observe({ input: 10, output: 5, cacheRead: 0, cacheWrite: 0 }, true);
  meter.begin();
  meter.observe(undefined, true);
  assert.deepEqual(gaps.map(event => event.response_seq), [0, 0, 1, 1]);
  assert.ok(gaps.every(event => event.missing_responses === 1));
  assert.deepEqual(gaps[1].usage, { input_tokens: 10, output_tokens: 5 });
  assert.deepEqual(gaps[3].usage, {});
});

for (const fault of ['missing', 'full']) test(`runner keeps actual success and records a gap when spool is ${fault}`, async t => {
  const directory = mkdtempSync(join(tmpdir(), 'cairn-consumption-test-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const path = join(directory, 'usage.jsonl');
  if (fault === 'full') { writeFileSync(path, ''); truncateSync(path, 16 * 1024 * 1024); }
  const { request, calls } = await fixture(t);
  request.capabilities.consumption = { protocol: 'cairn-consumption-v1', id: randomUUID(), spool_path: path };
  const audit = [];
  const result = await runRequest(request, { audit: event => audit.push(event) });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.equal(calls.length, 1);
  const gaps = audit.filter(event => event.event === 'consumption_gap');
  assert.ok(gaps.length >= 1);
  assert.ok(gaps.every(event => event.receipt.missing_responses === 1));
  assert.deepEqual(gaps.at(-1).receipt.usage, { input_tokens: 10, output_tokens: 5 });
});

test('spool failure does not hide a real provider failure', async t => {
  const directory = mkdtempSync(join(tmpdir(), 'cairn-consumption-test-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const { request, calls } = await fixture(t, 'openai-chat-completions', [{ status: 400, error: 'fixture failure' }]);
  request.capabilities.consumption = { protocol: 'cairn-consumption-v1', id: randomUUID(), spool_path: join(directory, 'missing') };
  const audit = [];
  const result = await runRequest(request, { audit: event => audit.push(event) });
  assert.equal(result.status, 'failed');
  assert.notEqual(result.error, null);
  assert.equal(calls.length, 1);
  assert.ok(audit.some(event => event.event === 'consumption_gap'));
});
