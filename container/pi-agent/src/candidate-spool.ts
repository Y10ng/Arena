import { createHash, timingSafeEqual } from "node:crypto";
import { constants } from "node:fs";
import { lstat, mkdir, open, realpath, type FileHandle } from "node:fs/promises";
import { TextDecoder } from "node:util";
import {
  AgentFailure,
  CANDIDATE_FRAME_PROTOCOL,
  CANDIDATE_SPOOL_HARD_LIMITS,
  CANDIDATE_SPOOL_ROOT,
  object,
  type AgentRequest,
  type JsonObject,
} from "./protocol.js";

const FRAME_MAGIC = Buffer.from("CRNCND01", "ascii");
const FRAME_HEADER_BYTES = FRAME_MAGIC.length + 8;
const FRAME_CHECKSUM_BYTES = 32;
const MAX_ARTIFACTS = CANDIDATE_SPOOL_HARD_LIMITS.max_artifacts;
const MAX_OUTCOMES_PER_CANDIDATE = 64;
const MAX_CANDIDATE_POSITION = 1_000_000;
const ARTIFACT_KINDS = new Set([
  "log", "file", "report", "screenshot", "patch", "raw_output",
]);
const SUBMISSION_FAILURE_CODES = [
  "submission_missing", "submission_invalid", "submission_conflict", "submission_injected_too_large",
  "artifact_unknown", "artifact_changed", "artifact_invalid", "artifact_unreadable", "artifact_permission", "tool_permission",
] as const;
const CANDIDATE_OUTCOME_CODES = new Set([
  ...SUBMISSION_FAILURE_CODES,
  "candidate_admission_rejected", "candidate_manifest_invalid", "candidate_outcome_invalid", "candidate_spool_conflict",
  "candidate_spool_invalid", "candidate_spool_limit", "candidate_spool_unavailable", "locally_validated", "provider_auth",
  "provider_error", "provider_rate_limit", "result_schema", "runner_error",
]);
const SUBMISSION_ISSUE_KEYWORDS = new Set([
  "schema", "strict_json", "input_size", "type", "required", "additionalProperties", "const", "enum", "anyOf", "oneOf",
  "allOf", "not", "if", "minLength", "maxLength", "pattern", "format", "minimum", "maximum", "exclusiveMinimum",
  "exclusiveMaximum", "multipleOf", "minItems", "maxItems", "uniqueItems", "minProperties", "maxProperties",
]);
const PRIVATE_KEY_MARKERS = [
  "-----BEGIN PRIVATE KEY-----",
  "-----BEGIN ENCRYPTED PRIVATE KEY-----",
  "-----BEGIN RSA PRIVATE KEY-----",
  "-----BEGIN DSA PRIVATE KEY-----",
  "-----BEGIN EC PRIVATE KEY-----",
  "-----BEGIN OPENSSH PRIVATE KEY-----",
  "-----BEGIN PGP PRIVATE KEY BLOCK-----",
] as const;
const SAFE_IDENTITY = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const CANDIDATE_FIELDS = [
  "protocol", "record_kind", "capture_id", "producer_candidate_key", "parent_producer_candidate_key",
  "assistant_turn", "content_index", "project_id", "task_run_id", "phase", "session", "binding_sha256",
  "result_schema_sha256", "artifacts", "raw_sha256", "raw_size_bytes",
] as const;
const OUTCOME_FIELDS = [
  "protocol", "record_kind", "capture_id", "producer_candidate_key", "outcome_sequence", "code", "path",
  "keyword", "count", "artifact_status", "binding_status",
] as const;

export interface CandidateArtifactIdentity {
  handle: string;
  kind: string;
  sha256: string;
  size_bytes: number;
}
export interface CandidateAttempt {
  producer_candidate_key: string;
  parent_producer_candidate_key: string | null;
  request_sha256: string;
  raw_sha256: string;
  raw_size_bytes: number;
}
export interface CandidateOutcome {
  code: string;
  path?: string;
  keyword?: string;
  count?: number;
  artifact_status: "not_evaluated" | "validated" | "rejected";
  binding_status: "not_evaluated" | "not_required" | "injected" | "rejected";
}
interface DecodedFrame {
  manifest: JsonObject;
  raw: Buffer;
}

