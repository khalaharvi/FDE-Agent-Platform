"""dashboard.py -- how decisions are flowing, aggregated into one page.

`hitl.proposal`, `hitl.proposal_gate`, `hitl.gate_decision`, `kg.commit`,
`sor.drift_signal`, `wf.run` and `wf.agent_launch` each answer a question
about one row. Nobody had a way to ask the question *across* rows -- is the
queue draining or filling, which gate kind stalls, who is actually deciding,
did anything merge this month. The console could show an operator every
individual proposal and no way to tell whether the system was healthy. This
module is the distance between those tables and a page a non-technical
operator can read in ten seconds.

Why it sits beside `playbook.py` rather than in fde-gate
---------------------------------------------------------
Same reason, and the reason is the same shape: three surfaces render this
dashboard -- the console's `/ui/dashboard`, `GET /api/dashboard.md`, and the
`hitl_export_dashboard` MCP tool -- and they must agree on the numbers. A
console reporting eleven open gates beside an export reporting nine is worse
than having neither. fde-gate depends on fde-mcp and not the reverse, so the
shared code can only live on this side of that edge.

Only the query TEXT lives here. Role selection stays at each call site, and
here that is not a formality -- see the next section.

The privilege partition, which is the real constraint on this module
---------------------------------------------------------------------
No database role can read every table this dashboard aggregates, and that is
deliberate rather than an oversight to route around. Verified against a
rebuilt database with `has_table_privilege`:

    table                fde_agent   fde_prodops   fde_gate_service
    hitl.proposal            yes         yes            yes
    hitl.proposal_gate       yes         yes            yes
    hitl.gate_decision       yes         yes            yes
    hitl.reviewer            yes         NO             yes
    kg.commit                yes         yes            yes
    sor.drift_signal         yes         yes            NO
    wf.run                   yes         yes            NO
    wf.agent_launch          NO          NO             yes

Two consequences shape every function below.

* **`hitl.gate_decision.reviewer_id` is a foreign key to `hitl.reviewer`,
  not a principal string.** "Decisions by reviewer" therefore needs a join
  that `fde_prodops` cannot perform. The console runs that one panel as the
  gate role, which db/010 already grants the whole `hitl` schema.
* **`wf.agent_launch` is readable only by `fde_gate_service`.** db/018 says
  so in prose and CI asserts the denials: an agent that could write there
  could attribute its own work to a human who never asked for it. So the MCP
  tool, which runs as `fde_agent`, *cannot show launches at all*.

That last one is not a gap to paper over. `launches=None` means "the role
that assembled this cannot see wf.agent_launch"; `launches=[]` means "it
looked, and there are none". Both renderers say which one happened, in
words, on the page. A dashboard that silently omitted a panel would have an
operator concluding no agents ran when the truth is that nobody asked.
`assemble_dashboard` takes each panel as an argument for exactly this
reason: the caller runs the subset its role permits and passes `None` for
the rest, rather than this module guessing at privileges it cannot see.

`now` is a parameter, never `now()`
------------------------------------
Every window boundary and every "overdue" verdict is computed against a
`now` the caller supplies -- in the SQL as `%(now)s`, in the renderers as an
argument. `now()` in the query text would make "is this gate overdue" depend
on the wall clock at render time, which is untestable without freezing the
system clock and makes two surfaces rendering the same instant disagree by
however long the second query took. Callers pass `datetime.now(tz=UTC)`;
tests pass a fixed instant and get the same bytes every run.

The anti-stability contract, which is the opposite of playbook.py's
--------------------------------------------------------------------
A playbook is pinned to a sealed commit and must export byte-identically
forever. **This document is the other thing.** It reflects live state at
`generated_at` and nothing else: run it twice a minute apart across a merge
and the numbers SHOULD differ. It cites no individual proposal as evidence
and pins to no commit. Nothing here is diffable, and no reader should treat
a saved copy as current. Both renderers state this on the page rather than
leaving it to be inferred, because the surface next door does guarantee
stability and a reader who assumes the same of this one will eventually act
on a stale count.

This module holds no database code: standard library only, no psycopg, no
MCP. It takes rows and returns a string, and it says which rows it wants.
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import datetime

__all__ = [
    "DRIFT_SQL",
    "GATES_SQL",
    "GATE_KIND_SQL",
    "LAUNCHES_SQL",
    "MERGES_SQL",
    "QUEUE_SQL",
    "REVIEWERS_SQL",
    "RUNS_SQL",
    "assemble_dashboard",
    "render_dashboard_html",
    "render_dashboard_markdown",
]

# ---------------------------------------------------------------------------
# The rows this module renders.
#
# Every query takes the same three parameters and no others:
#
#   %(eng)s     engagement uuid as text, or NULL for the all-engagements
#               rollup. Spelled `%(eng)s::uuid IS NULL OR ...` rather than
#               built by string concatenation, so one query text serves both
#               the scoped and the rollup case and there is no branch in
#               Python that could send a different shape to one of them.
#   %(now)s     the caller's instant. See the module docstring.
#   %(window)s  window length in days, applied to the panels that are about
#               a period (decisions, merges) and not to those that are about
#               a present state (the queue, open gates, drift).
#
# Enum columns are cast to text for the reason playbook.py casts them: the
# renderer formats what it is handed, and a status arriving as an enum on one
# surface and a string on another is a document that differs by caller.
# ---------------------------------------------------------------------------

QUEUE_SQL = """
    SELECT p.status::text AS status,
           count(*) AS n,
           min(p.submitted_at) AS oldest_submitted_at
      FROM hitl.proposal p
     WHERE (%(eng)s::uuid IS NULL OR p.engagement_id = %(eng)s::uuid)
     GROUP BY p.status
     ORDER BY p.status::text
