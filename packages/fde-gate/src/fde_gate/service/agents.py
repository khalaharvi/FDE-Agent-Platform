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

Nothing here writes
--------------------
A launch reads material and dispatches; the agent's own MCP session is what
proposes, under `fde_agent`, through the grants db/010 and db/011 already
draw. This module needs no new privilege, and the invariant is untouched: a
console launch cannot write the graph any more than a terminal one can.
"""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

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

    Synchronous by design. The alternative -- returning immediately and
    letting the operator find the outcome later -- needs somewhere to record
    the launch, and there is no table for one; inventing a row the gate
    service could write would mean a new grant, which is the one thing this
    feature is not allowed to add on its own. So the request waits, bounded
    by `FDE_GATE_STEP_TIMEOUT_SECONDS`, and what comes back is reported.

    `executor` is injected the way `runner.advance`'s is, so the dispatch
    path is exercisable end to end against a fake with no AWS.
    """
    _validate_choice(agent, task)

    fields = task_fields(agent, task)
    task_input = values_from_form(fields, form)

    async with db.tool_transaction(role=_gate_role()) as conn, conn.cursor() as cur:
        await _assert_active_reviewer(cur, actor)
        if task == "ingest_interview":
            task_input = await _resolve_material(cur, task_input, engagement_id)

    settings = get_gate_settings().gate
    if not settings.runtime_arn_for(agent):
        # The same selection `AgentExecutor` makes, asked one step early so
        # the refusal can be a sentence rather than a step-failure envelope.
        # The executor keeps its own check for the workflow-step path.
        raise GateError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            _NO_RUNTIME.format(
                agent=agent,
                var=agent.upper(),
                task=task,
                engagement_id=engagement_id,
            ),
        )

    session_id = new_runtime_session_id()
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
        log.info("agent_launch_failed", actor=actor, agent=agent, task=task, detail=detail)
        raise GateError(HTTPStatus.BAD_GATEWAY, str(detail.get("error", detail))) from None

    events = output.get("events") or []
    log.info(
        "agent_launched",
        actor=actor,
        agent=agent,
        task=task,
        engagement_id=engagement_id,
        runtime_session_id=session_id,
        events=len(events),
    )
    return {
        "agent": agent,
        "task": task,
        "engagement_id": engagement_id,
        "runtime_session_id": session_id,
        "events": len(events),
        "result": output.get("result"),
    }
