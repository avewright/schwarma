# ── Build stage ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY schwarma/ schwarma/

RUN pip install --no-cache-dir build \
 && python -m build --wheel --outdir /build/dist

# ── Runtime stage ────────────────────────────────────────────────────────
FROM python:3.12-slim

LABEL maintainer="Avery Wright <avewright>"
LABEL description="Schwarma Hub — agent-to-agent exchange server with PostgreSQL"

# Install the wheel + asyncpg
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl asyncpg \
 && rm -rf /tmp/*.whl

# Non-root user
RUN useradd --create-home schwarma
USER schwarma
WORKDIR /home/schwarma

# Default ports:  9741 = TCP Station,  8741 = HTTP API
EXPOSE 9741 8741

# Health check against the HTTP API
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8741/health')" || exit 1

ENTRYPOINT ["python", "-m", "schwarma.hub"]
CMD ["--host", "0.0.0.0", "--tcp-port", "9741", "--http-port", "8741"]
