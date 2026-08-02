"""The runner: the two-transaction step loop, the sweeps, and each executor.

No AWS anywhere. The agent executor is driven by a fake `bedrock-agentcore`
client so the real SSE decoding and session stamping are exercised; the tool
executor is replaced outright, because what matters about it here is how the
runner treats a failure, not how MCP frames a call.
"""

from __future__ import annotations

from typing import Any

import pytest
from gate_seed import OWNER, SME

from fde_gate.config import get_gate_settings
from fde_gate.executors import (
    AgentExecutor,
    DecisionExecutor,
    NotifyExecutor,
    SorWriteExecutor,
    StepExecutionError,
)
from fde_gate.runner import advance, tick
from fde_gate.service import runs
from fde_mcp.config import get_settings

pytestmark = pytest.mark.requires_db


class _Recorder:
    """A step executor that either returns a fixed output or always fails."""

    def __init__(
        self, output: dict[str, Any] | None = None, failure: dict[str, Any] | None = None
    ) -> None:
        self.output = output if output is not None else {"ok": True}
        self.failure = failure
        self.calls: list[str] = []

    async def execute(self, step: Any, run: Any) -> dict[str, Any]:
        self.calls.append(str(step["step_key"]))
        if self.failure is not None:
            raise StepExecutionError(dict(self.failure))
        return dict(self.output)


def _every_kind(**overrides: Any) -> dict[str, Any]:
    """An executor for every step kind.

    The sweeps operate on every idle run in the database, not only the one a
    test just made, so a table with a hole in it would let an unrelated
    leftover run reach a real client.
    """
    table: dict[str, Any] = {
        kind: _Recorder({"stub": kind}) for kind in ("agent", "tool", "sor_write")
    }
    table["decision"] = DecisionExecutor()
    table["notify"] = NotifyExecutor()
    table.update(overrides)
    return table


async def _start(make_workflow: Any, steps: list[dict[str, Any]], **kwargs: Any) -> int:
    workflow_id = make_workflow(steps, **kwargs)
    started = await runs.start_run(workflow_id, SME, run_input=kwargs.pop("run_input", None))
    return int(started["run"]["run_id"])


async def test_notify_step_succeeds_log_only(make_workflow: Any) -> None:
    """A notify step must not claim to have delivered anything.

    No channel is wired into this deployment. `delivered: true` recorded in
    the run context would be a lie the person asking "did the rep ever get
    told?" would find and believe.
    """
    run_id = await _start(
        make_workflow,
        [{"step_key": "tell_rep", "kind": "notify", "instruction": "Tell the rep."}],
    )

    result = await advance(run_id, executors={"notify": NotifyExecutor()})
    assert result["status"] == "succeeded"

    detail = await runs.get_run(run_id)
    (step,) = detail["steps"]
    assert step["status"] == "succeeded"
    assert step["output"]["delivered"] is False
    assert step["output"]["mode"] == "log_only"
    assert "no notification channel is wired" in step["output"]["detail"]
    assert detail["context"]["tell_rep"]["delivered"] is False


