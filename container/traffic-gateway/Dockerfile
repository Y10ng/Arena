FROM python:3.13-slim-bookworm@sha256:a881a81282cca8c57f97014867fa651def1b3799030cc6b1cff45ed55ba8ef36 AS gateway-dependencies

# 网关单独构建，不使 Worker 扫描工具层失效。
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates iproute2 iptables && \
    rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir 'mitmproxy==12.3.0'

FROM gateway-dependencies AS traffic-gateway
# 出口只增加传输桥，复用既有 mitmproxy 与 Worker 工具层。
RUN python -m pip install --no-cache-dir 'pproxy==2.7.9'
COPY capture.py ingress.py egress.py /opt/cairn/traffic/
ENTRYPOINT ["mitmdump", "--mode", "transparent@127.0.0.1:8080", "--mode", "regular@127.0.0.1:8081", "--mode", "socks5@127.0.0.1:1080", "--set", "confdir=/run/cairn-traffic/ca", "--set", "connection_strategy=lazy", "--set", "flow_detail=0", "--set", "termlog_verbosity=error", "-s", "/opt/cairn/traffic/ingress.py", "-s", "/opt/cairn/traffic/capture.py"]
