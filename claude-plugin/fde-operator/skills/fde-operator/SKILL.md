---
name: fde-operator
description: Operate the FDE Agent Platform knowledge graph — ingest an interview or SOP as anchored evidence, launch an engagement/workflow agent task, export a workflow as a playbook into a vault, and hand approvals to the review console. Use whenever the session touches an engagement, evidence sources, proposals, workflows, playbooks, or drift on this platform.
---

# Operating the FDE Agent Platform

You hold the `fde` MCP server's 22 tools. They run as the Postgres role
`fde_agent`, which can read the graph, register evidence, and write proposals.
It holds no privilege to write `kg.*` or to decide a gate. **You propose;
a human disposes in the review console.** Do not offer to approve or merge —
you cannot, and saying otherwise misleads the operator.

## 1. Start of session, every session

Call `kg_head_commit(engagement_id)` before anything else and say the commit id
out loud. Everything you read is "as of" that commit; a proposal you author is
based on it. If the operator has not named an engagement, ask for the
engagement id first — every tool here is scoped to one and there is no default.

Then ask, once per session and only if the task needs it, for the two settings
this skill uses:

- **Vault path** — `$FDE_VAULT_DIR` if set, otherwise ask where playbooks go
  and reuse that answer for the rest of the session.
- **Console URL** — `http://127.0.0.1:8787/ui` for a local `fde-gate-dev`,
  otherwise the deployed console's URL. Every hand-off link you produce is
  built from it.

## 2. Ingesting an interview or SOP

The discipline exists because of one asymmetry: **ingestion accepts chunks with
no anchors, and retrieval discards them.** `kg.hybrid_search`'s chunk arm drops
any chunk whose `anchor_keys` is empty (`db/008_retrieval.sql`), so an unanchored
chunk is stored, embedded, billed for, and permanently invisible. Nothing errors.
Follow the order below and check coverage at the end; do not improvise.

**Register.** `kg_register_source(engagement_id, source_kind=…, title=…,
captured_at=…)`. `source_kind` is one of exactly these — anything else is
rejected by a CHECK constraint:

`interview`, `sop_document`, `screen_recording`, `system_export`,
`observation`, `sme_assertion`, `sor_telemetry`, `agent_inference`

`captured_at` is when the evidence was captured from the business, **not now** —
ask if you do not know. Pass `checksum` (sha256 of the raw text) whenever you
have the raw artifact: re-registering the same document then returns the
existing `source_id` with `created: false` instead of forking the evidence
across two sources.

**Chunk.** Split the transcript yourself, in the source's own order:

- ≤ 8000 characters per chunk — split longer passages into more chunks, never
  truncate.
- Break on speaker turns and topic shifts, not on a character count. A chunk
  should be one claim a reviewer could cite.
- `ordinal` is the passage's position in the source, starting at 1, no gaps.
- ≤ 200 chunks per `kg_ingest_chunks` call. A longer document goes in several
  calls against the **same** `source_id`, with ordinals continuing to run.

**Anchor.** This is the step that decides whether the work was worth doing. For
each chunk, `anchor_keys` is the list of `node_key`s that passage is evidence
*for*. Find real keys before you ingest — `kg_search` for meaning,
`kg_lexical_search` for a name you can quote, `kg_get_node` to confirm one
exists. Never invent a key to satisfy the field: a key matching no live node
comes back as a warning, and a wrong key attaches the passage to the wrong node,
which is worse than leaving it dark.

**Ingest.** `kg_ingest_chunks(engagement_id, source_id, chunks)`. Then **read
`warnings` and report it** — do not summarize a successful call as "done" while
it names unanchored ordinals. Chunks are immutable: re-sending an ordinal is a
no-op reported in `skipped_ordinals`, and re-chunking a document means
registering a new source, never editing this one.

**Check coverage.** Count the chunks that got at least one anchor. State the
number as a fraction of the total. Then:

- **Most chunks anchored** — done. Point the operator at
  `<console>/sources/<source_id>`.
