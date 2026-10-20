import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash, randomUUID } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { chmod, lstat, mkdir, readFile, rm, symlink, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { CandidateSpool } from '../dist/candidate-spool.js';
import { parseRequest } from '../dist/protocol.js';
import { runRequest } from '../dist/runner.js';
import { fixture } from './fixtures.mjs';

const MAGIC = Buffer.from('CRNCND01', 'ascii');
const spoolRoot = '/tmp/cairn-prompts';
function capability(path, captureId = randomUUID(), overrides = {}) {
  return {
    protocol: 'cairn-candidate-spool-v1',
    spool_path: path,
    capture_id: captureId,
    max_candidates: 16,
    max_candidate_bytes: 2 * 1024 * 1024,
    max_manifest_bytes: 4 * 1024,
    max_spool_bytes: 34 * 1024 * 1024,
    forbidden_literals: [],
    ...overrides,
  };
}
function spoolPath() { return join(spoolRoot, `pi-candidates-${randomUUID()}.bin`); }
function decodeFrames(buffer) {
  const frames = [];
  let offset = 0;
  while (offset < buffer.length) {
    const start = offset;
    assert.deepEqual(buffer.subarray(offset, offset + 8), MAGIC);
    offset += 8;
    const manifestLength = buffer.readUInt32BE(offset); offset += 4;
    const rawLength = buffer.readUInt32BE(offset); offset += 4;
    const manifestBytes = buffer.subarray(offset, offset + manifestLength); offset += manifestLength;
    const raw = buffer.subarray(offset, offset + rawLength); offset += rawLength;
    const checksum = buffer.subarray(offset, offset + 32); offset += 32;
    assert.deepEqual(checksum, createHash('sha256').update(buffer.subarray(start, offset - 32)).digest());
    frames.push({ manifestBytes, manifest: JSON.parse(manifestBytes), raw, checksum });
  }
  return frames;
}
function encodeFrameBytes(manifestBytes, raw = Buffer.alloc(0)) {
  const header = Buffer.alloc(16);
  MAGIC.copy(header);
  header.writeUInt32BE(manifestBytes.length, 8);
  header.writeUInt32BE(raw.length, 12);
  const body = Buffer.concat([header, manifestBytes, raw]);
  return Buffer.concat([body, createHash('sha256').update(body).digest()]);
}
function encodeFrame(manifest, raw = Buffer.alloc(0)) {
  return encodeFrameBytes(Buffer.from(JSON.stringify(manifest)), raw);
}

async function requestWithSpool(t, overrides = {}) {
  const { request } = await fixture(t);
  const captureId = randomUUID();
  const path = join(spoolRoot, `pi-candidates-${captureId}.bin`);
  t.after(() => rm(path, { force: true }));
  request.capabilities = { candidate_capture: capability(path, captureId, overrides) };
  return { request: parseRequest(request), path };
}

test('candidate capability is required and strictly bounds identity, paths, literals, and sizes', async (t) => {
  const { request } = await fixture(t);
  assert.throws(() => parseRequest({ ...request, capabilities: undefined }), { code: 'invalid_request', path: 'capabilities' });
  assert.throws(() => parseRequest({ ...request, capabilities: {} }), { code: 'invalid_request', path: 'capabilities' });
  const original = request.capabilities.candidate_capture;
  for (const [field, value] of [
    ['max_candidates', 17], ['max_candidate_bytes', 2 * 1024 * 1024 + 1],
    ['max_manifest_bytes', 16 * 1024 + 1], ['max_spool_bytes', 34 * 1024 * 1024 + 1],
  ]) {
    assert.throws(() => parseRequest({ ...request, capabilities: { candidate_capture: { ...original, [field]: value } } }), { code: 'invalid_request', path: `capabilities.candidate_capture.${field}` });
  }
  assert.throws(() => parseRequest({ ...request, capabilities: { candidate_capture: { ...original, spool_path: join(request.cwd, 'candidate.bin') } } }), { code: 'invalid_request', path: 'capabilities.candidate_capture.spool_path' });
  assert.throws(() => parseRequest({ ...request, capabilities: { candidate_capture: { ...original, forbidden_literals: Array.from({ length: 33 }, (_, index) => `secret-${index}`) } } }), { code: 'invalid_request', path: 'capabilities.candidate_capture.forbidden_literals' });
  assert.throws(() => parseRequest({ ...request, capabilities: { candidate_capture: { ...original, unexpected: true } } }), { code: 'invalid_request', path: 'capabilities.candidate_capture' });
});

test('production explore schema and null binding have cross-language canonical digest goldens', async (t) => {
  const { request, path } = await requestWithSpool(t);
  const contracts = JSON.parse(await readFile(new URL('../contracts/submission-v1.json', import.meta.url), 'utf8'));
  request.result_schema = contracts.submit_result_schemas.explore_execute;
  await new CandidateSpool(request).appendCandidate(1, 0, Buffer.from('{}'));
  const manifest = decodeFrames(await readFile(path))[0].manifest;
  assert.equal(manifest.binding_sha256, '74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b');
  assert.equal(manifest.result_schema_sha256, '13aef69875fb7aa0ace390329a0af4fd262fc420a6b39cbb50dfa86c282ee6dd');
});

test('candidate spool preserves malformed Unicode and CRLF bytes in a checksummed binary frame', async (t) => {
  const { request, path } = await requestWithSpool(t);
  const raw = Buffer.from('{\r\n  "accepted": tru, "note": "雪"\r\n}', 'utf8');
  const spool = new CandidateSpool(request);
  const attempt = await spool.appendCandidate(1, 3, raw);
  const frames = decodeFrames(await readFile(path));
  assert.equal(frames.length, 1);
  assert.deepEqual(frames[0].raw, raw);
  assert.equal(attempt.producer_candidate_key, `${request.capabilities.candidate_capture.capture_id}:1:3`);
  assert.equal(attempt.parent_producer_candidate_key, null);
  assert.deepEqual(frames[0].manifest, {
    artifacts: [],
    assistant_turn: 1,
    binding_sha256: '74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b',
    capture_id: request.capabilities.candidate_capture.capture_id,
    content_index: 3,
    parent_producer_candidate_key: null,
    phase: request.phase,
    producer_candidate_key: attempt.producer_candidate_key,
    project_id: request.project_id,
    protocol: 'cairn-candidate-frame-v1',
    raw_sha256: createHash('sha256').update(raw).digest('hex'),
    raw_size_bytes: raw.length,
    record_kind: 'candidate',
    result_schema_sha256: '2cda507f000326e1443611448978582f45b5d5a851ceeec12e88c4a3e50dc405',
    session: request.session,
    task_run_id: request.task_run_id,
  });
  assert.equal(frames[0].manifestBytes.toString('utf8'), JSON.stringify(frames[0].manifest));
  assert.equal(attempt.request_sha256, createHash('sha256').update(frames[0].manifestBytes).update(raw).digest('hex'));
});

for (const protocol of ['anthropic-messages', 'openai-chat-completions', 'openai-responses']) {
  test(`${protocol} concatenates SDK argument deltas without changing Unicode, CRLF, or whitespace`, async (t) => {
    const parts = ['{\r\n  "accepted": false,\r\n  "reason": "', '雪', '"\r\n}'];
    const raw = parts.join('');
    const { request, candidateSpoolPath } = await fixture(t, protocol, [
      { tools: [{ name: 'submit_result', rawArgChunks: parts }] },
    ]);
    const result = await runRequest(parseRequest(request));
    assert.equal(result.status, 'succeeded', JSON.stringify(result));
    const frame = decodeFrames(await readFile(candidateSpoolPath)).find((entry) => entry.manifest.record_kind === 'candidate');
    assert.deepEqual(frame.raw, Buffer.from(raw));
  });
}

test('candidate spool rejects raw/count limits and checksum-corrupt existing frames without truncation', async (t) => {
  const rawLimited = await requestWithSpool(t, { max_candidate_bytes: 3 });
  await assert.rejects(new CandidateSpool(rawLimited.request).appendCandidate(1, 0, Buffer.from('four')), { code: 'candidate_spool_limit' });
  await assert.rejects(readFile(rawLimited.path), { code: 'ENOENT' });

  const countLimited = await requestWithSpool(t, { max_candidates: 1 });
  const countSpool = new CandidateSpool(countLimited.request);
  await countSpool.appendCandidate(1, 0, Buffer.alloc(0));
  await assert.rejects(countSpool.appendCandidate(2, 0, Buffer.from('{}')), { code: 'candidate_spool_limit' });
  const beforeCorruption = await readFile(countLimited.path);
  assert.equal(decodeFrames(beforeCorruption).length, 1);
  const corrupted = Buffer.from(beforeCorruption);
  corrupted[corrupted.length - 1] ^= 0xff;
  await writeFile(countLimited.path, corrupted, { mode: 0o600 });
  await assert.rejects(countSpool.appendCandidate(2, 0, Buffer.from('{}')), { code: 'candidate_spool_invalid' });
  assert.deepEqual(await readFile(countLimited.path), corrupted);
});

test('candidate spool rejects a checksummed UTF-8 BOM manifest', async (t) => {
  const { request, path } = await requestWithSpool(t);
  const spool = new CandidateSpool(request);
  await spool.appendCandidate(1, 0, Buffer.from('{}'));
  const frame = decodeFrames(await readFile(path))[0];
  const bomManifest = Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), frame.manifestBytes]);
  await writeFile(path, encodeFrameBytes(bomManifest, frame.raw), { mode: 0o600 });

  await assert.rejects(
    spool.appendCandidate(2, 0, Buffer.from('{}')),
    { code: 'candidate_spool_invalid' },
  );
});

