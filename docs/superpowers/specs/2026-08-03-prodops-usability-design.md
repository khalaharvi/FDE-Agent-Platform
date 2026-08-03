# Product-operations usability & Claude surfaces — design

**Date:** 2026-08-03 · **Status:** approved by user, pre-implementation — queued behind one-click AWS deploy (multi-provider sub-project 2)
**Standalone initiative.** Target persona: a product-operations operator who conducts discovery interviews and owns the resulting playbooks, works in Obsidian and Claude (Code/Desktop/chat), and will never run SQL or open a terminal. The gate console already serves this persona for the middle of the funnel; this initiative fixes the two broken ends (interviews in, playbooks out) and packages Claude as a first-class operator surface.

## Problem

The review/operate middle of the funnel is already no-SQL, no-terminal: the console's 14 routes (`packages/fde-gate/src/fde_gate/ui.py:355-373`) cover queue → proposal → gate decisions → merge, workflow publish, run start/cancel/respond, and drift triage, behind a JWT whose `sub` is the reviewer principal. But an operator cannot reach that middle or exit it:

- **Interviews can't get in.** Ingestion is `kg_register_source` + `kg_ingest_chunks` — pre-chunked JSON over MCP, no upload, no chunker. `anchor_keys` defaults to `[]` and ingestion succeeds anyway (`packages/fde-mcp/src/fde_mcp/tools/evidence.py:304-317` warns; nothing blocks), while retrieval drops anchor-less chunks entirely (`db/008_retrieval.sql:317`). A naive ingest silently produces chunks the graph will never surface.
- **Agents can't be triggered without a terminal.** `ingest_interview` and `author_workflow` — the two tasks this persona needs (`engagement/agent.py:42`, `workflow/agent.py:41`) — are reachable only via `fde-agents-local` CLI flags with hand-written JSON, raw HTTP, or an agent step inside an already-published workflow (`packages/fde-gate/src/fde_gate/executors.py:188-280`, AgentCore-only). The console cannot invoke either.
- **There is no playbook.** A published workflow is structured rows — ordered `wf.step`s with `title`, `instruction`, `kind`, `human_prompt`, `requires_human`, evidence bindings, pinned commit (`db/006_workflows.sql`) — a playbook's exact skeleton. Nothing renders it; the console has no workflow detail page at all (list only), so a reviewer publishes steps they cannot read outside a live run.
- **Reviewer onboarding is a raw SQL INSERT** into `hitl.reviewer` + `hitl.reviewer_authority` (`db/004_hitl_gates.sql:20,30`); no tool, CLI, console page, or API route exists.
- The console's one JSON-free gap inverts: `run_start_post` demands a raw-JSON textarea (`ui.py:278-299`) even though `human_schema` exists on human steps and could drive a form.

## Goals

1. A product-ops operator completes interview → agent proposal → console review/merge → workflow publish → **exported playbook in Obsidian** with zero SQL and zero terminal.
2. Claude (Code and Desktop) is a supported operator surface with the ingest discipline, agent invocation, and playbook generation packaged as an installable plugin — not tribal knowledge in one session.
3. Reviewer onboarding and agent task launch are console actions gated by the existing authority model.
4. The privilege invariant is untouched: the eight CI denials keep failing, every new grant is minimal and `REVOKE`d from PUBLIC, and handing Claude the MCP tools remains safe *because* the grant model — not the prompt — makes direct graph writes impossible.

## Non-goals (v1)

- **A new standalone web app.** The gate console is the UI chassis; every screen here is a console extension.
- **A remote claude.ai connector** (streamable-HTTP + OAuth in front of fde-mcp): deferred to ride the deploy sub-project, whose AgentCore Gateway path is the natural host; v1 covers Claude Code and Claude Desktop, which run the stdio server locally.
- **An Obsidian sync daemon / vault watcher.** V1 writes playbook Markdown into the vault via the plugin; no background sync.
- **New agent tasks.** No "draft playbook" agent task — the renderer is deterministic code over `wf.*` and needs no model.
- **Live-AWS validation.** The AgentCore-trigger path stays labeled "written against verified API shapes, not validated here" until the deploy sub-project validates it.

## Design

### 1. Playbook renderer (`fde-gate`, `fde-mcp`) — effort M, highest value per effort

- Console **workflow detail page** (`GET /ui/workflows/{id}`): header (title, status, autonomy, pinned commit + digest, runnable_by) and the ordered steps — title, kind badge, `instruction`, `human_prompt` where present, `requires_human`, evidence bindings resolved to node labels. The page the publish button should always have stood on.
- **`GET /api/workflows/{id}/playbook.md`**: deterministic Markdown export — front matter (workflow slug/version/commit digest), narrative process context from the same query `kg_process_flow` uses, then the step-by-step body. Obsidian-ready (plain Markdown, no proprietary syntax).
- **`wf_export_playbook` MCP tool** (fde-mcp, read-only grants only) returning the same Markdown, so every Claude surface gets playbooks without HTTP auth plumbing. Tool docstring written as a prompt per docs/11 §6.
- Golden-file test: seeded demo workflow → byte-stable playbook fixture.

### 2. Reviewer & authority admin (`fde-gate`, `db/`) — effort S–M

