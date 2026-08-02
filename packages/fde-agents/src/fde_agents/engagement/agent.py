"""The Engagement Agent's AgentCore Runtime entrypoint.

Entrypoint contract
--------------------
POST /invocations body:

    {
      "task": "map_process" | "score_opportunities" | "detect_bottlenecks" | "ingest_interview",
      "engagement_id": "<uuid>",
      "input": { ... task-specific ... }
    }

The handler is an async generator, so AgentCore streams the response as
SSE (`data: {json}\\n\\n` frames) rather than buffering the whole task to a
single JSON blob -- a `map_process` or `ingest_interview` call can run for
minutes of tool-calling, and a human FDE watching the session should see
progress as it happens, not a spinner.

Everything generic about that contract (the SSE loop, HITL gate waiting,
outcome computation, ping status, error envelopes) lives in
`fde_agents.common.runtime` -- this module contributes exactly what makes
the Engagement Agent the Engagement Agent: its system prompt, its four task
names, and how each task maps to a concrete prompt.

Trace-session note: `kg_propose`'s `trace_session_id` argument (see
`fde_mcp.server`) is the sanctioned, per-call way to associate a proposal
with this session's trace -- unlike the MCP server's OWN tool-call tracing
(`FDE_TRACE_SESSION_ID` env var, process-scoped), which only works
end-to-end in the local stdio dev deployment shape (see
`fde_agents.common.mcp_tools`'s module docstring), which is why this
module's task prompts explicitly tell the model to pass
`trace_session_id` on every `kg_propose` call it makes.
"""

from __future__ import annotations

from typing import Any

from fde_agents.common.runtime import AgentRuntimeConfig, create_app
from fde_agents.engagement.prompt import ENGAGEMENT_AGENT_SYSTEM_PROMPT

VALID_TASKS = frozenset(
    {"map_process", "score_opportunities", "detect_bottlenecks", "ingest_interview"}
)

# Tasks whose typical output is a kg_propose/kg_submit_proposal pair, where
# it makes sense for THIS invocation to wait (briefly) for the resulting
# gate rather than returning immediately after submission.
_TASKS_THAT_AWAIT_GATES = frozenset({"map_process", "ingest_interview", "detect_bottlenecks"})


def _build_task_prompt(
    task: str, engagement_id: str, session_id: str, task_input: dict[str, Any]
) -> str:
    header = (
        f"SESSION CONTEXT: engagement_id={engagement_id!r}, session_id={session_id!r}. "
        f"Whenever you call kg_propose, pass trace_session_id={session_id!r} explicitly so "
        "this proposal is linked back to this trace for training/audit purposes.\n\n"
    )
    if task == "map_process":
        body = (
            f"TASK: map_process\n"
            f"Target process / scope: {task_input.get('process_key') or task_input.get('description')!r}\n"
            f"Supporting material (interview notes, SOP excerpts, ticket exports, etc.):\n"
            f"{task_input.get('material', '(none provided -- rely on existing graph + ask for what is missing)')}\n\n"
            "Call kg_head_commit first. Then map this process faithfully: identify the "
            "activities, roles, systems, and control-flow edges involved, checking "
            "kg_search/kg_lexical_search for anything that may already exist before "
            "proposing a duplicate. Build one kg_propose with every item cited to a "
            "source. State which gates you expect BEFORE calling kg_submit_proposal, "
            "then submit."
        )
    elif task == "score_opportunities":
        body = (
            f"TASK: score_opportunities\n"
            f"Scope: {task_input.get('process_key') or task_input.get('activity_keys')!r}\n\n"
            "Call kg_head_commit first, then kg_process_flow (if scoped to a process) or "
            "kg_get_node (for each explicit activity_key) to retrieve the activities in "
            "scope. Score every one on the six-dimension rubric from your system prompt, "
            "citing the specific retrieval evidence for each dimension's score. Produce a "
            "kg_propose with one add_node(node_type='opportunity') item per scored "
            "activity plus its addresses edge(s) to any relevant pain_point, then state "
            "expected gates and submit."
        )
    elif task == "detect_bottlenecks":
        body = (
            f"TASK: detect_bottlenecks\n"
            f"Scope: {task_input.get('process_key')!r}\n\n"
            "Call kg_head_commit, then kg_process_flow for the process, then drift_list "
            "(min_severity='medium' unless told otherwise) to see what the Workflow "
            "Agent's monitoring has already flagged for this engagement. Cross-reference: "
            "a drift signal whose subject_ref falls inside this process's activity set is "
            "strong evidence for a pain_point. Propose pain_point nodes with blocks edges "
            "into the affected activities, citing the drift signal(s) and/or kg_traverse "
            "results that support each one, then state expected gates and submit."
        )
    else:  # ingest_interview
        body = (
            f"TASK: ingest_interview\n"
            f"Transcript / document:\n{task_input.get('material', '')}\n\n"
            "Call kg_head_commit first. Extract candidate graph facts from the material "
            "above. For each candidate, check kg_search/kg_lexical_search to see whether "
            "it already exists (populate supersedes_key if you are updating something "
            "live) before adding it fresh. Translate any named individual into the ROLE "
            "they occupy -- never propose a person's name. Build one kg_propose citing "
            "the specific passage of the transcript that backs each item (put the excerpt "
            "in the item's rationale/payload notes), state expected gates, then submit."
        )
    return header + body


_CONFIG = AgentRuntimeConfig(
    agent_key="engagement",
    system_prompt=ENGAGEMENT_AGENT_SYSTEM_PROMPT,
    valid_tasks=VALID_TASKS,
    build_prompt=_build_task_prompt,
    awaits_gate=lambda task, _task_input: task in _TASKS_THAT_AWAIT_GATES,
)

app, handler = create_app(_CONFIG)


def main() -> None:
    app.run()


if __name__ == "__main__":
    main()
