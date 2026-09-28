import { createServer } from 'node:http';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';

export const submissionSchema = {
  type: 'object',
  anyOf: [
    { type: 'object', properties: { accepted: { const: true }, data: { type: 'object', properties: { status: { const: 'ok' } }, required: ['status'], additionalProperties: false } }, required: ['accepted', 'data'], additionalProperties: false },
    { type: 'object', properties: { accepted: { const: false }, reason: { type: 'string', minLength: 1 } }, required: ['accepted', 'reason'], additionalProperties: false },
  ],
};
export const ok = { tools: [{ name: 'submit_result', args: { accepted: true, data: { status: 'ok' } } }] };
export async function fixture(t, protocol = 'openai-chat-completions', script = [ok]) {
  const cwd = await mkdtemp(join(tmpdir(), 'cairn-pi-test-'));
  const calls = [];
  const sockets = new Set();
  const server = createServer(async (req, res) => {
    let raw = '';
    for await (const chunk of req) raw += chunk;
    const call = { path: req.url, headers: req.headers, body: JSON.parse(raw) };
    calls.push(call);
    let action = script[Math.min(calls.length - 1, script.length - 1)];
    if (typeof action === 'function') action = await action(call, calls);
    if (action.hang) return;
    if (action.status) { res.writeHead(action.status, { 'content-type': 'application/json' }); res.end(JSON.stringify({ error: { message: action.error ?? 'fixture failure', type: 'api_error' } })); return; }
    res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' });
    const send = (value) => res.write(`${protocol === 'openai-chat-completions' ? '' : `event: ${value.type}\n`}data: ${JSON.stringify(value)}\n\n`);
    const tools = action.tools ?? [];
    const usage = action.noUsage ? undefined : { input_tokens: 10, output_tokens: 5, total_tokens: 15 };
    if (protocol === 'openai-chat-completions') {
      const chunk = (delta, finish = null, extra = {}) => send({ id: `chat_${calls.length}`, object: 'chat.completion.chunk', created: 1, model: 'fixture-model', choices: [{ index: 0, delta, finish_reason: finish }], ...extra });
      chunk({ role: 'assistant' });
      if (action.text) chunk({ content: action.text });
      tools.forEach((tool, index) => {
        const parts = tool.rawArgChunks ?? [tool.rawArgs ?? JSON.stringify(tool.args)];
        parts.forEach((part, partIndex) => chunk({ tool_calls: [{ index,
          ...(partIndex === 0 ? { id: `call_${calls.length}_${index}`, type: 'function' } : {}),
          function: { ...(partIndex === 0 ? { name: tool.name } : {}), arguments: part },
        }] }));
      });
      chunk({}, tools.length ? 'tool_calls' : 'stop', action.noUsage ? {} : { usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 } });
      res.write('data: [DONE]\n\n');
    } else if (protocol === 'anthropic-messages') {
      send({ type: 'message_start', message: { id: `msg_${calls.length}`, type: 'message', role: 'assistant', model: 'fixture-model', content: [], stop_reason: null, usage: action.noUsage ? undefined : { input_tokens: 10, output_tokens: 0 } } });
      let index = 0;
      if (action.text) {
        send({ type: 'content_block_start', index, content_block: { type: 'text', text: '' } });
        send({ type: 'content_block_delta', index, delta: { type: 'text_delta', text: action.text } });
        send({ type: 'content_block_stop', index }); index += 1;
      }
      for (const tool of tools) {
        send({ type: 'content_block_start', index, content_block: { type: 'tool_use', id: `tool_${calls.length}_${index}`, name: tool.name, input: {} } });
        const parts = tool.rawArgChunks ?? [tool.rawArgs ?? JSON.stringify(tool.args)];
        for (const part of parts) send({ type: 'content_block_delta', index, delta: { type: 'input_json_delta', partial_json: part } });
        send({ type: 'content_block_stop', index }); index += 1;
      }
      send({ type: 'message_delta', delta: { stop_reason: tools.length ? 'tool_use' : 'end_turn', stop_sequence: null }, usage: action.noUsage ? undefined : { output_tokens: 5 } });
      send({ type: 'message_stop' });
    } else {
      send({ type: 'response.created', response: { id: `resp_${calls.length}`, status: 'in_progress', output: [] } });
      if (action.text) {
        const item = { type: 'message', id: `msg_${calls.length}`, role: 'assistant', content: [{ type: 'output_text', text: action.text, annotations: [] }] };
        send({ type: 'response.output_item.added', output_index: 0, item: { ...item, content: [] } });
        send({ type: 'response.content_part.added', item_id: item.id, output_index: 0, content_index: 0, part: { type: 'output_text', text: '', annotations: [] } });
        send({ type: 'response.output_text.delta', item_id: item.id, output_index: 0, content_index: 0, delta: action.text });
        send({ type: 'response.output_item.done', output_index: 0, item });
      }
      tools.forEach((tool, index) => {
        const parts = tool.rawArgChunks ?? [tool.rawArgs ?? JSON.stringify(tool.args)];
        const item = { type: 'function_call', id: `fc_${calls.length}_${index}`, call_id: `call_${calls.length}_${index}`, name: tool.name, arguments: parts.join(''), status: 'completed' };
        send({ type: 'response.output_item.added', output_index: index, item: { ...item, arguments: '', status: 'in_progress' } });
        for (const part of parts) send({ type: 'response.function_call_arguments.delta', item_id: item.id, output_index: index, delta: part });
        send({ type: 'response.function_call_arguments.done', item_id: item.id, output_index: index, arguments: item.arguments });
        send({ type: 'response.output_item.done', output_index: index, item });
      });
      send({ type: 'response.completed', response: { id: `resp_${calls.length}`, status: 'completed', usage, output: [] } });
    }
    if (action.interrupt) res.destroy(); else res.end();
  });
  server.on('connection', (socket) => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const candidateSpoolPaths = [];
  const freshCandidateCapture = () => {
    const captureId = randomUUID();
    const spool_path = join('/tmp/cairn-prompts', `pi-candidates-${captureId}.bin`);
    candidateSpoolPaths.push(spool_path);
    return {
      protocol: 'cairn-candidate-spool-v1', spool_path, capture_id: captureId,
      max_candidates: 16, max_candidate_bytes: 2 * 1024 * 1024, max_manifest_bytes: 4 * 1024,
      max_spool_bytes: 34 * 1024 * 1024, forbidden_literals: [],
    };
  };
  const candidateCapture = freshCandidateCapture();
  const candidateSpoolPath = candidateCapture.spool_path;
  t.after(async () => { for (const socket of sockets) socket.destroy(); await new Promise((resolve) => server.close(resolve)); await rm(cwd, { recursive: true, force: true }); for (const path of candidateSpoolPaths) await rm(path, { force: true }); });
  const base = `http://127.0.0.1:${server.address().port}/tenant/prefix`;
  const suffix = protocol === 'anthropic-messages' ? '/v1/messages' : protocol === 'openai-responses' ? '/responses' : '/chat/completions';
  const request = {
    protocol: 'cairn-agent-request-v1', project_id: 'project-test', task_run_id: 'task-test', phase: 'explore_execute',
    prompt: 'Use submit_result to report status ok.', session: `pi:${randomUUID()}`, resume: false,
    model: { protocol, model_name: 'fixture-model', base_url: base, inference_endpoint: base + suffix, auth_header: 'authorization-bearer', api_key: 'fixture-secret-key', custom_headers: [{ name: 'X-Fixture-Header', value: 'fixture-header-value' }] },
    result_schema: submissionSchema, binding: null, timeout_seconds: 10, max_result_repairs: 2, conclude_only: false, cwd, session_root: join(cwd, '.cairn', 'pi'),
    capabilities: { candidate_capture: candidateCapture },
  };
  return { request, calls, server, cwd, suffix, candidateSpoolPath, freshCandidateCapture };
}
