# Build: install Shredarr and its locked dependencies into a virtual environment.
FROM python:3.14-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# Run: the virtual environment, plus unar so Undo can unpack RAR releases (Settings → RAR restore).
FROM python:3.14-slim
LABEL org.opencontainers.image.source="https://github.com/jschaef80/shredarr" \
      org.opencontainers.image.description="Delete it in Jellyfin or Plex, and it's gone everywhere." \
      org.opencontainers.image.licenses="MIT"
RUN apt-get update && apt-get install -y --no-install-recommends unar && rm -rf /var/lib/apt/lists/*
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" SHREDARR_CONFIG=/config/config.json SHREDARR_DB=/config/state.db PYTHONUNBUFFERED=1
VOLUME /config
EXPOSE 8089
HEALTHCHECK --interval=15s --timeout=5s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8089/health')"
CMD ["shredarr"]