- **Few or none anchored** — expected for a first interview, because the
  interview is what creates the nodes it would anchor to. Say so plainly, then
  go to the enrichment loop. Do not present low coverage as success.

**Enrichment loop** — the second pass that makes the dark passages searchable:

1. Run the `ingest_interview` task on the **engagement** agent (§3). It reads
   the material and proposes the nodes the transcript describes.
2. The operator reviews and merges the proposal **in the console** (§5). Until
   they do, the nodes do not exist and re-anchoring cannot work.
3. Re-ingest the still-dark passages against the now-live nodes: the console's
   source page has a re-ingest action that carries them into a new source
   version, re-anchored (`packages/fde-gate/src/fde_gate/service/sources.py`).
   Prefer it over re-chunking by hand — it re-runs the same anchor matcher the
   original intake used (`packages/fde-gate/src/fde_gate/intake.py`). Report the
   new `source_id` and the new coverage. Two refusals to expect and relay
   rather than retry: the action requires an **active reviewer** principal
   (403 otherwise), and it refuses outright when the matcher still finds no
   live node for any dark passage — re-ingesting then would only make a second
   copy retrieval also cannot see. That refusal means step 2 has not landed
   yet, not that the console is broken.

## 3. Getting a proposal made

A proposal is how anything reaches the graph. Two ways to produce one: hand the
material to a platform agent, or author it yourself.

### Run an agent task

Two surfaces. Offer the console first — the operator may not have a terminal.

**Console (preferred):** `<console>/agents/run` — pick engagement, agent, task,
then a generated form. No JSON.

**Local driver**, when the operator is in a terminal on a checkout:

```bash
fde-agents-local doctor          # preflight: db, model provider, model id, embeddings
fde-agents-local engagement --task ingest_interview --engagement-id <id> \
    --input '{"material": "<transcript text>"}'
```

Valid tasks and the `--input` keys each one reads — anything else is ignored or
rejected:

| Agent | Task | Input keys |
|---|---|---|
| `engagement` | `map_process` | `process_key` or `description`; optional `material` |
| `engagement` | `score_opportunities` | `process_key` or `activity_keys` |
| `engagement` | `detect_bottlenecks` | `process_key` |
| `engagement` | `ingest_interview` | `material` |
| `workflow` | `author_workflow` | `root_process_key`, `slug`, `title` |
| `workflow` | `monitor_drift` | `min_severity` (default `medium`) |
| `workflow` | `triage_drift` | `signal_ids` |
| `workflow` | `reauthor_stale` | `workflow_id` |

Every `workflow` task also accepts `await_gates` (default false): set it when
this invocation should wait for the gate its proposal triggers instead of
returning as soon as it submits. It then camps on the gate for up to **20
minutes** (`FDE_GATE_WAIT_TIMEOUT_S`), so use it only when a reviewer is
standing by. The key is reachable only through `fde-agents-local --input`; the
console launcher's forms do not offer it.

`fde-agents-local` also accepts a third agent, `development`. It is out of scope
here for the same reason the console launcher omits it
(`packages/fde-gate/src/fde_gate/service/agents.py`): its tasks author agents —
an engineering surface, not product-operations work.

**`ingest_interview` takes the transcript inline as `material`, not a
`source_id`.** It does not read what you ingested in §2 — registering evidence
and having an agent read it are two separate deliveries of the same text. Ingest
it anyway: the source is what the proposal's items will cite.

Run `doctor` first when a run fails; it isolates which prerequisite is missing
instead of making you guess from a traceback.

A task run ends by submitting a proposal, not by changing the graph. Follow it
with `kg_proposal_status(proposal_id)` and hand off to the console (§5).

### Or propose directly

When the operator asks you to record something you can already evidence, author
it yourself: `kg_propose(...)` then `kg_submit_proposal(proposal_id)`.

- `base_commit_id` is the commit from §1. It records what graph state you
  reasoned against, which is what makes the review reproducible.