function candidateFailure(code: string, message: string): AgentFailure {
  return new AgentFailure(code, message);
}
export function canonicalCandidateJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalCandidateJson).join(",")}]`;
  if (object(value)) return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalCandidateJson(value[key])}`).join(",")}}`;
  const encoded = JSON.stringify(value);
  if (encoded === undefined) throw candidateFailure("candidate_manifest_invalid", "Candidate manifest must contain JSON values only");
  return encoded;
}
function sha256(...values: (Buffer | string)[]): string {
  const hash = createHash("sha256");
  for (const value of values) hash.update(value);
  return hash.digest("hex");
}
function asciiJsonEscape(value: string, upperHex = false): string {
  let output = "";
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    const simple = code === 0x08 ? "\\b" : code === 0x09 ? "\\t" : code === 0x0a ? "\\n" : code === 0x0c ? "\\f" : code === 0x0d ? "\\r" : code === 0x22 ? "\\\"" : code === 0x5c ? "\\\\" : undefined;
    if (simple !== undefined) output += simple;
    else if (code < 0x20 || code > 0x7e) {
      const hex = code.toString(16).padStart(4, "0");
      output += `\\u${upperHex ? hex.toUpperCase() : hex}`;
    } else output += value[index];
  }
  return output;
}
async function readHandle(file: FileHandle, size: number): Promise<Buffer> {
  const output = Buffer.alloc(size);
  let offset = 0;
  while (offset < size) {
    const { bytesRead } = await file.read(output, offset, size - offset, offset);
    if (bytesRead === 0) throw candidateFailure("candidate_spool_invalid", "Candidate spool changed while being read");
    offset += bytesRead;
  }
  return output;
}
function exactKeys(value: JsonObject, expected: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  return actual.length === wanted.length && actual.every((key, index) => key === wanted[index]);
}
function validArtifactHandle(value: unknown): value is string {
  return typeof value === "string" && value.startsWith("local-") && SAFE_IDENTITY.test(value);
}
function compareAscii(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}
function validArtifactManifest(value: unknown): value is CandidateArtifactIdentity[] {
  if (!Array.isArray(value) || value.length > MAX_ARTIFACTS) return false;
  let previous = "";
  const handles = new Set<string>();
  for (const artifact of value) {
    if (!object(artifact) || !exactKeys(artifact, ["handle", "kind", "sha256", "size_bytes"])
      || !validArtifactHandle(artifact.handle) || compareAscii(artifact.handle, previous) < 0 || handles.has(artifact.handle)
      || typeof artifact.kind !== "string" || !ARTIFACT_KINDS.has(artifact.kind)
      || typeof artifact.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(artifact.sha256)
      || !Number.isSafeInteger(artifact.size_bytes) || (artifact.size_bytes as number) < 0) return false;
    handles.add(artifact.handle);
    previous = artifact.handle;
  }
  return value.reduce((total, artifact) => total + artifact.size_bytes, 0) <= CANDIDATE_SPOOL_HARD_LIMITS.max_artifact_bytes;
}
function submissionSchemaPaths(value: unknown): Set<string> {
  const paths = new Set<string>(["$"]);
  if (!object(value)) return paths;
  const root = value;
  const definitions = object(root.$defs) ? root.$defs : {};
  const walk = (value: unknown, path: string, depth = 0): void => {
    if (!object(value) || depth > 32) return;
    if (typeof value.$ref === "string" && value.$ref.startsWith("#/$defs/")) {
      walk(definitions[value.$ref.slice(8)], path, depth + 1);
    }
    if (object(value.properties)) {
      for (const [key, child] of Object.entries(value.properties)) {
        const childPath = `${path}/${key.replaceAll("~", "~0").replaceAll("/", "~1")}`;
        paths.add(childPath);
        walk(child, childPath, depth + 1);
      }
    }
    if (object(value.items)) {
      paths.add(`${path}/*`);
      walk(value.items, `${path}/*`, depth + 1);
    }
    for (const keyword of ["anyOf", "oneOf", "allOf"]) {
      const variants = value[keyword];
      if (Array.isArray(variants)) for (const child of variants) walk(child, path, depth + 1);
    }
  };
  walk(root, "");
  return paths;
}
function validOutcomeDiagnostic(manifest: JsonObject, allowedPaths: ReadonlySet<string>): boolean {
  if (typeof manifest.code !== "string" || !CANDIDATE_OUTCOME_CODES.has(manifest.code)
    || !(manifest.count === null || manifest.count === 1)
    || ((manifest.path === null) !== (manifest.keyword === null))
    || !["not_evaluated", "validated", "rejected"].includes(manifest.artifact_status as string)
    || !["not_evaluated", "not_required", "injected", "rejected"].includes(manifest.binding_status as string)) return false;
  if (manifest.path !== null) {
    if (manifest.code !== "submission_invalid" || typeof manifest.path !== "string" || !allowedPaths.has(manifest.path)
      || typeof manifest.keyword !== "string" || !SUBMISSION_ISSUE_KEYWORDS.has(manifest.keyword)
      || manifest.count !== 1 || manifest.artifact_status !== "not_evaluated" || manifest.binding_status !== "not_evaluated") return false;
    if (["strict_json", "input_size"].includes(manifest.keyword) && manifest.path !== "$") return false;
  }
  return !["locally_validated", "submission_invalid"].includes(manifest.code) || manifest.count === 1;
}
function validOutcomeManifest(manifest: JsonObject, allowedPaths: ReadonlySet<string>): boolean {
  return exactKeys(manifest, OUTCOME_FIELDS) && validOutcomeDiagnostic(manifest, allowedPaths);
}
function decodeSpool(buffer: Buffer, request: AgentRequest): DecodedFrame[] {
  const capability = request.capabilities.candidate_capture;
  const allowedPaths = submissionSchemaPaths(request.result_schema);
  if (buffer.length > capability.max_spool_bytes || buffer.length > CANDIDATE_SPOOL_HARD_LIMITS.max_spool_bytes) {
    throw candidateFailure("candidate_spool_limit", "Candidate spool exceeds the byte limit");
  }
  const frames: DecodedFrame[] = [];
  const candidateKeys = new Set<string>();
  const outcomeSequences = new Map<string, number>();
  let previousCandidateKey: string | null = null;
  let previousCandidatePosition: readonly [number, number] | null = null;
  let candidateCount = 0;
  let offset = 0;
  while (offset < buffer.length) {
    const start = offset;
    if (buffer.length - offset < FRAME_HEADER_BYTES + FRAME_CHECKSUM_BYTES || !buffer.subarray(offset, offset + FRAME_MAGIC.length).equals(FRAME_MAGIC)) {
      throw candidateFailure("candidate_spool_invalid", "Candidate spool contains an invalid or truncated frame");
    }
    offset += FRAME_MAGIC.length;
    const manifestLength = buffer.readUInt32BE(offset); offset += 4;
    const rawLength = buffer.readUInt32BE(offset); offset += 4;
    if (manifestLength === 0 || manifestLength > capability.max_manifest_bytes || manifestLength > CANDIDATE_SPOOL_HARD_LIMITS.max_manifest_bytes) {
      throw candidateFailure("candidate_spool_limit", "Candidate manifest exceeds the byte limit");
    }
    if (rawLength > capability.max_candidate_bytes || rawLength > CANDIDATE_SPOOL_HARD_LIMITS.max_candidate_bytes) {
      throw candidateFailure("candidate_spool_limit", "Candidate raw bytes exceed the byte limit");
    }
    const end = offset + manifestLength + rawLength + FRAME_CHECKSUM_BYTES;
    if (end > buffer.length) throw candidateFailure("candidate_spool_invalid", "Candidate spool contains a truncated frame");
    const manifestBytes = buffer.subarray(offset, offset + manifestLength); offset += manifestLength;
    const raw = buffer.subarray(offset, offset + rawLength); offset += rawLength;
    const checksum = buffer.subarray(offset, offset + FRAME_CHECKSUM_BYTES); offset += FRAME_CHECKSUM_BYTES;
    const expected = createHash("sha256").update(buffer.subarray(start, offset - FRAME_CHECKSUM_BYTES)).digest();
    if (!timingSafeEqual(checksum, expected)) throw candidateFailure("candidate_spool_invalid", "Candidate frame checksum does not match");
    let text: string;
    try { text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(manifestBytes); }
    catch { throw candidateFailure("candidate_spool_invalid", "Candidate manifest must be valid UTF-8"); }
    let manifest: unknown;
    try { manifest = JSON.parse(text); }
    catch { throw candidateFailure("candidate_spool_invalid", "Candidate manifest must be valid JSON"); }
    if (!object(manifest) || canonicalCandidateJson(manifest) !== text || manifest.protocol !== CANDIDATE_FRAME_PROTOCOL || manifest.capture_id !== capability.capture_id) {
      throw candidateFailure("candidate_spool_invalid", "Candidate manifest identity is invalid");
    }
    if (manifest.record_kind === "candidate") {
      candidateCount += 1;
      if (candidateCount > capability.max_candidates || candidateCount > CANDIDATE_SPOOL_HARD_LIMITS.max_candidates) throw candidateFailure("candidate_spool_limit", "Candidate spool exceeds the candidate limit");
      const assistantTurn = manifest.assistant_turn as number;
      const contentIndex = manifest.content_index as number;
      const positionValid = Number.isSafeInteger(assistantTurn) && assistantTurn >= 1
        && assistantTurn <= MAX_CANDIDATE_POSITION && Number.isSafeInteger(contentIndex)
        && contentIndex >= 0 && contentIndex <= MAX_CANDIDATE_POSITION;
      const positionMonotonic = previousCandidatePosition === null
        || assistantTurn > previousCandidatePosition[0]
        || (assistantTurn === previousCandidatePosition[0] && contentIndex > previousCandidatePosition[1]);
      const expectedKey = `${capability.capture_id}:${manifest.assistant_turn}:${manifest.content_index}`;
      if (!exactKeys(manifest, CANDIDATE_FIELDS) || !positionValid || !positionMonotonic || manifest.producer_candidate_key !== expectedKey || candidateKeys.has(expectedKey)) throw candidateFailure("candidate_spool_invalid", "Candidate producer key or position is invalid");
      if (manifest.parent_producer_candidate_key !== previousCandidateKey) throw candidateFailure("candidate_spool_invalid", "Candidate parent lineage is invalid");
      if (manifest.project_id !== request.project_id || manifest.task_run_id !== request.task_run_id || manifest.phase !== request.phase || manifest.session !== request.session
        || manifest.binding_sha256 !== sha256(canonicalCandidateJson(request.binding)) || manifest.result_schema_sha256 !== sha256(canonicalCandidateJson(request.result_schema))
        || !validArtifactManifest(manifest.artifacts)) throw candidateFailure("candidate_spool_invalid", "Candidate execution identity is invalid");
      try { new TextDecoder("utf-8", { fatal: true }).decode(raw); }
      catch { throw candidateFailure("candidate_spool_invalid", "Candidate raw bytes must be valid UTF-8"); }
      if (manifest.raw_size_bytes !== raw.length || manifest.raw_sha256 !== sha256(raw)) throw candidateFailure("candidate_spool_invalid", "Candidate raw identity is invalid");
      candidateKeys.add(expectedKey);
      previousCandidateKey = expectedKey;
      previousCandidatePosition = [assistantTurn, contentIndex];
    } else if (manifest.record_kind === "outcome") {
      if (!validOutcomeManifest(manifest, allowedPaths) || raw.length !== 0 || typeof manifest.producer_candidate_key !== "string" || !candidateKeys.has(manifest.producer_candidate_key) || !Number.isSafeInteger(manifest.outcome_sequence) || (manifest.outcome_sequence as number) < 1 || (manifest.outcome_sequence as number) > MAX_OUTCOMES_PER_CANDIDATE) {
        throw candidateFailure("candidate_spool_invalid", "Candidate outcome identity is invalid");
      }
      const expectedSequence = (outcomeSequences.get(manifest.producer_candidate_key) ?? 0) + 1;
      if (manifest.outcome_sequence !== expectedSequence) throw candidateFailure("candidate_spool_invalid", "Candidate outcome sequence is invalid");
      outcomeSequences.set(manifest.producer_candidate_key, expectedSequence);
    } else {
      throw candidateFailure("candidate_spool_invalid", "Candidate frame kind is invalid");
    }
    frames.push({ manifest, raw: Buffer.from(raw) });
  }
  return frames;
}
async function ensureSpoolRoot(): Promise<void> {
  try {
    await mkdir(CANDIDATE_SPOOL_ROOT, { recursive: true, mode: 0o700 });
    const stat = await lstat(CANDIDATE_SPOOL_ROOT);
    if (!stat.isDirectory() || stat.isSymbolicLink() || (stat.mode & 0o022) !== 0 || await realpath(CANDIDATE_SPOOL_ROOT) !== CANDIDATE_SPOOL_ROOT) {
      throw candidateFailure("candidate_spool_invalid", "Candidate spool directory is unsafe");
    }
  } catch (error) {
    if (error instanceof AgentFailure) throw error;
    throw candidateFailure("candidate_spool_unavailable", "Candidate spool directory is unavailable");
  }
}
function frame(manifest: JsonObject, raw: Buffer, maxManifestBytes: number): { bytes: Buffer; manifestBytes: Buffer; requestSha256: string } {
  const manifestBytes = Buffer.from(canonicalCandidateJson(manifest), "utf8");
  if (manifestBytes.length === 0 || manifestBytes.length > maxManifestBytes || manifestBytes.length > CANDIDATE_SPOOL_HARD_LIMITS.max_manifest_bytes) {
    throw candidateFailure("candidate_spool_limit", "Candidate manifest exceeds the byte limit");
  }
  const header = Buffer.alloc(FRAME_HEADER_BYTES);
  FRAME_MAGIC.copy(header, 0);
  header.writeUInt32BE(manifestBytes.length, FRAME_MAGIC.length);
  header.writeUInt32BE(raw.length, FRAME_MAGIC.length + 4);
  const payload = Buffer.concat([header, manifestBytes, raw]);
  const checksum = createHash("sha256").update(payload).digest();
  return { bytes: Buffer.concat([payload, checksum]), manifestBytes, requestSha256: sha256(manifestBytes, raw) };
}
function validateArtifacts(artifacts: readonly CandidateArtifactIdentity[]): CandidateArtifactIdentity[] {
  if (artifacts.length > MAX_ARTIFACTS) throw candidateFailure("candidate_spool_limit", "Candidate artifact identity count exceeds the limit");
  const result = artifacts.map((artifact) => {
    if (!artifact || !validArtifactHandle(artifact.handle) || typeof artifact.kind !== "string" || !ARTIFACT_KINDS.has(artifact.kind) || typeof artifact.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(artifact.sha256) || !Number.isSafeInteger(artifact.size_bytes) || artifact.size_bytes < 0) {
      throw candidateFailure("candidate_manifest_invalid", "Candidate artifact identity is invalid");
    }
    return { handle: artifact.handle, kind: artifact.kind, sha256: artifact.sha256, size_bytes: artifact.size_bytes };
  }).sort((left, right) => compareAscii(left.handle, right.handle));
  if (result.reduce((total, artifact) => total + artifact.size_bytes, 0) > CANDIDATE_SPOOL_HARD_LIMITS.max_artifact_bytes) throw candidateFailure("candidate_spool_limit", "Registered content exceeds the total byte limit");
  if (new Set(result.map((artifact) => artifact.handle)).size !== result.length) throw candidateFailure("candidate_manifest_invalid", "Candidate artifact handles must be unique");
  return result;
}

export class CandidateSpool {
  private readonly capability;
  private readonly bindingSha256: string;
  private readonly resultSchemaSha256: string;
  private readonly forbidden: Buffer[];
  constructor(private readonly request: AgentRequest) {
    this.capability = request.capabilities.candidate_capture;
    this.bindingSha256 = sha256(canonicalCandidateJson(request.binding));
    this.resultSchemaSha256 = sha256(canonicalCandidateJson(request.result_schema));
    const literals = [
      request.model.api_key,
      ...request.model.custom_headers.filter((header) => ["authorization", "x-api-key"].includes(header.name.toLowerCase())).map((header) => header.value),
      ...this.capability.forbidden_literals,
      ...PRIVATE_KEY_MARKERS,
    ];
    this.forbidden = [...new Set(literals)].flatMap((literal) => {
      const variants = [literal, JSON.stringify(literal).slice(1, -1), asciiJsonEscape(literal), asciiJsonEscape(literal, true)];
      return [...new Set(variants)].map((variant) => Buffer.from(variant, "utf8"));
    });
  }
  private async append(manifest: JsonObject, raw: Buffer): Promise<{ requestSha256: string; frames: DecodedFrame[] }> {
    await ensureSpoolRoot();
    const encoded = frame(manifest, raw, this.capability.max_manifest_bytes);
    const noFollow = typeof constants.O_NOFOLLOW === "number" ? constants.O_NOFOLLOW : 0;
    let existed = true;
    try { await lstat(this.capability.spool_path); }
    catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") existed = false; else throw candidateFailure("candidate_spool_unavailable", "Candidate spool cannot be inspected"); }
    let file: FileHandle | undefined;
    try {
      file = await open(this.capability.spool_path, constants.O_RDWR | constants.O_APPEND | constants.O_CREAT | noFollow, 0o600);
      const before = await file.stat();
      const linked = await lstat(this.capability.spool_path);
      if (!before.isFile() || before.nlink !== 1 || (before.mode & 0o077) !== 0 || !linked.isFile() || linked.isSymbolicLink() || before.dev !== linked.dev || before.ino !== linked.ino) {
        throw candidateFailure("candidate_spool_invalid", "Candidate spool must be a private regular file");
      }
      const existing = await readHandle(file, before.size);
      decodeSpool(existing, this.request);
      if (before.size + encoded.bytes.length > this.capability.max_spool_bytes || before.size + encoded.bytes.length > CANDIDATE_SPOOL_HARD_LIMITS.max_spool_bytes) {
        throw candidateFailure("candidate_spool_limit", "Candidate spool exceeds the byte limit");
      }
      let written = 0;
      while (written < encoded.bytes.length) {
        const result = await file.write(encoded.bytes.subarray(written));
        if (result.bytesWritten === 0) throw candidateFailure("candidate_spool_unavailable", "Candidate spool append was incomplete");
        written += result.bytesWritten;
      }
      await file.sync();
      if (!existed) {
        const directory = await open(CANDIDATE_SPOOL_ROOT, constants.O_RDONLY | (typeof constants.O_DIRECTORY === "number" ? constants.O_DIRECTORY : 0));
        try { await directory.sync(); }
        finally { await directory.close(); }
      }
      const after = await file.stat();
      if (!after.isFile() || after.dev !== before.dev || after.ino !== before.ino || after.size !== before.size + encoded.bytes.length) {
        throw candidateFailure("candidate_spool_invalid", "Candidate spool changed during append");
      }
      const complete = await readHandle(file, after.size);
      return { requestSha256: encoded.requestSha256, frames: decodeSpool(complete, this.request) };
    } catch (error) {
      if (error instanceof AgentFailure) throw error;
      if ((error as NodeJS.ErrnoException).code === "ELOOP") throw candidateFailure("candidate_spool_invalid", "Candidate spool cannot be a symlink");
      throw candidateFailure("candidate_spool_unavailable", "Candidate spool append failed");
    } finally {
      await file?.close();
    }
  }
  async appendCandidate(assistantTurn: number, contentIndex: number, rawInput: Buffer, artifacts: readonly CandidateArtifactIdentity[] = []): Promise<CandidateAttempt> {
    if (!Number.isSafeInteger(assistantTurn) || assistantTurn < 1 || assistantTurn > MAX_CANDIDATE_POSITION
      || !Number.isSafeInteger(contentIndex) || contentIndex < 0 || contentIndex > MAX_CANDIDATE_POSITION) {
      throw candidateFailure("candidate_manifest_invalid", "Candidate position is invalid");
    }
    const raw = Buffer.from(rawInput);
    if (raw.length > this.capability.max_candidate_bytes || raw.length > CANDIDATE_SPOOL_HARD_LIMITS.max_candidate_bytes) throw candidateFailure("candidate_spool_limit", "Candidate raw bytes exceed the byte limit");
    try { new TextDecoder("utf-8", { fatal: true }).decode(raw); }
    catch { throw candidateFailure("candidate_manifest_invalid", "Candidate raw bytes must be valid UTF-8"); }
    if (this.forbidden.some((literal) => literal.length > 0 && raw.indexOf(literal) !== -1)) throw candidateFailure("candidate_admission_rejected", "Candidate contains a forbidden runtime literal");
    let existing: DecodedFrame[] = [];
    try {
      const file = await open(this.capability.spool_path, constants.O_RDONLY | (typeof constants.O_NOFOLLOW === "number" ? constants.O_NOFOLLOW : 0));
      try { existing = decodeSpool(await readHandle(file, (await file.stat()).size), this.request); }
      finally { await file.close(); }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
        if (error instanceof AgentFailure) throw error;
        if ((error as NodeJS.ErrnoException).code === "ELOOP") throw candidateFailure("candidate_spool_invalid", "Candidate spool cannot be a symlink");
        throw candidateFailure("candidate_spool_unavailable", "Candidate spool cannot be inspected");
      }
    }
    const candidateFrames = existing.filter((entry) => entry.manifest.record_kind === "candidate");
    if (candidateFrames.length >= this.capability.max_candidates || candidateFrames.length >= CANDIDATE_SPOOL_HARD_LIMITS.max_candidates) throw candidateFailure("candidate_spool_limit", "Candidate spool exceeds the candidate limit");
    const producerKey = `${this.capability.capture_id}:${assistantTurn}:${contentIndex}`;
    if (candidateFrames.some((entry) => entry.manifest.producer_candidate_key === producerKey)) throw candidateFailure("candidate_spool_conflict", "Candidate producer key already exists");
    const latest = candidateFrames.at(-1)?.manifest;
    if (latest !== undefined) {
      const latestPosition: readonly [number, number] = [
        latest.assistant_turn as number,
        latest.content_index as number,
      ];
      if (assistantTurn < latestPosition[0]
        || (assistantTurn === latestPosition[0] && contentIndex <= latestPosition[1])) {
        throw candidateFailure("candidate_manifest_invalid", "Candidate position must increase monotonically");
      }
    }
    const parent = latest?.producer_candidate_key;
    const manifest: JsonObject = {
      protocol: CANDIDATE_FRAME_PROTOCOL,
      record_kind: "candidate",
      capture_id: this.capability.capture_id,
      producer_candidate_key: producerKey,
      parent_producer_candidate_key: typeof parent === "string" ? parent : null,
      assistant_turn: assistantTurn,
      content_index: contentIndex,
      project_id: this.request.project_id,
      task_run_id: this.request.task_run_id,
      phase: this.request.phase,
      session: this.request.session,
      binding_sha256: this.bindingSha256,
      result_schema_sha256: this.resultSchemaSha256,
      artifacts: validateArtifacts(artifacts),
      raw_sha256: sha256(raw),
      raw_size_bytes: raw.length,
    };
    const appended = await this.append(manifest, raw);
    return {
      producer_candidate_key: producerKey,
      parent_producer_candidate_key: typeof parent === "string" ? parent : null,
      request_sha256: appended.requestSha256,
      raw_sha256: manifest.raw_sha256 as string,
      raw_size_bytes: raw.length,
    };
  }
  async appendOutcome(producerCandidateKey: string, outcome: CandidateOutcome): Promise<string> {
    const diagnostic: JsonObject = {
      code: outcome.code,
      path: outcome.path ?? null,
      keyword: outcome.keyword ?? null,
      count: outcome.count ?? null,
      artifact_status: outcome.artifact_status,
      binding_status: outcome.binding_status,
    };
    if (!validOutcomeDiagnostic(diagnostic, submissionSchemaPaths(this.request.result_schema))) {
      throw candidateFailure("candidate_outcome_invalid", "Candidate outcome is not an authoritative diagnostic");
    }
    let existing: DecodedFrame[];
    try {
      const file = await open(this.capability.spool_path, constants.O_RDONLY | (typeof constants.O_NOFOLLOW === "number" ? constants.O_NOFOLLOW : 0));
      try { existing = decodeSpool(await readHandle(file, (await file.stat()).size), this.request); }
      finally { await file.close(); }
    } catch (error) {
      if (error instanceof AgentFailure) throw error;
      if ((error as NodeJS.ErrnoException).code === "ELOOP") throw candidateFailure("candidate_spool_invalid", "Candidate spool cannot be a symlink");
      throw candidateFailure("candidate_spool_unavailable", "Candidate spool cannot be inspected");
    }
    if (!existing.some((entry) => entry.manifest.record_kind === "candidate" && entry.manifest.producer_candidate_key === producerCandidateKey)) throw candidateFailure("candidate_outcome_invalid", "Candidate outcome references an unknown producer key");
    const sequence = existing.filter((entry) => entry.manifest.record_kind === "outcome" && entry.manifest.producer_candidate_key === producerCandidateKey).length + 1;
    if (sequence > MAX_OUTCOMES_PER_CANDIDATE) throw candidateFailure("candidate_outcome_invalid", "Candidate outcome count exceeds the limit");
    const manifest: JsonObject = {
      protocol: CANDIDATE_FRAME_PROTOCOL,
      record_kind: "outcome",
      capture_id: this.capability.capture_id,
      producer_candidate_key: producerCandidateKey,
      outcome_sequence: sequence,
      ...diagnostic,
    };
    return (await this.append(manifest, Buffer.alloc(0))).requestSha256;
  }
}
