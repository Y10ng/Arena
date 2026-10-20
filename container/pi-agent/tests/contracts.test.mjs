import test from 'node:test';
import assert from 'node:assert/strict';
import { access, readFile, writeFile, unlink, mkdir, readdir } from 'node:fs/promises';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { Compile } from 'typebox/compile';
import { PHASE_RESULT_KINDS } from '../dist/protocol.js';
import { canonicalJson, runRequest } from '../dist/runner.js';
import { fixture } from './fixtures.mjs';

const contracts = JSON.parse(await readFile(new URL('../contracts/submission-v1.json', import.meta.url), 'utf8'));
const bootstrap = { accepted: true, data: { fact: { description: '观察' }, complete: { description: '完成' } } };

function goalSchemas() {
  const source = fileURLToPath(new URL('../../../cairn/src/', import.meta.url));
  const code = `import json
from cairn.dispatcher.worker_protocol.submission import build_submit_result_schema
from cairn.evidence_contracts import EvidenceItem
print(json.dumps({
  'evidence_item': EvidenceItem.model_json_schema(),
  'external': build_submit_result_schema('external_verify_execute'),
  'explore': build_submit_result_schema('explore_execute', {'assessment_protocol': 'goal-driven-v1'}),
  'explore_conclude': build_submit_result_schema('explore_conclude', {'assessment_protocol': 'goal-driven-v1'}),
  'reason': build_submit_result_schema('reason_execute', {'allowed_fact_ids': ['f1'], 'allowed_intent_ids': ['i-closed'], 'max_intents': 3, 'decision_input_digest': 'a' * 64}, inputs={'open_intent_ids': []}),
}))`;
  return JSON.parse(execFileSync(process.env.PYTHON_BIN || 'python3', ['-c', code], {
    encoding: 'utf8', env: { ...process.env, PYTHONPATH: source },
  }));
}

const findingResult = {
  accepted: true,
  data: {
    fact: {
      description: 'Synthetic finding',
      vulnerability: {
        schema_version: 2,
        title: 'Synthetic access control finding', severity: 'high',
        evidence_items: [{ id: 'request-1', kind: 'http', source: 'capture', request: { artifact_id: 'a1' } }],
        identity: {
          vulnerability_class: 'broken_access_control',
          asset: { host: 'synthetic.example', port: 443 },
          entrypoint: { route: '/synthetic' },
        },
      },
    },
    assessment_record: {
      hypothesis: 'Synthetic access check', outcome: 'finding',
      observations: ['Synthetic owner data observed'], evidence_refs: [{ artifact_id: 'a1' }],
    },
  },
};

test('report schema v2 requires a single ordered markdown or fact layout', () => {
  const validator = Compile(contracts.submit_result_schemas.report_execute);
  const result = { accepted: true, data: {
    schema_version: 2, executive_summary: '仅解释冻结材料', findings: [],
    layout: [{ type: 'markdown', markdown: '# 合成报告' }, { type: 'fact', ref: 'scope' }],
  } };
  assert.equal(validator.Check(result), true);
  for (const change of [
    (value) => { delete value.data.schema_version; },
    (value) => { value.data.schema_version = 1; },
    (value) => { delete value.data.layout; },
    (value) => { value.data.layout = []; },
    (value) => { value.data.report_markdown = '# 另一份整篇报告'; },
    (value) => { value.data.layout[0] = { type: 'markdown', markdown: ' ' }; },
    (value) => { value.data.layout[0] = { type: 'markdown', markdown: '# 报告', ref: 'scope' }; },
    (value) => { value.data.layout[1] = { type: 'fact', ref: 'scope', markdown: '伪装事实' }; },
    (value) => { value.data.layout[1] = { type: 'fact' }; },
    (value) => { value.data.layout[1] = { type: 'template', ref: 'scope' }; },
  ]) {
    const invalid = structuredClone(result);
    change(invalid);
    assert.equal(validator.Check(invalid), false, change.toString());
  }
});

test('evidence v2 schemas require ordered items and reject inline or double-filled references', () => {
  for (const schema of Object.values(contracts.submit_result_schemas)) Compile(schema);
  const findingSchema = contracts.submit_result_schemas.explore_execute;
  const findingValidator = Compile(findingSchema);
  const finding = structuredClone(findingResult);
  delete finding.data.assessment_record;
  assert.equal(findingValidator.Check(finding), true);
  for (const change of [
    (value) => { delete value.data.fact.vulnerability.schema_version; },
    (value) => { value.data.fact.vulnerability.schema_version = 1; },
    (value) => { value.data.fact.evidence_refs = []; },
    (value) => { value.data.evidence_refs = []; },
    (value) => { value.data.fact.vulnerability.repro_request = 'GET /invented'; },
    (value) => { value.data.fact.vulnerability.evidence_items[0].request_text = 'GET /invented'; },
    (value) => { value.data.fact.vulnerability.evidence_items[0].request.artifact_handle = 'local-double'; },
  ]) {
    const invalid = structuredClone(finding);
    change(invalid);
    assert.equal(findingValidator.Check(invalid), false, change.toString());
  }
  const validator = Compile(contracts.submit_result_schemas.external_verify_execute);
  const external = { accepted: true, data: {
    schema_version: 3, outcome: 'confirmed', summary: '合成观察', tested_scope: '本地合成材料',
    key_evidence: [{ reason: '原件记录观察结果', evidence_item_ids: ['request-1'] }],
    test_steps: [{ action: '登记本地合成原件', observation: '登记成功', evidence_item_ids: ['request-1'] }], limitations: [],
    evidence_items: structuredClone(finding.data.fact.vulnerability.evidence_items),
  } };
  assert.equal(validator.Check(external), true);
  for (const change of [
    (value) => { delete value.data.schema_version; },
    (value) => { value.data.schema_version = 2; },
    (value) => { value.data.notes = 'retired'; },
    (value) => { delete value.data.key_evidence; },
    (value) => { value.data.test_steps[0].observation = '  '; },
    (value) => { value.data.evidence_refs = [{ artifact_id: 'a1' }]; },
    (value) => { value.data.evidence_items = []; },
    (value) => { value.data.evidence_items[0].request = null; },
  ]) {
    const invalid = structuredClone(external);
    change(invalid);
    assert.equal(validator.Check(invalid), false, change.toString());
  }
  external.data.outcome = 'insufficient_evidence';
  external.data.evidence_items = [];
  assert.equal(validator.Check(external), false);
  external.data.limitations = ['No response was received'];
  assert.equal(validator.Check(external), true);
});

