# Adoption & OSS maturity — design

**Date:** 2026-08-02 · **Status:** approved by user, pre-implementation — queued behind the in-flight one-click AWS deploy work (multi-provider sub-project 2)
**Standalone initiative.** Consumes the 2026-08-02 adoption audit (`docs/superpowers/audits/2026-08-02-adoption-audit.md`) and the delivered-but-unlanded Mintlify scaffold (`docs-site/`, 12 pages, `mint validate` clean). Multi-provider support (PR #1) is already on main; the only anticipated file overlap with in-flight work is README-only and rebases trivially.

## Problem

v0.2.0 is public and its trust signals are unusually strong — sync-tested docs, a 14-bug ledger (`docs/99-sources.md` §7–§8), CI-asserted privilege denials, an explicit honesty section. But the adoption funnel breaks at the exact moment of conviction: a new user reaches green tests and an open review console in ~10 minutes, and the console is empty, because the only end-to-end scenario lives in `db/tests/smoke_test.sql` and rolls itself back (`ROLLBACK`, line 1221). Before that, a silent breaker: `db/rebuild.sh` shells out to `dropdb`/`createdb`/`psql` on the host — CI installs them (`.github/workflows/ci.yml:66`) but the README never mentions them, so the Docker-compose audience hits `command not found: dropdb` as their first error. Positioning has the mirror problem (the README explains *how* before *who/why*, with no adopt-vs-build argument), the product's only UI has no screenshot anywhere, and the six screenshot-verified diagrams (`diagrams/01-system-topology.html` … `06-training-loop.html`) don't render on github.com. Finally, the hygiene surface an enterprise platform lead screens for is incomplete: no CODEOWNERS, no dependabot, no documented release process, no hosted docs, `CODE_OF_CONDUCT.md` has no enforcement contact, and the `CHANGELOG.md` preamble still says "the project is pre-release, so everything currently lives under 0.1.0 and Unreleased" directly above the `[0.2.0] — 2026-08-02` first-public-release entry.

## Goals

1. A first-time evaluator goes from clone to a *felt* propose → gates → approve → merge → training-label loop in under 30 minutes, without reading the smoke test.
2. The docs site is live on Mintlify hosted, linked from the README header, and CI-validated on every PR that touches it.
3. The repo passes the screening checklist a platform lead at a larger company applies — explicit ownership, automated dependency updates, a documented release process, security and conduct contacts — without weakening the eight privilege denials or the AWS honesty rule.
4. Positioning (the problem, who it's for / not for, adopt-vs-build) is stated in the README and docs site with zero unverifiable claims.

## Non-goals (v1)

- **Community program** (GitHub Discussions, `good first issue` labels): rejected by the maintainer — the contribution surface stays issues/PRs with the existing templates; revisit only on sustained inbound demand.
- **Category-education content** (the FDE-discipline essay series, a full comparison teaching post): future content work; the seed lives in `docs-site/faq.mdx`.
- **Docs-site Reference tab** (all 21 MCP tool contracts, env-var and schema reference): blocked on a docs↔code sync mechanism à la `test_docs_sync.py`; hand-written copies would drift.
- **PyPI / registry publishing**: releases stay git-tag based; the only build artifacts remain the CI images and deploy codezips.
- **Any metric, benchmark, testimonial, or "production-ready" claim**: the "Status and honesty" section is the brand; every deliverable below is compatible with it as written.

## Design

### 1. Demo data and the verified first merge (`db/`, `docs-site/`) — effort M

New `db/seed_demo.sql`: the smoke test's Quote-to-Cash scenario inserts (same role discipline — proposals authored as `fde_agent`, merged via `hitl.merge_proposal`) **without** the final rollback, leaving 2–3 pending proposals visible in the review console plus one already-merged commit to query. Exposed as `./db/rebuild.sh <db> --with-demo` (flag appended after the existing db-name argument). Hard constraints: `smoke_test.sql` itself is not modified (the CI step name hardcodes the 25-smoke-test count), no new grants (new-table/grant rules per CLAUDE.md invariant section), retrieval SQL untouched so `test_parity.py` and its whitelist are untouched.

Then the walkthrough gets verified: run `docs-site/first-merge.mdx`'s SQL top-to-bottom against a seeded database. It was adapted near-verbatim from the smoke test without execution; the two known risks are nullable `model_id` column defaults and which optional gates fire (the evidence-strength rule). Fix the page to match observed behavior, including expected output snippets.

### 2. Docs site: land and publish (`docs-site/`, `.github/workflows/`) — effort M

Commit the scaffold as-is (12 pages, 4 nav groups). Hosting is Mintlify hosted: create the project in the Mintlify dashboard, install the Mintlify GitHub app on `khalaharvi/FDE-Agent-Platform`, set the project's content directory to `docs-site/` (human-owned, dashboard steps — cannot be done from a checkout). Deploys then track main automatically.

CI: a new separate workflow `.github/workflows/docs.yml` (`on: pull_request` with `paths: ["docs-site/**"]`) running `npm i -g mint` → `mint validate` → `mint broken-links`. Kept out of the required-check set — path-filtered checks can't be blanket-required under branch protection — and out of `ci.yml`'s six jobs so the docs loop stays fast and independent. The README header gains the docs-site link (**PINNED**, applied per §5 guardrail).

### 3. README positioning and visuals (`README.md` — all PINNED) — effort S–M

- One line adding the Postgres client-tools prerequisite next to the quick start's rebuild step: `psql`/`createdb`/`dropdb` via `brew install libpq` or `apt-get install postgresql-client` (mirrors `.github/workflows/ci.yml:66`).
- A four-line "Who this is for / not for" block naming the audit's three personas — forward-deployed/solutions engineers, enterprise platform leads answering "what can the agent write and who approved it," and OSS practitioners extracting the HITL-in-SQL pattern — plus one honest non-goal ("not a general agent framework; if you don't need governed writes to shared state, you don't need this").
- A ~20-second review-console GIF (queue → open proposal → approve → merge) above the fold. Depends on §1's seed data; recorded against `fde-gate-dev` locally.
- PNG exports of the six `diagrams/*.html` (headless-browser screenshots at fixed viewport) into `docs-site/images/`, embedded in the matching docs-site pages; optionally the topology PNG in the README.

### 4. Enterprise hygiene (`.github/`, repo root) — effort S

| File | Content | Notes |
|---|---|---|
| `.github/CODEOWNERS` | `* @khalaharvi` | Makes single-maintainer review routing explicit. |
| `.github/dependabot.yml` | `uv` + `github-actions` ecosystems, weekly | Feeds the existing `uv lock --check` CI gate instead of letting the lockfile rot. |
| `RELEASING.md` | The process currently implicit in the v0.2.0 tag message: roll `[Unreleased]` into a dated section, lockstep-bump root + five package `pyproject.toml` versions, annotated `vX.Y.Z` tag; SemVer statement (pre-1.0: minor may break) | New; referenced from CONTRIBUTING's PR section. |
| `CHANGELOG.md` | Replace the "pre-release … 0.1.0 and Unreleased" preamble sentence with release-aware wording | Not pinned; one line. |
| `CODE_OF_CONDUCT.md` | Add the missing enforcement-contact line | Address needs user input at execution (personal email vs. dedicated alias). |
| `.github/ISSUE_TEMPLATE/` | Convert both markdown templates to YAML issue forms; add `config.yml` with `blank_issues_enabled: false` | Matches the maintainer's low-noise preference; keeps structured bug/feature intake. |

GitHub-side metadata (human-owned, web UI): repo description ("Agents propose, humans dispose — three FDE agents over a Postgres knowledge graph with SQL-enforced human gates"), topics (`agents`, `human-in-the-loop`, `postgres`, `pgvector`, `bedrock`, `mcp`), social-preview card (the topology PNG from §3 works).

### 5. Errors, testing, docs

**Pinned-file guardrail:** every `README.md` or `docs/` edit in this initiative is applied by hand and followed by `uv run pytest packages/fde-training/tests/test_docs_sync.py` — `0.78` must remain in the README, `0.78`/`0.82` in docs/06, and the CI smoke-count step name must not change. No new numbered `docs/NN-*.md` files are added, so the README reading-list count string is untouched.

**Testing:** CI-verifiable — docs workflow green (`mint validate` is strict), sync test green after every pinned edit, and a seed smoke check (`./db/rebuild.sh <db> --with-demo` then assert the expected pending-proposal count via SQL, runnable in the database CI job as a non-counted step or locally). Human-verified — Mintlify deploy live at the project URL, GitHub settings applied, GIF legibility.

**Honesty labels:** unchanged and non-negotiable — every AWS-touching page keeps "written against verified API shapes, not validated here"; `docs-site/deployment.mdx` already leads with it and stays that way.

**Workflow:** one branch per Design section; §1 → §3 sequence (the GIF needs seed data), §2 and §4 land in parallel any time. Each branch → PR → CI green → **user merges** (user retains sole merge control). GitHub-dashboard steps (Mintlify app, repo metadata) are the user's, prompted by checklists in the relevant PR descriptions.

## Decision log

- Mintlify hosted over GitHub Pages static export (user decision — hosted search, assistant, and analytics for free; the scaffold already targets it).
- No community program (user decision — Discussions/`good first issue` invite drive-by noise a solo maintainer would have to service; the audit's persona-3 engagement path is deferred with it).
- Demo data as a separate `db/seed_demo.sql` behind a `--with-demo` flag (chosen over de-rollbacking the smoke test — keeps the CI smoke-count step name, test semantics, and parity guarantees untouched — and over committing seeded dumps, which would drift from migrations).
- Spec-only now, execution queued behind multi-provider work (user is mid-flight on `feat/multi-provider-llm`; main is branch-protected).
- Audit archived under `docs/superpowers/audits/` (chosen over repo root — the root stays the curated front door — and over deletion: the friction table and persona analysis are the rationale this spec cites rather than restates).
