import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { copyFile, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { runRequest } from '../dist/runner.js';
import { parseRequest } from '../dist/protocol.js';
import { requestModel } from '../dist/model.js';
import { fixture, ok, parseFixtureRequest } from './fixtures.mjs';

const contracts = JSON.parse(await readFile(new URL('../contracts/submission-v1.json', import.meta.url), 'utf8'));
const { segmentSchema, markdownSegmentSchema } = JSON.parse(execFileSync(process.env.PYTHON_BIN || 'python3', ['-c', 'import json; from cairn.report_segments import REPORT_SEGMENT_ADAPTER, MARKDOWN_SEGMENT_ADAPTER; print(json.dumps({"segmentSchema": REPORT_SEGMENT_ADAPTER.json_schema(), "markdownSegmentSchema": MARKDOWN_SEGMENT_ADAPTER.json_schema()}))'], {
  encoding: 'utf8', env: { ...process.env, PYTHONPATH: fileURLToPath(new URL('../../../cairn/src/', import.meta.url)) },
}));
const save = (key, content) => ({ name: 'save_report_segment', args: { key, content } });
const summary = (text = '合成项目材料综合说明') => ({ kind: 'executive_summary', text });
function finish(saved) {
  return { tools: [{ name: 'submit_result', args: { accepted: true, data: { schema_version: 5, segment_refs: saved.map((artifact_handle) => ({ artifact_handle })) } } }] };
}
async function reportFixture(t, script, protocol = 'openai-chat-completions') {
  const result = await fixture(t, protocol, script);
  const { request } = result;
  const captureId = request.capabilities.candidate_capture.capture_id;
  const manifestPath = `/tmp/cairn-prompts/pi-report-segments-${captureId}.json`;
  await mkdir('/tmp/cairn-prompts', { recursive: true, mode: 0o700 });
  await writeFile(manifestPath, '', { flag: 'wx', mode: 0o600 });
  t.after(() => rm(manifestPath, { force: true }));
  request.phase = 'report_execute';
  delete request.capabilities.task_execution;
  request.result_schema = contracts.submit_result_schemas.report_execute;
  request.capabilities.candidate_capture.max_manifest_bytes = 128 * 1024;
  request.capabilities.report_segments = {
    binding: { project_id: request.project_id, task_run_id: request.task_run_id, generation_token: 'synthetic-generation', input_snapshot_sha256: 'a'.repeat(64), report_template_digest_sha256: 'b'.repeat(64) },
    segment_schema: segmentSchema, manifest_path: manifestPath,
  };
  return { ...result, manifestPath };
}

for (const protocol of ['anthropic-messages', 'openai-chat-completions', 'openai-responses']) test(`${protocol}: configured report output budget reaches the actual SDK payload unchanged`, async (t) => {
  const saved = [];
  const { request, calls } = await reportFixture(t, [{ tools: [save('summary', summary())] }, () => finish(saved)], protocol);
  request.model.max_output_tokens = 48000;
  const parsed = parseRequest(request);
  assert.equal(requestModel(parsed).maxTokens, 48000);
  const result = await runRequest(parsed, { audit: (event) => { if (event.event === 'report_segment_saved') saved.push(event.artifact_handle); } });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.equal(calls[0].body.max_tokens ?? calls[0].body.max_completion_tokens ?? calls[0].body.max_output_tokens, 48000);
  for (const invalid of [undefined, 0, -1, 1.5, '16384', Number.MAX_SAFE_INTEGER + 1]) {
    assert.throws(() => parseRequest({ ...request, model: { ...request.model, max_output_tokens: invalid } }), { code: 'invalid_request', path: 'model.max_output_tokens' });
  }
});

test('15 long findings save 47 bounded structural segments through the SDK and submit only refs', async (t) => {
  const saved = [];
  const audit = [];
  const packet = { project: { id: 'project-test', name: '合成项目' }, vulnerabilities: [], observations: [], artifacts: [], artifact_contents: {}, facts: [], accepted_readable_findings: [] };
  const script = [];
  let totalContentBytes = 0;
  for (let i = 1; i <= 15; i += 1) {
    const vulnerability_id = `v${i}`;
    const observation_id = `o${i}`;
    const original = `HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n合成用户甲读取到用户乙的对象 ${i}。\r\n`;
    packet.vulnerabilities.push({ id: vulnerability_id, title: `合成越权发现 ${i}`, severity: 'high' });
    packet.observations.push({ id: observation_id, vulnerability_id, evidence_items: [{ id: `e${i}`, kind: 'http', source: 'capture', response: { artifact_id: `a${i}` } }] });
    packet.artifacts.push({ id: `a${i}`, project_id: packet.project.id, kind: 'raw_output', sha256: createHash('sha256').update(original).digest('hex'), size: Buffer.byteLength(original) });
    packet.artifact_contents[`a${i}`] = { text: original };
    const identity = { kind: 'finding', vulnerability_id, observation_id };
    const description = `合成发现 ${i} 的必要前提与已验证影响。` + '这是仅用于离线合同验证的详细业务场景：普通身份读取其他所有者对象，已记录正常归属对照；没有访问真实目标。'.repeat(110);
    const tools = [
      save(`${vulnerability_id}-description`, { ...identity, description }),
      save(`${vulnerability_id}-steps`, { ...identity, steps: [1, 2, 3].map((step) => ({ operation: `合成有序步骤 ${step}：复用冻结参数与身份，不重新访问目标。`, observed: `合成记录 ${i}/${step}：已保存观察。`, evidence: [{ evidence_item_id: `e${i}`, selections: [{ side: 'response', artifact_id: `a${i}`, byte_start: 0, byte_end: Buffer.byteLength(original) }] }] })) }),
      save(`${vulnerability_id}-remediation`, { ...identity, remediation: '在对象读取处核对当前主体与资源归属；该建议未实施或验证。', limitations: '仅依据冻结合成材料。' }),
    ];
    totalContentBytes += Buffer.byteLength(JSON.stringify(tools));
    script.push({ tools });
  }
  script.push({ tools: [save('summary', summary()), save('overall-remediation', { kind: 'overall_remediation', text: '优先修复服务端对象归属校验，并复核同类读取路径；没有进行新的测试。' })] });
  script.push(() => finish(saved));
  const { request, calls, manifestPath, candidateSpoolPath } = await reportFixture(t, script);
  const result = await runRequest(parseRequest(request), { audit: (event) => { audit.push(event); if (event.event === 'report_segment_saved') saved.push(event.artifact_handle); } });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.ok(totalContentBytes > 256 * 1024, 'synthetic content did not reproduce a large final-body boundary');
  assert.equal(saved.length, 47);
  assert.equal(new Set(saved).size, 47);
  assert.equal(calls.length, 17);
  assert.equal(result.result.data.artifacts.length, 47);
  assert.deepEqual(Object.keys(result.result.data).sort(), ['artifacts', 'schema_version', 'segment_refs']);
  assert.equal(result.result.data.segment_refs.length, 47);
  const manifest = JSON.parse(await readFile(manifestPath));
  assert.equal(manifest.protocol, 'cairn-report-segment-manifest-v1');
  assert.deepEqual(manifest.binding, request.capabilities.report_segments.binding);
  assert.deepEqual(manifest.artifacts, result.result.data.artifacts);
  const records = {};
  for (const artifact of manifest.artifacts) {
    const raw = await readFile(artifact.path);
    assert.equal(artifact.kind, 'file');
    assert.equal(createHash('sha256').update(raw).digest('hex'), artifact.sha256);
    assert.equal(raw.length, artifact.size_bytes);
    const record = JSON.parse(raw);
    records[artifact.handle] = record;
    assert.deepEqual(Object.keys(record).sort(), ['binding', 'content', 'key', 'protocol']);
    assert.deepEqual(record.binding, manifest.binding);
    assert.equal(record.protocol, 'cairn-report-segment-v1');
  }
  for (const call of calls.slice(1)) {
    assert.ok(!JSON.stringify(call.body.messages).includes('这是仅用于离线合同验证的详细业务场景'), 'completed report bodies were reinjected into the model');
    assert.ok(call.body.tools.every((tool) => !['bash', 'capture_http_evidence', 'write'].includes(tool.function.name)));
    assert.equal(call.body.max_tokens ?? call.body.max_completion_tokens, 16384);
  }
  const finalRaw = JSON.parse(calls.at(-1).body.messages.filter((message) => message.role === 'user').at(-1).content.match(/\[.*\]/s)[0]);
  assert.equal(finalRaw.length, 47);
  assert.ok(!JSON.stringify(audit).includes('fixture-secret-key'));
  if (process.env.REPORT_SEGMENT_EXPORT_DIR) {
    const directory = process.env.REPORT_SEGMENT_EXPORT_DIR;
    await mkdir(join(directory, 'artifacts'), { recursive: true });
    const artifact_files = {};
    for (const artifact of manifest.artifacts) {
      const path = join(directory, 'artifacts', `${artifact.handle}.json`);
      await copyFile(artifact.path, path);
      artifact_files[artifact.handle] = path;
    }
    await copyFile(candidateSpoolPath, join(directory, 'candidate-spool.bin'));
    await writeFile(join(directory, 'bundle.json'), JSON.stringify({ result, manifest, records, artifact_files, packet, audit, total_content_bytes: totalContentBytes, sdk_calls: calls.length }));
  }
});

test('Markdown writer can reread saved prose and select a revision without overwriting the original', async (t) => {
  const saved = [];
  const first = { kind: 'markdown', markdown: '# 合成报告\n\n## 概述\n\n待修订的原文。', sources: [] };
  const revised = { ...first, markdown: '# 合成报告\n\n## 概述\n\n修订后的连续正文。' };
  let originalPath;
  const { request, manifestPath } = await reportFixture(t, [
    { tools: [save('draft', first)] },
    (call) => {
      const navigation = call.body.messages.filter((message) => message.role === 'user').at(-1).content;
      const refs = JSON.parse(navigation.match(/\[.*\]/s)[0]);
      assert.equal(refs[0].artifact_handle, saved[0]);
      originalPath = refs[0].path;
      return { tools: [{ name: 'read_evidence', args: { path: originalPath } }] };
    },
    (call) => {
      const result = JSON.parse(call.body.messages.filter((message) => message.role === 'tool').at(-1).content);
      assert.equal(JSON.parse(result.text).content.markdown, first.markdown);
      return { tools: [save('revised', revised)] };
    },
    () => finish([saved[1]]),
  ]);
  request.capabilities.report_segments.segment_schema = markdownSegmentSchema;
  const result = await runRequest(parseRequest(request), { audit: (event) => {
    if (event.event === 'report_segment_saved') saved.push(event.artifact_handle);
  } });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.deepEqual(result.result.data.segment_refs, [{ artifact_handle: saved[1] }]);
  const manifest = JSON.parse(await readFile(manifestPath));
  assert.equal(manifest.artifacts.length, 2);
  assert.deepEqual(JSON.parse(await readFile(originalPath)).content, first);
  assert.deepEqual(JSON.parse(await readFile(manifest.artifacts[1].path)).content, revised);
});

test('finding segment explicitly reuses frozen steps without restating actions or original ranges', async (t) => {
  const saved = [];
  const content = { kind: 'finding', vulnerability_id: 'v1', observation_id: 'o1', reuse_source: ['description', 'steps', 'remediation'] };
  const { request, manifestPath } = await reportFixture(t, [
    { tools: [save('source-finding', content)] },
    () => finish(saved),
  ]);
  const result = await runRequest(parseRequest(request), { audit: (event) => {
    if (event.event === 'report_segment_saved') saved.push(event.artifact_handle);
  } });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  const manifest = JSON.parse(await readFile(manifestPath));
  assert.equal(manifest.artifacts.length, 1);
  assert.deepEqual(JSON.parse(await readFile(manifest.artifacts[0].path)).content, content);
  assert.deepEqual(result.result.data.segment_refs, saved.map((artifact_handle) => ({ artifact_handle })));
});

test('report flow selection is saved against frozen schema without live traffic tools', async (t) => {
  const saved = [];
  const content = { kind: 'finding', vulnerability_id: 'v1', observation_id: 'o1', steps: [{ operation: '引用冻结流量', observed: '不重新访问目标', evidence: [{ evidence_item_id: 'flow-item', selections: [{ side: 'response', flow_id: 'flow-1', byte_start: 0, byte_end: 6 }] }] }] };
  const { request, calls, manifestPath } = await reportFixture(t, [{ tools: [save('flow-step', content)] }, () => finish(saved)]);
  const result = await runRequest(parseRequest(request), { audit: (event) => { if (event.event === 'report_segment_saved') saved.push(event.artifact_handle); } });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  const manifest = JSON.parse(await readFile(manifestPath));
  assert.deepEqual(JSON.parse(await readFile(manifest.artifacts[0].path)).content, content);
  for (const call of calls) assert.ok(call.body.tools.every((tool) => !['list_http_flows', 'read_http_flow', 'capture_http_evidence', 'bash'].includes(tool.function.name)));
});

test('report segment keys are idempotent and conflicting content cannot overwrite a durable segment', async (t) => {
  const saved = [];
  const { request, manifestPath } = await reportFixture(t, [
    { tools: [save('summary', summary('原始合成说明'))] },
    { tools: [save('summary', summary('原始合成说明'))] },
    { tools: [save('summary', summary('不同内容必须拒绝'))] },
    () => finish([...new Set(saved)]),
  ]);
  const audit = [];
  const result = await runRequest(parseRequest(request), { audit: (event) => { audit.push(event); if (event.event === 'report_segment_saved') saved.push(event.artifact_handle); } });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.equal(saved.length, 2);
  assert.equal(saved[0], saved[1]);
  assert.ok(audit.some((event) => event.event === 'tool_execution_end' && event.tool_name === 'save_report_segment' && event.is_error));
  const manifest = JSON.parse(await readFile(manifestPath));
  assert.equal(manifest.artifacts.length, 1);
  assert.equal(JSON.parse(await readFile(manifest.artifacts[0].path)).content.text, '原始合成说明');
});

for (const protocol of ['anthropic-messages', 'openai-chat-completions']) test(`${protocol}: length rejects incomplete segment JSON before SDK execution and shrinks only the unfinished unit`, async (t) => {
  const saved = [];
  const { request, calls, manifestPath } = await reportFixture(t, [
    { tools: [save('done', summary('已经持久保存，不重做'))] },
    { tools: [{ name: 'save_report_segment', rawArgs: '{"key":"unfinished","content":{"kind":"overall_remediation","text":"尚未完成' }], stopReason: protocol === 'anthropic-messages' ? 'max_tokens' : 'length' },
    { tools: [save('smaller-unit', { kind: 'overall_remediation', text: '缩小后的完整内容单元' })] },
    () => finish(saved),
  ], protocol);
  const audit = [];
  const result = await runRequest(parseRequest(request), { audit: (event) => { audit.push(event); if (event.event === 'report_segment_saved') saved.push(event.artifact_handle); } });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.equal(calls.length, 4);
  assert.equal(saved.length, 2);
  const manifest = JSON.parse(await readFile(manifestPath));
  assert.equal(manifest.artifacts.length, 2);
  const keys = await Promise.all(manifest.artifacts.map(async (artifact) => JSON.parse(await readFile(artifact.path)).key));
  assert.deepEqual(keys, ['done', 'smaller-unit']);
  assert.equal(audit.filter((event) => event.event === 'report_output_recovery').length, 1);
  assert.ok(audit.some((event) => event.event === 'report_segment_rejected' && event.code === 'model_output_limit' && event.keyword === 'strict_json'));
  assert.equal(audit.filter((event) => event.event === 'result_repair').length, 0);
  assert.match(JSON.stringify(calls[2].body.messages), /structurally smaller/);
  assert.ok(!JSON.stringify(calls[2].body.messages).includes('尚未完成'), 'truncated tool arguments were reinjected into recovery');
  if (protocol === 'anthropic-messages') {
    const saveTool = calls[0].body.tools.find((tool) => tool.name === 'save_report_segment');
    assert.deepEqual(saveTool.input_schema.$defs, segmentSchema.$defs);
    assert.deepEqual(saveTool.input_schema.additionalProperties, false);
  }
});

for (const malformed of [false, true]) test(`length never accepts a ${malformed ? 'partial' : 'syntactically complete'} final tool call`, async (t) => {
  const saved = [];
  const { request, calls, candidateSpoolPath } = await reportFixture(t, [
    { tools: [save('done', summary())] },
    () => ({ tools: [{ name: 'submit_result', ...(malformed ? { rawArgs: '{"accepted":true,"data":{"schema_version":5,"segment_refs":[' } : { args: finish(saved).tools[0].args }) }], stopReason: 'length' }),
    () => finish(saved),
  ]);
  const audit = [];
  const result = await runRequest(parseRequest(request), { audit: (event) => { audit.push(event); if (event.event === 'report_segment_saved') saved.push(event.artifact_handle); } });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.equal(calls.length, 3);
  assert.equal(audit.filter((event) => event.event === 'submission_validated').length, 1);
  assert.equal(audit.filter((event) => event.event === 'report_output_recovery').length, 1);
  assert.equal(audit.filter((event) => event.event === 'result_repair').length, 0);
  const spool = await readFile(candidateSpoolPath);
  assert.ok(spool.includes(Buffer.from(malformed ? 'strict_json' : 'model_output_limit')));
});

test('segment validation rejects SDK type coercion and oversized content before persistence', async (t) => {
  const saved = [];
  const { request, manifestPath } = await reportFixture(t, [
    { tools: [save('wrong-type', summary(7))] },
    { tools: [save('too-large', summary('长'.repeat(400000)))] },
    { tools: [save('valid', summary())] },
    () => finish(saved),
  ]);
  const result = await runRequest(parseRequest(request), { audit: (event) => { if (event.event === 'report_segment_saved') saved.push(event.artifact_handle); } });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  const manifest = JSON.parse(await readFile(manifestPath));
  assert.equal(manifest.artifacts.length, 1);
  assert.equal(JSON.parse(await readFile(manifest.artifacts[0].path)).key, 'valid');
});

for (const stopMode of ['exhausted', 'cancelled', 'deadline', 'provider']) test(`${stopMode}: saved segments remain durable material and cannot become accepted results`, async (t) => {
  const controller = new AbortController();
  const truncated = { tools: [{ name: 'save_report_segment', rawArgs: '{"key":"unfinished","content":' }], stopReason: 'length' };
  const { request, calls, manifestPath } = await reportFixture(t, [
    { tools: [save('saved-before-failure', summary())] },
    stopMode === 'exhausted' ? truncated : stopMode === 'provider' ? { status: 500 } : { hang: true },
  ]);
  if (stopMode === 'deadline') request.timeout_seconds = 0.8;
  const audit = [];
  const result = await runRequest(parseRequest(request), { signal: controller.signal, audit: (event) => {
    audit.push(event);
    if (stopMode === 'cancelled' && event.event === 'report_segment_saved') controller.abort();
  } });
  assert.equal(result.status, ['cancelled', 'deadline'].includes(stopMode) ? 'cancelled' : 'failed', JSON.stringify(result));
  assert.equal(result.error.code, { exhausted: 'model_output_limit', cancelled: 'cancelled', deadline: 'deadline_exceeded', provider: 'provider_error' }[stopMode]);
  assert.equal(result.result, null);
  if (stopMode === 'exhausted') {
    assert.equal(calls.length, 4);
    assert.equal(audit.filter((event) => event.event === 'report_output_recovery').length, 2);
  }
  const manifest = JSON.parse(await readFile(manifestPath));
  assert.equal(manifest.artifacts.length, 1);
  assert.equal(JSON.parse(await readFile(manifest.artifacts[0].path)).key, 'saved-before-failure');
  const sourceFiles = await readdir(join(request.cwd, '.cairn', 'report-segments', request.capabilities.candidate_capture.capture_id));
  assert.equal(sourceFiles.length, 1);
  assert.ok(!audit.some((event) => event.event === 'submission_validated'));
});

test('report capability is bound to its request and cannot expand ordinary exploration limits', async (t) => {
  const { request, freshTaskExecution } = await reportFixture(t, [ok]);
  assert.doesNotThrow(() => parseRequest(request));
  const capability = request.capabilities.report_segments;
  for (const altered of [
    { ...capability, binding: { ...capability.binding, project_id: 'wrong-project' } },
    { ...capability, binding: { ...capability.binding, task_run_id: 'wrong-task' } },
    { ...capability, binding: { ...capability.binding, input_snapshot_sha256: 'invalid' } },
    { ...capability, manifest_path: '/tmp/unbound-manifest.json' },
  ]) assert.throws(() => parseRequest({ ...request, capabilities: { ...request.capabilities, report_segments: altered } }), { code: 'invalid_request' });
  const task_execution = freshTaskExecution(request.task_run_id);
  assert.throws(() => parseFixtureRequest({ ...request, phase: 'explore_execute', capabilities: { ...request.capabilities, task_execution } }), { code: 'invalid_request', path: 'capabilities.report_segments' });
  assert.throws(() => parseFixtureRequest({ ...request, phase: 'explore_execute', capabilities: { candidate_capture: request.capabilities.candidate_capture, task_execution } }), { code: 'invalid_request', path: 'capabilities.candidate_capture.max_manifest_bytes' });
});
