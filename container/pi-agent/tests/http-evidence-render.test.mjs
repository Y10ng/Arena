import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:net';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { HttpEvidenceRenderer, httpEvidencePages } from '../dist/http-evidence-render.js';

const requestText = 'GET /synthetic HTTP/1.1\r\nHost: example.invalid\r\n\r\n';
const responseText = 'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n<script>fetch("https://example.invalid/unwanted")</script>\n<img src="https://example.invalid/pixel">\n合成离线证据，不访问目标。';
const selection = (text) => ({ offset: 0, length: Buffer.byteLength(text), size: Buffer.byteLength(text), sha256: createHash('sha256').update(text).digest('hex') });
const input = {
  source: { kind: 'flow', project_id: 'synthetic', flow_id: 'synthetic-flow', revision: 1,
    representation: 'http-evidence-v1', selections: { request: selection(requestText), response: selection(responseText) } },
  request_text: requestText,
  response_text: responseText,
  notice: '合成材料；缺侧与选段如实保留。',
};
async function directory(t) {
  const path = await mkdtemp(join(tmpdir(), 'http-render-test-'));
  t.after(() => rm(path, { recursive: true, force: true }));
  return path;
}
async function reader(t, dir, handler) {
  const socket_path = join(dir, 'reader.sock');
  const server = createServer((socket) => {
    let raw = '';
    socket.on('data', (chunk) => {
      raw += chunk;
      if (raw.endsWith('\n')) socket.end(JSON.stringify(handler(JSON.parse(raw))) + '\n');
    });
  });
  await new Promise((resolve) => server.listen(socket_path, resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  return { protocol: 'cairn-traffic-read-v1', project_id: 'synthetic', task_run_id: 'synthetic-run', socket_path };
}

test('text is escaped, source ranges remain explicit, and page count is bounded', () => {
  const pages = httpEvidencePages(input);
  assert.equal(pages.html.length, 1);
  assert.ok(pages.html[0].includes('&lt;script&gt;'));
  assert.ok(!pages.html[0].includes('<script>'));
  assert.ok(!pages.html[0].includes('<img '));
  assert.ok(pages.html[0].includes("connect-src 'none'"));
  const long = httpEvidencePages({ ...input, response_text: '安全合成正文\n'.repeat(2000), request_text: null });
  assert.equal(long.html.length, 4);
  assert.equal(long.truncated, true);
  assert.ok(long.html[0].includes('未记录此侧材料'));
});

test('denied source produces no pixels or publication attempt', async (t) => {
  const dir = await directory(t), calls = [];
  const binding = await reader(t, dir, (request) => {
    calls.push(request.method);
    return { ok: false, error: 'traffic_read_http_403' };
  });
  const result = await new HttpEvidenceRenderer(binding, undefined, dir).render({ source: { kind: 'flow', flow_id: 'synthetic-flow' } });
  assert.equal(result.status, 'unavailable');
  assert.deepEqual(calls, ['prepare_http_evidence_image']);
  assert.equal(result.images, undefined);
});

test('frozen failure persists only local unavailability and manifest failure is nonblocking', async (t) => {
  const dir = await directory(t), frozen = join(dir, 'packet.json'), manifest = join(dir, 'images.json');
  await writeFile(frozen, JSON.stringify({ http_evidence_render_sources: {} }));
  const renderer = new HttpEvidenceRenderer(undefined, { frozen_materials_path: frozen, manifest_path: manifest }, dir);
  assert.equal((await renderer.render({ source_key: 'absent' })).status, 'unavailable');
  assert.deepEqual(JSON.parse(await readFile(manifest, 'utf8')), { version: 1, images: [], unavailable: true });
  const broken = new HttpEvidenceRenderer(undefined, { frozen_materials_path: frozen, manifest_path: join(dir, 'missing-directory', 'images.json') }, dir);
  const result = await broken.render({ source_key: 'absent' });
  assert.equal(result.status, 'unavailable');
  assert.equal(result.feedback_saved, false);
});

test('missing Chinese font is explicit optional unavailability rather than a tofu image', { skip: !process.env.CAIRN_EXPECT_NO_CJK }, async (t) => {
  const dir = await directory(t), calls = [];
  const binding = await reader(t, dir, (request) => {
    calls.push(request.method);
    return { ok: true, data: input };
  });
  const result = await new HttpEvidenceRenderer(binding, undefined, dir).render({ source: { kind: 'flow', flow_id: 'synthetic-flow' } });
  assert.equal(result.status, 'unavailable');
  assert.equal(result.reason, 'http_image_font_unavailable');
  assert.deepEqual(calls, ['prepare_http_evidence_image']);
});

test('real offline Chromium renders only frozen material and writes an independent optional manifest', { skip: !process.env.CAIRN_OFFLINE_RENDER || !!process.env.CAIRN_EXPECT_NO_CJK }, async (t) => {
  const dir = await directory(t), frozen = join(dir, 'packet-full.json'), manifest = join(dir, 'images.json');
  const original = JSON.stringify({ http_evidence_render_sources: { 'flow:synthetic-flow': input } });
  await writeFile(frozen, original);
  const renderer = new HttpEvidenceRenderer(undefined, { frozen_materials_path: frozen, manifest_path: manifest }, dir);
  assert.equal((await renderer.render({ source: { kind: 'flow', flow_id: 'live' } })).status, 'unavailable');
  const result = await renderer.render({ source_key: 'flow:synthetic-flow' });
  assert.equal(result.status, 'ready', JSON.stringify(result));
  const saved = JSON.parse(await readFile(manifest, 'utf8'));
  assert.equal(saved.images.length, 1);
  assert.equal(saved.unavailable, true);
  const png = await readFile(saved.images[0].path);
  assert.equal(png.subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
  assert.equal(png.readUInt32BE(16), 1440);
  assert.equal(png.readUInt32BE(20), 1000);
  assert.equal(createHash('sha256').update(png).digest('hex'), saved.images[0].sha256);
  assert.equal(await readFile(frozen, 'utf8'), original);
  assert.equal(result.images[0].path, undefined);
  assert.equal(result.images[0].artifact_id, undefined);
  if (process.env.CAIRN_SYNTHETIC_PNG) await writeFile(process.env.CAIRN_SYNTHETIC_PNG, png);
});

test('real render followed by optional upload failure returns no dangling Artifact or required declaration', { skip: !process.env.CAIRN_OFFLINE_RENDER || !!process.env.CAIRN_EXPECT_NO_CJK }, async (t) => {
  const dir = await directory(t), calls = [];
  const binding = await reader(t, dir, (request) => {
    calls.push(request.method);
    if (request.method === 'prepare_http_evidence_image') return { ok: true, data: input };
    assert.deepEqual(request.params.expected_source, input.source);
    return { ok: false, error: 'traffic_read_http_503' };
  });
  const result = await new HttpEvidenceRenderer(binding, undefined, dir).render({ source: { kind: 'flow', flow_id: 'synthetic-flow' } });
  assert.equal(result.status, 'unavailable');
  assert.deepEqual(result.images, []);
  assert.deepEqual(result.failures, ['page_1_publish_failed']);
  assert.deepEqual(calls, ['prepare_http_evidence_image', 'publish_http_evidence_image']);
  assert.equal(result.artifacts, undefined);
});
