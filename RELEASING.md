# Releasing

## Versioning

Pre-1.0: SemVer applies loosely. A minor bump (`0.X.0`) may include
breaking changes; only patch releases (`0.2.X`) are guaranteed backward
compatible. Read the CHANGELOG before upgrading across a minor version.

## Steps

1. Roll `[Unreleased]` in `CHANGELOG.md` into a dated section:
   `## [X.Y.Z] — YYYY-MM-DD`, and put a fresh empty `## [Unreleased]`
   above it.
2. Bump the version to `X.Y.Z` in lockstep across all six
   `pyproject.toml` files — root and every package:
   ```
   pyproject.toml
   packages/fde-agents/pyproject.toml
   packages/fde-gate/pyproject.toml
   packages/fde-mcp/pyproject.toml
   packages/fde-sor/pyproject.toml
   packages/fde-training/pyproject.toml
   ```
3. Refresh the lockfile so it matches the new versions:
   ```bash
   uv sync --all-packages
   ```
   (not `--frozen` — this is the one time the lockfile is expected to
   change; commit the updated `uv.lock` alongside the version bumps.)
4. Open a PR with the CHANGELOG, version, and lockfile changes. `main`
   is branch-protected (PR + all CI checks, admins included) — the
   release commit goes through the same gate as any other change.
5. Once merged, tag the merge commit annotated, not lightweight, and
   push the tag:
   ```bash
   git tag -a vX.Y.Z -m "release: vX.Y.Z — <one-line summary>"
   git push origin vX.Y.Z
   ```

## Before you start

Confirm `FDE_DB_DSN=... uv run pytest packages` and `./db/rebuild.sh`
are green on the commit you're about to tag — the release shouldn't be
the first place a failure shows up.
