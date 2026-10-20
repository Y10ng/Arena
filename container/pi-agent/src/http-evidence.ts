import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { mkdir, open, readFile, realpath, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import type { TSchema } from "typebox";
import { ArtifactRegistry, evidencePath, type Artifact } from "./artifacts.js";
import { AgentFailure } from "./protocol.js";

export const HTTP_EVIDENCE_PARAMETERS = {
  type: "object", additionalProperties: false,
  properties: {
    url: { type: "string", minLength: 1, maxLength: 8192 },
    method: { type: "string", pattern: "^[A-Z]+$", maxLength: 32 },
    headers: { type: "array", maxItems: 64, items: { type: "string", maxLength: 16384 } },
    body_path: { type: "string", minLength: 1 },
    timeout_seconds: { type: "number", exclusiveMinimum: 0, maximum: 120 },
    title: { type: "string", maxLength: 200 },
    role: { type: "string", enum: ["primary", "supporting"] },
  },
  required: ["url"],
} as TSchema;

export interface HttpEvidenceInput {
  url: string; method?: string; headers?: string[]; body_path?: string;
  timeout_seconds?: number; title?: string; role?: "primary" | "supporting";
}
const REPRESENTATION = "curl HTTP/1.1 application-layer trace bytes; not network frames";
function configValue(value: string): string {
  return `"${value.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\n/g, "\\n").replace(/\r/g, "\\r").replace(/\t/g, "\\t")}"`;
}

// curl --trace 的 hex 栏是实际采集字节；忽略 TLS 记录及诊断，不从输入重新编造请求。
export function parseHttpTrace(trace: string): { request: Buffer; response: Buffer }[] {
  const exchanges: { request: Buffer[]; response: Buffer[] }[] = [];
  let exchange: typeof exchanges[number] | undefined;
  let side: "request" | "response" | undefined;
  let remaining = 0;
  for (const line of trace.split("\n")) {
    const event = /^(=> Send|<= Recv) (header|data), (\d+) bytes/.exec(line);
    if (event) {
      if (event[1] === "=> Send" && event[2] === "header") {
        exchange = { request: [], response: [] };
        exchanges.push(exchange);
      }
      side = exchange ? (event[1] === "=> Send" ? "request" : "response") : undefined;
      remaining = Number(event[3]);
      continue;
    }
    if (/^(?:=>|<=|==)/.test(line)) { side = undefined; remaining = 0; continue; }
    if (!side || !exchange || remaining <= 0 || !/^[0-9a-f]+: /i.test(line)) continue;
    const offset = line.indexOf(": ") + 2;
    const hex = line.slice(offset, offset + Math.min(16, remaining) * 3).trim();
    if (!/^(?:[0-9a-f]{2})(?: [0-9a-f]{2})*$/i.test(hex)) {
      throw new AgentFailure("artifact_invalid", "HTTP trace byte representation is invalid; original trace is retained");
    }
    const bytes = Buffer.from(hex.replaceAll(" ", ""), "hex");
    exchange[side].push(bytes);
    remaining -= bytes.length;
  }
  return exchanges.map((item) => ({ request: Buffer.concat(item.request), response: Buffer.concat(item.response) }));
}

export async function captureHttpEvidence(
  input: HttpEvidenceInput,
  context: { cwd: string; sessionRoot: string; artifacts: ArtifactRegistry; remainingSeconds: () => number; signal?: AbortSignal },
) {
  const url = new URL(input.url);
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) {
    throw new AgentFailure("artifact_invalid", "HTTP evidence requires an http(s) URL without embedded credentials");
  }
  if ((input.headers ?? []).some((header) => /[\r\n\0]/.test(header) || header.startsWith("@"))) {
    throw new AgentFailure("artifact_invalid", "HTTP headers must be individual literal header lines");
  }
  const timeout = Math.min(input.timeout_seconds ?? 30, 120, context.remainingSeconds());
  if (!(timeout > 0)) throw new AgentFailure("deadline_exceeded", "No execution time remains for HTTP capture");
  context.signal?.throwIfAborted();
  const id = `http-${randomUUID()}`;
  const directory = join(context.cwd, ".cairn", "artifacts", id);
  await mkdir(directory, { recursive: true, mode: 0o700 });
  if (await realpath(directory) !== resolve(directory)) throw new AgentFailure("artifact_invalid", "HTTP evidence directory cannot contain symlinks");
  let bodyPath: string | undefined;
  if (input.body_path) {
    const original = await evidencePath(context.cwd, input.body_path, context.sessionRoot);
    const source = await open(original, constants.O_RDONLY | constants.O_NOFOLLOW);
    try {
      const initial = await source.stat();
      if (!initial.isFile() || initial.size > 1024 * 1024) throw new AgentFailure("artifact_invalid", "HTTP capture requires a regular request body at most 1 MiB; no request was sent");
      const body = Buffer.alloc(initial.size);
      let offset = 0;
      while (offset < body.length) {
        const { bytesRead } = await source.read(body, offset, body.length - offset, offset);
        if (!bytesRead) throw new AgentFailure("artifact_unreadable", "HTTP request body capture was incomplete; no request was sent");
        offset += bytesRead;
      }
      bodyPath = join(directory, "input-body");
      await writeFile(bodyPath, body, { mode: 0o600, flag: "wx" });
    } finally { await source.close(); }
  }
  const tracePath = join(directory, "curl.trace");
  const stderrPath = join(directory, "curl.stderr");
  // 关闭 curlrc、代理及自动重定向，确保一轮只访问显式目标；Location 由后续独立调用处理。
  const args = ["--disable", "--config", "-", "--silent", "--show-error", "--globoff", "--path-as-is", "--http1.1", "--raw", "--proxy", "", "--proto", "=http,https", "--max-time", String(timeout), "--max-filesize", String(4 * 1024 * 1024), "--trace", tracePath, "--stderr", stderrPath, "--output", "/dev/null", ...(input.method === "HEAD" ? ["--head"] : [])];
  const config = [
    `url = ${configValue(input.url)}`,
    `request = ${configValue(input.method ?? (bodyPath ? "POST" : "GET"))}`,
    ...(input.headers ?? []).map((header) => `header = ${configValue(header)}`),
    ...(bodyPath ? [`data-binary = ${configValue(`@${bodyPath}`)}`] : []),
  ].join("\n") + "\n";
  const { exitCode, interrupted } = await new Promise<{ exitCode: number | null; interrupted: boolean }>((done, reject) => {
    const child = spawn("curl", args, { cwd: context.cwd, stdio: ["pipe", "ignore", "ignore"] });
    let stopped = false;
    const stop = () => { stopped = true; child.kill("SIGKILL"); };
    const timer = setTimeout(stop, Math.ceil(timeout * 1000) + 1000);
    context.signal?.addEventListener("abort", stop, { once: true });
    if (context.signal?.aborted) stop();
    child.stdin.on("error", () => {}); // 进程提前退出时由 close 的真实退出码报告，避免 EPIPE 丢失采集文件。
    child.once("error", () => {
      clearTimeout(timer); context.signal?.removeEventListener("abort", stop);
      reject(new AgentFailure("artifact_unreadable", "curl could not start; no HTTP capture was performed"));
    });
    child.once("close", (exitCode) => {
      clearTimeout(timer); context.signal?.removeEventListener("abort", stop);
      done({ exitCode, interrupted: stopped });
    });
    child.stdin.end(config);
  });
  const registered: Artifact[] = [];
  const register = async (path: string, kind: string) => {
    // 即使调用被取消也保留已经采集的原件；不得因此继续访问目标。
    const artifact = await context.artifacts.register(path, kind);
    registered.push(artifact);
    return { artifact_handle: artifact.handle };
  };
  const trace = await readFile(tracePath, "utf8");
  const traceRef = await register(tracePath, "raw_output");
  const evidenceItems: object[] = [];
  const exchanges = parseHttpTrace(trace);
  for (const [index, exchange] of exchanges.entries()) {
    const item: Record<string, unknown> = { id: `${id}-${index + 1}`, kind: "http", role: input.role ?? "primary", title: input.title || "HTTP 实际采集（应用层，非网络帧）", source: "capture" };
    for (const side of ["request", "response"] as const) if (exchange[side].length) {
      const path = join(directory, `${index + 1}-${side}.http`);
      await writeFile(path, exchange[side], { mode: 0o600, flag: "wx" });
      item[side] = await register(path, "raw_output");
    }
    evidenceItems.push(item);
  }
  evidenceItems.push({ id: `${id}-trace`, kind: "artifact", role: "supporting", title: "curl 采集 trace（含工具诊断，非网络帧）", source: "capture", artifact: traceRef });
  if ((await readFile(stderrPath)).length) {
    evidenceItems.push({ id: `${id}-stderr`, kind: "artifact", role: "supporting", title: "curl 实际错误输出", source: "capture", artifact: await register(stderrPath, "log") });
  }
  const metadataPath = join(directory, "capture.json");
  const metadata = { representation: REPRESENTATION, exit_code: exitCode, interrupted, complete: exitCode === 0 && !interrupted, redirects_followed: false, response_limit_bytes: 4 * 1024 * 1024, exchanges: exchanges.map((item) => ({ request_bytes: item.request.length, response_bytes: item.response.length })) };
  await writeFile(metadataPath, JSON.stringify(metadata, null, 2) + "\n", { mode: 0o600, flag: "wx" });
  evidenceItems.push({ id: `${id}-metadata`, kind: "artifact", role: "supporting", title: "HTTP 采集状态与缺项", source: "capture", artifact: await register(metadataPath, "file") });
  return { ...metadata, evidence_items: evidenceItems, artifacts: registered };
}
