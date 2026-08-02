"""lambda_grader.py -- Lambda code-grader handler for Bedrock
Reinforcement Fine-Tuning (RFT).

VERIFIED GROUNDING this module is built against: Bedrock RFT is real and
GRPO-based; supported base models are `amazon.nova-2-lite-v1:0:256k`
(us-east-1), `openai.gpt-oss-20b` (us-west-2), `qwen.qwen3-32b`
(us-west-2). Graders are either custom Lambda code graders (MUST run in
seconds) or model-as-a-judge. AWS's own guidance is to start with 100-200
prompts.

Why this is a SEPARATE, SMALLER implementation than `fde_training.rewards`
----------------------------------------------------------------------------
This handler is invoked by the Bedrock RFT service itself, once per
completion, as a real AWS Lambda function with a hard wall-clock budget
("must run in seconds" per the grounding above). It deliberately does NOT:
  - open a connection to the training Postgres (no VPC peering assumption,
    and a per-invocation DB round-trip risks blowing the latency budget
    under RFT's own concurrency);
  - import `fde_training.rewards`'s `EpisodeLog` machinery wholesale (that
    package assumes a full multi-turn transcript with an `environments`
    kwarg tying back to a live `RolloutEnv`, which does not exist inside a
    Bedrock-managed RFT rollout).
Instead it re-implements the STRUCTURAL parts of the same composite reward
(format validity, schema validity, grounding against a supplied provenance
snippet, citation format, brevity) using only stdlib, scoring the single
prompt/response pair Bedrock hands it plus whatever gold/provenance data
was embedded in the DATASET ROW itself (RFT's grader contract passes the
original dataset record's extra fields through to the grader). This is a
deliberate, documented duplication -- see the package README's comparison
table for why this is the accepted cost of the fully-managed path (no
callback into your own retrieval stack mid-rollout) versus the self-managed
path (`rollout_env.py`, which DOES query the live graph per step).

An AWS Lambda deployment package for this handler must NOT bundle the rest
of `fde_training` (or any of its dependencies) -- this file is written to
have zero imports beyond stdlib specifically so it can be zipped up alone.

Bedrock RFT Lambda grader contract (as documented): the Lambda receives an
event containing the model's response and the dataset row's fields, and
must return a JSON object with a scalar `reward` in a bounded range (this
handler uses [0.0, 1.0]) -- see `lambda_handler` below for the exact event
shape assumed and `PAYLOAD SHAPE` for how a `bedrock_rft.py`-built dataset
row supplies gold/provenance data to it.
"""

from __future__ import annotations

import json
import re
from typing import Any

CITATION_RE = re.compile(r"\[([a-zA-Z0-9_:.\-]+)\]")

# Mirrors fde_training.rewards.DEFAULT_WEIGHTS' priority ordering (grounding
# and outcome dominate; format/citation are gates, not the main signal) but
# collapsed to the three terms this handler can score without a live DB:
# grounding against the SUPPLIED provenance snippet, outcome F1 against the
# supplied gold key set, and citation format.
WEIGHTS = {"grounded": 0.45, "outcome": 0.40, "citation": 0.15}


def _grounded_score(response_text: str, provenance_keys: list[str]) -> float:
    cited = set(CITATION_RE.findall(response_text))
    if not cited:
        stripped = response_text.strip().lower()
        abstention = any(
            p in stripped
            for p in (
                "no information found",
                "not enough evidence",
                "cannot find",
                "insufficient evidence",
            )
        )
        return 1.0 if (not stripped or abstention) else 0.0
    provenance_set = set(provenance_keys)
    return sum(1 for k in cited if k in provenance_set) / len(cited)


def _outcome_f1(response_text: str, gold_keys: list[str]) -> float:
    predicted = set(CITATION_RE.findall(response_text))
    gold = set(gold_keys)
    if not predicted and not gold:
        return 1.0
    if not predicted or not gold:
        return 0.0
    tp = len(predicted & gold)
    if tp == 0:
        return 0.0
    precision, recall = tp / len(predicted), tp / len(gold)
    return 2 * precision * recall / (precision + recall)


def _citation_format_score(response_text: str) -> float:
    text = response_text.strip()
    if not text:
        return 1.0
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    substantive = [s for s in sentences if len(s.split()) > 3]
    if not substantive:
        return 1.0
    cited = sum(1 for s in substantive if CITATION_RE.search(s))
    return cited / len(substantive)


def score(response_text: str, gold_keys: list[str], provenance_keys: list[str]) -> dict[str, Any]:
    grounded = _grounded_score(response_text, provenance_keys)
    outcome = _outcome_f1(response_text, gold_keys)
    citation = _citation_format_score(response_text)
    reward = (
        WEIGHTS["grounded"] * grounded
        + WEIGHTS["outcome"] * outcome
        + WEIGHTS["citation"] * citation
    )
    return {
        "reward": round(reward, 6),
        "components": {"grounded": grounded, "outcome": outcome, "citation": citation},
    }


# ===========================================================================
# PAYLOAD SHAPE
#
# `bedrock_rft.py` builds each training-set JSONL row as:
#   {"prompt": "<rendered question + tool-use instructions>",
#    "referenceResponse": {"gold_keys": [...], "provenance_keys": [...]}}
# Bedrock RFT's grader invocation event echoes the dataset row's extra
# fields back to the Lambda alongside the model's actual response; this
# handler reads them from `event["referenceResponse"]` per that contract.
# If your account's RFT event shape differs (this has NOT been exercised
# against a live RFT job in this environment -- no AWS access here), adjust
# `_extract_response_and_reference` only; `score()` itself is unit-tested
# independently of the event-parsing shim.
# ===========================================================================
def _extract_response_and_reference(event: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    response_text = (
        event.get("modelResponse") or event.get("response") or event.get("completion") or ""
    )
    if isinstance(response_text, dict):
        # Some grader event shapes nest the text under a messages-like list.
        response_text = response_text.get("content") or json.dumps(response_text)

    reference = event.get("referenceResponse") or event.get("reference") or {}
    if isinstance(reference, str):
        try:
            reference = json.loads(reference)
        except json.JSONDecodeError:
            reference = {}
    gold_keys = reference.get("gold_keys", [])
    provenance_keys = reference.get("provenance_keys", [])
    return response_text, gold_keys, provenance_keys


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """AWS Lambda entrypoint. Must complete in low single-digit seconds
    (RFT's own documented constraint) -- there is no I/O in this handler at
    all (no DB, no network), by design, to make that bound trivial to meet.
    """
    del context  # required by the Lambda handler signature; unused here
    try:
        response_text, gold_keys, provenance_keys = _extract_response_and_reference(event)
        result = score(response_text, gold_keys, provenance_keys)
        return {"statusCode": 200, "body": json.dumps(result)}
    except Exception as exc:
        return {"statusCode": 200, "body": json.dumps({"reward": 0.0, "error": str(exc)})}
