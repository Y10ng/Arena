import { createHash } from "node:crypto";
import { constants } from "node:fs";
import { copyFile, mkdir, open, readFile, unlink, writeFile } from "node:fs/promises";
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
  createLocalBashOperations,
  ModelRegistry,
  SettingsManager,
  type AgentSession,
  type BashOperations,
  type ResourceLoader,
  type ToolDefinition,
} from "@mariozechner/pi-coding-agent";
import { preserveAnthropicSubmissionSchema } from "./anthropic-schema.js";
import { ArtifactRegistry, ARTIFACT_KINDS, evidencePath, evidenceFailure } from "./artifacts.js";
import { createCheckpointTool } from "./checkpoints.js";
import { captureHttpEvidence, HTTP_EVIDENCE_PARAMETERS, type HttpEvidenceInput } from "./http-evidence.js";
import { CandidateSpool, type CandidateAttempt, type CandidateOutcome } from "./candidate-spool.js";
import { SubmissionState, safeSchemaIssue } from "./submission.js";
import { AgentFailure, envelope, object, PHASE_RESULT_KINDS, type AgentEnvelope, type AgentRequest, type JsonObject } from "./protocol.js";
import { acquireSession, type SessionLease } from "./session.js";
import { requestModel, REQUEST_PROVIDER, SDK_AUTH_PLACEHOLDER } from "./model.js";

const SAFE_TOOLS = ["read_evidence", "register_artifact", "submit_result"];
const BASE_EXECUTION_TOOLS = ["read", "bash", "edit", "write", "grep", "find", "ls", "capture_http_evidence", ...SAFE_TOOLS];
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

