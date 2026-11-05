import test from 'node:test';
import assert from 'node:assert/strict';
import { emptyUsage, recordUsage, usageResult } from '../dist/usage.js';
import { envelope } from '../dist/protocol.js';

test('all purposes retain known subtotal after missing usage and business failure', () => {
  const positive = { input: 10, output: 5, cacheRead: 0, cacheWrite: 0 };
  const recorded = recordUsage(emptyUsage(), positive);
  const mixed = recordUsage(recorded, undefined);
  const failed = { ...envelope({ phase: 'chat_turn' }), status: 'failed', ...usageResult(mixed, true) };
  assert.deepEqual(failed.usage, { input_tokens: 10, output_tokens: 5, cache_read_tokens: null, cache_write_tokens: null });
  assert.equal(failed.usage_missing_responses, 1);
  assert.deepEqual(usageResult(recordUsage(emptyUsage(), undefined), true), { usage: null, usage_missing_responses: 1 });
  assert.deepEqual(usageResult(recordUsage(emptyUsage(), { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }), true), { usage: null, usage_missing_responses: 1 });
  assert.equal(usageResult(recorded, true).usage_missing_responses, 0);
  assert.deepEqual(usageResult(recorded, false), { usage: failed.usage });
  assert.deepEqual(usageResult(mixed, false), { usage: failed.usage });
  assert.ok(!Object.hasOwn(envelope({ phase: 'reason' }), 'usage_missing_responses'));
  assert.equal(envelope({ phase: 'chat_turn' }).usage_missing_responses, null);
});
