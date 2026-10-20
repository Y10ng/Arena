import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, writeFile, access, readdir, chmod } from 'node:fs/promises';
import { join } from 'node:path';
import { createRequestBashToolDefinition, runRequest } from '../dist/runner.js';
import { parseRequest, PHASE_RESULT_KINDS } from '../dist/protocol.js';
import { acquireSession } from '../dist/session.js';
import { fixture, ok, submissionSchema } from './fixtures.mjs';

for (const protocol of ['anthropic-messages', 'openai-chat-completions', 'openai-responses']) {
  for (const auth of ['authorization-bearer', 'x-api-key']) test(`published SDK ${protocol}: exact endpoint, ${auth}, submit and audit`, async (t) => {
    const { request, calls, suffix } = await fixture(t, protocol);
    request.model.auth_header = auth;
    const audit = [];
    const result = await runRequest(parseRequest(request), { audit: (event) => audit.push(event) });
    assert.equal(result.status, 'succeeded', JSON.stringify(result));
    assert.deepEqual(result.result, { protocol: 'cairn-assessment-v2', accepted: true, kind: 'explore_result', data: { status: 'ok' } });
    assert.match(result.submission_id, /^[a-f0-9]{64}$/);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].path, '/tenant/prefix' + suffix);
    assert.equal(calls[0].headers['x-fixture-header'], 'fixture-header-value');
    assert.equal(calls[0].headers.authorization, auth === 'authorization-bearer' ? 'Bearer fixture-secret-key' : undefined);
    assert.equal(calls[0].headers['x-api-key'], auth === 'x-api-key' ? 'fixture-secret-key' : undefined);
    const toolStart = audit.find((e) => e.event === 'tool_execution_start' && e.tool_name === 'submit_result');
    const toolEnd = audit.find((e) => e.event === 'tool_execution_end' && e.is_error === false);
    assert.ok(toolStart.tool_call_id);
    assert.equal(toolEnd.tool_call_id, toolStart.tool_call_id);
    assert.ok(!JSON.stringify(audit).includes('fixture-secret-key'));
    assert.ok(!JSON.stringify(audit).includes('fixture-header-value'));
    assert.equal(result.usage.input_tokens, 10);
  });
}

test('Anthropic request preserves the complete submit_result schema after provider conversion', async (t) => {
  const schema = {
    type: 'object',
    properties: {
      accepted: { type: 'boolean' },
      data: { type: 'object', properties: { vulnerability: { type: ['object', 'null'] } }, required: ['vulnerability'], additionalProperties: false },
      reason: { type: 'string' },
    },
    required: ['accepted'], additionalProperties: false,
    if: { properties: { accepted: { const: true } }, required: ['accepted'] },
    then: { required: ['data'], not: { required: ['reason'] } },
    else: { required: ['reason'], not: { required: ['data'] } },
    $defs: { unused: { type: 'string' } },
  };
  const valid = { accepted: true, data: { vulnerability: null } };
  const { request, calls } = await fixture(t, 'anthropic-messages', [
    { tools: [{ name: 'submit_result', args: { ...valid, reason: 'unexpected' } }] },
    { tools: [{ name: 'submit_result', args: { accepted: true, data: { vulnerability: 'invalid' } } }] },
    { tools: [{ name: 'submit_result', args: valid }] },
  ]);
  request.result_schema = schema;
  const result = await runRequest(request);
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.equal(calls.length, 3);
  for (const call of calls) {
    const submit = call.body.tools.find((tool) => tool.name === 'submit_result');
    assert.deepEqual(submit.input_schema, schema);
    assert.match(submit.description, /local candidate/);
    assert.ok(call.body.tools.some((tool) => tool.name === 'register_artifact'));
  }
});

test('text is not JSON fallback; normal repair keeps authorized tools in the same session', async (t) => {
  const { request, calls } = await fixture(t, 'openai-chat-completions', [{ text: '```json\n{"accepted":true,"data":{"status":"ok"}}\n```' }, ok]);
  request.phase = 'explore_execute';
  const audit = [];
  const result = await runRequest(request, { audit: (e) => audit.push(e) });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.equal(calls.length, 2);
  assert.ok(calls[0].body.tools.some((tool) => tool.function.name === 'bash'));
  assert.deepEqual(calls[1].body.tools.map((tool) => tool.function.name), calls[0].body.tools.map((tool) => tool.function.name));
  assert.equal(audit.filter((e) => e.event === 'result_repair').length, 1);
  assert.equal(new Set(audit.map((e) => e.session)).size, 1);
});

