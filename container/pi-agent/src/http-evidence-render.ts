import { execFile, spawn } from "node:child_process";
import { promisify } from "node:util";
import { createHash } from "node:crypto";
import { chmod, mkdtemp, readFile, rename, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import type { TSchema } from "typebox";
import { AgentFailure, object, type JsonObject } from "./protocol.js";
import { readHttpTraffic, type HttpTrafficBinding } from "./http-flows.js";

export interface FrozenHttpRenderCapability { frozen_materials_path: string; manifest_path: string }
export interface HttpRenderInput {
  source: JsonObject;
  request_text: string | null;
  response_text: string | null;
  notice: string;
}
export const RENDER_HTTP_EVIDENCE_PARAMETERS = {
  type: "object", additionalProperties: false,
  properties: {
    source_key: { type: "string", minLength: 1, maxLength: 300, description: "Report only: exact key in the frozen http_evidence_render_sources index." },
    source: { type: "object", additionalProperties: false, properties: {
      kind: { type: "string", enum: ["flow", "artifact"] }, flow_id: { type: "string" }, artifact_id: { type: "string" },
      sha256: { type: "string" }, side: { type: "string", enum: ["request", "response"] },
      offset: { type: "integer", minimum: 0 }, length: { type: "integer", minimum: 1, maximum: 65536 },
      excerpts: { type: "array", maxItems: 2, items: { type: "object", additionalProperties: false, properties: {
        side: { type: "string", enum: ["request", "response"] }, pointer: { type: "string" },
        representation: { type: "string", const: "http-evidence-v1" }, revision: { type: "integer", minimum: 1 }, sha256: { type: "string" },
      }, required: ["side", "pointer", "representation", "revision", "sha256"] } },
    }, required: ["kind"] },
    historical: { type: "boolean" },
  },
} as TSchema;

function escape(value: string): string {
  return value.replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]!);
}
function rows(text: string | null): string[] {
  if (text === null) return ["未记录此侧材料；不推测、不补造。"];
  const output: string[] = [];
  for (const [index, line] of text.split(/\r\n|\n|\r/).entries()) {
    let part = "", width = 0, first = true;
    for (const char of line) {
      const size = char.codePointAt(0)! > 255 ? 2 : char === "\t" ? 4 : 1;
      if (width + size > 68) { output.push(`${first ? index + 1 : ""}  ${part}`); first = false; part = ""; width = 0; }
      part += char; width += size;
    }
    output.push(`${first ? index + 1 : ""}  ${part}`);
  }
  return output;
}
export function httpEvidencePages(input: HttpRenderInput): { html: string[]; truncated: boolean } {
  if (!object(input.source) || typeof input.notice !== "string" || [input.request_text, input.response_text].some((text) => text !== null && (typeof text !== "string" || Buffer.byteLength(text) > 65536))) {
    throw new AgentFailure("http_image_source_invalid", "Screenshot source was not a bounded approved representation");
  }
  const request = rows(input.request_text), response = rows(input.response_text);
  const desired = Math.max(Math.ceil(request.length / 40), Math.ceil(response.length / 40), 1);
  const count = Math.min(desired, 4), truncated = desired > count;
  const source = input.source;
  const identity = `${source.kind === "flow" ? `flow ${source.flow_id} · revision ${source.revision}` : `Artifact ${source.artifact_id}`} · ${source.representation}`;
  const range = source.kind === "flow" ? JSON.stringify(source.selections) : `UTF-8 字节 ${source.offset}–${Number(source.offset) + Number(source.length)} / ${source.size}`;
  const html = Array.from({ length: count }, (_, index) => `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; font-src 'none'; connect-src 'none'; script-src 'none'; base-uri 'none'; form-action 'none'"><style>
    *{box-sizing:border-box}body{margin:0;padding:24px;background:#fff;color:#202124;font-family:sans-serif}h1{font-size:20px;margin:0 0 8px}p{font-size:12px;margin:5px 0;overflow-wrap:anywhere}.panes{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:14px}section{min-width:0;border:1px solid #d8dce2}h2{font-size:15px;margin:0;padding:10px 14px;background:#f3f5f7;border-bottom:1px solid #d8dce2}pre{margin:0;padding:12px;font:14px/19px monospace;white-space:pre;tab-size:4}.notice{color:#6b4400}
    </style><h1>HTTP 数据包展示 · 第 ${index + 1}/${count} 页</h1><p>${escape(identity)}</p><p>${escape(range)}</p><p>${escape(input.notice)}</p><p class="notice">${truncated ? "内容超过四页，本图不完整；请沿来源读取完整原件。" : "仅展示已准出的原文或选段；完整证据以来源为准。"}</p><div class="panes"><section><h2>请求</h2><pre>${escape(request.slice(index * 40, (index + 1) * 40).join("\n"))}</pre></section><section><h2>响应</h2><pre>${escape(response.slice(index * 40, (index + 1) * 40).join("\n"))}</pre></section></div></html>`);
  return { html, truncated };
}