"""

# Cleared / pending / overdue are computed here rather than in Python so the
# three counts and the median come from one pass over one consistent snapshot.
# `median_seconds_to_clear` measures submission to gate clearance -- the wait a
# human actually experienced -- and is NULL when nothing has cleared yet, which
# the renderers report as "not computable" rather than as zero.
GATES_SQL = """
    SELECT count(*) FILTER (WHERE g.cleared_at IS NOT NULL) AS cleared,
           count(*) FILTER (WHERE g.cleared_at IS NULL
                              AND g.due_at >= %(now)s::timestamptz) AS pending,
           count(*) FILTER (WHERE g.cleared_at IS NULL
                              AND g.due_at <  %(now)s::timestamptz) AS overdue,
           min(g.due_at) FILTER (WHERE g.cleared_at IS NULL) AS next_due_at,
           percentile_cont(0.5) WITHIN GROUP (
             ORDER BY EXTRACT(EPOCH FROM (g.cleared_at - p.submitted_at))
           ) FILTER (WHERE g.cleared_at IS NOT NULL
                       AND p.submitted_at IS NOT NULL) AS median_seconds_to_clear
      FROM hitl.proposal_gate g
      JOIN hitl.proposal p USING (proposal_id)
     WHERE (%(eng)s::uuid IS NULL OR p.engagement_id = %(eng)s::uuid)
"""

GATE_KIND_SQL = """
    SELECT g.gate_kind::text AS gate_kind,
           count(*) FILTER (WHERE g.cleared_at IS NOT NULL) AS cleared,
           count(*) FILTER (WHERE g.cleared_at IS NULL) AS open,
           count(*) FILTER (WHERE g.cleared_at IS NULL
                              AND g.due_at < %(now)s::timestamptz) AS overdue
      FROM hitl.proposal_gate g
      JOIN hitl.proposal p USING (proposal_id)
     WHERE (%(eng)s::uuid IS NULL OR p.engagement_id = %(eng)s::uuid)
     GROUP BY g.gate_kind
     ORDER BY g.gate_kind::text
"""

# Superseded decisions are excluded: db/004 keeps a reviewer's earlier verdict
# as history when they change their mind, and counting both would credit one
# reviewer twice for one judgement.
#
# This is the query that needs hitl.reviewer, and so the one fde_prodops
# cannot run. See the module docstring.
REVIEWERS_SQL = """
    SELECT r.principal,
           count(*) FILTER (WHERE d.decision = 'approve') AS approvals,
           count(*) FILTER (WHERE d.decision = 'reject') AS rejections,
           count(*) FILTER (WHERE d.decision = 'request_changes') AS changes_requested,
           count(*) FILTER (WHERE d.decision = 'abstain') AS abstentions,
           count(*) AS decisions,
           max(d.decided_at) AS last_decided_at
      FROM hitl.gate_decision d
      JOIN hitl.reviewer r USING (reviewer_id)
      JOIN hitl.proposal_gate g USING (gate_id)
      JOIN hitl.proposal p USING (proposal_id)
     WHERE d.superseded_by IS NULL
       AND d.decided_at >= %(now)s::timestamptz - make_interval(days => %(window)s)
       AND (%(eng)s::uuid IS NULL OR p.engagement_id = %(eng)s::uuid)
     GROUP BY r.principal
     ORDER BY count(*) DESC, r.principal
"""

# The latest sealed commit is named by content_digest as well as id, for
# playbook.py's reason: the id is a serial that differs between databases
# holding identical content, and the digest is what a reader can compare.
MERGES_SQL = """
    SELECT count(*) FILTER (
             WHERE c.sealed_at >= %(now)s::timestamptz - make_interval(days => %(window)s)
           ) AS merges_in_window,
           max(c.sealed_at) AS latest_sealed_at,
           (SELECT c2.content_digest FROM kg.commit c2
             WHERE c2.status = 'sealed'
               AND (%(eng)s::uuid IS NULL OR c2.engagement_id = %(eng)s::uuid)
             ORDER BY c2.sealed_at DESC NULLS LAST, c2.commit_id DESC
             LIMIT 1) AS latest_digest,
           count(*) AS sealed_total
      FROM kg.commit c
     WHERE c.status = 'sealed'
       AND (%(eng)s::uuid IS NULL OR c.engagement_id = %(eng)s::uuid)
"""

DRIFT_SQL = """
    SELECT s.state::text AS state, count(*) AS n
      FROM sor.drift_signal s
     WHERE (%(eng)s::uuid IS NULL OR s.engagement_id = %(eng)s::uuid)
     GROUP BY s.state
     ORDER BY s.state::text
"""

RUNS_SQL = """
    SELECT r.status::text AS status, count(*) AS n
      FROM wf.run r
     WHERE (%(eng)s::uuid IS NULL OR r.engagement_id = %(eng)s::uuid)
       AND r.started_at >= %(now)s::timestamptz - make_interval(days => %(window)s)
     GROUP BY r.status
     ORDER BY r.status::text
"""

# Readable only by fde_gate_service (db/018). A caller running as any other
# role must pass launches=None to assemble_dashboard rather than running this.
LAUNCHES_SQL = """
    SELECT l.status AS status, count(*) AS n
      FROM wf.agent_launch l
     WHERE (%(eng)s::uuid IS NULL OR l.engagement_id = %(eng)s::uuid)
       AND l.requested_at >= %(now)s::timestamptz - make_interval(days => %(window)s)
     GROUP BY l.status
     ORDER BY l.status