test('evidence kinds expose only readable slots in exported and dynamic schemas', () => {
  const schemas = goalSchemas();
  const stored = Compile(schemas.evidence_item);
  const findings = [schemas.explore, schemas.explore_conclude,
    contracts.submit_result_schemas.explore_execute, contracts.submit_result_schemas.explore_conclude].map((schema) => Compile(schema));
  assert.deepEqual(schemas.external, contracts.submit_result_schemas.external_verify_execute);
  const external = Compile(schemas.external);
  for (const [kind, source, content, valid] of [
    ['http', 'capture', { request: { artifact_id: 'a1' }, response: { artifact_id: 'a2' } }, true],
    ['http', 'capture', { request: { artifact_id: 'a1' } }, true],
    ['http', 'capture', { response: { artifact_id: 'a2' } }, true],
    ['artifact', 'capture', { artifact: { artifact_id: 'a1' } }, true],
    ['text', 'capture', { artifact: { artifact_id: 'a1' } }, true],
    ['http', 'historical', { request_text: 'GET / HTTP/1.1\r\n\r\n  ' }, true],
    ['text', 'historical', { text: '历史原文\n  ' }, true],
    ['http', 'capture', { artifact: { artifact_id: 'a1' } }, false],
    ['http', 'capture', { request: { artifact_id: 'a1' }, artifact: { artifact_id: 'a2' } }, false],
    ['http', 'historical', { text: '不可隐藏的正文' }, false],
    ['artifact', 'capture', { request: { artifact_id: 'a1' } }, false],
    ['text', 'capture', { response: { artifact_id: 'a1' } }, false],
    ['artifact', 'historical', { artifact: { artifact_id: 'a1' }, response_text: 'HTTP/1.1 200 OK' }, false],
    ['artifact', 'historical', { text: '附件应使用原件引用' }, false],
    ['http', 'capture', { request_text: 'GET / HTTP/1.1' }, false],
    ['http', 'capture', {}, false],
  ]) {
    const item = { id: 'synthetic', kind, source, ...content };
    const label = JSON.stringify(item);
    assert.equal(stored.Check(item), valid, label);
    const workerValid = valid && !['request_text', 'response_text', 'text'].some((slot) => slot in content);
    const result = structuredClone(findingResult);
    result.data.fact.vulnerability.evidence_items = [item];
    for (const validator of findings) assert.equal(validator.Check(result), workerValid, label);
    assert.equal(external.Check({ accepted: true, data: {
      schema_version: 3, outcome: 'confirmed', summary: '合成结果', tested_scope: '仅隔离合同',
      key_evidence: [], test_steps: [], limitations: [], evidence_items: [item],
    } }), workerValid, label);
  }
});

test('goal-driven Explore production Schema requires finding identity and matching outcome', () => {
  const schemas = goalSchemas();
  for (const schema of [schemas.explore, schemas.explore_conclude]) {
    const validator = Compile(schema);
    assert.equal(validator.Check(findingResult), true);
    const invalidChanges = [
      (data) => { delete data.fact.vulnerability; },
      (data) => { data.fact.vulnerability = null; },
      (data) => { delete data.fact.vulnerability.identity; },
      (data) => { data.fact.vulnerability.identity = null; },
      (data) => { data.fact.vulnerability.packet_artifact_handle = 'retired-packet'; },
      (data) => { data.fact.vulnerability.identity.vulnerability_class = ' \t'; },
      (data) => { data.fact.vulnerability.identity.asset.host = ' \t'; },
      (data) => { data.fact.vulnerability.identity.asset.port = 0; },
      (data) => { data.fact.vulnerability.identity.asset.port = 65536; },
      (data) => { data.fact.vulnerability.identity.asset.port = '443'; },
      (data) => { data.fact.vulnerability.identity.entrypoint = {}; },
      (data) => { data.fact.vulnerability.identity.entrypoint = { route: ' ', locus: '\t' }; },
      (data) => { data.fact.vulnerability.identity.unexpected = true; },
      (data) => { data.fact.vulnerability.identity.asset.unexpected = true; },
      (data) => { data.fact.vulnerability.identity.entrypoint.unexpected = true; },
      (data) => { data.fact.vulnerability.identity.vector = { unexpected: true }; },
    ];
    for (const change of invalidChanges) {
      const invalid = structuredClone(findingResult);
      change(invalid.data);
      assert.equal(validator.Check(invalid), false, change.toString());
    }
    const locus = structuredClone(findingResult);
    locus.data.fact.vulnerability.identity.entrypoint = { route: ' ', locus: 'Synthetic handler' };
    assert.equal(validator.Check(locus), true);
    for (const outcome of ['observation', 'tested_no_finding']) {
      const nonFinding = structuredClone(findingResult);
      nonFinding.data.assessment_record.outcome = outcome;
      assert.equal(validator.Check(nonFinding), false, outcome);
      nonFinding.data.fact.vulnerability = null;
      assert.equal(validator.Check(nonFinding), true, `${outcome}: null vulnerability`);
      delete nonFinding.data.fact.vulnerability;
      assert.equal(validator.Check(nonFinding), true, outcome);
    }
  }
});