test('thinking-only length continues the same session to tools and synthetic evidence without replay or repair', async (t) => {
  const { request, calls, cwd } = await fixture(t, 'anthropic-messages', [
    { tools: [{ name: 'bash', args: { command: 'printf before >> actions.txt' } }] },
    { thinking: 'synthetic unfinished reasoning', stopReason: 'max_tokens' },
    { tools: [{ name: 'bash', args: { command: 'printf after >> actions.txt; printf synthetic-evidence > evidence.txt' } }] },
    { tools: [{ name: 'register_artifact', args: { path: 'evidence.txt', kind: 'raw_output' } }] },
    ok,
  ]);
  const audit = [];
  const result = await runRequest(parseRequest(request), { audit: (event) => audit.push(event) });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.equal(calls.length, 5);
  assert.equal(await readFile(join(cwd, 'actions.txt'), 'utf8'), 'beforeafter');
  assert.equal(await readFile(result.result.data.artifacts[0].path, 'utf8'), 'synthetic-evidence');
  assert.ok(calls[2].body.tools.some((tool) => tool.name === 'bash'));
  assert.match(JSON.stringify(calls[2].body.messages), /synthetic unfinished reasoning/);
  assert.equal(audit.filter((event) => event.event === 'output_continuation').length, 1);
  assert.equal(audit.filter((event) => event.event === 'result_repair').length, 0);
  assert.equal(audit.filter((event) => event.event === 'tool_execution_start' && event.tool_name === 'bash').length, 2);
  assert.equal(new Set(audit.map((event) => event.session)).size, 1);
  assert.equal(new Set(audit.map((event) => event.task_run_id)).size, 1);
});

test('consecutive thinking-only length has a finite endpoint without result repairs', async (t) => {
  const { request, calls } = await fixture(t, 'anthropic-messages', [
    { thinking: 'synthetic unfinished reasoning', stopReason: 'max_tokens' },
  ]);
  const audit = [];
  const result = await runRequest(request, { audit: (event) => audit.push(event) });
  assert.equal(result.status, 'failed', JSON.stringify(result));
  assert.equal(result.error.code, 'submission_missing');
  assert.match(result.error.message, /truncation continuation limit/);
  assert.equal(result.error.submission_attempts, 0);
  assert.equal(result.error.repair_rounds, 0);
  assert.equal(calls.length, 3);
  assert.equal(audit.filter((event) => event.event === 'output_continuation').length, 2);
  assert.ok(audit.some((event) => event.event === 'output_truncation_limit'));
});

test('length continuation cannot extend the original deadline or continue after cancellation', async (t) => {
  for (const cancelled of [false, true]) await t.test(cancelled ? 'cancelled' : 'deadline', async (t) => {
    const controller = new AbortController();
    const { request, calls } = await fixture(t, 'anthropic-messages', [
      async () => { await new Promise((resolve) => setTimeout(resolve, 200)); return { thinking: 'synthetic', stopReason: 'max_tokens' }; },
      { hang: true },
    ]);
    request.timeout_seconds = 0.7;
    const audit = [];
    const start = performance.now();
    const result = await runRequest(request, {
      signal: controller.signal,
      audit: (event) => { audit.push(event); if (cancelled && event.event === 'output_continuation') controller.abort(); },
    });
    assert.equal(result.status, 'cancelled', JSON.stringify(result));
    assert.equal(result.error.code, cancelled ? 'cancelled' : 'deadline_exceeded');
    assert.ok(performance.now() - start < 850, 'continuation reset the original deadline');
    assert.equal(calls.length, cancelled ? 1 : 2);
    assert.equal(audit.some((event) => event.event === 'result_repair'), false);
  });
});

test('length during correction preserves normal execution or explicit delivery-only permissions', async (t) => {
  for (const concludeOnly of [false, true]) await t.test(concludeOnly ? 'conclude_only' : 'normal_correction', async (t) => {
    const { request, calls, cwd } = await fixture(t, 'anthropic-messages', [
      ...(concludeOnly ? [] : [{ tools: [{ name: 'submit_result', args: { accepted: true, data: {} } }] }]),
      { thinking: 'synthetic unfinished correction', stopReason: 'max_tokens' },
      { tools: [{ name: 'bash', args: { command: 'printf corrected > correction-evidence' } }] },
      ok,
    ]);
    request.conclude_only = concludeOnly;
    const audit = [];
    const result = await runRequest(request, { audit: (event) => audit.push(event) });
    assert.equal(result.status, concludeOnly ? 'failed' : 'succeeded', JSON.stringify(result));
    assert.equal(result.error?.code, concludeOnly ? 'tool_permission' : undefined);
    assert.equal(audit.some((event) => event.event === 'output_continuation'), false);
    for (const call of calls.slice(concludeOnly ? 0 : 1)) assert.equal(call.body.tools.some((tool) => tool.name === 'bash'), !concludeOnly);
    if (concludeOnly) await assert.rejects(access(join(cwd, 'correction-evidence')));
    else assert.equal(await readFile(join(cwd, 'correction-evidence'), 'utf8'), 'corrected');
  });
});