"""

# ---------------------------------------------------------------------------
# Vocabulary
#
# Statuses are rendered in lifecycle order, not alphabetically, because the
# reader is looking at a flow: work enters at the left and leaves at the
# right, and `changes_requested` sorting before `in_review` would put the
# later stage first. Anything not listed renders after the known ones,
# alphabetically, so a status added to the schema still appears.
# ---------------------------------------------------------------------------

_PROPOSAL_STATUS_ORDER = (
    "draft",
    "submitted",
    "in_review",
    "changes_requested",
    "approved",
    "rejected",
    "merged",
    "expired",
)
_DRIFT_STATE_ORDER = ("open", "triaged", "proposal_raised", "accepted", "dismissed", "resolved")
_RUN_STATUS_ORDER = (
    "pending",
    "running",
    "awaiting_human",
    "succeeded",
    "failed",
    "cancelled",
)
_LAUNCH_STATUS_ORDER = ("queued", "running", "succeeded", "failed")

# An unfinished proposal: somebody still owes it an action. The queue
# headline, the "waiting on a human" tile and the oldest-waiting figure all
# count these and nothing else.
#
# `approved` belongs here and its absence was a real understatement of the
# queue. An approved proposal has cleared every gate -- db/013:246 sets the
# status the moment `gates_satisfied` goes true -- and is then parked until a
# person clicks Merge, because `hitl.merge_proposal` refuses anything whose
# status is not `approved` (db/005:84) and nothing calls it automatically.
# That is the single human write that touches the graph, so a proposal
# sitting in front of it is the MOST waiting a proposal ever is, not the
# least. Left out, an engagement whose whole queue was approved-and-unmerged
# reported zero waiting and an oldest-waiting of "never".
#
# `changes_requested` stays for the same reason one step earlier: the author
# owes it a revision. `draft` does not -- it is unsubmitted agent work that
# has not been handed to anyone, and counting it would put agent scratch
# space in a human backlog. `merged`, `rejected` and `expired` are finished.
_OPEN_PROPOSAL_STATUSES = frozenset({"submitted", "in_review", "changes_requested", "approved"})

# Drift in one of these is still somebody's problem. `resolved`/`dismissed`
# are closed; `accepted` means the graph was changed to match reality.
_OPEN_DRIFT_STATES = frozenset({"open", "triaged", "proposal_raised"})


def _ordered(
    rows: Sequence[Mapping[str, Any]], key: str, order: Sequence[str]
) -> list[dict[str, Any]]:
    """Rows in lifecycle order, unknown values last and alphabetical."""
    index = {name: position for position, name in enumerate(order)}
    return [
        dict(row)
        for row in sorted(
            rows,
            key=lambda row: (
                index.get(str(row.get(key) or ""), len(index)),
                str(row.get(key) or ""),
            ),
        )
    ]


def _total(rows: Iterable[Mapping[str, Any]], key: str = "n") -> int:
    return sum(int(row.get(key) or 0) for row in rows)


# ---------------------------------------------------------------------------
# Scalars
# ---------------------------------------------------------------------------


def _int(value: Any) -> int:
    return int(value or 0)


def _duration(seconds: float | None) -> str:
    """A human-scale duration. `None` is a real answer here, not a zero.

    A median over an empty set is NULL, and rendering that as "0s" would
    claim every gate cleared instantly -- the most flattering possible
    misreading of "nothing has cleared yet".
    """
    if seconds is None:
        return "not computable yet"
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f}m"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def _age(then: datetime | None, now: datetime) -> str:
    """How long ago `then` was, phrased for an operator."""
    if then is None:
        return "never"
    return _duration((now - then).total_seconds())


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _label(value: str) -> str:
    """`changes_requested` -> `Changes requested`."""
    return value.replace("_", " ").capitalize()


def _scope(engagement_id: str | None) -> str:
    return f"engagement `{engagement_id}`" if engagement_id else "all engagements"


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def assemble_dashboard(
    *,
    engagement_id: str | None,
    window_days: int,
    now: datetime,
    queue: Sequence[Mapping[str, Any]],
    gates: Mapping[str, Any] | None,
    gate_kinds: Sequence[Mapping[str, Any]],
    reviewers: Sequence[Mapping[str, Any]] | None,
    merges: Mapping[str, Any] | None,
    drift: Sequence[Mapping[str, Any]],
    runs: Sequence[Mapping[str, Any]],
    launches: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Rows in, one data dict out. Pure; both renderers read only this.

    Every panel is a separate argument because no single database role can
    read every table (see the module docstring). The caller runs the subset
    its role permits and passes `None` for the panels it could not read --
    which is a different thing from `[]`, and both renderers say which.

    Args:
        engagement_id: The engagement this is scoped to, or `None` for the
            all-engagements rollup. Rendered verbatim; the caller has
            already validated it as a uuid by passing it to Postgres.
        window_days: The period the decision/merge/run/launch panels cover.
            The queue, gate and drift panels describe the present and ignore
            it -- an open gate is open regardless of when it opened.
        now: The instant everything is computed against. Never `now()`.
        queue: `QUEUE_SQL` rows.
        gates: The single `GATES_SQL` row, or `None` if it was not run.
        gate_kinds: `GATE_KIND_SQL` rows.
        reviewers: `REVIEWERS_SQL` rows, or `None` if the assembling role
            cannot read `hitl.reviewer` (`fde_prodops` cannot).
        merges: The single `MERGES_SQL` row, or `None`.
        drift: `DRIFT_SQL` rows.
        runs: `RUNS_SQL` rows.
        launches: `LAUNCHES_SQL` rows, or `None` if the assembling role
            cannot read `wf.agent_launch` (only `fde_gate_service` can).

    Returns:
        A JSON-safe dict. Timestamps are ISO strings, counts are ints, and
        the three optional panels are either a list or `None`.
    """
    queue_rows = _ordered(queue, "status", _PROPOSAL_STATUS_ORDER)
    open_rows = [row for row in queue_rows if str(row.get("status")) in _OPEN_PROPOSAL_STATUSES]
    oldest = [
        row["oldest_submitted_at"]
        for row in open_rows
        if row.get("oldest_submitted_at") is not None
    ]
    oldest_waiting = min(oldest) if oldest else None

    drift_rows = _ordered(drift, "state", _DRIFT_STATE_ORDER)
    gates = dict(gates) if gates is not None else {}
    merges = dict(merges) if merges is not None else {}

    return {
        "engagement_id": engagement_id,
        "window_days": window_days,
        "generated_at": now.isoformat(),
        "queue": {
            "by_status": [
                {
                    "status": str(row["status"]),
                    "n": _int(row["n"]),
                    "oldest_submitted_at": _iso(row.get("oldest_submitted_at")),
                }
                for row in queue_rows
            ],
            "open": _total(open_rows),
            "total": _total(queue_rows),
            "oldest_waiting_at": _iso(oldest_waiting),
            "oldest_waiting_age": _age(oldest_waiting, now),
        },
        "gates": {
            "cleared": _int(gates.get("cleared")),
            "pending": _int(gates.get("pending")),
            "overdue": _int(gates.get("overdue")),
            "next_due_at": _iso(gates.get("next_due_at")),
            "median_seconds_to_clear": (
                float(gates["median_seconds_to_clear"])
                if gates.get("median_seconds_to_clear") is not None
                else None
            ),
            "by_kind": [
                {
                    "gate_kind": str(row["gate_kind"]),
                    "cleared": _int(row["cleared"]),
                    "open": _int(row["open"]),
                    "overdue": _int(row.get("overdue")),
                }
                for row in sorted(gate_kinds, key=lambda row: str(row.get("gate_kind") or ""))
            ],
        },
        "reviewers": (
            None
            if reviewers is None
            else [
                {
                    "principal": str(row["principal"]),
                    "approvals": _int(row.get("approvals")),
                    "rejections": _int(row.get("rejections")),
                    "changes_requested": _int(row.get("changes_requested")),
                    "abstentions": _int(row.get("abstentions")),
                    "decisions": _int(row.get("decisions")),
                    "last_decided_at": _iso(row.get("last_decided_at")),
                }
                for row in reviewers
            ]
        ),
        "merges": {
            "in_window": _int(merges.get("merges_in_window")),
            "sealed_total": _int(merges.get("sealed_total")),
            "latest_sealed_at": _iso(merges.get("latest_sealed_at")),
            "latest_digest": (
                str(merges["latest_digest"]) if merges.get("latest_digest") else None
            ),
        },
        "drift": {
            "by_state": [{"state": str(row["state"]), "n": _int(row["n"])} for row in drift_rows],
            "open": _total(
                row for row in drift_rows if str(row.get("state")) in _OPEN_DRIFT_STATES
            ),
            "total": _total(drift_rows),
        },
        "runs": {
            "by_status": [
                {"status": str(row["status"]), "n": _int(row["n"])}
                for row in _ordered(runs, "status", _RUN_STATUS_ORDER)
            ],
            "total": _total(runs),
        },
        "launches": (
            None
            if launches is None
            else {
                "by_status": [
                    {"status": str(row["status"]), "n": _int(row["n"])}
                    for row in _ordered(launches, "status", _LAUNCH_STATUS_ORDER)
                ],
                "total": _total(launches),
            }
        ),
    }


