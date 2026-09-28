"""仅供隔离容器 loopback 验收的合成登录站点，不读取外部数据或真实凭据。"""

from __future__ import annotations

import json
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs


HTML = """<!doctype html>
<html><head><title>cairn browser acceptance</title>
<script src="/app.js" defer></script></head><body>
<form id="login" action="/login" method="post">
<label>Username <input id="username" name="username" autocomplete="username"></label>
<label>Password <input id="password" name="password" type="password" autocomplete="current-password"></label>
<button type="submit">Sign in</button></form>
<div id="login-state">signed-out</div>
<button id="load-data" type="button">Load protected data</button>
<div id="dynamic-data">not-loaded</div><div id="state">initial</div>
</body></html>
"""

SCRIPT = """window.cairnExternalScript = 'external-js-loaded';
document.getElementById('login').addEventListener('submit', async (event) => {
  event.preventDefault();
  const response = await fetch('/login', {
    method: 'POST', body: new URLSearchParams(new FormData(event.target))
  });
  // 读取业务响应后再更新状态；仅收到成功响应头不足以证明登录结果完整。
  const data = await response.json();
  document.getElementById('login-state').textContent = response.ok && data.user === 'synthetic-user' ? 'signed-in' : 'invalid-credentials';
});
document.getElementById('load-data').addEventListener('click', async () => {
  const response = await fetch('/api/data');
  const data = await response.json();
  document.getElementById('dynamic-data').textContent = response.ok ? data.marker : 'unauthorized';
});
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # 合成站点无需访问日志，避免后台管道或文件影响生命周期验收。
        pass

    def respond(self, status, payload, content_type="application/json", *, cookie=None):
        body = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def authenticated(self):
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        session = cookies.get("cairn_fixture_session")
        return session is not None and session.value == "synthetic-session"

    def do_GET(self):
        if self.path == "/":
            self.respond(200, HTML, "text/html; charset=utf-8")
        elif self.path == "/app.js":
            self.respond(200, SCRIPT, "application/javascript; charset=utf-8")
        elif self.path in {"/api/private", "/api/data"}:
            if not self.authenticated():
                self.respond(401, json.dumps({"error": "unauthorized"}))
            elif self.path == "/api/private":
                self.respond(200, json.dumps({"user": "synthetic-user"}))
            else:
                self.respond(200, json.dumps({"marker": "synthetic-dynamic-data"}))
        else:
            self.respond(404, json.dumps({"error": "not-found"}))

    def do_POST(self):
        if self.path != "/login":
            self.respond(404, json.dumps({"error": "not-found"}))
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        fields = parse_qs(body.decode("utf-8"))
        # 固定的公开合成值只用于验证服务端确实校验表单，不能用于任何真实认证。
        if fields != {"username": ["synthetic-user"], "password": ["synthetic-password"]}:
            self.respond(401, json.dumps({"error": "invalid-credentials"}))
            return
        self.respond(
            200, json.dumps({"user": "synthetic-user"}),
            cookie="cairn_fixture_session=synthetic-session; Path=/; HttpOnly; SameSite=Lax",
        )


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