test('length continuation keeps authorized tools after the remaining-time notice', async (t) => {
  const { request, calls, cwd } = await fixture(t, 'anthropic-messages', [
    { thinking: 'synthetic unfinished reasoning', stopReason: 'max_tokens' },
    async () => { await new Promise((resolve) => setTimeout(resolve, 350)); return { tools: [{ name: 'bash', args: { command: 'printf continued > length-evidence' } }] }; },
    ok,
  ]);
  request.timeout_seconds = 2;
  request.conclude_timeout_seconds = 1.8;
  const audit = [];
  const result = await runRequest(request, { audit: (event) => audit.push(event) });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.equal(calls.length, 3);
  assert.ok(calls[2].body.tools.some((tool) => tool.name === 'bash'));
  assert.equal(audit.filter((event) => event.event === 'output_continuation').length, 1);
  assert.equal(audit.filter((event) => event.event === 'execution_time_notice').length, 1);
  assert.equal(await readFile(join(cwd, 'length-evidence'), 'utf8'), 'continued');
});

test('length with partial tool arguments fails without execution continuation or tool replay', async (t) => {
  const { request, calls, cwd } = await fixture(t, 'anthropic-messages', [
    { tools: [{ name: 'bash', rawArgs: '{"command":"touch forbidden-partial' }], stopReason: 'max_tokens' },
    ok,
  ]);
  const audit = [];
  const result = await runRequest(request, { audit: (event) => audit.push(event) });
  assert.equal(result.status, 'failed', JSON.stringify(result));
  assert.equal(result.error.code, 'provider_error');
  assert.equal(calls.length, 1);
  assert.equal(audit.some((event) => event.event === 'output_continuation' || event.event === 'result_repair'), false);
  await assert.rejects(access(join(cwd, 'forbidden-partial')));
});

test('source growth delivers the fixed registered bytes without another repair', async (t) => {
  let cwd;
  let registered;
  const { request, calls, cwd: workspace } = await fixture(t, 'openai-chat-completions', [
    { tools: [{ name: 'register_artifact', args: { path: 'evidence.txt', kind: 'raw_output' } }] },
    async (call) => {
      registered = JSON.parse(call.body.messages.find((message) => message.role === 'tool').content);
      await writeFile(join(cwd, 'evidence.txt'), 'original synthetic evidence\ncontinued background output');
      return { tools: [{ name: 'submit_result', args: { accepted: true, data: { status: 'ok' } } }] };
    },
  ]);
  cwd = workspace;
  await writeFile(join(cwd, 'evidence.txt'), 'original synthetic evidence');
  const audit = [];
  const result = await runRequest(parseRequest(request), { audit: (event) => audit.push(event) });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.equal(calls.length, 2);
  assert.deepEqual(result.result.data.artifacts, [registered]);
  assert.equal(await readFile(registered.path, 'utf8'), 'original synthetic evidence');
  assert.equal(registered.source_path, join(cwd, 'evidence.txt'));
  assert.equal(audit.filter((event) => event.event === 'result_repair').length, 0);
});

test('artifact_changed repair never extends the original deadline', async (t) => {
  let cwd;
  const { request, calls, cwd: workspace } = await fixture(t, 'openai-chat-completions', [
    { tools: [{ name: 'register_artifact', args: { path: 'evidence.txt', kind: 'file' } }] },
    async (call) => {
      const registered = JSON.parse(call.body.messages.find((message) => message.role === 'tool').content);
      await chmod(registered.path, 0o600);
      await writeFile(registered.path, 'after');
      return ok;
    },
    { hang: true },
  ]);
  cwd = workspace;
  await writeFile(join(cwd, 'evidence.txt'), 'before');
  request.timeout_seconds = 0.8;
  const result = await runRequest(parseRequest(request));
  assert.equal(result.status, 'cancelled', JSON.stringify(result));
  assert.equal(result.error.code, 'deadline_exceeded');
  assert.equal(result.error.repair_rounds, 1);
  assert.equal(result.error.submission_attempts, 1);
  assert.equal(result.result, null);
  assert.equal(calls.length, 3);
  assert.ok(calls[2].body.tools.some((tool) => tool.function.name === 'bash'));
});

test('strict tool arguments reject coercion and allow at most two repair rounds', async (t) => {
  const { request, calls } = await fixture(t, 'openai-chat-completions', [{ tools: [{ name: 'submit_result', args: { accepted: 'true', data: { status: 'ok' } } }] }]);
  const result = await runRequest(request);
  assert.equal(result.status, 'failed');
  assert.equal(result.error.code, 'submission_invalid');
  assert.equal(calls.length, 3);
});