# ---------------------------------------------------------------------------
# The sentences every surface says about what this document is, and about the
# panels a given role could not fill.
#
# One constant each, because the disclosure is the same fact on the console,
# in the Markdown export and in the MCP tool result, and three paraphrases of
# it would drift into three different strengths of claim.
#
# They are written as PLAIN PROSE -- no backticks, no asterisks. Each renderer
# adds its own emphasis; a shared string carrying Markdown would render as
# literal `**` in the HTML, which is how a disclosure starts looking like a
# typo and stops being read.
# ---------------------------------------------------------------------------

_LIVE_STATE_NOTE = (
    "This is live state as of the timestamp above, not a pinned artefact. The "
    "same dashboard rendered a minute later can legitimately differ. It "
    "aggregates counts only and cites no individual proposal as evidence -- "
    "open the review console to act on anything here."
)

_NO_REVIEWER_ACCESS = (
    "Not shown: the role that read this database cannot see hitl.reviewer, the "
    "table that turns a decision into a person's name. This is a deliberate "
    "grant boundary (db/010), not a missing panel. It is not a statement that "
    "nobody has decided anything."
)

_NO_LAUNCH_ACCESS = (
    "Not shown: the role that read this database cannot see wf.agent_launch. "
    "Only the console's gate role may read it -- db/018 withholds it from "
    "fde_agent and fde_prodops so a launch record cannot be read or written by "
    "anything that could misattribute it. This is not a statement that no "
    "agents ran."
)


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _md_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines += ["| " + " | ".join(cells) + " |" for cells in rows]
    return [*lines, ""]


