import { randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { lstat, mkdir, open, realpath, type FileHandle } from "node:fs/promises";
import { TextDecoder } from "node:util";
import type { TSchema } from "typebox";
import type { ToolDefinition } from "@mariozechner/pi-coding-agent";
import {
  AgentFailure,
  EXECUTION_CHECKPOINT_HARD_LIMITS,
  EXECUTION_CHECKPOINT_PROTOCOL,
  EXECUTION_CHECKPOINT_SPOOL_ROOT,
  object,
  type ExecutionCheckpointCapability,
  type JsonObject,
  type Phase,
} from "./protocol.js";

export const CHECKPOINT_CATEGORIES = ["progress", "observation", "hypothesis", "limitation", "handoff"] as const;
const RESERVED_DETAIL_KEYS = new Set([
  "artifact", "artifacts", "artifact_id", "artifact_ids", "artifact_handle", "artifact_handles",
  "artifact_path", "artifact_paths", "evidence_ref", "evidence_refs", "path", "paths", "project_id",
  "task_run_id", "owner", "owner_subject", "verified_dispatcher_id", "dispatcher_id", "generation",
  "execution_generation", "generation_disposition", "producer_event_key", "payload", "payload_json",
  "payload_digest", "payload_digest_sha256", "digest", "received_at", "created_at", "updated_at",
  "receipt", "receipt_disposition", "promotion", "promotion_status", "business_acceptance",
]);
const RESERVED_DETAIL_KEYS_COMPACT = new Set([...RESERVED_DETAIL_KEYS].map((key) => key.replaceAll("_", "")));
type CheckpointCategory = typeof CHECKPOINT_CATEGORIES[number];
interface CheckpointInput {
  category: CheckpointCategory;
  summary: string;
  details: JsonObject;
}
interface CheckpointEvent {
  protocol: typeof EXECUTION_CHECKPOINT_PROTOCOL;
  producer_event_key: string;
  kind: "checkpoint";
  phase: Phase;
  recorded_at: string;
  checkpoint: CheckpointInput;
}

function checkpointFailure(code: string, message: string): AgentFailure {
  return new AgentFailure(code, message);
}
function exactKeys(value: JsonObject, expected: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  return actual.length === wanted.length && actual.every((key, index) => key === wanted[index]);
}
function validateDetailKey(key: string): void {
  const separated = key.replace(/(?<=[a-z0-9])(?=[A-Z])/g, "_");
  const normalized = separated.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  const compact = normalized.replaceAll("_", "");
  const tokens = new Set(normalized.split("_"));
  if (
    key !== key.trim() || key.length > EXECUTION_CHECKPOINT_HARD_LIMITS.max_detail_key_chars || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(key) ||
    RESERVED_DETAIL_KEYS.has(normalized) || RESERVED_DETAIL_KEYS_COMPACT.has(compact) ||
    /artifact(?:s|id|ids|handle|handles|path|paths|ref|refs)?$/.test(compact) ||
    compact === "evidenceref" || compact === "evidencerefs" || compact.endsWith("path") || compact.endsWith("paths") ||
    tokens.has("artifact") || tokens.has("artifacts") || tokens.has("path") || tokens.has("paths") ||
    (tokens.has("evidence") && (tokens.has("ref") || tokens.has("refs")))
  ) throw checkpointFailure("checkpoint_invalid", "Checkpoint details contain a reserved or unsafe key");
}
function validateDetails(details: unknown): asserts details is JsonObject {
  if (!object(details)) throw checkpointFailure("checkpoint_invalid", "Checkpoint details must be an object");
  const seen = new WeakSet<object>();
  let nodes = 0;
  const walk = (value: unknown, depth: number): void => {
    nodes += 1;
    if (nodes > EXECUTION_CHECKPOINT_HARD_LIMITS.max_details_nodes) throw checkpointFailure("checkpoint_limit", "Checkpoint details exceed the value limit");
    if (depth > EXECUTION_CHECKPOINT_HARD_LIMITS.max_details_depth) throw checkpointFailure("checkpoint_limit", "Checkpoint details exceed the nesting limit");
    if (typeof value === "number" && !Number.isFinite(value)) throw checkpointFailure("checkpoint_invalid", "Checkpoint details must contain finite JSON values");
    if (typeof value === "string") {
      if (value.includes("\0")) throw checkpointFailure("checkpoint_invalid", "Checkpoint detail strings must not contain NUL");
      if ([...value].length > EXECUTION_CHECKPOINT_HARD_LIMITS.max_detail_string_chars) throw checkpointFailure("checkpoint_limit", "Checkpoint detail string exceeds the limit");
      return;
    }
    if (value === null || typeof value === "boolean" || typeof value === "number") return;
    if (typeof value !== "object") throw checkpointFailure("checkpoint_invalid", "Checkpoint details must contain JSON values only");
    if (seen.has(value)) throw checkpointFailure("checkpoint_invalid", "Checkpoint details must not contain cycles");
    seen.add(value);
    if (Array.isArray(value)) {
      if (value.length > EXECUTION_CHECKPOINT_HARD_LIMITS.max_collection_items) throw checkpointFailure("checkpoint_limit", "Checkpoint detail collection exceeds the item limit");
      for (const item of value) walk(item, depth + 1);
    } else {
      const entries = Object.entries(value);
      if (entries.length > EXECUTION_CHECKPOINT_HARD_LIMITS.max_collection_items) throw checkpointFailure("checkpoint_limit", "Checkpoint detail collection exceeds the item limit");
      for (const [key, nested] of entries) {
        validateDetailKey(key);
        walk(nested, depth + 1);
      }
    }
    seen.delete(value);
  };
  walk(details, 1);
}
function validateInput(value: unknown): asserts value is CheckpointInput {
  if (!object(value) || !exactKeys(value, ["category", "summary", "details"])) throw checkpointFailure("checkpoint_invalid", "Checkpoint arguments are invalid");
  if (!CHECKPOINT_CATEGORIES.includes(value.category as CheckpointCategory)) throw checkpointFailure("checkpoint_invalid", "Checkpoint category is invalid");
  if (typeof value.summary !== "string" || !value.summary.trim() || value.summary.includes("\0")) {
    throw checkpointFailure("checkpoint_invalid", "Checkpoint summary is invalid");
  }
  if ([...value.summary].length > EXECUTION_CHECKPOINT_HARD_LIMITS.max_summary_chars) {
    throw checkpointFailure("checkpoint_limit", "Checkpoint summary exceeds the limit");
  }
  validateDetails(value.details);
}
function validateEvent(value: unknown, phase: Phase): asserts value is CheckpointEvent {
  if (!object(value) || !exactKeys(value, ["protocol", "producer_event_key", "kind", "phase", "recorded_at", "checkpoint"])) throw checkpointFailure("checkpoint_spool_invalid", "Checkpoint spool contains an invalid event");
  if (value.protocol !== EXECUTION_CHECKPOINT_PROTOCOL || value.kind !== "checkpoint" || value.phase !== phase) throw checkpointFailure("checkpoint_spool_invalid", "Checkpoint spool event identity is invalid");
  if (typeof value.producer_event_key !== "string" || value.producer_event_key.length < 1 || value.producer_event_key.length > 128 || !/^[A-Za-z0-9:_-]+$/.test(value.producer_event_key)) {
    throw checkpointFailure("checkpoint_spool_invalid", "Checkpoint spool event key is invalid");
  }
  if (typeof value.recorded_at !== "string" || Number.isNaN(Date.parse(value.recorded_at)) || new Date(value.recorded_at).toISOString() !== value.recorded_at) {
    throw checkpointFailure("checkpoint_spool_invalid", "Checkpoint spool event time is invalid");
  }
  validateInput(value.checkpoint);
}
async function readHandle(file: FileHandle, size: number): Promise<Buffer> {
  const output = Buffer.alloc(size);
  let offset = 0;
  while (offset < size) {
    const { bytesRead } = await file.read(output, offset, size - offset, offset);
    if (bytesRead === 0) throw checkpointFailure("checkpoint_spool_invalid", "Checkpoint spool changed while being read");
    offset += bytesRead;
  }
  return output;
}
function validateSpool(buffer: Buffer, capability: ExecutionCheckpointCapability, phase: Phase): number {
  if (buffer.length > capability.max_spool_bytes || buffer.length > EXECUTION_CHECKPOINT_HARD_LIMITS.max_spool_bytes) throw checkpointFailure("checkpoint_limit", "Checkpoint spool exceeds the byte limit");
  if (buffer.length === 0) return 0;
  if (buffer[buffer.length - 1] !== 0x0a) throw checkpointFailure("checkpoint_spool_invalid", "Checkpoint spool must contain complete JSONL records");
  let text: string;
  try { text = new TextDecoder("utf-8", { fatal: true }).decode(buffer.subarray(0, buffer.length - 1)); }
  catch { throw checkpointFailure("checkpoint_spool_invalid", "Checkpoint spool must be valid UTF-8 JSONL"); }
  const lines = text.split("\n");
  if (lines.length > capability.max_events || lines.length > EXECUTION_CHECKPOINT_HARD_LIMITS.max_events) throw checkpointFailure("checkpoint_limit", "Checkpoint spool exceeds the event limit");
  for (const line of lines) {
    const bytes = Buffer.byteLength(line);
    if (bytes === 0 || bytes > capability.max_event_bytes || bytes > EXECUTION_CHECKPOINT_HARD_LIMITS.max_event_bytes) throw checkpointFailure("checkpoint_limit", "Checkpoint spool contains an oversized event");
    let parsed: unknown;
    try { parsed = JSON.parse(line); }
    catch { throw checkpointFailure("checkpoint_spool_invalid", "Checkpoint spool contains invalid JSONL"); }
    validateEvent(parsed, phase);
  }
  return lines.length;
}
async function ensureSpoolRoot(): Promise<void> {
  try {
    await mkdir(EXECUTION_CHECKPOINT_SPOOL_ROOT, { recursive: true, mode: 0o700 });
    const stat = await lstat(EXECUTION_CHECKPOINT_SPOOL_ROOT);
    if (!stat.isDirectory() || stat.isSymbolicLink() || await realpath(EXECUTION_CHECKPOINT_SPOOL_ROOT) !== EXECUTION_CHECKPOINT_SPOOL_ROOT) throw checkpointFailure("checkpoint_spool_invalid", "Checkpoint spool directory is unsafe");
  } catch (error) {
    if (error instanceof AgentFailure) throw error;
    throw checkpointFailure("checkpoint_spool_unavailable", "Checkpoint spool directory is unavailable");
  }
}
async function appendEvent(capability: ExecutionCheckpointCapability, phase: Phase, event: CheckpointEvent): Promise<number> {
  validateEvent(event, phase);
  const line = Buffer.from(JSON.stringify(event), "utf8");
  if (line.length > capability.max_event_bytes || line.length > EXECUTION_CHECKPOINT_HARD_LIMITS.max_event_bytes) throw checkpointFailure("checkpoint_limit", "Checkpoint event exceeds the byte limit");
  await ensureSpoolRoot();
  let priorStat;
  try { priorStat = await lstat(capability.spool_path); }
  catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw checkpointFailure("checkpoint_spool_unavailable", "Checkpoint spool cannot be inspected"); }
  if (priorStat && (!priorStat.isFile() || priorStat.isSymbolicLink())) throw checkpointFailure("checkpoint_spool_invalid", "Checkpoint spool must be a regular file");
  const noFollow = typeof constants.O_NOFOLLOW === "number" ? constants.O_NOFOLLOW : 0;
  let file: FileHandle | undefined;
  try {
    file = await open(capability.spool_path, constants.O_RDWR | constants.O_APPEND | constants.O_CREAT | noFollow, 0o600);
    const before = await file.stat();
    const linked = await lstat(capability.spool_path);
    if (!before.isFile() || before.nlink !== 1 || !linked.isFile() || linked.isSymbolicLink() || before.dev !== linked.dev || before.ino !== linked.ino) {
      throw checkpointFailure("checkpoint_spool_invalid", "Checkpoint spool must be a safe regular file");
    }
    if (before.size > capability.max_spool_bytes || before.size > EXECUTION_CHECKPOINT_HARD_LIMITS.max_spool_bytes) throw checkpointFailure("checkpoint_limit", "Checkpoint spool exceeds the byte limit");
    const existing = await readHandle(file, before.size);
    const count = validateSpool(existing, capability, phase);
    if (count >= capability.max_events || count >= EXECUTION_CHECKPOINT_HARD_LIMITS.max_events) throw checkpointFailure("checkpoint_limit", "Checkpoint spool exceeds the event limit");
    const record = Buffer.concat([line, Buffer.from("\n")]);
    if (before.size + record.length > capability.max_spool_bytes || before.size + record.length > EXECUTION_CHECKPOINT_HARD_LIMITS.max_spool_bytes) throw checkpointFailure("checkpoint_limit", "Checkpoint spool exceeds the byte limit");
    const { bytesWritten } = await file.write(record);
    if (bytesWritten !== record.length) throw checkpointFailure("checkpoint_spool_unavailable", "Checkpoint spool append was incomplete");
    const after = await file.stat();
    if (!after.isFile() || after.dev !== before.dev || after.ino !== before.ino || after.size !== before.size + record.length) throw checkpointFailure("checkpoint_spool_invalid", "Checkpoint spool changed during append");
    const complete = await readHandle(file, after.size);
    const finalCount = validateSpool(complete, capability, phase);
    if (finalCount !== count + 1) throw checkpointFailure("checkpoint_spool_invalid", "Checkpoint spool event count changed during append");
    return line.length;
  } catch (error) {
    if (error instanceof AgentFailure) throw error;
    throw checkpointFailure("checkpoint_spool_unavailable", "Checkpoint spool append failed");
  } finally {
    await file?.close();
  }
}

