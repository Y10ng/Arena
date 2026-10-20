FROM node:24.16.0-bookworm-slim AS node-distribution
FROM kalilinux/kali-rolling:latest AS worker-node-base

# builder与运行镜像共用同一Node二进制和Kali libc，不复制宿主依赖。
COPY --from=node-distribution /usr/local/ /usr/local/

ARG TARGETARCH
ENV DEBIAN_FRONTEND=noninteractive

RUN printf '%s\n' \
    'deb http://mirrors.aliyun.com/kali kali-rolling main contrib non-free non-free-firmware' \
    > /etc/apt/sources.list
RUN printf '%s\n' \
    '[global]' \
    'index-url = https://pypi.tuna.tsinghua.edu.cn/simple' \
    > /etc/pip.conf
RUN printf '%s\n' \
    'registry=https://registry.npmmirror.com' \
    > /etc/npmrc

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

FROM worker-node-base AS pi-agent-builder
WORKDIR /build/pi-agent
COPY pi-agent/package.json pi-agent/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY pi-agent/tsconfig.json ./
COPY pi-agent/src ./src
RUN npm run build && npm run self-check && npm prune --omit=dev --no-audit --no-fund

FROM worker-node-base AS web-assets
RUN apt-get update && apt-get install -y --no-install-recommends curl tar && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
# 只复制固定revision的必要资产；归档、源码控制目录与下载缓存不进入运行层。
WORKDIR /downloads
COPY web-assets.sha256 ./
RUN set -eux; \
    curl -fL --retry 3 https://codeload.github.com/projectdiscovery/nuclei-templates/tar.gz/2df013c4fc9e8a7b5cbb8a52c77f8870f61590b9 -o nuclei-templates.tar.gz; \
    curl -fL --retry 3 https://codeload.github.com/ticarpi/jwt_tool/tar.gz/3bc7407cf2222d6a821dcc19c776e5a1b1cb9a9b -o jwt-tool.tar.gz; \
    base=https://api.github.com/repos/danielmiessler/SecLists/contents; \
    revision=8f4c1846cdb02a7024bd09decce1afc1ef5df46b; \
    curl -fL --retry 3 -H 'Accept: application/vnd.github.raw+json' "$base/LICENSE?ref=$revision" -o LICENSE; \
    curl -fL --retry 3 -H 'Accept: application/vnd.github.raw+json' "$base/Discovery/Web-Content/common.txt?ref=$revision" -o common.txt; \
    curl -fL --retry 3 -H 'Accept: application/vnd.github.raw+json' "$base/Discovery/Web-Content/burp-parameter-names.txt?ref=$revision" -o parameters.txt; \
    curl -fL --retry 3 -H 'Accept: application/vnd.github.raw+json' "$base/Fuzzing/XSS/robot-friendly/XSS-Jhaddix.txt?ref=$revision" -o xss.txt; \
    curl -fL --retry 3 -H 'Accept: application/vnd.github.raw+json' "$base/Fuzzing/Databases/SQLi/Generic-SQLi.txt?ref=$revision" -o sqli.txt; \
    sha256sum -c web-assets.sha256; \
    mkdir -p /assets/nuclei-templates /assets/jwt_tool /assets/wordlists; \
    tar -xzf nuclei-templates.tar.gz --strip-components=1 -C /assets/nuclei-templates; \
    tar -xzf jwt-tool.tar.gz --strip-components=1 -C /assets/jwt_tool; \
    cp LICENSE common.txt parameters.txt xss.txt sqli.txt /assets/wordlists/; \
    printf '%s\n' 'SecLists 8f4c1846cdb02a7024bd09decce1afc1ef5df46b' > /assets/wordlists/SOURCE; \
    printf '%s\n' '2df013c4fc9e8a7b5cbb8a52c77f8870f61590b9' > /assets/nuclei-templates/CAIRN_REVISION; \
    printf '%s\n' '3bc7407cf2222d6a821dcc19c776e5a1b1cb9a9b' > /assets/jwt_tool/CAIRN_REVISION

