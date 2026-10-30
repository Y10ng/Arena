"""按入口拆开代理握手与目标 HTTP，并将目标域名交给选定出口解析。"""

import ipaddress
import json
import re
from pathlib import Path

from mitmproxy import ctx, dns, http
from mitmproxy.proxy import commands, events, layer, mode_specs
from mitmproxy.proxy.layers import modes


NAMES = Path('/run/cairn-egress/names')


class RejectProxy(layer.Layer):
    def __init__(self, context, *, socks: bool):
        super().__init__(context)
        self.socks = socks

    def _handle_event(self, event):
        if isinstance(event, events.Start):
            body = b'traffic_proxy_conflict: use the project-configured proxy endpoint\n'
            response = (b'\x05\xff' if self.socks else b'HTTP/1.1 502 Bad Gateway\r\n'
                        b'Connection: close\r\nContent-Type: text/plain\r\nContent-Length: '
                        + str(len(body)).encode() + b'\r\n\r\n' + body)
            yield commands.SendData(self.context.client, response)
            yield commands.CloseConnection(self.context.client)


def incomplete_http_line(raw):
    return len(raw) < 8192 and re.fullmatch(rb'[A-Z]+(?: [^\r\n]*)?', raw) is not None


class HTTPLineProbe(layer.NextLayer):
    def _ask(self):
        # 复用 NextLayer 的缓冲与关闭处理；只等待疑似 HTTP 首行，不解析代理或 TLS。
        if incomplete_http_line(self.data_client()) and not self.data_server():
            return
        yield from super()._ask()


class Ingress:
    def __init__(self):
        self.names = {}
        self.next_address = int(ipaddress.IPv4Address('198.18.0.1'))

    def load(self, loader):
        if NAMES.exists():
            # 不向宿主 DNS 提交目标域名；透明工具也能使用仅代理侧可解析的名称。
            resolver = ctx.master.addons.get('dns_resolver')
            if resolver:
                ctx.master.addons.remove(resolver)

    def dns_request(self, flow: dns.DNSFlow):
        if not NAMES.exists():
            return
        answers = []
        for question in flow.request.questions:
            if question.type == dns.types.A and question.class_ == dns.classes.IN:
                name = question.name.rstrip('.')
                public = json.loads((NAMES.parent / 'public.json').read_text())
                if name == public['host']:
                    answers.append(dns.ResourceRecord.A(question.name, ipaddress.IPv4Address(public['addresses'][0])))
                    continue
                if name not in self.names:
                    address = str(ipaddress.IPv4Address(self.next_address))
                    self.next_address += 1
                    if self.next_address >= int(ipaddress.IPv4Address('198.20.0.0')):
                        flow.response = flow.request.fail(dns.response_codes.SERVFAIL)
                        return
                    path = NAMES / address
                    path.write_text(name)
                    path.chmod(0o644)
                    self.names[name] = address
                answers.append(dns.ResourceRecord.A(question.name, ipaddress.IPv4Address(self.names[name])))
        flow.response = flow.request.succeed(answers)

    def next_layer(self, data):
        if not NAMES.exists():
            return
        raw = data.data_client()
        if isinstance(data.context.client.proxy_mode, mode_specs.RegularMode) and raw.startswith(b'\x05'):
            # 同一显式入口复用 mitmproxy 的 SOCKS5 层，入站协议不绑定上游协议。
            data.context.client.proxy_mode = mode_specs.ProxyMode.parse('socks5')
            data.context.server.address = None
            data.layer = modes.Socks5Proxy(data.context)
            return
        if not isinstance(data.context.client.proxy_mode, mode_specs.TransparentMode):
            return
        if data.context.server.address:
            host, port = data.context.server.address
            mapping = NAMES / host
            if mapping.is_file():
                # 合成地址只是 namespace 内 DNS 路由，不应成为内层 HTTP 的目标身份。
                data.context.server.address = (mapping.read_text(), port)
        if data.context.client.alpn != b'h2' and incomplete_http_line(raw) and not data.data_server():
            # rawtcp 会把缺少换行的短片判为普通 TCP；先等首行，不能让代理认证绕过 HTTP 边界。
            data.layer = HTTPLineProbe(data.context)
            return
        # 已配置代理由 namespace 分流到显式入口；其他代理不能串联成为不同出口。
        if raw.startswith(b'\x05') or raw.startswith(b'CONNECT ') or re.match(rb'[^\s]+ https?://', raw):
            ctx.log.error('traffic_proxy_conflict')
            data.layer = RejectProxy(data.context, socks=raw.startswith(b'\x05'))

    def requestheaders(self, flow: http.HTTPFlow):
        if (NAMES.exists() and isinstance(flow.client_conn.proxy_mode, mode_specs.TransparentMode)
                and not (flow.request.is_http2 or flow.request.is_http3)
                and (flow.request.method == 'CONNECT' or flow.request.first_line_format == 'absolute')):
            # HTTP/2 的 :authority 不是代理首行；仅拒绝 HTTP/1 分片外层握手，不改写已存原件。
            flow.metadata['cairn_proxy_handshake'] = True
            flow.response = http.Response.make(502, b'traffic_proxy_conflict: use the project-configured proxy endpoint')
            ctx.log.error('traffic_proxy_conflict')
            return
        if isinstance(flow.client_conn.proxy_mode, mode_specs.RegularMode):
            # 这是本地代理入口消费的逐跳认证，不属于尚未形成的目标 HTTP 观察。
            flow.request.headers.pop('Proxy-Authorization', None)


addons = [Ingress()]
