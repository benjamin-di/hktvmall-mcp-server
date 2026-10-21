# syntax=docker/dockerfile:1.7
# hktvmall-mcp: the MCP server over streamable HTTP at :8000/mcp (needs MCP_BEARER_TOKEN). It runs as uid 1000 and
# works on a read-only root filesystem; /state (a volume) keeps budgets, the pause, the default account's session
# file and any connected accounts.
FROM ghcr.io/astral-sh/uv:0.9.26@sha256:9a23023be68b2ed09750ae636228e903a54a05ea56ed03a934d00fe9fbeded4b AS uv

FROM python:3.12.13-slim-trixie@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    HKTVMALL_STATE_DIR=/state

# Locked dependencies first, so a code-only release reuses this layer.
COPY requirements-runtime.lock /tmp/requirements-runtime.lock
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system --compile-bytecode --require-hashes -r /tmp/requirements-runtime.lock \
 && rm /tmp/requirements-runtime.lock

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv pip install --system --compile-bytecode --no-deps . \
 && rm -rf /app/src \
 && install -d -o 1000 -g 1000 -m 0700 /state

ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
ARG SOURCE_URL=https://github.com/benjamin-di/hktvmall-mcp-server
LABEL org.opencontainers.image.title="hktvmall-mcp" \
      org.opencontainers.image.description="MCP server for HKTVmall (Hong Kong) baskets" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}"

EXPOSE 8000
USER 1000:1000
HEALTHCHECK --interval=60s --timeout=5s --start-period=2m --start-interval=2s CMD ["hktvmall-mcp", "health"]
ENTRYPOINT ["hktvmall-mcp"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
