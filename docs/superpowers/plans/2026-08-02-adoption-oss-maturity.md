# Adoption & OSS maturity — Implementation Plan

Executes `docs/superpowers/specs/2026-08-02-adoption-oss-maturity-design.md`.
Consumes `docs/superpowers/audits/2026-08-02-adoption-audit.md` (personas, friction table, recommendations).

## Global Constraints

- **Branching:** each task gets its own worktree and branch off `origin/main` (`git worktree add <repo-root>/.claude/worktrees/<name> origin/main -b <branch>` from the repo root). One PR per task, `gh pr create --base main`. The user merges; never merge or push to main directly. NEVER touch the main checkout's working tree (the repo root) — it has in-flight work on `feat/one-click-deploy`; the sole exception is Task 2 moving the untracked `docs-site/` directory out of it, and Task 5 reading/committing the untracked `.claude/agents/marketing-strategist.md`.
- **Pinned files:** after ANY edit to `README.md` or `docs/*.md`, run `uv run pytest packages/fde-training/tests/test_docs_sync.py` and confirm 4 passed. `0.78` must remain in README; docs/06 numbers untouched; the CI smoke-count step name ("25 smoke tests") must stay true — `db/tests/smoke_test.sql` is never modified.
- **The invariant:** no new grants, no SECURITY DEFINER, no role changes. Seed data must flow through the same role discipline the smoke test uses (`fde_agent` authors proposals; merges only via `hitl.merge_proposal`). The eight CI privilege denials must be untouched.
- **AWS honesty rule:** every AWS-touching statement keeps "written against verified API shapes, not validated here." No new claims of live validation anywhere.
- **Mintlify authoring rules** for any `.mdx`: frontmatter `title` (+`description`), kebab-case filenames, root-relative links without extension, language tags on all code blocks, sentence case headings, no marketing fluff or emoji; every new page registered in `docs-site/docs.json`; `mint validate` and `mint broken-links` clean (CLI is installed).
- **Commits:** conventional prefixes (`feat:`/`docs:`/`chore:`), body explains why, and every commit message ends with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- **Verification honesty:** report actual command output; a skipped check is reported as skipped, never as passed.

### Task 1 — Demo seed data (branch `feat/demo-seed`)

Spec §1a. Files: `db/seed_demo.sql` (new), `db/rebuild.sh` (edit).

1. Read `db/tests/smoke_test.sql` end to end (~1,200 lines; the Quote-to-Cash scenario, final `ROLLBACK` at line ~1221). Read `db/rebuild.sh` fully (hand-rolled arg handling — keep its usage text working).
2. Write `db/seed_demo.sql`: adapt the scenario inserts so a fresh database ends with (a) 2–3 **pending** proposals visible in the review console with evidence attached, (b) at least one **merged** commit reachable by query (merged via `hitl.merge_proposal` under the correct role), (c) `COMMIT` at the end. Reuse the smoke test's own `SET ROLE` discipline verbatim. No schema changes, no grants. A header comment states it is demo data, safe only for local evaluation databases.
3. Add a `--with-demo` flag to `rebuild.sh` (after the existing db-name argument): when present, apply `db/seed_demo.sql` after migrations + smoke tests. Without the flag, behavior is byte-identical to today. Update the script's usage/help text.
4. Verify (live, local Postgres is running): `createdb fde_demo_check && ./db/rebuild.sh fde_demo_check --with-demo` succeeds end-to-end (all 25 smoke tests still pass); `psql fde_demo_check` assertions: pending-proposal count matches the seeded number, the merged commit exists, `SET ROLE fde_agent` still cannot write the graph directly (spot-check one denial). Then `./db/rebuild.sh fde_demo_check` (no flag) also succeeds and leaves zero pending proposals. Drop the scratch DB after. Paste the actual psql outputs in the report.
5. PR titled `feat: demo seed data for the review console (--with-demo)`. Body notes it implements spec §1a and that docs referencing it land in a sibling PR.

### Task 2 — Land docs-site, verify the walkthrough, diagrams, docs CI (branch `docs/site-launch`)

Spec §1b + §2 + §3's diagram exports. Files: `docs-site/**` (imported), `.github/workflows/docs.yml` (new), `docs-site/images/*.png` (new). No README edits in this task (consolidated in Task 4).

