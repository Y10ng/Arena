import { createHash } from "node:crypto";
import { constants, readFileSync } from "node:fs";
import { copyFile, mkdir, readFile, unlink, writeFile } from "node:fs/promises";
import { basename, join } from "node:path";
import { tmpdir } from "node:os";
import { Compile } from "typebox/compile";
import { System } from "typebox/system";
import type { TSchema } from "typebox";
import {
  AuthStorage,
  createAgentSession,
  createBashToolDefinition,
  createExtensionRuntime,
  ModelRegistry,
  SettingsManager,
  type AgentSession,
  type BashOperations,
  type ResourceLoader,
  type ToolDefinition,
} from "@mariozechner/pi-coding-agent";
import { preserveAnthropicSubmissionSchema } from "./anthropic-schema.js";
import { ArtifactRegistry, ARTIFACT_KINDS, evidencePath, evidenceFailure, readEvidence } from "./artifacts.js";
import { createCheckpointTool } from "./checkpoints.js";
import { readHttpTraffic, LIST_HTTP_FLOWS_PARAMETERS, READ_HTTP_FLOW_PARAMETERS } from "./http-flows.js";
import { HttpEvidenceRenderer, RENDER_HTTP_EVIDENCE_PARAMETERS } from "./http-evidence-render.js";
import { CandidateSpool, type CandidateAttempt, type CandidateOutcome } from "./candidate-spool.js";
import { SubmissionState, safeSchemaIssue } from "./submission.js";
import { ReportSegments } from "./report-segments.js";
import { AgentFailure, candidateLimits, envelope, object, PHASE_RESULT_KINDS, type AgentEnvelope, type AgentRequest, type JsonObject } from "./protocol.js";
import { acquireSession, type SessionLease } from "./session.js";
import { createTaskBashOperations } from "./task-execution.js";
import { requestModel, REQUEST_PROVIDER, SDK_AUTH_PLACEHOLDER } from "./model.js";
import { emptyUsage, recordUsage, usageResult } from "./usage.js";
import { ConsumptionSpool } from "./consumption.js";

import { assertPublicText, CHAT_SYSTEM_PROMPT, createProjectTools, PROJECT_TOOL_NAMES } from "./project-tools.js";
import { CHAT_FILE_TOOLS, createChatBashOperations, createChatFileTools } from "./chat-workspace.js";

const SAFE_TOOLS = ["read_evidence", "register_artifact", "submit_result"];
const BASE_EXECUTION_TOOLS = ["read", "bash", "edit", "write", "grep", "find", "ls", ...SAFE_TOOLS];
const MAX_RESULT_BYTES = 1024 * 1024;
// deadline 限制总时长但不限制快速重复的截断请求；每个 Run 只提供两次续行，不借用结果修正额度。
const MAX_OUTPUT_CONTINUATIONS = 2;
const OUTPUT_CONTINUATION_PROMPT =
  "Your previous model response reached its output limit without a tool call or submission. " +
  "Continue the unfinished task in this same session using the existing observations and tool results. " +
  "Do not repeat completed actions. The original authorization, execution deadline and resource limits still apply. " +
  "Use submit_result when you have a truthful result.";
const SCHEMA_ERROR_BUDGET = 256;
const SCHEMA_DIAGNOSTIC_VIEW_LIMIT = 32;
const SCHEMA_COLLECTED_ERROR_BUDGET = 4096;
const DEFAULT_BASH_TOOL_TIMEOUT_SECONDS = 120;

export function executionNow(): number {
  // 研究域挂载该只读偏移；独立报告和聊天没有此挂载，仍按各自正常预算计时。
  let paused = 0;
  try { paused = Number(readFileSync("/run/cairn-exec/paused-seconds", "utf8")); }
  catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
  if (!Number.isFinite(paused) || paused < 0) throw new Error("execution_clock_invalid");
  return performance.now() - paused * 1000;
}

