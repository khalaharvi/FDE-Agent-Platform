"""export_sft.py -- turns `trn.trace_step` trajectories into TRL-ready
JSONL, plus a free DPO/preference dataset mined from reviewer edits.

Why this does NOT just `SELECT * FROM trn.sft_export`
-------------------------------------------------------
`trn.sft_export` (db/009_training.sql) is the right place to look for the
population definition (`outcome IN ('accepted','corrected')`) and the
per-session aggregates (`fully_grounded`, `tool_calls`, ...), and this
module's queries mirror its WHERE/JOIN exactly. But the view's message
`jsonb_build_object` omits `tool_result` -- a 'tool' role message ends up
with `content: null`, which is unusable for training (the model needs to
see what the tool actually returned to learn when to stop retrieving and
what it may cite). So this module re-reads `trn.trace_step` directly for
full fidelity and treats `trn.sft_export`'s definition as the contract for
*which* sessions are in scope, not as the literal data source. If the view
is ever fixed upstream to include `tool_result`, this module still works
unchanged (it does not depend on the view being broken, only on its
filter).

THE SINGLE MOST IMPORTANT INVARIANT IN THIS MODULE
----------------------------------------------------
Every message with role in ('tool','user','system') MUST have
`trainable=false`, and every 'assistant' message MUST have `trainable=true`.
`trn.trace_step.trainable` is the column the SFT collator and TRL's
`assistant_only_loss` masking are both supposed to agree with (see
db/009_training.sql's column comment: "the single most common cause of a
retrieval-trained model that hallucinates plausible-looking evidence
instead of calling the tool"). This module re-derives nothing from role --
it reads `trainable` from the DB and ASSERTS it matches what role implies,
and refuses to emit a single row if it does not. A silently-wrong mask here
is worse than a crash: it trains a model that looks fine on the loss curve
and hallucinates in production. See `validate_message_masks`.

Normalisation pipeline (in order, see `common.py` for each primitive)
------------------------------------------------------------------------
  1. canonicalise tool-call argument ordering (`canonical_tool_call_arguments`)
  2. strip volatile fields from tool results (`strip_volatile`)
  3. truncate long tool results (`truncate_tool_result`)
  4. collapse consecutive identical retrievals (`_collapse_duplicate_retrievals`)
  5. dedup near-identical trajectories across sessions, keyed on the
     canonicalised (tool_name, args) sequence (`trajectory_signature_hash`)
  6. deterministic split assignment (`deterministic_split`, honouring an
     already-set `trace_session.split` first)

`fde_training.cli`'s `export-sft` subcommand is the operator entrypoint for
this module (`--stats`, `--pairs`, `--out`); everything below is pure
library code with no argument parsing of its own.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any

from fde_mcp.logging import get_logger
from fde_training import common

log = get_logger(__name__)

# Sessions in this outcome set are exactly what trn.sft_export selects.
SFT_OUTCOMES = ("accepted", "corrected")

FETCH_TRACE_STEPS_SQL = """
    SELECT ts.session_id, ts.agent_name, ts.task_kind, ts.split AS db_split,
           ts.base_commit_id, ts.outcome,
           st.turn, st.role, st.content, st.tool_calls, st.tool_call_id,
           st.tool_name, st.tool_result, st.trainable, st.grounded
      FROM trn.trace_step st
      JOIN trn.trace_session ts ON ts.session_id = st.session_id
     WHERE ts.outcome = ANY(%(outcomes)s)
     ORDER BY ts.session_id, st.turn
