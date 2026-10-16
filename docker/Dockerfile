# Изолированный исполнитель Claude Code (S08, R-110). Сборка на Mac (Apple Silicon):
#   docker build --platform linux/arm64 -t iam-claude:1 docker/
# Версию CLI фиксируем явно (проверена `claude --version` = 2.1.301 в среде разработки); образ-основу перед
# постоянным использованием закрепите по дайджесту (docker buildx imagetools inspect node:22-bookworm-slim).
FROM --platform=linux/arm64 node:22-bookworm-slim

ARG CLAUDE_CODE_VERSION=2.1.301

# curl нужен только для проверок сети в verify_isolation.sh; больше в образе ничего лишнего.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && npm cache clean --force

# Не root: числовые uid/gid совпадают с --user в iam.providers.command.SandboxConfig.
RUN groupadd --gid 10001 claude \
 && useradd --uid 10001 --gid 10001 --home-dir /home/claude --shell /usr/sbin/nologin claude \
 && mkdir -p /home/claude && chown 10001:10001 /home/claude

USER 10001:10001
ENV HOME=/home/claude \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DISABLE_TELEMETRY=1 \
    DISABLE_ERROR_REPORTING=1
# Рабочий каталог — пустой tmpfs (--tmpfs /tmp при запуске): исполнителю нечего читать и негде оставить следы.
WORKDIR /tmp
# Команда задаётся при запуске (iam.providers.command.docker_run); ENTRYPOINT намеренно нет.