test('goal-driven production Schema accepts missing comparisons and repairs missing observations', async (t) => {
  const schema = goalSchemas().explore;
  const validator = Compile(schema);
  const first = structuredClone(findingResult);
  first.data.assessment_record.variants = ['Synthetic variant'];
  const third = structuredClone(findingResult);
  const missing = structuredClone(findingResult);
  missing.data.assessment_record.baseline = 'Synthetic baseline';
  missing.data.assessment_record.variants = ['Synthetic variant'];
  delete missing.data.assessment_record.observations;
  assert.equal(validator.Check(first), true);
  assert.equal(validator.Check(third), true);
  assert.equal(validator.Check(missing), false);
  const { request, calls } = await fixture(t, 'openai-chat-completions', [
    { tools: [{ name: 'submit_result', args: missing }] },
    { tools: [{ name: 'submit_result', args: first }] },
  ]);
  Object.assign(request, { phase: 'explore_execute', result_schema: schema, binding: { assessment_protocol: 'goal-driven-v1' } });
  const audit = [];
  const result = await runRequest(request, { audit: (event) => audit.push(event) });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.equal(calls.length, 2);
  assert.deepEqual(result.result.data, first.data);
  const issue = audit.find((event) => event.event === 'result_repair').issue;
  assert.equal(issue.path, '/data/assessment_record');
  assert.ok(issue.rules.some((rule) => rule.required?.includes('observations')));
  assert.deepEqual(calls[1].body.tools.map((tool) => tool.function.name), calls[0].body.tools.map((tool) => tool.function.name));
});

test('goal-driven Reason production Schema preserves Intent structure and repairs invalid dependencies', async (t) => {
  const schema = goalSchemas().reason;
  const validator = Compile(schema);
  const intents = [
    { from: ['f1'], description: 'First' },
    { from: ['f1'], description: 'Second', depends_on: '@1' },
    { from: ['f1'], description: 'Third', depends_on: '@2' },
  ];
  const valid = { accepted: true, data: { intents } };
  const invalid = structuredClone(valid);
  invalid.data.intents[1].depends_on = 'semantic-name';
  assert.equal(validator.Check(valid), true);
  assert.equal(validator.Check(invalid), false);
  for (let index = 0; index < intents.length; index += 1) {
    for (const [label, mutate] of [
      ['extra field', (intent) => { intent.controls = []; }],
      ['missing from', (intent) => { delete intent.from; }],
      ['missing description', (intent) => { delete intent.description; }],
      ['nested from', (intent) => { intent.from = [{ id: 'f1' }]; }],
      ['unknown fact', (intent) => { intent.from = ['unknown-fact']; }],
      ['description type', (intent) => { intent.description = {}; }],
    ]) {
      const malformed = structuredClone(valid);
      mutate(malformed.data.intents[index]);
      assert.equal(validator.Check(malformed), false, `${index + 1}: ${label}`);
    }
    const nonObject = structuredClone(valid);
    nonObject.data.intents[index] = 'not-an-intent';
    assert.equal(validator.Check(nonObject), false, `${index + 1}: Intent type`);
    for (const dependency of [null, 'i-closed', ...Array.from({ length: index }, (_, i) => `@${i + 1}`)]) {
      const allowed = structuredClone(valid);
      allowed.data.intents[index].depends_on = dependency;
      assert.equal(validator.Check(allowed), true, `${index + 1}: ${dependency}`);
    }
    const independent = structuredClone(valid);
    delete independent.data.intents[index].depends_on;
    assert.equal(validator.Check(independent), true);
    for (const dependency of [`@${index + 1}`, `@${index + 2}`, '@99', 'semantic-name']) {
      const disallowed = structuredClone(valid);
      disallowed.data.intents[index].depends_on = dependency;
      assert.equal(validator.Check(disallowed), false, `${index + 1}: ${dependency}`);
    }
  }
  const { request, calls } = await fixture(t, 'openai-chat-completions', [
    { tools: [{ name: 'submit_result', args: invalid }] },
    { tools: [{ name: 'submit_result', args: valid }] },
  ]);
  Object.assign(request, {
    phase: 'reason_execute', result_schema: schema,
    binding: { allowed_fact_ids: ['f1'], allowed_intent_ids: ['i-closed'], max_intents: 3, decision_input_digest: 'a'.repeat(64) },
  });
  const audit = [];
  const result = await runRequest(request, { audit: (event) => audit.push(event) });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.deepEqual(result.result.data, valid.data);
  assert.equal(calls.length, 2);
  assert.equal(audit.find((event) => event.event === 'result_repair').issue.path, '/data/intents/*/depends_on');
});

