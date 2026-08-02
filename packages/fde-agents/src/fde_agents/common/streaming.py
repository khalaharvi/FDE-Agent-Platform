"""Shared Strands agent-turn streaming/tracing loop.

The Engagement, Workflow, and Development agents all do the same thing once
they have an `Agent` and a prompt: stream `agent.stream_async(...)`, forward
presentation-worthy events out over the AgentCore SSE response, record every
system/user/assistant turn into `trn.trace_step` via `tracing.record_turn`
(tool turns are already recorded by the MCP server itself -- see
`fde_mcp.tools._base.emit_trace` -- so this loop does NOT double-record
those; it only watches tool results to accumulate provenance/proposal IDs
for the caller), and hand back one final summary event. Factoring this once
here, instead of three times, is what keeps the trainable/non-trainable turn
accounting (see `tracing.py`'s module docstring) and the provenance
bookkeeping `guardrails.citation_required` needs identical across all three
runtimes -- divergence here is exactly the kind of bug that would silently
corrupt the SFT export (`trn.sft_export`) for one agent but not the others.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fde_mcp.logging import get_logger

from . import tracing
from .tracing import _extract_tool_json

log = get_logger(__name__)


def _walk_for_key(obj: Any, key: str, out: list[Any]) -> None:
    """Recursively collect every value found under `key` anywhere in a
    nested dict/list structure. Used to pull `provenance` blocks and
    `proposal_id`s out of arbitrarily-shaped tool_result JSON without each
    call site needing to know the exact response shape of every kg_* tool.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                out.append(v)
            _walk_for_key(v, key, out)
    elif isinstance(obj, list):
        for item in obj:
            _walk_for_key(item, key, out)


async def run_agent_turn(
    mcp_client: Any,
    session: tracing.TraceSession,
    agent: Any,
    prompt: str,
) -> AsyncIterator[dict[str, Any]]:
    """Stream one `agent.stream_async(prompt)` call.

    Yields presentation events of the following `type`s:
      - `"delta"`        -- `{"text": ...}` a chunk of assistant text
      - `"tool_call"`    -- `{"tool_name": ..., "tool_use_id": ...}`
      - `"tool_result"`  -- `{"tool_name": ..., "tool_use_id": ..., "status": ...}`
      - `"turn_complete"`-- ALWAYS the last event yielded, carrying
        `final_text`, `provenance` (accumulated `provenance` blocks from
        every tool result seen this turn), and `proposal_ids` (every
        `proposal_id` found in any tool result this turn, in first-seen
        order, deduplicated).

    Records the user turn before streaming starts and the assistant turn
    after streaming completes -- both via `tracing.record_turn`, which is
    the only place `trainable` is decided (see that module's docstring).
    """
    await tracing.record_turn(mcp_client, session, role="user", content=prompt)

    tool_use_names: dict[str, str] = {}
    tool_use_inputs: dict[str, dict[str, Any]] = {}
    text_parts: list[str] = []
    provenance: list[dict[str, Any]] = []
    proposal_ids: list[int] = []
    seen_proposal_ids: set[int] = set()

    async for event in agent.stream_async(prompt):
        current_tool_use = event.get("current_tool_use")
        if current_tool_use and current_tool_use.get("toolUseId") and current_tool_use.get("name"):
            tool_use_id = current_tool_use["toolUseId"]
            if tool_use_id not in tool_use_names:
                yield {
                    "type": "tool_call",
                    "tool_name": current_tool_use["name"],
                    "tool_use_id": tool_use_id,
                }
            tool_use_names[tool_use_id] = current_tool_use["name"]
            # `input` accumulates across successive ToolUseStreamEvents for
            # the same toolUseId as the model streams its JSON arguments; the
            # last one seen before the matching ToolResultEvent is complete.
            if isinstance(current_tool_use.get("input"), dict):
                tool_use_inputs[tool_use_id] = current_tool_use["input"]

        text = event.get("data")
        if text:
            text_parts.append(text)
            yield {"type": "delta", "text": text}

        tool_result = event.get("tool_result")
        if tool_result is not None:
            tool_use_id = tool_result.get("toolUseId")
            tool_name = tool_use_names.get(tool_use_id, "unknown_tool")
            payload = _extract_tool_json(tool_result) or {}

            found_provenance: list[Any] = []
            _walk_for_key(payload, "provenance", found_provenance)
            for p in found_provenance:
                if isinstance(p, dict):
                    provenance.append(p)

            found_ids: list[Any] = []
            _walk_for_key(payload, "proposal_id", found_ids)
            for pid in found_ids:
                try:
                    pid_int = int(pid)
                except (TypeError, ValueError):
                    continue
                if pid_int not in seen_proposal_ids:
                    seen_proposal_ids.add(pid_int)
                    proposal_ids.append(pid_int)

            yield {
                "type": "tool_result",
                "tool_name": tool_name,
                "tool_use_id": tool_use_id,
                "status": tool_result.get("status"),
                "tool_input": tool_use_inputs.get(tool_use_id, {}),
            }

    final_text = "".join(text_parts)
    await tracing.record_turn(mcp_client, session, role="assistant", content=final_text)

    yield {
        "type": "turn_complete",
        "final_text": final_text,
        "provenance": provenance,
        "proposal_ids": proposal_ids,
    }
