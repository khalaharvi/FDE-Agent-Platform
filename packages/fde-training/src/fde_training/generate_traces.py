"""generate_traces.py -- rejection-sampling / STaR trace generation.

Given seed questions with a known gold node-key set (`trn.eval_query`-shaped
input), sample N trajectories from a teacher policy against the LIVE
environment (`rollout_env.RolloutEnv`, i.e. real Postgres, real
`kg.hybrid_search`/`kg.traverse`), and keep only the trajectories that:
  (a) reach the gold answer -- `fde_training.rewards.r_outcome == 1.0`
      (every gold key cited, nothing uncited beyond them: a strict F1=1.0
      bar, not "recall > 0"; STaR's whole premise is that the KEPT examples
      become SFT training data, and training on a partially-right
      trajectory teaches partial correctness),
  (b) every tool call is schema-valid --
      `fde_training.rewards.r_schema_valid == 1.0`,
  (c) is fully grounded -- `fde_training.rewards.r_grounded == 1.0` (every
      citation in the final answer actually appears in retrieved
      provenance).
All three gates use `fde_training.rewards`'s OWN reward functions, not a
reimplementation -- there must be exactly one definition of "grounded"/
"schema-valid" across SFT curation, RL reward, and trace acceptance, or the
three stages silently drift apart and a trajectory that would score 0 under
RL reward ends up in the SFT set anyway.

Survivors are written to `trn.trace_session`/`trn.trace_step` with
`outcome='accepted'` (via `rollout_env.RolloutEnv.finish`). Rejected
trajectories' trace rows are DELETED (cascades via
`trn.trace_step.session_id ON DELETE CASCADE`) rather than kept with
`outcome='rejected'` -- the point of rejection sampling is to curate a
clean accepted set; keeping every failed attempt would silently 10-100x the
trace store with data nobody trains on. Aggregate accept/reject counts are
still reported (`GenerationStats`) so the acceptance rate itself is visible.

Teacher policy
--------------
`TeacherFn` is the pluggable seam: given the question and the transcript so
far, return the next action. `bedrock_teacher_fn` is the real path (Bedrock
Converse with tool-use, guarded/lazy-imported, NOT exercised in this
environment). `heuristic_teacher_fn` is a small, genuinely imperfect
scripted policy used for testing this module's rejection-sampling LOGIC
end-to-end without any model access: it sometimes cites a real top-ranked
result (accepted), sometimes fabricates a citation (rejected by the
grounding gate), and sometimes calls `kg_search` with `k` above the allowed
ceiling (rejected by the schema-validity gate) -- so the test suite can
assert rejection sampling actually rejects things, not merely that it runs.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fde_mcp.logging import get_logger
from fde_training import common
from fde_training import rollout_env as renv
from fde_training.rewards import r_grounded, r_outcome, r_schema_valid

log = get_logger(__name__)


@dataclass
class SeedQuestion:
    engagement_id: str
    question: str
    gold_keys: list[str]
    task_kind: str = "map_workflow"
    query_id: int | None = None


TeacherAction = dict[str, Any]  # {"action": "tool_call", "tool_name":..., "arguments": {...}}
# | {"action": "answer", "text": ...}
TeacherFn = Callable[[str, list[dict[str, Any]]], TeacherAction]


def heuristic_teacher_fn(rng: random.Random) -> TeacherFn:
    """A deliberately imperfect scripted teacher for exercising the
    acceptance gates without a real model. On each call it decides between
    three behaviours so a batch of samples produces a realistic mix of
    accept/reject outcomes:
      - 'good': search, then answer citing the top real result -> should be
        accepted (modulo whether that result is actually in gold_keys).
      - 'hallucinate': search, then answer citing a fabricated key ->
        rejected by r_grounded.
      - 'bad_schema': issue a kg_search with k above the allowed ceiling ->
        rejected by r_schema_valid.
    """

    def teacher(question: str, transcript: list[dict[str, Any]]) -> TeacherAction:
        n_tool_calls = sum(
            1 for m in transcript if m.get("role") == "assistant" and m.get("tool_calls")
        )
        if n_tool_calls == 0:
            mode = rng.choice(["good", "good", "hallucinate", "bad_schema"])
            if mode == "bad_schema":
                return {
                    "action": "tool_call",
                    "tool_name": "kg_search",
                    "arguments": {"query": question, "k": 9999},
                }
            return {
                "action": "tool_call",
                "tool_name": "kg_search",
                "arguments": {"query": question, "k": 5},
            }

        # second turn: answer using whatever the last tool result contained
        last_tool = next((m for m in reversed(transcript) if m.get("role") == "tool"), None)
        results = []
        if last_tool and last_tool.get("content"):
            try:
                results = json.loads(last_tool["content"]).get("results", [])
            except (json.JSONDecodeError, AttributeError):
                results = []
        if not results:
            return {"action": "answer", "text": "No information found for this question."}
        top_key = results[0]["node_key"]
        if rng.random() < 0.25:
            return {"action": "answer", "text": "The answer involves [fabricated:not_a_real_key]."}
        return {"action": "answer", "text": f"The answer involves {top_key} [{top_key}]."}

    return teacher


def bedrock_teacher_fn(
    model_id: str = "anthropic.claude-sonnet-5", region: str | None = None
) -> TeacherFn:
    """Real path: Bedrock Converse with `toolConfig` describing the
    `kg_search`/`kg_traverse`/`kg_get_node` tool schemas, one turn per call.
    Lazy-imports boto3 so this module has zero AWS dependency until a
    caller actually requests this teacher. NOT exercised in this
    environment (sandbox credentials).
    """
    import boto3  # noqa: PLC0415

    client = boto3.client("bedrock-runtime", region_name=region)
    tool_config = {
        "tools": [
            {
                "toolSpec": {
                    "name": "kg_search",
                    "description": "Hybrid ANN + graph retrieval.",
                    "inputSchema": {
                        "json": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}, "k": {"type": "integer"}},
                            "required": ["query"],
                        }
                    },
                }
            },
        ]
    }

    def teacher(question: str, transcript: list[dict[str, Any]]) -> TeacherAction:
        messages = [{"role": "user", "content": [{"text": question}]}]
        for m in transcript:
            if m.get("role") == "tool":
                messages.append(
                    {"role": "user", "content": [{"text": f"Tool result: {m['content']}"}]}
                )
        resp = client.converse(modelId=model_id, messages=messages, toolConfig=tool_config)
        content = resp["output"]["message"]["content"]
        for block in content:
            if "toolUse" in block:
                tu = block["toolUse"]
                return {"action": "tool_call", "tool_name": tu["name"], "arguments": tu["input"]}
            if "text" in block:
                return {"action": "answer", "text": block["text"]}
        return {"action": "answer", "text": ""}

    return teacher


@dataclass
class GenerationStats:
    attempted: int = 0
    accepted: int = 0
    rejected_ungrounded: int = 0
    rejected_schema_invalid: int = 0
    rejected_wrong_answer: int = 0
    rejected_other: int = 0
    accepted_session_ids: list[str] = field(default_factory=list)


def sample_trajectory(
    env: renv.RolloutEnv, question: str, teacher_fn: TeacherFn, max_steps: int = 6
) -> tuple[list[dict[str, Any]], str]:
    """Run one teacher rollout against `env` (already reset). Returns
    (transcript_in_episodelog_shape, final_answer_text)."""
    transcript: list[dict[str, Any]] = []
    final_answer = ""
    for _ in range(max_steps):
        action = teacher_fn(question, transcript)
        if action["action"] == "answer":
            final_answer = action["text"]
            break
        tool_name, arguments = action["tool_name"], action["arguments"]
        try:
            env.step(tool_name, arguments)
        except (renv.ToolBudgetExceededError, renv.StaleCommitError, TypeError, ValueError) as exc:
            # A malformed/over-budget call still needs to show up in the
            # transcript so r_schema_valid can see and penalise it -- we do
            # NOT silently retry with corrected arguments (that would teach
            # the acceptance gate to launder a bad call into a good one).
            transcript.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call_{len(transcript)}",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": common.canonical_json(arguments),
                            },
                        }
                    ],
                }
            )
            transcript.append(
                {
                    "role": "tool",
                    "tool_call_id": f"call_{len(transcript) - 1}",
                    "name": tool_name,
                    "content": json.dumps({"error": str(exc)}),
                }
            )
            continue
        transcript = env.transcript()  # env already recorded it faithfully
    return transcript, final_answer


def accept_trajectory(
    transcript: list[dict[str, Any]], final_answer: str, gold_keys: list[str]
) -> tuple[bool, str]:
    """Runs the three STaR gates via `fde_training.rewards` itself (see
    module docstring for why). Returns (accepted, reason_if_rejected)."""
    full = [*transcript, {"role": "assistant", "content": final_answer}]
    schema_valid = r_schema_valid([full])[0]
    if schema_valid < 1.0:
        return False, "schema_invalid"
    grounded = r_grounded([full])[0]
    if grounded < 1.0:
        return False, "ungrounded"
    outcome = r_outcome([full], gold_keys=[gold_keys])[0]
    if outcome < 1.0:
        return False, "wrong_answer"
    return True, ""


def generate(
    seeds: list[SeedQuestion],
    n_samples_per_question: int,
    teacher_fn: TeacherFn,
    embed_fn: Callable[[str], list[float]],
    *,
    tool_budget: int = 6,
    agent_name: str = "engagement",
    model_id: str = "star-teacher",
    policy_version: str = "star-v0",
) -> GenerationStats:
    stats = GenerationStats()
    for seed in seeds:
        for _ in range(n_samples_per_question):
            stats.attempted += 1
            env = renv.RolloutEnv(
                engagement_id=seed.engagement_id,
                embed_fn=embed_fn,
                tool_budget=tool_budget,
                agent_name=agent_name,
                model_id=model_id,
                policy_version=policy_version,
            )
            env.reset(
                task_kind=seed.task_kind,
                task_input={"question": seed.question, "gold_keys": seed.gold_keys},
            )
            try:
                transcript, final_answer = sample_trajectory(env, seed.question, teacher_fn)
            except Exception:
                log.exception("teacher_rollout_crashed", question=seed.question)
                _discard_episode(env)
                stats.rejected_other += 1
                continue

            accepted, reason = accept_trajectory(transcript, final_answer, seed.gold_keys)
            if accepted:
                env.finish(final_answer=final_answer, outcome="accepted")
                stats.accepted += 1
                assert env.state is not None
                stats.accepted_session_ids.append(env.state.session_id)
            else:
                _discard_episode(env)
                if reason == "ungrounded":
                    stats.rejected_ungrounded += 1
                elif reason == "schema_invalid":
                    stats.rejected_schema_invalid += 1
                elif reason == "wrong_answer":
                    stats.rejected_wrong_answer += 1
                else:
                    stats.rejected_other += 1
    return stats


def _discard_episode(env: renv.RolloutEnv) -> None:
    """Deletes a rejected episode's trace rows -- see module docstring for
    why rejected samples are not persisted with outcome='rejected'. Uses
    `env.role` (the role that WROTE these rows, see
    `rollout_env.ENSURE_ROLE_SQL`/`db/012_rl_rollout_role.sql`), not the
    default `fde_training` role, which is deliberately read-mostly and has
    no DELETE grant on trn.trace_session -- generate_traces.py is a
    rollout-and-curate tool, not a pure analytics reader.
    """
    if env.state is None:
        return
    with common.connect(role=env.role) as db:
        cur = db.cursor()
        cur.execute(
            "DELETE FROM trn.trace_session WHERE session_id = %(sid)s",
            {"sid": env.state.session_id},
        )


def load_seeds_from_eval_query(engagement_id: str | None = None) -> list[SeedQuestion]:
    with common.connect() as db:
        cur = db.cursor()
        if engagement_id:
            cur.execute(
                "SELECT * FROM trn.eval_query WHERE engagement_id = %(eng)s::uuid",
                {"eng": engagement_id},
            )
        else:
            cur.execute("SELECT * FROM trn.eval_query")
        rows = cur.fetchall()
    return [
        SeedQuestion(
            engagement_id=str(r["engagement_id"]),
            question=r["question"],
            gold_keys=list(r["relevant_keys"] or []),
            query_id=r["query_id"],
        )
        for r in rows
    ]
