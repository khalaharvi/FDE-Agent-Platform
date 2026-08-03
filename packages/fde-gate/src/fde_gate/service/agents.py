"""service/agents.py -- launching an agent task from the console.

Before this, the two tasks a product-operations operator actually needs --
`ingest_interview` and `author_workflow` -- were reachable only by typing a
JSON object into `fde-agents-local` in a terminal, by posting raw HTTP at an
AgentCore runtime, or by burying an `agent` step inside a workflow that had
to be authored and published first. The persona this console is for does
none of those things. So the task list becomes a dropdown, each task's input
becomes labelled fields, and dispatch goes through the executor the workflow
runner already uses.

Dispatch is not re-implemented here
------------------------------------
`executors.AgentExecutor` owns the AgentCore call: which runtime ARN a
persona resolves to, the >=33-character session id, the payload shape, the
SSE decode, the timeout. This module builds the `step`/`run` pair that
executor already takes and hands it over. A second invocation path would be
a second place for the payload contract to drift from what
`fde_agents.common.runtime` parses -- and the console's copy would be the
one nobody tested against a live runtime.

The synthetic `run` carries a session id on purpose. `AgentExecutor` stamps
`wf.run.runtime_session_id` only when the run it was given does not already
have one, so pre-filling it is what keeps a console launch -- which belongs
to no `wf.run` -- from issuing an UPDATE against a row that does not exist.

Why the task list is copied instead of imported
------------------------------------------------
`fde-gate`'s whole reason for being a separate package is its dependency
ceiling (see its pyproject: "must never pull strands-agents or the AgentCore
SDK"), and `fde_agents` pulls both. So `AGENT_TASKS` below is a copy, and
`tests/test_agent_launcher.py::test_task_lists_match_the_agents` imports the
real ones and fails when this copy falls behind.

The one thing this writes
--------------------------
`wf.agent_launch`, and only that -- one row per launch, filed before the
dispatch and stamped with how it ended (db/018). Everything else about a
launch still happens elsewhere: the agent's own MCP session is what
proposes, under `fde_agent`, through the grants db/010 and db/011 already
draw. The invariant is untouched, and the denial matrix says so from the
outside: a console launch cannot write the graph any more than a terminal
one can, and the record it leaves is not a graph fact -- it is the receipt
for having asked.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from fde_gate.config import get_gate_settings
from fde_gate.executors import AgentExecutor, StepExecutionError, new_runtime_session_id
from fde_gate.forms import Field, values_from_form
from fde_gate.http import GateError
from fde_gate.rows import fetchall, fetchone
from fde_gate.service import sources, workflows
from fde_mcp import db
from fde_mcp.logging import get_logger

if TYPE_CHECKING:
    from fde_gate.executors import StepExecutor

log = get_logger(__name__)

__all__ = [
    "AGENT_TASKS",
    "TASK_SUMMARY",
    "launch",
    "launcher_context",
    "list_launches",
    "task_fields",
]

# ---------------------------------------------------------------------------
# The mirrored registry. SOURCE OF TRUTH -- do not edit these tuples without
# editing the agents:
#
#   engagement -> packages/fde-agents/src/fde_agents/engagement/agent.py
#                 VALID_TASKS
#   workflow   -> packages/fde-agents/src/fde_agents/workflow/agent.py
#                 VALID_TASKS
#
# Ordered by how often this console's operator wants them, not alphabetically:
# a dropdown's first entry is the one it is really for.
#
# The `development` agent is deliberately absent. Its tasks author agents
# (generate_evals, scaffold_agent, review_agent) -- an engineering surface
# with a read-only tool filter, invoked from a terminal by whoever is
# building an agent, not from the product-operations console. The drift test
# asserts that absence too, so a fourth agent cannot appear here by accident
# or vanish from here by accident.
# ---------------------------------------------------------------------------
AGENT_TASKS: dict[str, tuple[str, ...]] = {
    "engagement": (
        "ingest_interview",
        "map_process",
        "detect_bottlenecks",
        "score_opportunities",
    ),
    "workflow": (
        "author_workflow",
        "monitor_drift",
        "triage_drift",
        "reauthor_stale",
    ),
}

#: One line per task, shown under the picker. Written for someone who has
#: never read an agent prompt -- the task NAMES are the agents' vocabulary,
#: not an operator's.
TASK_SUMMARY: dict[str, str] = {
    "ingest_interview": (
        "Read an interview or document and propose the facts it contains as graph nodes and edges."
    ),
    "map_process": (
        "Map one process end to end -- its activities, roles, systems and "
        "control flow -- and propose the result."
    ),
    "detect_bottlenecks": (
        "Look for pain points in a process, cross-referenced against the "
        "drift signals monitoring has already raised."
    ),
    "score_opportunities": (
        "Score a process's activities for automation opportunity on the six-dimension rubric."
    ),
    "author_workflow": (
        "Draft a runnable workflow from a mapped process. The draft still "
        "has to be reviewed and published before anyone can run it."
    ),
    "monitor_drift": (
        "Run the drift detectors, triage everything they surface, and "
        "propose fixes where the evidence supports one."
    ),
    "triage_drift": "Triage specific drift signals you have already seen on the Drift page.",
    "reauthor_stale": (
        "Check whether a published workflow's pinned commit has moved under "
        "it, and draft a new version if it has."
    ),
}

#: `sor.drift_severity`, in the order db/001:134 declares it.
_SEVERITIES: tuple[str, ...] = ("info", "low", "medium", "high", "critical")

#: The empty entry that turns a source picker into "picker OR paste box".
#: Spelled as an option rather than a second radio button because a select
#: with an explicit "none" is one control an operator has to answer once.
_PASTE_OPTION = ("", "— paste the text below instead —")

# Which pickers a task's form needs filled. `engagement_id` is not in any of
# them: it selects which graph the task runs against, so it is chosen on the
# step-one picker, before there is a task to have fields for.
_NEEDS_SOURCES = frozenset({"ingest_interview"})
_NEEDS_PROCESSES = frozenset(
    {"map_process", "detect_bottlenecks", "score_opportunities", "author_workflow"}
)
_NEEDS_WORKFLOWS = frozenset({"reauthor_stale"})

_NOT_A_REVIEWER = (
    "{actor} is not an active reviewer, and launching an agent is attributed "
    "to a person -- the proposal it raises records who asked for it. Ask an "
    "administrator to add you on /ui/reviewers (or to reactivate you), then "
    "try again."
)


def _gate_role() -> str:
    return get_gate_settings().gate.gate_role


async def _assert_active_reviewer(cur: Any, actor: str) -> None:
    """Refuse anyone who is not a live reviewer, in the caller's transaction.

    Same shape and same placement as `sources._assert_active_reviewer`, and
    for the same reason -- inside the transaction that reads, so the roster
    cannot change between the check and the work. The message differs because
    the two acts differ; a shared one would have to describe neither.
    """
    if not actor:
        raise GateError(HTTPStatus.FORBIDDEN, _NOT_A_REVIEWER.format(actor="anonymous"))
    await cur.execute(
        "SELECT 1 AS ok FROM hitl.reviewer WHERE principal = %(p)s AND is_active",
        {"p": actor},
    )
    if await fetchone(cur) is None:
        raise GateError(HTTPStatus.FORBIDDEN, _NOT_A_REVIEWER.format(actor=actor))


# ---------------------------------------------------------------------------
# The per-task forms
# ---------------------------------------------------------------------------


def _validate_choice(agent: str, task: str) -> None:
    if agent not in AGENT_TASKS:
        raise GateError(
            HTTPStatus.BAD_REQUEST,
            f"{agent!r} is not an agent this console can launch. Choose one of: "
            f"{', '.join(sorted(AGENT_TASKS))}.",
        )
    if task not in AGENT_TASKS[agent]:
        raise GateError(
            HTTPStatus.BAD_REQUEST,
            f"{task!r} is not a task the {agent} agent performs. Choose one of: "
            f"{', '.join(AGENT_TASKS[agent])}.",
        )


def task_fields(
    agent: str,
    task: str,
    *,
    source_options: tuple[tuple[str, str], ...] = (),
    process_options: tuple[tuple[str, str], ...] = (),
    workflow_options: tuple[tuple[str, str], ...] = (),
) -> list[Field]:
    """The labelled fields one task's input is built from.

    The three option tuples are passed in rather than fetched here because
    the POST path needs the field NAMES and types to read a submission back,
    and re-running three queries to coerce a form that has already been
    filled in would be three queries spent on nothing.
    """
    _validate_choice(agent, task)
    builder = _FIELD_BUILDERS[task]
    return builder(source_options, process_options, workflow_options)


def _ingest_interview_fields(
    source_options: tuple[tuple[str, str], ...],
    _processes: tuple[tuple[str, str], ...],
    _workflows: tuple[tuple[str, str], ...],
) -> list[Field]:
    """A registered source, or pasted text. Never both -- see `_material`."""
    return [
        Field(
            name="source_id",
            label="Registered source",
            options=(_PASTE_OPTION, *source_options),
            hint=(
                "Everything registered on this engagement. Registering it first "
                "(Sources → New source) is what gets the text anchored so search "
                "can reach it afterwards."
            ),
        ),
        Field(
            name="material",
            label="…or paste the transcript",
            rows=14,
            placeholder="Paste an interview transcript or document…",
            hint="Only fill this in if you did not pick a registered source above.",
        ),
    ]


def _map_process_fields(
    _sources: tuple[tuple[str, str], ...],
    process_options: tuple[tuple[str, str], ...],
    _workflows: tuple[tuple[str, str], ...],
) -> list[Field]:
    return [
        Field(
            name="process_key",
            label="Existing process to re-map",
            options=(("", "— none; describe a new one below —"), *process_options),
            hint="Leave this empty when the process is not in the graph yet.",
        ),
        Field(
            name="description",
            label="…or describe the process",
            rows=3,
            placeholder="Quote to cash, from the rep building a quote to booked revenue",
            hint="Used when no existing process is selected.",
        ),
        Field(
            name="material",
            label="Supporting material",
            rows=10,
            placeholder="Interview notes, SOP excerpts, ticket exports…",
            hint="Optional. Without it the agent works from what is already in the graph.",
        ),
    ]


def _detect_bottlenecks_fields(
    _sources: tuple[tuple[str, str], ...],
    process_options: tuple[tuple[str, str], ...],
    _workflows: tuple[tuple[str, str], ...],
) -> list[Field]:
    return [
        Field(
            name="process_key",
            label="Process",
            options=process_options,
            required=True,
            hint="The process to look for pain points in.",
        )
    ]


def _score_opportunities_fields(
    _sources: tuple[tuple[str, str], ...],
    process_options: tuple[tuple[str, str], ...],
    _workflows: tuple[tuple[str, str], ...],
) -> list[Field]:
    return [
        Field(
            name="process_key",
            label="Process",
            options=(("", "— score specific activities instead —"), *process_options),
            hint="Scores every activity in this process.",
        ),
        Field(
            name="activity_keys",
            label="…or specific activity keys",
            value_type="array",
            hint="One per line, or separated by commas. Used when no process is selected.",
        ),
    ]


def _author_workflow_fields(
    _sources: tuple[tuple[str, str], ...],
    process_options: tuple[tuple[str, str], ...],
    _workflows: tuple[tuple[str, str], ...],
) -> list[Field]:
    return [
        Field(
            name="root_process_key",
            label="Process to turn into a workflow",
            options=process_options,
            required=True,
            hint=(
                "Only mapped processes appear here -- a workflow's steps are "
                "derived from the process's activities, so there has to be one."
            ),
        ),
        Field(
            name="title",
            label="Workflow title",
            required=True,
            placeholder="Quote to cash discount approval",
        ),
        Field(
            name="slug",
            label="Short name",
            required=True,
            placeholder="q2c-discount-approval",
            hint="Lower case, dashes. Versions of the same workflow share it.",
        ),
    ]


def _monitor_drift_fields(
    _sources: tuple[tuple[str, str], ...],
    _processes: tuple[tuple[str, str], ...],
    _workflows: tuple[tuple[str, str], ...],
) -> list[Field]:
    return [
        Field(
            name="min_severity",
            label="Triage signals at least this severe",
            options=tuple((sev, sev) for sev in _SEVERITIES),
            value="medium",
            hint="Anything below this is scanned for but left alone.",
        )
    ]


def _triage_drift_fields(
    _sources: tuple[tuple[str, str], ...],
    _processes: tuple[tuple[str, str], ...],
    _workflows: tuple[tuple[str, str], ...],
) -> list[Field]:
    return [
        Field(
            name="signal_ids",
            label="Signal numbers",
            value_type="array",
            # Integers, so the agent's prompt renders [41, 42] rather than
            # ['41', '42'] -- it matches these against `drift_list` output,
            # where signal_id is a number.
            item_type="integer",
            required=True,
            placeholder="41\n42",
            rows=3,
            hint="From the Drift page -- one per line, or separated by commas.",
        )
    ]


def _reauthor_stale_fields(
    _sources: tuple[tuple[str, str], ...],
    _processes: tuple[tuple[str, str], ...],
    workflow_options: tuple[tuple[str, str], ...],
) -> list[Field]:
    return [
        Field(
            name="workflow_id",
            label="Workflow",
            value_type="integer",
            options=workflow_options,
            required=True,
            hint="The agent reports back when nothing material has actually changed.",
        )
    ]


#: (sources, processes, workflows) -> fields. One positional shape for all
#: eight builders, so the dispatch table needs no per-task call site.
_Options = tuple[tuple[str, str], ...]
_FieldBuilder = Callable[[_Options, _Options, _Options], list[Field]]

_FIELD_BUILDERS: dict[str, _FieldBuilder] = {
    "ingest_interview": _ingest_interview_fields,
    "map_process": _map_process_fields,
    "detect_bottlenecks": _detect_bottlenecks_fields,
    "score_opportunities": _score_opportunities_fields,
    "author_workflow": _author_workflow_fields,
    "monitor_drift": _monitor_drift_fields,
    "triage_drift": _triage_drift_fields,
    "reauthor_stale": _reauthor_stale_fields,
}

# Every task in the mirrored registry has to have a form, or the page renders
# a KeyError at the moment an operator picks it. Checked at import so a task
# added to AGENT_TASKS without fields fails the whole suite instead of one
# dropdown selection nobody tried.
assert set(_FIELD_BUILDERS) == {task for tasks in AGENT_TASKS.values() for task in tasks}, (
    "every task in AGENT_TASKS needs an entry in _FIELD_BUILDERS"
)


# ---------------------------------------------------------------------------
# What the pickers are filled from
# ---------------------------------------------------------------------------

_PROCESS_SQL = """
SELECT n.node_key, n.label,
       count(e.src_key) AS activity_count
  FROM kg.node_current n
  LEFT JOIN kg.edge_current e
         ON e.engagement_id = n.engagement_id
        AND e.edge_type     = 'belongs_to'
        AND e.dst_key       = n.node_key
 WHERE n.engagement_id = %(eng)s::uuid
   AND n.node_type     = 'process'
 GROUP BY n.node_key, n.label
 ORDER BY n.label, n.node_key