"""


class MaskIntegrityError(RuntimeError):
    """Raised when a trace_step's `trainable` flag does not match what its
    `role` implies. This must never be caught and "handled" -- it means the
    upstream trace writer (fde_mcp.server, or whatever wrote the assistant
    turns) has a bug that would silently corrupt the loss mask. Fail the
    whole export run.
    """


def validate_message_masks(session_id: str, messages: list[dict[str, Any]]) -> None:
    for i, m in enumerate(messages):
        role = m["role"]
        trainable = m["trainable"]
        if role in ("tool", "user", "system") and trainable is not False:
            msg = (
                f"session {session_id} message #{i} role={role!r} has "
                f"trainable={trainable!r}, expected False. A non-assistant "
                f"turn with a truthy trainable flag would train the model "
                f"on retrieved/user tokens -- refusing to export."
            )
            raise MaskIntegrityError(msg)
        if role == "assistant" and trainable is not True:
            msg = (
                f"session {session_id} message #{i} role=assistant has "
                f"trainable={trainable!r}, expected True. An assistant turn "
                f"silently excluded from the loss mask trains a model that "
                f"looks fine on the loss curve but never learns that turn -- "
                f"refusing to export."
            )
            raise MaskIntegrityError(msg)


def _render_tool_content(tool_result: Any) -> str:
    """tool_result (already volatile-stripped + truncated) rendered as the
    'content' string of a 'tool' role message, matching the OpenAI shape
    TRL's chat template expects (tool messages carry their result as plain
    text content, not a nested object)."""
    cleaned = common.strip_volatile(tool_result)
    cleaned = common.truncate_tool_result(cleaned)
    return common.canonical_json(cleaned)


def _collapse_duplicate_retrievals(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Collapse a consecutive (assistant tool_call, tool result) pair into
    nothing when it is an exact repeat -- same tool_name, same canonical
    arguments, same (post-normalisation) result -- of the immediately
    preceding tool call/response pair. This is the "agent looped and issued
    the identical retrieval twice" pattern; keeping the duplicate teaches
    the model that re-issuing an unchanged query is a productive move, which
    is exactly the kind of thing `fde_training.rewards.r_hop_efficiency`
    later penalises at rollout time -- SFT should not teach it in the first
    place.

    Only ADJACENT duplicates are collapsed (a retrieval repeated after other
    intervening tool calls may be deliberately re-checking a value that
    changed in between, e.g. re-reading a node after a proposal was staged;
    collapsing those would be an unjustified assumption this module is not
    in a position to make).
    """
    if not messages:
        return messages, 0

    out: list[dict[str, Any]] = []
    collapsed = 0
    i = 0
    last_sig: tuple[str, str, str] | None = None  # (tool_name, args, result)
    while i < len(messages):
        m = messages[i]
        if (
            m["role"] == "assistant"
            and m.get("tool_calls")
            and len(m["tool_calls"]) == 1
            and i + 1 < len(messages)
            and messages[i + 1]["role"] == "tool"
        ):
            tool_msg = messages[i + 1]
            tc = m["tool_calls"][0]
            fn = tc.get("function") or {}
            sig = (
                fn.get("name") or "",
                common.canonical_tool_call_arguments(fn.get("arguments")),
                tool_msg.get("content") or "",
            )
            if last_sig is not None and sig == last_sig:
                collapsed += 1
                i += 2
                continue
            last_sig = sig
            out.append(m)
            out.append(tool_msg)
            i += 2
            continue
        # Any other message shape (multi-call assistant turn, plain
        # assistant answer, system/user) breaks the "last retrieval" chain.
        last_sig = None
        out.append(m)
        i += 1
    return out, collapsed