async def test_decision_step_branches_on_context_jsonpath(make_workflow: Any) -> None:
    """Branches are Postgres jsonpath, evaluated by Postgres.

    Same engine as `hitl.gate_policy.match_jsonpath`, so a condition behaves
    identically wherever it is evaluated -- and there is no Python expression
    language to disagree with it.

    This also pins down what a branch actually DOES, which a workflow author
    has to know: `wf.advance_cursor` REPOSITIONS the cursor, it does not
    terminate. Jumping forward skips the steps in between, but whatever
    follows the target by ordinal still runs. So the two branches below are
    not symmetric, and asserting that they were would be asserting a
    behaviour the schema does not have.
    """
    workflow_id = make_workflow(
        [
            {
                "step_key": "route",
                "kind": "decision",
                "branches": [
                    {"when": "$.input.discount_pct > 20", "goto": "escalate"},
                    {"else": "auto_approve"},
                ],
            },
            {"step_key": "auto_approve", "kind": "notify"},
            {"step_key": "escalate", "kind": "notify"},
        ]
    )

    high = int(
        (await runs.start_run(workflow_id, SME, run_input={"discount_pct": 30}))["run"]["run_id"]
    )
    assert (await advance(high, executors=_every_kind()))["status"] == "succeeded"
    high_detail = await runs.get_run(high)
    assert high_detail["context"]["route"]["branch"] == "escalate"
    # The jump forward really did skip `auto_approve`.
    assert [s["step_key"] for s in high_detail["steps"]] == ["route", "escalate"]

    low = int(
        (await runs.start_run(workflow_id, SME, run_input={"discount_pct": 5}))["run"]["run_id"]
    )
    assert (await advance(low, executors=_every_kind()))["status"] == "succeeded"
    low_detail = await runs.get_run(low)
    assert low_detail["context"]["route"]["branch"] == "auto_approve"
    # ...and the else-branch, jumping to the step that was next anyway, falls
    # through to the one after it. Authoring a genuinely terminal branch means
    # making its target the last step by ordinal.
    assert [s["step_key"] for s in low_detail["steps"]] == [
        "route",
        "auto_approve",
        "escalate",
    ]


class _FakeStream:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def iter_chunks(self) -> Any:
        # Deliberately split mid-frame, so the decoder's buffering is what is
        # under test rather than a single tidy read.
        midpoint = len(self._body) // 2
        yield self._body[:midpoint]
        yield self._body[midpoint:]


class _FakeAgentCore:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        body = (
            b'data: {"type":"chunk","text":"looking at the graph"}\n\n'
            b'data: {"type":"final","result":{"activities":3}}\n\n'
        )
        return {"response": _FakeStream(body)}