test('invalid submission keeps bash, read and write available for same-session evidence correction', async (t) => {
  const { request, calls, cwd } = await fixture(t, 'openai-chat-completions', [{ tools: [
    { name: 'submit_result', args: { accepted: true, data: {} } },
    { name: 'bash', args: { command: 'printf original > correction.txt' } },
  ] },
  { tools: [{ name: 'read', args: { path: 'correction.txt' } }] },
  { tools: [{ name: 'write', args: { path: 'corrected.txt', content: 'synthetic corrected evidence' } }] },
  { tools: [{ name: 'register_artifact', args: { path: 'corrected.txt', kind: 'raw_output' } }] },
  ok]);
  const audit = [];
  const result = await runRequest(request, { audit: (event) => audit.push(event) });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.equal(calls.length, 5);
  assert.equal(audit.filter((event) => event.event === 'result_repair').length, 1);
  assert.equal(audit.filter((event) => event.event === 'tool_execution_start' && event.tool_name === 'bash').length, 1);
  assert.equal(await readFile(join(cwd, 'correction.txt'), 'utf8'), 'original');
  assert.match(JSON.stringify(calls[2].body.messages), /original/);
  assert.equal(await readFile(result.result.data.artifacts[0].path, 'utf8'), 'synthetic corrected evidence');
  for (const call of calls) assert.ok(call.body.tools.some((tool) => tool.function.name === 'bash'));
});

test('valid candidate ends execution without running or poisoning trailing SDK tool calls', async (t) => {
  const { request, calls, cwd } = await fixture(t, 'openai-chat-completions', [{ tools: [
    ok.tools[0],
    { name: 'bash', args: { command: 'touch forbidden-after-candidate' } },
    { name: 'write', args: { path: 'forbidden-after-candidate-write', content: 'forbidden' } },
    { name: 'register_artifact', args: { path: 'missing', kind: 'file' } },
  ] }]);
  const audit = [];
  const result = await runRequest(request, { audit: (event) => audit.push(event) });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.equal(calls.length, 1);
  assert.equal(result.result.data.status, 'ok');
  assert.equal(audit.filter((event) => event.event === 'submission_tail_skipped').length, 3);
  assert.equal(audit.some((event) => event.event === 'result_repair'), false);
  assert.ok(!JSON.stringify(result).includes('tool_permission'));
  await assert.rejects(access(join(cwd, 'forbidden-after-candidate')));
  await assert.rejects(access(join(cwd, 'forbidden-after-candidate-write')));
});

test('no submission fails rather than extracting prose after repairs', async (t) => {
  const { request, calls } = await fixture(t, 'openai-chat-completions', [{ text: JSON.stringify(ok.tools[0].args) }]);
  const result = await runRequest(request);
  assert.equal(result.status, 'failed');
  assert.equal(calls.length, 3);
  assert.equal(result.result, null);
});

test('cancel during provider stream cannot start repairs or succeed', async (t) => {
  const { request, calls } = await fixture(t, 'openai-chat-completions', [{ hang: true }]);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 120);
  t.after(() => clearTimeout(timer));
  const result = await runRequest(request, { signal: controller.signal });
  assert.equal(result.status, 'cancelled', JSON.stringify(result));
  assert.equal(result.result, null);
  assert.equal(result.error.code, 'cancelled');
  assert.equal(calls.length, 1);
});

test('deadline aborts without a repair or success', async (t) => {
  const { request, calls } = await fixture(t, 'openai-chat-completions', [{ hang: true }]);
  request.timeout_seconds = 0.1;
  const result = await runRequest(request);
  assert.equal(result.status, 'cancelled');
  assert.equal(result.error.code, 'deadline_exceeded');
  assert.equal(calls.length, 1);
});

test('bash timeout cap defaults strictly and effective timeout uses every finite minimum', async (t) => {
  const { request, cwd } = await fixture(t);
  assert.equal(parseRequest(request).bash_tool_timeout_seconds, 120);
  for (const value of [0, -1, 1.5, 3601, Number.POSITIVE_INFINITY, '120']) {
    assert.throws(() => parseRequest({ ...request, bash_tool_timeout_seconds: value }), { code: 'invalid_request', path: 'bash_tool_timeout_seconds' });
  }

  const observedTimeout = async ({ timeout, cap = 120, remaining = 300 }) => {
    let observed;
    const operations = {
      async exec(_command, _cwd, options) {
        observed = options.timeout;
        return { exitCode: 0 };
      },
    };
    const tool = createRequestBashToolDefinition(cwd, cap, () => remaining, operations);
    await tool.execute('bash-timeout', { command: 'true', ...(timeout === undefined ? {} : { timeout }) });
    return observed;
  };

  assert.equal(await observedTimeout({}), 120);
  assert.equal(await observedTimeout({ timeout: 7200 }), 120);
  assert.equal(await observedTimeout({ timeout: 7 }), 7);
  assert.equal(await observedTimeout({ timeout: 7, remaining: 3 }), 3);
  assert.equal(await observedTimeout({ timeout: 0, cap: 9, remaining: 20 }), 9);
});