async function chromium(htmlPath: string, pngPath: string, profile: string, signal?: AbortSignal, timeoutMilliseconds = 10000): Promise<void> {
  signal?.throwIfAborted();
  await new Promise<void>((resolve, reject) => {
    // 固定 CLI 入口，不借用嵌套 npm 包。CSP 禁止文档脚本与资源，无可用代理隔离浏览器网络。
    const child = spawn("/usr/bin/chromium", ["--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--disable-background-networking", "--disable-component-update", "--disable-extensions", "--disable-sync", "--no-first-run", "--no-default-browser-check", "--disable-breakpad", "--disable-features=MediaRouter", "--host-resolver-rules=MAP * ~NOTFOUND", "--proxy-server=http://127.0.0.1:9", "--proxy-bypass-list=<-loopback>", "--window-size=1440,1000", "--hide-scrollbars", `--user-data-dir=${profile}`, `--screenshot=${pngPath}`, `file://${htmlPath}`], { stdio: "ignore", detached: true });
    const kill = () => { if (child.pid) { try { process.kill(-child.pid, "SIGKILL"); } catch (error) { if ((error as NodeJS.ErrnoException).code !== "ESRCH") throw error; } } };
    const abort = () => kill();
    const timer = setTimeout(kill, Math.max(1, timeoutMilliseconds));
    signal?.addEventListener("abort", abort, { once: true });
    child.once("error", () => { clearTimeout(timer); signal?.removeEventListener("abort", abort); reject(new AgentFailure("http_image_renderer_unavailable", "Offline Chromium is unavailable")); });
    child.once("close", (code) => { clearTimeout(timer); signal?.removeEventListener("abort", abort); kill(); code === 0 ? resolve() : reject(new AgentFailure("http_image_render_failed", "Offline rendering failed or timed out; original evidence is unchanged")); });
  });
}

export async function renderHttpEvidence(input: HttpRenderInput, directory: string, signal?: AbortSignal): Promise<{ paths: string[]; truncated: boolean }> {
  const pages = httpEvidencePages(input);
  // 缺少中文字体会把证据变成方框，不能把这种 PNG 当作可读成功发布。
  try {
    const fonts = await promisify(execFile)("/usr/bin/fc-list", [":lang=zh", "family"], { timeout: 3000, maxBuffer: 65536 });
    if (!fonts.stdout.trim()) throw new Error("missing font");
  } catch {
    throw new AgentFailure("http_image_font_unavailable", "Offline screenshot requires an installed Chinese font; original evidence remains deliverable");
  }
  const paths: string[] = [];
  const deadline = Date.now() + 35000;
  for (const [index, html] of pages.html.entries()) {
    if (Date.now() > deadline) throw new AgentFailure("http_image_render_timeout", "Optional image rendering exceeded its time budget");
    const htmlPath = join(directory, `${index + 1}.html`), pngPath = join(directory, `${index + 1}.png`);
    await writeFile(htmlPath, html, { mode: 0o600 });
    try { await chromium(htmlPath, pngPath, join(directory, "profile"), signal, Math.min(10000, deadline - Date.now())); }
    finally { await rm(htmlPath, { force: true }); }
    const png = await readFile(pngPath);
    if (png.length > 2 * 1024 * 1024 || png.subarray(0, 8).toString("hex") !== "89504e470d0a1a0a" || png.readUInt32BE(16) !== 1440 || png.readUInt32BE(20) !== 1000) throw new AgentFailure("http_image_png_invalid", "Renderer did not produce a bounded PNG");
    await chmod(pngPath, 0o600);
    paths.push(pngPath);
  }
  await rm(join(directory, "profile"), { recursive: true, force: true });
  return { paths, truncated: pages.truncated };
}

