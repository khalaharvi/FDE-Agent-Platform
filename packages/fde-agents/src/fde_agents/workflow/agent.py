"""The Workflow Agent's AgentCore Runtime entrypoint.

Entrypoint contract
--------------------
POST /invocations body:

    {
      "task": "author_workflow" | "monitor_drift" | "triage_drift" | "reauthor_stale",
      "engagement_id": "<uuid>",
      "input": { ... task-specific ... }
    }

`monitor_drift` is the autonomous/scheduled path (invoked on a cron rather
than by an interactive FDE session): it runs `drift_scan`, triages every
signal the deterministic detectors surfaced, and drafts `kg_propose` calls
where warranted -- but ALWAYS stops at proposal, per `db/007_drift.sql`'s
module docstring ("the agent never silently edits a published workflow").
Unlike the Engagement Agent's entrypoint, this task does NOT block waiting
on any resulting HITL gate by default (`input.await_gates` opts in) -- a
scheduled monitor run may raise several proposals across many signals in
one pass, and camping on each one serially would turn a batch job into a
multi-hour invocation for no benefit (nothing in the monitor's own job
depends on the gate resolving; the human review queue is where that
happens next, independently of this process).

Everything generic about the entrypoint contract lives in
`fde_agents.common.runtime` -- this module contributes the system prompt,
the four task names, the prompt-per-task mapping, the `await_gates` opt-in
rule, and one Workflow-specific hook: a post-hoc faithfulness check against
a `wf_draft` call's actual arguments (see `_on_event` below).
"""

from __future__ import annotations

from typing import Any

from fde_agents.common import guardrails
from fde_agents.common.runtime import AgentRuntimeConfig, StreamEvent, create_app
from fde_agents.workflow.prompt import WORKFLOW_AGENT_SYSTEM_PROMPT

VALID_TASKS = frozenset({"author_workflow", "monitor_drift", "triage_drift", "reauthor_stale"})


def _build_task_prompt(
    task: str, engagement_id: str, session_id: str, task_input: dict[str, Any]
) -> str:
    header = (
        f"SESSION CONTEXT: engagement_id={engagement_id!r}, session_id={session_id!r}. "
        f"Whenever you call kg_propose, pass trace_session_id={session_id!r} explicitly.\n\n"
    )
    if task == "author_workflow":
        body = (
            f"TASK: author_workflow\n"
            f"root_process_key: {task_input.get('root_process_key')!r}\n"
            f"target slug: {task_input.get('slug')!r}\n"
            f"title: {task_input.get('title')!r}\n\n"
            "Call kg_head_commit first and use its commit_id as pinned_commit_id. Call "
            "kg_process_flow for the root_process_key to get the ordered activity "
            "sequence, then kg_get_node on each activity as needed to determine "
            "performed_by/gated_by/records_to detail. Build the step list per your "
            "system prompt's routing rules (derive order from next_keys, decision steps "
            "from gated_by, step boundaries from hands_off_to), attach a binding to every "
            "non-notify step, and call wf_draft. Report the resulting workflow_id, "
            "version, and the autonomy_level you assessed."
        )
    elif task == "monitor_drift":
        min_sev = task_input.get("min_severity", "medium")
        body = (
            f"TASK: monitor_drift\n"
            f"min_severity floor for triage: {min_sev!r}\n\n"
            "Call drift_scan first to run the deterministic detectors. Then call "
            f"drift_list(state='open', min_severity={min_sev!r}) to see what's new. "
            "Apply your system prompt's triage rules to EVERY signal returned: judge "
            "whether the sample size clears the minimum for its drift_kind, decide "
            "dismissed/accepted/triaged, call drift_triage to record your judgment on "
            "each one, and where warranted call kg_propose (citing the signal_id and its "
            "detail numbers in the rationale) followed by kg_submit_proposal. You ALWAYS "
            "stop at proposal -- never attempt to merge or to edit a published workflow "
            "directly. Finish with a summary: how many signals were dismissed / accepted "
            "/ resulted in a proposal, and the proposal_ids you raised."
        )
    elif task == "triage_drift":
        signal_ids = task_input.get("signal_ids") or []
        body = (
            f"TASK: triage_drift\n"
            f"signal_ids: {signal_ids!r}\n\n"
            "Call drift_list to find these specific signals (filter to the given "
            "signal_ids in your own reasoning; the tool does not take a signal_id filter, "
            "so read detail/signal_id from the full list). Apply the same triage rules as "
            "monitor_drift to just these signals, call drift_triage for each, and where "
            "warranted kg_propose + kg_submit_proposal. Report your judgment and "
            "rationale for each requested signal_id explicitly, even the ones you dismiss."
        )
    else:  # reauthor_stale
        workflow_id = task_input.get("workflow_id")
        body = (
            f"TASK: reauthor_stale\n"
            f"workflow_id: {workflow_id!r}\n\n"
            "Call wf_get(workflow_id) to see the current pin and commits_behind. If "
            "commits_behind is 0, report that no re-authoring is needed and stop. "
            "Otherwise call drift_list to check for stale_pin signals referencing this "
            "workflow, then use kg_get_node/kg_traverse AT THE CURRENT HEAD COMMIT on "
            "every subject_key this workflow's steps are bound to, to determine whether "
            "anything it actually depends on changed in a way that matters (not just "
            "'some unrelated part of the graph moved'). If something material changed, "
            "author a new draft version at the current head via wf_draft using the SAME "
            "slug (versioning is automatic) reflecting the update, and report exactly "
            "which bound elements changed and how. If nothing material changed despite "
            "commits_behind > 0, say so explicitly and recommend leaving the published "
            "version as-is."
        )
    return header + body


def _on_event(event: StreamEvent) -> list[StreamEvent]:
    """Post-hoc faithfulness audit: the database is the real enforcement
    (`wf.assert_faithful` rolls back the whole draft on any unbound step --
    see `db/006_workflows.sql`), but surfacing the same finding here,
    against exactly the arguments the model sent, lets a human watching the
    stream see WHY a `wf_draft` call failed without digging through a
    raised database exception.
    """
    if event.get("type") == "tool_result" and event.get("tool_name") == "wf_draft":
        steps = (event.get("tool_input") or {}).get("steps") or []
        violations = guardrails.check_workflow(steps)
        if violations:
            return [{"type": "guardrail_violation", "violations": [v.__dict__ for v in violations]}]
    return []


_CONFIG = AgentRuntimeConfig(
    agent_key="workflow",
    system_prompt=WORKFLOW_AGENT_SYSTEM_PROMPT,
    valid_tasks=VALID_TASKS,
    build_prompt=_build_task_prompt,
    awaits_gate=lambda _task, task_input: bool(task_input.get("await_gates", False)),
    on_event=_on_event,
)

app, handler = create_app(_CONFIG)


def main() -> None:
    app.run()


if __name__ == "__main__":
    main()