def _md_head(data: Mapping[str, Any]) -> list[str]:
    """Front matter, title and the headline figures.

    The front matter carries `pinned: false` so a vault can filter this
    document away from the playbooks, which are pinned and diffable. It is
    the same disclosure as the prose note, in the one place a query can see.
    """
    engagement_id = data.get("engagement_id")
    window_days = _int(data.get("window_days"))
    queue, gates, merges, drift = data["queue"], data["gates"], data["merges"], data["drift"]
    return [
        "---",
        'title: "FDE decision dashboard"',
        f'scope: "{engagement_id or "all engagements"}"',
        f"window_days: {window_days}",
        f'generated_at: "{data.get("generated_at")}"',
        "pinned: false",
        "---",
        "",
        "# Decision dashboard",
        "",
        f"Scope: {_scope(engagement_id)} · window: last {window_days} days · "
        f"generated {data.get('generated_at')}",
        "",
        f"> {_LIVE_STATE_NOTE}",
        "",
        "## Headline",
        "",
        f"- **{queue['open']}** proposals waiting on a human (of {queue['total']} on record)",
        f"- **{gates['overdue']}** gates overdue, {gates['pending']} pending, "
        f"{gates['cleared']} cleared",
        f"- Oldest still waiting: **{queue['oldest_waiting_age']}**",
        f"- Median time from submission to a gate clearing: "
        f"**{_duration(gates['median_seconds_to_clear'])}**",
        f"- **{merges['in_window']}** merges in the window "
        f"({merges['sealed_total']} sealed commits in total)",
        f"- **{drift['open']}** drift signals still open",
        "",
    ]


def _md_queue(queue: Mapping[str, Any]) -> list[str]:
    lines = ["## Review queue", ""]
    if not queue["by_status"]:
        return [*lines, "No proposals on record for this scope.", ""]
    return lines + _md_table(
        ("Status", "Proposals", "Oldest submitted"),
        [
            (_label(row["status"]), str(row["n"]), row["oldest_submitted_at"] or "—")
            for row in queue["by_status"]
        ],
    )


def _md_gates(gates: Mapping[str, Any]) -> list[str]:
    lines = ["## Gates", ""]
    lines += _md_table(
        ("State", "Gates"),
        [
            ("Cleared", str(gates["cleared"])),
            ("Pending", str(gates["pending"])),
            ("Overdue", str(gates["overdue"])),
        ],
    )
    lines += [
        f"Median time to clear: **{_duration(gates['median_seconds_to_clear'])}**. "
        f"Next gate due: {gates['next_due_at'] or 'nothing outstanding'}.",
        "",
    ]
    if gates["by_kind"]:
        lines += ["### By gate kind", ""]
        lines += _md_table(
            ("Gate kind", "Cleared", "Open", "Overdue"),
            [
                (
                    _label(row["gate_kind"]),
                    str(row["cleared"]),
                    str(row["open"]),
                    str(row["overdue"]),
                )
                for row in gates["by_kind"]
            ],
        )
    return lines


def _md_reviewers(reviewers: Sequence[Mapping[str, Any]] | None, window_days: int) -> list[str]:
    lines = ["## Decisions by reviewer", ""]
    if reviewers is None:
        return [*lines, _NO_REVIEWER_ACCESS, ""]
    if not reviewers:
        return [*lines, f"No decisions recorded in the last {window_days} days.", ""]
    return lines + _md_table(
        ("Reviewer", "Approved", "Changes requested", "Rejected", "Abstained", "Last decision"),
        [
            (
                row["principal"],
                str(row["approvals"]),
                str(row["changes_requested"]),
                str(row["rejections"]),
                str(row["abstentions"]),
                row["last_decided_at"] or "—",
            )
            for row in reviewers
        ],
    )


def _md_merges(merges: Mapping[str, Any], window_days: int) -> list[str]:
    return [
        "## Merges",
        "",
        f"**{merges['in_window']}** commits sealed in the last {window_days} days; "
        f"**{merges['sealed_total']}** on record for this scope.",
        "",
        f"Latest sealed commit: `{merges['latest_digest'] or 'none'}` "
        f"at {merges['latest_sealed_at'] or '—'}.",
        "",
    ]


def _md_counts(
    heading: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    key: str,
    value_header: str,
    empty: str,
) -> list[str]:
    """A `## heading` over a two-column count table, or a sentence saying
    there is nothing to count. Drift, runs and launches are the same shape."""
    lines = [f"## {heading}", ""]
    if not rows:
        return [*lines, empty, ""]
    return lines + _md_table(
        ("State" if key == "state" else "Status", value_header),
        [(_label(str(row[key])), str(row["n"])) for row in rows],
    )


def render_dashboard_markdown(data: Mapping[str, Any]) -> str:
    """Render the assembled dashboard as Obsidian-ready Markdown.

    Pure. Takes the dict `assemble_dashboard` returns and nothing else --
    in particular it does not read the clock, so the same data renders the
    same bytes. Plain CommonMark plus YAML front matter: it renders in a
    vault, on GitHub and in a terminal pager alike.
    """
    window_days = _int(data.get("window_days"))
    launches = data.get("launches")
    launch_lines = (
        ["## Agent launches", "", _NO_LAUNCH_ACCESS, ""]
        if launches is None
        else _md_counts(
            "Agent launches",
            launches["by_status"],
            key="status",
            value_header="Launches",
            empty=f"No agent launches requested in the last {window_days} days.",
        )
    )

    lines = [
        *_md_head(data),
        *_md_queue(data["queue"]),
        *_md_gates(data["gates"]),
        *_md_reviewers(data.get("reviewers"), window_days),
        *_md_merges(data["merges"], window_days),
        *_md_counts(
            "Drift",
            data["drift"]["by_state"],
            key="state",
            value_header="Signals",
            empty="No drift signals recorded for this scope.",
        ),
        *_md_counts(
            "Workflow runs",
            data["runs"]["by_status"],
            key="status",
            value_header="Runs",
            empty=f"No workflow runs started in the last {window_days} days.",
        ),
        *launch_lines,
    ]
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# HTML
#
# Self-contained by construction: one scoped <style>, no external stylesheet,
# no script, no image, no font. It is emitted as a <section> rather than a
# whole document so the console can drop it into its own chrome inline
# (an iframe would need a second request and would not inherit page width),
# and a browser opening the saved .html on its own renders a fragment fine.
# Every selector and every custom property is namespaced under `.fde-dash` so
# nothing here can reach the console's own styles, or be reached by them.
#
# Colours follow the dataviz skill's status palette (good/warning/critical),
# which is fixed and never themed, over its light/dark chart surfaces. Dark
# mode is declared twice on purpose -- once under `prefers-color-scheme` for
# the OS setting and once under `[data-theme]` for an explicit toggle, so the
# toggle wins in both directions. Status colour never carries meaning alone:
# every segment is paired with a text label and a count in the legend and
# repeated in the table beneath it, which is also what keeps the two
# sub-3:1-on-light status steps (warning, serious) legible.
# ---------------------------------------------------------------------------