test('candidate spool rejects out-of-range and non-monotonic producer coordinates', async (t) => {
  const outOfRange = await requestWithSpool(t);
  await assert.rejects(
    new CandidateSpool(outOfRange.request).appendCandidate(1_000_001, 0, Buffer.from('{}')),
    { code: 'candidate_manifest_invalid' },
  );
  await assert.rejects(readFile(outOfRange.path), { code: 'ENOENT' });

  const nonMonotonic = await requestWithSpool(t);
  const spool = new CandidateSpool(nonMonotonic.request);
  await spool.appendCandidate(2, 1, Buffer.from('{}'));
  const before = await readFile(nonMonotonic.path);
  await assert.rejects(
    spool.appendCandidate(2, 0, Buffer.from('{}')),
    { code: 'candidate_manifest_invalid' },
  );
  assert.deepEqual(await readFile(nonMonotonic.path), before);
});

test('candidate spool accepts read-only shared access and rejects shared write access', async (t) => {
  await mkdir(spoolRoot, { recursive: true, mode: 0o700 });
  const originalMode = (await lstat(spoolRoot)).mode & 0o777;
  await chmod(spoolRoot, 0o755);
  try {
    const { request, path } = await requestWithSpool(t);
    const spool = new CandidateSpool(request);
    await spool.appendCandidate(1, 0, Buffer.from('{}'));
    assert.equal(decodeFrames(await readFile(path)).length, 1);

    await chmod(spoolRoot, 0o775);
    await assert.rejects(
      spool.appendCandidate(2, 0, Buffer.from('{}')),
      { code: 'candidate_spool_invalid' },
    );
    assert.equal(decodeFrames(await readFile(path)).length, 1);
  } finally {
    await chmod(spoolRoot, originalMode);
  }
});

