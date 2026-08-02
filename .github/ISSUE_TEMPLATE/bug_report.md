---
name: Bug report
about: Something behaves differently than the code or docs claim
labels: bug
---

**What happened**

**What you expected** (cite the doc/docstring/test that made you expect it —
this repo's docs are load-bearing, so a docs-vs-behavior mismatch is a real bug
even when the code is "working as coded")

**Repro**
```bash
# commands, starting from a clean `./db/rebuild.sh` where relevant
```

**Environment**
- OS / architecture:
- Postgres + pgvector version (`SELECT extversion FROM pg_extension WHERE extname='vector';`):
- `uv --version`, Python version:
- Model IDs in play (if agent behavior): `FDE_MODEL_ID` / `FDE_MODEL_PRESET`:

**Layer** (best guess): SQL / MCP tools / agents / gate service / SoR adapters / training / deploy CLI
