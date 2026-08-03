---
description: Export a workflow as a playbook and write it into the operator's vault
argument-hint: <engagement-id> [workflow-id-or-slug]
---

Export a published workflow as a Markdown playbook and file it in the vault. See
the `fde-operator` skill §4.

Arguments: `$ARGUMENTS` — engagement id, optionally a workflow id or slug. If no
workflow is named, call `wf_list(engagement_id)` and ask which one.

Do this:

1. `wf_export_playbook(workflow_id)`.
2. Check `status`. If it is not `published`, tell the operator the document
   carries a draft banner and has not passed the publication gate, and ask
   before writing it into the vault.
3. Resolve the vault directory: `$FDE_VAULT_DIR` if set, otherwise ask once and
   reuse the answer for the session.
4. Write `markdown` **unchanged** to `<vault>/<slug>-v<version>.md`. Do not
   reformat, re-order, summarize, or add a preamble — the export is byte-stable
   against the workflow's pinned commit, and editing it destroys the property
   that makes two exports diffable.
5. If that file already exists, diff it against the new export first and report
   what changed rather than overwriting silently.

End with the file path you wrote and a link to
`<console>/workflows/<workflow_id>`, where `<console>` is the review console's
base URL — `http://127.0.0.1:8787/ui` for a local `fde-gate-dev`, otherwise the
deployed console's; ask once if unknown.