test('production submit schema carries success explanation through provider, candidate and result', async (t) => {
  const schema = contracts.submit_result_schemas.explore_execute;
  const validator = Compile(schema);
  const args = { accepted: true, data: { fact: { description: 'Observed an authorized response' } }, reason: 'The evidence supports another follow-up' };
  assert.equal(validator.Check(args), true);
  assert.equal(validator.Check({ ...args, reason: '' }), false);
  assert.equal(validator.Check({ ...args, data: {} }), false);
  assert.equal(validator.Check({ accepted: false, reason: 'blocked', data: args.data }), false);
  assert.equal(validator.Check({ accepted: false, reason: 'blocked' }), true);
  const { request, calls, candidateSpoolPath } = await fixture(t, 'anthropic-messages', [
    { tools: [{ name: 'submit_result', args }] },
  ]);
  request.result_schema = schema;
  const result = await runRequest(request);
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  const expected = { protocol: 'cairn-assessment-v2', accepted: true, kind: 'explore_result', data: args.data, reason: args.reason };
  assert.deepEqual(result.result, expected);
  assert.equal(result.submission_id, createHash('sha256').update(canonicalJson(expected)).digest('hex'));
  assert.deepEqual(calls[0].body.tools.find((tool) => tool.name === 'submit_result').input_schema, schema);
  const directory = join(request.session_root, request.session.slice(3));
  const [file] = (await readdir(directory)).filter((name) => name.startsWith('candidate-'));
  assert.deepEqual(JSON.parse(await readFile(join(directory, file), 'utf8')).result, expected);
  assert.ok((await readFile(candidateSpoolPath)).includes(Buffer.from(JSON.stringify(args))));
});

test('production Bootstrap submission hides complete.from while Reason retains it', async (t) => {
  const schema = contracts.submit_result_schemas.bootstrap_execute;
  const args = structuredClone(bootstrap);
  args.data.complete.from = ['f1'];
  const { request } = await fixture(t, 'openai-chat-completions', [{ tools: [{ name: 'submit_result', args }] }]);
  Object.assign(request, { phase: 'bootstrap_execute', result_schema: schema, max_result_repairs: 0 });
  const result = await runRequest(request);
  assert.equal(result.status, 'failed');
  assert.equal(result.error.code, 'submission_invalid');
  assert.equal(result.error.issue.keyword, 'additionalProperties');
  assert.equal(result.error.issue.path, '/data/complete');
  assert.ok(contracts.submit_result_schemas.reason_execute.$defs.CompleteResult.properties.from);
});

test('production Schema SDK pre-execute rejection has bounded safe diagnostics and normal repair tools', async (t) => {
  const canary = 'SECRET_DYNAMIC_VALUE_7fa9';
  const args = structuredClone(bootstrap);
  args.data.fact.description = { [canary]: canary };
  const { request, calls } = await fixture(t, 'openai-chat-completions', [{ tools: [{ name: 'submit_result', args }] }]);
  Object.assign(request, { phase: 'bootstrap_execute', result_schema: contracts.submit_result_schemas.bootstrap_execute });
  const audit = [];
  const result = await runRequest(request, { audit: (event) => audit.push(event) });
  assert.equal(result.error.code, 'submission_invalid');
  assert.deepEqual(result.error.issue, {
    keyword: 'type', path: '/data/fact/description',
    rules: [{ keyword: 'type', path: '/data/fact/description', types: ['string'] }],
  });
  assert.equal(result.error.submission_attempts, 3);
  assert.equal(result.error.repair_rounds, 2);
  assert.equal(result.error.max_repair_rounds, 2);
  assert.equal(calls.length, 3);
  assert.equal(result.result, null);
  assert.ok(!JSON.stringify({ result, audit }).includes(canary));
  for (const call of calls.slice(1)) {
    assert.deepEqual(call.body.tools.map((tool) => tool.function.name), calls[0].body.tools.map((tool) => tool.function.name));
    assert.ok(!call.body.messages.filter((message) => message.role === 'user').at(-1).content.includes(canary));
  }
});
for (const [label, mutate, keyword, path] of [
  ['extra property', (args, secret) => { args.data.fact[secret] = secret; }, 'additionalProperties', '/data/fact'],
  ['dynamic mapping key', (args, secret) => { args.data.discovered_surfaces = [{ surface_kind: 'endpoint', surface_key: 'fixture', traits: { [secret]: [] } }]; }, 'additionalProperties', '/data/discovered_surfaces/*/traits'],
]) test(`production diagnostics never disclose ${label}`, async (t) => {
  const canary = 'PRIVATE_CANARY_4dea';
  const args = structuredClone(bootstrap);
  mutate(args, canary);
  const { request } = await fixture(t, 'openai-chat-completions', [{ tools: [{ name: 'submit_result', args }] }]);
  Object.assign(request, { phase: 'bootstrap_execute', result_schema: contracts.submit_result_schemas.bootstrap_execute, max_result_repairs: 0 });
  const audit = [];
  const result = await runRequest(request, { audit: (event) => audit.push(event) });
  assert.equal(result.error.code, 'submission_invalid');
  assert.equal(result.error.issue.keyword, keyword);
  assert.equal(result.error.issue.path, path);
  assert.ok(!JSON.stringify({ result, audit }).includes(canary));
});

