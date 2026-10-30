import { request as httpRequest } from "node:http";
import type { BashOperations } from "@mariozechner/pi-coding-agent";
import { AgentFailure } from "./protocol.js";

export interface TaskExecutionBinding {
  protocol: "cairn-task-execution-v1";
  scope_id: string;
  task_run_id: string;
  socket_path: string;
}

export function createTaskBashOperations(binding: TaskExecutionBinding): BashOperations {
  return {
    exec(command, cwd, options) {
      return executeTaskCommand(binding, command, cwd, {
        timeout: options.timeout ?? 120,
        // SDK 的 env 包含整个控制进程环境；测试侧只继承 Dispatcher 准备的环境。
        signal: options.signal,
        onData: (data) => options.onData(data),
      });
    },
  };
}

export async function executeTaskCommand(
  binding: TaskExecutionBinding,
  command: string,
  cwd: string,
  options: {
    timeout: number;
    env?: Record<string, string>;
    signal?: AbortSignal;
    stdin?: Buffer;
    onData: (data: Buffer, side: "stdout" | "stderr") => void;
  },
): Promise<{ exitCode: number | null }> {
  options.signal?.throwIfAborted();
  const body = JSON.stringify({
    scope_id: binding.scope_id, task_run_id: binding.task_run_id,
    command, cwd, timeout_seconds: options.timeout, env: options.env ?? {},
    stdin_base64: (options.stdin ?? Buffer.alloc(0)).toString("base64"),
  });
  if (Buffer.byteLength(body) > 2 * 1024 * 1024) {
    throw new AgentFailure("execution_request_too_large", "Test execution request exceeds the local transport limit");
  }
  return new Promise((resolve, reject) => {
    let settled = false;
    const fail = (code: string) => {
      if (settled) return;
      settled = true;
      reject(new AgentFailure(code, "Task-bound test execution did not complete"));
    };
    const request = httpRequest({
      socketPath: binding.socket_path, path: "/execute", method: "POST", signal: options.signal,
      headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
    }, (response) => {
      if (response.statusCode !== 200) {
        response.resume();
        fail("execution_rejected");
        return;
      }
      let pending = "";
      response.setEncoding("utf8");
      response.on("data", (chunk: string) => {
        pending += chunk;
        if (pending.length > 256 * 1024) {
          fail("execution_protocol_invalid");
          request.destroy();
          return;
        }
        let end: number;
        while ((end = pending.indexOf("\n")) >= 0) {
          const line = pending.slice(0, end);
          pending = pending.slice(end + 1);
          try {
            const event = JSON.parse(line);
            for (const side of ["stdout", "stderr"] as const) {
              if (typeof event[side] === "string") options.onData(Buffer.from(event[side], "base64"), side);
            }
            if (typeof event.error === "string") fail(event.error);
            if (Number.isInteger(event.exit_code) && !settled) {
              settled = true;
              resolve({ exitCode: event.exit_code });
            }
          } catch {
            fail("execution_protocol_invalid");
            request.destroy();
            return;
          }
        }
      });
      response.on("error", () => fail("execution_disconnected"));
      response.on("end", () => { if (!settled) fail("execution_result_missing"); });
    });
    // 执行端负责命令截止；客户端只为失联提供额外的有界终点，不回退本地执行。
    request.setTimeout((options.timeout + 5) * 1000, () => {
      fail("execution_disconnected");
      request.destroy();
    });
    request.on("error", () => fail(options.signal?.aborted ? "cancelled" : "execution_unavailable"));
    request.end(body);
  });
}
