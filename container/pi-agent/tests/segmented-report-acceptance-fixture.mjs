import assert from 'node:assert/strict';
import { copyFile, mkdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { runRequest } from '../dist/runner.js';
import { parseRequest } from '../dist/protocol.js';
import { fixture } from './fixtures.mjs';

// PG 先冻结真实 claim、Packet 和模板，再将该次身份交给实际 SDK；不重绑既有产物。
const [inputPath, outputPath] = process.argv.slice(2);
const input = JSON.parse(await readFile(inputPath, 'utf8'));
const contracts = JSON.parse(await readFile(new URL('../contracts/submission-v1.json', import.meta.url), 'utf8'));
const cleanups = [];
const saved = [];
const audit = [];
const save = (key, content) => ({ name: 'save_report_segment', args: { key, content } });
const finish = () => ({ tools: [{ name: 'submit_result', args: {
  accepted: true, data: { schema_version: 5, segment_refs: saved.map((artifact_handle) => ({ artifact_handle })) },
} }] });
const script = [];
let totalContentBytes = 0;
if (input.mode === 'length') {
  script.push({ tools: [save('saved-before-length', { kind: 'executive_summary', text: '已保存合成材料；未形成完整报告。' })] });
  script.push({ tools: [{ name: 'submit_result', rawArgs: '{"accepted":true,"data":{"schema_version":5,"segment_refs":[' }], stopReason: 'length' });
} else {
  for (const vulnerability of input.packet.vulnerabilities) {
    const observation = input.packet.observations.find((item) => item.vulnerability_id === vulnerability.id);
    const evidence = observation.evidence_items[0];
    const original = input.packet.artifact_contents[evidence.response.artifact_id].text;
    const identity = { kind: 'finding', vulnerability_id: vulnerability.id, observation_id: observation.id };
    const description = `合成发现 ${vulnerability.id} 的必要前提与已验证影响。` + '这是仅用于离线合同验证的详细业务场景：普通身份读取其他所有者对象，已记录正常归属对照；没有访问真实目标。'.repeat(110);
    const tools = [
      save(`${vulnerability.id}-description`, { ...identity, description }),
      save(`${vulnerability.id}-steps`, { ...identity, steps: [1, 2, 3].map((step) => ({
        operation: `合成有序步骤 ${step}：复用冻结参数与身份，不重新访问目标。`,
        observed: `合成记录 ${vulnerability.id}/${step}：已保存观察。`,
        evidence: [{ evidence_item_id: evidence.id, selections: [{ side: 'response', artifact_id: evidence.response.artifact_id, byte_start: 0, byte_end: Buffer.byteLength(original) }] }],
      })) }),
      save(`${vulnerability.id}-remediation`, { ...identity, remediation: '在对象读取处核对当前主体与资源归属；该建议未实施或验证。', limitations: '仅依据冻结合成材料。' }),
    ];
    totalContentBytes += Buffer.byteLength(JSON.stringify(tools));
    script.push({ tools });
  }
  script.push({ tools: [
    save('summary', { kind: 'executive_summary', text: '仅整理十五条冻结合成发现，没有访问目标或新增测试。' }),
    save('overall-remediation', { kind: 'overall_remediation', text: '优先核对服务端对象归属，并在修复后复测；建议尚未执行。' }),
  ] });
  // 先注入半截最终调用，再让同一 SDK 会话在原期限内提交已保存引用。
  script.push({ tools: [{ name: 'submit_result', rawArgs: '{"accepted":true,"data":{"schema_version":5,"segment_refs":[' }], stopReason: 'length' });
  script.push(finish);
}
try {
  const { request, calls, candidateSpoolPath } = await fixture({ after: (cleanup) => cleanups.push(cleanup) }, 'openai-chat-completions', script);
  request.project_id = input.binding.project_id;
  request.task_run_id = input.binding.task_run_id;
  request.phase = 'report_execute';
  request.result_schema = contracts.submit_result_schemas.report_execute;
  request.prompt = '只整理冻结合成证据，逐段保存后仅提交引用；不得访问目标。';
  request.cwd = '/home/kali/workspace';
  request.session_root = join(request.cwd, '.cairn', 'pi');
  request.timeout_seconds = 60;
  request.capabilities.candidate_capture.max_manifest_bytes = 128 * 1024;
  const manifestPath = `/tmp/cairn-prompts/pi-report-segments-${request.capabilities.candidate_capture.capture_id}.json`;
  await mkdir('/tmp/cairn-prompts', { recursive: true, mode: 0o700 });
  await writeFile(manifestPath, '', { flag: 'wx', mode: 0o600 });
  request.capabilities.report_segments = { binding: input.binding, segment_schema: input.segment_schema, manifest_path: manifestPath };
  const result = await runRequest(parseRequest(request), { audit: (event) => {
    audit.push(event);
    if (event.event === 'report_segment_saved') saved.push(event.artifact_handle);
  } });
  assert.equal(result.status, input.mode === 'length' ? 'failed' : 'succeeded', JSON.stringify(result));
  const spoolPath = `${outputPath}.candidate-spool.bin`;
  await copyFile(candidateSpoolPath, spoolPath);
  const manifest = JSON.parse(await readFile(manifestPath));
  await writeFile(outputPath, JSON.stringify({ result, manifest, manifest_path: manifestPath, candidate_spool_path: spoolPath, audit, total_content_bytes: totalContentBytes, sdk_calls: calls.length }));
} finally {
  for (const cleanup of cleanups.reverse()) await cleanup();
}
