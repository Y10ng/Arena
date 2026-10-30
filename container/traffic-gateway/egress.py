"""出口只负责 TCP 传输；代理认证不进入采集进程和测试容器。"""

import asyncio
from contextvars import ContextVar
import json
from pathlib import Path

import pproxy
from pproxy.proto import HTTP, Redir
from pproxy.server import stream_handler


ROOT = Path('/run/cairn-egress')
DIAGNOSTIC = ContextVar('diagnostic')


class TunnelError(Exception):
    pass


class CheckedHTTP(HTTP):
    async def connect(self, reader_remote, **kwargs):
        class ReplyReader:
            async def read_until(self, separator):
                reply = await reader_remote.read_until(separator)
                first = reply.split(b'\r\n', 1)[0].split()
                if len(first) < 2 or not first[0].startswith(b'HTTP/'):
                    raise TunnelError('traffic_proxy_handshake_failed')
                if first[1] == b'407':
                    raise TunnelError('traffic_proxy_auth_failed')
                if first[1] != b'200':
                    raise TunnelError('traffic_proxy_tunnel_rejected')
                return reply
        # pproxy 2.7.9 的 HTTP 客户端未校验 CONNECT 状态；只补守卫，不复制协议引擎。
        await super().connect(reader_remote=ReplyReader(), **kwargs)


class TargetRedir(Redir):
    def query_remote(self, sock):
        destination = super().query_remote(sock)
        if destination is None:
            return None
        host, port = destination
        mapping = ROOT / 'names' / host
        if mapping.is_file():
            host = mapping.read_text()
        return host, port


def diagnose(code):
    path = DIAGNOSTIC.get()
    path.write_text(code)
    path.chmod(0o644)


async def handle(reader, writer, **kwargs):
    # 使用本次 collector TCP 源端口关联失败；不写目标内容或上游响应正文。
    path = ROOT / 'diagnostics' / str(writer.get_extra_info('peername')[1])
    path.unlink(missing_ok=True)
    token = DIAGNOSTIC.set(path)
    try:
        await stream_handler(reader, writer, **kwargs)
    finally:
        DIAGNOSTIC.reset(token)


async def main():
    config = json.loads((ROOT / 'private.json').read_text())
    remote = pproxy.Connection(f"{config['mode']}://{config['host']}:{config['port']}")
    if config['mode'] == 'http':
        remote.protos = [CheckedHTTP('')]
    if config.get('username') is not None:
        remote.users = [(config['username'] + ':' + config['password']).encode()]
    open_connection, prepare_connection = remote.open_connection, remote.prepare_connection

    async def connect(*args, **kwargs):
        try:
            return await asyncio.wait_for(open_connection(*args, **kwargs), timeout=10)
        except Exception:
            diagnose('traffic_proxy_connect_failed')
            raise

    async def handshake(*args, **kwargs):
        try:
            return await prepare_connection(*args, **kwargs)
        except Exception as error:
            code = str(error) if isinstance(error, TunnelError) else (
                'traffic_proxy_auth_failed' if str(error) == 'Unknown SOCKS auth' else 'traffic_proxy_handshake_failed')
            diagnose(code)
            raise

    remote.open_connection, remote.prepare_connection = connect, handshake
    # 不启动健康轮询或备用出口；namespace 同时只允许该进程连接指定上游。
    listener = pproxy.Connection('redir://127.0.0.1:9091')
    listener.protos = [TargetRedir('')]
    server = await listener.start_server({'rserver': [remote]}, stream_handler=handle)
    (ROOT / 'ready').touch()
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    asyncio.run(main())