- **Every `add_*`/`update_*` item needs at least one `source_ids` entry.**
  Submission fails without it — evidence is mandatory, not encouraged. If you
  have no source, register the evidence first (§2); do not invent a citation.
- `kg_propose` returns `required_gates` computed live. Tell the operator up
  front which reviewers will be needed and how many. Adding or removing items
  changes it; `kg_submit_proposal` re-computes and freezes it.
- Submitting is not approving. The proposal now waits for humans (§5).

## 4. Exporting a playbook

`wf_list(engagement_id)` to find the workflow, then
`wf_export_playbook(workflow_id)`. It returns `{workflow_id, slug, version,
status, markdown}`; `markdown` is a whole document — front matter, process
context, then every step — and it is plain Markdown, so write it into the vault
unchanged. Do not reformat, re-order, or "improve" it: the export is
byte-stable against the workflow's pinned commit, which is what makes two
exports diffable.

One carve-out to that stability, and it matters when you diff: the **Process
context** section reads the graph as it stands *now*, not at the pinned commit,
and says so in its own text. Two exports of the same workflow taken across a
merge can legitimately differ in that section alone. A diff there is not drift
in the workflow.

Write it to `$FDE_VAULT_DIR/<slug>-v<version>.md` (or the vault path agreed in
§1). If a file is already there, diff before overwriting and tell the operator
what changed.

**A `draft` workflow exports too**, carrying a banner that it has not passed the
publication gate. Never file a draft into the vault as though it were the
procedure — if `status` is not `published`, say so in your reply and ask before
writing.

Use `wf_get` instead when the operator wants one attribute; `wf_export_playbook`
is for when a person is going to read the whole thing.

## 5. The console handoff — every approval, no exceptions

When work reaches a decision, stop and produce a link:

| The operator wants to | Send them to |
|---|---|
| approve a gate, edit an item, merge a proposal | `<console>/proposals/<proposal_id>` |
| see evidence coverage, re-ingest dark passages | `<console>/sources/<source_id>` |
| read or publish a workflow | `<console>/workflows/<workflow_id>` |
| launch an agent task | `<console>/agents/run` |
| watch a run, answer a human step | `<console>/runs` |
| triage a drift signal | `<console>/drift` |
| onboard a reviewer or grant authority | `<console>/reviewers` (admin only) |

**Why it is a hand-off and not a limitation you should work around:** the only
path that writes the knowledge graph is `hitl.merge_proposal`, and the
`fde_agent` role this server runs as does not hold the privilege. The refusal is
a missing grant in Postgres, not a rule in this prompt — so there is no phrasing,
retry, or tool combination that gets around it, and you should not spend the
operator's time looking for one. Approvals are recorded against the reviewer's
own principal from their console JWT, which is what makes the audit trail mean
something.

## 6. Reporting rules

- Name the commit id you read at, and the `source_id` / `proposal_id` /
  `workflow_id` you produced. The operator's next click needs them.
- Surface every `warnings` entry a tool returns. A warning you swallow is a
  passage the graph will never see.
- Never claim a proposal is merged. You can see status via
  `kg_proposal_status`; you cannot cause it.
- If a tool returns `error` with a `hint`, act on the hint rather than retrying
  the same call.

## Tool roster (22)

| Group | Tools |
|---|---|
| Graph reads (9) | `kg_head_commit`, `kg_as_of`, `kg_search`, `kg_lexical_search`, `kg_get_node`, `kg_traverse`, `kg_dependency_closure`, `kg_impact_radius`, `kg_process_flow` |
| Proposals (3) | `kg_propose`, `kg_submit_proposal`, `kg_proposal_status` |
| Drift (3) | `drift_list`, `drift_scan`, `drift_triage` |
| Workflow (4) | `wf_list`, `wf_get`, `wf_draft`, `wf_export_playbook` |
| Evidence (3) | `kg_register_source`, `kg_ingest_chunks`, `kg_list_sources` |

Tool-by-tool contracts: `docs/05-mcp-surface.md` and
`packages/fde-mcp/README.md` in the platform repo.
