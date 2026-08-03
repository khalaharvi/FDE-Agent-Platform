"""playbook.py -- a published workflow, rendered as Markdown a person can follow.

`wf.workflow` + `wf.step` + `wf.step_binding` already ARE a playbook: an
ordered list of instructions, each citing the graph element it implements,
frozen against a sealed commit. Nothing rendered them, so a reviewer could
publish a procedure they had no way to read outside a live run. This module
is the whole of the distance between those rows and a document an operator
opens in Obsidian.

Why the renderer lives here and not in fde-gate
------------------------------------------------
Three surfaces render the same playbook -- the console's workflow page,
`GET /api/workflows/{id}/playbook.md`, and the `wf_export_playbook` MCP tool
-- and they have to emit the same bytes, or "export the playbook" means
something different depending on which one you asked. fde-gate depends on
fde-mcp and not the other way round, so the shared renderer can only sit on
this side of that edge. It is pure: standard library only, no database, no
MCP; it takes rows and returns a string.

Determinism is the contract, not a nicety
------------------------------------------
The same workflow must render byte-identically across database rebuilds, and
a golden-file test holds it there. Two rules follow, and they shape every
line below:

* Nothing that varies per database appears in the output. `workflow_id`,
  `step_id`, `commit_id` and every timestamp are serials or wall-clock, and
  a database rebuilt from the same content hands out different ones. The
  pinned commit is named by its `content_digest` instead -- a hash of the
  merged items, identical wherever that commit exists. Step ordinals stay:
  they are authored positions, not identities.
* Anything SQL leaves in serial order is sorted here. `wf.step_binding` rows
  arrive in `binding_id` order, which is insertion order, which is whatever
  order the authoring agent happened to emit them in; sorting by
  (relation, kind, key) makes two databases holding the same bindings
  produce the same document.

A playbook is a document derived from a sealed commit. Two people exporting
the same workflow should be able to diff their files and get nothing back.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = ["render_playbook", "sort_bindings"]

# Bindings are listed in the order db/006's `relation` CHECK declares, which
# runs from the strongest claim to the weakest: a step's `implements` is what
# it IS, its `depends_on` is context. Sorting them alphabetically instead was
# just as deterministic and put `enforces` above `implements` on every step
# that had both. Anything not in this list sorts after it, alphabetically, so
# a relation added to the schema still renders in a fixed place.
_RELATION_ORDER = {
    relation: index
    for index, relation in enumerate(
        ("implements", "enforces", "records_to", "depends_on", "measured_by")
    )
}


def sort_bindings(bindings: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """`wf.step_binding` rows in the order a reader should meet them.

    Public because the console's workflow page renders the same bindings as
    an HTML table rather than through this module, and a page that listed a
    step's citations in one order beside a download that listed them in
    another would look like two different sets of facts.
    """
    return sorted(
        bindings,
        key=lambda binding: (
            _RELATION_ORDER.get(str(binding.get("relation") or ""), len(_RELATION_ORDER)),
            str(binding.get("relation") or ""),
            str(binding.get("subject_kind") or ""),
            str(binding.get("subject_key") or ""),
        ),
    )


# ---------------------------------------------------------------------------
# Scalars
# ---------------------------------------------------------------------------


def _yaml(value: Any) -> str:
    """One YAML scalar or flow sequence, produced by `json.dumps`.

    YAML 1.2 is a superset of JSON, so a JSON string/number/null/array is
    already valid YAML -- and it is the escaping that matters here. A
    workflow titled `Discount: over 20%` breaks an unquoted scalar, and
    hand-rolling the quoting rules is how front matter starts failing on the
    one title that happens to contain a colon.
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _text(value: Any) -> str:
    """Authored prose, normalised so the same words always render the same bytes.

    Instructions and prompts are pasted in by people and by agents, and
    arrive carrying CRLFs and trailing spaces that say nothing about the
    content. Normalising them here means an instruction retyped with
    different invisible characters does not produce a different playbook.
    """
    if value is None:
        return ""
    normalised = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalised.split("\n")).strip()