test('local bash timeout is returned to the model so it can submit a bounded result', async (t) => {
  const { request, calls } = await fixture(t, 'openai-chat-completions', [
    { tools: [{ name: 'bash', args: { command: 'node -e "setTimeout(() => {}, 1000)"', timeout: 0.05 } }] },
    ok,
  ]);
  request.phase = 'explore_execute';
  const result = await runRequest(parseRequest(request));
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.equal(calls.length, 2);
  assert.match(JSON.stringify(calls[1].body), /Command timed out after 0\.05 seconds/);
});

test('remaining-time notice never aborts a provider stream or extends the hard deadline', async (t) => {
  const { request, calls } = await fixture(t, 'openai-chat-completions', [{ hang: true }, ok]);
  request.timeout_seconds = 1;
  request.conclude_timeout_seconds = 0.6;
  const audit = [];
  const started = performance.now();
  const result = await runRequest(parseRequest(request), { audit: (event) => audit.push(event) });
  const elapsed = performance.now() - started;
  assert.equal(result.status, 'cancelled', JSON.stringify(result));
  assert.equal(result.error.code, 'deadline_exceeded');
  assert.equal(calls.length, 1);
  assert.equal(audit.filter((event) => event.event === 'execution_time_notice').length, 1);
  assert.equal(audit.some((event) => event.event === 'readonly_conclusion'), false);
  assert.ok(elapsed < 1400, `original deadline was extended: ${elapsed}ms`);
});

test('authorized bash crosses the former soft deadline and completes before normal submission', async (t) => {
  const script = "setTimeout(()=>require('node:fs').writeFileSync('crossed-soft-deadline','completed'),400)";
  const { request, calls, cwd } = await fixture(t, 'openai-chat-completions', [
    { tools: [{ name: 'bash', args: { command: `node -e ${JSON.stringify(script)}` } }] },
    ok,
  ]);
  request.timeout_seconds = 2;
  request.conclude_timeout_seconds = 1.8;
  const audit = [];
  const result = await runRequest(parseRequest(request), { audit: (event) => audit.push(event) });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.equal(calls.length, 2);
  assert.ok(calls[1].body.tools.some((tool) => tool.function.name === 'bash'));
  assert.equal(audit.some((event) => event.event === 'result_repair'), false);
  assert.ok(!JSON.stringify({ result, audit }).includes('tool_permission'));
  const start = audit.findIndex((event) => event.event === 'tool_execution_start' && event.tool_name === 'bash');
  const notice = audit.findIndex((event) => event.event === 'execution_time_notice');
  const end = audit.findIndex((event) => event.event === 'tool_execution_end' && event.tool_name === 'bash');
  assert.ok(start >= 0 && start < notice && notice < end, JSON.stringify(audit));
  assert.equal(await readFile(join(cwd, 'crossed-soft-deadline'), 'utf8'), 'completed');
  assert.match(JSON.stringify(calls[1].body.messages), /authorized tools remain available/i);
});

test('new authorized action after the former soft deadline remains available in external verification', async (t) => {
  const { request, calls, cwd } = await fixture(t, 'openai-chat-completions', [
    async () => { await new Promise((resolve) => setTimeout(resolve, 350)); return { tools: [{ name: 'bash', args: { command: 'printf verified > after-soft-deadline' } }] }; },
    ok,
  ]);
  request.phase = 'external_verify_execute';
  request.timeout_seconds = 2;
  request.conclude_timeout_seconds = 1.8;
  const audit = [];
  const result = await runRequest(parseRequest(request), { audit: (event) => audit.push(event) });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.equal(calls.length, 2);
  assert.equal(await readFile(join(cwd, 'after-soft-deadline'), 'utf8'), 'verified');
  assert.equal(audit.filter((event) => event.event === 'execution_time_notice').length, 1);
  assert.ok(calls[1].body.tools.some((tool) => tool.function.name === 'bash'));
});