test('candidate append rejects symlink spool targets without changing the target', async (t) => {
  const { request, path } = await requestWithSpool(t);
  await mkdir(spoolRoot, { recursive: true, mode: 0o700 });
  const target = join('/tmp', `cairn-candidate-target-${randomUUID()}`);
  await writeFile(target, 'unchanged', { mode: 0o600 });
  await symlink(target, path);
  t.after(() => rm(target, { force: true }));
  await assert.rejects(new CandidateSpool(request).appendCandidate(1, 0, Buffer.from('{}')), { code: 'candidate_spool_invalid' });
  assert.equal(await readFile(target, 'utf8'), 'unchanged');
});

test('repairs append immutable lineage, artifact identity, and safe outcomes', async (t) => {
  const { request, path } = await requestWithSpool(t);
  const spool = new CandidateSpool(request);
  const artifact = { handle: 'local-00000000-0000-4000-8000-000000000001', kind: 'log', sha256: 'a'.repeat(64), size_bytes: 19 };
  const firstRaw = Buffer.from('{"accepted":true,"data":{}}');
  const secondRaw = Buffer.from('{"accepted":true,"data":{"status":"ok"}}');
  const first = await spool.appendCandidate(1, 0, firstRaw, [artifact]);
  await spool.appendOutcome(first.producer_candidate_key, {
    code: 'submission_invalid', path: '/data', keyword: 'required', count: 1,
    artifact_status: 'not_evaluated', binding_status: 'not_evaluated',
  });
  const second = await spool.appendCandidate(2, 0, secondRaw, [artifact]);
  await spool.appendOutcome(second.producer_candidate_key, {
    code: 'locally_validated', count: 1,
    artifact_status: 'validated', binding_status: 'not_required',
  });
  assert.equal(second.parent_producer_candidate_key, first.producer_candidate_key);
  const frames = decodeFrames(await readFile(path));
  assert.deepEqual(frames.map((entry) => entry.manifest.record_kind), ['candidate', 'outcome', 'candidate', 'outcome']);
  assert.deepEqual(frames[0].raw, firstRaw);
  assert.deepEqual(frames[2].raw, secondRaw);
  assert.deepEqual(frames[0].manifest.artifacts, [artifact]);
  assert.equal(frames[1].manifest.outcome_sequence, 1);
  assert.equal(frames[1].manifest.artifact_status, 'not_evaluated');
  assert.equal(frames[3].manifest.binding_status, 'not_required');
});