_CSS = """
.fde-dash{color-scheme:light;
  --surface:#fcfcfb;--plane:#f9f9f7;--ink:#0b0b0b;--ink-2:#52514e;--ink-3:#898781;
  --rule:#e1e0d9;--ring:rgba(11,11,11,.10);
  --good:#0ca30c;--warn:#fab219;--crit:#d03b3b;--info:#2a78d6;--inert:#c3c2b7;
  font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink);
  background:var(--plane);padding:1.25rem;border-radius:10px;line-height:1.45}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .fde-dash{
  color-scheme:dark;--surface:#1a1a19;--plane:#0d0d0d;--ink:#fff;--ink-2:#c3c2b7;
  --ink-3:#898781;--rule:#2c2c2a;--ring:rgba(255,255,255,.10);--info:#3987e5;--inert:#383835}}
:root[data-theme="dark"] .fde-dash{
  color-scheme:dark;--surface:#1a1a19;--plane:#0d0d0d;--ink:#fff;--ink-2:#c3c2b7;
  --ink-3:#898781;--rule:#2c2c2a;--ring:rgba(255,255,255,.10);--info:#3987e5;--inert:#383835}
.fde-dash *{box-sizing:border-box}
.fde-dash h2{font-size:1.05rem;margin:1.6rem 0 .6rem;color:var(--ink)}
.fde-dash h3{font-size:.9rem;margin:1.1rem 0 .4rem;color:var(--ink-2);font-weight:600}
.fde-dash .dash-scope{color:var(--ink-2);font-size:.85rem;margin:0 0 .75rem}
.fde-dash .dash-note{background:var(--surface);border:1px solid var(--ring);
  border-left:3px solid var(--info);border-radius:6px;padding:.6rem .8rem;
  color:var(--ink-2);font-size:.83rem;margin:.75rem 0 0}
.fde-dash .dash-kpis{display:flex;flex-wrap:wrap;gap:.75rem;margin:1rem 0 0}
.fde-dash .dash-kpi{background:var(--surface);border:1px solid var(--ring);
  border-radius:8px;padding:.7rem .9rem;min-width:9.5rem;flex:1 1 9.5rem}
.fde-dash .dash-kpi .k{display:block;color:var(--ink-3);font-size:.72rem;
  text-transform:uppercase;letter-spacing:.04em}
.fde-dash .dash-kpi .v{display:block;font-size:1.7rem;font-weight:600;color:var(--ink);
  margin-top:.15rem}
.fde-dash .dash-kpi .s{display:block;color:var(--ink-2);font-size:.76rem}
.fde-dash table{border-collapse:collapse;width:100%;margin:.5rem 0;font-size:.85rem;
  background:var(--surface);border:1px solid var(--ring);border-radius:6px}
.fde-dash th,.fde-dash td{text-align:left;padding:.4rem .6rem;
  border-bottom:1px solid var(--rule)}
.fde-dash th{color:var(--ink-3);font-weight:600;font-size:.75rem;
  text-transform:uppercase;letter-spacing:.03em}
.fde-dash tr:last-child td{border-bottom:0}
.fde-dash td.num{text-align:right;font-variant-numeric:tabular-nums}
.fde-dash .dash-legend{display:flex;flex-wrap:wrap;gap:.9rem;margin:.45rem 0 .2rem;
  font-size:.8rem;color:var(--ink-2)}
.fde-dash .dash-legend span{display:inline-flex;align-items:center;gap:.35rem}
.fde-dash .dash-legend i{width:.6rem;height:.6rem;border-radius:2px;display:inline-block}
.fde-dash .dash-empty{color:var(--ink-2);font-size:.85rem;background:var(--surface);
  border:1px dashed var(--rule);border-radius:6px;padding:.6rem .8rem;margin:.5rem 0}
.fde-dash .dash-blocked{color:var(--ink-2);font-size:.83rem;background:var(--surface);
  border:1px solid var(--ring);border-left:3px solid var(--warn);border-radius:6px;
  padding:.6rem .8rem;margin:.5rem 0}
.fde-dash code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85em}
"""


def _esc(value: Any) -> str:
    """Escape for HTML text and attribute content alike.

    Everything data-derived goes through this -- reviewer principals, gate
    kinds, statuses, digests, the engagement id. The demo roster is all
    `@example.com` and the enum columns cannot hold anything exotic, but a
    renderer that is safe only because of what happens to be in the database
    is a renderer that is not safe.
    """
    return escape("" if value is None else str(value), quote=True)


# Segment colours by role, not by position: a status keeps its colour when a
# neighbouring status drops to zero and disappears from the bar.
_SEGMENT_COLOURS = {
    "cleared": "var(--good)",
    "pending": "var(--warn)",
    "overdue": "var(--crit)",
    "succeeded": "var(--good)",
    "failed": "var(--crit)",
    "running": "var(--info)",
    "queued": "var(--inert)",
    "pending_run": "var(--inert)",
    "awaiting_human": "var(--warn)",
    "cancelled": "var(--inert)",
    "open": "var(--warn)",
    "triaged": "var(--info)",
    "proposal_raised": "var(--info)",
    "accepted": "var(--good)",
    "resolved": "var(--good)",
    "dismissed": "var(--inert)",
    "submitted": "var(--warn)",
    "in_review": "var(--info)",
    "changes_requested": "var(--crit)",
    "merged": "var(--good)",
    "approved": "var(--good)",
    "rejected": "var(--inert)",
    "draft": "var(--inert)",
    "expired": "var(--inert)",
}


