import { createHash } from "node:crypto";
import { mkdir, open, readFile, lstat, realpath, rename, unlink, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { CURRENT_SESSION_VERSION, SessionManager } from "@mariozechner/pi-coding-agent";
import { AgentFailure, object, type AgentRequest } from "./protocol.js";

const digest = (raw: string) => createHash("sha256").update(raw).digest("hex");
interface Manifest {
  protocol: "cairn-pi-session-v1";
  project_id: string;
  session: string;
  sdk_session_id: string;
  cwd: string;
  phase_family: string;
  binding_sha256: string;
  current_binding_sha256?: string;
  last_task_run_id?: string;
  sha256: string;
}
export interface SessionLease {
  manager: SessionManager;
  directory: string;
  finish(): Promise<void>;
}
async function regularFile(path: string): Promise<string> {
  const stat = await lstat(path);
  if (!stat.isFile() || stat.isSymbolicLink()) throw new AgentFailure("session_corrupt", "Session file is not a regular file");
  return readFile(path, "utf8");
}
function validateTranscript(raw: string, manifest: Manifest): void {
  if (!raw.endsWith("\n")) throw new AgentFailure("session_corrupt", "Session transcript is truncated");
  const lines = raw.trimEnd().split("\n").map((line: string) => JSON.parse(line) as unknown);
  const header = lines.shift();
  if (!object(header) || header.type !== "session" || header.version !== CURRENT_SESSION_VERSION || header.id !== manifest.sdk_session_id || header.cwd !== manifest.cwd || !object(header.cairn) || header.cairn.project_id !== manifest.project_id || header.cairn.session !== manifest.session) {
    throw new AgentFailure("session_corrupt", "Session header ownership or version mismatch");
  }
  const seen = new Set<string>();
  for (const entry of lines) {
    if (!object(entry) || typeof entry.id !== "string" || seen.has(entry.id) || typeof entry.timestamp !== "string" || (entry.parentId !== null && !seen.has(entry.parentId as string))) {
      throw new AgentFailure("session_corrupt", "Session entry chain is invalid");
    }
    if (!["message", "thinking_level_change", "model_change", "compaction", "branch_summary", "custom", "custom_message", "label", "session_info"].includes(entry.type as string)) {
      throw new AgentFailure("session_corrupt", "Session entry type is invalid");
    }
    if (entry.type === "message" && (!object(entry.message) || !["user", "assistant", "toolResult", "bashExecution", "custom"].includes(entry.message.role as string))) {
      throw new AgentFailure("session_corrupt", "Session message is invalid");
    }
    seen.add(entry.id);
  }
}
export async function acquireSession(request: AgentRequest): Promise<SessionLease> {
  const cwd = await realpath(request.cwd);
  const phaseFamily = request.phase.replace(/_(execute|conclude)$/, "");
  const bindingDigest = digest(JSON.stringify(Object.fromEntries(Object.entries(request.binding ?? {}).sort(([left], [right]) => left.localeCompare(right)))));
  const continuation = request.continuation;
  if (request.resume && !request.conclude_only && request.phase !== "report_execute" && !continuation) {
    throw new AgentFailure("invalid_request", "Normal resumed execution requires a verified continuation source", "continuation");
  }
  await mkdir(request.session_root, { recursive: true, mode: 0o700 });
  if ((await lstat(request.session_root)).isSymbolicLink()) throw new AgentFailure("session_corrupt", "Session root cannot be a symlink");
  const root = await realpath(request.session_root);
  const id = request.session.slice(3).toLowerCase();
  const directory = join(root, id);
  const lockPath = join(root, `${id}.lock`);
  let lock;
  try { lock = await open(lockPath, "wx", 0o600); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === "EEXIST") throw new AgentFailure("session_locked", "Session already has a writer; stale locks require explicit recovery");
    throw error;
  }
  const release = async () => { await lock.close(); await unlink(lockPath); };
  try {
    await lock.writeFile(JSON.stringify({ pid: process.pid, project_id: request.project_id }));
    const manifestPath = join(directory, "manifest.json");
    const transcriptPath = join(directory, "session.jsonl");
    let manifest: Manifest;
    let manager: SessionManager;
    if (!request.resume) {
      try { await mkdir(directory, { mode: 0o700 }); }
      catch (error) {
        if ((error as NodeJS.ErrnoException).code === "EEXIST") throw new AgentFailure("session_exists", "New session already exists; refusing to overwrite it");
        throw error;
      }
      // SDK 首个assistant到达前不落盘；预写header会触发0.73.0重复header缺陷。
      manager = SessionManager.open(transcriptPath, directory, cwd);
      const header = manager.getHeader();
      Object.assign(header!, { cairn: { project_id: request.project_id, session: request.session } });
      const raw = `${JSON.stringify(header)}\n`;
      manifest = { protocol: "cairn-pi-session-v1", project_id: request.project_id, session: request.session, sdk_session_id: manager.getSessionId(), cwd, phase_family: phaseFamily, binding_sha256: bindingDigest, last_task_run_id: request.task_run_id, sha256: digest(raw) };
      await writeFile(manifestPath, JSON.stringify(manifest), { flag: "wx", mode: 0o600 });
    } else {
      if ((await lstat(directory)).isSymbolicLink()) throw new AgentFailure("session_corrupt", "Session directory cannot be a symlink");
      const parsed: unknown = JSON.parse(await regularFile(manifestPath));
      // manifest 的初始绑定只证明对话归属；续作的新绑定须由本次执行来源显式确认。
      if (!object(parsed) || parsed.protocol !== "cairn-pi-session-v1" || parsed.project_id !== request.project_id || parsed.session !== request.session || parsed.cwd !== cwd || parsed.phase_family !== phaseFamily || !/^[a-f0-9]{64}$/.test(parsed.binding_sha256 as string) || typeof parsed.sha256 !== "string" || typeof parsed.sdk_session_id !== "string" || (parsed.current_binding_sha256 !== undefined && !/^[a-f0-9]{64}$/.test(parsed.current_binding_sha256 as string)) || (parsed.last_task_run_id !== undefined && (typeof parsed.last_task_run_id !== "string" || parsed.last_task_run_id.length === 0))) {
        throw new AgentFailure("session_ownership", "Session manifest ownership or version mismatch");
      }
      manifest = parsed as unknown as Manifest;
      if (continuation && manifest.last_task_run_id && continuation.source_task_run_id !== manifest.last_task_run_id) {
        throw new AgentFailure("session_ownership", "Continuation source does not match the latest session writer");
      }
      if (bindingDigest !== (manifest.current_binding_sha256 ?? manifest.binding_sha256) && !continuation) {
        throw new AgentFailure("session_ownership", "Session execution binding mismatch");
      }
      const raw = await regularFile(transcriptPath);
      if (digest(raw) !== manifest.sha256) throw new AgentFailure("session_corrupt", "Session transcript digest mismatch");
      validateTranscript(raw, manifest);
      const initialized = raw.trimEnd().split("\n").some((line) => { const entry = JSON.parse(line); return entry.type === "message" && entry.message?.role === "assistant"; });
      if (!initialized) throw new AgentFailure("session_corrupt", "Session never initialized an assistant turn; refusing silent reopening");
      manager = SessionManager.open(transcriptPath, directory, cwd);
    }
    if (manager.getSessionId() !== manifest.sdk_session_id) throw new AgentFailure("session_corrupt", "SDK changed the session identity");
    if (continuation) {
      const previousBinding = manifest.current_binding_sha256 ?? manifest.binding_sha256;
      if (previousBinding !== bindingDigest) {
        // 原绑定与历史消息不变；新执行段只描述本次来源和输入，不代替 Server 的归属验证。
        manager.appendCustomEntry("cairn_execution_segment_v1", {
          source_task_run_id: continuation.source_task_run_id,
          task_run_id: request.task_run_id,
          execution_generation: continuation.execution_generation,
          previous_binding_sha256: previousBinding,
          binding_sha256: bindingDigest,
        });
        manifest.current_binding_sha256 = bindingDigest;
      }
    }
    // 只读交付仍写入同一对话，后续续作须指向实际最后一个 TaskRun。
    manifest.last_task_run_id = request.task_run_id;
    return {
      manager, directory,
      async finish() {
        try {
          try { await lstat(transcriptPath); }
          catch (error) {
            if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
            const snapshot = [manager.getHeader(), ...manager.getEntries()].map((entry) => JSON.stringify(entry)).join("\n") + "\n";
            await writeFile(transcriptPath, snapshot, { flag: "wx", mode: 0o600 });
          }
          const raw = await regularFile(transcriptPath);
          validateTranscript(raw, manifest);
          manifest.sha256 = digest(raw);
          await writeFile(`${manifestPath}.tmp`, JSON.stringify(manifest), { mode: 0o600 });
          await rename(`${manifestPath}.tmp`, manifestPath);
        } finally { await release(); }
      },
    };
  } catch (error) {
    await release();
    if (error instanceof AgentFailure) throw error;
    throw new AgentFailure("session_corrupt", "Session is missing, unreadable, or malformed");
  }
}