test('candidate spool accepts only authoritative outcomes and leaves bytes unchanged on rejection', async (t) => {
  const { request, path } = await requestWithSpool(t);
  const spool = new CandidateSpool(request);
  const candidate = await spool.appendCandidate(1, 0, Buffer.from('{}'));
  const before = await readFile(path);
  const cases = [
    { code: 'arbitrary_canary', artifact_status: 'not_evaluated', binding_status: 'not_evaluated' },
    { code: 'submission_invalid', keyword: 'required', count: 1, artifact_status: 'not_evaluated', binding_status: 'not_evaluated' },
    { code: 'submission_invalid', path: '/data', keyword: 'unknownKeyword', count: 1, artifact_status: 'not_evaluated', binding_status: 'not_evaluated' },
    { code: 'submission_invalid', path: '/not/a/schema/path', keyword: 'required', count: 1, artifact_status: 'not_evaluated', binding_status: 'not_evaluated' },
    { code: 'submission_invalid', path: '/data', keyword: 'strict_json', count: 1, artifact_status: 'not_evaluated', binding_status: 'not_evaluated' },
    { code: 'submission_invalid', path: '/data', keyword: 'required', count: 2, artifact_status: 'not_evaluated', binding_status: 'not_evaluated' },
    { code: 'locally_validated', artifact_status: 'validated', binding_status: 'not_required' },
    { code: 'provider_error', path: '/data', keyword: 'required', count: 1, artifact_status: 'not_evaluated', binding_status: 'not_evaluated' },
  ];
  for (const outcome of cases) {
    await assert.rejects(spool.appendOutcome(candidate.producer_candidate_key, outcome), { code: 'candidate_outcome_invalid' });
    assert.deepEqual(await readFile(path), before);
  }
});

