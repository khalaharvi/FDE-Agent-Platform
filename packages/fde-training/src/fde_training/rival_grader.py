"""rival_grader.py -- the retrieval tournament runner.

Two `trn.retriever_variant` configs answer the same `trn.eval_query` via
`kg.hybrid_search`; a judge (Bedrock Converse API) picks a winner; results
feed `trn.bradley_terry` (leaderboard) and `trn.judge_kappa`/
`trn.judge_position_bias` (calibration). See db/009_training.sql's own
"RIVAL GRADERS" comment block for why pairwise beats pointwise here
(absolute 1-10 scoring drifts across sessions; relative preference is
stable) and why position bias is worst exactly when candidates are close in
quality -- which is precisely the regime a retrieval A/B test lives in.

THE TWO INVARIANTS THIS MODULE MUST NEVER VIOLATE
----------------------------------------------------
1. Every duel is run in BOTH orders (A-first and B-first), and both rows are
   written with `mirror_duel_id` pointing at each other and `consistent`
   set correctly. `run_duel` is the only function allowed to INSERT into
   `trn.duel`, and it always inserts both rows in one call -- there is no
   code path that writes a single, unmirrored duel row.
2. The judge prompt (`JUDGE_PROMPT_V1`, versioned so a future prompt change
   is a new, comparable `judge_prompt_version`, never a silent edit of one
   that duels already reference) explicitly forbids using result-list
   length or presentation order as a signal, and REQUIRES the judge to name
   which specific retrieved items (by node_key) support its verdict --
   without that, "which one do you prefer" degenerates into "which one
   looks longer", which is exactly the kind of judge artefact
   `trn.judge_kappa`'s "suspiciously high" band exists to catch.

Judge backend
-------------
`bedrock_judge` calls Bedrock's Converse API (model configurable via
`--judge-model`) through `boto3`. This environment's AWS credentials are a
sandbox placeholder -- `bedrock_judge` is real, runnable code but has NOT
been exercised against a live Bedrock endpoint here. Every command in this
module also accepts an injectable `judge_fn` (see `MockJudge`) so the
duel/calibration/leaderboard machinery itself -- the part with actual logic
bugs to catch -- is fully testable without any AWS access.
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fde_mcp.logging import get_logger
from fde_training import common

log = get_logger(__name__)

# ===========================================================================
# Judge prompt -- VERSIONED. Bump JUDGE_PROMPT_VERSION and keep the old
# constant around (as JUDGE_PROMPT_V1, V2, ...) whenever the wording
# changes; `trn.duel.judge_prompt_version` is how a later calibration run
# knows which wording produced which kappa.
# ===========================================================================
JUDGE_PROMPT_VERSION = "v1"

JUDGE_PROMPT_V1 = """You are evaluating two retrieval result sets, Set A and Set B, both produced \
in an attempt to answer the same question against the same knowledge graph.

Question: {question}

Set A ({a_count} results):
{a_rendered}

Set B ({b_count} results):
{b_rendered}

Decide which set BETTER equips someone to answer the question. Judge only \
the CONTENT of what each set found -- whether the retrieved nodes, their \
labels/summaries, and their relationships actually contain the information \
needed to answer the question correctly and completely.

STRICT RULES, both mandatory:
1. Do NOT prefer a set merely because it contains more or fewer results. A \
   set with 3 highly relevant results can and should beat a set with 20 \
   mostly irrelevant ones, and vice versa.
2. Do NOT let the order in which Set A and Set B are presented to you \
   influence your verdict. Presentation order carries no information about \
   quality.
3. You MUST name at least one specific retrieved item (by its node_key, in \
   brackets like [this]) from the winning set that concretely supports your \
   verdict. A verdict with no cited item is invalid.

