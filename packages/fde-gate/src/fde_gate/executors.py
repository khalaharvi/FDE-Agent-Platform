"""executors.py -- what actually happens when a workflow step runs.

Six step kinds exist in `wf.step_kind`. Five of them are executed here; the
sixth (`human`) has no executor at all, because "execute" for a human step
means parking it in the review queue and that is `wf.await_human`'s job.

An executor is deliberately small: it takes the step row and the run row,
does one thing, and returns the JSON that becomes the step's `output` and
lands in the run context under the step's `step_key`. It never opens a
transaction that spans its work (see `runner`'s two-transaction pattern), it
never decides what happens next, and it never interprets `on_failure` -- it
raises `StepExecutionError` and `wf.fail_step` decides.

Two of the five are honest about not being able to do the thing
-------------------------------------------------------------
`notify` and `sor_write` are stubs, and they are stubs that SAY SO in their
output and their error rather than returning a cheerful `{"ok": true}`.

* `notify` succeeds with `{"delivered": false, "mode": "log_only"}`. There is
  no Slack or email channel wired into this deployment. A notify step that
  returned `delivered: true` would be a lie recorded in the run context, and
  the person who eventually asks "did the rep ever get told?" would find the
  answer yes in a database that had never sent anything.
* `sor_write` always fails, with a message naming the missing adapter. No
  SoR write adapter is deployed, and the alternative to failing is silently
  not writing to the system of record while the run goes green. Author these
  steps with `on_failure = 'escalate'`: the failure then parks an
  awaiting_human attempt, an operator performs the write by hand, and
  answers `retry` or `skip`. That is a worse workflow than an automated
  write and a much better one than an imaginary write.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import TYPE_CHECKING, Any, Protocol

from fde_gate.config import get_gate_settings
from fde_gate.rows import fetchone
from fde_mcp import db
from fde_mcp.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

log = get_logger(__name__)

__all__ = [
    "AgentExecutor",
    "DecisionExecutor",
    "NotifyExecutor",
    "SorWriteExecutor",
    "StepExecutionError",
    "StepExecutor",
    "ToolExecutor",
    "default_executors",
    "resolve_args",
]


class StepExecutionError(Exception):
    """A step did not do its job. `detail` is stored verbatim on the attempt.

    Verbatim because docs/10 §2 tells the operator to read the error, and it
    is the only evidence they have. The runner passes `detail` straight into
    `wf.fail_step(p_error)` without paraphrase.
    """

    def __init__(self, detail: dict[str, Any]) -> None:
        super().__init__(str(detail.get("error", detail)))
        self.detail = detail


class StepExecutor(Protocol):
    """One step kind's execution. Structural, so a test fake needs no import."""

    async def execute(self, step: Mapping[str, Any], run: Mapping[str, Any]) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Argument templating and branch evaluation, both via Postgres jsonpath.
#
# There is exactly one expression language in this platform and it is SQL/JSON
# path. hitl.gate_policy.match_jsonpath already uses it (db/004:208), decision
# branches are authored in it, and evaluating it in Postgres rather than in
# Python means the rules a workflow author writes behave identically wherever
# they are evaluated. The alternative -- eval, or a hand-rolled mini-language,
# or jsonpath-ng as a fourth dependency -- would be a second semantics that
# agrees with the first until the day it does not.
# ---------------------------------------------------------------------------


async def resolve_args(args: Any, context: Mapping[str, Any]) -> Any:
    """Replace every `{"$ctx": "<jsonpath>"}` in `args` with its value.

    Walks the structure so a template can appear at any depth. A path that
    matches nothing resolves to null rather than raising: a tool argument
    the context does not carry yet is the tool's business to complain about,
    with a message about ITS schema, which is more use than ours.
    """
    if isinstance(args, dict):
        if set(args) == {"$ctx"} and isinstance(args["$ctx"], str):
            return await _jsonpath_first(context, args["$ctx"])
        return {key: await resolve_args(value, context) for key, value in args.items()}
    if isinstance(args, list):
        return [await resolve_args(item, context) for item in args]
    return args