test('candidate spool bounds outcomes per candidate', async (t) => {
  const { request, path } = await requestWithSpool(t);
  const spool = new CandidateSpool(request);
  const candidate = await spool.appendCandidate(1, 0, Buffer.from('{}'));
  for (let sequence = 1; sequence <= 64; sequence += 1) {
    await spool.appendOutcome(candidate.producer_candidate_key, {
      code: 'provider_error', artifact_status: 'not_evaluated', binding_status: 'not_evaluated',
    });
  }
  const before = await readFile(path);

  await assert.rejects(spool.appendOutcome(candidate.producer_candidate_key, {
    code: 'provider_error', artifact_status: 'not_evaluated', binding_status: 'not_evaluated',
  }), { code: 'candidate_outcome_invalid' });
  assert.deepEqual(await readFile(path), before);
});

test('candidate spool enforces registry artifact identity at append and readback', async (t) => {
  const accepted = await requestWithSpool(t);
  const acceptedSpool = new CandidateSpool(accepted.request);
  const kinds = ['log', 'file', 'report', 'screenshot', 'patch', 'raw_output'];
  const artifacts = kinds.map((kind, index) => ({
    handle: `local-proof-${index}`,
    kind,
    sha256: index.toString(16).padStart(64, '0'),
    size_bytes: index,
  }));
  await acceptedSpool.appendCandidate(1, 0, Buffer.from('{}'), artifacts.toReversed());
  assert.deepEqual(decodeFrames(await readFile(accepted.path))[0].manifest.artifacts, artifacts);

  for (const artifact of [
    null,
    { handle: 'evidence-1', kind: 'log', sha256: 'a'.repeat(64), size_bytes: 1 },
    { handle: 'local-proof', kind: 'http_exchange', sha256: 'a'.repeat(64), size_bytes: 1 },
    { handle: `local-${'a'.repeat(123)}`, kind: 'log', sha256: 'a'.repeat(64), size_bytes: 1 },
  ]) {
    const current = await requestWithSpool(t);
    await assert.rejects(new CandidateSpool(current.request).appendCandidate(1, 0, Buffer.from('{}'), [artifact]), { code: 'candidate_manifest_invalid' });
    await assert.rejects(readFile(current.path), { code: 'ENOENT' });
  }

  const frames = decodeFrames(await readFile(accepted.path));
  frames[0].manifest.artifacts[0].kind = 'http_exchange';
  await writeFile(accepted.path, encodeFrame(frames[0].manifest, frames[0].raw), { mode: 0o600 });
  await assert.rejects(acceptedSpool.appendCandidate(2, 0, Buffer.from('{}')), { code: 'candidate_spool_invalid' });
});

test('candidate spool rejects checksummed outcomes outside the finite protocol', async (t) => {
  const { request, path } = await requestWithSpool(t);
  const spool = new CandidateSpool(request);
  const candidate = await spool.appendCandidate(1, 0, Buffer.from('{}'));
  await spool.appendOutcome(candidate.producer_candidate_key, {
    code: 'locally_validated', count: 1,
    artifact_status: 'validated', binding_status: 'not_required',
  });
  const frames = decodeFrames(await readFile(path));
  frames[1].manifest.code = 'arbitrary_canary';
  await writeFile(path, Buffer.concat(frames.map((entry) => encodeFrame(entry.manifest, entry.raw))), { mode: 0o600 });

  await assert.rejects(spool.appendCandidate(2, 0, Buffer.from('{}')), { code: 'candidate_spool_invalid' });
});

test('runner durably captures every malformed submit_result before SDK argument rejection', async (t) => {
  const malformed = '{\r\n"accepted":true,"data":{"status":"雪"}';
  const { request, calls, candidateSpoolPath } = await fixture(t, 'openai-chat-completions', [
    { tools: [{ name: 'submit_result', rawArgs: malformed }] },
  ]);
  const result = await runRequest(parseRequest(request));
  assert.equal(result.status, 'failed', JSON.stringify(result));
  assert.equal(result.error.code, 'submission_invalid');
  assert.equal(result.error.issue.keyword, 'strict_json');
  assert.equal(calls.length, 3);
  const frames = decodeFrames(await readFile(candidateSpoolPath));
  const candidates = frames.filter((entry) => entry.manifest.record_kind === 'candidate');
  const outcomes = frames.filter((entry) => entry.manifest.record_kind === 'outcome');
  assert.equal(candidates.length, 3);
  assert.equal(outcomes.length, 3);
  for (const candidate of candidates) assert.deepEqual(candidate.raw, Buffer.from(malformed));
  assert.equal(candidates[0].manifest.parent_producer_candidate_key, null);
  assert.equal(candidates[1].manifest.parent_producer_candidate_key, candidates[0].manifest.producer_candidate_key);
  assert.equal(candidates[2].manifest.parent_producer_candidate_key, candidates[1].manifest.producer_candidate_key);
  assert.ok(outcomes.every((entry) => entry.manifest.code === 'submission_invalid' && entry.manifest.keyword === 'strict_json'));
});