Respond with ONLY a JSON object, no other text, in this exact shape:
{{"winner": "A" | "B" | "tie", "confidence": <0.0-1.0>, "cited_items": ["node_key", ...], "rationale": "<one or two sentences>"}}
"""


@dataclass
class JudgeVerdict:
    winner: str | None  # "A" | "B" | None (tie)
    confidence: float
    cited_items: list[str]
    rationale: str


JudgeFn = Callable[[str], JudgeVerdict]


def bedrock_judge(
    prompt: str,
    model_id: str = "anthropic.claude-3-5-sonnet-20241022-v2:0",
    region: str | None = None,
) -> JudgeVerdict:
    """Real Bedrock Converse call. Import boto3 lazily so this module is
    importable (and its non-judge logic testable) with no AWS SDK
    dependency resolved at all until a caller actually wants a live judge.
    """
    import boto3  # noqa: PLC0415

    client = boto3.client("bedrock-runtime", region_name=region)
    resp = client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 400, "temperature": 0.0},
    )
    text = "".join(block.get("text", "") for block in resp["output"]["message"]["content"])
    return _parse_verdict(text)


def _parse_verdict(text: str) -> JudgeVerdict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        msg = f"judge response did not contain a JSON object: {text!r}"
        raise ValueError(msg)
    obj = json.loads(match.group(0))
    winner_raw = str(obj.get("winner", "")).strip().upper()
    winner = winner_raw if winner_raw in ("A", "B") else None
    cited = obj.get("cited_items") or []
    if winner is not None and not cited:
        msg = f"judge picked a winner but cited no items (rule 3 violated): {obj}"
        raise ValueError(msg)
    return JudgeVerdict(
        winner=winner,
        confidence=float(obj.get("confidence", 0.5)),
        cited_items=list(cited),
        rationale=str(obj.get("rationale", "")),
    )


class MockJudge:
    """Deterministic, dependency-free stand-in for `bedrock_judge`, used by
    tests and by the CLI's `--judge mock` for dry-running the tournament
    machinery without AWS access. Prefers whichever result set has the
    higher sum of `rrf_score` (a real, if crude, quality proxy) -- NOT list
    length, so tests can assert the "do not prefer more results" rule is
    meaningful. Optional `position_bias` in [0, 1] injects a controllable
    fraction of verdicts that ignore content and just pick whichever set is
    labelled "A" in the prompt (i.e. whichever was presented first) -- this
    is what the test suite uses to prove `judge_position_bias`/
    `judge_kappa` actually detect a biased judge rather than
    rubber-stamping it.
    """

    def __init__(self, position_bias: float = 0.0, seed: int = 0) -> None:
        self.position_bias = position_bias
        self._rng = random.Random(seed)  # noqa: S311 -- deterministic test double, not cryptographic

    def __call__(self, prompt: str) -> JudgeVerdict:
        # NOTE: do not `prompt.split("Set B")` -- JUDGE_PROMPT_V1's own
        # intro sentence ("...Set A and Set B, both produced...") contains
        # the literal substring "Set B" BEFORE the actual Set A section
        # even starts, so a naive split silently misattributes Set A's own
        # rendered block to "B" and leaves "A" empty. Anchor on the
        # section-header lines instead, which are unambiguous.
        a_block = self._section(prompt, "Set A", "Set B")
        b_block = self._section(prompt, "Set B", "Decide")
        a_scores = [float(x) for x in re.findall(r"rrf_score=([\-0-9.]+)", a_block)]
        b_scores = [float(x) for x in re.findall(r"rrf_score=([\-0-9.]+)", b_block)]
        a_keys = re.findall(r"\[([a-zA-Z0-9_:.\-]+)\]", a_block)
        b_keys = re.findall(r"\[([a-zA-Z0-9_:.\-]+)\]", b_block)

        if self.position_bias > 0 and self._rng.random() < self.position_bias:
            winner, cite = "A", (a_keys[:1] or ["_none_"])
        else:
            a_sum, b_sum = sum(a_scores), sum(b_scores)
            if abs(a_sum - b_sum) < 1e-9:
                return JudgeVerdict(
                    winner=None,
                    confidence=0.5,
                    cited_items=[],
                    rationale="tie: equal content quality",
                )
            winner, cite = (
                ("A", a_keys[:1] or ["_none_"])
                if a_sum > b_sum
                else ("B", b_keys[:1] or ["_none_"])
            )
        return JudgeVerdict(
            winner=winner, confidence=0.9, cited_items=cite, rationale="mock: higher rrf_score sum"
        )

    @staticmethod
    def _section(prompt: str, start_marker: str, end_marker: str) -> str:
        match = re.search(
            re.escape(start_marker) + r" \(\d+ results\):\n(.*?)\n\n" + re.escape(end_marker),
            prompt,
            re.DOTALL,
        )
        return match.group(1) if match else ""


# ===========================================================================
# Rendering a variant's retrieval result set
# ===========================================================================
def render_variant_results(
    engagement_id: str, query_vec: list[float], config: dict[str, Any], k: int = 10
) -> list[dict[str, Any]]:
    """Run kg.hybrid_search with one trn.retriever_variant's exact config
    (see db/010_roles_and_seed_policy.sql for the seeded variants' shapes:
    seed_k, expand_hops, rrf_k, ef_search, iterative_scan, node_types)."""
    ef_search = config.get("ef_search", 100)
    iterative_scan = config.get("iterative_scan", "relaxed_order")
    max_scan = config.get("max_scan_tuples", 20000)

    with common.connect() as db:
        conn = db.conn
        conn.execute("SELECT kg.tune_session(%s, %s, %s)", (ef_search, iterative_scan, max_scan))
        cur = db.cursor()
        cur.execute(
            """SELECT node_key, node_type, label, summary, rrf_score, provenance
                 FROM kg.hybrid_search(%(eng)s::uuid, %(vec)s::kg.embedding, %(k)s, %(seed_k)s,
                                        %(hops)s, NULL::kg.edge_type[], %(node_types)s::kg.node_type[],
                                        %(rrf_k)s)""",
            {
                "eng": engagement_id,
                "vec": "[" + ",".join(repr(float(x)) for x in query_vec) + "]",
                "k": k,
                "seed_k": config.get("seed_k", 30),
                "hops": config.get("expand_hops", 2),
                "node_types": config.get("node_types"),
                "rrf_k": config.get("rrf_k", 60),
            },
        )
        return common.jsonify(cur.fetchall())  # type: ignore[no-any-return]


def _render_block(results: list[dict[str, Any]]) -> str:
    if not results:
        return "(no results)"
    lines = []
    for r in results:
        lines.append(
            f"- [{r['node_key']}] ({r['node_type']}) {r['label']}: {r.get('summary') or ''} "
            f"rrf_score={r.get('rrf_score', 0.0):.6f}"
        )
    return "\n".join(lines)


# ===========================================================================
# run_duel -- the ONLY function that writes trn.duel.
# ===========================================================================
def run_duel(
    query_id: int,
    variant_a_id: int,
    variant_b_id: int,
    judge_fn: JudgeFn,
    *,
    judge_model: str,
    embed_fn: Callable[[str], list[float]],
    k: int = 10,
) -> tuple[int, int]:
    with common.connect() as db:
        cur = db.cursor()
        cur.execute("SELECT * FROM trn.eval_query WHERE query_id = %(qid)s", {"qid": query_id})
        q = cur.fetchone()
        cur.execute(
            "SELECT * FROM trn.retriever_variant WHERE variant_id = %(vid)s", {"vid": variant_a_id}
        )
        variant_a = cur.fetchone()
        cur.execute(
            "SELECT * FROM trn.retriever_variant WHERE variant_id = %(vid)s", {"vid": variant_b_id}
        )
        variant_b = cur.fetchone()
    if q is None or variant_a is None or variant_b is None:
        msg = f"query_id={query_id}, variant_a={variant_a_id}, variant_b={variant_b_id}: not all found"
        raise ValueError(msg)

    vec = embed_fn(q["question"])
    results_a = render_variant_results(q["engagement_id"], vec, variant_a["config"], k=k)
    results_b = render_variant_results(q["engagement_id"], vec, variant_b["config"], k=k)

    # Pool relevance judgements TREC-style as a side effect of running the
    # duel -- every key either variant surfaced becomes judgeable.
    _pool_keys(query_id, [r["node_key"] for r in results_a] + [r["node_key"] for r in results_b])

    verdict_order1 = _judge_pair(q["question"], results_a, results_b, judge_fn)  # A shown first
    verdict_order2 = _judge_pair(q["question"], results_b, results_a, judge_fn)  # B shown first

    winner_order1 = {"A": variant_a_id, "B": variant_b_id, None: None}[verdict_order1.winner]
    # order2 labels B as "A" and A as "B" in the prompt, so translate back.
    winner_order2 = {"A": variant_b_id, "B": variant_a_id, None: None}[verdict_order2.winner]

    consistent = winner_order1 == winner_order2

    with common.connect() as db:
        cur = db.cursor()
        cur.execute(
            """INSERT INTO trn.duel (query_id, variant_a, variant_b, presented_first, judge_model,
                                      judge_prompt_version, winner, confidence, rationale)
               VALUES (%(qid)s,%(va)s,%(vb)s,%(pf)s,%(jm)s,%(jpv)s,%(winner)s,%(conf)s,%(rat)s)
               RETURNING duel_id""",
            {
                "qid": query_id,
                "va": variant_a_id,
                "vb": variant_b_id,
                "pf": variant_a_id,
                "jm": judge_model,
                "jpv": JUDGE_PROMPT_VERSION,
                "winner": winner_order1,
                "conf": verdict_order1.confidence,
                "rat": verdict_order1.rationale,
            },
        )
        row = cur.fetchone()
        assert row is not None
        duel_id_1 = row["duel_id"]

        cur.execute(
            """INSERT INTO trn.duel (query_id, variant_a, variant_b, presented_first, judge_model,
                                      judge_prompt_version, winner, confidence, rationale,
                                      mirror_duel_id, consistent)
               VALUES (%(qid)s,%(va)s,%(vb)s,%(pf)s,%(jm)s,%(jpv)s,%(winner)s,%(conf)s,%(rat)s,%(mirror)s,%(consistent)s)
               RETURNING duel_id""",
            {
                "qid": query_id,
                "va": variant_a_id,
                "vb": variant_b_id,
                "pf": variant_b_id,
                "jm": judge_model,
                "jpv": JUDGE_PROMPT_VERSION,
                "winner": winner_order2,
                "conf": verdict_order2.confidence,
                "rat": verdict_order2.rationale,
                "mirror": duel_id_1,
                "consistent": consistent,
            },
        )
        row2 = cur.fetchone()
        assert row2 is not None
        duel_id_2 = row2["duel_id"]

        cur.execute(
            "UPDATE trn.duel SET mirror_duel_id = %(d2)s, consistent = %(consistent)s WHERE duel_id = %(d1)s",
            {"d2": duel_id_2, "consistent": consistent, "d1": duel_id_1},
        )
    if not consistent:
        log.warning(
            "duel_order_inconsistent",
            query_id=query_id,
            variant_a=variant_a_id,
            variant_b=variant_b_id,
            winner_order1=winner_order1,
            winner_order2=winner_order2,
        )
    return duel_id_1, duel_id_2


def _judge_pair(
    question: str,
    results_first: list[dict[str, Any]],
    results_second: list[dict[str, Any]],
    judge_fn: JudgeFn,
) -> JudgeVerdict:
    prompt = JUDGE_PROMPT_V1.format(
        question=question,
        a_count=len(results_first),
        a_rendered=_render_block(results_first),
        b_count=len(results_second),
        b_rendered=_render_block(results_second),
    )
    return judge_fn(prompt)


def _pool_keys(query_id: int, keys: list[str]) -> None:
    if not keys:
        return
    with common.connect() as db:
        cur = db.cursor()
        cur.execute(
            """UPDATE trn.eval_query
                  SET pooled_keys = (SELECT array_agg(DISTINCT k) FROM unnest(pooled_keys || %(new)s) AS k)
                WHERE query_id = %(qid)s""",
            {"qid": query_id, "new": keys},
        )


# ===========================================================================
# calibrate() -- sample duels for human adjudication, report kappa + bias.
# ===========================================================================
def calibrate(
    judge_model: str, n: int = 30, human_labels: dict[int, int | None] | None = None
) -> dict[str, Any]:
    """Sample up to `n` un-adjudicated duels for `judge_model` and apply
    `human_labels` (duel_id -> winning variant_id, or None for a human tie)
    if provided; otherwise prompt on stdin. Returns trn.judge_kappa +
    trn.judge_position_bias's output plus an explicit go/no-go verdict on
    using this judge as an RL reward signal.
    """
    with common.connect() as db:
        cur = db.cursor()
        cur.execute(
            """SELECT d.duel_id, q.question, va.name AS variant_a_name, vb.name AS variant_b_name, d.winner
                 FROM trn.duel d
                 JOIN trn.eval_query q ON q.query_id = d.query_id
                 JOIN trn.retriever_variant va ON va.variant_id = d.variant_a
                 JOIN trn.retriever_variant vb ON vb.variant_id = d.variant_b
                WHERE d.judge_model = %(jm)s AND d.human_winner IS NULL
                ORDER BY d.duel_id LIMIT %(n)s""",
            {"jm": judge_model, "n": n},
        )
        sample = cur.fetchall()

    for row in sample:
        duel_id = row["duel_id"]
        if human_labels is not None:
            if duel_id not in human_labels:
                continue
            human_winner = human_labels[duel_id]
        else:  # pragma: no cover -- interactive path, not exercised by tests
            print(f"\nQuestion: {row['question']}")
            print(f"  [A] {row['variant_a_name']}   [B] {row['variant_b_name']}")
            choice = input("Which is better? (a/b/tie): ").strip().lower()
            human_winner = None
            with common.connect() as db2:
                cur2 = db2.cursor()
                cur2.execute(
                    "SELECT variant_a, variant_b FROM trn.duel WHERE duel_id = %(d)s",
                    {"d": duel_id},
                )
                pair_row = cur2.fetchone()
                assert pair_row is not None
                va, vb = pair_row["variant_a"], pair_row["variant_b"]
            if choice == "a":
                human_winner = va
            elif choice == "b":
                human_winner = vb

        with common.connect() as db3:
            cur3 = db3.cursor()
            cur3.execute(
                "UPDATE trn.duel SET human_winner = %(w)s, human_judged_by = %(by)s WHERE duel_id = %(d)s",
                {"w": human_winner, "by": "fde_training.rival_grader:calibrate", "d": duel_id},
            )

    with common.connect() as db:
        cur = db.cursor()
        cur.execute("SELECT * FROM trn.judge_kappa(%(jm)s)", {"jm": judge_model})
        kappa_row = cur.fetchone()
        cur.execute("SELECT * FROM trn.judge_position_bias(%(jm)s)", {"jm": judge_model})
        bias_row = cur.fetchone()

    usable_as_rl_reward = (
        kappa_row is not None
        and kappa_row["cohens_kappa"] is not None
        and 0.60 <= kappa_row["cohens_kappa"] <= 0.82
        and (bias_row is None or bias_row["bias_rate"] is None or bias_row["bias_rate"] < 0.15)
    )
    return {
        "kappa": dict(kappa_row) if kappa_row else None,
        "position_bias": dict(bias_row) if bias_row else None,
        "usable_as_rl_reward": usable_as_rl_reward,
        "verdict": (
            "USABLE as an RL reward signal"
            if usable_as_rl_reward
            else "NOT recommended as an RL reward signal yet -- see kappa verdict / position bias rate above"
        ),
    }


def leaderboard(iterations: int = 100) -> list[dict[str, Any]]:
    with common.connect() as db:
        cur = db.cursor()
        cur.execute("SELECT * FROM trn.bradley_terry(%(it)s)", {"it": iterations})
        return cur.fetchall()


def default_embed_fn() -> Callable[[str], list[float]]:
    """Lazily import `rollout_env` (which itself has zero heavy-ML imports,
    but keeping the import inside the function avoids a module-level
    dependency cycle: `rollout_env` does not import this module, but
    keeping the wiring one-directional at import time is simpler to reason
    about than relying on that being true forever)."""
    from fde_training import rollout_env  # noqa: PLC0415

    return rollout_env.deterministic_fake_embed
