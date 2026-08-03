---
description: Launch an engagement or workflow agent task and hand the proposal to the console
argument-hint: <engagement-id> [engagement|workflow] [task-name]
---

Launch an FDE agent task. See the `fde-operator` skill §3 for the surfaces and
the task roster.

Arguments: `$ARGUMENTS` — engagement id, then optionally the agent and task. Ask
for whatever is missing. Reject any task not in this table before dispatching,
naming the valid ones:

| Agent | Tasks |
|---|---|
| `engagement` | `map_process`, `score_opportunities`, `detect_bottlenecks`, `ingest_interview` |
| `workflow` | `author_workflow`, `monitor_drift`, `triage_drift`, `reauthor_stale` |

Do this:

1. `kg_head_commit(engagement_id)` and report the commit id — it is the base the
   agent's proposal will be authored against.
2. Gather the task input by asking targeted questions, never by asking for JSON.
   Each task reads its own keys — send these and nothing else:

   | Task | `--input` keys |
   |---|---|
   | `map_process` | `process_key` or `description`; optional `material` |
   | `score_opportunities` | `process_key` or `activity_keys` |
   | `detect_bottlenecks` | `process_key` |
   | `ingest_interview` | `material` — the transcript text **inline**, not a `source_id` |
   | `author_workflow` | `root_process_key`, `slug`, `title` |
   | `monitor_drift` | `min_severity` (default `medium`) |
   | `triage_drift` | `signal_ids` |
   | `reauthor_stale` | `workflow_id` |

   Confirm any `process_key` / `root_process_key` against the graph with
   `kg_process_flow` or `kg_search` before dispatching — a key that matches no
   node wastes a whole run.
3. Offer both launch surfaces and let the operator choose:
   - **Console:** `<console>/agents/run` — generated forms, no terminal.
   - **Terminal:** print the exact command, do not run it unless asked:

     ```bash
     fde-agents-local <agent> --task <task> --engagement-id <id> --input '<json>'
     ```

     If a run fails, have them run `fde-agents-local doctor` first — it isolates
     the missing prerequisite instead of leaving them to read a traceback.
4. When the run reports a proposal, call `kg_proposal_status(proposal_id)` and
   report status and outstanding gates.

End with a link to `<console>/proposals/<proposal_id>`, where `<console>` is the
review console's base URL — `http://127.0.0.1:8787/ui` for a local
`fde-gate-dev`, otherwise the deployed console's; ask once if unknown. State
that the review and merge happen there, under the operator's own principal, and
that you cannot do either.