test('bootstrap scope conflict is captured then repaired without repeating exploration', async (t) => {
  const conflicting = structuredClone(bootstrap);
  conflicting.data.discovered_surfaces = [{ surface_kind: 'endpoint', surface_key: 'GET /fixture' }];
  conflicting.data.coverage_scope_disposition = {
    disposition_kind: 'no_reachable_surfaces', target_ids: ['target-1'], reason: 'none reachable',
    evidence_refs: [{ artifact_id: 'a1' }],
  };
  const conflictingRaw = JSON.stringify(conflicting);
  const repairedRaw = JSON.stringify(bootstrap);
  const { request, calls, candidateSpoolPath } = await fixture(t, 'openai-chat-completions', [
    { tools: [{ name: 'submit_result', rawArgs: conflictingRaw }] },
    { tools: [{ name: 'submit_result', rawArgs: repairedRaw }] },
  ]);
  Object.assign(request, { phase: 'bootstrap_execute', result_schema: contracts.submit_result_schemas.bootstrap_execute });

  const result = await runRequest(request);

  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.deepEqual(result.result.data, bootstrap.data);
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[1].body.tools.map((tool) => tool.function.name), calls[0].body.tools.map((tool) => tool.function.name));
  const spool = await readFile(candidateSpoolPath);
  assert.equal(spool.includes(Buffer.from(conflictingRaw)), true);
  assert.equal(spool.includes(Buffer.from(repairedRaw)), true);
  assert.equal(spool.includes(Buffer.from('"code":"submission_invalid"')), true);
  assert.equal(spool.includes(Buffer.from('"keyword":"if"')), true);
  assert.equal(spool.includes(Buffer.from('"path":"/data"')), true);
});

for (const fact of [{ statement: 'PRIVATE_FACT_VALUE' }, {}]) {
  for (const accepted of [true, false]) test(`Bootstrap feedback explains ${Object.keys(fact).length ? 'statement' : 'empty fact'} and accepted=${accepted} data failure`, async (t) => {
    const args = { ...structuredClone(bootstrap), accepted, reason: 'PRIVATE_REASON_VALUE' };
    args.data.fact = fact;
    const raw = JSON.stringify(args);
    const { request, calls, candidateSpoolPath } = await fixture(t, 'openai-chat-completions', [
      { tools: [{ name: 'submit_result', rawArgs: raw }] },
      { tools: [{ name: 'submit_result', args: bootstrap }] },
    ]);
    Object.assign(request, { phase: 'bootstrap_execute', result_schema: contracts.submit_result_schemas.bootstrap_execute });
    const audit = [];
    const result = await runRequest(request, { audit: (event) => audit.push(event) });
    assert.equal(result.status, 'succeeded', JSON.stringify(result));
    const repair = audit.find((event) => event.event === 'result_repair');
    const rules = repair.issue.rules;
    assert.ok(rules.some((rule) => rule.path === '/data/fact' && rule.missing?.includes('description')));
    const structure = rules.find((rule) => rule.path === '/data/fact' && rule.allowed);
    assert.deepEqual(structure.allowed, Object.keys(request.result_schema.$defs.FactResult.properties));
    assert.deepEqual(structure.required, ['description']);
    if (!accepted) assert.ok(rules.some((rule) => rule.path === '$' && rule.required?.includes('reason')
      && rule.not?.required.includes('data')));
    const feedback = JSON.stringify(calls[1].body.messages.filter((message) => ['user', 'tool'].includes(message.role)));
    assert.match(feedback, /description/);
    for (const secret of ['statement', 'PRIVATE_FACT_VALUE', 'PRIVATE_REASON_VALUE']) {
      assert.ok(!feedback.includes(secret));
      assert.ok(!JSON.stringify(audit).includes(secret));
    }
    assert.ok((await readFile(candidateSpoolPath)).includes(Buffer.from(raw)));
    assert.equal(calls.length, 2);
  });
}

test('Reason diagnostics preserve union choices after bounded deep error collection', async (t) => {
  const schema = contracts.submit_result_schemas.reason_execute;
  const privateField = 'PRIVATE_EXTRA_FIELD_8c41';
  const privateValue = 'PRIVATE_EXTRA_VALUE_3b72';
  const invalid = {
    accepted: true,
    reason: 'PRIVATE_ENVELOPE_REASON_1122',
    data: {
      evidence_refs: Array.from({ length: 1000 }, (_, index) => ({
        artifact_id: `artifact-${index}`,
        artifact_handle: `handle-${index}`,
        [privateField]: privateValue,
      })),
      intents: [{ from: ['f1'], description: 'follow up', depends_on: [] }],
    },
  };
  assert.ok(Buffer.byteLength(JSON.stringify(invalid)) < 1024 * 1024);
  const repaired = { accepted: false, reason: 'insufficient existing evidence' };
  const { request, calls } = await fixture(t, 'openai-chat-completions', [
    { tools: [{ name: 'submit_result', args: invalid }] },
    { tools: [{ name: 'submit_result', args: repaired }] },
  ]);
  Object.assign(request, { phase: 'reason_execute', result_schema: schema });
  const audit = [];
  const result = await runRequest(request, { audit: (event) => audit.push(event) });
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.equal(calls.length, 2);
  const issue = audit.find((event) => event.event === 'result_repair').issue;
  assert.equal(issue.keyword, 'additionalProperties');
  assert.equal(issue.path, '/data/evidence_refs/*');
  assert.ok(issue.rules.length <= 8);
  const reference = issue.rules.find((rule) => rule.keyword === 'oneOf' && rule.path === '/data/evidence_refs/*');
  assert.ok(reference);
  assert.deepEqual(reference.alternatives.map((alternative) => alternative.required), [['artifact_id'], ['artifact_handle']]);
  assert.deepEqual(reference.alternatives[0].properties.artifact_id, { types: ['string'], minLength: 1 });
  assert.deepEqual(reference.alternatives[0].properties.artifact_handle, { types: ['null'] });
  assert.deepEqual(reference.alternatives[1].properties.artifact_id, { types: ['null'] });
  assert.deepEqual(reference.alternatives[1].properties.artifact_handle, { types: ['string'], minLength: 1 });
  assert.ok(!issue.rules.some((rule) => rule.required?.includes('artifact_id') && rule.required?.includes('artifact_handle')));
  const dependsOn = issue.rules.find((rule) => rule.keyword === 'anyOf' && rule.path === '/data/intents/*/depends_on');
  assert.deepEqual(dependsOn.alternatives, [{ types: ['string'], minLength: 1 }, { types: ['null'] }]);
  const diagnostic = JSON.stringify(issue);
  assert.ok(diagnostic.length < 8192);
  for (const secret of [privateField, privateValue, 'PRIVATE_ENVELOPE_REASON_1122']) assert.ok(!diagnostic.includes(secret));
  const repairPrompt = calls[1].body.messages.filter((message) => message.role === 'user').at(-1).content;
  for (const secret of [privateField, privateValue, 'PRIVATE_ENVELOPE_REASON_1122']) assert.ok(!repairPrompt.includes(secret));
});

