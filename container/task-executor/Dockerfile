ARG WORKER_IMAGE=arena-worker:local
FROM ${WORKER_IMAGE}
USER root
# 仅增加浏览器信任工具，复用现有扫描器、Chromium 和 Pi 依赖层。
RUN apt-get update && apt-get install -y --no-install-recommends libnss3-tools && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
COPY server.py /opt/cairn/task-executor/server.py
USER kali
ENTRYPOINT ["python3", "-u", "/opt/cairn/task-executor/server.py"]