async def _jsonpath_first(context: Mapping[str, Any], path: str) -> Any:
    async with (
        db.tool_transaction(role=get_gate_settings().gate.prodops_role) as conn,
        conn.cursor() as cur,
    ):
        await cur.execute(
            "SELECT jsonb_path_query_first(%(ctx)s::jsonb, %(path)s::jsonpath) AS value",
            {"ctx": json.dumps(context, default=str), "path": path},
        )
        row = await fetchone(cur)
    return None if row is None else row["value"]


async def _jsonpath_match(context: Mapping[str, Any], path: str) -> bool:
    async with (
        db.tool_transaction(role=get_gate_settings().gate.prodops_role) as conn,
        conn.cursor() as cur,
    ):
        await cur.execute(
            "SELECT jsonb_path_match(%(ctx)s::jsonb, %(path)s::jsonpath) AS matched",
            {"ctx": json.dumps(context, default=str), "path": path},
        )
        row = await fetchone(cur)
    return bool(row is not None and row["matched"])


# ---------------------------------------------------------------------------
# agent
# ---------------------------------------------------------------------------


def _new_runtime_session_id() -> str:
    """>= 33 characters, the verified AgentCore constraint.

    Same construction as `fde_agents.deploy.invoke`: two hex UUIDs truncated
    to 40, rather than one `uuid4()` whose exact string length depends on the
    form it is rendered in.
    """
    return (uuid.uuid4().hex + uuid.uuid4().hex)[:40]


def _iter_sse_events(stream: Any) -> Iterator[dict[str, Any]]:
    """Decode a botocore `StreamingBody` of `data: {json}` SSE frames.

    Lifted from `fde_agents.deploy.invoke._iter_sse_events` -- the agents
    emit exactly the frames `BedrockAgentCoreApp` produces from an
    async-generator entrypoint, so the decoder has to match it. A trailing
    partial frame is surfaced rather than dropped: a stream cut mid-frame is
    information about the failure.
    """
    buffer = b""
    for chunk in stream.iter_chunks():
        buffer += chunk
        while b"\n\n" in buffer:
            frame, buffer = buffer.split(b"\n\n", 1)
            yield from _parse_sse_lines(frame.decode("utf-8", errors="replace").strip())
    tail = buffer.decode("utf-8", errors="replace").strip()
    if tail:
        yield from _parse_sse_lines(tail)


def _parse_sse_lines(text: str) -> Iterator[dict[str, Any]]:
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[len("data:") :].strip()
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                yield {"_raw": payload}
            else:
                yield parsed if isinstance(parsed, dict) else {"value": parsed}


