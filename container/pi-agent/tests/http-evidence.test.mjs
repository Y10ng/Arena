import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:net';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ArtifactRegistry } from '../dist/artifacts.js';
import { captureHttpEvidence } from '../dist/http-evidence.js';

// 直接访问本地 TCP fixture，不启动模型、Agent session 或真实任务。
test('HTTP evidence preserves actual bytes, redirects and incomplete exchanges', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'cairn-http-evidence-'));
  const cwd = join(root, 'workspace');
  const sessionRoot = join(root, 'sessions');
  await mkdir(cwd); await mkdir(sessionRoot);
  const sockets = new Set();
  const requests = new Map();
  const binaryBody = Buffer.from([0, 1, 10, 13, 128, 255]);
  const binaryResponse = Buffer.concat([
    Buffer.from('HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n6\r\n'),
    binaryBody, Buffer.from('\r\n0\r\n\r\n'),
  ]);
  const redirectResponse = Buffer.from('HTTP/1.1 302 Found\r\nLocation: /followed\r\nContent-Length: 0\r\nConnection: close\r\n\r\n');
  const partialResponse = Buffer.from('HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\npartial-body');
  const server = createServer((socket) => {
    sockets.add(socket);
    socket.on('close', () => sockets.delete(socket));
    let raw = Buffer.alloc(0);
    let handled = false;
    socket.on('data', (chunk) => {
      raw = Buffer.concat([raw, chunk]);
      const headerEnd = raw.indexOf('\r\n\r\n');
      if (handled || headerEnd < 0) return;
      const headers = raw.subarray(0, headerEnd).toString('latin1');
      const bodyLength = Number(/\r\nContent-Length: (\d+)/i.exec(headers)?.[1] ?? 0);
      if (raw.length < headerEnd + 4 + bodyLength) return;
      handled = true;
      const path = headers.split(' ')[1];
      requests.set(path, raw);
      if (path.startsWith('/binary')) socket.end(binaryResponse);
      else if (path === '/redirect') socket.end(redirectResponse);
      else if (path === '/partial') socket.write(partialResponse);
      else if (path === '/head') socket.end('HTTP/1.1 200 OK\r\nContent-Length: 123\r\nConnection: close\r\n\r\n');
      else if (path === '/empty') socket.end();
      // /silent 刻意不返回响应，检查只保留真实已发送的一侧。
    });
  });
  await new Promise((done) => server.listen(0, '127.0.0.1', done));
  t.after(async () => {
    for (const socket of sockets) socket.destroy();
    await new Promise((done) => server.close(done));
    await rm(root, { recursive: true, force: true });
  });
  const base = `http://127.0.0.1:${server.address().port}`;
  const capture = async (path, input = {}) => {
    const artifacts = new ArtifactRegistry(cwd, sessionRoot);
    const result = await captureHttpEvidence({ url: `${base}${path}`, ...input }, {
      cwd, sessionRoot, artifacts, remainingSeconds: () => 10,
    });
    const injected = { evidence_items: result.evidence_items };
    await artifacts.inject(injected);
    assert.deepEqual(injected.artifacts, result.artifacts);
    assert.equal(result.redirects_followed, false);
    assert.match(result.representation, /application-layer.*not network frames/);
    return result;
  };
  const bytes = (result, ref) => readFile(result.artifacts.find((item) => item.handle === ref.artifact_handle).path);

  await t.test('binary POST and chunk framing are actual collected bytes', async () => {
    await writeFile(join(cwd, 'body.bin'), binaryBody);
    const result = await capture('/binary?literal={one,two}', { body_path: 'body.bin', headers: ['X-Fixture: actual'] });
    assert.equal(result.complete, true);
    const [item] = result.evidence_items;
    assert.equal(item.kind, 'http');
    assert.equal(item.source, 'capture');
    assert.deepEqual(await bytes(result, item.request), requests.get('/binary?literal={one,two}'));
    assert.deepEqual(await bytes(result, item.response), binaryResponse);
    assert.ok(!(JSON.stringify(result).includes('partial-body')));
  });
  await t.test('redirect retains the 302 and never visits Location', async () => {
    const result = await capture('/redirect');
    assert.equal(result.complete, true);
    assert.deepEqual(await bytes(result, result.evidence_items[0].response), redirectResponse);
    assert.equal(requests.has('/followed'), false);
  });
  await t.test('timeout retains partial response rather than fabricating completion', async () => {
    const result = await capture('/partial', { timeout_seconds: 0.2 });
    assert.equal(result.complete, false);
    assert.notEqual(result.exit_code, 0);
    assert.deepEqual(await bytes(result, result.evidence_items[0].response), partialResponse);
    assert.ok(result.evidence_items.some((item) => item.id.endsWith('-stderr')));
  });
  await t.test('timeout without response leaves the response side absent', async () => {
    const result = await capture('/silent', { timeout_seconds: 0.2 });
    assert.equal(result.complete, false);
    assert.ok(result.evidence_items[0].request);
    assert.equal(result.evidence_items[0].response, undefined);
    assert.equal(result.exchanges[0].response_bytes, 0);
  });
  await t.test('empty response and HEAD retain their real outcomes', async () => {
    const failed = await capture('/empty');
    assert.equal(failed.complete, false);
    assert.equal(failed.evidence_items[0].response, undefined);
    const head = await capture('/head', { method: 'HEAD', timeout_seconds: 0.2 });
    assert.equal(head.complete, true);
    assert.match((await bytes(head, head.evidence_items[0].response)).toString(), /Content-Length: 123/);
  });
});