async def test_agent_step_uses_fake_executor_and_stamps_session_id(
    make_workflow: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One run is one runtimeSessionId, stamped on the run by the first
    agent step.

    db/006:152-154 makes that value the join key across the agent's logs, the
    OTEL span, and `trn.trace_session` -- so it has to be written where a
    later query can find it, not held in the executor's memory.
    """
    monkeypatch.setenv("FDE_RUNTIME_ARN_WORKFLOW", "arn:aws:bedrock-agentcore:::runtime/fde-wf")
    get_gate_settings.cache_clear()
    get_settings.cache_clear()

    fake = _FakeAgentCore()
    run_id = await _start(
        make_workflow,
        [
            {
                "step_key": "map_it",
                "kind": "agent",
                "tool_name": "workflow",
                "instruction": "Map the dunning flow.",
                "tool_args": {"depth": 2},
            }
        ],
    )

    result = await advance(run_id, executors={"agent": AgentExecutor(client_factory=lambda: fake)})
    assert result["status"] == "succeeded"

    (call,) = fake.calls
    assert call["agentRuntimeArn"] == "arn:aws:bedrock-agentcore:::runtime/fde-wf"
    assert len(call["runtimeSessionId"]) >= 33, "AgentCore's verified minimum"

    detail = await runs.get_run(run_id)
    assert detail["runtime_session_id"] == call["runtimeSessionId"]
    assert detail["agent_runtime_arn"] == "arn:aws:bedrock-agentcore:::runtime/fde-wf"

    (step,) = detail["steps"]
    # Both frames survived the mid-frame chunk split, and the LAST one is the
    # answer while the earlier ones are the reviewable trail.
    assert len(step["output"]["events"]) == 2
    assert step["output"]["result"] == {"type": "final", "result": {"activities": 3}}


async def test_agent_step_without_a_configured_arn_fails_honestly(
    make_workflow: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unset ARN must not silently fall back to a configured sibling."""
    monkeypatch.delenv("FDE_RUNTIME_ARN_ENGAGEMENT", raising=False)
    monkeypatch.setenv("FDE_RUNTIME_ARN_WORKFLOW", "arn:aws:bedrock-agentcore:::runtime/fde-wf")
    get_gate_settings.cache_clear()
    get_settings.cache_clear()

    run_id = await _start(
        make_workflow,
        [
            {
                "step_key": "ask",
                "kind": "agent",
                "tool_name": "engagement",
                "on_failure": "halt",
            }
        ],
    )
    await advance(run_id, executors={"agent": AgentExecutor(client_factory=_FakeAgentCore)})

    detail = await runs.get_run(run_id)
    assert detail["status"] == "failed"
    (step,) = detail["steps"]
    assert "FDE_RUNTIME_ARN_ENGAGEMENT" in step["error"]["error"]


async def test_tool_step_failure_routes_retry_then_fails_at_max(
    make_workflow: Any,
) -> None:
    """`on_failure='retry'` is interpreted in SQL; the runner only reports.

    Three attempts, each a real `wf.begin_step`/`wf.fail_step` pair, and then
    the run fails at `FDE_GATE_MAX_STEP_ATTEMPTS`.
    """
    failing = _Recorder(failure={"error": "MCP endpoint refused the connection"})
    run_id = await _start(
        make_workflow,
        [
            {
                "step_key": "flaky",
                "kind": "tool",
                "tool_name": "kg_get_node",
                "on_failure": "retry",
            }
        ],
    )

    result = await advance(run_id, executors={"tool": failing})
    assert result["status"] == "failed"
    assert failing.calls == ["flaky", "flaky", "flaky"], "default max_step_attempts is 3"

    detail = await runs.get_run(run_id)
    assert [s["attempt"] for s in detail["steps"]] == [1, 2, 3]
    assert all(s["status"] == "failed" for s in detail["steps"])
    assert detail["error"]["error"] == "MCP endpoint refused the connection"


async def test_sor_write_fails_honestly_and_escalates(make_workflow: Any) -> None:
    """A system-of-record write that cannot happen must not report success.

    The alternative to failing is a green run that never wrote to the SoR,
    which is the failure nobody finds until the quarter closes.
    """
    run_id = await _start(
        make_workflow,
        [
            {
                "step_key": "write_back",
                "kind": "sor_write",
                "sor_adapter_key": "salesforce",
                "sor_write_op": "update_opportunity",
                "on_failure": "escalate",
            }
        ],
    )

    result = await advance(run_id, executors={"sor_write": SorWriteExecutor()})
    assert result["status"] == "awaiting_human"

    detail = await runs.get_run(run_id)
    failed = next(s for s in detail["steps"] if s["status"] == "failed")
    assert "no system-of-record write adapter is deployed" in failed["error"]["error"]
    assert failed["error"]["adapter"] == "salesforce"
    # The escalation guidance is in the error itself, where the operator is.
    assert "on_failure='escalate'" in failed["error"]["error"]

    parked = next(s for s in detail["steps"] if s["status"] == "awaiting_human")
    assert parked["attempt"] == 2
    assert parked["input"]["escalated_error"]["adapter"] == "salesforce"

    # It reaches the review queue as an escalation, not as a normal question.
    queue = await runs.awaiting_steps(OWNER)
    waiting = next(s for s in queue["awaiting_steps"] if s["run_id"] == run_id)
    assert waiting["escalated"] is True

    # An operator who did the write by hand answers `retry`, and the run
    # picks up at the same step rather than skipping it.
    answered = await runs.respond(int(parked["run_step_id"]), OWNER, "retry", response={})
    assert answered["run"]["status"] == "running"
    assert answered["run"]["current_step_id"] == parked["step_id"]


async def test_advance_returns_202_when_async_boundary_hit(make_workflow: Any) -> None:
    """A slow step is handed to an async invoke, not run inside the request.

    API Gateway allows 29 seconds; an agent step can take minutes. Crucially
    the handoff must NOT open an attempt -- if it did, the async invocation
    would find an open step and refuse to begin its own.
    """
    dispatched: list[int] = []

    async def _invoker(run_id: int) -> None:
        dispatched.append(run_id)

    run_id = await _start(
        make_workflow,
        [{"step_key": "long_call", "kind": "tool", "tool_name": "kg_search"}],
    )
    executor = _Recorder()

    result = await advance(run_id, executors={"tool": executor}, invoker=_invoker)

    assert result["status"] == "running"
    assert "asynchronously" in result["detail"]
    assert dispatched == [run_id]
    assert executor.calls == [], "the request must not have executed the step itself"

    detail = await runs.get_run(run_id)
    assert detail["steps"] == [], "no attempt may be open when the async invoke arrives"

    # And the async invocation -- which passes no invoker -- runs it inline.
    inline = await advance(run_id, executors={"tool": executor})
    assert inline["status"] == "succeeded"
    assert executor.calls == ["long_call"]


async def test_tick_times_out_overdue_step_and_advances_idle_runs(
    make_workflow: Any, sql: Any
) -> None:
    """The sweep that makes the two-transaction pattern safe.

    A process that dies between `begin_step` and `complete_step` leaves a
    `running` attempt nothing else would ever finish. `wf.timeout_steps`
    fails it at the step's own `timeout_seconds` and routes it through
    `on_failure`, so a crash self-heals. In the same pass, runs left with no
    open step are advanced -- which is also the backstop for a lost async
    self-invoke.
    """
    stranded_id = await _start(
        make_workflow,
        [
            {
                "step_key": "stranded",
                "kind": "tool",
                "tool_name": "kg_get_node",
                "on_failure": "halt",
                "timeout_seconds": 1,
            }
        ],
    )
    # Open the attempt and then walk away from it, exactly as a crashed
    # process would, and age it past its timeout.
    sql("SELECT run_step_id FROM wf.begin_step(%(rid)s)", {"rid": stranded_id})
    sql(
        "UPDATE wf.run_step SET started_at = now() - interval '1 hour' WHERE run_id = %(rid)s",
        {"rid": stranded_id},
    )

    idle_id = await _start(make_workflow, [{"step_key": "tell_rep", "kind": "notify"}])

    result = await tick(executors=_every_kind())

    assert result["timed_out"] >= 1
    advanced_ids = {envelope["run_id"] for envelope in result["advanced"]}
    assert idle_id in advanced_ids, "a run with no open step must be picked up"

    stranded = await runs.get_run(stranded_id)
    assert stranded["status"] == "failed"
    (attempt,) = stranded["steps"]
    assert attempt["status"] == "failed"
    assert attempt["error"]["error"] == "step timed out"
    assert attempt["error"]["timeout_seconds"] == 1

    assert (await runs.get_run(idle_id))["status"] == "succeeded"


async def test_tick_never_disturbs_a_step_waiting_on_a_human(make_workflow: Any, sql: Any) -> None:
    """docs/10 §2 promises the operator "the workflow will wait for you".

    A run that quietly failed while someone was at lunch is exactly the
    behaviour that teaches operators not to trust the queue, so
    `awaiting_human` attempts are never eligible for the timeout sweep no
    matter how long they have been open.
    """
    run_id = await _start(
        make_workflow,
        [
            {
                "step_key": "hold",
                "kind": "human",
                "human_prompt": "Proceed?",
                "timeout_seconds": 1,
            }
        ],
    )
    await advance(run_id)
    sql(
        "UPDATE wf.run_step SET started_at = now() - interval '30 days' WHERE run_id = %(rid)s",
        {"rid": run_id},
    )

    await tick(executors=_every_kind())

    detail = await runs.get_run(run_id)
    assert detail["status"] == "awaiting_human"
    (step,) = detail["steps"]
    assert step["status"] == "awaiting_human"
    assert step["error"] is None
