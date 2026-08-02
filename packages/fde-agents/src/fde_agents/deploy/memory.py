"""Provision the AgentCore Memory resource(s) the FDE agents use for
cross-turn/cross-session context that does NOT belong in the knowledge
graph.

Why this exists alongside the graph at all: `kg.*` is the system of record
for "how the business works" -- reviewed, gated, versioned. AgentCore
Memory is for the much smaller, much less consequential category of "what
did this session already discuss" and "what does this particular reviewer/
operator tend to prefer" -- ephemeral-ish conversational context that would
be actively wrong to push through a HITL gate (nobody should have to
approve a proposal to remember that an FDE prefers terse status updates).

Three strategies are provisioned, matching the real
`CreateMemory.memoryStrategies` union keys exactly (`semanticMemoryStrategy`,
`summaryMemoryStrategy`, `userPreferenceMemoryStrategy`; `customMemoryStrategy`
is defined here too, commented on below; `episodicMemoryStrategy` also
exists on the API but is not enabled by default for this platform -- see
its section):

  1. **Semantic** (`fde-semantic`) -- durable facts a session surfaces about
     how the FDE prefers to work THIS ENGAGEMENT (not graph facts about the
     customer's business -- those go through `kg_propose`, never through
     memory). Namespaced per engagement+actor so one FDE's notes on one
     engagement never leak into another.
  2. **Summary** (`fde-summary`) -- session-level rolling summaries, so a
     `human`-kind workflow step or a multi-day HITL gate wait
     (`fde_agents.common.hitl`) can resume a long-idle session without
     replaying its full transcript.
  3. **User preference** (`fde-user-pref`) -- durable operator preferences
     (e.g. "always show me the SQL a drift signal is based on") that should
     follow a specific human reviewer across engagements, not just within
     one.

`customMemoryStrategy` is deliberately NOT provisioned by default: its
`selfManagedConfiguration` routes extraction through an operator-owned
Lambda via SNS/S3, which is the right knob for a customer who needs their
own PII/redaction pipeline in front of anything AgentCore stores -- but that
is a per-engagement decision, not a platform default, so it is left as a
documented `--enable-custom-strategy` opt-in rather than always-on.
`episodicMemoryStrategy` (multi-session narrative reflection) is likewise
left off by default: it is the strategy most likely to accumulate exactly
the kind of narrative-about-people content the platform's PII posture
(`db/001_extensions_and_types.sql`) is careful to keep out of the graph, so
enabling it is a decision that should be made deliberately per engagement,
not baked into the base provisioning script.

Namespace templates use the syntax from the verified AgentCore facts this
script was built against: `/strategy/{strategyId}/actor/{actorId}/session/
{sessionId}`. `actorId` here is expected to be `<agent_name>:<engagement_id>`
(e.g. `engagement:3fae...`) for the semantic/summary strategies (scoped per
engagement), and the bare human reviewer's `hitl.reviewer.principal` for the
user-preference strategy (scoped per person, across engagements) -- see
`--actor-id-scheme` below.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import boto3

from fde_mcp.logging import get_logger

log = get_logger(__name__)

# 90 days: long enough to span a typical multi-week FDE engagement plus
# slack for a delayed follow-up, short enough that this is clearly NOT
# meant as a durable store (that's the graph's job).
DEFAULT_EVENT_EXPIRY_DAYS = 90


def _semantic_strategy() -> dict[str, Any]:
    return {
        "semanticMemoryStrategy": {
            "name": "fde-semantic",
            "description": (
                "Durable engagement-scoped working notes an agent has surfaced "
                "this session -- NOT graph facts about the customer's business "
                "(those go through kg_propose/HITL, never through memory)."
            ),
            "namespaces": [],
            "namespaceTemplates": ["/strategy/{strategyId}/actor/{actorId}/session/{sessionId}"],
        }
    }


def _summary_strategy() -> dict[str, Any]:
    return {
        "summaryMemoryStrategy": {
            "name": "fde-summary",
            "description": (
                "Rolling session summaries so a long HITL-gate wait or a "
                "human-kind workflow step can resume without replaying the "
                "full transcript."
            ),
            "namespaces": [],
            "namespaceTemplates": ["/strategy/{strategyId}/actor/{actorId}/session/{sessionId}"],
        }
    }


def _user_preference_strategy() -> dict[str, Any]:
    return {
        "userPreferenceMemoryStrategy": {
            "name": "fde-user-pref",
            "description": (
                "Durable per-reviewer preferences that follow a human across "
                "engagements (e.g. hitl.reviewer.principal), never a graph fact."
            ),
            "namespaces": [],
            "namespaceTemplates": ["/strategy/{strategyId}/actor/{actorId}/session/{sessionId}"],
        }
    }


def _custom_strategy(sns_topic_arn: str, delivery_bucket: str) -> dict[str, Any]:
    """Opt-in strategy routing extraction through an operator-owned pipeline
    via `selfManagedConfiguration` -- see module docstring. Requires the
    caller to already own an SNS topic + S3 bucket wired to their own
    redaction/extraction Lambda; this script does not provision those.
    """
    return {
        "customMemoryStrategy": {
            "name": "fde-custom-self-managed",
            "description": "Operator-owned extraction pipeline for customers requiring their own PII/redaction step.",
            "namespaces": [],
            "namespaceTemplates": ["/strategy/{strategyId}/actor/{actorId}/session/{sessionId}"],
            "configuration": {
                "selfManagedConfiguration": {
                    "triggerConditions": [
                        {"messageBasedTrigger": {"messageCount": 20}},
                        {"timeBasedTrigger": {"idleSessionTimeout": 900}},
                    ],
                    "invocationConfiguration": {
                        "topicArn": sns_topic_arn,
                        "payloadDeliveryBucketName": delivery_bucket,
                    },
                }
            },
        }
    }


def create_memory(client: Any, args: argparse.Namespace) -> dict[str, Any]:
    strategies = [_semantic_strategy(), _summary_strategy(), _user_preference_strategy()]
    if args.enable_custom_strategy:
        if not (args.custom_sns_topic_arn and args.custom_delivery_bucket):
            msg = "--enable-custom-strategy requires --custom-sns-topic-arn and --custom-delivery-bucket"
            raise ValueError(msg)
        strategies.append(_custom_strategy(args.custom_sns_topic_arn, args.custom_delivery_bucket))

    response = client.create_memory(
        name=args.memory_name,
        description="FDE Platform cross-turn/cross-session agent memory (not the knowledge graph)",
        memoryExecutionRoleArn=args.execution_role_arn,
        eventExpiryDuration=args.event_expiry_days,
        memoryStrategies=strategies,
    )
    log.info(
        "memory_created",
        memory_name=args.memory_name,
        memory_id=response.get("memoryId"),
        strategies=[next(iter(s)) for s in strategies],
    )
    return response  # type: ignore[no-any-return]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--memory-name", default=os.environ.get("FDE_MEMORY_NAME", "fde-agent-memory"))
    p.add_argument("--execution-role-arn", required=True)
    p.add_argument("--region", dest="aws_region", default=os.environ.get("AWS_REGION"))
    p.add_argument("--event-expiry-days", type=int, default=DEFAULT_EVENT_EXPIRY_DAYS)
    p.add_argument("--enable-custom-strategy", action="store_true")
    p.add_argument(
        "--custom-sns-topic-arn", default=os.environ.get("FDE_MEMORY_CUSTOM_SNS_TOPIC_ARN")
    )
    p.add_argument(
        "--custom-delivery-bucket", default=os.environ.get("FDE_MEMORY_CUSTOM_DELIVERY_BUCKET")
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    client = boto3.client("bedrock-agentcore-control", region_name=args.aws_region)
    response = create_memory(client, args)
    print(
        json.dumps(
            {"memoryId": response.get("memoryId"), "status": response.get("status")},
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