def _colour(name: str) -> str:
    return _SEGMENT_COLOURS.get(name, "var(--info)")


def _bar(segments: Sequence[tuple[str, int]], *, ident: str) -> str:
    """One horizontal part-to-whole bar, plus its legend.

    Segments are separated by a 2px gap in the surface colour rather than by
    a stroke (the dataviz skill's surface-gap rule): a border would add ink
    that is not data. Zero-count segments are dropped from the bar but stay
    in the legend and the table, so a status at zero reads as zero rather
    than as absent.
    """
    total = sum(count for _, count in segments)
    legend = "".join(
        f'<span><i style="background:{_colour(name)}"></i>{_esc(_label(name))} {count}</span>'
        for name, count in segments
    )
    if total <= 0:
        return f'<div class="dash-legend">{legend}</div>'

    width, height, gap = 100.0, 12, 0.4
    drawn = [(name, count) for name, count in segments if count > 0]
    parts: list[str] = []
    cursor = 0.0
    for position, (name, count) in enumerate(drawn):
        span = width * count / total
        # Every segment but the last gives up `gap` to the surface, so the
        # gaps come out of the fills rather than out of the bar's length.
        visible = span - (gap if position < len(drawn) - 1 else 0.0)
        if visible > 0:
            parts.append(
                f'<rect x="{cursor:.3f}" y="0" width="{visible:.3f}" height="{height}" '
                f'fill="{_colour(name)}"/>'
            )
        cursor += span
    described = ", ".join(f"{_label(name)} {count}" for name, count in segments)
    return (
        f'<svg class="dash-bar" viewBox="0 0 {width:.0f} {height}" width="100%" '
        f'height="{height}" preserveAspectRatio="none" role="img" '
        f'aria-label="{_esc(described)}">'
        f'<clipPath id="{_esc(ident)}"><rect x="0" y="0" width="{width:.0f}" '
        f'height="{height}" rx="3"/></clipPath>'
        f'<g clip-path="url(#{_esc(ident)})">{"".join(parts)}</g></svg>'
        f'<div class="dash-legend">{legend}</div>'
    )


def _html_table(headers: Sequence[str], rows: Sequence[Sequence[tuple[str, bool]]]) -> str:
    """A table. Each cell is `(text, is_numeric)`; numerics right-align with
    tabular figures so columns of counts line up."""
    head = "".join(f"<th>{_esc(header)}</th>" for header in headers)
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="num">{_esc(text)}</td>' if numeric else f"<td>{_esc(text)}</td>"
            for text, numeric in row
        )
        + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _kpi(label: str, value: str, sub: str = "") -> str:
    sub_html = f'<span class="s">{_esc(sub)}</span>' if sub else ""
    return (
        f'<div class="dash-kpi"><span class="k">{_esc(label)}</span>'
        f'<span class="v">{_esc(value)}</span>{sub_html}</div>'
    )


def _html_counts(
    heading: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    key: str,
    value_header: str,
    empty: str,
    ident: str,
) -> list[str]:
    """A heading over a proportion bar and its table, or an empty-state line.

    Drift, runs and launches are the same shape, and the bar is always
    accompanied by the table: the table is what makes the two status steps
    that sit under 3:1 on the light surface readable, and what a screen
    reader gets instead of the bar.
    """
    out = [f"<h2>{_esc(heading)}</h2>"]
    if not rows:
        return [*out, f'<p class="dash-empty">{_esc(empty)}</p>']
    out.append(_bar([(str(row[key]), _int(row["n"])) for row in rows], ident=ident))
    out.append(
        _html_table(
            ("State" if key == "state" else "Status", value_header),
            [((_label(str(row[key])), False), (str(row["n"]), True)) for row in rows],
        )
    )
    return out


def _html_head(data: Mapping[str, Any], *, titled: bool) -> list[str]:
    """Scoped stylesheet, scope line, the KPI row and the live-state note."""
    engagement_id = data.get("engagement_id")
    window_days = _int(data.get("window_days"))
    queue, gates, merges, drift = data["queue"], data["gates"], data["merges"], data["drift"]
    return [
        f"<style>{_CSS}</style>",
        '<section class="fde-dash">',
        *(["<h2>Decision dashboard</h2>"] if titled else []),
        f'<p class="dash-scope">Scope: '
        f"{_esc(engagement_id) if engagement_id else 'all engagements'} · "
        f"window: last {window_days} days · generated "
        f"{_esc(data.get('generated_at'))}</p>",
        '<div class="dash-kpis">',
        _kpi("Waiting on a human", str(queue["open"]), f"of {queue['total']} proposals"),
        _kpi("Gates overdue", str(gates["overdue"]), f"{gates['pending']} pending"),
        _kpi("Oldest waiting", queue["oldest_waiting_age"]),
        _kpi("Median to clear", _duration(gates["median_seconds_to_clear"])),
        _kpi("Merges in window", str(merges["in_window"]), f"{merges['sealed_total']} total"),
        _kpi("Drift open", str(drift["open"]), f"of {drift['total']} signals"),
        "</div>",
        f'<p class="dash-note">{_esc(_LIVE_STATE_NOTE)}</p>',
    ]


