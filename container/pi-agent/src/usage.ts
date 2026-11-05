import type { Usage } from "@mariozechner/pi-ai";
import type { AgentEnvelope } from "./protocol.js";

type ResponseUsage = Pick<Usage, "input" | "output" | "cacheRead" | "cacheWrite">;

export function emptyUsage() {
  return { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0, reported: 0, missing: 0 };
}

export function recordUsage(current: ReturnType<typeof emptyUsage>, usage: ResponseUsage | undefined) {
  if (!usage || usage.input + usage.output + usage.cacheRead + usage.cacheWrite <= 0) {
    return { ...current, missing: current.missing + 1 };
  }
  return {
    input_tokens: current.input_tokens + usage.input,
    output_tokens: current.output_tokens + usage.output,
    cache_read_tokens: current.cache_read_tokens + usage.cacheRead,
    cache_write_tokens: current.cache_write_tokens + usage.cacheWrite,
    reported: current.reported + 1,
    missing: current.missing,
  };
}

export function usageResult(current: ReturnType<typeof emptyUsage>, chat: boolean):
  Pick<AgentEnvelope, "usage" | "usage_missing_responses"> {
  // 各类执行均保留已知小计；权威消费读取独立回执，不依赖成功 stdout。
  const known = current.reported > 0;
  // SDK 对缺失分量也填 0，不能将这些 0 宣称为真实零消耗。
  const amount = (value: number) => value > 0 ? value : null;
  return {
    usage: known ? {
      input_tokens: amount(current.input_tokens), output_tokens: amount(current.output_tokens),
      cache_read_tokens: amount(current.cache_read_tokens), cache_write_tokens: amount(current.cache_write_tokens),
    } : null,
    ...(chat ? { usage_missing_responses: current.missing } : {}),
  };
}