export function createRequestBashToolDefinition(
  cwd: string,
  maxTimeoutSeconds: number,
  remainingSeconds: () => number,
  operations: BashOperations = createLocalBashOperations(),
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
  const deadline = performance.now() + request.timeout_seconds * 1000;
  const readonly = request.conclude_only || request.phase.endsWith("_conclude") || request.phase === "report_execute";
  const concludeTimeoutSeconds = readonly ? 0 : (request.conclude_timeout_seconds ?? 0);
  const remainingTimeNoticeAt = deadline - Math.min(request.timeout_seconds, concludeTimeoutSeconds) * 1000;
  const safeTools = SAFE_TOOLS;
  let session: AgentSession | undefined;
  let lease: SessionLease | undefined;
  let cancelled: "cancelled" | "deadline_exceeded" | undefined;
  const checkpointCapability = request.capabilities?.execution_checkpoints;
  const executionTools = checkpointCapability && !readonly ? [...BASE_EXECUTION_TOOLS, "record_checkpoint"] : BASE_EXECUTION_TOOLS;
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
  let truncatedWithoutToolCalls = false;
  let currentToolCallId = "";
  const submission = new SubmissionState(request.max_result_repairs);
  let candidateCaptureError: AgentFailure | undefined;
  let providerError: AgentFailure | undefined;
  let usageKnown = true;
  let usageSeen = false;
  const usage = { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0 };
  const audit = (event: string, extra: JsonObject = {}) => {
    // 调用处只生成固定码、声明路径和计数；不复制原参数，也不事后替换而改写诊断事实。
    options.audit?.({ protocol: "cairn-agent-audit-v1", timestamp: new Date().toISOString(), project_id: request.project_id, task_run_id: request.task_run_id, phase: request.phase, session: request.session, event, ...extra });
  };
  const stop = (reason: "cancelled" | "deadline_exceeded") => {
    cancelled ??= reason;
    session?.agent.abort();
  };
  const onAbort = () => stop("cancelled");
  options.signal?.addEventListener("abort", onAbort, { once: true });
  const timer = setTimeout(() => stop("deadline_exceeded"), request.timeout_seconds * 1000);
  let remainingTimeTimer: NodeJS.Timeout | undefined;
  const ensureActive = () => {
    if (options.signal?.aborted) stop("cancelled");
    if (performance.now() >= deadline) stop("deadline_exceeded");
    if (cancelled) throw new AgentFailure(cancelled, cancelled === "cancelled" ? "Request cancelled" : "Request deadline exceeded");
  };
  try {
    ensureActive();
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
    const artifacts = new ArtifactRegistry(request.cwd, request.session_root);
    const candidateSpool = new CandidateSpool(request);
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
      await durableCapture(candidateSpool.appendOutcome(attempt.producer_candidate_key, outcome));
      candidateOutcomeCalls.add(toolCallId);
    };
    const notifyRemainingTime = () => {
      if (candidate || cancelled) return;
      const remainingSeconds = Math.max(0, Math.floor((deadline - performance.now()) / 1000));
      audit("execution_time_notice", { remaining_seconds: remainingSeconds });
      // 收尾窗口只提示剩余时间，不撤销工具权限、不打断在途调用或延长硬截止。
      session?.agent.steer({ role: "user", content: `Approximately ${remainingSeconds} seconds remain before the original execution deadline. Prepare a truthful submission; authorized tools remain available until then.`, timestamp: Date.now() });
    };
    const bashToolTimeoutSeconds = request.bash_tool_timeout_seconds ?? DEFAULT_BASH_TOOL_TIMEOUT_SECONDS;
    const bashTool = createRequestBashToolDefinition(
      request.cwd,
      bashToolTimeoutSeconds,
      () => Math.max(0, (deadline - performance.now()) / 1000),
    );
    const customTools: ToolDefinition[] = [
      bashTool as unknown as ToolDefinition,
      {
        name: "capture_http_evidence", label: "Capture HTTP evidence",
        description: "Actually send one HTTP/1.1 request using curl and register the captured request/response bytes plus trace and capture status. Returns evidence_items ready for finding or external verification submission. These are application-layer/tool bytes, not network frames. Redirects are NOT followed; capture each authorized Location separately. Partial responses and missing sides remain truthful on timeout/failure. Optional body_path reads an existing authorized evidence file. No response body is copied into the tool reply. Request body limit 1 MiB, response limit 4 MiB; larger transfers stop and retain actual partial evidence. Use other existing tools for larger or specialized captures.",
        parameters: HTTP_EVIDENCE_PARAMETERS, executionMode: "sequential",
        async execute(_id, args, signal) {
          ensureActive();
          try {
            const capture = await captureHttpEvidence(args as HttpEvidenceInput, {
              cwd: request.cwd, sessionRoot: request.session_root, artifacts,
              remainingSeconds: () => Math.max(0, (deadline - performance.now()) / 1000), signal,
            });
            audit("http_evidence_captured", { exit_code: capture.exit_code, interrupted: capture.interrupted, artifact_count: capture.artifacts.length });
            return toolResult(capture);
          } catch (error) { throw evidenceFailure(error); }
        },
      },
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
              submitted = { protocol: "cairn-assessment-v2", accepted: true, kind: PHASE_RESULT_KINDS[request.phase], data: value.data };
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
        description: "Preserve the observed bytes of an existing workspace evidence file as a fixed content version. Returns handle, delivery path, source_path, SHA256 and size. Source changes do not change previous versions. At most 32 versions and 32 MiB total, including Harness output; oversized files are rejected, not truncated. Registration is not upload or Server acceptance.",
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
        description: "Read existing local workspace evidence or Dispatcher-managed read-only input without network access or shell execution. Output is bounded to 32768 bytes; use offset to continue.",
        parameters: { type: "object", properties: { path: { type: "string", minLength: 1 }, offset: { type: "integer", minimum: 0 } }, required: ["path"], additionalProperties: false } as TSchema,
        executionMode: "sequential",
        async execute(_id, args) {
          ensureActive();
          const input = args as { path: string; offset?: number };
          try {
            const path = await evidencePath(request.cwd, input.path, request.session_root);
            const file = await open(path, "r");
            try {
              const buffer = Buffer.alloc(32768);
              const { bytesRead } = await file.read(buffer, 0, buffer.length, input.offset ?? 0);
              return toolResult({ text: buffer.subarray(0, bytesRead).toString("utf8"), bytes_read: bytesRead, next_offset: (input.offset ?? 0) + bytesRead });
            } finally { await file.close(); }
          } catch (error) {
            // 辅助工具失败由工具回执和执行事件保留，不覆盖提交诊断或消耗修正机会。
            throw evidenceFailure(error);
          }
        },
      },
    ];
    if (checkpointCapability && executionTools.includes("record_checkpoint")) customTools.push(createCheckpointTool(checkpointCapability, request.phase, ensureActive));
    const authStorage = AuthStorage.inMemory();
    authStorage.setRuntimeApiKey(REQUEST_PROVIDER, SDK_AUTH_PLACEHOLDER);
    const registry = ModelRegistry.inMemory(authStorage);
    let instructions = "";
    try { instructions = await readFile("/opt/cairn/AGENTS.md", "utf8"); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
    // 在加载点记录实际使用版本的内容身份，不读取宿主版本，也不向审计复制指引正文。
    if (instructions) audit("instructions_loaded", {
      prompt_name: "AGENTS.md", prompt_hash: createHash("sha256").update(instructions).digest("hex"),
      prompt_bytes: Buffer.byteLength(instructions),
    });
    const systemPrompt = `${instructions}\nYou are Cairn's pi agent. Follow the authorized task scope. Use submit_result for the final business result; prose/JSON text is never a submission. Register evidence files before using their handles; capture_http_evidence already registers its actual captures and returns evidence_items ready for schema_version 2 findings or schema_version 3 external verification. External verification reasons and test steps reference those original item IDs via evidence_item_ids, not artifact handles. Use the same items, not separately authored evidence_refs or inline HTTP text. Do not write wrapper protocol, kind, project or task identity. Explicit delivery-only requests may only read existing evidence, register it and submit; normal execution keeps its authorized tools during result correction. After a valid submission, do not perform further target actions. Local submission is not Server acceptance.`;
    const created = await createAgentSession({
      cwd: request.cwd, agentDir: lease.directory, authStorage, modelRegistry: registry, model,
      thinkingLevel: "off", sessionManager: lease.manager,
      settingsManager: SettingsManager.inMemory({
        retry: { enabled: false, provider: { maxRetries: 1, timeoutMs: Math.ceil(request.timeout_seconds * 1000), maxRetryDelayMs: 1000 } },
        compaction: { enabled: false }, enableSkillCommands: false, enableInstallTelemetry: false,
      }),
      resourceLoader: loader(systemPrompt), tools: readonly ? safeTools : executionTools, customTools,
    });
    session = created.session;
    if (request.model.protocol === "anthropic-messages") preserveAnthropicSubmissionSchema(session.agent, request.result_schema);
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
      if (readonly && !safeTools.includes(context.toolCall.name)) {
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
      if (event.type === "message_start" && event.message.role === "assistant") {
        assistantTurn += 1;
        rawArguments.clear();
        oversizedArgumentIndexes.clear();
        invalidArgumentCalls.clear();
      } else if (event.type === "message_update" && event.assistantMessageEvent.type === "toolcall_delta") {
        const delta = event.assistantMessageEvent;
        if (!oversizedArgumentIndexes.has(delta.contentIndex)) {
          const next = (rawArguments.get(delta.contentIndex) ?? "") + delta.delta;
          if (Buffer.byteLength(next) > request.capabilities.candidate_capture.max_candidate_bytes) {
            rawArguments.delete(delta.contentIndex);
            oversizedArgumentIndexes.add(delta.contentIndex);
          } else rawArguments.set(delta.contentIndex, next);
        }
      } else if (event.type === "tool_execution_start") {
        currentToolCallId = event.toolCallId;
        if (event.toolName === "submit_result") attempted = true;
        audit(event.type, { tool_name: executionTools.includes(event.toolName) ? event.toolName : "unknown", tool_call_id: event.toolCallId, input_bytes: Buffer.byteLength(JSON.stringify(event.args)) });
      } else if (event.type === "tool_execution_end") {
        const authorizedAtEntry = authorizedToolCalls.delete(event.toolCallId);
        if (!candidate && !authorizedAtEntry && readonly && !safeTools.includes(event.toolName)) {
          attempted = true;
          submission.reject("tool_permission");
        }
        audit(event.type, { tool_name: executionTools.includes(event.toolName) ? event.toolName : "unknown", tool_call_id: event.toolCallId, is_error: event.isError, output_bytes: Buffer.byteLength(JSON.stringify(event.result)) });
      } else if (event.type === "message_end" && event.message.role === "assistant") {
        const message = event.message;
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
          if (block.name === "submit_result") {
            submission.attempts += 1;
            attempted = true;
            if (oversizedArgumentIndexes.has(index)) {
              candidateCaptureError = new AgentFailure("candidate_spool_limit", "Candidate raw bytes exceed the byte limit");
              throw candidateCaptureError;
            }
            const attempt = await durableCapture(candidateSpool.appendCandidate(assistantTurn, index, Buffer.from(rawText, "utf8"), artifactIdentities()));
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
          if (!["toolUse", "error", "aborted"].includes(message.stopReason)) providerError = new AgentFailure("provider_error", "Provider tool stream did not finish normally");
        }
        if (message.stopReason === "error") providerError = providerFailure(message.errorMessage ?? "");
        if (message.stopReason !== "aborted") {
          const u = message.usage;
          if (!u || u.input + u.output + u.cacheRead + u.cacheWrite <= 0) usageKnown = false;
          else { usageSeen = true; usage.input_tokens += u.input; usage.output_tokens += u.output; usage.cache_read_tokens += u.cacheRead; usage.cache_write_tokens += u.cacheWrite; }
        }
      } else if (event.type === "turn_end" && (attempted || providerError || candidateCaptureError)) {
        // 提交及不完整工具流不能进入 SDK 自动续轮；结果修正只走统一有界入口。
        session?.agent.abort();
      }
    });
    if (concludeTimeoutSeconds > 0) {
      const delay = remainingTimeNoticeAt - performance.now();
      if (delay <= 0) notifyRemainingTime();
      else remainingTimeTimer = setTimeout(notifyRemainingTime, delay);
    }
    ensureActive();
    audit("session_start", { resume: request.resume, conclude_only: readonly });
    await session.prompt(request.prompt, { expandPromptTemplates: false });
    while (truncatedWithoutToolCalls && !readonly && !attempted && !candidate
      && !candidateCaptureError && !providerError && !conflict) {
      ensureActive();
      if (outputContinuations >= MAX_OUTPUT_CONTINUATIONS) {
        audit("output_truncation_limit", { stop_reason: "length", output_continuations: outputContinuations });
        throw new AgentFailure("submission_missing", `Model output truncation continuation limit reached after ${outputContinuations} continuations without a submission`);
      }
      outputContinuations += 1;
      audit("output_continuation", {
        stop_reason: "length", output_continuations: outputContinuations,
        remaining_execution_seconds: Math.max(0, (deadline - performance.now()) / 1000),
      });
      ensureActive();
      await session.prompt(OUTPUT_CONTINUATION_PROMPT, { expandPromptTemplates: false });
    }
    while (!candidate && !candidateCaptureError && !providerError && !conflict && submission.repairable() && submission.repairs < request.max_result_repairs) {
      ensureActive();
      submission.repairs += 1;
      attempted = false;
      audit("result_repair", { code: submission.code, ...submission.diagnostics() });
      await session.prompt(`Your previous response was not accepted locally: ${submissionMessage()}. Correct the result in this session and use submit_result; do not replay completed actions. ${readonly ? "Only read existing evidence, register artifacts and submit; target actions remain disabled." : "Authorized tools remain available within the original deadline."}`, { expandPromptTemplates: false });
    }
    ensureActive();
    if (candidateCaptureError) throw candidateCaptureError;
    if (conflict) throw new AgentFailure("submission_conflict", "Conflicting local submissions");
    if (providerError) throw providerError;
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
    try { await lease?.finish(); }
    catch { result.status = "failed"; result.error = { code: "session_corrupt", message: "Could not finalize session transcript" }; }
    if (options.signal?.aborted) stop("cancelled");
    if (performance.now() >= deadline) stop("deadline_exceeded");
    if (cancelled) { result.status = "cancelled"; result.error = { code: cancelled, message: "Request stopped before completion" }; }
    if (result.status !== "succeeded") { result.result = null; result.submission_id = null; }
    // SDK缺失usage分量时也填0；无法区分真实0与未报告，保守输出null而非伪造计量。
    result.usage = usageSeen && usageKnown ? Object.fromEntries(Object.entries(usage).map(([key, value]) => [key, value > 0 ? value : null])) : null;
    if (result.error) result.error = { ...result.error, ...submission.diagnostics() };
    audit("session_end", { status: result.status, ...submission.diagnostics(), submission_id: result.submission_id });
    clearTimeout(timer);
    if (remainingTimeTimer) clearTimeout(remainingTimeTimer);
    options.signal?.removeEventListener("abort", onAbort);
  }
  return result;
}
