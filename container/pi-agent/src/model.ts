import type { Api, Model } from "@mariozechner/pi-ai";
import { AgentFailure, type AgentRequest } from "./protocol.js";

export const REQUEST_PROVIDER = "cairn-request";
export const SDK_AUTH_PLACEHOLDER = "cairn-explicit-header-auth";
export function requestModel(request: AgentRequest): Model<Api> {
  const config = request.model;
  const suffix = config.protocol === "anthropic-messages" ? "/v1/messages" : config.protocol === "openai-chat-completions" ? "/chat/completions" : "/responses";
  let endpoint: URL;
  try { endpoint = new URL(config.inference_endpoint); new URL(config.base_url); }
  catch { throw new AgentFailure("unsupported_model_config", "Model URLs must be absolute HTTP(S) URLs"); }
  if (!["http:", "https:"].includes(endpoint.protocol) || endpoint.username || endpoint.password || endpoint.search || endpoint.hash || !endpoint.pathname.endsWith(suffix)) {
    throw new AgentFailure("unsupported_model_config", `SDK endpoint must end with ${suffix}, without URL credentials, query, or fragment`);
  }
  const headers: Record<string, string | null> = { Authorization: null, "x-api-key": null };
  const seen = new Set<string>();
  for (const header of config.custom_headers) {
    const name = header.name.toLowerCase();
    if (!/^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/.test(header.name) || /[\r\n\0]/.test(header.value) || seen.has(name) || ["authorization", "x-api-key", "host", "content-length", "transfer-encoding", "connection"].includes(name)) {
      throw new AgentFailure("unsupported_model_config", "Custom header name, value, duplicate, or reserved header is invalid");
    }
    seen.add(name);
    headers[header.name] = header.value;
  }
  if (/[\r\n\0]/.test(config.api_key)) throw new AgentFailure("unsupported_model_config", "Invalid credential header value");
  if (config.auth_header === "authorization-bearer") headers.Authorization = `Bearer ${config.api_key}`;
  else headers["x-api-key"] = config.api_key;
  return {
    id: config.model_name, name: config.model_name, provider: REQUEST_PROVIDER,
    api: config.protocol === "openai-chat-completions" ? "openai-completions" : config.protocol,
    baseUrl: `${endpoint.origin}${endpoint.pathname.slice(0, -suffix.length)}`,
    reasoning: false, input: ["text", "image"],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: 128000, maxTokens: 16384,
    // pi 类型未表达底层官方客户端支持的 null 删除语义；HTTP 夹具检查只发送选定的认证头。
    headers: headers as Record<string, string>,
    ...(config.protocol === "openai-chat-completions" ? { compat: { supportsStore: false, supportsDeveloperRole: false, supportsReasoningEffort: false } } : {}),
  };
}