def _keys(values: Any) -> str:
    """A sorted, comma-separated list of graph keys as inline code."""
    items = sorted(str(item) for item in (values or []))
    return ", ".join(f"`{item}`" for item in items)


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _front_matter(workflow: Mapping[str, Any], steps: Sequence[Mapping[str, Any]]) -> list[str]:
    """YAML front matter: the fields an Obsidian vault can index and filter on."""
    fields: list[tuple[str, Any]] = [
        ("title", _text(workflow.get("title"))),
        ("slug", workflow.get("slug")),
        ("version", workflow.get("version")),
        ("status", workflow.get("status")),
        ("process", workflow.get("root_process_key")),
        ("autonomy", workflow.get("autonomy_level")),
        ("runnable_by", [str(group) for group in (workflow.get("runnable_by") or [])]),
        ("pinned_commit_digest", workflow.get("pinned_digest")),
        ("steps", len(steps)),
        ("authored_by", workflow.get("authored_by")),
    ]
    if workflow.get("published_by"):
        fields.append(("published_by", workflow.get("published_by")))

    return ["---", *(f"{name}: {_yaml(value)}" for name, value in fields), "---", ""]


def _header(workflow: Mapping[str, Any]) -> list[str]:
    status = str(workflow.get("status") or "unknown")
    lines = [
        f"# {_text(workflow.get('title'))}",
        "",
        f"Status **{status}** · autonomy **{workflow.get('autonomy_level')}** · "
        f"`{workflow.get('slug')}` v{workflow.get('version')}",
        "",
    ]

    runnable_by = [str(group) for group in (workflow.get("runnable_by") or [])]
    lines += [
        f"Runnable by: {_keys(runnable_by)}."
        if runnable_by
        else "Runnable by: anyone in the product-operations group.",
        "",
    ]

    digest = workflow.get("pinned_digest")
    if digest:
        lines += [f"Pinned to graph commit `{digest}`.", ""]
    else:
        # An unpinned digest is not cosmetic: it is the value a reader would
        # compare against the commit to know this export was not tampered
        # with, and saying nothing would let its absence pass unnoticed.
        lines += [
            "The pinned commit carries no content digest, so this export cannot be "
            "checked back against the graph state it claims to be faithful to.",
            "",
        ]

    description = _text(workflow.get("description"))
    if description:
        lines += [description, ""]

    if status == "published":
        lines += [
            "Each step below lists the process elements it implements. That grounding "
            "is checked at publication, so no step here is one somebody invented.",
            "",
        ]
    else:
        lines += [
            f"> **This workflow is {status}, not published.** It has not been through "
            "the publication gate, so its steps are not yet guaranteed to cite the "
            "process elements they implement. Do not run it from this document.",
            "",
        ]
    return lines


def _process_context(
    workflow: Mapping[str, Any], process_flow: Sequence[Mapping[str, Any]]
) -> list[str]:
    """The process walk `kg_process_flow` returns, as narrative.

    Deliberately labelled as current rather than pinned: the steps below are
    frozen to a commit, this walk reads `kg.node_current`/`kg.edge_current`,
    and a reader who assumes both are the same vintage will eventually be
    wrong in a way that matters.
    """
    process_key = workflow.get("root_process_key")
    lines = ["## Process context", ""]

    if not process_flow:
        lines += [
            f"The graph records no activities under `{process_key}`, so there is no "
            "process walk to show. Each step's grounding is listed with the step.",
            "",
        ]
        return lines

    count = len(process_flow)
    noun = "activity" if count == 1 else "activities"
    lines += [
        f"This workflow implements `{process_key}`. The graph records {count} {noun} in "
        "that process, in the order control flows through them. This section reflects "
        "the graph as it stands today; the steps below are frozen to the pinned commit.",
        "",
    ]

    for row in process_flow:
        label = _text(row.get("label")) or str(row.get("activity_key"))
        lines.append(f"{row.get('ordinal')}. **{label}** (`{row.get('activity_key')}`)")
        for caption, values in (
            ("Performed by", row.get("performed_by")),
            ("Gated by", row.get("gated_by")),
            ("Recorded in", row.get("records_to")),
            ("Followed by", row.get("next_keys")),
        ):
            if values:
                lines.append(f"    - {caption}: {_keys(values)}")
    lines.append("")
    return lines


def _facts(step: Mapping[str, Any]) -> list[str]:
    """The badge block: what kind of step this is and how it behaves."""
    lines = [
        f"- Kind: `{step.get('kind')}`",
        f"- Step key: `{step.get('step_key')}`",
        f"- Requires a human: {'yes' if step.get('requires_human') else 'no'}",
        f"- On failure: `{step.get('on_failure')}`",
    ]
    if step.get("tool_name"):
        lines.append(f"- Tool: `{step.get('tool_name')}`")
    if step.get("sor_adapter_key"):
        lines.append(f"- System of record: `{step.get('sor_adapter_key')}`")
    if step.get("sor_write_op"):
        lines.append(f"- Write operation: `{step.get('sor_write_op')}`")
    return lines