- New migration granting `fde_gate_service` INSERT/UPDATE on `hitl.reviewer` and `hitl.reviewer_authority` (it already writes decisions), `REVOKE ALL FROM PUBLIC`, no SECURITY DEFINER. The eight privilege denials must remain failing; CI's denial list gains "agent cannot insert reviewer" if not already covered.
- Console page `GET/POST /ui/reviewers`, visible only to principals holding a new `admin` authority; seed the first admin in the demo seed and document the one-time bootstrap for real deployments in the runbook (docs/10).
- Deactivation only, never deletion — decisions reference reviewers; history is the audit trail.

### 3. Transcript intake with anchor guardrails (`fde-gate`, `fde-mcp`) — effort M

- Console **"New source" page**: paste-or-upload a transcript/SOP, pick engagement, title, kind, captured_at. Server-side splitter chunks to the existing ≤8000-char contract and calls the same code path as `kg_register_source`/`kg_ingest_chunks` (checksum dedupe preserved).
- **Anchor strategy, two-pass and honest about it:** intake stores chunks with lexical-match anchors where a chunk's text matches live node labels/keys, and marks the source "pending enrichment" when coverage is low. The follow-on `ingest_interview` agent run (§4) — which receives the raw material inline today (`engagement/agent.py:100`) — proposes the new nodes; once merged, intake offers one-click re-ingest of the still-dark chunks under the now-live anchors (new source version; chunks stay INSERT-only per `db/014`).
- Hard UI rule: a source whose chunks are 100% anchor-less renders with a blocking warning naming the consequence ("retrieval will never see this") — the runtime warning at `evidence.py:304-317` surfaces in the console instead of dying in an MCP response.

### 4. Agent task launcher (`fde-gate`) — effort M

- Console page `POST /ui/agents/run`: agent dropdown (engagement/workflow), task dropdown from each agent's declared TASKS, and per-task templated fields (e.g. `ingest_interview`: source picker or paste box; `author_workflow`: process-key picker from `kg_process_flow`-eligible nodes) — never a raw JSON textarea. Dispatch via the existing executor selection: `FDE_RUNTIME_ARN_*` when deployed (AgentCore, honesty-labeled), the local driver's HTTP shape in dev.
- Same PR replaces the run-start raw-JSON textarea with forms generated from `human_schema` (the schema already exists on human steps; the textarea is the only reason it isn't a form).
- Launch records land next to runs (`/ui/runs`) so the operator watches the SSE-backed progress the executor already streams.

### 5. Claude surfaces: connector/plugin packaging (`claude-plugin/`) — effort S–M, mostly packaging

Yes — this ships as an installable Claude artifact, three tiers:

- **v1a, Claude Code plugin** (in-repo `claude-plugin/fde-operator/`): `.mcp.json` launching fde-mcp over stdio; commands `/ingest-interview`, `/run-agent`, `/playbook`; a skill encoding the ingest discipline (register → chunk → anchor rules → agent run → console handoff link) and playbook generation into a configurable vault path. Near-zero product code — the update-docs skill is the in-repo precedent.
- **v1b, Claude Desktop MCPB bundle**: the same server + config packaged as a one-click desktop install (MCP bundle), for operators who live in Desktop rather than a terminal app; the only required setting is the DSN/endpoint an engineer provisions. Desktop's filesystem access covers the Obsidian write-back.
- **v2 (deferred, non-goal above), claude.ai remote connector**: fde-mcp behind streamable HTTP + OAuth — rides the deploy sub-project's Gateway.
- **Why this is safe to hand to a model:** the connector runs as `fde_agent` — it can search, register evidence, and propose, and *cannot* write the graph, decide gates, or merge, no matter what the model does; approvals stay in the console under the operator's JWT. That sentence goes in the plugin README verbatim — it is the adoption argument.

### 6. Errors, testing, docs

**Error posture:** every operator-facing failure names the fix in plain language (unanchored source → what to click next; launcher dispatch failure → whether the runtime is deployed). No silent fallbacks.

**Testing:** golden-file playbook render; chunker property tests (boundaries, dedupe, anchor matching); admin-page authz tests (non-admin principal 403s); launcher dispatch tests against a fake executor; the eight privilege denials untouched and the new reviewer-write grant covered by an explicit denial test for `fde_agent`; `test_parity.py` untouched (no retrieval SQL changes). All network-free in CI; AgentCore dispatch stub-tested and honesty-labeled.

**Docs:** docs-site gains one "For operators" guide (funnel walkthrough, no terminal anywhere on the page); update-docs skill inventory gains rows for it and the plugin README; docs/10 runbook gains reviewer-bootstrap and intake-enrichment sections.

**Workflow:** one branch per Design section, PR → CI → user merges; §1 first (value), §2 next (unblocks real operators), §3+§4 together (the intake/launch loop), §5 last (packages what then exists).

## Decision log

- Console-as-chassis over a new operator app (chosen over a separate frontend — the console already owns auth, the authority model, and the review surface; a second app would fork identity).
- Plugin + MCPB first, remote claude.ai connector deferred (chosen over building OAuth/HTTP transport now — the deploy sub-project's Gateway is the natural host and avoids a second auth stack).
- Playbook renderer before agent launcher (highest value per effort; the data model already is the playbook).
- Deterministic renderer over a "draft playbook" agent task (documents derived from sealed commits should not vary with a model's mood; agents stay on the propose side).
- Reviewer admin via `fde_gate_service` grants + console authority (chosen over a DBA runbook — the SQL requirement is the thing being removed — and over SECURITY DEFINER, which the invariant discourages).
- Two-pass anchor enrichment over blocking anchor-less ingest outright (interviews naturally precede the nodes they evidence; the fix is sequencing + visibility, not refusal).
