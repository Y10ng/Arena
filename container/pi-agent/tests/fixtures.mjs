import { createServer } from 'node:http';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { createLocalBashOperations } from '@mariozechner/pi-coding-agent';
import { parseRequest } from '../dist/protocol.js';

export function canonicalFixtureRequest(request) {
  const execution = request.capabilities.task_execution;
  return execution ? { ...request, capabilities: { ...request.capabilities, task_execution: {
    ...execution, socket_path: `/run/cairn-exec/${execution.scope_id}/execute.sock`,
  } } } : request;
}

export function parseFixtureRequest(request) {
  // 先用正式路径校验契约，再仅为单元替身恢复临时 socket；生产 parser 不接受该路径。
  const parsed = parseRequest(canonicalFixtureRequest(request));
  if (request.capabilities.task_execution) parsed.capabilities.task_execution = request.capabilities.task_execution;
  return parsed;
}

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
  const sessionRoot = await mkdtemp(join(tmpdir(), 'cairn-pi-control-'));
  const calls = [];
  const sockets = new Set();
  const executionSockets = new Set();
  const executions = new Set();
  const executionErrors = [];
  const controllers = new Set();
  const candidateSpoolPaths = [];
  const scopeId = randomUUID().replaceAll('-', '');
  const taskRuns = new Set();
  const socketPath = join(sessionRoot, 'execute.sock');
  const freshTaskExecution = (taskRunId = 'task-test') => {
    taskRuns.add(taskRunId);
    return { protocol: 'cairn-task-execution-v1', scope_id: scopeId, task_run_id: taskRunId, socket_path: socketPath };
  };
  // 仅测试替身调用本地 SDK operations，生产 runner 仍通过 task-bound Unix HTTP 执行。
  const localOperations = createLocalBashOperations();
  const executionServer = createServer((req, res) => {
    const controller = new AbortController();
    controllers.add(controller);
    res.on('close', () => controller.abort());
    const execution = (async () => {
      try {
        let raw = '';
        for await (const chunk of req) raw += chunk;
        const body = JSON.parse(raw);
        // 仅承载 Bash 单元语义；stdin 驱动的证据采集由正式执行端用例验证。
        if (req.method !== 'POST' || req.url !== '/execute' || body.scope_id !== scopeId || !taskRuns.has(body.task_run_id)
          || body.cwd !== cwd || typeof body.command !== 'string' || !(body.timeout_seconds > 0) || body.stdin_base64) {
          res.writeHead(400); res.end(); return;
        }
        res.writeHead(200, { 'content-type': 'application/x-ndjson' });
        res.flushHeaders();
        const send = (event) => { if (!res.destroyed) res.write(`${JSON.stringify(event)}\n`); };
        try {
          const result = await localOperations.exec(body.command, cwd, {
            timeout: body.timeout_seconds, signal: controller.signal,
            onData: (data) => {
              for (let offset = 0; offset < data.length; offset += 65536) send({ stdout: data.subarray(offset, offset + 65536).toString('base64') });
            },
          });
          send({ exit_code: result.exitCode ?? -1 });
        } catch (error) {
          const code = error.message?.startsWith('timeout:') ? 'execution_timeout' : controller.signal.aborted ? 'cancelled' : 'execution_unavailable';
          executionErrors.push(code);
          send({ error: code });
        }
        res.end();
      } catch {
        if (!res.headersSent) res.writeHead(400);
        res.end();
      } finally {
        controllers.delete(controller);
      }
    })();
    executions.add(execution);
    void execution.finally(() => executions.delete(execution));
  });
  executionServer.on('connection', (socket) => { executionSockets.add(socket); socket.on('close', () => executionSockets.delete(socket)); });
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
      if (action.thinking) chunk({ reasoning_content: action.thinking });
      tools.forEach((tool, index) => {
        const parts = tool.rawArgChunks ?? [tool.rawArgs ?? JSON.stringify(tool.args)];
        parts.forEach((part, partIndex) => chunk({ tool_calls: [{ index,
          ...(partIndex === 0 ? { id: `call_${calls.length}_${index}`, type: 'function' } : {}),
          function: { ...(partIndex === 0 ? { name: tool.name } : {}), arguments: part },
        }] }));
      });
      chunk({}, action.stopReason ?? (tools.length ? 'tool_calls' : 'stop'), action.noUsage ? {} : { usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 } });
      res.write('data: [DONE]\n\n');
    } else if (protocol === 'anthropic-messages') {
      send({ type: 'message_start', message: { id: `msg_${calls.length}`, type: 'message', role: 'assistant', model: 'fixture-model', content: [], stop_reason: null, usage: action.noUsage ? undefined : { input_tokens: 10, output_tokens: 0 } } });
      let index = 0;
      if (action.thinking) {
        send({ type: 'content_block_start', index, content_block: { type: 'thinking', thinking: '', signature: '' } });
        send({ type: 'content_block_delta', index, delta: { type: 'thinking_delta', thinking: action.thinking } });
        send({ type: 'content_block_delta', index, delta: { type: 'signature_delta', signature: 'fixture-signature' } });
        send({ type: 'content_block_stop', index }); index += 1;
      }
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
      send({ type: 'message_delta', delta: { stop_reason: action.stopReason ?? (tools.length ? 'tool_use' : 'end_turn'), stop_sequence: null }, usage: action.noUsage ? undefined : { output_tokens: 5 } });
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
  t.after(async () => {
    for (const controller of controllers) controller.abort();
    for (const socket of [...sockets, ...executionSockets]) socket.destroy();
    await Promise.all([server, executionServer].map((listener) => new Promise((resolve) => listener.close(resolve))));
    await Promise.all([...executions]);
    await rm(cwd, { recursive: true, force: true });
    await rm(sessionRoot, { recursive: true, force: true });
    for (const path of candidateSpoolPaths) await rm(path, { force: true });
  });
  await new Promise((resolve, reject) => { executionServer.once('error', reject); executionServer.listen(socketPath, resolve); });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  const freshCandidateCapture = () => {
    const captureId = randomUUID();
    const spool_path = join('/tmp/cairn-prompts', `pi-candidates-${captureId}.bin`);
    candidateSpoolPaths.push(spool_path);
    return {
      protocol: 'cairn-candidate-spool-v1', spool_path, capture_id: captureId,
      max_candidates: 16, max_candidate_bytes: 2 * 1024 * 1024, max_manifest_bytes: 16 * 1024,
      max_spool_bytes: 34 * 1024 * 1024, forbidden_literals: [],
    };
  };
  const candidateCapture = freshCandidateCapture();
  const candidateSpoolPath = candidateCapture.spool_path;
  const base = `http://127.0.0.1:${server.address().port}/tenant/prefix`;
  const suffix = protocol === 'anthropic-messages' ? '/v1/messages' : protocol === 'openai-responses' ? '/responses' : '/chat/completions';
  const request = {
    protocol: 'cairn-agent-request-v1', project_id: 'project-test', task_run_id: 'task-test', phase: 'explore_execute',
    prompt: 'Use submit_result to report status ok.', session: `pi:${randomUUID()}`, resume: false,
    model: { protocol, max_output_tokens: 16384, model_name: 'fixture-model', base_url: base, inference_endpoint: base + suffix, auth_header: 'authorization-bearer', api_key: 'fixture-secret-key', custom_headers: [{ name: 'X-Fixture-Header', value: 'fixture-header-value' }] },
    result_schema: submissionSchema, binding: null, timeout_seconds: 10, max_result_repairs: 2, conclude_only: false, cwd, session_root: sessionRoot,
    capabilities: { candidate_capture: candidateCapture, task_execution: freshTaskExecution() },
  };
  return { request, calls, server, cwd, suffix, candidateSpoolPath, freshCandidateCapture, freshTaskExecution, executionErrors,
    waitForExecutions: () => Promise.all([...executions]) };
}
