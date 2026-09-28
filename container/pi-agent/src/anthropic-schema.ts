import type { AgentSession } from "@mariozechner/pi-coding-agent";
import type { TSchema } from "typebox";

export function preserveAnthropicSubmissionSchema(agent: AgentSession["agent"], schema: TSchema): void {
  const originalOnPayload = agent.onPayload;
  agent.onPayload = async (payload, model) => {
    const outbound = await originalOnPayload?.(payload, model) ?? payload;
    if (!outbound || typeof outbound !== "object" || !("tools" in outbound) || !Array.isArray(outbound.tools)) {
      throw new Error("Anthropic payload has no tools");
    }
    let found = false;
    const tools = outbound.tools.map((tool) => {
      if (!tool || typeof tool !== "object" || tool.name !== "submit_result") return tool;
      found = true;
      // pi-ai 的出站转换仅保留根 properties/required；这里恢复本次提交使用的完整冻结 Schema。
      return { ...tool, input_schema: schema };
    });
    if (!found) throw new Error("Anthropic payload has no submit_result tool");
    return { ...outbound, tools };
  };
}
