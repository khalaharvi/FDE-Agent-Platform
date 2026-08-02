"""Post-deploy smoke tests against LIVE AWS resources.

Run by ci.yml's `deploy` job after runtimes and the grader Lambda update.
Deliberately tiny: two read-only probes that prove the deployed artifacts
answer at all, not a functional suite (that is what the requires_db suites
are for, pre-deploy). Skips itself entirely unless FDE_SMOKE_RUNTIME_ARN is
set, so `pytest tests/` on a laptop stays green and honest.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

RUNTIME_ARN = os.environ.get("FDE_SMOKE_RUNTIME_ARN")
GRADER_NAME = os.environ.get("FDE_SMOKE_GRADER_NAME", "fde-rft-grader")

pytestmark = pytest.mark.skipif(
    not RUNTIME_ARN,
    reason="FDE_SMOKE_RUNTIME_ARN not set -- live-AWS smoke tests only run in the deploy job",
)


def test_engagement_runtime_answers_a_trivial_read_only_task() -> None:
    import boto3

    client = boto3.client("bedrock-agentcore")
    response = client.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        runtimeSessionId=uuid.uuid4().hex + uuid.uuid4().hex[:8],  # >= 33 chars
        payload=json.dumps(
            {
                "task": "map_workflow",
                "engagement_id": str(uuid.uuid4()),
                "input": {"instruction": "List the tools you have available. Do not call any."},
            }
        ).encode(),
    )
    # SSE stream must terminate with at least one event; an empty body means
    # the runtime accepted the request but the agent process never answered.
    body = response["response"].read()
    assert body, "runtime returned an empty response stream"


def test_grader_lambda_returns_bounded_reward_for_canned_event() -> None:
    import boto3

    client = boto3.client("lambda")
    event = {
        "modelResponse": "The discount threshold is 20% [ctl:discount_threshold_20].",
        "referenceResponse": {
            "gold_keys": ["ctl:discount_threshold_20"],
            "provenance_keys": ["ctl:discount_threshold_20"],
        },
    }
    result = client.invoke(FunctionName=GRADER_NAME, Payload=json.dumps(event).encode())
    payload = json.loads(result["Payload"].read())
    assert payload["statusCode"] == 200
    reward = json.loads(payload["body"])["reward"]
    assert 0.0 <= reward <= 1.0