def _build_messages(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for r in rows:
        role = r["role"]
        msg: dict[str, Any] = {"role": role, "trainable": r["trainable"]}
        if role == "assistant":
            msg["content"] = r["content"]
            if r["tool_calls"]:
                msg["tool_calls"] = common.canonicalize_tool_calls(r["tool_calls"])
        elif role == "tool":
            msg["tool_call_id"] = r["tool_call_id"]
            msg["name"] = r["tool_name"]
            msg["content"] = _render_tool_content(r["tool_result"])
        else:  # system / user
            msg["content"] = r["content"]
        messages.append(msg)
    return messages


def _trajectory_signature(messages: list[dict[str, Any]]) -> str:
    sigs = []
    for m in messages:
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            sigs.append(
                common.tool_call_signature(
                    fn.get("name"), common.canonical_tool_call_arguments(fn.get("arguments"))
                )
            )
    return common.trajectory_signature_hash(sigs)


def resolve_split(db_split: str | None, session_id: str) -> str:
    """Prefer a human/pipeline-curated split already stored on
    trace_session; otherwise fall back to the deterministic hash. See
    `common.deterministic_split`'s docstring for why re-hashing is stable
    across re-exports."""
    if db_split in ("train", "validation", "test"):
        return db_split
    if db_split == "holdout":
        return "test"  # holdout is never trained/validated on; fold into test-side reporting
    return common.deterministic_split(session_id)


def build_records(
    rows_by_session: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    stats = {
        "sessions_seen": 0,
        "sessions_mask_failed": 0,
        "collapsed_duplicate_retrievals": 0,
        "sessions_dropped_as_duplicate_trajectory": 0,
        "sessions_exported": 0,
    }
    records: list[dict[str, Any]] = []
    seen_trajectory_hashes: dict[str, str] = {}  # hash -> first session_id that used it

    for session_id in sorted(rows_by_session):  # sorted: stable "first wins" for dedup
        rows = rows_by_session[session_id]
        stats["sessions_seen"] += 1
        messages = _build_messages(rows)
        try:
            validate_message_masks(session_id, messages)
        except MaskIntegrityError:
            stats["sessions_mask_failed"] += 1
            raise

        messages, n_collapsed = _collapse_duplicate_retrievals(messages)
        stats["collapsed_duplicate_retrievals"] += n_collapsed

        traj_hash = _trajectory_signature(messages)
        if traj_hash in seen_trajectory_hashes:
            stats["sessions_dropped_as_duplicate_trajectory"] += 1
            continue
        seen_trajectory_hashes[traj_hash] = session_id

        meta = {
            "session_id": session_id,
            "agent_name": rows[0]["agent_name"],
            "task_kind": rows[0]["task_kind"],
            "outcome": rows[0]["outcome"],
            "base_commit_id": rows[0]["base_commit_id"],
            "trajectory_hash": traj_hash,
            "collapsed_duplicate_retrievals": n_collapsed,
            "tool_call_count": sum(len(m.get("tool_calls") or []) for m in messages),
            "fully_grounded": all(r["grounded"] is not False for r in rows),
        }
        split = resolve_split(rows[0]["db_split"], session_id)
        meta["split"] = split

        record = {
            "messages": messages,
            "assistant_mask": [m["trainable"] for m in messages],
            "meta": meta,
        }
        records.append(record)
        stats["sessions_exported"] += 1

    return records, stats


def fetch_rows(outcomes: tuple[str, ...] = SFT_OUTCOMES) -> dict[str, list[dict[str, Any]]]:
    rows_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with common.connect() as db:
        cur = db.cursor()
        cur.execute(FETCH_TRACE_STEPS_SQL, {"outcomes": list(outcomes)})
        for row in cur.fetchall():
            rows_by_session[str(row["session_id"])].append(row)
    return rows_by_session


# ===========================================================================
# --stats
# ===========================================================================
def print_stats(rows_by_session: dict[str, list[dict[str, Any]]]) -> None:
    records, build_stats = build_records(rows_by_session)

    by_agent: Counter[str] = Counter()
    by_task: Counter[str] = Counter()
    by_outcome: Counter[str] = Counter()
    by_split: Counter[str] = Counter()
    depth_hist: Counter[str] = Counter()
    grounded = 0

    def depth_bucket(n: int) -> str:
        if n == 0:
            return "0"
        if n <= 2:
            return "1-2"
        if n <= 5:
            return "3-5"
        if n <= 10:
            return "6-10"
        return "11+"

    for r in records:
        m = r["meta"]
        by_agent[m["agent_name"]] += 1
        by_task[m["task_kind"]] += 1
        by_outcome[m["outcome"]] += 1
        by_split[m["split"]] += 1
        depth_hist[depth_bucket(m["tool_call_count"])] += 1
        if m["fully_grounded"]:
            grounded += 1

    print("=== export-sft --stats ===")
    print(f"sessions with outcome in {SFT_OUTCOMES}: {build_stats['sessions_seen']}")
    print(
        f"  collapsed duplicate adjacent retrievals: {build_stats['collapsed_duplicate_retrievals']}"
    )
    print(
        f"  dropped as duplicate trajectory: {build_stats['sessions_dropped_as_duplicate_trajectory']}"
    )
    print(f"  exported: {build_stats['sessions_exported']}")
    print()
    print("by agent_name:", dict(by_agent))
    print("by task_kind: ", dict(by_task))
    print("by outcome:   ", dict(by_outcome))
    print("by split:     ", dict(by_split))
    print("tool-call depth distribution:", dict(sorted(depth_hist.items())))
    total = len(records) or 1
    print(f"fully_grounded: {grounded}/{len(records)} ({100.0 * grounded / total:.1f}%)")


# ===========================================================================
# --pairs (DPO / preference export from reviewer edits)
# ===========================================================================
FETCH_EDITED_ITEMS_SQL = """
    SELECT p.proposal_id, p.trace_session_id, p.title, p.rationale, p.engagement_id,
           pi.item_id, pi.ordinal, pi.op, pi.node_type, pi.edge_type, pi.subject_key,
           pi.payload, pi.original_payload, pi.source_ids, pi.agent_confidence
      FROM hitl.proposal_item pi
      JOIN hitl.proposal p ON p.proposal_id = pi.proposal_id
     WHERE pi.item_status = 'edited' AND pi.original_payload IS NOT NULL
     ORDER BY p.proposal_id, pi.ordinal
"""


def _find_propose_prompt(session_id: str | None, subject_key: str) -> list[dict[str, Any]] | None:
    """Prompt = every message up to and including the assistant tool_call
    that proposed `subject_key`, EXCLUDING the tool response (the model has
    not seen the outcome yet -- that's what makes this a valid prompt for a
    preference pair). Returns None if no such trace is found (proposal
    authored outside a traced session, or the subject_key match fails), in
    which case the caller falls back to a synthetic prompt from the
    proposal's own title/rationale.
    """
    if not session_id:
        return None
    with common.connect() as db:
        cur = db.cursor()
        cur.execute(
            "SELECT turn, role, content, tool_calls, tool_call_id, tool_name, tool_result, trainable "
            "FROM trn.trace_step WHERE session_id = %(sid)s ORDER BY turn",
            {"sid": session_id},
        )
        rows = cur.fetchall()
    if not rows:
        return None

    for idx, r in enumerate(rows):
        if r["role"] != "assistant" or not r["tool_calls"]:
            continue
        for tc in r["tool_calls"]:
            fn = tc.get("function") or {}
            if fn.get("name") != "kg_propose":
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            items = args.get("items") or []
            if any(it.get("subject_key") == subject_key for it in items):
                prefix_rows = rows[: idx + 1]  # up to and including this assistant turn
                return _build_messages(prefix_rows)
    return None


def build_preference_pairs() -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    with common.connect() as db:
        cur = db.cursor()
        cur.execute(FETCH_EDITED_ITEMS_SQL)
        edited_items = cur.fetchall()

    for it in edited_items:
        prompt_messages = _find_propose_prompt(
            str(it["trace_session_id"]) if it["trace_session_id"] else None,
            it["subject_key"],
        )
        if prompt_messages is None:
            # Fallback: a synthetic single-user-turn prompt built from the
            # proposal's own rationale. Lower fidelity than the real
            # trajectory prefix, but still a valid (prompt, chosen,
            # rejected) triple -- the gate produced a real human edit either
            # way, and dropping it would waste the highest-value label kind
            # the schema comment calls out ("the most valuable label of
            # all").
            prompt_messages = [
                {
                    "role": "system",
                    "content": "Propose a knowledge-graph change item, citing evidence.",
                    "trainable": False,
                },
                {
                    "role": "user",
                    "content": f"{it['title']}\n\n{it['rationale']}\nsubject_key={it['subject_key']}",
                    "trainable": False,
                },
            ]
        pairs.append(
            {
                "prompt": prompt_messages,
                "chosen": common.canonical_json(it["payload"]),
                "rejected": common.canonical_json(it["original_payload"]),
                "meta": {
                    "proposal_id": it["proposal_id"],
                    "item_id": it["item_id"],
                    "subject_key": it["subject_key"],
                    "op": it["op"],
                    "node_type": it["node_type"],
                    "edge_type": it["edge_type"],
                },
            }
        )
    return pairs