1. Move the untracked `docs-site/` directory from the repo root's `docs-site/` into this task's worktree root (`git mv` does not apply — it is untracked; plain `mv`). 12 pages + `docs.json` expected; run `mint validate` to confirm the import is intact before changing anything.
2. Live-verify `docs-site/first-merge.mdx`: create a scratch DB seeded via Task 1's branch (its worktree path is provided in the dispatch; run ITS `db/rebuild.sh <scratch> --with-demo`). Execute the page's SQL top-to-bottom exactly as written. Fix the page where reality disagrees — known risks: nullable `model_id` defaults and which optional gates fire (evidence-strength rule). Add real expected-output snippets from the run. Also verify the console flow the page describes against `fde-gate-dev` (routes were checked against `fde_gate/ui.py` when the page was written; confirm against the running server, `FDE_GATE_DEV_PRINCIPAL` required).
3. Export the six `diagrams/0*.html` to PNG (headless Chrome screenshot at a fixed viewport, e.g. `chrome --headless --screenshot`); store as `docs-site/images/diagram-0N-<slug>.png` with descriptive alt text, embed each in the topically matching docs-site page (topology → architecture, training loop → training-flywheel, etc.).
4. New `.github/workflows/docs.yml`: `on: pull_request` with `paths: ["docs-site/**"]`; single job: checkout, setup-node, `npm i -g mint`, `mint validate`, `mint broken-links`. Not added to required checks.
5. Verify: `mint validate` + `mint broken-links` clean; `actionlint` or careful YAML review of the workflow; sync test still green (no pinned files touched — confirm with `git status`).
6. PR titled `docs: public docs site (Mintlify), verified first-merge walkthrough, diagram exports, docs CI`. Body includes the human-owned checklist: Mintlify dashboard project + GitHub app install, content directory `docs-site/`.

### Task 3 — OSS hygiene (branch `chore/oss-hygiene`)

Spec §4. All-new files plus two small edits.

1. `.github/CODEOWNERS`: `* @khalaharvi`.
2. `.github/dependabot.yml`: `uv` ecosystem (root) + `github-actions`, weekly.
3. `RELEASING.md`: the process implicit in the v0.2.0 tag — roll `[Unreleased]` into a dated section, lockstep-bump root + five package `pyproject.toml` versions, `uv sync --all-packages` to refresh the lock, annotated `vX.Y.Z` tag, push tag; SemVer statement (pre-1.0: minor releases may break). Reference it from CONTRIBUTING.md's PR section (one line).
4. `CHANGELOG.md`: replace the "the project is pre-release, so everything currently lives under 0.1.0 and Unreleased" preamble sentence with release-aware wording. Touch nothing else in the file.
5. `CODE_OF_CONDUCT.md`: add an enforcement-contact line that does NOT expose a personal email — direct reports to private channels GitHub already provides (the maintainer's GitHub profile contact link, and GitHub's built-in report/abuse flow); note in the PR body that the maintainer can swap in a dedicated alias later if they create one. No email address appears anywhere in the repo.
6. Convert `.github/ISSUE_TEMPLATE/bug_report.md` + `feature_request.md` to YAML issue forms preserving their current fields; add `config.yml` with `blank_issues_enabled: false`.
7. `.gitignore`: add `.claude/worktrees/` and `.superpowers/`.
8. Verify: YAML validity of all new files (`python -c 'import yaml,...'` or actionlint for workflows); sync test green (CONTRIBUTING edit is not pinned, but run it anyway); `git status` clean.
9. PR titled `chore: OSS hygiene — CODEOWNERS, dependabot, RELEASING, issue forms, CoC contact`.

### Task 4 — README positioning + console visuals (branch `docs/readme-positioning`)

