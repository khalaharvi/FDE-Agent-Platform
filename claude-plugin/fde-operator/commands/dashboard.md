---
description: Export the decision dashboard and write it into the operator's vault
argument-hint: "[engagement-id] [window-days]"
---

Export the decision dashboard — how work is flowing through the human gates —
and file it in the vault.

Arguments: `$ARGUMENTS` — optionally an engagement id and a window in days. With
no engagement id this rolls up every engagement, which is the right default when
the operator asks "how are we doing" without naming one. The window defaults to
30 days and bounds only the panels that are about a period (decisions, merges,
runs); the queue, gate and drift panels describe the present.

Do this:

1. `hitl_export_dashboard(engagement_id, window_days)`.
2. Resolve the vault directory: `$FDE_VAULT_DIR` if set, otherwise ask once and
   reuse the answer for the session.
3. Write `markdown` **unchanged** to `<vault>/dashboards/fde-dashboard-<date>.md`,
   where `<date>` is the date part of `generated_at`. Create the `dashboards/`
   directory if it is not there.
4. Offer to write `html` beside it as `fde-dashboard-<date>.html` — it is a
   self-contained styled page with no external assets, so it opens in a browser
   and pastes into an email intact. Only write it if the operator says yes; most
   of the time the Markdown is what they wanted.
5. If a file for today already exists, say so and ask before overwriting. Do NOT
   diff it against the new export and report the difference as a finding — see
   below.

**This document is not a playbook, and the difference matters.** A playbook is
pinned to a sealed commit and exports byte-identically forever, so diffing two
copies is meaningful. This one is a reading of live state at `generated_at`: two
exports taken a minute apart across a merge SHOULD differ, and the difference is
the clock passing, not a change worth reporting. A dated filename is what keeps
yesterday's reading from being mistaken for today's. It also aggregates counts
only and names no individual proposal, so never cite it as evidence for a
proposal — use `kg_as_of` or `wf_export_playbook` when you need something
citable.

One panel will say it could not be read: agent launches live in a table only the
console's gate role may see (db/018), and the MCP server runs as `fde_agent`.
Report that as written. It does **not** mean no agents ran — the console's
Dashboard page shows them.

End with the file path you wrote and a link to `<console>/dashboard` for the
live view, where `<console>` is the review console's base URL —
`http://127.0.0.1:8787/ui` for a local `fde-gate-dev`, otherwise the deployed
console's; ask once if unknown.
