"""The Development Agent's AgentCore Runtime entrypoint.

Entrypoint contract
--------------------
POST /invocations body:

    {
      "task": "generate_agent_spec" | "generate_evals" | "generate_guardrails" | "scaffold_agent",
      "engagement_id": "<uuid>",
      "input": { "workflow_id": <int>, ... }
    }

Unlike the Engagement and Workflow agents, this agent never stages a
`hitl.proposal` and therefore never needs `fde_agents.common.hitl`'s gate
wait -- its entire tool surface (see `_READ_ONLY_TOOL_NAMES` below) is
read-only graph/workflow retrieval, by design (see `prompt.py`'s closing
section: "you do not write the graph, and you do not deploy anything").
Every task still gets a `trn.trace_session`/`trn.trace_step` trail like the
other two agents, because the generated artifacts (an agent spec, an eval
config) are themselves subject to human review and, eventually, to the same
kind of accepted/corrected labelling the training pipeline uses for
proposals -- see `db/009_training.sql`'s `trace_session.label_source`,
which explicitly allows `'operator_feedback'` as a label source for exactly
this kind of non-HITL-gated output.

`scaffold_agent` asks the model to emit a fenced ```json block whose top-
level shape is `{"files": {"<relative/path>": "<file contents>", ...}}` --
this entrypoint extracts that block and, only when `input.write_to_disk` is
true AND `FDE_SCAFFOLD_OUTPUT_DIR` is configured (see
`fde_agents.common.config.AgentProcessSettings.scaffold_output_dir`), writes
the files under that directory (never elsewhere, and never on any task
besides `scaffold_agent`). In every case the file map is also returned
directly in the `scaffold_result` event so a caller not running with local
disk access (the normal AgentCore Runtime deployment shape, whose container
filesystem is not a durable artifact store) can pull the generated files
out of the response itself.

Everything generic about the entrypoint contract lives in
`fde_agents.common.runtime` -- this module contributes the system prompt,
the four task names, the `workflow_id` precondition, the read-only tool
allowlist, and the scaffold-file extraction hook.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fde_agents.common.config import get_agent_runtime_settings
from fde_agents.common.runtime import AgentRuntimeConfig, StreamEvent, create_app
from fde_agents.development.prompt import DEVELOPMENT_AGENT_SYSTEM_PROMPT
from fde_mcp.logging import get_logger

log = get_logger(__name__)

VALID_TASKS = frozenset(
    {"generate_agent_spec", "generate_evals", "generate_guardrails", "scaffold_agent"}
)

# This agent's tool surface is read-only by design (see prompt.py's closing
# section) -- we filter the full MCP tool list down to this allowlist rather
# than trusting the deployed MCP server to never grow a write tool this
# agent shouldn't have. Belt-and-suspenders: the MCP server's own DB grants
# (db/010_roles_and_seed_policy.sql / 011_mcp_agent_supplemental_grants.sql)
# are the authoritative enforcement; this is a second, independent layer at
# the tool-selection boundary.
_READ_ONLY_TOOL_NAMES = frozenset(
    {
        "kg_head_commit",
        "kg_as_of",
        "kg_search",
        "kg_lexical_search",
        "kg_get_node",
        "kg_traverse",
        "kg_dependency_closure",
        "kg_impact_radius",
        "kg_process_flow",
        "wf_list",
        "wf_get",
    }
)

_FILES_BLOCK_RE = re.compile(r"```json\s*(\{.*?\"files\"\s*:.*?\})\s*```", re.DOTALL)


def _filter_read_only_tools(tools: list[Any]) -> list[Any]:
    filtered = []
    for tool in tools:
        name = getattr(tool, "tool_name", None) or getattr(tool, "name", None)
        if name in _READ_ONLY_TOOL_NAMES:
            filtered.append(tool)
        else:
            log.debug("development_tool_dropped", tool_name=name)
    return filtered


def _validate_input(task_input: dict[str, Any]) -> str | None:
    if not task_input.get("workflow_id"):
        return "input.workflow_id is required for every development-agent task"
    return None


def _build_task_prompt(
    task: str, engagement_id: str, session_id: str, task_input: dict[str, Any]
) -> str:
    header = f"SESSION CONTEXT: engagement_id={engagement_id!r}, session_id={session_id!r}.\n\n"
    workflow_id = task_input.get("workflow_id")
    if task == "generate_agent_spec":
        body = (
            f"TASK: generate_agent_spec\nworkflow_id: {workflow_id!r}\n\n"
            "Call wf_get(workflow_id). It must be status='published' -- if it is not, "
            "say so explicitly and stop; do not generate a spec off a draft/review "
            "workflow. For any step whose bound graph element's confidence/"
            "evidence_strength you need to check, call kg_get_node. Produce the full "
            "agent spec per your system prompt's GENERATE_AGENT_SPEC section: system "
            "prompt, tool allowlist, memory strategy (with justification), guardrails "
            "summary, and autonomy level (never exceeding the workflow's own)."
        )
    elif task == "generate_evals":
        body = (
            f"TASK: generate_evals\nworkflow_id: {workflow_id!r}\n\n"
            "Call wf_get(workflow_id). Produce an AgentCore Evaluations config per your "
            "system prompt's GENERATE_EVALS section, using only the real Builtin.* "
            "evaluator IDs at the correct level (SESSION/TRACE/TOOL_CALL), and justify "
            "each inclusion against a specific step of the workflow."
        )
    elif task == "generate_guardrails":
        body = (
            f"TASK: generate_guardrails\nworkflow_id: {workflow_id!r}\n\n"
            "Call wf_get(workflow_id) and, for each control-bound or low-confidence-"
            "bound step, kg_get_node on its bound key. Produce a guardrails config per "
            "your system prompt's GENERATE_GUARDRAILS section."
        )
    else:  # scaffold_agent
        body = (
            f"TASK: scaffold_agent\nworkflow_id: {workflow_id!r}\n\n"
            "Assume the agent spec, eval config, and guardrail config for this workflow "
            "have already been produced (regenerate them briefly here if not given "
            "explicitly in the input below) and produce a deployable agent package per "
            "your system prompt's GENERATE_SCAFFOLD_AGENT section. "
            "End your response with a single fenced ```json code block whose content is "
            'exactly {"files": {"<relative/path>": "<file contents as a string>", ...}} '
            "covering at minimum agent.py, prompt.py, requirements.txt, and Dockerfile. "
            f"\n\nProvided spec/config context (may be empty): {json.dumps(task_input.get('context', {}))}"
        )
    return header + body


def _extract_files_block(text: str) -> dict[str, str] | None:
    match = _FILES_BLOCK_RE.search(text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(1))
    except json.JSONDecodeError:
        log.warning("scaffold_files_block_unparseable")
        return None
    files = parsed.get("files")
    if not isinstance(files, dict):
        return None
    return {str(k): str(v) for k, v in files.items()}


def _maybe_write_files(files: dict[str, str]) -> str | None:
    output_dir = get_agent_runtime_settings().process.scaffold_output_dir
    if not output_dir:
        return None
    base = Path(output_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    for rel_path, content in files.items():
        # Refuse anything that would escape the output directory.
        dest = (base / rel_path).resolve()
        if base not in dest.parents and dest != base:
            log.warning("scaffold_write_refused_outside_output_dir", rel_path=rel_path)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
    return str(base)


def _after_final_text(
    task: str, task_input: dict[str, Any], final_text: str, task_failed: bool
) -> list[StreamEvent]:
    if task_failed or task != "scaffold_agent":
        return []
    files = _extract_files_block(final_text)
    if files is None:
        return [
            {
                "type": "warning",
                "message": (
                    "scaffold_agent produced no parseable ```json files block; "
                    "returning raw text only"
                ),
            }
        ]
    written_to = _maybe_write_files(files) if task_input.get("write_to_disk") else None
    return [{"type": "scaffold_result", "files": files, "written_to": written_to}]


_CONFIG = AgentRuntimeConfig(
    agent_key="development",
    system_prompt=DEVELOPMENT_AGENT_SYSTEM_PROMPT,
    valid_tasks=VALID_TASKS,
    build_prompt=_build_task_prompt,
    filter_tools=_filter_read_only_tools,
    validate_input=_validate_input,
    after_final_text=_after_final_text,
    include_proposal_ids_in_done=False,
)

app, handler = create_app(_CONFIG)


def main() -> None:
    app.run()


if __name__ == "__main__":
    main()
