# Robbie server — container image.
#
# Base MUST be glibc: the `claude` binary bundled in claude-agent-sdk is a
# dynamically-linked ELF (needs glibc) → Debian slim, NOT Alpine/musl.
# Runs as non-root uid 1000: the bundled Claude CLI refuses
# --dangerously-skip-permissions under root.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 -s /bin/bash app

WORKDIR /app

# Python deps first (layer cache). The ~230 MB bundled claude binary ships in
# the claude-agent-sdk wheel. --chown on the FIRST copy: it creates
# /app/server, and a later --chown does not re-own an existing dir (uid 1000
# must be able to write .bak siblings next to the config files).
COPY --chown=app:app server/requirements.txt ./server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt

# App code, MCP plugins, wakeword model.
COPY --chown=app:app server/ ./server/
COPY --chown=app:app mcp_datetime/ ./mcp_datetime/
COPY --chown=app:app mcp_weather/ ./mcp_weather/
COPY --chown=app:app models/ ./models/

ENV PYTHONUNBUFFERED=1 \
    TZ=Europe/Berlin \
    HOME=/home/app
EXPOSE 8422
USER app

# The config files (config.toml, plugins.toml, prompt.toml) live in
# server/config/ — copy the *.example.toml files there and bind-mount the
# directory (see compose.example.yaml) so edits survive image rebuilds.
ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "server.app", "--port", "8422", "--entities", "server/config/config.toml", "--config", "server/config/config.toml"]
