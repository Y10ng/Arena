import { createHash, randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { lstat, mkdir, open, readFile, realpath, rename, unlink } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { Compile } from "typebox/compile";
import type { TSchema } from "typebox";
import { ArtifactRegistry, type Artifact } from "./artifacts.js";
import { canonicalCandidateJson } from "./candidate-spool.js";
import { AgentFailure, REPORT_SEGMENT_HARD_LIMITS, type JsonObject, type ReportSegmentCapability } from "./protocol.js";

async function syncDirectory(path: string): Promise<void> {
  const directory = await open(path, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
  try { await directory.sync(); } finally { await directory.close(); }
}
export class ReportSegments {
  readonly parameters: TSchema;
  private readonly validator;
  private readonly saved = new Map<string, { digest: string; artifact: Artifact }>();
  private initialized = false;
  private persistenceError: AgentFailure | undefined;
  constructor(
    private readonly capability: ReportSegmentCapability,
    private readonly cwd: string,
    private readonly captureId: string,
    private readonly artifacts: ArtifactRegistry,
    private readonly ensureActive: () => void,
    private readonly admitRaw: (raw: Buffer) => void,
  ) {
    const { $defs, ...content } = capability.segment_schema as JsonObject;
    this.parameters = {
      type: "object", properties: { key: { type: "string", minLength: 1, maxLength: 256 }, content },
      required: ["key", "content"], additionalProperties: false,
      ...($defs === undefined ? {} : { $defs }),
    } as TSchema;
    try { this.validator = Compile(this.parameters); }
    catch { throw new AgentFailure("invalid_result_schema", "Cannot compile report segment schema"); }
  }
  references(): { key: string; artifact_handle: string; path: string }[] {
    return [...this.saved].map(([key, value]) => ({ key, artifact_handle: value.artifact.handle, path: value.artifact.path }));
  }
  private async persistManifest(artifacts: readonly Artifact[]): Promise<void> {
    const bytes = Buffer.from(JSON.stringify({ protocol: "cairn-report-segment-manifest-v1", binding: this.capability.binding, artifacts }));
    // 完整声明包含路径，单独受 256 项与总正文预算约束；不占候选身份 manifest 的 128 KiB。
    if (bytes.length > 1024 * 1024) throw new AgentFailure("report_segment_limit", "Report segment manifest exceeds its byte limit");
    const path = this.capability.manifest_path;
    const temporary = `${path}.${randomUUID()}.tmp`;
    let file;
    try {
      file = await open(temporary, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
      await file.writeFile(bytes);
      await file.sync();
      await file.close();
      file = undefined;
      await rename(temporary, path);
      await syncDirectory(dirname(path));
    } finally {
      await file?.close();
      await unlink(temporary).catch((error: NodeJS.ErrnoException) => { if (error.code !== "ENOENT") throw error; });
    }
  }
  validate(input: unknown): void {
    this.ensureActive();
    if (this.persistenceError) throw this.persistenceError;
    if (!this.validator.Check(input)) throw new AgentFailure("report_segment_invalid", "Report segment does not match the frozen schema");
  }
  async save(input: unknown): Promise<{ artifact_handle: string; key: string }> {
    this.validate(input);
    const { key, content } = input as { key: string; content: unknown };
    const bytes = Buffer.from(canonicalCandidateJson({ protocol: "cairn-report-segment-v1", binding: this.capability.binding, key, content }));
    if (bytes.length > REPORT_SEGMENT_HARD_LIMITS.max_segment_bytes) throw new AgentFailure("report_segment_limit", "Report segment exceeds 1 MiB; use a smaller structural content unit");
    this.admitRaw(bytes);
    const digest = createHash("sha256").update(bytes).digest("hex");
    const previous = this.saved.get(key);
    if (previous) {
      if (previous.digest !== digest) throw new AgentFailure("report_segment_conflict", "A saved report segment key cannot be reused for different content");
      return { artifact_handle: previous.artifact.handle, key };
    }
    if (this.saved.size >= REPORT_SEGMENT_HARD_LIMITS.max_artifacts) throw new AgentFailure("report_segment_limit", "Report segment count exceeds 256");
    try {
      if (!this.initialized) {
        const path = this.capability.manifest_path;
        const metadata = await lstat(path);
        if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.nlink !== 1 || (metadata.mode & 0o077) !== 0
          || await realpath(dirname(path)) !== dirname(path) || (await readFile(path)).length !== 0) {
          throw new AgentFailure("report_segment_persistence", "Report segment manifest must be a precreated empty private file");
        }
        this.initialized = true;
      }
      const directory = join(this.cwd, ".cairn", "report-segments", this.captureId);
      await mkdir(directory, { recursive: true, mode: 0o700 });
      if (await realpath(directory) !== resolve(directory)) throw new AgentFailure("report_segment_persistence", "Report segment directory cannot contain symlinks");
      const path = join(directory, `${createHash("sha256").update(key).digest("hex")}.json`);
      // 校验与准入必须在原期限内；开始持久写入后完成登记和 manifest，取消不删除已保存材料。
      this.ensureActive();
      const file = await open(path, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o400);
      try { await file.writeFile(bytes); await file.sync(); } finally { await file.close(); }
      await syncDirectory(directory);
      await syncDirectory(dirname(directory));
      await syncDirectory(join(this.cwd, ".cairn"));
      const artifact = await this.artifacts.register(path, "file");
      await this.persistManifest([...this.saved.values()].map((value) => value.artifact).concat(artifact));
      this.saved.set(key, { digest, artifact });
      return { artifact_handle: artifact.handle, key };
    } catch (error) {
      if (error instanceof AgentFailure && ["cancelled", "deadline_exceeded"].includes(error.code)) throw error;
      // 持久化异常后禁止继续改写同一清单；此前已确认的段与原件仍保留供诊断。
      this.persistenceError = error instanceof AgentFailure ? error : new AgentFailure("report_segment_persistence", "Could not durably preserve report segment");
      throw this.persistenceError;
    }
  }
}