test('AssessmentEvidenceRef accepts exactly one nonempty identifier', () => {
  const schema = contracts.submit_result_schemas.reason_execute;
  const validator = Compile(schema);
  for (const reference of [
    { artifact_id: 'artifact-1' },
    { artifact_id: 'artifact-1', artifact_handle: null },
    { artifact_handle: 'handle-1' },
    { artifact_handle: 'handle-1', artifact_id: null },
  ]) assert.equal(validator.Check({ accepted: true, data: { evidence_refs: [reference] } }), true, JSON.stringify(reference));
  assert.equal(validator.Check({ accepted: true, data: { evidence_refs: [{ artifact_id: 'artifact-1', artifact_handle: 'handle-1' }] } }), false);
  assert.equal(validator.Check({ accepted: true, data: { evidence_refs: [{}] } }), false);
});

for (const tool of ['read_evidence', 'register_artifact']) {
  for (const sameTurn of [true, false]) test(`auxiliary ${tool} failure preserves second repair ${sameTurn ? 'same-turn' : 'SDK continuation'} submission`, async (t) => {
    const bad = { tools: [{ name: 'submit_result', args: { accepted: true, data: { fact: {} } } }] };
    const good = { name: 'submit_result', args: bootstrap };
    const failedTool = { name: tool, args: { path: '.', ...(tool === 'register_artifact' ? { kind: 'file' } : {}) } };
    const { request, calls, cwd, candidateSpoolPath } = await fixture(t, 'openai-chat-completions', [
      bad, bad,
      { tools: [failedTool, ...(sameTurn ? [good] : [])] },
      { tools: [{ name: 'read_evidence', args: { path: 'existing.txt' } }, { name: 'register_artifact', args: { path: 'existing.txt', kind: 'file' } }] },
      { tools: [good] },
    ]);
    await writeFile(join(cwd, 'existing.txt'), 'synthetic evidence');
    Object.assign(request, { phase: 'bootstrap_execute', result_schema: contracts.submit_result_schemas.bootstrap_execute });
    const audit = [];
    const result = await runRequest(request, { audit: (event) => audit.push(event) });
    assert.equal(result.status, 'succeeded', JSON.stringify(result));
    assert.equal(calls.length, sameTurn ? 3 : 5);
    const end = audit.find((event) => event.event === 'session_end');
    assert.equal(end.submission_attempts, 3);
    assert.equal(end.repair_rounds, 2);
    assert.equal(end.issue.keyword, 'required');
    assert.ok(audit.some((event) => event.event === 'tool_execution_end' && event.tool_name === tool && event.is_error));
    for (const call of calls.slice(1)) assert.deepEqual(call.body.tools.map((entry) => entry.function.name), calls[0].body.tools.map((entry) => entry.function.name));
    if (!sameTurn) assert.match(JSON.stringify(calls[3].body.messages.filter((message) => message.role === 'tool')), /regular authorized input file outside session storage/);
    assert.ok((await readFile(candidateSpoolPath)).includes(Buffer.from(JSON.stringify(bad.tools[0].args))));
  });
}

for (const submitted of [true, false]) test(`auxiliary failure alone preserves ${submitted ? 'Schema rejection' : 'missing submission'} and attempt count`, async (t) => {
  const initial = submitted ? { tools: [{ name: 'submit_result', args: { accepted: true, data: {} } }] } : { text: 'No submission yet' };
  const { request, calls } = await fixture(t, 'openai-chat-completions', [initial,
    { tools: [{ name: 'read_evidence', args: { path: '.' } }, { name: 'register_artifact', args: { path: '.', kind: 'file' } }] },
    { text: 'Unable to deliver' },
  ]);
  Object.assign(request, { phase: 'bootstrap_execute', result_schema: contracts.submit_result_schemas.bootstrap_execute, max_result_repairs: 1 });
  const audit = [];
  const result = await runRequest(request, { audit: (event) => audit.push(event) });
  assert.equal(result.error.code, submitted ? 'submission_invalid' : 'submission_missing');
  assert.equal(result.error.submission_attempts, submitted ? 1 : 0);
  assert.equal(result.error.repair_rounds, 1);
  assert.equal(result.error.issue?.keyword, submitted ? 'required' : undefined);
  assert.equal(calls.length, 3);
  assert.equal(audit.filter((event) => event.event === 'tool_execution_end' && event.is_error && ['read_evidence', 'register_artifact'].includes(event.tool_name)).length, 2);
});