"""


async def _process_options(cur: Any, engagement_id: str) -> tuple[tuple[str, str], ...]:
    """Live process nodes, labelled with how much of a flow each one has.

    `kg.process_flow` walks the `belongs_to` activities of a process key, so
    a process with none of them produces an empty flow -- which an operator
    should be able to see BEFORE launching `author_workflow` against it and
    reading a puzzled answer. This is a plain read of `kg.node_current` and
    `kg.edge_current`; db/008's retrieval SQL is not touched.
    """
    await cur.execute(_PROCESS_SQL, {"eng": engagement_id})
    rows = await fetchall(cur)
    options: list[tuple[str, str]] = []
    for row in rows:
        count = int(row["activity_count"])
        noun = "activity" if count == 1 else "activities"
        options.append((str(row["node_key"]), f"{row['label']} ({count} {noun})"))
    return tuple(options)


def _source_options(listing: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """Registered sources, labelled with what retrieval can currently see."""
    options: list[tuple[str, str]] = []
    for source in listing["sources"]:
        coverage = source["coverage"]
        if coverage.total == 0:
            # A `kg.source` row with no chunks is a real state -- an agent
            # that called kg_register_source and never got to
            # kg_ingest_chunks leaves one, and so does the demo seed. Saying
            # "0/0 passages searchable" describes it as a document search
            # cannot reach; there is no document. Picking it is refused, and
            # the label has to explain that BEFORE the click, not after.
            detail = "no text stored"
        else:
            detail = f"{coverage.anchored}/{coverage.total} passages searchable"
        options.append(
            (str(source["source_id"]), f"{source['title']} ({source['source_kind']}, {detail})")
        )
    return tuple(options)


async def launcher_context(
    actor: str,
    *,
    engagement_id: str | None = None,
    agent: str | None = None,
    task: str | None = None,
) -> dict[str, Any]:
    """Everything the launcher page renders, for whatever has been chosen.

    The page is a GET-driven two-step -- pick engagement/agent/task, then
    fill the fields -- because the fields depend on the task and this console
    has no JavaScript to swap them in place. Choosing is a GET, so the
    half-filled state is a URL an operator can bookmark or send to a
    colleague.

    Authorisation comes from `sources.list_sources`, which refuses anyone who
    is not an active reviewer and is already the source of the engagement
    list. One call, one refusal, no second implementation of either.
    """
    listing = await sources.list_sources(actor, engagement_id=engagement_id)

    fields: list[Field] = []
    if agent and task and engagement_id:
        _validate_choice(agent, task)
        processes: tuple[tuple[str, str], ...] = ()
        published: tuple[tuple[str, str], ...] = ()
        if task in _NEEDS_PROCESSES:
            async with db.tool_transaction(role=_gate_role()) as conn, conn.cursor() as cur:
                processes = await _process_options(cur, engagement_id)
        if task in _NEEDS_WORKFLOWS:
            listed = await workflows.list_workflows(status="published", engagement_id=engagement_id)
            published = tuple(
                (
                    str(row["workflow_id"]),
                    f"{row['title']} ({row['slug']} v{row['version']})",
                )
                for row in listed["workflows"]
            )
        fields = task_fields(
            agent,
            task,
            source_options=_source_options(listing) if task in _NEEDS_SOURCES else (),
            process_options=processes,
            workflow_options=published,
        )

    return {
        "engagements": listing["engagements"],
        "engagement_id": engagement_id,
        "agent": agent,
        "task": task,
        "agent_tasks": AGENT_TASKS,
        "task_summary": TASK_SUMMARY,
        "fields": fields,
    }


# ---------------------------------------------------------------------------
# The launch record (db/018)
#
# A launch used to leave nothing behind, and the deployed front door is an
# API Gateway HTTP API whose integration times out well before the wait
# below does -- so an operator could be shown an error for work that was
# still running, with no page anywhere that would later say whether it
# finished. The row is what makes that survivable. It is filed BEFORE the
# dispatch, in the transaction that authorised the caller, and stamped
# afterwards; a launch that dies at the gateway leaves a `running` row,
# which is the honest reading of "dispatched, and nothing reported back".
# ---------------------------------------------------------------------------

#: How many launches the console lists at once. Same ceiling `runs.list_runs`
#: applies, for the same reason -- a page is not an export.
_MAX_LAUNCHES = 500

#: The field whose value is a document rather than an answer. Every task form
#: that has one names it `material` (`_ingest_interview_fields`,
#: `_map_process_fields`), so this is one name and not a per-task rule.
_BULK_FIELD = "material"


def _input_snapshot(task_input: Mapping[str, Any]) -> dict[str, Any]:
    """What the record keeps of a launch's input: everything but the document.

    `material` is a transcript -- an interview, an SOP, whatever somebody
    pasted -- and this row is an operational trace, not a second evidence
    store. Where the text came from a registered source it is already in
    `kg.chunk`, which is INSERT-only and which the denial matrix keeps that
    way; copying it here would put the same evidence in two places with two
    different safety stories, only one of them audited. Where it was pasted,
    it was never evidence at all -- nothing anchored it and nothing can
    retrieve it.

    So the key is replaced rather than kept, and replaced by a COUNT rather
    than by a shortened version of itself. A truncated transcript looks like
    the transcript and reads like a different one, which is the worst of both
    -- `material_chars` cannot be mistaken for the text.
    """
    snapshot = {key: value for key, value in task_input.items() if key != _BULK_FIELD}
    material = task_input.get(_BULK_FIELD)
    if material is not None:
        snapshot[f"{_BULK_FIELD}_chars"] = len(str(material))
    return snapshot


async def _record_launch(
    cur: Any,
    *,
    actor: str,
    agent: str,
    task: str,
    engagement_id: str,
    task_input: dict[str, Any],
) -> int:
    """File the launch, in the caller's transaction. Returns its id.

    Written inside the transaction that just checked the caller is an active
    reviewer, which is what makes the `principal` column an attribution
    rather than a claim: the roster cannot change between the check and the
    row. db/018 grants no UPDATE on that column to anyone, so it stays one.
    """
    await cur.execute(
        """
        INSERT INTO wf.agent_launch (engagement_id, agent, task, principal, input)
        VALUES (%(eng)s::uuid, %(agent)s, %(task)s, %(who)s, %(input)s::jsonb)
        RETURNING launch_id
        """,
        {
            "eng": engagement_id,
            "agent": agent,
            "task": task,
            "who": actor,
            "input": Jsonb(_input_snapshot(task_input)),
        },
    )
    row = await fetchone(cur)
    if row is None:  # pragma: no cover -- RETURNING on a successful INSERT
        raise GateError(HTTPStatus.INTERNAL_SERVER_ERROR, "the launch record could not be written.")
    return int(row["launch_id"])


async def _mark_dispatched(launch_id: int, session_id: str) -> None:
    """queued -> running, stamping the session the trace will be under.

    Its own transaction, and that is the whole point of the two-step: the
    INSERT has to be COMMITTED before the dispatch starts, or the row is
    invisible for the entire minutes-long window it exists to cover.
    """
    async with db.tool_transaction(role=_gate_role()) as conn, conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE wf.agent_launch
               SET status = 'running', runtime_session_id = %(sid)s
             WHERE launch_id = %(lid)s
            """,
            {"sid": session_id, "lid": launch_id},
        )


