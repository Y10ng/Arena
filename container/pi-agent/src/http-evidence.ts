import { AgentFailure } from "./protocol.js";

// 保留历史 curl trace 的原字节解析；新 HTTP 请求由测试网络自动采集，不再生产 trace Artifact。
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