Spec §3, consolidating ALL README edits (deviation from spec §2's placement of the docs link — noted deliberately so exactly one PR touches the pinned README). Files: `README.md` (PINNED), `docs-site/images/console-*.{gif,png}` (new).

1. README edits, hand-applied and minimal:
   - Postgres client-tools prerequisite line next to the rebuild step: `psql`/`createdb`/`dropdb` via `brew install libpq` (add to PATH) or `apt-get install postgresql-client`.
   - A "Who this is for" block (≤6 lines) after the opening invariant blockquote: the three audit personas (forward-deployed/solutions engineers; enterprise platform leads who must answer "what can the agent write and who approved it"; practitioners extracting the HITL-in-SQL pattern) and the honest non-goal ("not a general agent framework; if you don't need governed writes to shared state, you don't need this"). Draw wording from the audit §1; keep the README's existing voice.
   - Docs-site link in the header area (URL: the Mintlify project URL if known by then, else the placeholder `https://fde-agent-platform.mintlify.app` with a TODO note in the PR body).
   - Console media above the fold: a ~20s GIF of queue → open proposal → approve → merge against a `--with-demo` database with `fde-gate-dev` running. Record via browser automation (`gif_creator` MCP tool) at a clean viewport; if GIF proves infeasible, fall back to 2–3 captioned PNGs (audit allows either). Files under `docs-site/images/`.
2. After EVERY README save: `uv run pytest packages/fde-training/tests/test_docs_sync.py` → 4 passed, and confirm `0.78` still present (`grep -c "0.78" README.md`).
3. Verify rendered README (`gh markdown-preview` or push and eyeball the PR diff render). Media files referenced with raw.githubusercontent-safe relative paths.
4. PR titled `docs: README positioning, quickstart prerequisite, review-console demo media`.

### Task 5 — Docs-maintenance automation with the marketing agent in the loop (branch `docs/update-automation`)

User addition + spec §5's spirit. Files: `.claude/skills/update-docs/SKILL.md` (new), `.claude/agents/marketing-strategist.md` (commit the existing untracked definition from the main checkout, verbatim), `CLAUDE.md` (one line).

1. Commit the existing `.claude/agents/marketing-strategist.md` (read it from `.claude/agents/marketing-strategist.md` in the main checkout) so the review loop ships with the repo.
2. Write `.claude/skills/update-docs/SKILL.md` — a repeatable docs-sync workflow for Claude Code sessions:
   - **Trigger guidance** (frontmatter description): run after merging feature/behavior changes, before releases, or when README/docs-site drift is suspected.
   - **Steps:** (a) diff since the last docs-touching commit (`git log --oneline -- docs-site/ README.md` to find it); (b) map changed packages/commands/env vars to the docs-site pages and README sections that mention them (page inventory table included in the skill); (c) apply updates following the Mintlify rules and the pinned-file protocol (both restated compactly in the skill); (d) run `mint validate`, `mint broken-links`, and the sync test; (e) **mandatory:** dispatch the `marketing-strategist` agent to review the changed pages for positioning, adopter clarity, and honesty-rule compliance before the PR — its findings are applied or explicitly declined in the PR body; (f) PR per feature-branch workflow.
   - Include the AWS honesty rule and "never edit docs/00–99 for outsiders" boundary.
3. Add one line to CLAUDE.md near the docs gotchas pointing at the skill (e.g. "Docs drift: run the update-docs skill (.claude/skills/update-docs) — it ends with a marketing-strategist review pass").
4. Verify: skill frontmatter parses (name + description); CLAUDE.md edit is additive only; sync test green.
5. PR titled `chore: update-docs skill — docs-sync workflow with marketing review gate`.

### Task 6 — Marketing review pass (no new branch)

The marketing agent in the loop, now: dispatch the `marketing-strategist` agent to review the full delivered set — Task 4's README copy, Task 2's amended first-merge page and diagram embeds, Task 5's skill — against the audit's positioning goals and the honesty rule. Route its Critical/Important findings into the standard fix loop on the owning branch; note accepted/declined findings in the affected PR bodies.

## Sequencing

Task 1 → Task 2 (walkthrough verification needs the seed) → Task 4 (GIF needs a seeded DB; README docs link benefits from Task 2's site being final). Task 3 and Task 5 are independent and may run at any point between others. Task 6 runs last, before the final whole-branch review of each open PR.

## Post-merge (human + gh)

- `gh repo edit` description ("Agents propose, humans dispose — three FDE agents over a Postgres knowledge graph with SQL-enforced human gates") + topics (`agents`, `human-in-the-loop`, `postgres`, `pgvector`, `bedrock`, `mcp`) — can be done via CLI with user consent; social-preview card is web-UI-only.
- Mintlify dashboard: create project, install GitHub app, set content dir `docs-site/`, confirm the URL, then fix Task 4's placeholder link if it differs.