class AgentExecutor:
    """Invoke an AgentCore runtime and collect its streamed events.

    `step.tool_name` selects which of the three agent personas to invoke
    ("engagement" / "workflow" / "development"); the ARN comes from the
    matching `FDE_RUNTIME_ARN_*` variable. An unset ARN is a `StepExecutionError`
    naming the variable rather than a fallback to some other agent.

    The first agent step of a run stamps `wf.run.runtime_session_id` and
    `agent_runtime_arn`, and every later one reuses that session id. One run
    is one `runtimeSessionId` by design (db/006:152-154): it is the same
    value as the OTEL session id and `trn.trace_session.session_id`, so the
    run, its agent's logs, and its training trace all join on one field.

    `client_factory` exists so tests can hand in a fake with an
    `invoke_agent_runtime` method and exercise this whole path -- session
    stamping included -- without AWS.
    """

    def __init__(self, client_factory: Callable[[], Any] | None = None) -> None:
        self._client_factory = client_factory
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                import boto3  # noqa: PLC0415 -- keep boto3 off the import path of DB-only tests

                settings = get_gate_settings().gate
                self._client = boto3.client("bedrock-agentcore", region_name=settings.aws_region)
        return self._client

    async def execute(self, step: Mapping[str, Any], run: Mapping[str, Any]) -> dict[str, Any]:
        settings = get_gate_settings().gate
        agent_name = step.get("tool_name")
        arn = settings.runtime_arn_for(agent_name)
        if not arn:
            raise StepExecutionError(
                {
                    "error": (
                        f"no AgentCore runtime configured for agent {agent_name!r}; "
                        f"set FDE_RUNTIME_ARN_{str(agent_name or 'UNSET').upper()}"
                    ),
                    "step_key": step.get("step_key"),
                    "agent": agent_name,
                }
            )

        session_id = run.get("runtime_session_id") or _new_runtime_session_id()
        payload = {
            "task": step.get("instruction"),
            "engagement_id": str(run.get("engagement_id")),
            "input": await resolve_args(step.get("tool_args") or {}, run.get("context") or {}),
            "context": run.get("context") or {},
        }

        client = self._get_client()

        def _call() -> list[dict[str, Any]]:
            response = client.invoke_agent_runtime(
                agentRuntimeArn=arn,
                payload=json.dumps(payload).encode("utf-8"),
                runtimeSessionId=session_id,
                contentType="application/json",
                accept="text/event-stream",
            )
            return list(_iter_sse_events(response["response"]))

        try:
            events = await asyncio.wait_for(
                asyncio.to_thread(_call), timeout=settings.request_timeout_seconds
            )
        except TimeoutError:
            raise StepExecutionError(
                {
                    "error": "agent invocation exceeded the step execution budget",
                    "timeout_seconds": settings.request_timeout_seconds,
                    "agent_runtime_arn": arn,
                }
            ) from None
        except Exception as exc:
            raise StepExecutionError(
                {
                    "error": f"agent invocation failed: {exc}",
                    "agent_runtime_arn": arn,
                    "runtime_session_id": session_id,
                }
            ) from exc

        if run.get("runtime_session_id") != session_id:
            await _stamp_session(int(run["run_id"]), session_id, arn)

        log.info(
            "wf_agent_step_done",
            run_id=run.get("run_id"),
            step_key=step.get("step_key"),
            agent=agent_name,
            events=len(events),
        )
        return {
            "agent": agent_name,
            "runtime_session_id": session_id,
            "events": events,
            # The last frame is the agent's answer; the rest are the reasoning
            # trail. Both are kept -- the trail is what makes a failed run
            # reviewable, and it is already bounded by the runtime.
            "result": events[-1] if events else None,
        }


async def _stamp_session(run_id: int, session_id: str, arn: str) -> None:
    async with db.tool_transaction(role=get_gate_settings().gate.prodops_role) as conn:
        await conn.execute(
            """
            UPDATE wf.run
               SET runtime_session_id = coalesce(runtime_session_id, %(sid)s),
                   agent_runtime_arn  = coalesce(agent_runtime_arn, %(arn)s)
             WHERE run_id = %(rid)s
            """,
            {"rid": run_id, "sid": session_id, "arn": arn},
        )


# ---------------------------------------------------------------------------
# tool
# ---------------------------------------------------------------------------


