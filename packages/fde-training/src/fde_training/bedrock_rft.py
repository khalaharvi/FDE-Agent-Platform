"""bedrock_rft.py -- the AWS-managed RL path: build a Bedrock
Reinforcement Fine-Tuning (RFT) job.

VERIFIED GROUNDING this module is built against (repeated here, not just in
`lambda_grader.py`, because both files must stay consistent with it):
  - Bedrock RFT is real and GRPO-based.
  - Supported base models (as of this pipeline's design):
      `amazon.nova-2-lite-v1:0:256k` (us-east-1),
      `openai.gpt-oss-20b`           (us-west-2),
      `qwen.qwen3-32b`               (us-west-2).
    `SUPPORTED_MODELS` below is the single source of truth for this
    constraint; `build_job_config` refuses to build a job for anything else
    or in the wrong region, loudly, rather than letting a
    CreateModelCustomizationJob call fail confusingly deep into a training
    run.
  - Graders are either custom Lambda code graders (see `lambda_grader.py` --
    MUST run in seconds) or model-as-a-judge.
  - AWS's own guidance is to START WITH 100-200 PROMPTS. `write_dataset`
    below WARNS (does not refuse -- a deliberately small smoke-test run is
    legitimate) when asked to build fewer than 100 or more than a few
    thousand prompts without an explicit override, because the single most
    common way to waste an RFT budget is to point it at your entire
    eval_query table on the first attempt.

This module builds:
  1. The prompt dataset (JSONL), reading the same `trn.eval_query` +
     `kg.hybrid_search` pipeline `rival_grader.py` uses for provenance, so
     the Lambda grader's `referenceResponse.provenance_keys` (see
     `lambda_grader.py`'s PAYLOAD SHAPE) is grounded in the SAME retrieval
     the SFT/GRPO paths use -- not a separately-curated gold set that could
     drift from what the live graph actually returns.
  2. A job config dict shaped like `bedrock.create_model_customization_job`
     (Bedrock RFT is exposed through that same control-plane API family;
     the exact parameter names are versioned by boto3's model, so this
     module builds a plain dict and only touches `boto3` inside
     `submit_job`, lazily, so building/validating a job config has zero AWS
     dependency).

NOT exercised against a live AWS account in this environment -- no network
egress to the Bedrock control plane / no real credentials here. `build_dataset`
(pure Postgres + stdlib) and `build_job_config` (pure stdlib validation) ARE
exercised, against the live fixture DB; `submit_job` is real, runnable code
that has not been invoked.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fde_mcp.logging import get_logger
from fde_training import common, rival_grader

log = get_logger(__name__)

# region -> allowed model id, per VERIFIED GROUNDING.
SUPPORTED_MODELS: dict[str, str] = {
    "amazon.nova-2-lite-v1:0:256k": "us-east-1",
    "openai.gpt-oss-20b": "us-west-2",
    "qwen.qwen3-32b": "us-west-2",
}

MIN_RECOMMENDED_PROMPTS = 100
MAX_RECOMMENDED_PROMPTS = 200


def build_dataset(
    engagement_id: str | None = None,
    k: int = 10,
    embed_fn: Callable[[str], list[float]] | None = None,
) -> list[dict[str, Any]]:
    """One row per `trn.eval_query`: `{"prompt": ..., "referenceResponse":
    {"gold_keys": ..., "provenance_keys": ...}}` -- the exact shape
    `lambda_grader.py` expects in its `referenceResponse` field.
    `provenance_keys` comes from actually running `kg.hybrid_search`
    (the champion `trn.retriever_variant`) against the live graph right
    now, not from a stale cached set, so the grader's grounding check
    reflects what a real rollout would actually be able to retrieve.
    """
    if embed_fn is None:
        embed_fn = rival_grader.default_embed_fn()

    with common.connect() as db:
        cur = db.cursor()
        if engagement_id:
            cur.execute(
                "SELECT * FROM trn.eval_query WHERE engagement_id = %(eng)s::uuid",
                {"eng": engagement_id},
            )
        else:
            cur.execute("SELECT * FROM trn.eval_query")
        queries = cur.fetchall()
        cur.execute("SELECT * FROM trn.retriever_variant WHERE is_champion")
        champion = cur.fetchone()
    if champion is None:
        msg = "no champion trn.retriever_variant set -- see db/010_roles_and_seed_policy.sql"
        raise RuntimeError(msg)

    rows = []
    for q in queries:
        vec = embed_fn(q["question"])
        results = rival_grader.render_variant_results(
            str(q["engagement_id"]), vec, champion["config"], k=k
        )
        provenance_keys = [r["node_key"] for r in results]
        prompt = (
            f"Answer the following question about the business, citing every entity you "
            f"reference in [bracket] form using its node_key. If you cannot answer with "
            f"confidence, say so explicitly rather than guessing.\n\nQuestion: {q['question']}"
        )
        rows.append(
            {
                "prompt": prompt,
                "referenceResponse": {
                    "gold_keys": list(q["relevant_keys"] or []),
                    "provenance_keys": provenance_keys,
                },
            }
        )
    return rows


def write_dataset(
    rows: list[dict[str, Any]], path: str, *, allow_out_of_band_size: bool = False
) -> None:
    n = len(rows)
    if not allow_out_of_band_size and not (MIN_RECOMMENDED_PROMPTS <= n <= MAX_RECOMMENDED_PROMPTS):
        log.warning(
            "rft_dataset_size_out_of_band",
            n_prompts=n,
            min_recommended=MIN_RECOMMENDED_PROMPTS,
            max_recommended=MAX_RECOMMENDED_PROMPTS,
            hint="pass allow_out_of_band_size=True (--i-know-what-im-doing on the CLI) once decided",
        )
    with Path(path).open("w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    log.info("rft_dataset_written", n_prompts=n, path=path)


def build_job_config(
    job_name: str,
    base_model_id: str,
    region: str,
    *,
    training_dataset_s3_uri: str,
    grader_lambda_arn: str,
    role_arn: str,
    output_s3_uri: str,
    max_prompts_per_step: int = 32,
) -> dict[str, Any]:
    """Validates the model/region pairing against SUPPORTED_MODELS and
    returns a plain dict shaped for `boto3` Bedrock's model-customization
    job API. Kept as a dict (not a live API call) so this function is
    testable and diffable without touching AWS."""
    expected_region = SUPPORTED_MODELS.get(base_model_id)
    if expected_region is None:
        msg = (
            f"base_model_id={base_model_id!r} is not one of the supported RFT base models: "
            f"{sorted(SUPPORTED_MODELS)}"
        )
        raise ValueError(msg)
    if region != expected_region:
        msg = f"base_model_id={base_model_id!r} is only supported in {expected_region!r}, got region={region!r}"
        raise ValueError(msg)

    return {
        "jobName": job_name,
        "customizationType": "REINFORCEMENT_FINE_TUNING",
        "baseModelIdentifier": base_model_id,
        "roleArn": role_arn,
        "trainingDataConfig": {"s3Uri": training_dataset_s3_uri},
        "outputDataConfig": {"s3Uri": output_s3_uri},
        "customizationConfig": {
            "reinforcementFineTuningConfig": {
                "grader": {
                    "type": "LAMBDA",
                    "lambdaArn": grader_lambda_arn,
                },
                "maxPromptsPerStep": max_prompts_per_step,
            }
        },
    }


def submit_job(job_config: dict[str, Any], region: str) -> dict[str, Any]:
    """Real call, lazily imports boto3. NOT exercised in this environment
    (see module docstring)."""
    import boto3  # noqa: PLC0415

    client = boto3.client("bedrock", region_name=region)
    return client.create_model_customization_job(**job_config)  # type: ignore[no-any-return]