async def _mark_finished(launch_id: int, *, status: str, error: dict[str, Any] | None) -> None:
    """Stamp how a launch ended. `error` is stored verbatim when there is one.

    Verbatim for `StepExecutionError.detail`'s own reason: docs/10 §2 tells
    an operator to read the error, and this row is now the only place a
    timed-out launch's error survives at all.
    """
    async with db.tool_transaction(role=_gate_role()) as conn, conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE wf.agent_launch
               SET status = %(status)s, error = %(err)s::jsonb, completed_at = now()
             WHERE launch_id = %(lid)s
            """,
            {
                "status": status,
                "err": None if error is None else Jsonb(error),
                "lid": launch_id,
            },
        )


async def list_launches(*, engagement_id: str | None = None, limit: int = 50) -> dict[str, Any]:
    """Launches, most recent first. Rendered as a section of `/ui/runs`.

    Fifty by default where `runs.list_runs` takes a hundred, and the
    difference is deliberate rather than left over: this is the SECOND list on
    a page whose subject is runs, and each launch row can carry a failure
    message several lines long. The operator reading it is chasing a launch
    they made minutes ago, which is at the top either way. The 500 ceiling is
    the same as the runs list's, for the same reason -- a page is not an
    export.

    No reviewer check, deliberately, and the asymmetry with `launch` is the
    point: WRITING a launch record is an act attributed to a person, so it is
    refused to anyone who is not an active reviewer; READING the list is the
    same class of fact as `/ui/runs` showing every run's `started_by`, which
    that page has never gated. Gating it would take the runs page away from
    everyone who is not on the roster in order to hide a task name from them.

    Runs as the gate role rather than `fde_prodops` because db/018 grants
    `wf.agent_launch` to the gate service alone -- prodops is the narrower of
    the console's two roles and widening it "because it is also the console"
    is what db/017 declined to do.
    """
    limit = max(1, min(limit, _MAX_LAUNCHES))
    async with db.tool_transaction(role=_gate_role()) as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT launch_id, engagement_id, agent, task, principal,
                   status, input, runtime_session_id, error,
                   requested_at, completed_at
              FROM wf.agent_launch
             WHERE (%(eng)s::uuid IS NULL OR engagement_id = %(eng)s::uuid)
             ORDER BY requested_at DESC, launch_id DESC
             LIMIT %(limit)s
            """,
            {"eng": engagement_id, "limit": limit},
        )
        launches = await fetchall(cur)

    return {
        "engagement_id": engagement_id,
        "returned": len(launches),
        "launches": launches,
    }


