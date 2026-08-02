# syntax=docker/dockerfile:1.9
# =====================================================================
# SoR adapters -- ONE image, six run modes.
#
#   docker buildx build --platform linux/arm64 \
#     -f packages/fde-sor/Dockerfile -t $ECR/fde-sor:$TAG --push .
#
# Build context is the REPOSITORY ROOT (uv needs the workspace manifests).
#
# Serverless: four Lambdas differing only in ImageConfig, which overrides the
# ENTRYPOINT/CMD below --
#   EntryPoint ["python","-m","awslambdaric"]
#   Command    ["fde_sor.lambda_handlers.{poll,stream,backfill,drift_scan}_handler"]
# Containers (infra/k8s/): the same image with `command: ["fde-sor", ...]`.
#
# Keeping both on one image is what makes "it worked in the CronJob but not in
# the Lambda" impossible: the dependency set, the Python version, and the
# package code are byte-identical, and only the entry point differs.
#
# ARM64 is mandatory and getting it wrong is a silent failure -- the image
# builds and runs fine on an x86 laptop under emulation, then fails in the
# Lambda. Pinned in the FROM rather than left to the build invocation.
# =====================================================================

ARG PYTHON_VERSION=3.12

FROM --platform=linux/arm64 ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages/fde-mcp/pyproject.toml      packages/fde-mcp/pyproject.toml
COPY packages/fde-sor/pyproject.toml      packages/fde-sor/pyproject.toml
COPY packages/fde-agents/pyproject.toml   packages/fde-agents/pyproject.toml
COPY packages/fde-training/pyproject.toml packages/fde-training/pyproject.toml

# Dependency resolution split from the source copy (--no-install-project), so a
# code change does not invalidate the dependency layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --package fde-sor

# fde-sor depends on fde-mcp (db/config/logging), so both sources are needed.
# fde-agents and fde-training are NOT copied and NOT depended on -- `uv sync
# --package fde-sor` structurally cannot pull torch into an image that holds
# customer system-of-record credentials.
COPY packages/fde-mcp/src packages/fde-mcp/src
COPY packages/fde-sor/src packages/fde-sor/src
COPY README.md ./

# --no-editable: the runtime stage copies ONLY .venv, so workspace members
# must be real site-packages installs, not .pth pointers back into /app/packages.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package fde-sor

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
    FDE_SERVICE_NAME=fde-sor

USER fde

# Fail the BUILD, not the first invocation, if a handler name is wrong or a
# module does not import under the pruned dependency set.
RUN python -c "\
import fde_sor.lambda_handlers as h; \
[getattr(h, n) for n in ('poll_handler','stream_handler','backfill_handler','drift_scan_handler')]; \
import fde_sor.cli"

# The container default is the CLI (the EKS path). Lambda's ImageConfig
# overrides both of these with awslambdaric plus a handler name.
ENTRYPOINT ["fde-sor"]
CMD ["--help"]