export function createCheckpointTool(capability: ExecutionCheckpointCapability, phase: Phase, ensureActive: () => void): ToolDefinition {
  return {
    name: "record_checkpoint",
    label: "Record checkpoint",
    description: "Record only meaningful new progress, observations, hypotheses, limitations, or handoff context. This writes a local checkpoint spool only; it is not Server receipt or business acceptance. Artifact references are not supported.",
    parameters: {
      type: "object",
      properties: {
        category: { type: "string", enum: [...CHECKPOINT_CATEGORIES] },
        summary: { type: "string", minLength: 1, maxLength: EXECUTION_CHECKPOINT_HARD_LIMITS.max_summary_chars },
        details: { type: "object", additionalProperties: true },
      },
      required: ["category", "summary", "details"],
      additionalProperties: false,
    } as TSchema,
    executionMode: "sequential",
    async execute(_id, args) {
      ensureActive();
      validateInput(args);
      const event: CheckpointEvent = {
        protocol: EXECUTION_CHECKPOINT_PROTOCOL,
        producer_event_key: randomUUID(),
        kind: "checkpoint",
        phase,
        recorded_at: new Date().toISOString(),
        checkpoint: structuredClone(args) as CheckpointInput,
      };
      const eventBytes = await appendEvent(capability, phase, event);
      ensureActive();
      return {
        content: [{ type: "text", text: JSON.stringify({ locally_spooled: true, scope: "local_checkpoint_spool_only", producer_event_key: event.producer_event_key, event_bytes: eventBytes, server_received: false, business_accepted: false }) }],
        details: {},
      };
    },
  };
}
