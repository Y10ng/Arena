"""仅供隔离候选镜像运行的合成 Web 烟测；不调用模型，也不验证漏洞召回率。"""

from __future__ import annotations

import base64
import configparser
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pwd
import re
import signal
import shutil
import subprocess
import tempfile
import threading
import xml.etree.ElementTree as ET


WORDLIST_HASHES = {
    "common.txt": "47fb86ca6fb3f97e5491161581900d6d99851fb16764eacddf16aa85617c956a",
    "parameters.txt": "91f7e457415ffd0e4278d13887804036b62240a135a022a6fd6e831a7151899f",
    "xss.txt": "1b7ec661af015daa7d8d67d3c9032099275e346b503ec8686f8b7df4dca3ca69",
    "sqli.txt": "09e5930609e4ce0663505168432796881f493ec0e63643300166d36ed57d40ec",
}
TEMPLATE = """id: cairn-loopback-smoke
info:
  name: Cairn synthetic loopback response
  author: cairn
  severity: info
http:
  - method: GET
    path:
      - "{{BaseURL}}/probe"
    matchers-condition: and
    matchers:
      - type: status
        status: [200]
      - type: word
        part: body
        words: [CAIRN_SYNTHETIC_NUCLEI_OK]
"""


def main():
    import requests
    from bs4 import BeautifulSoup
    import yaml

    assert pwd.getpwuid(os.geteuid()).pw_name == "kali", "smoke must run as kali"
    assert Path.cwd() == Path("/home/kali/workspace")
    for legacy_command in ("claude", "codex", "pi"):
        assert shutil.which(legacy_command) is None, legacy_command
    assert not Path("/usr/local/lib/node_modules/@jshookmcp/jshook").exists()
    # 备用工具只检查安装，不为验收启动MCP服务或额外浏览器。
    for package, command, version in (("@playwright/mcp", "playwright-mcp", "0.0.81"),
                                      ("chrome-devtools-mcp", "chrome-devtools-mcp", "1.9.0")):
        metadata = Path("/usr/local/lib/node_modules") / package / "package.json"
        assert json.loads(metadata.read_text())["version"] == version
        assert shutil.which(command), command
    evidence = Path(tempfile.mkdtemp(prefix="cairn-web-smoke-", dir=Path.cwd()))
    (evidence / "workspace-write.txt").write_text("kali workspace writable\n")
    report = {"status": "running", "evidence_directory": str(evidence)}
    command_number = 0

    def run(*command, expected=0, timeout=20, input_text=None):
        nonlocal command_number
        command_number += 1
        output_file = evidence / f"command-{command_number:02d}.log"
        # 输出写文件而不是 PIPE，子进程持有描述符也不会使 communicate 无限等待。
        with output_file.open("wb") as output, tempfile.TemporaryFile() as command_input:
            if input_text is not None:
                command_input.write(input_text.encode())
                command_input.seek(0)
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                                       stdin=command_input if input_text is not None else subprocess.DEVNULL,
                                       start_new_session=True)
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
                raise AssertionError(f"command timed out: {command}")
        text = output_file.read_text(errors="replace")
        print("SMOKE_COMMAND=" + json.dumps({"argv": command, "exit": code,
                                               "output": text[-2000:]}, ensure_ascii=False), flush=True)
        assert code == expected, f"{command}: exit={code}\n{text[-4000:]}"
        return text

    assert run("cairn-agent", "--version").strip() == "cairn-agent 1.0.0 (pi-coding-agent 0.73.0)"
    assert json.loads(run("cairn-agent", "--self-check")) == {
        "status": "ok", "engine": "pi", "sdk_version": "0.73.0", "offline": True,
        "capabilities": ["execution-checkpoint-spool-v1"],
    }
    assert run("node", "--version").strip() == "v24.16.0"
    versions = json.loads(run("node", "-e", """
const path = require('node:path');
const cli = '/usr/local/lib/node_modules/@playwright/cli/package.json';
const core = require.resolve('playwright-core/package.json', {paths: [path.dirname(cli)]});
console.log(JSON.stringify({cli: require(cli).version, core: require(core).version}));
"""))
    assert versions == {"cli": "0.1.15", "core": "1.62.0-alpha-2026-06-29"}, versions
    # CLI 的显示版本可能来自 core；包元数据分别锁定二者，命令本身仍需能离线启动。
    assert run("playwright-cli", "--version").strip()
    # 不启动浏览器；真实浏览器与多会话生命周期由既有独立验收负责。
    assert "Chromium" in run("chromium", "--version")
    for name, arguments, version in [
        ("katana", ("-duc", "-version"), "1.6.1"),
        ("nuclei", ("-duc", "-version"), "3.7.1"),
        ("dalfox", ("version",), "2.12.0"),
        ("rg", ("--version",), "15.1.0"),
        ("fd", ("--version",), "10.4.2"),
    ]:
        assert re.search(r"(?<![\d.])" + re.escape(version) + r"(?![\d.])", run(name, *arguments)), name
    report["versions"] = {"node": "24.16.0", **versions, "pi": "1.0.0 / SDK 0.73.0",
                          "katana": "1.6.1", "nuclei": "3.7.1", "dalfox": "2.12.0",
                          "rg": "15.1.0", "fd": "10.4.2"}

    wordlists = Path("/opt/cairn/wordlists/web")
    for name, digest in WORDLIST_HASHES.items():
        assert hashlib.sha256((wordlists / name).read_bytes()).hexdigest() == digest, name
    assert (wordlists / "SOURCE").read_text().strip() == "SecLists 8f4c1846cdb02a7024bd09decce1afc1ef5df46b"
    templates = Path("/home/kali/.local/nuclei-templates")
    assert (templates / "CAIRN_REVISION").read_text().strip() == "2df013c4fc9e8a7b5cbb8a52c77f8870f61590b9"
    installed_template = next(templates.glob("http/**/*.yaml"))
    assert yaml.safe_load(installed_template.read_text())["id"]
    config = yaml.safe_load(Path("/home/kali/.config/nuclei/config.yaml").read_text())
    assert config["disable-update-check"] is True
    assert config["update-template-dir"] == str(templates)
    report["assets"] = {"wordlist_sha256": WORDLIST_HASHES,
                        "readable_installed_template": str(installed_template),
                        "nuclei_revision": "2df013c4fc9e8a7b5cbb8a52c77f8870f61590b9"}

    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            body = self.rfile.read(min(int(self.headers.get("Content-Length", "0")), 4096))
            seen.append({"method": self.command, "path": self.path, "body": body.decode()})
            pages = {
                "/": '<html><title>Cairn smoke</title><a href="/linked">local link</a></html>',
                "/linked": "CAIRN_LINKED_OK", "/found": "CAIRN_FFUF_OK",
                "/probe": "CAIRN_SYNTHETIC_NUCLEI_OK",
            }
            if self.command == "POST" and self.path == "/replay" and body == b"message=cairn-synthetic":
                status, response = 200, "CAIRN_REPLAY_OK:message=cairn-synthetic"
            else:
                status, response = (200, pages[self.path]) if self.path in pages else (404, "not found")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(response.encode())))
            self.send_header("X-Cairn-Smoke", "synthetic-only")
            self.end_headers()
            self.wfile.write(response.encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        response = requests.get(url, timeout=3)
        assert response.status_code == 200
        assert BeautifulSoup(response.text, "html.parser").title.string == "Cairn smoke"
        report["python_imports"] = ["requests", "bs4", "yaml"]

        body_file, headers_file = evidence / "replay-response.txt", evidence / "replay-headers.txt"
        payload = evidence / "replay-body.txt"
        payload.write_text("message=cairn-synthetic")
        run("curl", "--noproxy", "*", "--fail", "--silent", "--show-error",
            "--connect-timeout", "2", "--max-time", "5", "--request", "POST",
            "--header", "Content-Type: application/x-www-form-urlencoded", "--data-binary", f"@{payload}",
            "--dump-header", str(headers_file), "--output", str(body_file), url + "/replay")
        assert body_file.read_text() == "CAIRN_REPLAY_OK:message=cairn-synthetic"
        assert "X-Cairn-Smoke: synthetic-only" in headers_file.read_text()
        report["curl_replay"] = {"body": body_file.read_text(), "headers": headers_file.read_text()}

        small_dictionary = evidence / "ffuf-words.txt"
        small_dictionary.write_text("found\ncairn-not-found\n")
        ffuf_result = evidence / "ffuf.json"
        run("ffuf", "-noninteractive", "-w", str(small_dictionary), "-u", url + "/FUZZ",
            "-mc", "200", "-t", "1", "-rate", "5", "-timeout", "3", "-maxtime", "10",
            "-of", "json", "-o", str(ffuf_result))
        matches = json.loads(ffuf_result.read_text())["results"]
        assert [(item["url"], item["status"]) for item in matches] == [(url + "/found", 200)]
        report["ffuf"] = [{"url": item["url"], "status": item["status"]} for item in matches]

        crawl = evidence / "katana.txt"
        run("katana", "-duc", "-silent", "-u", url, "-d", "2", "-c", "1", "-p", "1",
            "-rl", "5", "-timeout", "3", "-retry", "0", "-o", str(crawl))
        assert url + "/linked" in crawl.read_text().splitlines()
        assert any(item["path"] == "/linked" for item in seen), "crawler must fetch the link"
        report["katana"] = crawl.read_text().splitlines()

        synthetic_template = evidence / "synthetic-nuclei.yaml"
        synthetic_template.write_text(TEMPLATE)
        nuclei_result = evidence / "nuclei.jsonl"
        # 仅执行无 OOB/代码/外部引用的合成模板；禁止更新检查和 interactsh。
        run("nuclei", "-duc", "-ni", "-no-stdin", "-silent", "-u", url,
            "-t", str(synthetic_template), "-timeout", "3", "-retries", "0", "-c", "1",
            "-rl", "5", "-jsonl", "-o", str(nuclei_result), timeout=40)
        findings = [json.loads(line) for line in nuclei_result.read_text().splitlines() if line.strip()]
        assert any(item["template-id"] == "cairn-loopback-smoke" and item["matched-at"] == url + "/probe"
                   for item in findings), findings
        report["nuclei"] = [{"template-id": item["template-id"], "matched-at": item["matched-at"]}
                            for item in findings]

        nmap_result = evidence / "nmap.xml"
        run("nmap", "-sT", "-Pn", "-n", "--max-retries", "0", "--host-timeout", "10s",
            "-p", str(server.server_port), "-oX", str(nmap_result), "127.0.0.1")
        port = ET.parse(nmap_result).find(f".//port[@portid='{server.server_port}']")
        assert port is not None and port.attrib["protocol"] == "tcp" and port.find("state").attrib["state"] == "open"
        report["nmap"] = {"address": "127.0.0.1", "port": server.server_port, "state": "open", "scan": "connect"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        (evidence / "http-requests.json").write_text(json.dumps(seen))
        print("SMOKE_HTTP=" + json.dumps(seen), flush=True)

    assert re.search(r"\d+\.\d+", run("sqlmap", "--version"))
    assert "--batch" in run("sqlmap", "-h")
    assert "--skip-bav" in run("dalfox", "url", "--help")
    assert "--url" in run("dirsearch", "--help")
    # 这两项只验 CLI/依赖可用性，不声称 SQLi/XSS 漏洞召回，也不向工具提供目标。
    report["sqlmap_dalfox"] = "version/help only; NOT SQLi/XSS vulnerability recall validation"

    wrapper = Path("/usr/local/bin/jwt_tool").read_text()
    assert "/opt/cairn/jwt-venv/bin/python /opt/cairn/jwt_tool/jwt_tool.py" in wrapper
    assert Path("/opt/cairn/jwt_tool/CAIRN_REVISION").read_text().strip() == "3bc7407cf2222d6a821dcc19c776e5a1b1cb9a9b"

    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    jwt_home = Path.home() / ".jwt_tool"
    assert not jwt_home.exists(), "image must not contain initialized JWT configuration or keys"
    # 固定上游首次仅初始化并退出 1；精确验证该状态，不吞掉任意非零退出。
    initialized = run("jwt_tool", "--bare", expected=1, timeout=40)
    jwt_config = configparser.ConfigParser(interpolation=None)
    assert jwt_config.read(jwt_home / "jwtconf.ini")
    assert jwt_config["services"]["jwt_tool_version"] == "2.3.0"
    assert "Traceback" not in initialized
    for key, filename in (("wordlist", "jwt-common.txt"), ("commonheaders", "common-headers.txt"),
                          ("commonpayloads", "common-payloads.txt")):
        resource = Path(jwt_config["input"][key])
        assert resource == Path("/opt/cairn/jwt_tool") / filename
        assert resource.read_text().strip(), resource
    dictionary = Path(jwt_config["input"]["wordlist"])
    secret = next(line.strip() for line in dictionary.read_text().splitlines()
                  if re.fullmatch(r"[A-Za-z0-9_-]{1,20}", line.strip()))
    message = encode({"alg": "HS256", "typ": "JWT"}) + "." + encode({"cairn_smoke": "offline-decode-ok"})
    signature = base64.urlsafe_b64encode(hmac.digest(secret.encode(), message.encode(), "sha256")).rstrip(b"=").decode()
    token = message + "." + signature
    # 固定源码正常解码同样在主入口末尾退出 1，需校验解码内容而非改写工具退出语义。
    decoded = run("jwt_tool", token, expected=1)
    assert "Traceback" not in decoded
    assert 'alg = "HS256"' in decoded and 'typ = "JWT"' in decoded
    assert 'cairn_smoke = "offline-decode-ok"' in decoded
    # 此工具的成功 action 也退出 1，必须同时校验成功内容；只用自制 token 和本地字典。
    cracked = run("jwt_tool", token, "-C", "-d", str(dictionary), expected=1)
    uncolored = re.sub(r"\x1b\[[0-9;]*m", "", cracked)
    assert f"[+] {secret} is the CORRECT key!" in uncolored
    claims = run("jwt_tool", token, "-b", "-M", "cc", expected=1, input_text="Y\n")
    assert "Traceback" not in claims
    mutated = []
    for candidate in re.findall(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*", claims):
        try:
            parts = candidate.split(".")[:2]
            pair = [json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))) for part in parts]
        except (ValueError, UnicodeError):
            continue
        if all(isinstance(value, dict) for value in pair):
            mutated.append(pair)
    header_names = set(Path(jwt_config["input"]["commonheaders"]).read_text().splitlines())
    payload_names = set(Path(jwt_config["input"]["commonpayloads"]).read_text().splitlines())
    assert any((set(header) - {"alg", "typ"}) & header_names for header, _ in mutated)
    assert any((set(payload) - {"cairn_smoke"}) & payload_names for _, payload in mutated)
    report["jwt_tool"] = "runtime initialization, synthetic decode, local HS256 dictionary and offline common claims"
    assert str(body_file) in run("rg", "-l", "-F", "CAIRN_REPLAY_OK", str(body_file)).splitlines()
    assert str(body_file) in run("fd", "--color", "never", "--type", "f", "^replay-response\\.txt$", str(evidence)).splitlines()
    report["rg_fd"] = "saved curl evidence found by content and filename"

    # 独立 socket 与精确 session 名，验证关闭目标时相似名称会话仍然存活；不用 kill-server。
    tmux = ("tmux", "-S", str(evidence / "tmux.sock"))
    run(*tmux, "new-session", "-d", "-s", "smoke-other", "sleep 60")
    try:
        run(*tmux, "new-session", "-d", "-s", "smoke", "sleep 60")
        run(*tmux, "has-session", "-t", "=smoke")
        run(*tmux, "kill-session", "-t", "=smoke")
        run(*tmux, "has-session", "-t", "=smoke", expected=1)
        run(*tmux, "has-session", "-t", "=smoke-other")
    finally:
        run(*tmux, "kill-session", "-t", "=smoke-other")
    run(*tmux, "has-session", "-t", "=smoke-other", expected=1)
    report["tmux"] = "exact target stopped; similarly named session survived then stopped"
    report["status"] = "ok"
    print("WEB_IMAGE_SMOKE=" + json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