export function createRequestBashToolDefinition(
  cwd: string,
  maxTimeoutSeconds: number,
  remainingSeconds: () => number,
  operations: BashOperations,
): ReturnType<typeof createBashToolDefinition> {
  const boundedOperations: BashOperations = {
    async exec(command, commandCwd, options) {
      const requested = typeof options.timeout === "number" && options.timeout > 0
        ? options.timeout
        : maxTimeoutSeconds;
      const remaining = Math.max(0, remainingSeconds());
      const timeout = Math.min(requested, maxTimeoutSeconds, remaining);
      if (!(timeout > 0)) throw new Error("timeout:0");
      return operations.exec(command, commandCwd, { ...options, timeout });
    },
  };
  const tool = createBashToolDefinition(cwd, { operations: boundedOperations });
  tool.description = `${tool.description} Commands default to at most ${maxTimeoutSeconds} seconds and are also limited by the remaining execution budget.`;
  return tool;
}
export function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (object(value)) return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}
function hash(value: unknown): string { return createHash("sha256").update(canonicalJson(value)).digest("hex"); }
function toolResult(value: unknown, terminate = false) {
  return { content: [{ type: "text" as const, text: JSON.stringify(value) }], details: {}, terminate };
}
function failure(error: unknown): NonNullable<AgentEnvelope["error"]> {
  if (error instanceof AgentFailure) return error.toJSON();
  return { code: "runner_error", message: "Runner could not complete this request" };
}
function providerFailure(message: string): AgentFailure {
  if (/\b(401|403)\b|auth|credential|api.?key/i.test(message)) return new AgentFailure("provider_auth", "Provider rejected authentication or authorization");
  if (/\b429\b|rate.?limit/i.test(message)) return new AgentFailure("provider_rate_limit", "Provider rate limit exceeded");
  return new AgentFailure("provider_error", "Provider stream failed or returned an error");
}
function withSchemaErrorBudget<T>(collect: () => T): T {
  const previous = System.Settings.Get().maxErrors;
  try {
    // 每次 TypeBox 遍历都保留硬上限；公开反馈仍由 safeSchemaIssue 限为 8 条规则。
    System.Settings.Set({ maxErrors: SCHEMA_ERROR_BUDGET });
    return collect();
  } finally {
    System.Settings.Set({ maxErrors: previous });
  }
}
function schemaDiagnosticViews(schema: JsonObject): JsonObject[] {
  const definitions = object(schema.$defs) ? schema.$defs : undefined;
  const views: JsonObject[] = [];
  const addView = (view: JsonObject) => {
    if (views.length >= SCHEMA_DIAGNOSTIC_VIEW_LIMIT) return;
    views.push(definitions ? { ...view, $defs: definitions } : view);
  };
  const rootLogic: JsonObject = {};
  for (const keyword of ["if", "then", "else", "allOf", "anyOf", "oneOf", "not"]) {
    if (schema[keyword] !== undefined) rootLogic[keyword] = schema[keyword];
  }
  if (Object.keys(rootLogic).length > 0) addView(rootLogic);

  const addPropertyViews = (node: JsonObject, path: string[], depth: number) => {
    const properties = object(node.properties) ? node.properties : undefined;
    if (!properties) return;
    for (const [name, child] of Object.entries(properties)) {
      if (!object(child) || views.length >= SCHEMA_DIAGNOSTIC_VIEW_LIMIT) continue;
      const childPath = [...path, name];
      let projected: unknown = child;
      for (let index = childPath.length - 1; index >= 0; index -= 1) {
        projected = {
          type: "object",
          properties: { [childPath[index]!]: projected },
        };
      }
      addView(projected as JsonObject);
      // 冻结结果信封目前只需展开到 data 的直接字段；该界限也避免按候选规模生成验证器。
      if (depth < 1) addPropertyViews(child, childPath, depth + 1);
    }
  };
  addPropertyViews(schema, [], 0);
  return views;
}
function loader(systemPrompt: string): ResourceLoader {
  return {
    getExtensions: () => ({ extensions: [], errors: [], runtime: createExtensionRuntime() }),
    getSkills: () => ({ skills: [], diagnostics: [] }),
    getPrompts: () => ({ prompts: [], diagnostics: [] }),
    getThemes: () => ({ themes: [], diagnostics: [] }),
    getAgentsFiles: () => ({ agentsFiles: [] }),
    getSystemPrompt: () => systemPrompt, getAppendSystemPrompt: () => [],
    extendResources: () => {}, reload: async () => {},
  };
}
export interface RunnerOptions {
  signal?: AbortSignal;
  audit?: (event: JsonObject) => void;
}
export async function runRequest(request: AgentRequest, options: RunnerOptions = {}): Promise<AgentEnvelope> {
  const result = envelope(request);
  const chat = request.phase === "chat_turn";
  const chatForbidden = chat ? [request.model.api_key, ...request.model.custom_headers.map(({ value }) => value), ...request.capabilities.project_tools!.forbidden_literals, request.capabilities.project_tools!.socket_path, CHAT_SYSTEM_PROMPT] : [];
  const toolCwd = chat ? request.capabilities.project_tools!.workspace_path : request.cwd;
  let finalText: string | undefined;
  const deadline = executionNow() + request.timeout_seconds * 1000;
  const readonly = !chat && (request.conclude_only || request.phase.endsWith("_conclude") || request.phase === "report_execute");
  // 聊天由私有桥惰性准入研究；自然回答与结构化任务收口不是同一个开关。
  const bridgedChat = chat && request.capabilities.project_tools!.research_execution.task_run_id === request.task_run_id;
  const limitedTools = bridgedChat || readonly;
  const concludeTimeoutSeconds = limitedTools ? 0 : (request.conclude_timeout_seconds ?? 0);
  const remainingTimeNoticeAt = deadline - Math.min(request.timeout_seconds, concludeTimeoutSeconds) * 1000;
  const reportCapability = request.capabilities.report_segments;
  const trafficCapability = request.phase === "report_execute" ? undefined : request.capabilities.http_traffic;
  const trafficTools = trafficCapability ? ["list_http_flows", "read_http_flow"] : [];
  const renderTools = trafficCapability || request.capabilities.http_evidence_render ? ["render_http_evidence"] : [];
  const safeTools = chat ? [...PROJECT_TOOL_NAMES, ...CHAT_FILE_TOOLS] : reportCapability ? [...SAFE_TOOLS, "save_report_segment", ...renderTools] : [...SAFE_TOOLS, ...trafficTools, ...renderTools];
  let session: AgentSession | undefined;
  let lease: SessionLease | undefined;
  let cancelled: "cancelled" | "deadline_exceeded" | undefined;
  const checkpointCapability = request.capabilities?.execution_checkpoints;
  const executionTools = chat ? PROJECT_TOOL_NAMES : reportCapability ? safeTools : [...BASE_EXECUTION_TOOLS, ...trafficTools, ...renderTools, ...(checkpointCapability && !readonly ? ["record_checkpoint"] : [])];
  let candidate: JsonObject | null = null;
  let candidateDigest: string | null = null;
  let conflict = false;
  let attempted = false;
  const rawArguments = new Map<number, string>();
  const oversizedArgumentIndexes = new Set<number>();
  const invalidArgumentCalls = new Set<string>();
  const authorizedToolCalls = new Set<string>();
  const candidateAttempts = new Map<string, CandidateAttempt>();
  const candidateOutcomeCalls = new Set<string>();
  let assistantTurn = 0;
  let outputContinuations = 0;
  let reportOutputRecoveries = 0;
  let truncatedReportUnit = false;
  let truncatedWithoutToolCalls = false;
  const savedSegmentCalls = new Map<string, { key: string; artifact_handle: string }>();
  const rejectedReportCalls = new Set<string>();
  let currentToolCallId = "";
  const submission = new SubmissionState(request.max_result_repairs);
  let candidateCaptureError: AgentFailure | undefined;
  let providerError: AgentFailure | undefined;
  let outputLimitReached = false;
  let usage = emptyUsage();
  const audit = (event: string, extra: JsonObject = {}) => {
    // 调用处只生成固定码、声明路径和计数；不复制原参数，也不事后替换而改写诊断事实。
    options.audit?.({ protocol: "cairn-agent-audit-v1", timestamp: new Date().toISOString(), project_id: request.project_id, task_run_id: request.task_run_id, phase: request.phase, session: request.session, event, ...extra });
  };
  const consumption = request.capabilities.consumption
    ? new ConsumptionSpool(request.capabilities.consumption.spool_path, receipt => {
      audit("consumption_gap", { consumption_id: request.capabilities.consumption!.id, receipt });
    }) : undefined;
  const stop = (reason: "cancelled" | "deadline_exceeded") => {
    cancelled ??= reason;
    session?.agent.abort();
  };
  const onAbort = () => stop("cancelled");
  options.signal?.addEventListener("abort", onAbort, { once: true });
  // 解冻后先核对执行时钟，不能让已到期的 Node 墙钟 timer 直接中止原 session。
  const timer = setInterval(() => {
    if (executionNow() >= deadline) stop("deadline_exceeded");
  }, 100);
  let remainingTimeTimer: NodeJS.Timeout | undefined;
  const ensureActive = () => {
    if (options.signal?.aborted) stop("cancelled");
    if (executionNow() >= deadline) stop("deadline_exceeded");
    if (cancelled) throw new AgentFailure(cancelled, cancelled === "cancelled" ? "Request cancelled" : "Request deadline exceeded");
  };
  try {
    ensureActive();
    if (!bridgedChat && !readonly && !request.capabilities.task_execution) throw new AgentFailure("execution_binding_missing", "Task-bound test execution is required");
    const model = requestModel(request);
    let validator;
    let diagnosticValidators;
    try {
      validator = Compile(request.result_schema);
      diagnosticValidators = schemaDiagnosticViews(request.result_schema as JsonObject)
        .map((schema) => Compile(schema as TSchema));
    } catch {
      throw new AgentFailure("invalid_result_schema", "Cannot compile request.result_schema");
    }
    const rejectSubmission = (args: unknown): boolean => {
      if (Buffer.byteLength(JSON.stringify(args)) > MAX_RESULT_BYTES) {
        submission.reject("submission_invalid", { keyword: "input_size", path: "$" });
        return true;
      }
      if (!validator.Check(args)) {
        const errors = withSchemaErrorBudget(() => {
          const collected = [...validator.Errors(args)];
          if (collected.length < SCHEMA_ERROR_BUDGET) return collected;
          // 重复数组错误不能遮蔽根逻辑或兄弟字段；各视图仍直接由同一冻结 Schema 编译并校验原候选。
          for (const focused of diagnosticValidators) {
            if (collected.length >= SCHEMA_COLLECTED_ERROR_BUDGET) break;
            const remaining = SCHEMA_COLLECTED_ERROR_BUDGET - collected.length;
            collected.push(...focused.Errors(args).slice(0, remaining));
          }
          return collected;
        });
        submission.reject("submission_invalid", safeSchemaIssue(request.result_schema as JsonObject, errors));
        return true;
      }
      return false;
    };
    const submissionMessage = () => JSON.stringify({ code: submission.code, ...(submission.message ? { message: submission.message } : {}), ...submission.diagnostics() });
    lease = await acquireSession(request);
    const artifacts = new ArtifactRegistry(request.cwd, request.session_root, undefined, candidateLimits(request).max_artifacts);
    const candidateSpool = chat ? undefined : new CandidateSpool(request);
    const reportSegments = reportCapability ? new ReportSegments(reportCapability, request.cwd, request.capabilities.candidate_capture!.capture_id, artifacts, ensureActive, (raw) => candidateSpool!.admitRaw(raw)) : undefined;
    const artifactIdentities = () => artifacts.items.map(({ handle, kind, sha256, size_bytes }) => ({ handle, kind, sha256, size_bytes }));
    const durableCapture = async <T>(operation: Promise<T>): Promise<T> => {
      try { return await operation; }
      catch (error) {
        candidateCaptureError = error instanceof AgentFailure ? error : new AgentFailure("candidate_spool_unavailable", "Candidate spool operation failed");
        throw candidateCaptureError;
      }
    };
    const appendOutcome = async (toolCallId: string, outcome: CandidateOutcome) => {
      const attempt = candidateAttempts.get(toolCallId);
      if (!attempt) {
        candidateCaptureError = new AgentFailure("candidate_spool_invalid", "Candidate outcome has no durable attempt");
        throw candidateCaptureError;
      }
      await durableCapture(candidateSpool!.appendOutcome(attempt.producer_candidate_key, outcome));
      candidateOutcomeCalls.add(toolCallId);
    };
    const notifyRemainingTime = () => {
      if (candidate || cancelled) return;
      const remainingSeconds = Math.max(0, Math.floor((deadline - executionNow()) / 1000));
      audit("execution_time_notice", { remaining_seconds: remainingSeconds });
      // 收尾窗口只提示剩余时间，不撤销工具权限、不打断在途调用或延长硬截止。
      session?.agent.steer({ role: "user", content: `Approximately ${remainingSeconds} seconds remain before the original execution deadline. Prepare a truthful submission; authorized tools remain available until then.`, timestamp: Date.now() });
    };
    const bashToolTimeoutSeconds = request.bash_tool_timeout_seconds ?? DEFAULT_BASH_TOOL_TIMEOUT_SECONDS;
    const bashTool = createRequestBashToolDefinition(
      toolCwd,
      bashToolTimeoutSeconds,
      () => Math.max(0, (deadline - executionNow()) / 1000),
      chat ? createChatBashOperations({ ...request.capabilities.project_tools!, forbidden_literals: chatForbidden }, () => currentToolCallId) : request.capabilities.task_execution ? createTaskBashOperations(request.capabilities.task_execution) : {
        async exec() { throw new AgentFailure("execution_binding_missing", "Read-only delivery cannot execute target tools"); },
      },
    );
    const customTools: ToolDefinition[] = chat ? [
      ...createProjectTools({ ...request.capabilities.project_tools!, forbidden_literals: chatForbidden }, ensureActive),
      ...createChatFileTools(toolCwd, { ...request.capabilities.project_tools!, forbidden_literals: chatForbidden }, ensureActive),
      bashTool as unknown as ToolDefinition,
    ] : [
      bashTool as unknown as ToolDefinition,
      {
        name: "submit_result", label: "Submit result",
        description: "Submit the typed business result. Wrapper identity and registered artifacts are added by the runner. This is a local candidate, not Server acceptance. Do not perform more target actions after submitting.",
        parameters: request.result_schema, executionMode: "sequential",
        prepareArguments(args) {
          attempted = true;
          // SDK 默认会做类型转换；先严格校验原始参数，禁止字符串布尔值等被静默修复。
          if (invalidArgumentCalls.has(currentToolCallId) || !submission.repairable() || rejectSubmission(args)) throw new Error(submissionMessage());
          return args as never;
        },
        async execute(id, args, signal) {
          ensureActive();
          const value = structuredClone(args) as JsonObject;
          let artifactStatus: CandidateOutcome["artifact_status"] = "not_evaluated";
          let bindingStatus: CandidateOutcome["binding_status"] = "not_evaluated";
          if (value.accepted !== true && value.accepted !== false) {
            await appendOutcome(id, { code: "result_schema", artifact_status: artifactStatus, binding_status: bindingStatus });
            throw new AgentFailure("result_schema", "accepted must be a boolean");
          }
          let submitted: JsonObject;
          try {
            if (value.accepted === true) {
              if (!object(value.data)) throw new AgentFailure("result_schema", "data must be an object");
              await artifacts.inject(value.data, signal);
              artifactStatus = "validated";
              if (request.binding) {
                if (object(value.data.coverage_result)) for (const key of ["slot_key", "surface_key", "test_family"] as const) if (request.binding[key] !== undefined) value.data.coverage_result[key] = request.binding[key];
                if (object(value.data.verification_result) && request.binding.verification_observation_id !== undefined) value.data.verification_result.observation_id = request.binding.verification_observation_id;
                if (object(value.data.depth_review) && request.binding.review_input_digest !== undefined) value.data.depth_review.input_digest = request.binding.review_input_digest;
                bindingStatus = "injected";
              } else bindingStatus = "not_required";
              submitted = { protocol: "cairn-assessment-v2", accepted: true, kind: PHASE_RESULT_KINDS[request.phase as keyof typeof PHASE_RESULT_KINDS], data: value.data };
              if ("reason" in value) submitted.reason = value.reason;
            } else {
              if (typeof value.reason !== "string" || !value.reason.trim()) throw new AgentFailure("result_schema", "reason must be nonempty");
              bindingStatus = "not_required";
              submitted = { protocol: "cairn-assessment-v2", accepted: false, reason: value.reason };
            }
          } catch (error) {
            const safe = evidenceFailure(error);
            submission.reject(safe.code, undefined, safe.code === "artifact_changed" ? safe.message : undefined);
            await appendOutcome(id, {
              code: safe.code,
              artifact_status: safe.code.startsWith("artifact_") ? "rejected" : artifactStatus,
              binding_status: bindingStatus,
            });
            throw new Error(submissionMessage());
          }
          ensureActive();
          if (Buffer.byteLength(JSON.stringify(submitted)) > MAX_RESULT_BYTES) {
            submission.reject("submission_injected_too_large");
            await appendOutcome(id, { code: "submission_injected_too_large", artifact_status: artifactStatus, binding_status: bindingStatus });
            throw new Error(submissionMessage());
          }
          const digest = hash(submitted);
          if (candidateDigest && candidateDigest !== digest) {
            conflict = true;
            await appendOutcome(id, { code: "submission_conflict", artifact_status: artifactStatus, binding_status: bindingStatus });
            throw new AgentFailure("submission_conflict", "Conflicting results submitted in one request");
          }
          const record = JSON.stringify({ protocol: "cairn-local-candidate-v1", project_id: request.project_id, task_run_id: request.task_run_id, phase: request.phase, submission_id: digest, result: submitted, server_accepted: false });
          const candidatePath = join(lease!.directory, `candidate-${hash([request.task_run_id, request.phase])}-${digest}.json`);
          try { await writeFile(candidatePath, record, { flag: "wx", mode: 0o600 }); }
          catch (error) {
            if ((error as NodeJS.ErrnoException).code !== "EEXIST" || await readFile(candidatePath, "utf8") !== record) {
              providerError = new AgentFailure("runner_error", "Could not preserve local candidate result");
              await appendOutcome(id, { code: "runner_error", artifact_status: artifactStatus, binding_status: bindingStatus });
              throw providerError;
            }
          }
          ensureActive();
          await appendOutcome(id, { code: "locally_validated", count: 1, artifact_status: artifactStatus, binding_status: bindingStatus });
          candidate = submitted;
          candidateDigest = digest;
          audit("submission_validated", { submission_id: digest, server_accepted: false });
          return toolResult({ locally_validated: true, submission_id: digest, server_accepted: false }, true);
        },
      },
      {
        name: "register_artifact", label: "Register artifact",
        description: `Preserve the observed bytes of an existing workspace evidence file as a fixed content version. Returns handle, delivery path, source_path, SHA256 and size. Source changes do not change previous versions. At most ${candidateLimits(request).max_artifacts} versions and 32 MiB total, including Harness output; oversized files are rejected, not truncated. Registration is not upload or Server acceptance.`,
        parameters: { type: "object", properties: { path: { type: "string", minLength: 1 }, kind: { type: "string", enum: [...ARTIFACT_KINDS] } }, required: ["path", "kind"], additionalProperties: false } as TSchema,
        executionMode: "sequential",
        async execute(_id, args, signal) {
          ensureActive();
          const input = args as { path: string; kind: string };
          try {
            const artifact = await artifacts.register(input.path, input.kind, signal);
            audit("artifact_registered", { size_bytes: artifact.size_bytes });
            return toolResult(artifact);
          } catch (error) {
            // 辅助工具失败由工具回执和执行事件保留，不覆盖提交诊断或消耗修正机会。
            throw evidenceFailure(error);
          }
        },
      },
      {
        name: "read_evidence", label: "Read evidence",
        description: "Read one existing local evidence file or Dispatcher-managed read-only input without network or shell. offset is an original UTF-8 byte start; length caps returned text bytes (default/max 32768). Without match, read a UTF-8-safe chunk and use next_offset to continue. Optional match is nonempty exact original text, not a regex: return only that text and its tool-computed pointer if it occurs uniquely at or after offset; missing or repeated matches fail explicitly. Matching scans only the requested file within 32 MiB. For finding evidence_items[].excerpts, copy the returned utf8-bytes=START:END pointer with the appropriate side; do not calculate UTF-8 boundaries. Read registered delivery paths for registered evidence. Select relevant material on demand, including tails and counterevidence; do not load all attachments or mechanically select their first N bytes.",
        parameters: { type: "object", properties: { path: { type: "string", minLength: 1 }, offset: { type: "integer", minimum: 0 }, length: { type: "integer", minimum: 1, maximum: 32768 }, match: { type: "string", minLength: 1 } }, required: ["path"], additionalProperties: false } as TSchema,
        executionMode: "sequential",
        async execute(_id, args) {
          ensureActive();
          const input = args as { path: string; offset?: number; length?: number; match?: string };
          try {
            const path = await evidencePath(request.cwd, input.path, request.session_root);
            return toolResult(await readEvidence(path, input));
          } catch (error) {
            // 辅助工具失败由工具回执和执行事件保留，不覆盖提交诊断或消耗修正机会。
            throw evidenceFailure(error);
          }
        },
      },
    ];
    if (trafficCapability) for (const [name, parameters, description] of [
      ["list_http_flows", LIST_HTTP_FLOWS_PARAMETERS, "List bounded HTTP flow summaries automatically captured for this project and current TaskRun (default limit 20, max 50). Follow next_cursor while has_more. No headers or bodies are returned. historical=true explicitly permits same-project existing material, optionally narrowed by task_run_id. Flows are not automatically assigned to the most recent tool call."],
      ["read_http_flow", READ_HTTP_FLOW_PARAMETERS, "Read one captured flow without sending a target request. Default side=metadata returns source and sync state; request/response returns the Server's http-evidence-v1 structured reconstruction (not original wire bytes), bounded to 32768 UTF-8 bytes, with absolute offset, next_offset, revision and SHA256. Receiving flows return only an explicitly marked body-preview-base64 prefix (body byte offsets, excerpt_eligible=false), never a stable HTTP projection or excerpt pointer. Continue only as needed. historical=true is required for another TaskRun in this project. Preserve missing sides and receiving/partial/failed states. Cite flow_id in an HTTP evidence item, source=capture for this run or historical for earlier runs; do not copy packets to Artifacts. For excerpts use returned side/pointer/representation/sha256/revision; binary/compressed display placeholders are not evidence text."],
    ] as const) customTools.push({
      name, label: name, description, parameters, executionMode: "sequential",
      async execute(_id, args, signal) {
        ensureActive();
        return toolResult(await readHttpTraffic(trafficCapability, name, args as JsonObject, signal));
      },
    });
    if (renderTools.length) {
      const renderer = new HttpEvidenceRenderer(trafficCapability, request.capabilities.http_evidence_render, request.capabilities.http_evidence_render ? "/tmp/cairn-prompts" : lease.directory);
      customTools.push({
        name: "render_http_evidence", label: "Render HTTP evidence",
        description: "Optionally render one existing HTTP interaction offline as PNG. Server source-delivery approval is required before rendering. Ordinary tasks pass source identity and verified excerpts (historical=true for earlier runs); report tasks pass only source_key from frozen http_evidence_render_sources. No URL, HTML, arbitrary file path or new target request is accepted. Images are optional presentation derivatives, never current-run evidence: do not put them in evidence_items, artifact declarations or the final required artifact set. Missing screenshots never prevent submitting valid text and original evidence. Rendering and publication failures return local status; do not repeat target requests to obtain an image.",
        parameters: RENDER_HTTP_EVIDENCE_PARAMETERS, executionMode: "sequential",
        async execute(_id, args, signal) {
          ensureActive();
          const rendered = await renderer.render(args as JsonObject, signal);
          audit("http_evidence_rendered", { status: rendered.status, optional: true });
          return toolResult(rendered);
        },
      });
    }
    if (reportSegments) customTools.push({
      name: "save_report_segment", label: "Save report segment",
      description: "Durably save one bounded structural report content unit against the frozen binding. key is idempotent within this execution: identical content returns its existing handle, different content is rejected. Follow the supplied content schema. For Markdown writing, follow the selected example's structure, style and length, preserving explicit frozen source identities. A short report may be saved whole; split long text at coherent boundaries as one continuous article, never as standalone reports. Evidence insertion belongs to verified source references. Never truncate content. At most 256 artifacts and 32 MiB total; each segment at most 1 MiB. Saved segments are local material, not accepted reports. Returns only artifact_handle and key. Final submit_result must reference saved handles, never resend their body.",
      parameters: reportSegments.parameters, executionMode: "sequential",
      prepareArguments(args) {
        if (invalidArgumentCalls.has(currentToolCallId) || truncatedReportUnit) throw new AgentFailure("report_segment_invalid", "Report segment requires complete strict JSON from a completed tool response");
        reportSegments.validate(args);
        return args as never;
      },
      async execute(id, args) {
        const saved = await reportSegments.save(args);
        savedSegmentCalls.set(id, saved);
        audit("report_segment_saved", { artifact_handle: saved.artifact_handle, saved_segments: reportSegments.references().length, server_accepted: false });
        ensureActive();
        return toolResult(saved);
      },
    });
    if (checkpointCapability && executionTools.includes("record_checkpoint")) customTools.push(createCheckpointTool(checkpointCapability, request.phase, ensureActive));
    const authStorage = AuthStorage.inMemory();
    authStorage.setRuntimeApiKey(REQUEST_PROVIDER, SDK_AUTH_PLACEHOLDER);
    const registry = ModelRegistry.inMemory(authStorage);
    let instructions = "";
    try { if (!chat) instructions = await readFile("/opt/cairn/AGENTS.md", "utf8"); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
    // 在加载点记录实际使用版本的内容身份，不读取宿主版本，也不向审计复制指引正文。
    if (instructions) audit("instructions_loaded", {
      prompt_name: "AGENTS.md", prompt_hash: createHash("sha256").update(instructions).digest("hex"),
      prompt_bytes: Buffer.byteLength(instructions),
    });
    const systemPrompt = chat ? CHAT_SYSTEM_PROMPT : `${instructions}\nYou are Cairn's pi agent. Follow the authorized task scope. Use submit_result for the final business result; prose/JSON text is never a submission. HTTP is automatically recorded. Query and read actual flows on demand, then explicitly cite flow_id in evidence_items without duplicate request/response Artifacts or inline packets; never fabricate missing captures. Preserve item IDs and actual source (capture for this TaskRun, historical for same-project prior material). Register other evidence files before using their handles. Finding fact.description states necessary preconditions and verified impact; vulnerability.reproduction_steps records the complete ordered actions, parameter sources, dependencies, observations and original item IDs via evidence_item_ids; vulnerability.remediation addresses the actual defect. No fixed word count, step count or HTTP-per-step requirement applies. Excerpts are optional. For flow excerpts copy read_http_flow side/pointer/representation/sha256/revision from the exact projection; for historical Artifact excerpts use read_evidence side/pointer only. Preserve independent fragments separately; never substitute display coordinates or copy evidence text. Reports consume only their frozen Packet, not live flows. External verification reasons and test steps reference those original item IDs via evidence_item_ids, not artifact handles. Use the same items, not separately authored evidence_refs or inline HTTP text. Do not write wrapper protocol, kind, project or task identity. Explicit delivery-only requests may only read existing evidence, register it and submit; normal execution keeps its authorized tools during result correction. After a valid submission, do not perform further target actions. Local submission is not Server acceptance.`;
    const created = await createAgentSession({
      cwd: request.cwd, agentDir: lease.directory, authStorage, modelRegistry: registry, model,
      thinkingLevel: "off", sessionManager: lease.manager,
      settingsManager: SettingsManager.inMemory({
        // SDK 使用墙钟 timer；执行截止由上面的可扣暂停时钟通过 AbortSignal 控制。
        // 传输 timer 取 Node 上限，避免恢复时旧预算 timer 先触发并重发同一请求。
        retry: { enabled: false, provider: { maxRetries: 1, timeoutMs: 2_147_483_647, maxRetryDelayMs: 1000 } },
        compaction: { enabled: false }, enableSkillCommands: false, enableInstallTelemetry: false,
      }),
      resourceLoader: loader(systemPrompt), tools: limitedTools ? safeTools : executionTools, customTools,
    });
    session = created.session;
    const meteredStream = session.agent.streamFn;
    session.agent.streamFn = (streamModel, context, streamOptions) => {
      // 先记可观察的 SDK 调用开始；内部网络重试不冒充可完整计量的响应。
      consumption?.begin();
      return meteredStream(streamModel, context, streamOptions);
    };
    if (reportSegments) {
      const originalStream = session.agent.streamFn;
      // 报告显式传递实际输出预算；普通探索保留原 SDK 选值，避免静默扩大原有费用范围。
      session.agent.streamFn = (streamModel, context, streamOptions) => originalStream(streamModel, context, { ...streamOptions, maxTokens: request.model.max_output_tokens });
    }
    if (!chat && request.model.protocol === "anthropic-messages") preserveAnthropicSubmissionSchema(session.agent, request.result_schema, reportSegments?.parameters);
    if (reportSegments) {
      const originalTransform = session.agent.transformContext;
      session.agent.transformContext = async (messages, signal) => {
        const original = await originalTransform?.(messages, signal) ?? messages;
        // 仅投影模型工作上下文；原始 SDK transcript 不变，保存正文与已拒绝半参数不重新回灌。
        const omittedCall = (id: string) => savedSegmentCalls.has(id) || rejectedReportCalls.has(id);
        const projected: typeof original = [];
        for (const message of original) {
          if (message.role === "toolResult" && omittedCall(message.toolCallId)) continue;
          if (message.role !== "assistant") { projected.push(message); continue; }
          const content = message.content.filter((block) => block.type !== "toolCall" || !omittedCall(block.id));
          if (content.length) projected.push({ ...message, content });
        }
        const refs = reportSegments.references();
        if (refs.length) projected.push({ role: "user", content: `Durably saved report segments (local material, not Server acceptance): ${JSON.stringify(refs)}\n这些是同一篇报告的已保存部分，不是独立小报告。用 read_evidence 按 path 回读必要的已保存正文，核对整篇结构、章节顺序和重复内容；继续时承接前文，不重新起标题或下结论。不要重发未修改的正文；需修订时用新 key 保存，旧段保留。最终 segment_refs 只选择成品版本的 artifact_handle 并按正文顺序提交，不携带 path 或 key。`, timestamp: Date.now() });
        return projected;
      };
    }
    session.agent.toolExecution = "sequential";
    const originalBefore = session.agent.beforeToolCall;
    session.agent.beforeToolCall = async (context, signal) => {
      ensureActive();
      if (candidate && context.toolCall.name !== "submit_result") {
        // SDK 会继续遍历同一响应的工具；固定候选后的尾随调用只跳过，不污染已完成的提交。
        audit("submission_tail_skipped", { tool_call_id: context.toolCall.id });
        return { block: true, reason: "A valid candidate is fixed; execution has ended" };
      }
      if (candidateCaptureError || providerError || invalidArgumentCalls.has(context.toolCall.id) || !submission.repairable()) {
        if (context.toolCall.name === "submit_result" && candidateAttempts.has(context.toolCall.id) && !candidateOutcomeCalls.has(context.toolCall.id)) {
          await appendOutcome(context.toolCall.id, { code: candidateCaptureError?.code ?? providerError?.code ?? submission.code, count: 1, artifact_status: "not_evaluated", binding_status: "not_evaluated" });
        }
        return { block: true, reason: submissionMessage() };
      }
      if (limitedTools && !safeTools.includes(context.toolCall.name)) {
        submission.reject("tool_permission");
        attempted = true;
        return { block: true, reason: "conclude_only: target actions are disabled" };
      }
      const inherited = await originalBefore?.(context, signal);
      if (!inherited?.block) authorizedToolCalls.add(context.toolCall.id);
      return inherited;
    };
    const originalAfter = session.agent.afterToolCall;
    session.agent.afterToolCall = async (context, signal) => {
      const inherited = await originalAfter?.(context, signal);
      const details = context.result.details;
      if (context.toolCall.name === "bash" && object(details) && typeof details.fullOutputPath === "string") {
        const source = details.fullOutputPath;
        if (join(tmpdir(), basename(source)) !== source || !/^pi-bash-[0-9a-f]{16}\.log$/.test(basename(source))) throw new AgentFailure("artifact_invalid", "Unexpected SDK output spill path");
        if (chat) {
          const path = join(toolCwd, basename(source));
          await copyFile(source, path, constants.COPYFILE_EXCL);
          await unlink(source);
          return { ...inherited, content: [...(inherited?.content ?? context.result.content), { type: "text", text: `完整命令输出已保存到当前工作区：${path}` }], details: { ...details, fullOutputPath: path } };
        }
        // SDK溢出输出转入工作区并登记，收尾阶段无需bash搬运才能读取已有证据。
        const directory = join(request.cwd, ".cairn", "artifacts", request.session.slice(3));
        await mkdir(directory, { recursive: true, mode: 0o700 });
        const path = join(directory, basename(source));
        await copyFile(source, path, constants.COPYFILE_EXCL);
        const artifact = await artifacts.register(path, "raw_output", signal);
        await unlink(source);
        audit("artifact_registered", { size_bytes: artifact.size_bytes, source: "sdk_bash_overflow" });
        return { ...inherited, content: [...(inherited?.content ?? context.result.content), { type: "text", text: `Preserved full output: ${JSON.stringify(artifact)}` }], details: { ...details, fullOutputPath: path } };
      }
      return inherited;
    };
    // 订阅 Agent 内核事件；该订阅会被 SDK await，候选 fsync 完成后才可进入参数准备和工具执行。
    session.agent.subscribe(async (event) => {
      // 用量必须先于候选／正文校验落盘；error、aborted 也保留已知部分。
      if (event.type === "message_update" && event.message.role === "assistant") consumption?.observe(event.message.usage);
      if (event.type === "message_end" && event.message.role === "assistant") {
        consumption?.observe(event.message.usage, true);
        usage = recordUsage(usage, event.message.usage);
      }
      if (event.type === "message_start" && event.message.role === "assistant") {
        assistantTurn += 1;
        outputLimitReached = false;
        truncatedReportUnit = false;
        rawArguments.clear();
        oversizedArgumentIndexes.clear();
        invalidArgumentCalls.clear();
      } else if (event.type === "message_update" && event.assistantMessageEvent.type === "toolcall_delta") {
        const delta = event.assistantMessageEvent;
        if (!oversizedArgumentIndexes.has(delta.contentIndex)) {
          const next = (rawArguments.get(delta.contentIndex) ?? "") + delta.delta;
          if (Buffer.byteLength(next) > (request.capabilities.candidate_capture?.max_candidate_bytes ?? MAX_RESULT_BYTES)) {
            rawArguments.delete(delta.contentIndex);
            oversizedArgumentIndexes.add(delta.contentIndex);
          } else rawArguments.set(delta.contentIndex, next);
        }
      } else if (event.type === "tool_execution_start") {
        currentToolCallId = event.toolCallId;
        if (event.toolName === "submit_result") attempted = true;
        audit(event.type, { tool_name: executionTools.includes(event.toolName) ? event.toolName : "unknown", tool_call_id: event.toolCallId, input_bytes: Buffer.byteLength(JSON.stringify(event.args)) });
      } else if (event.type === "tool_execution_end") {
        // SDK 先 await message_end 再准备／执行工具，但 length 本身不停止 batch。
        // 已阻断调用的结果使用 SDK terminate 标记，避免 abort 后额外的 aborted 消息覆盖恢复依据。
        if (reportSegments && providerError?.code === "model_output_limit" && object(event.result)) event.result.terminate = true;
        const authorizedAtEntry = authorizedToolCalls.delete(event.toolCallId);
        if (!candidate && !authorizedAtEntry && limitedTools && !safeTools.includes(event.toolName)) {
          attempted = true;
          submission.reject("tool_permission");
        }
        audit(event.type, { tool_name: executionTools.includes(event.toolName) ? event.toolName : "unknown", tool_call_id: event.toolCallId, is_error: event.isError, output_bytes: Buffer.byteLength(JSON.stringify(event.result)) });
      } else if (event.type === "message_end" && event.message.role === "assistant") {
        const message = event.message;
        outputLimitReached = message.stopReason === "length";
        const toolCalls = message.content.filter((block) => block.type === "toolCall");
        if (chat) {
          // 只接受自然终止的最后一条公开 text；工具前的旁白和 thinking 不进入用户消息。
          finalText = message.stopReason === "stop" && toolCalls.length === 0
            ? message.content.filter((block) => block.type === "text").map((block) => block.text).join("\n")
            : undefined;
          if (message.stopReason === "aborted" && !cancelled) providerError = new AgentFailure("provider_error", "Provider response was interrupted");
        }
        truncatedReportUnit = !!reportSegments && outputLimitReached && toolCalls.every((block) => ["save_report_segment", "submit_result"].includes(block.name));
        if (outputLimitReached && (reportSegments || toolCalls.length > 0 || rawArguments.size > 0 || oversizedArgumentIndexes.size > 0)) {
          providerError = new AgentFailure("model_output_limit", "Model output reached its token limit; the current content unit is incomplete");
        }
        // 部分参数即使未成为 toolCall 也不能走正常执行续行，避免把截断工具当成可重放动作。
        truncatedWithoutToolCalls = message.stopReason === "length"
          && !message.content.some((block) => block.type === "toolCall")
          && rawArguments.size === 0 && oversizedArgumentIndexes.size === 0;
        for (const [index, block] of message.content.entries()) {
          if (block.type !== "toolCall") continue;
          const rawText = rawArguments.get(index) ?? "";
          if (oversizedArgumentIndexes.has(index)) invalidArgumentCalls.add(block.id);
          else {
            try {
              if (canonicalJson(JSON.parse(rawText)) !== canonicalJson(block.arguments)) invalidArgumentCalls.add(block.id);
            } catch { invalidArgumentCalls.add(block.id); }
          }
          if (reportSegments && ["save_report_segment", "submit_result"].includes(block.name) && (invalidArgumentCalls.has(block.id) || outputLimitReached)) rejectedReportCalls.add(block.id);
          if (block.name === "save_report_segment" && (invalidArgumentCalls.has(block.id) || outputLimitReached)) {
            audit("report_segment_rejected", { code: outputLimitReached ? "model_output_limit" : "report_segment_invalid", keyword: invalidArgumentCalls.has(block.id) ? "strict_json" : "stop_reason", raw_sha256: createHash("sha256").update(rawText).digest("hex"), raw_size_bytes: Buffer.byteLength(rawText) });
            if (!outputLimitReached) { attempted = true; submission.reject("submission_invalid", { keyword: "strict_json", path: "$" }); }
          }
          if (!chat && block.name === "submit_result") {
            submission.attempts += 1;
            attempted = true;
            if (oversizedArgumentIndexes.has(index)) {
              candidateCaptureError = new AgentFailure("candidate_spool_limit", "Candidate raw bytes exceed the byte limit");
              throw candidateCaptureError;
            }
            const attempt = await durableCapture(candidateSpool!.appendCandidate(assistantTurn, index, Buffer.from(rawText, "utf8"), artifactIdentities()));
            candidateAttempts.set(block.id, attempt);
            audit("candidate_spooled", { producer_candidate_key: attempt.producer_candidate_key, raw_sha256: attempt.raw_sha256, raw_size_bytes: attempt.raw_size_bytes });
            if (submission.repairable()) {
              if (Buffer.byteLength(rawText) > MAX_RESULT_BYTES) {
                submission.reject("submission_invalid", { keyword: "input_size", path: "$" });
                await appendOutcome(block.id, { code: "submission_invalid", path: "$", keyword: "input_size", count: 1, artifact_status: "not_evaluated", binding_status: "not_evaluated" });
              } else if (invalidArgumentCalls.has(block.id)) {
                submission.reject("submission_invalid", { keyword: "strict_json", path: "$" });
                await appendOutcome(block.id, { code: "submission_invalid", path: "$", keyword: "strict_json", count: 1, artifact_status: "not_evaluated", binding_status: "not_evaluated" });
              } else if (rejectSubmission(block.arguments)) {
                const issue = submission.issue;
                await appendOutcome(block.id, { code: "submission_invalid", path: issue?.path, keyword: issue?.keyword, count: 1, artifact_status: "not_evaluated", binding_status: "not_evaluated" });
              }
            } else {
              await appendOutcome(block.id, { code: submission.code, count: 1, artifact_status: "not_evaluated", binding_status: "not_evaluated" });
            }
          }
          if (!["toolUse", "error", "aborted", "length"].includes(message.stopReason)) providerError = new AgentFailure("provider_error", "Provider tool stream did not finish normally");
        }
        if (outputLimitReached) audit("model_output_limit", { stop_reason: "length", max_output_tokens: request.model.max_output_tokens, saved_segments: reportSegments?.references().length ?? 0 });
        if (message.stopReason === "error") providerError = providerFailure(message.errorMessage ?? "");
      } else if (event.type === "turn_end" && (attempted || providerError || candidateCaptureError)) {
        // 报告 length 的阻断 batch 已用 terminate 收束；不生成多余的 aborted 响应。
        if (reportSegments && providerError?.code === "model_output_limit") return;
        // 提交及不完整工具流不能进入 SDK 自动续轮；结果修正只走统一有界入口。
        session?.agent.abort();
      }
    });
    if (concludeTimeoutSeconds > 0) {
      if (remainingTimeNoticeAt <= executionNow()) notifyRemainingTime();
      else remainingTimeTimer = setInterval(() => {
        if (executionNow() < remainingTimeNoticeAt) return;
        clearInterval(remainingTimeTimer);
        notifyRemainingTime();
      }, 100);
    }
    ensureActive();
    audit("session_start", { resume: request.resume, conclude_only: readonly, max_output_tokens: request.model.max_output_tokens });
    const promptWithReportRecovery = async (prompt: string) => {
      await session!.prompt(prompt, { expandPromptTemplates: false });
      while (!candidate && truncatedReportUnit && providerError?.code === "model_output_limit" && !candidateCaptureError && !conflict && reportOutputRecoveries < 2) {
        ensureActive();
        reportOutputRecoveries += 1;
        audit("report_output_recovery", { recovery_round: reportOutputRecoveries, max_recovery_rounds: 2, saved_segments: reportSegments!.references().length, remaining_execution_seconds: Math.max(0, (deadline - executionNow()) / 1000) });
        providerError = undefined;
        attempted = false;
        await session!.prompt("The current unfinished report content unit reached the model output limit and was NOT saved or accepted. Recover only that unfinished unit: make it structurally smaller (a shorter coherent Markdown block per save_report_segment call). Do not repeat or overwrite already saved segment keys or their content. Do not resend the whole report or increase the output budget. Final submit_result contains only schema_version 5 and segment_refs. The original deadline and read-only authorization still apply.", { expandPromptTemplates: false });
      }
    };
    await promptWithReportRecovery(request.prompt);
    while (truncatedWithoutToolCalls && !limitedTools && !attempted && !candidate
      && !candidateCaptureError && !providerError && !conflict) {
      ensureActive();
      if (outputContinuations >= MAX_OUTPUT_CONTINUATIONS) {
        audit("output_truncation_limit", { stop_reason: "length", output_continuations: outputContinuations });
        throw new AgentFailure("model_output_limit", `Model output truncation continuation limit reached after ${outputContinuations} continuations without a submission`);
      }
      outputContinuations += 1;
      audit("output_continuation", {
        stop_reason: "length", output_continuations: outputContinuations,
        remaining_execution_seconds: Math.max(0, (deadline - executionNow()) / 1000),
      });
      ensureActive();
      await session.prompt(OUTPUT_CONTINUATION_PROMPT, { expandPromptTemplates: false });
    }
    while (!chat && !candidate && !candidateCaptureError && !providerError && !conflict && submission.repairable() && submission.repairs < request.max_result_repairs) {
      ensureActive();
      submission.repairs += 1;
      attempted = false;
      audit("result_repair", { code: submission.code, ...submission.diagnostics() });
      await promptWithReportRecovery(`Your previous response was not accepted locally: ${submissionMessage()}. Correct the result in this session and use submit_result; do not replay completed actions. ${readonly ? "Only read existing evidence, save report segments when available, register artifacts and submit; target actions remain disabled." : "Authorized tools remain available within the original deadline."}`);
    }
    ensureActive();
    if (candidateCaptureError) throw candidateCaptureError;
    if (conflict) throw new AgentFailure("submission_conflict", "Conflicting local submissions");
    if (providerError) throw providerError;
    if (!candidate && outputLimitReached) throw new AgentFailure("model_output_limit", "Model output reached its token limit without a complete submission");
    if (chat) {
      if (finalText === undefined || !finalText.trim()) throw new AgentFailure("chat_response_missing", "Chat did not complete a public answer");
      assertPublicText(finalText, chatForbidden);
      result.status = "succeeded";
      result.result = { text: finalText };
      return result;
    }
    if (!candidate || !submission.repairable()) throw new AgentFailure(submission.code, "Local result submission did not complete");
    result.status = "succeeded";
    result.result = candidate;
    result.submission_id = candidateDigest;
  } catch (error) {
    result.status = cancelled ? "cancelled" : "failed";
    result.error = cancelled ? { code: cancelled, message: cancelled === "cancelled" ? "Request cancelled" : "Request deadline exceeded" } : failure(error);
    result.error = { ...result.error, ...submission.diagnostics() };
    audit("agent_error", { error: result.error, candidate_submission_id: candidateDigest });
  } finally {
    if (session) { await session.abort(); session.dispose(); }
    try {
      const initialized = await lease?.finish();
      if (chat) result.session_initialized = initialized ?? null;
    }
    catch { result.status = "failed"; result.error = { code: "session_corrupt", message: "Could not finalize session transcript" }; }
    if (options.signal?.aborted) stop("cancelled");
    if (executionNow() >= deadline) stop("deadline_exceeded");
    if (cancelled) { result.status = "cancelled"; result.error = { code: cancelled, message: "Request stopped before completion" }; }
    if (result.status !== "succeeded") { result.result = null; result.submission_id = null; }
    Object.assign(result, usageResult(usage, chat));
    if (result.error) result.error = { ...result.error, ...submission.diagnostics() };
    audit("session_end", { status: result.status, ...submission.diagnostics(), submission_id: result.submission_id });
    clearInterval(timer);
    if (remainingTimeTimer) clearInterval(remainingTimeTimer);
    options.signal?.removeEventListener("abort", onAbort);
  }
  return result;
}