test('SDK awaits candidate fsync before submit_result tool execution starts', async (t) => {
  const raw = '{"accepted":true,"data":{"status":"ok"}}';
  const { request, candidateSpoolPath } = await fixture(t, 'openai-chat-completions');
  let observedRaw;
  const result = await runRequest(parseRequest(request), { audit: (event) => {
    if (event.event === 'tool_execution_start' && event.tool_name === 'submit_result') {
      observedRaw = decodeFrames(readFileSync(candidateSpoolPath))[0].raw;
    }
  } });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.deepEqual(observedRaw, Buffer.from(raw));
});

test('schema-invalid attempts retain exact bytes and safe schema outcomes across repairs', async (t) => {
  const raw = '{"accepted":true,"data":{}}';
  const { request, candidateSpoolPath } = await fixture(t, 'openai-chat-completions', [
    { tools: [{ name: 'submit_result', args: { accepted: true, data: {} } }] },
  ]);
  const result = await runRequest(parseRequest(request));
  assert.equal(result.error.code, 'submission_invalid');
  assert.equal(result.error.issue.keyword, 'additionalProperties');
  assert.equal(result.error.issue.path, '$');
  assert.ok(result.error.issue.rules.length <= 8);
  assert.ok(result.error.issue.rules.some((rule) => rule.keyword === 'anyOf' && rule.path === '$'));
  const frames = decodeFrames(await readFile(candidateSpoolPath));
  const candidates = frames.filter((entry) => entry.manifest.record_kind === 'candidate');
  const outcomes = frames.filter((entry) => entry.manifest.record_kind === 'outcome');
  assert.equal(candidates.length, 3);
  assert.ok(candidates.every((entry) => entry.raw.equals(Buffer.from(raw))));
  assert.ok(outcomes.every((entry) => entry.manifest.keyword === 'additionalProperties' && entry.manifest.path === '$'));
});

test('artifact injection failure preserves pre-injection candidate and registered artifact identity', async (t) => {
  let workspace;
  const { request, calls, candidateSpoolPath } = await fixture(t, 'openai-chat-completions', [
    { tools: [{ name: 'register_artifact', args: { path: 'evidence.log', kind: 'log' } }] },
    async (call) => {
      const registered = JSON.parse(call.body.messages.find((message) => message.role === 'tool').content);
      await chmod(registered.path, 0o600);
      await writeFile(registered.path, 'after');
      return { tools: [{ name: 'submit_result', rawArgs: '{"accepted":true,"data":{"status":"ok"}}' }] };
    },
  ]);
  workspace = request.cwd;
  await writeFile(join(workspace, 'evidence.log'), 'before');
  const result = await runRequest(parseRequest(request));
  assert.equal(result.status, 'failed');
  assert.equal(result.result, null);
  assert.equal(result.submission_id, null);
  assert.equal(result.error.code, 'artifact_changed');
  assert.equal(result.error.repair_rounds, request.max_result_repairs);
  assert.equal(result.error.submission_attempts, 1 + request.max_result_repairs);
  assert.equal(calls.length, 2 + request.max_result_repairs);
  for (const call of calls.slice(2)) assert.deepEqual(call.body.tools.map((tool) => tool.function.name), calls[0].body.tools.map((tool) => tool.function.name));
  const frames = decodeFrames(await readFile(candidateSpoolPath));
  const candidates = frames.filter((entry) => entry.manifest.record_kind === 'candidate');
  const outcomes = frames.filter((entry) => entry.manifest.record_kind === 'outcome');
  assert.equal(candidates.length, 1 + request.max_result_repairs);
  assert.equal(outcomes.length, candidates.length);
  assert.ok(outcomes.every((entry) => entry.manifest.code === 'artifact_changed' && entry.manifest.artifact_status === 'rejected'));
  assert.ok(candidates.every((entry) => entry.raw.equals(Buffer.from('{"accepted":true,"data":{"status":"ok"}}'))));
  const candidate = candidates[0];
  const outcome = outcomes[0];
  assert.equal(candidate.manifest.artifacts.length, 1);
  assert.deepEqual(Object.keys(candidate.manifest.artifacts[0]).sort(), ['handle', 'kind', 'sha256', 'size_bytes']);
  assert.equal(candidate.manifest.artifacts[0].kind, 'log');
  assert.equal(candidate.manifest.artifacts[0].size_bytes, 6);
  assert.equal(outcome.manifest.code, 'artifact_changed');
  assert.equal(outcome.manifest.artifact_status, 'rejected');
  assert.equal(outcome.manifest.binding_status, 'not_evaluated');
});