for (const round of [1, 2]) test(`production Schema repairs successfully in round ${round} with authorized tools retained`, async (t) => {
  const bad = { tools: [{ name: 'submit_result', args: { accepted: true, data: {} } }] };
  const good = { tools: [{ name: 'submit_result', args: bootstrap }] };
  const { request, calls } = await fixture(t, 'openai-chat-completions', [...Array(round).fill(bad), good]);
  Object.assign(request, { phase: 'bootstrap_execute', result_schema: contracts.submit_result_schemas.bootstrap_execute });
  const result = await runRequest(request);
  assert.equal(result.status, 'succeeded', JSON.stringify(result));
  assert.deepEqual(result.result.data, bootstrap.data);
  assert.equal(calls.length, round + 1);
  for (const call of calls.slice(1)) assert.deepEqual(call.body.tools.map((tool) => tool.function.name), calls[0].body.tools.map((tool) => tool.function.name));
});

for (const [label, action, code, keyword, attempts] of [
  ['missing tool', { text: 'No result' }, 'submission_missing', undefined, 0],
  ['truncated JSON', { tools: [{ name: 'submit_result', rawArgs: '{"accepted": true, "data":' }] }, 'submission_invalid', 'strict_json', 3],
  ['model input size', { tools: [{ name: 'submit_result', args: { accepted: false, reason: 'X'.repeat(1024 * 1024 + 10) } }] }, 'submission_invalid', 'input_size', 3],
]) test(`production ${label} preserves terminal classification and counts`, async (t) => {
  const { request, calls } = await fixture(t, 'openai-chat-completions', [action]);
  Object.assign(request, { phase: 'bootstrap_execute', result_schema: contracts.submit_result_schemas.bootstrap_execute });
  const result = await runRequest(request);
  assert.equal(result.error.code, code, JSON.stringify(result));
  assert.equal(result.error.issue?.keyword, keyword);
  assert.equal(result.error.submission_attempts, attempts);
  assert.equal(result.error.repair_rounds, 2);
  assert.equal(calls.length, 3);
  assert.equal(result.submission_id, null);
});

test('configured secret equal to a keyword cannot rewrite generated audit facts', async (t) => {
  const args = structuredClone(bootstrap);
  args.data.fact.description = 1;
  const { request } = await fixture(t, 'openai-chat-completions', [{ tools: [{ name: 'submit_result', args }] }]);
  Object.assign(request, { phase: 'bootstrap_execute', result_schema: contracts.submit_result_schemas.bootstrap_execute, max_result_repairs: 0 });
  request.model.custom_headers = [{ name: 'X-Fixture-Header', value: 'type' }];
  const audit = [];
  const result = await runRequest(request, { audit: (event) => audit.push(event) });
  const failure = audit.find((event) => event.event === 'agent_error').error;
  assert.deepEqual(failure, result.error);
  assert.equal(failure.issue.keyword, 'type');
});

for (const state of ['changed', 'unreadable', 'invalid']) test(`production evidence ${state} is not a model delivery error`, async (t) => {
  let cwd;
  const { request, calls, cwd: workspace } = await fixture(t, 'openai-chat-completions', [
    { tools: [{ name: 'register_artifact', args: { path: 'PRIVATE_PATH_CANARY', kind: 'file' } }] },
    async () => {
      if (state === 'changed') await writeFile(join(cwd, 'PRIVATE_PATH_CANARY'), 'changed');
      else {
        await unlink(join(cwd, 'PRIVATE_PATH_CANARY'));
        if (state === 'invalid') await mkdir(join(cwd, 'PRIVATE_PATH_CANARY'));
      }
      return { tools: [{ name: 'submit_result', args: bootstrap }] };
    },
  ]);
  cwd = workspace;
  await writeFile(join(cwd, 'PRIVATE_PATH_CANARY'), 'before');
  Object.assign(request, { phase: 'bootstrap_execute', result_schema: contracts.submit_result_schemas.bootstrap_execute });
  const audit = [];
  const result = await runRequest(request, { audit: (event) => audit.push(event) });
  assert.equal(result.error.code, `artifact_${state}`);
  assert.equal(result.error.submission_attempts, 1);
  assert.equal(result.error.repair_rounds, 0);
  assert.equal(calls.length, 2);
  assert.ok(!JSON.stringify({ result, audit }).includes('PRIVATE_PATH_CANARY'));
});

test('production unregistered evidence can be fixed locally but does not become submission_invalid', async (t) => {
  const args = structuredClone(bootstrap);
  args.data.evidence_refs = [{ artifact_handle: 'PRIVATE_HANDLE' }];
  const { request, calls } = await fixture(t, 'openai-chat-completions', [{ tools: [{ name: 'submit_result', args }] }]);
  Object.assign(request, { phase: 'bootstrap_execute', result_schema: contracts.submit_result_schemas.bootstrap_execute });
  const result = await runRequest(request);
  assert.equal(result.error.code, 'artifact_unknown');
  assert.equal(result.error.repair_rounds, 2);
  assert.equal(calls.length, 3);
  assert.ok(!JSON.stringify(result).includes('PRIVATE_HANDLE'));
});

test('size overflow after authoritative binding injection is a program request error', async (t) => {
  const args = { accepted: true, data: { fact: { description: '观察' }, coverage_result: {
    slot_key: 'cell', surface_key: 'surface', test_family: 'authorization', outcome: 'tested_no_finding',
  } } };
  const { request, calls } = await fixture(t, 'openai-chat-completions', [{ tools: [{ name: 'submit_result', args }] }]);
  Object.assign(request, { phase: 'explore_execute', result_schema: contracts.submit_result_schemas.explore_execute,
    binding: { slot_key: 'X'.repeat(1024 * 1024) } });
  const result = await runRequest(request);
  assert.equal(result.error.code, 'submission_injected_too_large');
  assert.equal(result.error.repair_rounds, 0);
  assert.equal(calls.length, 1);
});