def _html_queue(queue: Mapping[str, Any]) -> list[str]:
    out = ["<h2>Review queue</h2>"]
    if not queue["by_status"]:
        return [*out, '<p class="dash-empty">No proposals on record for this scope.</p>']
    out.append(
        _bar(
            [(str(row["status"]), _int(row["n"])) for row in queue["by_status"]],
            ident="dash-clip-queue",
        )
    )
    out.append(
        _html_table(
            ("Status", "Proposals", "Oldest submitted"),
            [
                (
                    (_label(row["status"]), False),
                    (str(row["n"]), True),
                    (row["oldest_submitted_at"] or "—", False),
                )
                for row in queue["by_status"]
            ],
        )
    )
    return out


def _html_gates(gates: Mapping[str, Any]) -> list[str]:
    out = [
        "<h2>Gates</h2>",
        _bar(
            [
                ("cleared", _int(gates["cleared"])),
                ("pending", _int(gates["pending"])),
                ("overdue", _int(gates["overdue"])),
            ],
            ident="dash-clip-gates",
        ),
        _html_table(
            ("State", "Gates"),
            [
                (("Cleared", False), (str(gates["cleared"]), True)),
                (("Pending", False), (str(gates["pending"]), True)),
                (("Overdue", False), (str(gates["overdue"]), True)),
            ],
        ),
        f'<p class="dash-scope">Median time to clear: '
        f"<strong>{_esc(_duration(gates['median_seconds_to_clear']))}</strong>. "
        f"Next gate due: {_esc(gates['next_due_at'] or 'nothing outstanding')}.</p>",
    ]
    if gates["by_kind"]:
        out.append("<h3>By gate kind</h3>")
        out.append(
            _html_table(
                ("Gate kind", "Cleared", "Open", "Overdue"),
                [
                    (
                        (_label(row["gate_kind"]), False),
                        (str(row["cleared"]), True),
                        (str(row["open"]), True),
                        (str(row["overdue"]), True),
                    )
                    for row in gates["by_kind"]
                ],
            )
        )
    return out


def _html_reviewers(reviewers: Sequence[Mapping[str, Any]] | None, window_days: int) -> list[str]:
    out = ["<h2>Decisions by reviewer</h2>"]
    if reviewers is None:
        return [*out, f'<p class="dash-blocked">{_esc(_NO_REVIEWER_ACCESS)}</p>']
    if not reviewers:
        return [
            *out,
            f'<p class="dash-empty">No decisions recorded in the last {window_days} days.</p>',
        ]
    out.append(
        _html_table(
            ("Reviewer", "Approved", "Changes requested", "Rejected", "Abstained", "Last decision"),
            [
                (
                    (row["principal"], False),
                    (str(row["approvals"]), True),
                    (str(row["changes_requested"]), True),
                    (str(row["rejections"]), True),
                    (str(row["abstentions"]), True),
                    (row["last_decided_at"] or "—", False),
                )
                for row in reviewers
            ],
        )
    )
    return out


def _html_merges(merges: Mapping[str, Any], window_days: int) -> list[str]:
    return [
        "<h2>Merges</h2>",
        f'<p class="dash-scope"><strong>{merges["in_window"]}</strong> commits sealed '
        f"in the last {window_days} days; <strong>{merges['sealed_total']}</strong> "
        f"on record. Latest sealed commit "
        f"<code>{_esc(merges['latest_digest'] or 'none')}</code> at "
        f"{_esc(merges['latest_sealed_at'] or '—')}.</p>",
    ]


def render_dashboard_html(data: Mapping[str, Any], *, titled: bool = True) -> str:
    """Render the assembled dashboard as a self-contained HTML section.

    Pure, and the same numbers as `render_dashboard_markdown` over the same
    data -- a test asserts that, because two surfaces disagreeing about the
    count of overdue gates is the failure this whole module exists to avoid.

    Returns a `<section class="fde-dash">` carrying its own scoped `<style>`.
    No external stylesheet, script, font or image: it renders identically
    saved to disk, inlined into the console, or pasted into an email.

    Args:
        data: The dict `assemble_dashboard` returns.
        titled: Whether to emit the section's own "Decision dashboard"
            heading. True standalone -- a file that does not name itself is
            an anonymous wall of numbers. False when a host page already
            carries that title in its own `<h1>`, as the console does: the
            heading rendered twice within one screen reads as a template bug
            to exactly the non-technical operator this page is for. The
            scope line stays either way, because `generated_at` is part of
            what the numbers MEAN and no host page carries it.
    """
    window_days = _int(data.get("window_days"))
    launches = data.get("launches")
    launch_html = (
        ["<h2>Agent launches</h2>", f'<p class="dash-blocked">{_esc(_NO_LAUNCH_ACCESS)}</p>']
        if launches is None
        else _html_counts(
            "Agent launches",
            launches["by_status"],
            key="status",
            value_header="Launches",
            empty=f"No agent launches requested in the last {window_days} days.",
            ident="dash-clip-launches",
        )
    )

    out = [
        *_html_head(data, titled=titled),
        *_html_queue(data["queue"]),
        *_html_gates(data["gates"]),
        *_html_reviewers(data.get("reviewers"), window_days),
        *_html_merges(data["merges"], window_days),
        *_html_counts(
            "Drift",
            data["drift"]["by_state"],
            key="state",
            value_header="Signals",
            empty="No drift signals recorded for this scope.",
            ident="dash-clip-drift",
        ),
        *_html_counts(
            "Workflow runs",
            data["runs"]["by_status"],
            key="status",
            value_header="Runs",
            empty=f"No workflow runs started in the last {window_days} days.",
            ident="dash-clip-runs",
        ),
        *launch_html,
        "</section>",
    ]
    return "\n".join(out) + "\n"
