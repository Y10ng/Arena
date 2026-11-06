import { createHash, randomUUID } from "node:crypto";
import { constants, createReadStream } from "node:fs";
import { lstat, mkdir, open, realpath, unlink } from "node:fs/promises";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { AgentFailure, CANDIDATE_SPOOL_HARD_LIMITS, object, type JsonObject } from "./protocol.js";

const READONLY_INPUT_ROOTS = ["/opt/cairn/readonly-inputs", "/cairn/advisory/project", "/home/kali/workspace/inputs"];

export const ARTIFACT_KINDS = ["log", "file", "report", "screenshot", "patch", "raw_output"] as const;
export interface Artifact { handle: string; kind: string; path: string; source_path: string; sha256: string; size_bytes: number }
export function evidenceFailure(error: unknown): AgentFailure {
  if (error instanceof AgentFailure) return error;
  // 不传播 fs 异常正文：其中常含模型提供的路径或凭据。
  const code = (error as NodeJS.ErrnoException | null)?.code;
  return code === "EACCES" || code === "EPERM"
    ? new AgentFailure("artifact_permission", "Evidence access denied")
    : new AgentFailure("artifact_unreadable", "Evidence could not be read");
}
export async function evidencePath(
  cwd: string,
  path: string,
  sessionRoot: string,
  readonlyInputRoots = READONLY_INPUT_ROOTS,
): Promise<string> {
  const candidate = resolve(cwd, path);
  if ((await lstat(candidate)).isSymbolicLink()) throw new AgentFailure("artifact_invalid", "Evidence cannot be a symlink");
  const actual = await realpath(candidate);
  const root = await realpath(cwd);
  const rel = relative(root, actual);
  const sessionRel = relative(sessionRoot, actual);
  const inWorkspace = rel !== "" && rel !== ".." && !rel.startsWith(`..${sep}`) && !isAbsolute(rel);
  const outsideSession = sessionRel === ".." || sessionRel.startsWith(`..${sep}`) || isAbsolute(sessionRel);
  const inReadonlyInputs = (await readonlyInputsRoots(cwd, readonlyInputRoots)).some((readonlyRoot) => within(readonlyRoot, actual));
  if ((!inReadonlyInputs && !inWorkspace) || !outsideSession || !(await lstat(actual)).isFile()) {
    throw new AgentFailure("artifact_invalid", "Evidence must be a regular authorized input file outside session storage");
  }
  return actual;
}
export async function readEvidence(path: string, input: { offset?: number; length?: number; match?: string }) {
  const offset = input.offset ?? 0;
  const length = input.length ?? 32768;
  if (!Number.isSafeInteger(offset) || offset < 0 || !Number.isSafeInteger(length) || length < 1 || length > 32768) {
    throw new AgentFailure("artifact_invalid", "Evidence offset must be a nonnegative byte position and length must be 1..32768 bytes");
  }
  const match = input.match === undefined ? undefined : Buffer.from(input.match, "utf8");
  if (match && (match.length === 0 || match.length > length || match.toString("utf8") !== input.match)) {
    throw new AgentFailure("artifact_invalid", "Evidence match must be nonempty valid UTF-8 text within the requested length (at most 32768 bytes)");
  }
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const total = (await file.stat()).size;
    if (offset > total) throw new AgentFailure("artifact_invalid", "Evidence byte offset is beyond the original file");
    if (match && total > CANDIDATE_SPOOL_HARD_LIMITS.max_artifact_bytes) {
      throw new AgentFailure("artifact_invalid", "Exact evidence matching is limited to files within the 32 MiB byte budget; read explicit ranges instead");
    }
    // 匹配只按需查找这一份原件，读入量受现有 32 MiB 预算约束，回执仍只返回选中范围。
    const buffer = Buffer.alloc(match ? total - offset : Math.min(length, total - offset));
    let read = 0;
    while (read < buffer.length) {
      const { bytesRead } = await file.read(buffer, read, buffer.length - read, offset + read);
      if (bytesRead === 0) throw new AgentFailure("artifact_unreadable", "Evidence changed or could not be read completely");
      read += bytesRead;
    }
    let start = offset;
    let text: string;
    if (match) {
      const found = buffer.indexOf(match);
      if (found < 0) throw new AgentFailure("artifact_invalid", "Evidence match was not found at or after offset");
      // 包括重叠出现；不能默认挑第一个而让选段指向另一处同文。
      if (buffer.indexOf(match, found + 1) >= 0) {
        throw new AgentFailure("artifact_invalid", "Evidence match is ambiguous; supply offset to restrict the search start or use a longer exact match");
      }
      start += found;
      text = match.toString("utf8");
    } else {
      try {
        // stream 只允许非 EOF 的尾部不完整字符留给下一块，起点、内部或 EOF 无效字节不替换也不丢弃。
        text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(buffer, { stream: offset + read < total });
      } catch {
        throw new AgentFailure("artifact_unreadable", "Evidence range is not valid UTF-8; use a valid byte boundary or match exact original text");
      }
      if (read > 0 && text.length === 0) {
        throw new AgentFailure("artifact_invalid", "Evidence length is too small to read a complete UTF-8 character at this offset");
      }
    }
    const count = Buffer.byteLength(text, "utf8");
    const next = start + count;
    return { text, byte_start: start, bytes_read: count, next_offset: next, total_bytes: total, eof: next === total, pointer: `utf8-bytes=${start}:${next}` };
  } finally { await file.close(); }
}
function within(root: string, path: string): boolean {
  const rel = relative(root, path);
  return rel !== ".." && !rel.startsWith(`..${sep}`) && !isAbsolute(rel);
}
async function readonlyInputsRoots(cwd: string, managedRoots = READONLY_INPUT_ROOTS): Promise<string[]> {
  const candidates = [...new Set([resolve(await realpath(cwd), ".cairn", "readonly-inputs"), resolve(await realpath(cwd), "inputs"), ...managedRoots])];
  return Promise.all(candidates.map(async (root) => {
    try { return await realpath(root); }
    catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") return root; throw error; }
  }));
}
async function fileDigest(path: string, signal?: AbortSignal): Promise<{ sha256: string; size_bytes: number }> {
  const hash = createHash("sha256");
  let size = 0;
  const initial = await lstat(path);
  if (initial.size > CANDIDATE_SPOOL_HARD_LIMITS.max_artifact_bytes) throw new AgentFailure("artifact_changed", "Registered content exceeds its bounded byte budget");
  if (initial.size > 0) for await (const chunk of createReadStream(path, { signal, end: initial.size - 1 })) { hash.update(chunk); size += chunk.length; }
  return { sha256: hash.digest("hex"), size_bytes: size };
}
export class ArtifactRegistry {
  readonly items: Artifact[] = [];
  constructor(
    private cwd: string,
    private sessionRoot: string,
    private readonlyInputRoots = READONLY_INPUT_ROOTS,
    private maxArtifacts: number = CANDIDATE_SPOOL_HARD_LIMITS.max_artifacts,
  ) {}
  async register(sourcePath: string, kind: string, signal?: AbortSignal): Promise<Artifact> {
    if (!ARTIFACT_KINDS.includes(kind as typeof ARTIFACT_KINDS[number])) throw new AgentFailure("artifact_invalid", "Unknown artifact kind");
    const actual = await evidencePath(this.cwd, sourcePath, this.sessionRoot, this.readonlyInputRoots);
    if ((await readonlyInputsRoots(this.cwd, this.readonlyInputRoots)).some((root) => within(root, actual))) throw new AgentFailure("artifact_invalid", "Read-only input files cannot be registered as artifacts");
    if (this.items.length >= this.maxArtifacts) {
      throw new AgentFailure("artifact_invalid", `Registration capacity reached (${this.maxArtifacts} content versions, including Harness output); existing versions are retained`);
    }
    const remaining = CANDIDATE_SPOOL_HARD_LIMITS.max_artifact_bytes - this.items.reduce((total, item) => total + item.size_bytes, 0);
    const source = await open(actual, constants.O_RDONLY | constants.O_NOFOLLOW);
    let destination;
    let saved = false;
    const handle = `local-${randomUUID()}`;
    const directory = join(this.cwd, ".cairn", "artifacts", "registered");
    const path = join(directory, handle);
    try {
      const initial = await source.stat();
      if (!initial.isFile()) throw new AgentFailure("artifact_invalid", "Evidence must be a regular file");
      if (initial.size > remaining) throw new AgentFailure("artifact_invalid", "Registered content versions exceed the 32 MiB total byte budget; no content was truncated");
      await mkdir(directory, { recursive: true, mode: 0o700 });
      if (await realpath(directory) !== resolve(directory)) throw new AgentFailure("artifact_invalid", "Managed artifact directory cannot contain symlinks");
      destination = await open(path, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o400);
      const hash = createHash("sha256");
      let size = 0;
      const buffer = Buffer.alloc(64 * 1024);
      // 只捕获打开时已有的长度，不追赶持续增长的日志；摘要与副本由同一次读取产生。
      while (size < initial.size) {
        signal?.throwIfAborted();
        const { bytesRead } = await source.read(buffer, 0, Math.min(buffer.length, initial.size - size), size);
        if (bytesRead === 0) throw new AgentFailure("artifact_unreadable", "Evidence capture was incomplete; no partial version was registered");
        const bytes = buffer.subarray(0, bytesRead);
        let written = 0;
        while (written < bytesRead) {
          const result = await destination.write(bytes.subarray(written));
          if (result.bytesWritten === 0) throw new AgentFailure("artifact_unreadable", "Evidence capture write was incomplete");
          written += result.bytesWritten;
        }
        hash.update(bytes);
        size += bytesRead;
      }
      await destination.sync();
      for (const parentPath of [directory, dirname(directory), join(this.cwd, ".cairn")]) {
        const parent = await open(parentPath, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
        try { await parent.sync(); } finally { await parent.close(); }
      }
      const artifact = { handle, kind, path, source_path: actual, sha256: hash.digest("hex"), size_bytes: size };
      this.items.push(artifact);
      saved = true;
      return artifact;
    } finally {
      await source.close();
      await destination?.close();
      if (destination && !saved) await unlink(path);
    }
  }
  async inject(data: JsonObject, signal?: AbortSignal): Promise<void> {
    if (data.artifacts !== undefined) {
      if (!Array.isArray(data.artifacts) || data.artifacts.some((item) => !object(item) || !this.items.some((registered) => registered.handle === item.handle && registered.path === item.path && registered.kind === item.kind))) {
        throw new AgentFailure("artifact_unknown", "data.artifacts must only reference register_artifact results");
      }
    }
    for (const artifact of this.items) {
      const actual = await evidencePath(this.cwd, artifact.path, this.sessionRoot, this.readonlyInputRoots);
      const current = await fileDigest(actual, signal);
      if (current.sha256 !== artifact.sha256 || current.size_bytes !== artifact.size_bytes) {
        // 只用 Runner 生成的 handle 和已校验的 kind 定位，不将路径或证据正文带入错误反馈。
        throw new AgentFailure("artifact_changed", `Registered content version ${artifact.handle} (${artifact.kind}) changed; delivery copies must match their registered bytes; changing the source or registering another version cannot repair this copy`);
      }
    }
    const walk = (value: unknown): void => {
      if (Array.isArray(value)) { for (const item of value) walk(item); return; }
      if (!object(value)) return;
      if (typeof value.artifact_id === "string" && !/^a[0-9]+$/.test(value.artifact_id) && !this.items.some((item) => item.handle === value.artifact_id)) {
        throw new AgentFailure("artifact_unknown", "Evidence reference is not a registered handle or Server Artifact ID");
      }
      for (const key of ["artifact_handle", "packet_artifact_handle"]) if (typeof value[key] === "string" && !this.items.some((item) => item.handle === value[key])) {
        throw new AgentFailure("artifact_unknown", "Evidence handle was not registered in this request");
      }
      for (const nested of Object.values(value)) walk(nested);
    };
    walk(data);
    if (this.items.length > 0 || data.artifacts !== undefined) data.artifacts = this.items.map((item) => ({ ...item }));
  }
}
