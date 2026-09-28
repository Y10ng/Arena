"""定点修正固定 CLI 的正文读取入口，未完成请求不进入无截止的等待。"""

import json
from pathlib import Path
import sys


CORE_VERSION = "1.62.0-alpha-2026-06-29"
ORIGINAL = '''  const contentType = httpResponse.headers()["content-type"];
  if (isTextualMimeType(contentType ?? "")) {'''
REPLACEMENT = '''  // 响应头和数据到达不等于请求完成；不让正文取证阻塞后续浏览器操作。
  if (request2.timing().responseEnd < 0)
    throw new Error("response_body_incomplete: request has not finished; keep available headers and retry only after completion. Do not replay the request to fabricate captured evidence.");
''' + ORIGINAL


def patch_response_body(core: Path) -> None:
    version = json.loads((core / "package.json").read_text())["version"]
    bundle = core / "lib" / "coreBundle.js"
    source = bundle.read_text()
    # 仅支持已验收的固定源码；漂移或重复执行时先失败，不能静默部分修补。
    if version != CORE_VERSION or source.count(ORIGINAL) != 1 or "response_body_incomplete:" in source:
        raise ValueError("unexpected Playwright response body source")
    bundle.write_text(source.replace(ORIGINAL, REPLACEMENT, 1))


if __name__ == "__main__":
    patch_response_body(Path(sys.argv[1]))
