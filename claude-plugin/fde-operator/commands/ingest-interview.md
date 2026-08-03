---
description: Ingest an interview or SOP as anchored evidence, then report coverage
argument-hint: <engagement-id> [path-to-transcript]
---

Ingest a transcript or SOP into the FDE knowledge graph as searchable evidence.
Follow the `fde-operator` skill's ingest discipline (§2) exactly — the steps
below are that discipline, not a summary of it.

Arguments: `$ARGUMENTS` — the engagement id, optionally followed by a path to
the transcript. If no engagement id is present, ask for one and stop; there is
no default. If no path is present, ask the operator to paste the text.

Do this:

1. `kg_head_commit(engagement_id)`. Report the commit id.
2. Read the transcript. Compute its sha256 and pass it as `checksum` to
   `kg_register_source` so a re-run dedupes instead of forking the evidence.
   Ask for `captured_at` (when the interview happened — not now) and a
   `title` if they are not obvious from the file. `source_kind` must be one of
   `interview`, `sop_document`, `screen_recording`, `system_export`,
   `observation`, `sme_assertion`, `sor_telemetry`, `agent_inference`.
3. Split into chunks of ≤ 8000 characters on speaker turns and topic shifts,
   `ordinal` starting at 1. Max 200 chunks per call — a longer document goes in
   several calls against the same `source_id`, ordinals continuing to run.
4. For each chunk, find real `anchor_keys` with `kg_search` and
   `kg_lexical_search` before ingesting. Never invent a node_key.
5. `kg_ingest_chunks`. Print every entry of `warnings` verbatim.
6. Report coverage as "N of M passages anchored". If coverage is low, say
   plainly that the unanchored passages are invisible to retrieval, and offer
   the enrichment loop: run the `ingest_interview` task on the engagement
   agent, have the operator merge the resulting proposal in the console, then
   re-ingest the dark passages from the console's source page.

End with the `source_id` and a link to `<console>/sources/<source_id>`, where
`<console>` is the review console's base URL — `http://127.0.0.1:8787/ui` for a
local `fde-gate-dev`, otherwise the deployed console's; ask once if unknown.

Do not offer to approve or merge anything.