def _branch_line(branch: Any) -> str:
    """One line of `wf.step.branches`, in the shape `DecisionExecutor` evaluates.

    Anything that is not a `{"when", "goto"}` / `{"else"}` object renders as
    its own JSON rather than being dropped: a branch the runner will refuse
    at execution time is exactly the thing a reviewer should see in the
    playbook, not the thing to hide from them.
    """
    if isinstance(branch, dict):
        if "else" in branch:
            return f"Otherwise, go to step `{branch['else']}`"
        if "when" in branch and "goto" in branch:
            return f"When `{branch['when']}`, go to step `{branch['goto']}`"
    return f"`{json.dumps(branch, ensure_ascii=False, sort_keys=True)}`"


def _branches(step: Mapping[str, Any]) -> list[str]:
    branches = step.get("branches")
    if isinstance(branches, list) and branches:
        lines = ["**Branches, evaluated in order:**", ""]
        lines += [f"{index}. {_branch_line(b)}" for index, b in enumerate(branches, start=1)]
        return [*lines, ""]
    if branches:
        # A non-list value the runner cannot evaluate. Shown verbatim for the
        # same reason as above.
        return [
            "**Branches:**",
            "",
            f"```json\n{json.dumps(branches, ensure_ascii=False, indent=2, sort_keys=True)}\n```",
            "",
        ]
    return [
        "**No branches are defined.** A decision step with no branches halts the run "
        "when it is reached; add branches before publishing.",
        "",
    ]


def _grounding(step: Mapping[str, Any], step_bindings: Sequence[Mapping[str, Any]]) -> list[str]:
    if not step_bindings:
        if step.get("kind") == "notify":
            return [
                "No grounding required: `notify` steps are exempt from the faithfulness check.",
                "",
            ]
        return [
            "**No grounding recorded.** A step that cites no graph element cannot be "
            "published -- add a binding to the element this step implements.",
            "",
        ]

    lines = ["**Grounded in:**", ""]
    for binding in sort_bindings(step_bindings):
        entry = (
            f"- `{binding.get('relation')}` {binding.get('subject_kind')} "
            f"`{binding.get('subject_key')}`"
        )
        label = _text(binding.get("pinned_label"))
        lines.append(f"{entry} — {label}" if label else entry)
    return [*lines, ""]


def _steps(
    steps: Sequence[Mapping[str, Any]],
    bindings: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[str]:
    lines = ["## Steps", ""]
    if not steps:
        return [*lines, "This workflow has no steps.", ""]

    for step in steps:
        lines += [f"### {step.get('ordinal')}. {_text(step.get('title'))}", ""]
        lines += _facts(step)
        lines.append("")

        instruction = _text(step.get("instruction"))
        if instruction:
            lines += [instruction, ""]

        prompt = _text(step.get("human_prompt"))
        if prompt:
            lines += [f"**Ask the operator:** {prompt}", ""]

        if step.get("kind") == "decision":
            lines += _branches(step)

        lines += _grounding(step, bindings.get(str(step.get("step_key")), ()))

    return lines


# ---------------------------------------------------------------------------
# The renderer
# ---------------------------------------------------------------------------


def render_playbook(
    workflow: Mapping[str, Any],
    steps: Sequence[Mapping[str, Any]],
    bindings: Mapping[str, Sequence[Mapping[str, Any]]],
    process_flow: Sequence[Mapping[str, Any]],
) -> str:
    """Render one workflow as Obsidian-ready Markdown. Pure; see the module docstring.

    Args:
        workflow: A `wf.workflow` row. Reads title, slug, version, status,
            root_process_key, autonomy_level, runnable_by, pinned_digest,
            description, authored_by and published_by; ignores every id and
            timestamp on it, deliberately.
        steps: `wf.step` rows in `ordinal` order. The caller orders them,
            because the ordering belongs in the query that already has an
            index for it.
        bindings: `wf.step_binding` rows grouped by `step_key` -- not by
            `step_id`, which is a serial that differs between databases
            holding identical content. Order within a group does not matter;
            this function sorts.
        process_flow: `kg.process_flow` rows for the workflow's root process,
            in the order that function returns them. Empty is fine and says
            so in the output.

    Returns:
        Markdown ending in exactly one newline. Plain CommonMark plus YAML
        front matter -- no proprietary syntax, so it renders in Obsidian, on
        GitHub, and in a terminal pager alike.
    """
    lines = [
        *_front_matter(workflow, steps),
        *_header(workflow),
        *_process_context(workflow, process_flow),
        *_steps(steps, bindings),
    ]
    return "\n".join(lines).rstrip("\n") + "\n"
