# Subscription-only Codex executor. Build: docker build --platform linux/arm64 -t iam-codex:1 docker/
FROM --platform=linux/arm64 node:22-bookworm-slim@sha256:4b150b48b4116e2a22f2c39cc27025fbf4f153e2078fd3b14d159fdb5c7279da
ARG CODEX_VERSION=0.166.0
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g "@openai/codex@${CODEX_VERSION}" \
 && npm cache clean --force
RUN groupadd --gid 10001 codex \
 && useradd --uid 10001 --gid 10001 --home-dir /home/codex --shell /usr/sbin/nologin codex \
 && mkdir -p /home/codex /work /opt/iam \
 && chown 10001:10001 /home/codex /work
# Code and public schema only. Authentication is delivered over stdin at runtime.
COPY launch.cjs response.schema.json /opt/iam/
USER 10001:10001
ENV HOME=/home/codex CODEX_HOME=/home/codex/.codex
WORKDIR /work
