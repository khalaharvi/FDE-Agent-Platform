**What & why**

**Test evidence** (paste the tail of `FDE_DB_DSN=... uv run pytest packages`
and, if you touched db/, the `./db/rebuild.sh` smoke-test banner)

**Checklist**
- [ ] ruff check + format clean; `uv run mypy` clean; `uv lock --check` clean
- [ ] The twenty-nine CI privilege denials still deny (if you touched db/ or grants)
- [ ] Docs updated where behavior changed (`test_docs_sync.py` will catch docs/06 drift)
- [ ] CHANGELOG entry under `[Unreleased]`
- [ ] AWS-touching code labeled "not validated here" + `requires_aws` skips cleanly
