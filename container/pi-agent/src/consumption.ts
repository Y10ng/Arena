import { constants, openSync, closeSync, fstatSync, fsyncSync, writeSync } from "node:fs";
import type { Usage } from "@mariozechner/pi-ai";

type Counts = Record<string, number | null>;
type ConsumptionEvent = { response_seq: number; revision: number; occurred_at: string;
  time_basis: "observed"; measurement: string; usage: Counts; missing_responses?: number };

/** 单次 SDK 响应独立编号；流内累计值覆盖同一响应，不与下一响应相加。 */
export class ConsumptionSpool {
  private seq = -1;
  private revision = 0;
  private occurredAt = "";
  private previous = "";
  private known: Counts = {};
  private failed = false;

  constructor(private readonly path: string, private readonly onGap?: (event: ConsumptionEvent) => void) {}

  begin() {
    this.seq += 1;
    this.revision = 0;
    this.previous = "";
    this.known = {};
    this.occurredAt = new Date().toISOString();
    this.append("started", {});
  }

  observe(usage: Usage | undefined, final = false) {
    if (this.seq < 0) this.begin();
    const pairs = [["input_tokens", usage?.input], ["output_tokens", usage?.output],
      ["cache_read_tokens", usage?.cacheRead], ["cache_write_tokens", usage?.cacheWrite]] as const;
    // SDK 已拆分缓存且输出已含 reasoning；其默认 0 无法证明上游明确报告零。
    for (const [field, value] of pairs) {
      if (typeof value === "number" && Number.isSafeInteger(value) && value > 0) {
        this.known[field] = Math.max(this.known[field] ?? 0, value);
      }
    }
    const key = JSON.stringify(this.known);
    if (!final && (key === this.previous || this.failed)) return;
    this.previous = key;
    this.append("sdk_normalized", this.known);
  }

  private append(measurement: string, usage: Counts) {
    const event: ConsumptionEvent = { response_seq: this.seq, revision: this.revision++,
      occurred_at: this.occurredAt, time_basis: "observed", measurement, usage: { ...usage } };
    if (!this.failed) {
      try {
        const line = Buffer.from(JSON.stringify(event) + "\n");
        const fd = openSync(this.path, constants.O_WRONLY | constants.O_APPEND | constants.O_NOFOLLOW);
        try {
          const stat = fstatSync(fd);
          if (!stat.isFile() || stat.size + line.length > 16 * 1024 * 1024) throw new Error("consumption_spool_invalid");
          let offset = 0;
          while (offset < line.length) {
            const written = writeSync(fd, line, offset, line.length - offset);
            if (written === 0) throw new Error("consumption_spool_write_failed");
            offset += written;
          }
          fsyncSync(fd);
        } finally { closeSync(fd); }
        return;
      } catch {
        // 可能已有不完整行，停写原 spool；缺口通过既有 stderr 审计交给 Dispatcher。
        this.failed = true;
      }
    }
    // 比可能已写入但 fsync 失败的版本更新，仍保留已知数值和明确缺口。
    event.revision = this.revision++;
    event.missing_responses = 1;
    try { this.onGap?.(event); } catch { /* 审计输出不可用也不能改变模型执行。 */ }
  }
}
