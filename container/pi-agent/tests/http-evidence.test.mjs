import test from 'node:test';
import assert from 'node:assert/strict';
import { parseHttpTrace } from '../dist/http-evidence.js';

function traceBlock(direction, kind, bytes) {
  const lines = [`${direction} ${kind}, ${bytes.length} bytes`];
  for (let offset = 0; offset < bytes.length; offset += 16) {
    lines.push(`${offset.toString(16).padStart(4, '0')}: ${[...bytes.subarray(offset, offset + 16)].map((byte) => byte.toString(16).padStart(2, '0')).join(' ')}`);
  }
  return lines.join('\n');
}

test('historical trace reading preserves binary bytes, repeated exchanges and missing sides', () => {
  const request = Buffer.from('GET /same HTTP/1.1\r\n\r\n');
  const response = Buffer.from('HTTP/1.1 200 OK\r\n\r\n');
  const body = Buffer.from([0, 128, 255, 10, 13]);
  const trace = [traceBlock('=> Send', 'header', request), traceBlock('<= Recv', 'header', response), traceBlock('<= Recv', 'data', body), '== Info: closed', traceBlock('=> Send', 'header', request)].join('\n');
  assert.deepEqual(parseHttpTrace(trace), [
    { request, response: Buffer.concat([response, body]) },
    { request, response: Buffer.alloc(0) },
  ]);
});

test('historical trace reading rejects malformed captured byte columns', () => {
  assert.throws(() => parseHttpTrace('=> Send header, 2 bytes\n0000: xx yy'), { code: 'artifact_invalid' });
});