# ---------------------------------------------------------------------------
# The launch
# ---------------------------------------------------------------------------

_NO_RUNTIME = (
    "The {agent} agent is not deployed in this environment, so the console "
    "cannot launch it. An administrator deploys the agent runtimes and sets "
    "FDE_RUNTIME_ARN_{var} on this service to the runtime's ARN "
    "(docs/09-deployment.md §3 covers the deploy). Until then this task can "
    "still be run from a terminal on a machine that has the repository: "
    "fde-agents-local {agent} --task {task} --engagement-id {engagement_id}"
)


async def _material_from_source(cur: Any, source_id: int, engagement_id: str) -> str:
    """A registered source's text, reassembled from its stored chunks.

    The chunks ARE the document: intake split it and never kept the original
    (`kg.source.uri` is null for pasted text), so joining them back on blank
    lines is the closest thing to the transcript that exists. The agent reads
    it as `input.material`, exactly as a pasted one arrives.

    The join to `kg.source` is the access check, not decoration. `source_id`
    arrives from a form, and the picker it came from lists ONE engagement --
    so a number from a different engagement is either a stale page or a
    typed-in id, and reading its text would hand an operator a document from
    an engagement they were not looking at. No rows means the refusal below,
    which is the same sentence a genuinely empty source produces: both say
    "not something this engagement has", which is what the caller can act on.
    """
    await cur.execute(
        """
        SELECT c.content
          FROM kg.chunk c
          JOIN kg.source s ON s.source_id = c.source_id
         WHERE c.source_id = %(sid)s AND s.engagement_id = %(eng)s::uuid
         ORDER BY c.ordinal
        """,
        {"sid": source_id, "eng": engagement_id},
    )
    rows = await fetchall(cur)
    if not rows:
        raise GateError(
            HTTPStatus.BAD_REQUEST,
            f"source {source_id} has no stored passages on this engagement. "
            "Pick another source, or paste the text instead.",
        )
    return "\n\n".join(str(row["content"]) for row in rows)


