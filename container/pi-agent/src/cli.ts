#!/usr/bin/env node
import { lstat, readFile } from "node:fs/promises";
import { SessionManager, CURRENT_SESSION_VERSION } from "@mariozechner/pi-coding-agent";
import { Compile } from "typebox/compile";
import { AgentFailure, envelope, object, parseRequest, type AgentRequest } from "./protocol.js";
import { runRequest } from "./runner.js";

const VERSION = "cairn-agent 1.0.0 (pi-coding-agent 0.73.0)";
const args = process.argv.slice(2);
if (args.length === 1 && args[0] === "--version") {
  process.stdout.write(`${VERSION}\n`);
} else if (args.length === 1 && args[0] === "--self-check") {
  const manager = SessionManager.inMemory("/tmp");
  const valid = Compile({ type: "object", properties: { accepted: { const: true } }, required: ["accepted"] }).Check({ accepted: true });
  if (!valid || manager.getHeader()?.version !== CURRENT_SESSION_VERSION) throw new Error("Offline SDK self-check failed");
  process.stdout.write(`${JSON.stringify({ status: "ok", engine: "pi", sdk_version: "0.73.0", offline: true, capabilities: ["candidate-spool-v1", "execution-checkpoint-spool-v1", "session-continuation-v1"] })}\n`);
} else {
  let identity: Partial<AgentRequest> = {};
  const controller = new AbortController();
  const stop = () => controller.abort();
  process.on("SIGTERM", stop);
  process.on("SIGINT", stop);
  const audit = (event: unknown) => { process.stderr.write(`${JSON.stringify(event)}\n`); };
  // stdout 属于唯一机器信封；依赖偶发console输出只计数，不泄露配置或混入协议。
  console.log = console.info = (...values: unknown[]) => audit({ protocol: "cairn-agent-audit-v1", event: "sdk_console", value_count: values.length });
  try {
    if (args.length !== 2 || args[0] !== "--request" || !args[1]) throw new AgentFailure("invalid_request", "Usage: cairn-agent --request FILE");
    const stat = await lstat(args[1]);
    if (!stat.isFile() || stat.isSymbolicLink() || (stat.mode & 0o077) !== 0 || stat.size > 8 * 1024 * 1024) throw new AgentFailure("invalid_request", "Request must be a private regular file of at most 8 MiB");
    const raw: unknown = JSON.parse(await readFile(args[1], "utf8"));
    if (object(raw)) for (const key of ["project_id", "task_run_id", "phase", "session"] as const) if (typeof raw[key] === "string") Object.assign(identity, { [key]: raw[key] });
    const request = parseRequest(raw);
    identity = request;
    const result = await runRequest(request, { signal: controller.signal, audit });
    process.stdout.write(`${JSON.stringify(result)}\n`);
    process.exitCode = result.status === "succeeded" ? 0 : result.status === "cancelled" ? 130 : 1;
  } catch (error) {
    const result = envelope(identity);
    result.error = error instanceof AgentFailure ? error.toJSON() : { code: "invalid_request", message: "Cannot read or parse request file" };
    process.stdout.write(`${JSON.stringify(result)}\n`);
    process.exitCode = 1;
  } finally {
    process.off("SIGTERM", stop);
    process.off("SIGINT", stop);
  }
}
