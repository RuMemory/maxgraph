FROM python:3.12-slim

# Связка GHCR-пакета с публичной витриной (пакет виден на странице RuMemory/maxgraph)
LABEL org.opencontainers.image.source="https://github.com/RuMemory/maxgraph" \
      org.opencontainers.image.description="MaxGraph — persistent memory for AI agents (MCP server, self-hosted)" \
      org.opencontainers.image.licenses="BUSL-1.1" \
      io.modelcontextprotocol.server.name="io.github.RuMemory/maxgraph"

WORKDIR /app

RUN pip install --no-cache-dir "mcp>=1.14.0,<2.0" "uvicorn>=0.30" "pydantic>=2.7"

COPY app/ /app/
COPY VERSION /app/VERSION

ENV MEMORY_TOKEN="" \
    HOST_FP="" \
    MEMORY_DIR=/data \
    PORT=8787

EXPOSE 8787
VOLUME /data

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8787/healthz',timeout=4)" || exit 1

CMD ["python", "app.py"]