test('second repair still expires at the original deadline', async (t) => {
  const { request, calls } = await fixture(t, 'openai-chat-completions', [{ text: 'not a submission' }, { text: 'still no submission' }, { hang: true }]);
  Object.assign(request, { phase: 'bootstrap_execute', result_schema: contracts.submit_result_schemas.bootstrap_execute, timeout_seconds: 0.4 });
  const started = performance.now();
  const result = await runRequest(request);
  assert.equal(result.error.code, 'deadline_exceeded');
  assert.equal(result.error.repair_rounds, 2);
  assert.equal(result.error.submission_attempts, 0);
  assert.equal(calls.length, 3);
  assert.ok(performance.now() - started < 1000);
  for (const call of calls.slice(1)) assert.deepEqual(call.body.tools.map((tool) => tool.function.name), calls[0].body.tools.map((tool) => tool.function.name));
});

for (const tool of [
  { name: 'bash', args: { command: 'touch forbidden-delivery-action' } },
  { name: 'write', args: { path: 'forbidden-delivery-action', content: 'forbidden' } },
]) test(`production cross-TaskRun readonly delivery rejects actual SDK ${tool.name}`, async (t) => {
  const { request, calls, cwd } = await fixture(t, 'openai-chat-completions', [
    { text: 'No structured result yet' }, { tools: [tool] },
  ]);
  Object.assign(request, { phase: 'bootstrap_execute', result_schema: contracts.submit_result_schemas.bootstrap_execute,
    max_result_repairs: 0, binding: { allowed_fact_ids: ['fact-1'] } });
  const initial = await runRequest(request);
  assert.equal(initial.error.code, 'submission_missing');
  const result = await runRequest({ ...request, task_run_id: 'readonly-delivery-run', resume: true, conclude_only: true });
  assert.equal(result.error.code, 'tool_permission', JSON.stringify(result));
  assert.equal(result.error.repair_rounds, 0);
  assert.equal(result.session, request.session);
  assert.equal(result.phase, 'bootstrap_execute');
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[1].body.tools.map((entry) => entry.function.name).sort(), ['read_evidence', 'register_artifact', 'submit_result']);
  await assert.rejects(access(join(cwd, 'forbidden-delivery-action')));
  assert.equal((await runRequest({ ...request, task_run_id: 'wrong-binding', resume: true, conclude_only: true,
    binding: { allowed_fact_ids: ['fact-other'] } })).error.code, 'session_ownership');
  assert.equal(calls.length, 2);
});

function sample(schema, root, field = '') {
  if (schema.$ref) return sample(root.$defs[schema.$ref.split('/').at(-1)], root, field);
  if ('const' in schema) return schema.const;
  if (schema.enum) return schema.enum[0];
  if (schema.anyOf) return sample(schema.anyOf.find((variant) => variant.type !== 'null') ?? schema.anyOf[0], root, field);
  if (schema.type === 'object') return Object.fromEntries([...new Set([...(schema.required ?? []), ...(schema.oneOf?.[0]?.required ?? [])])].map((key) => [key, sample(schema.properties[key], root, key)]));
  if (schema.type === 'array') {
    if (field === 'evidence_items') return [{ id: 'fixture', kind: 'artifact', source: 'capture', artifact: { artifact_id: 'a1' } }];
    return Array.from({ length: schema.minItems ?? 0 }, () => sample(schema.items, root, field));
  }
  if (schema.type === 'string') return field === 'artifact_id' ? 'a1' : 'fixture';
  if (schema.type === 'integer' || schema.type === 'number') return schema.minimum ?? 1;
  if (schema.type === 'boolean') return true;
  if (schema.type === 'null') return null;
  throw new Error(`No test sample for schema field ${field}`);
}

test('runner phase kind map equals the Python generated source contract', () => {
  assert.deepEqual(PHASE_RESULT_KINDS, contracts.phase_result_kinds);
  assert.equal(Object.keys(PHASE_RESULT_KINDS).length, 9);
  assert.equal(Object.hasOwn(PHASE_RESULT_KINDS, 'healthcheck'), false);
  assert.equal(Object.hasOwn(contracts.submit_result_schemas, 'healthcheck'), false);
  assert.equal(contracts.request_protocol, 'cairn-agent-request-v1');
  assert.equal(contracts.result_protocol, 'cairn-agent-result-v1');
});

for (const [phase, schema] of Object.entries(contracts.submit_result_schemas)) {
  test(`Python ${phase} schema compiles and submits through actual SDK`, async (t) => {
    const data = sample(schema.properties.data, schema);
    const args = { accepted: true, data };
    const validator = Compile(schema);
    assert.equal(validator.Check(args), true, JSON.stringify(validator.Errors(args)));
    assert.equal(validator.Check({ accepted: false, reason: 'fixture blocked' }), true);
    assert.equal(validator.Check({ accepted: false, data }), false);
    assert.equal(validator.Check({ accepted: true, data, protocol: 'injected-by-model' }), false);
    assert.equal(validator.Check({ accepted: 'true', data }), false);
    const { request } = await fixture(t, 'openai-chat-completions', [{ tools: [{ name: 'submit_result', args }] }]);
    request.phase = phase;
    request.result_schema = schema;
    const result = await runRequest(request);
    assert.equal(result.status, 'succeeded', JSON.stringify(result));
    assert.equal(result.result.kind, contracts.phase_result_kinds[phase]);
    assert.deepEqual(result.result.data, data);
  });
}