test('runner admission failure stops before tool execution and never starts a repair model round', async (t) => {
  const raw = '{"accepted":false,"reason":"fixture-secret-key"}';
  const { request, calls, candidateSpoolPath } = await fixture(t, 'openai-chat-completions', [
    { tools: [{ name: 'submit_result', rawArgs: raw }] },
  ]);
  const audit = [];
  const result = await runRequest(parseRequest(request), { audit: (event) => audit.push(event) });
  assert.equal(result.status, 'failed');
  assert.equal(result.error.code, 'candidate_admission_rejected');
  assert.equal(calls.length, 1);
  assert.ok(!audit.some((entry) => entry.event === 'tool_execution_start' && entry.tool_name === 'submit_result'));
  assert.ok(!audit.some((entry) => entry.event === 'result_repair'));
  await assert.rejects(readFile(candidateSpoolPath), { code: 'ENOENT' });
});

test('admission rejects direct and standard JSON-escaped runtime literals before file creation', async (t) => {
  const cases = [
    { configure: (request) => { request.model.api_key = 'DIRECT_SECRET_81f0'; }, raw: '{"reason":"DIRECT_SECRET_81f0"}' },
    { configure: (request) => { request.model.api_key = 'line one\nline two'; }, raw: '{"reason":"line one\\nline two"}' },
    { configure: (request) => { request.model.custom_headers.push({ name: 'Authorization', value: 'HEADER_SECRET_a611' }); }, raw: '{"reason":"HEADER_SECRET_a611"}' },
    { configure: (request) => { request.capabilities.candidate_capture.forbidden_literals = ['FORBIDDEN_277f']; }, raw: '{"reason":"FORBIDDEN_277f"}' },
    { configure: (request) => { request.capabilities.candidate_capture.forbidden_literals = ['密钥\n']; }, raw: '{"reason":"\\u5bc6\\u94a5\\n"}' },
    { configure: () => {}, raw: '{"reason":"-----BEGIN PRIVATE KEY-----\\ncanary"}' },
    { configure: () => {}, raw: '{"reason":"-----BEGIN ENCRYPTED PRIVATE KEY-----\\ncanary"}' },
    { configure: () => {}, raw: '{"reason":"-----BEGIN DSA PRIVATE KEY-----\\ncanary"}' },
    { configure: () => {}, raw: '{"reason":"-----BEGIN PGP PRIVATE KEY BLOCK-----\\ncanary"}' },
  ];
  for (const [index, item] of cases.entries()) {
    const current = await requestWithSpool(t);
    item.configure(current.request);
    const spool = new CandidateSpool(current.request);
    await assert.rejects(spool.appendCandidate(index + 1, 0, Buffer.from(item.raw)), { code: 'candidate_admission_rejected' });
    await assert.rejects(readFile(current.path), { code: 'ENOENT' });
  }
});