test('hard deadline and user cancellation stop an in-flight local process before return', async (t) => {
  for (const cancelled of [false, true]) await t.test(cancelled ? 'user_cancel' : 'hard_deadline', async (t) => {
    const { request, cwd, calls } = await fixture(t, 'openai-chat-completions', [
      { tools: [{ name: 'bash', args: { command: `printf '%s' "$$" > active.pid; exec node -e 'setInterval(()=>{},1000)'` } }] },
      ok,
    ]);
    request.timeout_seconds = cancelled ? 2 : 0.6;
    request.conclude_timeout_seconds = cancelled ? 1.8 : 0.4;
    const controller = new AbortController();
    let cancelTimer;
    t.after(() => clearTimeout(cancelTimer));
    const result = await runRequest(parseRequest(request), {
      signal: controller.signal,
      audit: (event) => {
        if (cancelled && event.event === 'tool_execution_start' && event.tool_name === 'bash') cancelTimer = setTimeout(() => controller.abort(), 150);
      },
    });
    assert.equal(result.status, 'cancelled', JSON.stringify(result));
    assert.equal(result.error.code, cancelled ? 'cancelled' : 'deadline_exceeded');
    assert.equal(calls.length, 1);
    const pid = Number(await readFile(join(cwd, 'active.pid'), 'utf8'));
    t.after(() => { try { process.kill(pid, 'SIGKILL'); } catch {} });
    assert.throws(() => process.kill(pid, 0), { code: 'ESRCH' });
  });
});

test('explicit conclude and report requests never gain normal execution tools', async (t) => {
  for (const phase of ['explore_conclude', 'report_execute']) await t.test(phase, async (t) => {
    const { request, calls, cwd } = await fixture(t, 'openai-chat-completions', [
      { tools: [{ name: 'bash', args: { command: 'touch forbidden-delivery-action' } }] },
    ]);
    request.phase = phase;
    const result = await runRequest(parseRequest(request));
    assert.equal(result.status, 'failed', JSON.stringify(result));
    assert.equal(result.error.code, 'tool_permission');
    assert.deepEqual(calls[0].body.tools.map((tool) => tool.function.name).sort(), ['read_evidence', 'register_artifact', 'submit_result']);
    await assert.rejects(access(join(cwd, 'forbidden-delivery-action')));
  });
});

test('bounded SDK bash timeout terminates the spawned process group', async (t) => {
  const { cwd } = await fixture(t);
  const pidPath = join(cwd, 'bash-descendant.pid');
  const script = "setInterval(()=>{},1000)";
  const command = `node -e ${JSON.stringify(script)} & child=$!; printf '%s' "$child" > bash-descendant.pid; wait "$child"`;
  const tool = createRequestBashToolDefinition(cwd, 0.2, () => 10);
  await assert.rejects(tool.execute('bash-process-group', { command }), /Command timed out after 0\.2 seconds/);
  const pid = Number(await readFile(pidPath, 'utf8'));
  t.after(() => { try { process.kill(pid, 'SIGKILL'); } catch {} });
  let running = true;
  for (let attempt = 0; attempt < 50 && running; attempt += 1) {
    try { process.kill(pid, 0); await new Promise((resolve) => setTimeout(resolve, 20)); }
    catch (error) { if (error.code === 'ESRCH') running = false; else throw error; }
  }
  assert.equal(running, false, `descendant process ${pid} survived the SDK timeout`);
});

test('single writer, explicit resume, project ownership and corrupt transcript fail closed', async (t) => {
  const { request, calls, freshCandidateCapture } = await fixture(t);
  const physical = (overrides = {}) => ({ ...request, ...overrides, capabilities: { ...request.capabilities, candidate_capture: freshCandidateCapture() } });
  const lease = await acquireSession(request);
  await assert.rejects(acquireSession({ ...request, resume: true, task_run_id: 'task-next', continuation: { source_task_run_id: 'task-test', execution_generation: 1 } }), { code: 'session_locked' });
  await lease.finish();
  request.session = `pi:${crypto.randomUUID()}`;
  let result = await runRequest(request);
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  result = await runRequest(physical({ resume: true, task_run_id: 'task-continuation', continuation: { source_task_run_id: 'task-test', execution_generation: 1 } }));
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  result = await runRequest(physical());
  assert.equal(result.error.code, 'session_exists');
  result = await runRequest(physical({ project_id: 'other-project', resume: true, task_run_id: 'task-next', continuation: { source_task_run_id: 'task-continuation', execution_generation: 2 } }));
  assert.equal(result.error.code, 'session_ownership');
  const file = join(request.session_root, request.session.slice(3), 'session.jsonl');
  await writeFile(file, (await readFile(file, 'utf8')) + '{bad');
  result = await runRequest(physical({ resume: true, task_run_id: 'task-next', continuation: { source_task_run_id: 'task-continuation', execution_generation: 2 } }));
  assert.equal(result.error.code, 'session_corrupt');
  assert.equal(calls.length, 2);
});