FROM worker-node-base AS worker-runtime
# 显式声明Web闭环与进程生命周期依赖，不再由Kali全套元包隐式提供。
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash coreutils procps findutils grep sed gawk util-linux sudo tmux \
    ca-certificates curl wget git unzip tar jq openssl iputils-ping dnsutils \
    python3 python3-venv python3-requests python3-bs4 python3-yaml \
    ncat nmap ffuf dirsearch sqlmap chromium && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    mkdir -p /opt/google/chrome && \
    ln -s /usr/bin/chromium /opt/google/chrome/chrome && \
    useradd -m -s /bin/bash kali && \
    printf '%s\n' 'kali ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/kali && \
    chmod 0440 /etc/sudoers.d/kali

# CLI 0.1.15精确依赖core 1.62.0-alpha-2026-06-29，与定向会话清理协议一致。
RUN npm install -g @playwright/cli@0.1.15 && npm cache clean --force

RUN set -eux; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in amd64|arm64) ;; *) echo "Unsupported architecture: $arch"; exit 1;; esac; \
    mkdir /tmp/cairn-web-tools; cd /tmp/cairn-web-tools; \
    gh_asset() { repo="$1"; tag="$2"; asset="$3"; out="$4"; \
      curl -fL --retry 3 --retry-all-errors --connect-timeout 20 --max-time 3600 "https://github.com/${repo}/releases/download/${tag}/${asset}" -o "$out"; \
    }; \
    gh_asset projectdiscovery/katana v1.6.1 "katana_1.6.1_linux_${arch}.zip" katana.zip; \
    gh_asset projectdiscovery/nuclei v3.7.1 "nuclei_3.7.1_linux_${arch}.zip" nuclei.zip; \
    gh_asset hahwul/dalfox v2.12.0 "dalfox-linux-${arch}.tar.gz" dalfox.tar.gz; \
    unzip -q katana.zip katana; install -m 0755 katana /usr/local/bin/katana; \
    unzip -q nuclei.zip nuclei; install -m 0755 nuclei /usr/local/bin/nuclei; \
    tar -xzf dalfox.tar.gz; install -m 0755 "dalfox-linux-${arch}" /usr/local/bin/dalfox; \
    katana -version >/dev/null; \
    nuclei -version >/dev/null; \
    dalfox version >/dev/null; \
    rm -rf /tmp/cairn-web-tools

# 固定同一 release 的官方资产 ID 与摘要，不依赖 github.com 下载入口的连通性。
RUN set -eux; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      amd64) rust_target="x86_64-unknown-linux-musl"; rg_asset=307306614; fd_asset=370662855; \
        rg_sha=1c9297be4a084eea7ecaedf93eb03d058d6faae29bbc57ecdaf5063921491599; \
        fd_sha=e3257d48e29a6be965187dbd24ce9af564e0fe67b3e73c9bdcd180f4ec11bdde ;; \
      arm64) rust_target="aarch64-unknown-linux-gnu"; rg_asset=307306472; fd_asset=370663239; \
        rg_sha=2b661c6ef508e902f388e9098d9c4c5aca72c87b55922d94abdba830b4dc885e; \
        fd_sha=6c51f7c5446b3338b1e401ff15dc194c590bb2fa64fd43ff3278300f073adec5 ;; \
      *) echo "Unsupported architecture: $arch"; exit 1 ;; \
    esac; \
    mkdir /tmp/cairn-search-tools; cd /tmp/cairn-search-tools; \
    curl -fL --retry 3 --retry-all-errors --connect-timeout 20 --max-time 3600 -H 'Accept: application/octet-stream' "https://api.github.com/repos/BurntSushi/ripgrep/releases/assets/$rg_asset" -o ripgrep.tar.gz; \
    curl -fL --retry 3 --retry-all-errors --connect-timeout 20 --max-time 3600 -H 'Accept: application/octet-stream' "https://api.github.com/repos/sharkdp/fd/releases/assets/$fd_asset" -o fd.tar.gz; \
    printf '%s  %s\n' "$rg_sha" ripgrep.tar.gz "$fd_sha" fd.tar.gz | sha256sum -c -; \
    tar -xzf ripgrep.tar.gz; tar -xzf fd.tar.gz; \
    install -m 0755 "ripgrep-15.1.0-${rust_target}/rg" /usr/local/bin/rg; \
    install -m 0755 "fd-v10.4.2-${rust_target}/fd" /usr/local/bin/fd; \
    rg --version; fd --version; \
    rm -rf /tmp/cairn-search-tools