async def _resolve_material(
    cur: Any, task_input: dict[str, Any], engagement_id: str
) -> dict[str, Any]:
    """Turn the ingest_interview form's two ways in into one `material`.

    `engagement/agent.py` reads `input.material` and nothing else, so both
    the picker and the paste box have to end there. Refusing when BOTH are
    filled is `sources._submitted_text`'s rule and it is here for the same
    reason: silently preferring one means the agent reads a document the
    operator did not think they had submitted.
    """
    raw_source = str(task_input.pop("source_id", "") or "").strip()
    material = str(task_input.get("material", "") or "").strip()

    if raw_source and material:
        raise GateError(
            HTTPStatus.BAD_REQUEST,
            "you picked a registered source AND pasted text. Use one or the "
            "other, so it is clear which document the agent reads.",
        )
    if raw_source:
        try:
            source_id = int(raw_source)
        except ValueError:
            raise GateError(
                HTTPStatus.BAD_REQUEST, f"{raw_source!r} is not a source number."
            ) from None
        task_input["material"] = await _material_from_source(cur, source_id, engagement_id)
        # Carried alongside the text the agent reads, not instead of it: the
        # payload is what the trace records, and "which document was this?"
        # is otherwise unanswerable from a transcript that got pasted twice.
        task_input["source_id"] = source_id
        return task_input
    if not material:
        raise GateError(
            HTTPStatus.BAD_REQUEST,
            "there is nothing for the agent to read. Pick a registered source, "
            "or paste the transcript into the box.",
        )
    task_input["material"] = material
    return task_input


