# syntax=docker/dockerfile:1.9
# =====================================================================
# One Dockerfile for all three AgentCore runtimes.
#
# The agents differ only in which entrypoint runs, so they differ only in
# ARG AGENT -- three near-identical Dockerfiles was exactly the duplication
# `fde_agents.common.runtime` removed from the Python, and it should not
# survive in the build.
#
#   docker buildx build --platform linux/arm64 \
#     --build-arg AGENT=engagement -f packages/fde-agents/Dockerfile \
#     -t $ECR/fde-engagement:$TAG --push .
#
# Build context is the REPOSITORY ROOT, not this directory -- uv needs the
# workspace root pyproject.toml and uv.lock to resolve workspace members.
#
# ARM64 is mandatory for the AgentCore container path. Building x86 is a
# silent failure: the image pushes fine, runs fine on a dev laptop under
# emulation, and then fails in the runtime.
# =====================================================================

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------
# Stage 1 -- resolve dependencies into a self-contained venv.
#
# Split from the app COPY so a source change does not invalidate the
# dependency layer. `--no-install-project` is what makes that split real:
# it installs ONLY the locked third-party tree, so the expensive layer is
# keyed on uv.lock alone.
# ---------------------------------------------------------------------
FROM --platform=linux/arm64 ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Only the manifests. Sources arrive in the next layer.
COPY pyproject.toml uv.lock ./
COPY packages/fde-mcp/pyproject.toml     packages/fde-mcp/pyproject.toml
COPY packages/fde-agents/pyproject.toml  packages/fde-agents/pyproject.toml
COPY packages/fde-training/pyproject.toml packages/fde-training/pyproject.toml

# --frozen: fail rather than silently resolving something uv.lock does not
#   pin. A container that quietly installs a different dependency set than
#   CI tested is the whole problem lockfiles exist to solve.
# --no-dev: ruff/mypy/pytest never ship.
# --package fde-agents: the workspace member selector. fde-training is NOT a
#   dependency of fde-agents, which is what structurally guarantees torch
#   cannot end up in this image.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --package fde-agents

# Now the sources, then install the workspace packages themselves.
COPY packages/fde-mcp/src    packages/fde-mcp/src
COPY packages/fde-agents/src packages/fde-agents/src
COPY README.md ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package fde-agents

# ---------------------------------------------------------------------
# Stage 2 -- runtime. No uv, no compilers, no build cache.
# ---------------------------------------------------------------------
FROM --platform=linux/arm64 python:${PYTHON_VERSION}-slim-bookworm AS runtime

ARG AGENT
ENV FDE_AGENT_NAME=${AGENT}

# libpq5 only. psycopg[binary] ships its own libpq for most paths, but the
# tracing sidecar (fde_agents.common.tracing -> fde_mcp.db) opens a real
# connection, and the system libpq is what that resolves against.
# ca-certificates for TLS to Bedrock, Secrets Manager, and RDS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root. AgentCore gives each session an isolated microVM, so this is
# defence in depth rather than the primary boundary -- but a process that
# does not need root should not have it, and a writable /app owned by the
# app user is the difference between a contained bug and a rewritten image.
RUN groupadd --system --gid 1001 fde \
    && useradd --system --uid 1001 --gid fde --create-home fde

WORKDIR /app
COPY --from=builder --chown=fde:fde /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FDE_SERVICE_NAME=fde-agent

USER fde

# AgentCore Runtime health-checks GET /ping and proxies POST /invocations to
# this exact port. BedrockAgentCoreApp.run() binds 0.0.0.0:8080.
EXPOSE 8080

# Fails the build if AGENT was not passed or names a runtime that does not
# exist -- better than shipping an image that dies on first invocation.
RUN python -c "import importlib, os; importlib.import_module(f'fde_agents.{os.environ[\"FDE_AGENT_NAME\"]}.agent')"

# Exec form: signals reach Python directly, so AgentCore's shutdown is clean
# rather than a 30-second SIGKILL wait.
CMD ["sh", "-c", "exec python -m fde_agents.${FDE_AGENT_NAME}.agent"]