export class HttpEvidenceRenderer {
  private attempts = 0;
  private images: JsonObject[] = [];
  private unavailable = false;
  constructor(private traffic: HttpTrafficBinding | undefined, private frozen: FrozenHttpRenderCapability | undefined, private directory: string) {}
  private async saveManifest(images: JsonObject[]): Promise<boolean> {
    if (!this.frozen) return true;
    try {
      await writeFile(`${this.frozen.manifest_path}.tmp`, JSON.stringify({ version: 1, images, unavailable: this.unavailable }), { mode: 0o600 });
      await rename(`${this.frozen.manifest_path}.tmp`, this.frozen.manifest_path);
      return true;
    } catch { return false; }
  }
  async render(params: JsonObject, signal?: AbortSignal): Promise<JsonObject> {
    let directory: string | undefined;
    let outcome: JsonObject | undefined;
    try {
      if (++this.attempts > 8) throw new AgentFailure("http_image_attempt_limit", "Optional image attempt limit reached");
      let input: HttpRenderInput;
      if (this.frozen) {
        if (typeof params.source_key !== "string" || params.source !== undefined || params.historical !== undefined) throw new AgentFailure("http_image_frozen_source_required", "Report screenshots require a frozen source key");
        const packet = JSON.parse(await readFile(this.frozen.frozen_materials_path, "utf8"));
        const value = packet.http_evidence_render_sources?.[params.source_key];
        if (!object(value)) throw new AgentFailure("http_image_frozen_source_missing", "No approved frozen image source exists for this key");
        input = value as unknown as HttpRenderInput;
      } else {
        if (!this.traffic || !object(params.source) || params.source_key !== undefined) throw new AgentFailure("http_image_source_required", "Screenshot requires an existing flow or Artifact identity");
        input = await readHttpTraffic(this.traffic, "prepare_http_evidence_image", params, signal) as unknown as HttpRenderInput;
      }
      directory = await mkdtemp(join(this.directory, "http-evidence-"));
      const rendered = await renderHttpEvidence(input, directory, signal);
      const images: JsonObject[] = [], failures: string[] = [];
      for (const [index, path] of rendered.paths.entries()) {
        const png = await readFile(path);
        const page = { page: index + 1, page_count: rendered.paths.length, truncated: rendered.truncated };
        if (this.frozen) {
          const image = { source_key: params.source_key, path, ...page, sha256: createHash("sha256").update(png).digest("hex"), size: png.length };
          images.push(image);
        } else {
          try {
            const artifact = await readHttpTraffic(this.traffic!, "publish_http_evidence_image", { ...params, ...page, expected_source: input.source, content_base64: png.toString("base64") }, signal);
            images.push({ artifact_id: artifact.id, ...page });
          } catch { failures.push(`page_${index + 1}_publish_failed`); }
        }
      }
      if (this.frozen) {
        const all = [...this.images, ...images];
        if (!await this.saveManifest(all)) throw new AgentFailure("http_image_manifest_unavailable", "Optional image manifest could not be saved");
        this.images = all;
        directory = undefined;
      }
      return outcome = { status: failures.length ? images.length ? "partial" : "unavailable" : "ready", optional: true, source: input.source,
        images: images.map(({ path: _path, ...image }) => image), failures,
        notice: this.frozen ? "Images are optional local derivatives awaiting publication, not evidence or a submission requirement." : "Only published images are referenced; do not add these derived PNGs to evidence_items or artifact declarations." };
    } catch (error) {
      this.unavailable = true;
      const feedbackSaved = await this.saveManifest(this.images);
      return outcome = { status: "unavailable", optional: true, reason: error instanceof AgentFailure ? error.code : "http_image_unavailable", feedback_saved: feedbackSaved, notice: "截图不可用；继续交付正文与原始证据，不重新请求目标。" };
    } finally {
      if (directory) {
        try { await rm(directory, { recursive: true, force: true }); }
        catch { if (outcome) outcome.cleanup_unavailable = true; }
      }
    }
  }
}
