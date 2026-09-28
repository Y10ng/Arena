import { createHash, randomUUID } from "node:crypto";
import { createReadStream } from "node:fs";
import { lstat, realpath } from "node:fs/promises";
import { isAbsolute, relative, resolve, sep } from "node:path";
import { AgentFailure, object, type JsonObject } from "./protocol.js";

const READONLY_INPUT_ROOT = "/opt/cairn/readonly-inputs";

export const ARTIFACT_KINDS = ["log", "file", "report", "screenshot", "patch", "raw_output"] as const;
export interface Artifact { handle: string; kind: string; path: string; sha256: string; size_bytes: number }
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
  readonlyInputRoot = READONLY_INPUT_ROOT,
): Promise<string> {
  const candidate = resolve(cwd, path);
  if ((await lstat(candidate)).isSymbolicLink()) throw new AgentFailure("artifact_invalid", "Evidence cannot be a symlink");
  const actual = await realpath(candidate);
  const root = await realpath(cwd);
  const rel = relative(root, actual);
  const sessionRel = relative(sessionRoot, actual);
  const inWorkspace = rel !== "" && rel !== ".." && !rel.startsWith(`..${sep}`) && !isAbsolute(rel);
  const outsideSession = sessionRel === ".." || sessionRel.startsWith(`..${sep}`) || isAbsolute(sessionRel);
  const inReadonlyInputs = (await readonlyInputsRoots(cwd, readonlyInputRoot)).some((readonlyRoot) => within(readonlyRoot, actual));
  if ((!inReadonlyInputs && (!inWorkspace || !outsideSession)) || !(await lstat(actual)).isFile()) {
    throw new AgentFailure("artifact_invalid", "Evidence must be a regular authorized input file outside session storage");
  }
  return actual;
}
function within(root: string, path: string): boolean {
  const rel = relative(root, path);
  return rel !== ".." && !rel.startsWith(`..${sep}`) && !isAbsolute(rel);
}
async function readonlyInputsRoots(cwd: string, managedRoot = READONLY_INPUT_ROOT): Promise<string[]> {
  const candidates = [...new Set([resolve(await realpath(cwd), ".cairn", "readonly-inputs"), managedRoot])];
  return Promise.all(candidates.map(async (root) => {
    try { return await realpath(root); }
    catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") return root; throw error; }
  }));
}
async function fileDigest(path: string, signal?: AbortSignal): Promise<{ sha256: string; size_bytes: number }> {
  const hash = createHash("sha256");
  let size = 0;
  for await (const chunk of createReadStream(path, { signal })) { hash.update(chunk); size += chunk.length; }
  return { sha256: hash.digest("hex"), size_bytes: size };
}
export class ArtifactRegistry {
  readonly items: Artifact[] = [];
  constructor(
    private cwd: string,
    private sessionRoot: string,
    private readonlyInputRoot = READONLY_INPUT_ROOT,
  ) {}
  async register(path: string, kind: string, signal?: AbortSignal): Promise<Artifact> {
    if (!ARTIFACT_KINDS.includes(kind as typeof ARTIFACT_KINDS[number])) throw new AgentFailure("artifact_invalid", "Unknown artifact kind");
    const actual = await evidencePath(this.cwd, path, this.sessionRoot, this.readonlyInputRoot);
    if ((await readonlyInputsRoots(this.cwd, this.readonlyInputRoot)).some((root) => within(root, actual))) throw new AgentFailure("artifact_invalid", "Read-only input files cannot be registered as artifacts");
    const digest = await fileDigest(actual, signal);
    const prior = this.items.find((item) => item.path === actual && item.kind === kind && item.sha256 === digest.sha256);
    if (prior) return prior;
    for (let index = this.items.length - 1; index >= 0; index -= 1) if (this.items[index]!.path === actual && this.items[index]!.kind === kind) this.items.splice(index, 1);
    const artifact = { handle: `local-${randomUUID()}`, kind, path: actual, ...digest };
    this.items.push(artifact);
    return artifact;
  }
  async inject(data: JsonObject, signal?: AbortSignal): Promise<void> {
    if (data.artifacts !== undefined) {
      if (!Array.isArray(data.artifacts) || data.artifacts.some((item) => !object(item) || !this.items.some((registered) => registered.handle === item.handle && registered.path === item.path && registered.kind === item.kind))) {
        throw new AgentFailure("artifact_unknown", "data.artifacts must only reference register_artifact results");
      }
    }
    for (const artifact of this.items) {
      const actual = await evidencePath(this.cwd, artifact.path, this.sessionRoot, this.readonlyInputRoot);
      const current = await fileDigest(actual, signal);
      if (current.sha256 !== artifact.sha256 || current.size_bytes !== artifact.size_bytes) throw new AgentFailure("artifact_changed", "Registered evidence changed; register it again before submitting");
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
    if (this.items.length > 0 || data.artifacts !== undefined) data.artifacts = this.items.map(({ handle, kind, path }) => ({ handle, kind, path }));
  }
}
