import { basename, dirname, isAbsolute, join, resolve } from "node:path";
import type { TSchema } from "typebox";
import type { SubmissionDiagnostics } from "./submission.js";
import type { ProjectToolsCapability } from "./project-tools.js";
import type { TaskExecutionBinding } from "./task-execution.js";
import type { HttpTrafficBinding } from "./http-flows.js";
import type { FrozenHttpRenderCapability } from "./http-evidence-render.js";

export const PHASE_RESULT_KINDS = {
  bootstrap_execute: "bootstrap_result",
  bootstrap_conclude: "bootstrap_conclude_result",
  explore_execute: "explore_result",
  explore_conclude: "explore_conclude_result",
  verifier_execute: "verifier_result",
  verifier_conclude: "verifier_conclude_result",
  external_verify_execute: "external_verification_result",
  reason_execute: "reason_result",
  report_execute: "report_result",
} as const;
export type Phase = keyof typeof PHASE_RESULT_KINDS | "chat_turn";
export type JsonObject = Record<string, unknown>;
export const EXECUTION_CHECKPOINT_PROTOCOL = "cairn-execution-event-v1" as const;
export const EXECUTION_CHECKPOINT_SPOOL_ROOT = "/tmp/cairn-prompts";
export const CANDIDATE_SPOOL_PROTOCOL = "cairn-candidate-spool-v1" as const;
export const CANDIDATE_FRAME_PROTOCOL = "cairn-candidate-frame-v1" as const;
export const CANDIDATE_SPOOL_ROOT = "/tmp/cairn-prompts";
export const CANDIDATE_SPOOL_HARD_LIMITS = {
  max_candidates: 16,
  max_candidate_bytes: 2 * 1024 * 1024,
  // 32 个身份按最长合法 handle 编码仍可装入 manifest；原文与 spool 总预算不变。
  max_manifest_bytes: 16 * 1024,
  max_artifacts: 32,
  max_artifact_bytes: 32 * 1024 * 1024,
  max_spool_bytes: 34 * 1024 * 1024,
  max_forbidden_literals: 32,
  max_forbidden_literal_bytes: 4 * 1024,
} as const;
export const REPORT_SEGMENT_HARD_LIMITS = {
  max_artifacts: 256,
  max_manifest_bytes: 128 * 1024,
  max_segment_bytes: 1024 * 1024,
} as const;
export function candidateLimits(request: Pick<AgentRequest, "phase" | "capabilities">) {
  return request.phase === "report_execute" && request.capabilities.report_segments
    ? { ...CANDIDATE_SPOOL_HARD_LIMITS, max_artifacts: REPORT_SEGMENT_HARD_LIMITS.max_artifacts, max_manifest_bytes: REPORT_SEGMENT_HARD_LIMITS.max_manifest_bytes }
    : CANDIDATE_SPOOL_HARD_LIMITS;
}
export const EXECUTION_CHECKPOINT_HARD_LIMITS = {
  max_events: 64,
  max_event_bytes: 16 * 1024,
  max_spool_bytes: 512 * 1024,
  max_details_depth: 6,
  max_details_nodes: 256,
  max_collection_items: 64,
  max_detail_string_chars: 4000,
  max_detail_key_chars: 128,
  max_summary_chars: 1000,
} as const;
export interface ExecutionCheckpointCapability {
  protocol: typeof EXECUTION_CHECKPOINT_PROTOCOL;
  spool_path: string;
  max_events: number;
  max_event_bytes: number;
  max_spool_bytes: number;
}
export interface CandidateCaptureCapability {
  protocol: typeof CANDIDATE_SPOOL_PROTOCOL;
  spool_path: string;
  capture_id: string;
  max_candidates: number;
  max_candidate_bytes: number;
  max_manifest_bytes: number;
  max_spool_bytes: number;
  forbidden_literals: string[];
}
export interface ReportSegmentCapability {
  binding: {
    project_id: string;
    task_run_id: string;
    generation_token: string;
    input_snapshot_sha256: string;
    report_template_digest_sha256: string;
  };
  segment_schema: TSchema;
  manifest_path: string;
}
export interface AgentRequest {
  protocol: "cairn-agent-request-v1";
  project_id: string;
  task_run_id: string;
  phase: Phase;
  prompt: string;
  session: string;
  resume: boolean;
  continuation?: { source_task_run_id: string; execution_generation: number };
  model: {
    protocol: "anthropic-messages" | "openai-chat-completions" | "openai-responses";
    model_name: string;
    max_output_tokens: number;
    inference_endpoint: string;
    base_url: string;
    auth_header: "authorization-bearer" | "x-api-key";
    api_key: string;
    custom_headers: { name: string; value: string }[];
  };
  result_schema: TSchema;
  binding: {
    slot_key?: string;
    surface_key?: string;
    test_family?: string;
    verification_observation_id?: string;
    verification_request_id?: string;
    review_input_digest?: string;
    max_intents?: number;
    allowed_fact_ids?: string[];
    allowed_intent_ids?: string[];
    structured_intent_binding?: JsonObject;
  } | null;
  timeout_seconds: number;
  conclude_timeout_seconds: number;
  bash_tool_timeout_seconds: number;
  max_result_repairs: number;
  conclude_only: boolean;
  cwd: string;
  session_root: string;
  capabilities: {
    candidate_capture?: CandidateCaptureCapability;
    project_tools?: ProjectToolsCapability;
    task_execution?: TaskExecutionBinding;
    http_traffic?: HttpTrafficBinding;
    http_evidence_render?: FrozenHttpRenderCapability;
    execution_checkpoints?: ExecutionCheckpointCapability;
    report_segments?: ReportSegmentCapability;
  };
}
export interface AgentEnvelope {
  protocol: "cairn-agent-result-v1";
  engine: "pi";
  project_id: string;
  task_run_id: string;
  phase: string;
  session: string;
  status: "succeeded" | "failed" | "cancelled";
  result: JsonObject | null;
  submission_id: string | null;
  usage: JsonObject | null;
  error: ({ code: string; message: string; path?: string } & Partial<SubmissionDiagnostics>) | null;
}
export class AgentFailure extends Error {
  constructor(readonly code: string, message: string, readonly path?: string) { super(message); }
  toJSON(): NonNullable<AgentEnvelope["error"]> {
    return { code: this.code, message: this.message, ...(this.path === undefined ? {} : { path: this.path }) };
  }
}
export function object(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function exactKeys(value: JsonObject, expected: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  return actual.length === wanted.length && actual.every((key, index) => key === wanted[index]);
}
function assert(condition: unknown, field: string): asserts condition {
  if (!condition) throw new AgentFailure("invalid_request", `Invalid request field: ${field}`, field);
}
function parseCandidateCapture(value: unknown, report = false): CandidateCaptureCapability {
  const prefix = "capabilities.candidate_capture";
  assert(object(value), prefix);
  assert(exactKeys(value, ["protocol", "spool_path", "capture_id", "max_candidates", "max_candidate_bytes", "max_manifest_bytes", "max_spool_bytes", "forbidden_literals"]), prefix);
  assert(value.protocol === CANDIDATE_SPOOL_PROTOCOL, `${prefix}.protocol`);
  assert(typeof value.capture_id === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value.capture_id), `${prefix}.capture_id`);
  assert(typeof value.spool_path === "string" && isAbsolute(value.spool_path) && !value.spool_path.includes("\0"), `${prefix}.spool_path`);
  const spoolPath = resolve(value.spool_path as string);
  assert(dirname(spoolPath) === CANDIDATE_SPOOL_ROOT && spoolPath === value.spool_path && basename(spoolPath) === `pi-candidates-${value.capture_id}.bin`, `${prefix}.spool_path`);
  for (const key of ["max_candidates", "max_candidate_bytes", "max_manifest_bytes", "max_spool_bytes"] as const) {
    const maximum = report && key === "max_manifest_bytes" ? REPORT_SEGMENT_HARD_LIMITS.max_manifest_bytes : CANDIDATE_SPOOL_HARD_LIMITS[key];
    assert(Number.isInteger(value[key]) && (value[key] as number) > 0 && (value[key] as number) <= maximum, `${prefix}.${key}`);
  }
  assert(Array.isArray(value.forbidden_literals) && value.forbidden_literals.length <= CANDIDATE_SPOOL_HARD_LIMITS.max_forbidden_literals, `${prefix}.forbidden_literals`);
  const forbiddenLiterals = value.forbidden_literals as unknown[];
  assert(forbiddenLiterals.every((literal) => typeof literal === "string" && literal.length > 0 && !literal.includes("\0") && Buffer.byteLength(literal) <= CANDIDATE_SPOOL_HARD_LIMITS.max_forbidden_literal_bytes), `${prefix}.forbidden_literals`);
  assert(new Set(forbiddenLiterals).size === forbiddenLiterals.length, `${prefix}.forbidden_literals`);
  return {
    protocol: CANDIDATE_SPOOL_PROTOCOL,
    spool_path: spoolPath,
    capture_id: value.capture_id as string,
    max_candidates: value.max_candidates as number,
    max_candidate_bytes: value.max_candidate_bytes as number,
    max_manifest_bytes: value.max_manifest_bytes as number,
    max_spool_bytes: value.max_spool_bytes as number,
    forbidden_literals: [...forbiddenLiterals] as string[],
  };
}
function parseExecutionCheckpoints(value: unknown): ExecutionCheckpointCapability {
  const prefix = "capabilities.execution_checkpoints";
  assert(object(value), prefix);
  assert(exactKeys(value, ["protocol", "spool_path", "max_events", "max_event_bytes", "max_spool_bytes"]), prefix);
  assert(value.protocol === EXECUTION_CHECKPOINT_PROTOCOL, `${prefix}.protocol`);
  assert(typeof value.spool_path === "string" && isAbsolute(value.spool_path) && !value.spool_path.includes("\0"), `${prefix}.spool_path`);
  const spoolPath = resolve(value.spool_path as string);
  const spoolName = basename(spoolPath);
  assert(dirname(spoolPath) === EXECUTION_CHECKPOINT_SPOOL_ROOT && spoolPath === value.spool_path && /^[A-Za-z0-9][A-Za-z0-9._-]{0,191}\.jsonl$/.test(spoolName), `${prefix}.spool_path`);
  for (const key of ["max_events", "max_event_bytes", "max_spool_bytes"] as const) {
    assert(Number.isInteger(value[key]) && (value[key] as number) > 0 && (value[key] as number) <= EXECUTION_CHECKPOINT_HARD_LIMITS[key], `${prefix}.${key}`);
  }
  return {
    protocol: EXECUTION_CHECKPOINT_PROTOCOL,
    spool_path: spoolPath,
    max_events: value.max_events as number,
    max_event_bytes: value.max_event_bytes as number,
    max_spool_bytes: value.max_spool_bytes as number,
  };
}
function parseReportSegments(value: unknown, raw: JsonObject): ReportSegmentCapability {
  const prefix = "capabilities.report_segments";
  assert(raw.phase === "report_execute" && object(value) && exactKeys(value, ["binding", "segment_schema", "manifest_path"]), prefix);
  const binding = value.binding;
  assert(object(binding) && exactKeys(binding, ["project_id", "task_run_id", "generation_token", "input_snapshot_sha256", "report_template_digest_sha256"]), `${prefix}.binding`);
  assert(binding.project_id === raw.project_id && binding.task_run_id === raw.task_run_id, `${prefix}.binding`);
  assert(typeof binding.generation_token === "string" && binding.generation_token.length > 0 && !binding.generation_token.includes("\0"), `${prefix}.binding.generation_token`);
  for (const key of ["input_snapshot_sha256", "report_template_digest_sha256"]) {
    assert(typeof binding[key] === "string" && /^[a-f0-9]{64}$/.test(binding[key] as string), `${prefix}.binding.${key}`);
  }
  assert(object(value.segment_schema), `${prefix}.segment_schema`);
  const capture = (raw.capabilities as JsonObject).candidate_capture;
  assert(object(capture) && typeof capture.capture_id === "string", "capabilities.candidate_capture.capture_id");
  assert(value.manifest_path === join(CANDIDATE_SPOOL_ROOT, `pi-report-segments-${capture.capture_id}.json`), `${prefix}.manifest_path`);
  return value as unknown as ReportSegmentCapability;
}
export function parseRequest(raw: unknown): AgentRequest {
  assert(object(raw), "$");
  assert(raw.protocol === "cairn-agent-request-v1", "protocol");
  for (const key of ["project_id", "task_run_id", "phase", "prompt", "session"]) {
    assert(typeof raw[key] === "string" && raw[key].length > 0 && !raw[key].includes("\0"), key);
  }
  const chat = raw.phase === "chat_turn";
  assert(chat || Object.hasOwn(PHASE_RESULT_KINDS, raw.phase as string), "phase");
  assert(/^pi:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(raw.session as string), "session");
  for (const key of ["resume", "conclude_only"]) assert(typeof raw[key] === "boolean", key);
  const normalContinuation = raw.resume === true && raw.conclude_only === false && (raw.phase as string).endsWith("_execute") && raw.phase !== "report_execute";
  if (normalContinuation) assert(object(raw.continuation), "continuation");
  if (raw.continuation !== undefined) {
    assert(normalContinuation && object(raw.continuation) && exactKeys(raw.continuation, ["source_task_run_id", "execution_generation"]), "continuation");
    assert(typeof raw.continuation.source_task_run_id === "string" && raw.continuation.source_task_run_id.length > 0 && !raw.continuation.source_task_run_id.includes("\0") && raw.continuation.source_task_run_id !== raw.task_run_id, "continuation.source_task_run_id");
    assert(Number.isSafeInteger(raw.continuation.execution_generation) && (raw.continuation.execution_generation as number) >= 0, "continuation.execution_generation");
  }
  assert(typeof raw.timeout_seconds === "number" && Number.isFinite(raw.timeout_seconds) && raw.timeout_seconds > 0 && raw.timeout_seconds <= 86400, "timeout_seconds");
  const concludeTimeoutSeconds = raw.conclude_timeout_seconds ?? 0;
  assert(
    typeof concludeTimeoutSeconds === "number" && Number.isFinite(concludeTimeoutSeconds) &&
    concludeTimeoutSeconds >= 0 && concludeTimeoutSeconds <= raw.timeout_seconds,
    "conclude_timeout_seconds",
  );
  const bashToolTimeoutSeconds = raw.bash_tool_timeout_seconds ?? 120;
  assert(
    typeof bashToolTimeoutSeconds === "number" && Number.isInteger(bashToolTimeoutSeconds) &&
    bashToolTimeoutSeconds > 0 && bashToolTimeoutSeconds <= 3600,
    "bash_tool_timeout_seconds",
  );
  const maxRepairs = raw.max_result_repairs ?? 2;
  assert(Number.isInteger(maxRepairs) && (maxRepairs as number) >= 0 && (maxRepairs as number) <= 2, "max_result_repairs");
  assert(object(raw.result_schema), "result_schema");
  assert(object(raw.model), "model");
  const model = raw.model;
  assert(["anthropic-messages", "openai-chat-completions", "openai-responses"].includes(model.protocol as string), "model.protocol");
  assert(["authorization-bearer", "x-api-key"].includes(model.auth_header as string), "model.auth_header");
  for (const key of ["model_name", "inference_endpoint", "base_url", "api_key"]) assert(typeof model[key] === "string" && model[key].length > 0, `model.${key}`);
  assert(Number.isSafeInteger(model.max_output_tokens) && (model.max_output_tokens as number) > 0, "model.max_output_tokens");
  assert(Array.isArray(model.custom_headers) && model.custom_headers.every((h) => object(h) && typeof h.name === "string" && typeof h.value === "string"), "model.custom_headers");
  assert(raw.binding === null || object(raw.binding), "binding");
  if (object(raw.binding)) {
    const binding = raw.binding;
    for (const key of ["slot_key", "surface_key", "test_family", "verification_observation_id", "verification_request_id", "review_input_digest"]) assert(binding[key] === undefined || (typeof binding[key] === "string" && binding[key].length > 0), `binding.${key}`);
    assert(binding.max_intents === undefined || (Number.isInteger(binding.max_intents) && (binding.max_intents as number) >= 0), "binding.max_intents");
    assert(binding.allowed_fact_ids === undefined || (Array.isArray(binding.allowed_fact_ids) && binding.allowed_fact_ids.every((id) => typeof id === "string")), "binding.allowed_fact_ids");
    assert(binding.allowed_intent_ids === undefined || (Array.isArray(binding.allowed_intent_ids) && binding.allowed_intent_ids.every((id) => typeof id === "string" && id.length > 0 && id.length <= 256) && new Set(binding.allowed_intent_ids).size === binding.allowed_intent_ids.length), "binding.allowed_intent_ids");
    assert(binding.structured_intent_binding === undefined || object(binding.structured_intent_binding), "binding.structured_intent_binding");
  }
  assert(object(raw.capabilities), "capabilities");
  const capabilityKeys = Object.keys(raw.capabilities);
  assert(chat
    ? capabilityKeys.length === 1 && capabilityKeys[0] === "project_tools"
    : capabilityKeys.length >= 1 && capabilityKeys.length <= 6 && capabilityKeys.includes("candidate_capture") && capabilityKeys.every((key) => ["candidate_capture", "execution_checkpoints", "report_segments", "task_execution", "http_traffic", "http_evidence_render"].includes(key)), "capabilities");
  const projectTools = raw.capabilities.project_tools;
  if (chat) {
    assert(raw.conclude_only === false && raw.binding === null && raw.continuation === undefined && maxRepairs === 0, "chat_turn");
    assert(object(projectTools) && exactKeys(projectTools, ["protocol", "socket_path", "workspace_path", "forbidden_literals"]), "capabilities.project_tools");
    assert(projectTools.protocol === "cairn-project-tools-v1", "capabilities.project_tools.protocol");
    assert(typeof projectTools.workspace_path === "string" && isAbsolute(projectTools.workspace_path) && /^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,199}$/.test(basename(projectTools.workspace_path)) && dirname(projectTools.workspace_path) === (raw.cwd ?? "/home/kali/workspace"), "capabilities.project_tools.workspace_path");
    assert(typeof projectTools.socket_path === "string" && /^\/tmp\/cairn-prompts\/pi-project-[0-9a-f]{32}\.sock$/.test(projectTools.socket_path), "capabilities.project_tools.socket_path");
    assert(Array.isArray(projectTools.forbidden_literals) && projectTools.forbidden_literals.length <= 32 && projectTools.forbidden_literals.every((item) => typeof item === "string" && item.length > 0 && Buffer.byteLength(item) <= 4096), "capabilities.project_tools.forbidden_literals");
  }
  const httpRender = raw.capabilities.http_evidence_render;
  if (httpRender !== undefined) {
    assert(raw.phase === "report_execute" && object(httpRender) && exactKeys(httpRender, ["frozen_materials_path", "manifest_path"]), "capabilities.http_evidence_render");
    const capture = raw.capabilities.candidate_capture;
    assert(object(capture) && httpRender.manifest_path === `/tmp/cairn-prompts/pi-http-evidence-${capture.capture_id}.json`, "capabilities.http_evidence_render.manifest_path");
    assert(typeof httpRender.frozen_materials_path === "string" && /^\/opt\/cairn\/readonly-inputs\/[A-Za-z0-9_-]+\/packet-full\.json$/.test(httpRender.frozen_materials_path), "capabilities.http_evidence_render.frozen_materials_path");
  }
  const httpTraffic = raw.capabilities.http_traffic;
  if (httpTraffic !== undefined) {
    assert(raw.phase !== "report_execute" && object(httpTraffic) && exactKeys(httpTraffic, ["protocol", "project_id", "task_run_id", "socket_path"]), "capabilities.http_traffic");
    assert(httpTraffic.protocol === "cairn-traffic-read-v1" && httpTraffic.project_id === raw.project_id && httpTraffic.task_run_id === raw.task_run_id, "capabilities.http_traffic");
    const capture = raw.capabilities.candidate_capture;
    assert(object(capture) && httpTraffic.socket_path === `/tmp/cairn-prompts/pi-traffic-${capture.capture_id}.sock`, "capabilities.http_traffic.socket_path");
  }
  const taskExecution = raw.capabilities.task_execution;
  const readonly = chat || raw.conclude_only || (raw.phase as string).endsWith("_conclude") || raw.phase === "report_execute";
  assert(readonly || taskExecution !== undefined, "capabilities.task_execution");
  if (taskExecution !== undefined) {
    assert(object(taskExecution) && exactKeys(taskExecution, ["protocol", "scope_id", "task_run_id", "socket_path"]), "capabilities.task_execution");
    assert(taskExecution.protocol === "cairn-task-execution-v1" && taskExecution.task_run_id === raw.task_run_id, "capabilities.task_execution.task_run_id");
    assert(typeof taskExecution.scope_id === "string" && /^[0-9a-f]{32}$/.test(taskExecution.scope_id), "capabilities.task_execution.scope_id");
    assert(taskExecution.socket_path === `/run/cairn-exec/${taskExecution.scope_id}/execute.sock`, "capabilities.task_execution.socket_path");
  }
  const reportSegments = raw.capabilities.report_segments === undefined ? undefined : parseReportSegments(raw.capabilities.report_segments, raw);
  assert(raw.phase !== "report_execute" || reportSegments !== undefined, "capabilities.report_segments");
  const capabilities: AgentRequest["capabilities"] = {
    ...(chat ? { project_tools: projectTools as unknown as ProjectToolsCapability } : { candidate_capture: parseCandidateCapture(raw.capabilities.candidate_capture, reportSegments !== undefined) }),
    ...(taskExecution === undefined ? {} : { task_execution: taskExecution as unknown as TaskExecutionBinding }),
    ...(httpTraffic === undefined ? {} : { http_traffic: httpTraffic as unknown as HttpTrafficBinding }),
    ...(httpRender === undefined ? {} : { http_evidence_render: httpRender as unknown as FrozenHttpRenderCapability }),
    ...(raw.capabilities.execution_checkpoints === undefined ? {} : { execution_checkpoints: parseExecutionCheckpoints(raw.capabilities.execution_checkpoints) }),
    ...(reportSegments === undefined ? {} : { report_segments: reportSegments }),
  };
  const cwd = raw.cwd ?? "/home/kali/workspace";
  assert(typeof cwd === "string" && isAbsolute(cwd), "cwd");
  const sessionRoot = raw.session_root ?? "/home/kali/.cairn/pi";
  assert(typeof sessionRoot === "string" && isAbsolute(sessionRoot), "session_root");
  assert(!(chat || taskExecution || httpTraffic) || (resolve(sessionRoot) !== resolve(cwd) && !resolve(sessionRoot).startsWith(resolve(cwd) + "/")), "session_root");
  return {
    ...raw,
    cwd,
    session_root: sessionRoot,
    conclude_timeout_seconds: concludeTimeoutSeconds,
    bash_tool_timeout_seconds: bashToolTimeoutSeconds,
    max_result_repairs: maxRepairs,
    capabilities,
  } as unknown as AgentRequest;
}
export function envelope(request: Partial<AgentRequest>): AgentEnvelope {
  return {
    protocol: "cairn-agent-result-v1", engine: "pi",
    project_id: request.project_id ?? "", task_run_id: request.task_run_id ?? "",
    phase: request.phase ?? "", session: request.session ?? "",
    status: "failed", result: null, submission_id: null, usage: null, error: null,
  };
}