test('resume verifies phase family and original execution binding without new exploration', async (t) => {
  const { request, calls, freshCandidateCapture } = await fixture(t);
  const physical = (source) => ({ ...source, capabilities: { ...source.capabilities, candidate_capture: freshCandidateCapture() } });
  request.phase = 'explore_execute';
  request.binding = { slot_key: 'cell-1', surface_key: 'surface-1', test_family: 'authorization' };
  assert.equal((await runRequest(request)).status, 'succeeded');
  const delivery = { ...request, resume: true, conclude_only: true, task_run_id: 'delivery-run' };
  assert.equal((await runRequest(physical(delivery))).status, 'succeeded');
  assert.deepEqual(calls[1].body.tools.map((tool) => tool.function.name).sort(), ['read_evidence', 'register_artifact', 'submit_result']);
  assert.equal((await runRequest(physical({ ...delivery, phase: 'report_execute' }))).error?.code, 'session_ownership');
  assert.equal((await runRequest(physical({ ...delivery, binding: { ...request.binding, slot_key: 'cell-other' } }))).error?.code, 'session_ownership');
  assert.equal(calls.length, 2);
  const path = join(request.session_root, request.session.slice(3), 'manifest.json');
  const manifest = JSON.parse(await readFile(path, 'utf8'));
  assert.equal(manifest.last_task_run_id, 'delivery-run');
  delete manifest.binding_sha256;
  await writeFile(path, JSON.stringify(manifest));
  assert.equal((await runRequest(physical(delivery))).error?.code, 'session_ownership');
  assert.equal(calls.length, 2);
});

test('missing and legacy sessions never reopen as new', async (t) => {
  const { request, calls } = await fixture(t);
  const result = await runRequest({ ...request, resume: true, task_run_id: 'task-next', continuation: { source_task_run_id: 'task-test', execution_generation: 1 } });
  assert.equal(result.error.code, 'session_corrupt');
  assert.equal(calls.length, 0);
  assert.throws(() => parseRequest({ ...request, session: 'claude-old' }), { code: 'invalid_request' });
});

test('cooperative abort preserves original conversation for a new Run and binding', async (t) => {
  const prior = 'Prior observation from the original assistant turn';
  const { request, calls, freshCandidateCapture } = await fixture(t, 'openai-chat-completions', [
    { text: prior }, { hang: true }, ok, ok,
  ]);
  request.binding = { allowed_fact_ids: ['origin'] };
  const controller = new AbortController();
  const interrupted = await runRequest(parseRequest(request), {
    signal: controller.signal,
    audit: (event) => { if (event.event === 'result_repair') setTimeout(() => controller.abort(), 60); },
  });
  assert.equal(interrupted.status, 'cancelled', JSON.stringify(interrupted));
  assert.equal(calls.length, 2);
  const directory = join(request.session_root, request.session.slice(3));
  const firstManifest = JSON.parse(await readFile(join(directory, 'manifest.json'), 'utf8'));
  const originalTranscript = await readFile(join(directory, 'session.jsonl'), 'utf8');
  assert.match(originalTranscript, /Prior observation from the original assistant turn/);
  const continuation = {
    ...request, task_run_id: 'task-next', resume: true, prompt: 'Continue the unfinished work.',
    binding: { allowed_fact_ids: ['origin', 'accepted-new-fact'] },
    continuation: { source_task_run_id: 'task-test', execution_generation: 1 },
    capabilities: { ...request.capabilities, candidate_capture: freshCandidateCapture() },
  };
  assert.throws(() => parseRequest({ ...continuation, continuation: undefined }), { code: 'invalid_request', path: 'continuation' });
  assert.throws(() => parseRequest({ ...continuation, continuation: { source_task_run_id: 'task-next', execution_generation: 1 } }), { code: 'invalid_request', path: 'continuation.source_task_run_id' });
  assert.throws(() => parseRequest({ ...continuation, continuation: { source_task_run_id: 'task-test', execution_generation: -1 } }), { code: 'invalid_request', path: 'continuation.execution_generation' });
  assert.throws(() => parseRequest({ ...continuation, conclude_only: true }), { code: 'invalid_request', path: 'continuation' });
  assert.equal((await runRequest({ ...continuation, continuation: { source_task_run_id: 'wrong-run', execution_generation: 1 } })).error?.code, 'session_ownership');
  const writer = await acquireSession({ ...request, resume: true, conclude_only: true });
  assert.equal((await runRequest(continuation)).error?.code, 'session_locked');
  await writer.finish();
  const next = await runRequest(parseRequest(continuation));
  assert.equal(next.status, 'succeeded', JSON.stringify(next));
  assert.equal(next.task_run_id, 'task-next');
  assert.equal(calls.length, 3);
  assert.match(JSON.stringify(calls[2].body.messages), /Prior observation from the original assistant turn/);
  assert.match(JSON.stringify(calls[2].body.messages), /Continue the unfinished work/);
  assert.ok(calls[2].body.tools.some((tool) => tool.function.name === 'bash'));
  const secondManifest = JSON.parse(await readFile(join(directory, 'manifest.json'), 'utf8'));
  assert.equal(secondManifest.binding_sha256, firstManifest.binding_sha256);
  assert.notEqual(secondManifest.current_binding_sha256, firstManifest.binding_sha256);
  assert.equal(secondManifest.last_task_run_id, 'task-next');
  const transcript = await readFile(join(directory, 'session.jsonl'), 'utf8');
  assert.ok(transcript.startsWith(originalTranscript));
  assert.match(transcript, /cairn_execution_segment_v1/);
  assert.match(transcript, /"source_task_run_id":"task-test"/);
  assert.equal((await runRequest({ ...continuation, task_run_id: 'task-third', continuation: { source_task_run_id: 'wrong-run', execution_generation: 2 }, capabilities: { ...continuation.capabilities, candidate_capture: freshCandidateCapture() } })).error?.code, 'session_ownership');
  await writeFile(join(directory, 'session.jsonl'), transcript + '{bad');
  assert.equal((await runRequest({ ...continuation, task_run_id: 'task-third', continuation: { source_task_run_id: 'task-next', execution_generation: 2 }, capabilities: { ...continuation.capabilities, candidate_capture: freshCandidateCapture() } })).error?.code, 'session_corrupt');
  assert.equal(calls.length, 3);
});