class ToolExecutor:
    """Call one MCP tool over streamable HTTP.

    The endpoint is `FDE_MCP_URL` -- either the AgentCore Gateway or the MCP
    server directly -- with `FDE_MCP_TOKEN` as a bearer. The tool name and
    arguments come from the step, with `{"$ctx": "..."}` templates resolved
    against the run context first, so a step can pass an earlier step's
    output without the workflow author writing any code.

    A tool result flagged `isError` is a `StepExecutionError`, not a successful step
    with an error inside it: the difference decides whether `on_failure`
    runs, and a workflow whose retry policy never fires because failures
    arrive as successes is a workflow with no error handling at all.
    """

    def __init__(self, session_factory: Callable[[], Any] | None = None) -> None:
        self._session_factory = session_factory

    async def execute(self, step: Mapping[str, Any], run: Mapping[str, Any]) -> dict[str, Any]:
        settings = get_gate_settings().gate
        tool_name = step.get("tool_name")
        if not tool_name:
            raise StepExecutionError(
                {"error": "tool step has no tool_name", "step_key": step.get("step_key")}
            )
        if self._session_factory is None and not settings.mcp_url:
            raise StepExecutionError(
                {
                    "error": (
                        "FDE_MCP_URL is not configured; this deployment cannot execute tool steps"
                    ),
                    "tool": tool_name,
                    "step_key": step.get("step_key"),
                }
            )

        arguments = await resolve_args(step.get("tool_args") or {}, run.get("context") or {})
        if not isinstance(arguments, dict):
            raise StepExecutionError(
                {
                    "error": f"tool_args must resolve to an object, got {type(arguments).__name__}",
                    "tool": tool_name,
                }
            )

        try:
            result = await asyncio.wait_for(
                self._call(str(tool_name), arguments),
                timeout=settings.request_timeout_seconds,
            )
        except TimeoutError:
            raise StepExecutionError(
                {
                    "error": "tool call exceeded the step execution budget",
                    "timeout_seconds": settings.request_timeout_seconds,
                    "tool": tool_name,
                }
            ) from None
        except StepExecutionError:
            raise
        except Exception as exc:
            raise StepExecutionError(
                {"error": f"tool call failed: {exc}", "tool": tool_name}
            ) from exc

        log.info("wf_tool_step_done", run_id=run.get("run_id"), tool=tool_name)
        return {"tool": tool_name, "arguments": arguments, "result": result}

    async def _call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if self._session_factory is not None:
            session = self._session_factory()
            return _unwrap_tool_result(await session.call_tool(tool_name, arguments), tool_name)

        # Imported lazily: `mcp` pulls httpx and anyio, and the review-side
        # request paths in this Lambda have no business paying for that on a
        # cold start they will never use it on.
        from mcp import ClientSession  # noqa: PLC0415
        from mcp.client.streamable_http import streamablehttp_client  # noqa: PLC0415

        settings = get_gate_settings().gate
        headers = {"Authorization": f"Bearer {settings.mcp_token}"} if settings.mcp_token else {}
        url = settings.mcp_url or ""
        async with (
            streamablehttp_client(url, headers=headers) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            return _unwrap_tool_result(await session.call_tool(tool_name, arguments), tool_name)


def _unwrap_tool_result(result: Any, tool_name: str) -> Any:
    if getattr(result, "isError", False):
        raise StepExecutionError(
            {
                "error": f"MCP tool {tool_name} reported an error",
                "tool": tool_name,
                "detail": _content_to_json(result),
            }
        )
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    return _content_to_json(result)


def _content_to_json(result: Any) -> Any:
    """Flatten an MCP `CallToolResult` into plain JSON.

    Text blocks that happen to be JSON are parsed, because every tool in
    `fde_mcp` returns a JSON object and leaving it as a string would make
    `{"$ctx": "$.step.result.field"}` unusable downstream.
    """
    blocks = getattr(result, "content", None) or []
    out: list[Any] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if text is None:
            out.append({"type": getattr(block, "type", "unknown")})
            continue
        try:
            out.append(json.loads(text))
        except json.JSONDecodeError:
            out.append(text)
    if len(out) == 1:
        return out[0]
    return out


# ---------------------------------------------------------------------------
# decision
# ---------------------------------------------------------------------------


class DecisionExecutor:
    """Pick a branch by evaluating jsonpaths against the run context.

    `wf.step.branches` is a list, evaluated in order:

        [{"when": "$.fetch_quote.discount_pct > 20", "goto": "approve_discount"},
         {"else": "notify_rep"}]

    The chosen `step_key` comes back as `{"branch": ...}` and the runner
    passes it to `wf.complete_step(p_goto_step_key)`, which refuses a key
    that is not a step of the same workflow -- so a typo in a branch target
    is a loud failure, not a run that quietly falls through to the next
    ordinal.
    """

    async def execute(self, step: Mapping[str, Any], run: Mapping[str, Any]) -> dict[str, Any]:
        branches = step.get("branches")
        if not isinstance(branches, list) or not branches:
            raise StepExecutionError(
                {
                    "error": (
                        "decision step has no branches; expected a JSON array of "
                        '{"when": <jsonpath>, "goto": <step_key>} with an optional '
                        '{"else": <step_key>}'
                    ),
                    "step_key": step.get("step_key"),
                }
            )

        context = run.get("context") or {}
        evaluated: list[dict[str, Any]] = []
        for branch in branches:
            if not isinstance(branch, dict):
                raise StepExecutionError(
                    {
                        "error": f"branch must be an object, got {branch!r}",
                        "step_key": step.get("step_key"),
                    }
                )
            if "else" in branch:
                return {"branch": str(branch["else"]), "matched": "else", "evaluated": evaluated}
            when, goto = branch.get("when"), branch.get("goto")
            if not isinstance(when, str) or not isinstance(goto, str):
                raise StepExecutionError(
                    {
                        "error": f'branch needs string "when" and "goto", got {branch!r}',
                        "step_key": step.get("step_key"),
                    }
                )
            try:
                matched = await _jsonpath_match(context, when)
            except Exception as exc:
                raise StepExecutionError(
                    {"error": f"branch condition {when!r} is not a valid jsonpath: {exc}"}
                ) from exc
            evaluated.append({"when": when, "matched": matched})
            if matched:
                return {"branch": goto, "matched": when, "evaluated": evaluated}

        raise StepExecutionError(
            {
                "error": (
                    "no decision branch matched and no else branch was authored; "
                    'add {"else": "<step_key>"} as the last branch'
                ),
                "step_key": step.get("step_key"),
                "evaluated": evaluated,
            }
        )


# ---------------------------------------------------------------------------
# notify -- log-only, and says so
# ---------------------------------------------------------------------------


class NotifyExecutor:
    """Record that a notification WOULD have been sent. See module docstring."""

    async def execute(self, step: Mapping[str, Any], run: Mapping[str, Any]) -> dict[str, Any]:
        channel = await resolve_args(step.get("tool_args") or {}, run.get("context") or {})
        log.info(
            "wf_notify",
            run_id=run.get("run_id"),
            step_key=step.get("step_key"),
            channel=channel,
            instruction=step.get("instruction"),
        )
        return {
            "delivered": False,
            "mode": "log_only",
            "channel": channel,
            "instruction": step.get("instruction"),
            "detail": (
                "no notification channel is wired into this deployment; the message "
                "was written to the service log only"
            ),
        }


# ---------------------------------------------------------------------------
# sor_write -- always fails, honestly. See module docstring.
# ---------------------------------------------------------------------------


class SorWriteExecutor:
    """Refuse to pretend a system-of-record write happened."""

    async def execute(self, step: Mapping[str, Any], run: Mapping[str, Any]) -> dict[str, Any]:
        raise StepExecutionError(
            {
                "error": (
                    "sor_write is not executable: no system-of-record write adapter is "
                    "deployed in this environment. Author this step with "
                    "on_failure='escalate' so an operator performs the write and answers "
                    "the escalation with retry or skip."
                ),
                "adapter": step.get("sor_adapter_key"),
                "write_op": step.get("sor_write_op"),
                "step_key": step.get("step_key"),
                "run_id": run.get("run_id"),
            }
        )


def default_executors() -> dict[str, StepExecutor]:
    """The production executor table, keyed by `wf.step_kind`.

    `human` is absent on purpose: the runner checks for it before it ever
    reaches this table, and a KeyError on an unknown kind is a better outcome
    than a default executor that silently succeeds on a step nobody wrote
    code for.
    """
    return {
        "agent": AgentExecutor(),
        "tool": ToolExecutor(),
        "decision": DecisionExecutor(),
        "notify": NotifyExecutor(),
        "sor_write": SorWriteExecutor(),
    }