COPY --from=web-assets /assets/jwt_tool /opt/cairn/jwt_tool
COPY patch-jwt-paths.py /tmp/cairn-patch-jwt-paths.py
# 只修正内置字典路径，不切换调用者cwd，也不在构建时生成共享密钥。
# JWT工具沿用独立依赖集，避免pip覆盖系统Python和dirsearch依赖。
RUN python3 /tmp/cairn-patch-jwt-paths.py /opt/cairn/jwt_tool/jwt_tool.py && \
    rm /tmp/cairn-patch-jwt-paths.py && \
    python3 -m venv /opt/cairn/jwt-venv && \
    /opt/cairn/jwt-venv/bin/pip install --no-cache-dir -r /opt/cairn/jwt_tool/requirements.txt && \
    printf '%s\n' '#!/bin/sh' 'exec /opt/cairn/jwt-venv/bin/python /opt/cairn/jwt_tool/jwt_tool.py "$@"' > /usr/local/bin/jwt_tool && \
    chmod 0755 /usr/local/bin/jwt_tool
COPY --from=web-assets /assets/wordlists /opt/cairn/wordlists/web
COPY --from=web-assets --chown=kali:kali /assets/nuclei-templates /home/kali/.local/nuclei-templates
# 备用MCP只安装不启动；共享系统Chromium，不下载第二套浏览器。
RUN PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install -g --no-audit --no-fund \
    @playwright/mcp@0.0.81 chrome-devtools-mcp@1.9.0 && npm cache clean --force
ENV CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS=1
ENV CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS=1

# 只修补固定 CLI 的正文读取，不改备用 MCP、浏览器启动或会话回收协议。
COPY patch-playwright-response.py /tmp/cairn-patch-playwright-response.py
RUN python3 /tmp/cairn-patch-playwright-response.py /usr/local/lib/node_modules/@playwright/cli/node_modules/playwright-core && \
    node --check /usr/local/lib/node_modules/@playwright/cli/node_modules/playwright-core/lib/coreBundle.js && \
    rm /tmp/cairn-patch-playwright-response.py

USER kali
RUN cd /tmp && PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 playwright-cli install && \
    mkdir -p /home/kali/.config/nuclei /home/kali/workspace && \
    printf '%s\n' 'disable-update-check: true' \
    'update-template-dir: /home/kali/.local/nuclei-templates' > /home/kali/.config/nuclei/config.yaml && \
    cd /home/kali/workspace && git init
ENV TZ=Asia/Shanghai
ENV PLAYWRIGHT_MCP_BROWSER=chromium
ENV PLAYWRIGHT_MCP_EXECUTABLE_PATH=/usr/bin/chromium
ENV PYTHONUNBUFFERED=1

USER root
COPY --from=pi-agent-builder /build/pi-agent/package.json /opt/cairn/pi-agent/package.json
COPY --from=pi-agent-builder /build/pi-agent/node_modules /opt/cairn/pi-agent/node_modules
COPY --from=pi-agent-builder /build/pi-agent/dist /opt/cairn/pi-agent/dist
RUN chmod +x /opt/cairn/pi-agent/dist/cli.js && \
    ln -s /opt/cairn/pi-agent/dist/cli.js /usr/local/bin/cairn-agent
USER kali
RUN cairn-agent --self-check
LABEL io.cairn.agent.engine="pi" \
      io.cairn.agent.version="1.0.0" \
      io.cairn.pi-sdk.version="0.73.0"

# runner显式加载工具说明，不使用旧CLI自动发现、hooks或配置注入。
COPY ./AGENTS.md /opt/cairn/AGENTS.md
WORKDIR /home/kali/workspace