test('structured Reason binding crosses the request parser as an opaque object', async (t) => {
  const { request } = await fixture(t);
  const structured = {
    version: 1,
    assessment_phase: 'baseline',
    issued_at: '2026-09-19T08:00:00Z',
    eligible_cells: [{ slot_key: 'cell:authorization', strategy: { mode: 'any' } }],
    digest: 'a'.repeat(64),
  };
  const parsed = parseRequest({
    ...request,
    phase: 'reason_execute',
    binding: { max_intents: 1, structured_intent_binding: structured },
  });
  assert.deepEqual(parsed.binding.structured_intent_binding, structured);
  for (const invalid of [null, [], 'invalid', 42, false]) {
    assert.throws(
      () => parseRequest({ ...request, binding: { structured_intent_binding: invalid } }),
      { code: 'invalid_request', path: 'binding.structured_intent_binding' },
    );
  }
});

test('conflicting submissions in same batch fail; repeated identical submissions are idempotent', async (t) => {
  const { request } = await fixture(t, 'openai-chat-completions', [{ tools: [ok.tools[0], { name: 'submit_result', args: { accepted: false, reason: 'blocked' } }] }]);
  const result = await runRequest(request);
  assert.equal(result.status, 'failed', JSON.stringify(result));
  assert.equal(result.error.code, 'submission_conflict');
});

test('large assistant output stays out of audit and unknown usage remains null', async (t) => {
  const { request } = await fixture(t, 'openai-chat-completions', [{ ...ok, text: 'x'.repeat(1024 * 1024), noUsage: true }]);
  const audit = [];
  const result = await runRequest(request, { audit: (e) => audit.push(e) });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.equal(result.usage, null);
  assert.ok(JSON.stringify(audit).length < 10000);
});

test('unsupported endpoint and unknown phase are rejected without a request', async (t) => {
  const { request, calls } = await fixture(t);
  request.model.inference_endpoint += '?key=secret';
  const result = await runRequest(request);
  assert.equal(result.error.code, 'unsupported_model_config');
  assert.equal(calls.length, 0);
  assert.throws(() => parseRequest({ ...request, phase: 'explore' }), { code: 'invalid_request', path: 'phase' });
  assert.throws(() => parseRequest({ ...request, phase: 'healthcheck' }), { code: 'invalid_request', path: 'phase' });
});

for (const status of [401, 429, 503]) test(`provider ${status} has bounded retries and no result repair`, async (t) => {
  const { request, calls } = await fixture(t, 'openai-chat-completions', [{ status, error: 'fixture-secret-key should not be echoed' }]);
  const audit = [];
  const result = await runRequest(request, { audit: (e) => audit.push(e) });
  assert.equal(result.status, 'failed');
  assert.equal(result.error.code, status === 401 ? 'provider_auth' : status === 429 ? 'provider_rate_limit' : 'provider_error');
  assert.ok(calls.length <= 2);
  assert.ok(!audit.some((e) => e.event === 'result_repair'));
  assert.ok(!JSON.stringify(result).includes('fixture-secret-key'));
});
