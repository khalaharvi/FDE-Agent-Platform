"""rewards/_episode.py -- the one place completions get parsed.

Every reward function in this package builds `EpisodeLog` from its
`completions` argument rather than re-parsing message shapes itself. That
is the whole point of this module existing separately from the reward
functions: if `EpisodeLog.from_completion` has a bug (a tool result
attached to the wrong call, a malformed tool call silently dropped instead
of counted), every reward function inherits the fix at once instead of six
subtly-different parsers drifting apart one at a time.

TRL / verl compatibility
-------------------------
Every `r_*` function across this package has the TRL reward-function
signature::

    def r_xxx(completions: list[Any], **kwargs) -> list[float]

`completions` is, per element, EITHER:
  - a list of OpenAI-shape message dicts for that rollout's assistant/tool
    turns (what TRL's multi-turn tool rollout / `environment_factory` and
    verl's multi-turn `BaseTool` rollout both produce), OR
  - a dict `{"messages": [...], ...}` (`export_sft.py`'s record shape, or
    whatever the harness wraps the trajectory in) -- unwrapped automatically
    by `EpisodeLog.from_completion`.

`**kwargs` carries whatever extra per-example dataset columns the trainer
forwards (TRL forwards every other dataset column verbatim) -- reward
modules read `gold_keys`, `pooled_keys`, `relevant_keys`, `hops_required`
from kwargs when present via `_kwarg_per_example`, and degrade gracefully
(documented per-function) when they are absent, which is what makes every
function unit-testable with a bare hand-constructed `completions` list and
no trainer at all. GRPO's `environments` kwarg (stateful env instances,
e.g. `rollout_env.RolloutEnv`) is read OPTIONALLY, via `_episode_logs`, for
terms that need ground truth the transcript itself does not carry (exact DB
latency, token accounting) -- see `efficiency.r_cost`.

No heavy imports. This module imports only stdlib -- it must be importable
inside a GRPOTrainer process AND inside a bare `pytest` run with none of
torch/trl/peft installed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# ===========================================================================
# CITATION_RE -- how every reward term (and lambda_grader.py's independent
# Lambda-side scorer) finds an entity citation in free text: a bracketed
# node_key, e.g. "[ctrl:discount_threshold]". One regex, shared by grounding
# and outcome scoring, so "what counts as a citation" cannot silently drift
# between the term that checks citations are TRUE (r_grounded) and the term
# that checks citations are PRESENT (r_citation) and the term that scores
# task success against them (r_outcome).
# ===========================================================================
CITATION_RE = re.compile(r"\[([a-zA-Z0-9_:.\-]+)\]")

# Node keys/results are found under different field names per tool; this is
# the exhaustive list of (tool_name -> jsonpath-ish accessor) used by
# `EpisodeLog._extract_retrieved_keys`... via `EpisodeLog.retrieved_keys`.
_RESULT_LIST_FIELDS = {
    "kg_search": ("results", "node_key"),
    "kg_lexical_search": ("results", "node_key"),
    "kg_traverse": ("nodes", "node_key"),
    "kg_dependency_closure": ("closure", "node_key"),
    "kg_impact_radius": ("impact", "node_key"),
}


@dataclass
class ToolCallRecord:
    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None = None
    retrieval: dict[str, Any] | None = None  # from trace_step.retrieval / rollout_env
    latency_ms: float | None = None
    raw_arguments_str: str | None = None
    parse_ok: bool = True


@dataclass
class EpisodeLog:
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    final_answer: str | None = None
    malformed_tool_call_count: int = 0
    env: Any | None = None  # optional rollout_env.RolloutEnv, if provided via kwargs

    @property
    def tool_call_count(self) -> int:
        return len(self.tool_calls)

    def retrieved_keys(self) -> set[str]:
        keys: set[str] = set()
        for tc in self.tool_calls:
            if not tc.result:
                continue
            spec = _RESULT_LIST_FIELDS.get(tc.tool_name)
            if spec is None:
                # kg_get_node returns a single `node` object, not a list.
                node = tc.result.get("node") if isinstance(tc.result, dict) else None
                if isinstance(node, dict) and node.get("node_key"):
                    keys.add(node["node_key"])
                continue
            list_field, key_field = spec
            for item in tc.result.get(list_field) or []:
                if isinstance(item, dict) and item.get(key_field):
                    keys.add(item[key_field])
        return keys

    def max_hops_requested(self) -> int:
        hops = [0]
        for tc in self.tool_calls:
            h = tc.arguments.get("max_hops") or tc.arguments.get("expand_hops")
            if isinstance(h, int):
                hops.append(h)
            if tc.retrieval and isinstance(tc.retrieval.get("hops"), int):
                hops.append(tc.retrieval["hops"])
        return max(hops)

    def total_tokens_estimate(self) -> int:
        if self.env is not None and hasattr(self.env, "total_tokens"):
            return int(self.env.total_tokens)
        total = 0
        for tc in self.tool_calls:
            total += len(tc.raw_arguments_str or "") // 4
            if tc.result:
                total += len(json.dumps(tc.result)) // 4
        if self.final_answer:
            total += len(self.final_answer) // 4
        return total

    def total_db_ms_estimate(self) -> float:
        if self.env is not None and hasattr(self.env, "total_db_ms"):
            return float(self.env.total_db_ms)
        return sum((tc.latency_ms or 0.0) for tc in self.tool_calls)

    @classmethod
    def from_completion(cls, completion: Any, env: Any | None = None) -> EpisodeLog:
        messages = _unwrap_messages(completion)
        log = cls(env=env)
        pending_calls: dict[str, ToolCallRecord] = {}
        for msg in messages:
            role = msg.get("role")
            if role == "assistant":
                if msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        rec = _parse_tool_call(tc)
                        if rec.parse_ok:
                            log.tool_calls.append(rec)
                            call_id = (tc.get("id") if isinstance(tc, dict) else None) or ""
                            pending_calls[call_id] = rec
                        else:
                            log.malformed_tool_call_count += 1
                if msg.get("content"):
                    log.final_answer = msg["content"]
            elif role == "tool":
                call_id = msg.get("tool_call_id") or ""
                rec = pending_calls.get(call_id)
                content = msg.get("content")
                parsed_result = _try_parse_json(content) if isinstance(content, str) else content
                if rec is not None:
                    rec.result = (
                        parsed_result if isinstance(parsed_result, dict) else {"_raw": content}
                    )
                elif log.tool_calls:
                    # tool_call_id didn't match anything we saw (shouldn't
                    # happen with well-formed transcripts) -- attach to the
                    # most recent unresolved call as a best-effort fallback
                    # rather than silently dropping retrieval evidence.
                    last = log.tool_calls[-1]
                    if last.result is None:
                        last.result = (
                            parsed_result if isinstance(parsed_result, dict) else {"_raw": content}
                        )
        return log


def _unwrap_messages(completion: Any) -> list[dict[str, Any]]:
    if isinstance(completion, dict) and "messages" in completion:
        return completion["messages"]  # type: ignore[no-any-return]
    if isinstance(completion, list):
        return completion
    if isinstance(completion, str):
        return [{"role": "assistant", "content": completion}]
    msg = f"unsupported completion shape: {type(completion)!r}"
    raise TypeError(msg)


def _try_parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _parse_tool_call(tc: Any) -> ToolCallRecord:
    if not isinstance(tc, dict):
        return ToolCallRecord(tool_name="", arguments={}, parse_ok=False)
    fn = tc.get("function")
    if not isinstance(fn, dict):
        return ToolCallRecord(tool_name="", arguments={}, parse_ok=False)
    name = fn.get("name")
    raw_args = fn.get("arguments")
    if not isinstance(name, str) or not name:
        return ToolCallRecord(
            tool_name=str(name), arguments={}, raw_arguments_str=raw_args, parse_ok=False
        )
    args: Any
    if isinstance(raw_args, str):
        args = _try_parse_json(raw_args)
        if not isinstance(args, dict):
            return ToolCallRecord(
                tool_name=name, arguments={}, raw_arguments_str=raw_args, parse_ok=False
            )
    elif isinstance(raw_args, dict):
        args = raw_args
    else:
        return ToolCallRecord(
            tool_name=name, arguments={}, raw_arguments_str=str(raw_args), parse_ok=False
        )
    return ToolCallRecord(
        tool_name=name, arguments=args, raw_arguments_str=json.dumps(args), parse_ok=True
    )


def _kwarg_per_example(kwargs: dict[str, Any], key: str, i: int, default: Any) -> Any:
    val = kwargs.get(key)
    if val is None:
        return default
    if isinstance(val, (list, tuple)):
        return val[i] if i < len(val) else default
    return val  # a single scalar shared across the whole batch


def episode_logs(completions: list[Any], kwargs: dict[str, Any]) -> list[EpisodeLog]:
    envs = kwargs.get("environments")
    logs = []
    for i, c in enumerate(completions):
        env = envs[i] if envs is not None and i < len(envs) else None
        logs.append(EpisodeLog.from_completion(c, env=env))
    return logs
