# Product-operations usability & Claude surfaces — Implementation Plan

Executes `docs/superpowers/specs/2026-08-03-prodops-usability-design.md`.

## Global Constraints

- **Branching:** one worktree + branch per task off current `origin/main`, from the repo root (`git worktree add .claude/worktrees/<name> origin/main -b <branch>`). PR per task; branch protection requires up-to-date branches, so merges are sequential (update-branch → CI → merge). NEVER touch the main checkout (in-flight deploy work on `feat/one-click-deploy`) and never modify `.github/workflows/ci.yml` (the deploy session owns its deploy job).
- **The invariant (do not weaken):** only `hitl.merge_proposal` writes the graph. Any new grant is minimal, on evidence/admin tables only (never `kg.node`/`kg.edge`/`kg.commit` writes), with `REVOKE ALL ... FROM PUBLIC` on any new function. The eight CI privilege denials must keep failing; new grants get NEW explicit denial tests proving `fde_agent` cannot use them. `test_parity.py` and its whitelist untouched (no retrieval SQL changes anywhere in this initiative).
- **Quality gates per task:** `uv run ruff check` + `format --check`, `uv run mypy` (strict; fde-gate/fde-mcp are covered), targeted pytest with `FDE_DB_DSN` against a live scratch DB, and `./db/rebuild.sh <scratch>` green when a migration is added (CI's smoke-count step name must stay true — do not add smoke tests to `smoke_test.sql`).
- **Pinned files:** `README.md` and `docs/*.md` are OFF LIMITS except in Task 6, which follows the update-docs skill (`.claude/skills/update-docs/SKILL.md`) including its mandatory marketing-strategist review gate and the sync test after every pinned edit (`0.78` stays in README).
- **MCP tool docstrings are model-facing prompts** (docs/11 §6). New env vars go in the owning package's `config.py` only. Servers' hand-rolled `--help` must keep working.
- **Tool count changes:** Task 1 adds a 22nd MCP tool; the "21 tools" claims across CLAUDE.md, README, docs-site, and docs/05 are updated ONLY in Task 6 via the update-docs skill sweep — implementation tasks do not chase docs.
- **Honesty rule:** AgentCore dispatch paths are stub-tested and labeled "written against verified API shapes, not validated here". No new claims of live AWS validation.
- **Commits:** conventional prefixes, trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. No email addresses anywhere except the trailer and fictional `@example.com` principals.
- **Templates/UX:** follow the existing console patterns (`ui.py` route table, `templates/*.html.j2`, `service/` modules, module-level pool). Server-rendered Jinja, no JS frameworks. Every operator-facing error names the fix in plain language.

### Task 1 — Playbook renderer (branch `feat/playbook-renderer`)

Spec §1. Files: `packages/fde-gate/src/fde_gate/ui.py` (+route), `service/workflows.py` (or the existing workflow service module), new `templates/workflow.html.j2`, new `packages/fde-gate/src/fde_gate/playbook.py` (renderer), `handler.py` (API route), `packages/fde-mcp/src/fde_mcp/tools/workflow.py` (new tool), tests in both packages.

1. Read first: `db/006_workflows.sql` end to end, `ui.py`, the workflow service/queries, `tools/workflow.py` (`wf_get`'s query shape), `kg_process_flow`'s SQL, docs/03 §artifact sections.
2. `playbook.py`: pure function `render_playbook(workflow, steps, bindings, process_flow) -> str` producing deterministic Markdown — front matter (slug, version, status, pinned commit digest, autonomy, runnable_by), a "Process context" narrative from the process-flow rows, then per-step sections: ordinal + title, kind badge line, `instruction`, `human_prompt` (when present), `requires_human`, `on_failure`, evidence bindings resolved to node labels + keys. No timestamps, no ids that vary per database — the output must be byte-stable across rebuilds for the golden test (use keys/digests, not serials, except step ordinals).
3. Console: `GET /ui/workflows/{id}` detail page rendering the same data as HTML (nav stays 4 pages; detail links from the list). Publish button moves/duplicates onto the detail page.
4. API: `GET /api/workflows/{id}/playbook.md` returning `text/markdown` via the same renderer.
5. MCP: `wf_export_playbook(workflow_id) -> str` in `tools/workflow.py` — read-only queries only (no new grants), docstring written as a prompt (when to use, what it returns, that it reflects the pinned commit). Registers as the 22nd tool.
6. Tests: golden-file test — seed a scratch DB via `./db/rebuild.sh <scratch> --with-demo`, author a fixture workflow via the same SQL path `wf_draft` uses (or reuse an existing test fixture pattern from fde-gate/fde-mcp tests), assert the rendered Markdown equals the committed fixture byte-for-byte; unit tests for renderer edge cases (no bindings, decision branches, human-only workflow); route tests for 404/authz parity with existing pages. Test module basenames unique across ALL packages.
7. Verify: full gate + mcp test suites, mypy strict, ruff. No `db/` changes in this task.

### Task 2 — Reviewer & authority admin (branch `feat/reviewer-admin`)

Spec §2. Files: new `db/016_reviewer_admin.sql` (next free number — verify), `db/tests/` NO changes to smoke_test.sql, `packages/fde-gate/src/fde_gate/ui.py`, new `templates/reviewers.html.j2`, service module, seed update `db/seed_demo.sql` (grant one demo principal the admin authority), denial-test additions where the existing eight live.

1. Read first: `db/004_hitl_gates.sql` (reviewer/authority shape — how `authority` values are constrained), `db/010`–`db/014` (grant patterns and how the eight denials are asserted in CI), the smoke-count constraint note in `.github/workflows/ci.yml`.
2. Migration: extend the authority domain with `admin` following db/004's existing constraint pattern; `GRANT INSERT, UPDATE ON hitl.reviewer, hitl.reviewer_authority TO fde_gate_service` (UPDATE for deactivation — `is_active` only; enforce via trigger or column-level grant per existing house style, choose what db/011-014 already do). No SECURITY DEFINER. Explicit `REVOKE` where the pattern requires it.
3. Console `GET/POST /ui/reviewers`: list (principal, display name, authorities, active), add-reviewer form, per-row deactivate/reactivate, authority grant/revoke. Page and POSTs visible/allowed only when the session principal holds `admin` (server-side check in the service layer, mirroring how `i_can_clear` works for gates). Never deletion.
4. Seed: `db/seed_demo.sql` grants `owner@example.com` the `admin` authority so the demo console shows the page (update the seed's self-assertions count if they enumerate authorities).
5. Denials: add explicit tests that `fde_agent` (and `fde_training` etc. per the existing denial matrix) cannot INSERT/UPDATE reviewer tables; the original eight stay untouched and failing.
6. Verify: `./db/rebuild.sh <scratch>` green (25 smoke tests unchanged), rebuild `--with-demo` green including seed assertions, gate suite + new authz tests (non-admin principal gets 403 on GET and POST), mypy, ruff.

### Task 3 — Transcript intake with anchor guardrails (branch `feat/transcript-intake`)

Spec §3. Files: `packages/fde-gate/src/fde_gate/ui.py`, new `templates/source_new.html.j2` + source list/detail additions, new `packages/fde-gate/src/fde_gate/intake.py` (chunker + anchor matcher), possible small migration ONLY if gate lacks INSERT on `kg.source`/`kg.chunk`/embedding queue (check `db/010`–`db/014` first; if needed, fold the grant into a `db/017` with denial tests, same rules as Task 2).

1. Read first: `packages/fde-mcp/src/fde_mcp/tools/evidence.py` end to end (chunk contract: ≤8000 chars, ≤200 chunks, checksum dedupe, anchor warnings at :304-317), `db/014` (INSERT-only enforcement), `db/008_retrieval.sql:300-330` (why anchor-less chunks are dark), how the embedder worker picks up new chunks.
2. `intake.py`: paragraph-boundary chunker honoring the 8000-char contract; anchor pass = lexical match of chunk text against live node labels/keys for the engagement (reuse `kg_lexical_search`'s SQL shape read-only; do NOT modify retrieval SQL); returns per-chunk anchor sets + a coverage ratio.
3. Console "New source" page: engagement picker, title, kind, captured_at, paste box + file upload (text/markdown only v1); preview screen shows chunk count and anchor coverage BEFORE commit; a 0%-coverage source can still be saved but renders a blocking-style warning banner naming the consequence and the next step ("run the engagement agent's ingest_interview, then re-ingest dark chunks"). Source list/detail shows coverage and a "re-ingest dark chunks" action that re-registers under a new source version once anchors exist (chunks stay INSERT-only).
4. Writes go through the same SQL the MCP tools use (same checksum dedupe semantics) under the gate's role; if that requires the Task-3 migration, the grant is INSERT-only on evidence tables, never graph tables.
5. Tests: chunker property tests (boundary, unicode, oversize input), anchor-matcher tests against a seeded scratch DB, intake round-trip test (paste → chunks visible to `kg_search` when anchored; dark when not — proving the guardrail's claim), authz.
6. Verify: gate suite, mypy, ruff, rebuild green if a migration was added.

### Task 4 — Agent task launcher + schema-driven forms (branch `feat/agent-launcher`)

Spec §4. Files: `packages/fde-gate/src/fde_gate/ui.py`, `executors.py` (reuse, do not fork, the dispatch selection), new `templates/agent_run.html.j2`, edit `templates/run.html.j2` + the run-start form, service module.

1. Read first: `executors.py:150-300` (agent dispatch via `FDE_RUNTIME_ARN_*`), `fde_agents` task registries (`engagement/agent.py:42`, `workflow/agent.py:41`) — but fde-gate must NOT import fde-agents; mirror the task list as data in the gate service with a comment naming the source of truth, and add a test that fails when the lists drift (read the agent modules' TASKS via a lightweight import in the TEST only, which may depend on fde-agents as a dev/test dependency if the workspace already allows it — otherwise pin the list in the test with a loud comment).
2. `GET/POST /ui/agents/run`: agent dropdown, task dropdown, per-task templated fields (ingest_interview: source picker from registered sources OR paste box mapped to the inline `material` payload; author_workflow: process-key picker of nodes eligible for `kg_process_flow`; generic tasks: labeled fields, no raw JSON). Dispatch through the existing executor path; when no runtime ARN is configured, fail with the plain-language message the spec demands (and in dev, document the local-driver alternative in the error text).
3. Replace the run-start raw-JSON textarea: render form fields from the workflow's first-step/declared `human_schema` where present; keep a fallback textarea ONLY when no schema exists, labeled as such.
4. Honesty: AgentCore dispatch remains stub-tested ("written against verified API shapes, not validated here") — tests use a fake boto client per the existing `requires_aws`-skipping patterns.
5. Tests: form rendering from schema fixtures, dispatch payload shape against the fake executor, task-list drift test, authz, the no-ARN error path.
6. Verify: gate suite, mypy, ruff.

### Task 5 — Claude operator plugin + MCPB (branch `feat/claude-operator-plugin`)

Spec §5a/b. Files: new `claude-plugin/fde-operator/` — `.claude-plugin/plugin.json`, `.mcp.json` (stdio launch of fde-mcp via `uv run`), `commands/ingest-interview.md`, `commands/run-agent.md`, `commands/playbook.md`, `skills/fde-operator/SKILL.md`, `README.md`; MCPB packaging config for Claude Desktop.

1. Read first: the plugin-dev skills' structure requirements (plugin.json shape, commands frontmatter), `.claude/skills/update-docs/SKILL.md` as the house skill style, `docs-site/first-merge.mdx` for the operator narrative the commands should mirror.
2. The skill encodes: session start (`kg_head_commit` first), ingest discipline (register → chunk → anchors → coverage check → agent enrichment loop), agent invocation (`wf_export_playbook`/`kg_process_flow` for reading; local driver command shapes for running), playbook generation into a configurable vault path (`FDE_VAULT_DIR` or asked once per session), and the console handoff (link to the queue for approvals — approvals NEVER happen via the plugin).
3. README carries the safety argument verbatim: the plugin runs as `fde_agent`; it can search, register evidence, and propose — it structurally cannot write the graph, decide gates, or merge; approvals stay in the console under the operator's JWT.
4. MCPB: manifest bundling the stdio server for Claude Desktop one-click install; the only required user setting is `FDE_DB_DSN` (engineer-provisioned). Validate with the plugin-validator agent before review.
5. Tests: plugin JSON validity (jq/python in CI-free script executed in the task, documented in the report); command frontmatter parses; no automated harness exists for plugins in this repo — the task review is the gate, plus plugin-validator output pasted in the report.
6. Verify: `uv run fde-mcp --help` still works from a fresh worktree per the README instructions the plugin cites.

### Task 6 — Docs pass via the update-docs skill (branch `docs/operator-guide`)

Spec §6 docs. RUN THE COMMITTED SKILL (`.claude/skills/update-docs/SKILL.md`) end to end — this is its first real execution and part of the deliverable is confirming it works.

1. Follow the skill: drift diff (Tasks 1–5 all count), sweep (the 21→22 tool count across CLAUDE.md:? / README / docs-site / docs/05 will surface — fix every claim the sweep finds), page updates: new `docs-site/guides/for-operators.mdx` (funnel walkthrough with zero terminal commands on the page), plugin README linked, update the skill's own inventory table with the new page + plugin README rows.
2. Pinned protocol per the skill; sync test after every README/docs edit.
3. MANDATORY marketing-strategist review gate per the skill; findings applied or declined in the PR body.
4. Verify: `mint validate`, `mint broken-links`, sync test, sweep returns no stale counts.

### Task 7 — Final reviews

Marketing-strategist pass over the operator-facing whole (detail page, intake flow copy, launcher copy, plugin commands/README, operator guide) against the spec's goals + honesty rule; then a final whole-branch code review per open PR triaging the ledger's deferred minors. Route findings via the standard fix loops.

## Sequencing

Task 1 first (standalone, highest value). Task 2 second (unblocks real operators; db migration). Tasks 3 and 4 after 2 (all three touch `ui.py`/templates — SEQUENTIAL implementation and merges to avoid route-table conflicts; rebase each on the prior's merge). Task 5 after 1 (needs `wf_export_playbook` to exist for the `/playbook` command). Task 6 after 1–5 merge. Task 7 last. Merge chain per PR: update-branch → CI → merge, in task order.

## Post-merge (human)

- Provision an operator: admin adds them via `/ui/reviewers` (bootstrap: first admin per Task 2's runbook note).
- Claude Desktop/Code install on the operator's machine + `FDE_DB_DSN` to the shared database; Obsidian vault path setting.
- The claude.ai remote connector remains deferred to the deploy sub-project (Gateway).
