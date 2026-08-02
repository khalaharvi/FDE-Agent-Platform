# syntax=docker/dockerfile:1.9
# =====================================================================
# KG MCP server -- runs as its own service (ECS/Fargate behind an ALB, or
# as an AgentCore Gateway mcpServer target).
#
#   docker buildx build --platform linux/arm64 \
#     -f packages/fde-mcp/Dockerfile -t $ECR/fde-mcp:$TAG --push .
#
# Build context is the REPOSITORY ROOT (uv needs the workspace manifests).
#
# This image holds the database credentials and is the only component that
# talks to Postgres, so its dependency set is kept deliberately minimal:
# fde-mcp does not depend on fde-agents or fde-training, and `uv sync
# --package fde-mcp` cannot pull either.
# =====================================================================

ARG PYTHON_VERSION=3.12

FROM --platform=linux/arm64 ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages/fde-mcp/pyproject.toml      packages/fde-mcp/pyproject.toml
COPY packages/fde-agents/pyproject.toml   packages/fde-agents/pyproject.toml
COPY packages/fde-training/pyproject.toml packages/fde-training/pyproject.toml

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --package fde-mcp

COPY packages/fde-mcp/src packages/fde-mcp/src
COPY README.md ./

# --no-editable: the runtime stage copies ONLY .venv, so workspace members
# must be real site-packages installs, not .pth pointers back into /app/packages.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package fde-mcp

FROM --platform=linux/arm64 python:${PYTHON_VERSION}-slim-bookworm AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 1001 fde \
    && useradd --system --uid 1001 --gid fde --create-home fde

WORKDIR /app
COPY --from=builder --chown=fde:fde /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FDE_SERVICE_NAME=fde-mcp \
    FDE_MCP_TRANSPORT=http

USER fde

# Streamable HTTP on :8080/mcp -- the transport AgentCore Gateway and
# AgentCore Runtime (protocol=MCP) both require. SSE is not an option there.
EXPOSE 8080

RUN python -c "import fde_mcp.server"

CMD ["python", "-m", "fde_mcp"]
