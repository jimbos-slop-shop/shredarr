FROM python:3.13-slim
LABEL org.opencontainers.image.source="https://github.com/jschaef80/shredarr" \
      org.opencontainers.image.description="Delete it in Jellyfin, and it's gone everywhere." \
      org.opencontainers.image.licenses="MIT"
# unar unpacks RAR releases so Undo can restore them (Settings → RAR restore)
RUN apt-get update && apt-get install -y --no-install-recommends unar && rm -rf /var/lib/apt/lists/*
COPY shredarr.py /app/
COPY static /app/static
ENV SHREDARR_CONFIG=/config/config.json SHREDARR_DB=/config/state.db PYTHONUNBUFFERED=1
VOLUME /config
EXPOSE 8089
HEALTHCHECK --interval=15s --timeout=5s --retries=3 CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8089/health')"
CMD ["python3", "/app/shredarr.py"]
