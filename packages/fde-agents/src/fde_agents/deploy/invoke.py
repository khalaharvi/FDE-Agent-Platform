"""CLI to invoke a deployed FDE agent runtime directly against the
`bedrock-agentcore` data-plane API (bypassing the Gateway -- useful for
smoke-testing a runtime right after `runtimes.py` provisions it, before
wiring it into any higher-level orchestration).

`runtimeSessionId` must be >= 33 characters (a verified AgentCore
constraint) -- a bare `uuid4()` is only 36 characters INCLUDING dashes but
some UUID string forms come in shorter after normalization, so this script
generates one defensively as `uuid4().hex + uuid4().hex` truncated to a
fixed 40 characters rather than trusting a single UUID's exact length.

The response's `response` field is a botocore `StreamingBody` (the runtime
streams SSE frames back exactly as `bedrock_agentcore.runtime.
BedrockAgentCoreApp` emits them from an async-generator entrypoint -- see
`fde_agents.{engagement,workflow,development}.agent`), so this script reads
it via `iter_chunks()` and prints each decoded SSE frame as it arrives
rather than buffering the whole response, mirroring how a real streaming
consumer would use it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Iterator
from typing import Any

import boto3

from fde_mcp.logging import get_logger

log = get_logger(__name__)


def _new_runtime_session_id() -> str:
    """>= 33 chars, per the verified AgentCore constraint this module was
    built against. 40 hex chars comfortably clears that with room to spare.
    """
    return (uuid.uuid4().hex + uuid.uuid4().hex)[:40]


def _iter_sse_events(stream: Any) -> Iterator[dict[str, Any]]:
    """Decode a botocore `StreamingBody` of `data: {json}\\n\\n` SSE frames
    into parsed JSON events, one at a time, as chunks arrive -- does not
    wait for the whole response before yielding the first event.
    """
    buffer = b""
    for chunk in stream.iter_chunks():
        buffer += chunk
        while b"\n\n" in buffer:
            frame, buffer = buffer.split(b"\n\n", 1)
            text = frame.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            yield from _parse_sse_lines(text)
    # Trailing partial frame with no closing \n\n (a stream that ended
    # mid-frame, e.g. connection cut) -- surface it rather than drop it.
    tail = buffer.decode("utf-8", errors="replace").strip()
    if tail:
        yield from _parse_sse_lines(tail)


def _parse_sse_lines(text: str) -> Iterator[dict[str, Any]]:
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[len("data:") :].strip()
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                yield {"_raw": payload}


def invoke(
    client: Any,
    agent_runtime_arn: str,
    payload: dict[str, Any],
    *,
    runtime_session_id: str | None = None,
    qualifier: str | None = None,
) -> None:
    session_id = runtime_session_id or _new_runtime_session_id()
    if len(session_id) < 33:
        msg = f"runtimeSessionId must be >= 33 chars, got {len(session_id)}"
        raise ValueError(msg)

    kwargs: dict[str, Any] = {
        "agentRuntimeArn": agent_runtime_arn,
        "payload": json.dumps(payload).encode("utf-8"),
        "runtimeSessionId": session_id,
        "contentType": "application/json",
        "accept": "text/event-stream",
    }
    if qualifier:
        kwargs["qualifier"] = qualifier

    log.info("agent_runtime_invoking", agent_runtime_arn=agent_runtime_arn, session_id=session_id)
    response = client.invoke_agent_runtime(**kwargs)

    print(f"# runtimeSessionId={session_id}", file=sys.stderr)
    stream = response["response"]
    for event in _iter_sse_events(stream):
        print(json.dumps(event))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--agent-runtime-arn", required=True)
    p.add_argument("--qualifier", default=None)
    p.add_argument("--region", dest="aws_region", default=os.environ.get("AWS_REGION"))
    p.add_argument("--runtime-session-id", default=None)
    p.add_argument("--task", required=True)
    p.add_argument("--engagement-id", required=True)
    p.add_argument("--input-json", default="{}", help="JSON-encoded task input")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        task_input = json.loads(args.input_json)
    except json.JSONDecodeError as exc:
        print(f"--input-json is not valid JSON: {exc}", file=sys.stderr)
        return 2

    payload = {"task": args.task, "engagement_id": args.engagement_id, "input": task_input}
    client = boto3.client("bedrock-agentcore", region_name=args.aws_region)
    try:
        invoke(
            client,
            args.agent_runtime_arn,
            payload,
            runtime_session_id=args.runtime_session_id,
            qualifier=args.qualifier,
        )
    except Exception:
        log.exception("agent_runtime_invoke_failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