async def launch(
    actor: str,
    *,
    agent: str,
    task: str,
    engagement_id: str,
    form: dict[str, str],
    executor: StepExecutor | None = None,
) -> dict[str, Any]:
    """Run one agent task and return what it streamed back.

    Synchronous, and that is still the weak part of this feature rather than
    a design anyone would choose: the wait is bounded here by
    `FDE_GATE_STEP_TIMEOUT_SECONDS`, but the deployed front door is an API
    Gateway HTTP API whose integrations time out well before that, so a long
    task returns an error to the browser while the Lambda -- and the agent --
    keep going.

    What changed is that this is now survivable rather than silent. The
    launch is recorded before it is dispatched (db/018), so the operator whose
    request died has a row to read: `running` with no `completed_at` means
    dispatched and nothing reported back, and a terminal row says which way it
    went and why. Making the launch itself asynchronous is a bigger change and
    a different one -- it would need a dispatcher and a poller and a second
    place the payload contract could drift -- and it is not what the harm was.
    The harm was that a timed-out launch left nothing behind.

    Where the record starts, and where it deliberately does not
    -----------------------------------------------------------
    The row is filed at the point where the request is COMPLETE and VALID and
    the only thing left is dispatch. Two refusals therefore land in front of
    it and record nothing:

      * Someone who may not launch anything. The row's `principal` is an
        attribution, and writing one for a caller the roster just refused
        would make the record assert something untrue -- and would let an
        anonymous request write a row.
      * A submission that does not parse: no material, both a source and
        pasted text, a source from another engagement, a missing required
        field. Nothing was requested that could have been dispatched; the
        operator is still filling the form in, and it re-renders with what
        they typed still in it. A launch log of typos answers no question.

    An unconfigured runtime is on the OTHER side of that line and does get a
    row, failed. The request was complete and valid; what was missing was the
    deployment. That is exactly the outcome somebody needs to find later, and
    it is the one a retry will keep reproducing.

    `executor` is injected the way `runner.advance`'s is, so the dispatch
    path is exercisable end to end against a fake with no AWS.

    Authorisation happens FIRST, before the submission is read at all. It
    used to run after `values_from_form`, so someone who may not launch
    anything was answered "Short name is required." -- an instruction to fix
    a form that was never going to be accepted, and one that says nothing
    about the only thing wrong. Same ordering `ui._reshow_new` settled on for
    the New source page: the refusal that is about the PERSON wins over the
    one that is about what they typed.
    """
    async with db.tool_transaction(role=_gate_role()) as conn, conn.cursor() as cur:
        await _assert_active_reviewer(cur, actor)

        _validate_choice(agent, task)
        task_input = values_from_form(task_fields(agent, task), form)
        if task == "ingest_interview":
            task_input = await _resolve_material(cur, task_input, engagement_id)

        # Last statement in the transaction that authorised: the record is
        # committed the moment this block exits, which is what makes it
        # visible for the whole dispatch that follows.
        launch_id = await _record_launch(
            cur,
            actor=actor,
            agent=agent,
            task=task,
            engagement_id=engagement_id,
            task_input=task_input,
        )

    settings = get_gate_settings().gate
    if not settings.runtime_arn_for(agent):
        # The same selection `AgentExecutor` makes, asked one step early so
        # the refusal can be a sentence rather than a step-failure envelope.
        # The executor keeps its own check for the workflow-step path.
        #
        # The record closes as failed and never leaves `runtime_session_id`
        # set, because nothing was ever sent: a null session id means there
        # is no trace to go looking for, which is the useful reading.
        unconfigured = _NO_RUNTIME.format(
            agent=agent,
            var=agent.upper(),
            task=task,
            engagement_id=engagement_id,
        )
        await _mark_finished(launch_id, status="failed", error={"error": unconfigured})
        raise GateError(HTTPStatus.SERVICE_UNAVAILABLE, unconfigured)

    session_id = new_runtime_session_id()
    await _mark_dispatched(launch_id, session_id)
    step: dict[str, Any] = {
        # Not a `wf.step` row -- there is no workflow here. The keys are the
        # ones `AgentExecutor` reads: `tool_name` picks the runtime,
        # `instruction` becomes the payload's `task`, `tool_args` its `input`.
        "step_key": f"console-launch:{task}",
        "kind": "agent",
        "tool_name": agent,
        "instruction": task,
        "tool_args": task_input,
    }
    run: dict[str, Any] = {
        "run_id": None,
        "engagement_id": engagement_id,
        "context": {},
        # Pre-minted so the executor does not try to stamp a wf.run row that
        # does not exist. See the module docstring.
        "runtime_session_id": session_id,
    }

    try:
        output = await (executor or AgentExecutor()).execute(step, run)
    except StepExecutionError as failure:
        detail = failure.detail
        log.info(
            "agent_launch_failed",
            actor=actor,
            agent=agent,
            task=task,
            launch_id=launch_id,
            detail=detail,
        )
        await _mark_finished(launch_id, status="failed", error=detail)
        raise GateError(HTTPStatus.BAD_GATEWAY, str(detail.get("error", detail))) from None
    except Exception as crash:
        # Anything the executor did not wrap -- a bug here, a botocore
        # exception that is not a step failure. The row must not be left
        # saying `running` when this process already knows it stopped:
        # `running` means "nobody reported back", and there is a difference
        # between not knowing and not saying. Re-raised untouched; the
        # console turns it into a 500 the operator can read.
        await _mark_finished(
            launch_id, status="failed", error={"error": f"{type(crash).__name__}: {crash}"}
        )
        raise

    events = output.get("events") or []
    await _mark_finished(launch_id, status="succeeded", error=None)
    log.info(
        "agent_launched",
        actor=actor,
        agent=agent,
        task=task,
        engagement_id=engagement_id,
        launch_id=launch_id,
        runtime_session_id=session_id,
        events=len(events),
    )
    return {
        "launch_id": launch_id,
        "agent": agent,
        "task": task,
        "engagement_id": engagement_id,
        "runtime_session_id": session_id,
        "events": len(events),
        "result": output.get("result"),
    }
