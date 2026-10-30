import { createConnection } from "node:net";
import type { TSchema } from "typebox";
import { AgentFailure, object, type JsonObject } from "./protocol.js";

export interface HttpTrafficBinding {
  protocol: "cairn-traffic-read-v1";
  project_id: string;
  task_run_id: string;
  socket_path: string;
}
const historical = { type: "boolean", description: "Explicitly read existing material from other TaskRuns in this same project; preserve its original scope and use source=historical when citing it." };
export const LIST_HTTP_FLOWS_PARAMETERS = {
  type: "object", additionalProperties: false,
  properties: {
    historical, task_run_id: { type: "string", minLength: 1, maxLength: 200, pattern: "^[A-Za-z0-9_-]+$" },
    cursor: { type: "integer", minimum: 0 }, limit: { type: "integer", minimum: 1, maximum: 50 },
    host: { type: "string", minLength: 1, maxLength: 255 }, method: { type: "string", minLength: 1, maxLength: 255 },
    state: { type: "string", enum: ["receiving", "complete", "partial", "failed"] },
    status_code: { type: "integer", minimum: 100, maximum: 999 },
  },
} as TSchema;
export const READ_HTTP_FLOW_PARAMETERS = {
  type: "object", additionalProperties: false,
  properties: {
    flow_id: { type: "string", minLength: 1, maxLength: 200, pattern: "^[A-Za-z0-9_-]+$" }, historical,
    side: { type: "string", enum: ["metadata", "request", "response"] },
    offset: { type: "integer", minimum: 0 }, limit: { type: "integer", minimum: 1, maximum: 32768 },
  }, required: ["flow_id"],
} as TSchema;

export async function readHttpTraffic(
  binding: HttpTrafficBinding, method: "list_http_flows" | "read_http_flow" | "prepare_http_evidence_image" | "publish_http_evidence_image", params: JsonObject, signal?: AbortSignal,
): Promise<JsonObject> {
  signal?.throwIfAborted();
  return new Promise((resolve, reject) => {
    const socket = createConnection(binding.socket_path);
    const chunks: Buffer[] = [];
    let size = 0;
    let done = false;
    const finish = (error?: AgentFailure, data?: JsonObject) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      socket.destroy();
      if (error) reject(error); else resolve(data!);
    };
    const abort = () => finish(new AgentFailure("cancelled", "Traffic read cancelled"));
    const timer = setTimeout(() => finish(new AgentFailure("traffic_read_timeout", "Traffic read timed out; no target request was sent")), 30000);
    signal?.addEventListener("abort", abort, { once: true });
    socket.on("connect", () => socket.write(JSON.stringify({ method, params }) + "\n"));
    socket.on("error", () => finish(new AgentFailure("traffic_read_unavailable", "Private traffic reader is unavailable")));
    socket.on("end", () => { if (!done) finish(new AgentFailure("traffic_read_incomplete", "Traffic read response was incomplete")); });
    socket.on("data", (chunk: Buffer) => {
      size += chunk.length;
      if (size > 2 * 1024 * 1024) return finish(new AgentFailure("traffic_response_too_large", "Traffic read exceeded the bounded response size"));
      chunks.push(chunk);
      if (!chunk.includes(10)) return;
      try {
        const response: unknown = JSON.parse(Buffer.concat(chunks).toString("utf8"));
        if (!object(response)) throw new Error();
        if (response.ok !== true) {
          const code = typeof response.error === "string" && /^traffic_[a-z0-9_]+$/.test(response.error) ? response.error : "traffic_read_failed";
          return finish(new AgentFailure(code, `Traffic read failed (${code}); existing material was not changed`));
        }
        if (!object(response.data)) throw new Error();
        const data = response.data;
        // 偏移只来自服务端的同一版本投影；不在 Pi 重新拼接 HTTP 或给二进制占位生成选段。
        if (method === "read_http_flow" && data.representation === "http-evidence-v1" && typeof data.text === "string" && data.text.length && data.body_display === "utf8") {
          const end = Number(data.offset) + Buffer.byteLength(data.text, "utf8");
          data.pointer = `utf8-bytes=${data.offset}:${end}`;
        }
        finish(undefined, data);
      } catch { finish(new AgentFailure("traffic_response_invalid", "Private traffic reader returned an invalid response")); }
    });
  });
}
