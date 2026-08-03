"""The launch record: what it says, WHEN it says it, and what it never stores.

`wf.agent_launch` (db/018) exists for one failure mode. The console launch is
synchronous, the deployed front door is an API Gateway HTTP API whose
integration times out well before `FDE_GATE_STEP_TIMEOUT_SECONDS` does, and
before this table a launch that outlived its request left nothing behind at
all -- the operator could not learn whether the agent had run, whether it had
finished, or whether launching again would duplicate the work.

So the load-bearing assertion in this file is not that a record exists. It is
`test_the_record_is_committed_before_the_dispatch_starts`: the row has to be
readable from ANOTHER connection while the agent is still running, because
that is the only window in which anybody needs it. A record written on
completion would be absent in exactly the case it was built for, and every
other test here would still pass.

The rest is the boundary. Two refusals record nothing (a caller who may not
launch, a submission that does not parse) and one records a failure (a
runtime that is not deployed) -- see `agents.launch`'s docstring for why the
line falls there. And the transcript is never stored: `_input_snapshot` keeps
a character count instead, so evidence stays in `kg.chunk` where the denial
matrix keeps it immutable rather than being copied into a second table with
a different safety story.

Nothing here has run against a live AgentCore runtime; dispatch is the same
`_FakeAgentCore` stub `test_agent_launcher.py` documents.
"""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Any

import pytest
from gate_seed import SME

from fde_gate import ui
from fde_gate.config import get_gate_settings
from fde_gate.executors import AgentExecutor, StepExecutionError
from fde_gate.forms import FIELD_PREFIX
from fde_gate.http import GateError, Request
from fde_gate.service import agents
from fde_mcp.config import get_settings

ENGAGEMENT_ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fde-engagement"

TRANSCRIPT = "Every deal over ten percent goes to discount review before it ships."


@pytest.fixture
def engagement(seed: dict[str, Any]) -> str:
    """A fresh engagement id.

    Fresh per test rather than the session's, because every assertion here
    counts rows in `wf.agent_launch` and a shared engagement would make each
    test's row count depend on which tests ran before it. Nothing in this
    file needs graph nodes -- the tasks it launches take pasted text.
    """
    return str(uuid.uuid4())


@pytest.fixture
def runtime_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FDE_RUNTIME_ARN_ENGAGEMENT", ENGAGEMENT_ARN)
    get_gate_settings.cache_clear()
    get_settings.cache_clear()


class _FakeStream:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def iter_chunks(self) -> Any:
        yield self._body


class _FakeAgentCore:
    """Stands in for the `bedrock-agentcore` client. See the module docstring."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"response": _FakeStream(b'data: {"type":"final","result":{"proposal_id":31}}\n\n')}


def _fake_executor(client: _FakeAgentCore) -> AgentExecutor:
    return AgentExecutor(client_factory=lambda: client)


def _launches(sql: Any, engagement_id: str) -> list[dict[str, Any]]:
    """Every launch record on one engagement, oldest first, read as the owner.

    Read through the `sql` fixture rather than through `list_launches` where
    the assertion is about what was STORED: a bug in the service's own read
    would otherwise hide a bug in its write.
    """
    return sql(
        """
        SELECT launch_id, engagement_id::text AS engagement_id, agent, task, principal,
               status, input, runtime_session_id, error, requested_at, completed_at
          FROM wf.agent_launch
         WHERE engagement_id = %(eng)s::uuid
         ORDER BY launch_id
        """,
        {"eng": engagement_id},
    )


async def _launch(engagement_id: str, executor: Any, *, principal: str = SME) -> dict[str, Any]:
    return await agents.launch(
        principal,
        agent="engagement",
        task="ingest_interview",
        engagement_id=engagement_id,
        form={f"{FIELD_PREFIX}material": TRANSCRIPT},
        executor=executor,
    )


# ---------------------------------------------------------------------------
# The window the record exists for
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
@pytest.mark.usefixtures("runtime_configured")
async def test_the_record_is_committed_before_the_dispatch_starts(
    engagement: str, sql: Any
) -> None:
    """The whole feature, in one assertion.

    This executor reads `wf.agent_launch` on a SEPARATE connection while it
    is standing in for the agent, which is the only way to prove the row was
    committed rather than merely written: an uncommitted INSERT is invisible
    to every other session, and every other session is what an operator's
    next page load is. Write the record after the dispatch instead and this
    is the only test in the file that fails.

    It also pins the state the row is in during that window. `running` is
    what a timed-out launch is left saying, and the console's copy is written
    around that word meaning "dispatched, nothing reported back".
    """
    seen: list[dict[str, Any]] = []

    class _ObservingExecutor:
        async def execute(self, step: Any, run: Any) -> dict[str, Any]:
            seen.extend(_launches(sql, engagement))
            return {"events": [], "result": None}

    await _launch(engagement, _ObservingExecutor())

    assert len(seen) == 1, "the launch was not visible to another session mid-dispatch"
    (mid_flight,) = seen
    assert mid_flight["status"] == "running"
    assert mid_flight["completed_at"] is None
    assert mid_flight["principal"] == SME
    assert mid_flight["agent"] == "engagement"
    assert mid_flight["task"] == "ingest_interview"
    # Stamped on the way in, so a trace can be found for a launch that never
    # came back.
    assert mid_flight["runtime_session_id"]
    assert len(mid_flight["runtime_session_id"]) >= 33, "AgentCore's verified minimum"


# ---------------------------------------------------------------------------
# How a launch ends
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
@pytest.mark.usefixtures("runtime_configured")
async def test_a_finished_launch_is_stamped_succeeded_and_returns_its_id(
    engagement: str, sql: Any
) -> None:
    result = await _launch(engagement, _fake_executor(_FakeAgentCore()))

    (record,) = _launches(sql, engagement)
    assert record["status"] == "succeeded"
    assert record["completed_at"] is not None
    assert record["error"] is None
    assert record["runtime_session_id"] == result["runtime_session_id"]
    # The id comes back so the console can name the record in its notice --
    # without it the operator is told the launch was recorded and not which.
    assert result["launch_id"] == record["launch_id"]


@pytest.mark.requires_db
async def test_an_undeployed_runtime_records_the_failure_it_refused_with(
    engagement: str, sql: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal that DOES leave a record, and the reason it is on that side
    of the line: the request was complete and valid, and what was missing was
    the deployment. A retry reproduces it exactly, so "did this ever work?"
    is a question somebody will ask about this one specifically.

    `runtime_session_id` stays null, which is the useful reading -- nothing
    was sent, so there is no trace to go looking for.
    """
    monkeypatch.delenv("FDE_RUNTIME_ARN_ENGAGEMENT", raising=False)
    get_gate_settings.cache_clear()
    get_settings.cache_clear()

    client = _FakeAgentCore()
    with pytest.raises(GateError) as caught:
        await _launch(engagement, _fake_executor(client))
    assert caught.value.status == HTTPStatus.SERVICE_UNAVAILABLE

    (record,) = _launches(sql, engagement)
    assert record["status"] == "failed"
    assert record["completed_at"] is not None
    assert record["runtime_session_id"] is None, "it never reached AgentCore"
    assert client.calls == []
    # The same sentence the operator was shown, so the page and the record do
    # not tell two versions of one story.
    assert record["error"] == {"error": caught.value.message}
    assert "FDE_RUNTIME_ARN_ENGAGEMENT" in record["error"]["error"]


@pytest.mark.requires_db
@pytest.mark.usefixtures("runtime_configured")
async def test_a_failed_dispatch_stores_the_detail_verbatim(engagement: str, sql: Any) -> None:
    """`StepExecutionError.detail` is what the runner writes onto an attempt
    and what docs/10 §2 tells an operator to read. A launch has no attempt
    row, and this record is now the only place that error survives at all --
    so it is stored whole, not summarised to the sentence the page showed.
    """

    class _Exploding:
        async def execute(self, step: Any, run: Any) -> dict[str, Any]:
            raise StepExecutionError(
                {"error": "agent invocation failed: connection reset", "runtime": "engagement"}
            )

    with pytest.raises(GateError) as caught:
        await _launch(engagement, _Exploding())
    assert caught.value.status == HTTPStatus.BAD_GATEWAY

    (record,) = _launches(sql, engagement)
    assert record["status"] == "failed"
    assert record["completed_at"] is not None
    assert record["error"] == {
        "error": "agent invocation failed: connection reset",
        "runtime": "engagement",
    }
    # The dispatch happened, so there is a session to correlate a trace with.
    assert record["runtime_session_id"]


@pytest.mark.requires_db
@pytest.mark.usefixtures("runtime_configured")
async def test_a_crash_that_is_not_a_step_failure_still_closes_the_record(
    engagement: str, sql: Any
) -> None:
    """`running` means "nobody reported back". A bug in the executor is not
    that: this process knows the launch stopped, and a row left saying
    `running` would be the difference between not knowing and not saying.
    The exception itself is re-raised untouched.
    """

    class _Buggy:
        async def execute(self, step: Any, run: Any) -> dict[str, Any]:
            raise RuntimeError("boto3 is not installed")

    with pytest.raises(RuntimeError, match="boto3 is not installed"):
        await _launch(engagement, _Buggy())

    (record,) = _launches(sql, engagement)
    assert record["status"] == "failed"
    assert record["error"] == {"error": "RuntimeError: boto3 is not installed"}


# ---------------------------------------------------------------------------
# The refusals that record nothing
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
@pytest.mark.usefixtures("runtime_configured")
async def test_someone_who_may_not_launch_leaves_no_record(engagement: str, sql: Any) -> None:
    """`principal` is an attribution, not a log line.

    Writing a row for a caller the roster just refused would make the record
    assert something untrue -- and the anonymous case below would let a
    request with no identity at all write into the table.
    """
    off_roster = f"pytest-off-{uuid.uuid4().hex[:8]}@example.com"
    for actor in (off_roster, ""):
        with pytest.raises(GateError) as caught:
            await _launch(engagement, _fake_executor(_FakeAgentCore()), principal=actor)
        assert caught.value.status == HTTPStatus.FORBIDDEN

    assert _launches(sql, engagement) == []


@pytest.mark.requires_db
@pytest.mark.usefixtures("runtime_configured")
async def test_a_submission_that_does_not_parse_leaves_no_record(engagement: str, sql: Any) -> None:
    """Nothing was requested that could have been dispatched -- the operator
    is still filling the form in, and it re-renders with what they typed
    still in it. A launch log of typos answers no question anybody has.
    """
    with pytest.raises(GateError, match="nothing for the agent to read"):
        await agents.launch(
            SME,
            agent="engagement",
            task="ingest_interview",
            engagement_id=engagement,
            form={f"{FIELD_PREFIX}material": "   "},
            executor=_fake_executor(_FakeAgentCore()),
        )

    with pytest.raises(GateError, match="Short name is required"):
        await agents.launch(
            SME,
            agent="workflow",
            task="author_workflow",
            engagement_id=engagement,
            form={
                f"{FIELD_PREFIX}root_process_key": "proc.x",
                f"{FIELD_PREFIX}title": "Untitled",
            },
            executor=_fake_executor(_FakeAgentCore()),
        )

    assert _launches(sql, engagement) == []


# ---------------------------------------------------------------------------
# What the record deliberately does not keep
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
@pytest.mark.usefixtures("runtime_configured")
async def test_the_transcript_is_counted_not_stored(engagement: str, sql: Any) -> None:
    """Evidence lives in `kg.chunk`, which is INSERT-only and which the CI
    denial matrix keeps that way. Copying a pasted transcript into a table the
    console may UPDATE would put the same text in two places with two
    different safety stories, only one of them audited.

    A COUNT rather than a truncation on purpose: a shortened transcript looks
    like the transcript and reads like a different one.
    """
    await _launch(engagement, _fake_executor(_FakeAgentCore()))

    (record,) = _launches(sql, engagement)
    assert "material" not in record["input"], "the transcript must not be stored here"
    assert record["input"]["material_chars"] == len(TRANSCRIPT)
    assert TRANSCRIPT not in str(record["input"])


@pytest.mark.requires_db
async def test_the_answers_that_are_choices_are_kept_whole(
    engagement: str, sql: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the document field is elided. Everything else IS the request --
    which process, which workflow, which severity -- and a record that dropped
    it would not say what was asked for.
    """
    monkeypatch.setenv("FDE_RUNTIME_ARN_WORKFLOW", ENGAGEMENT_ARN.replace("engagement", "workflow"))
    get_gate_settings.cache_clear()
    get_settings.cache_clear()

    await agents.launch(
        SME,
        agent="workflow",
        task="monitor_drift",
        engagement_id=engagement,
        form={f"{FIELD_PREFIX}min_severity": "high"},
        executor=_fake_executor(_FakeAgentCore()),
    )
    (record,) = _launches(sql, engagement)
    assert record["input"] == {"min_severity": "high"}


# ---------------------------------------------------------------------------
# Reading them back
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
@pytest.mark.usefixtures("runtime_configured")
async def test_launches_come_back_newest_first_and_scoped_to_one_engagement(
    engagement: str, sql: Any
) -> None:
    other = str(uuid.uuid4())
    await _launch(engagement, _fake_executor(_FakeAgentCore()))
    await _launch(engagement, _fake_executor(_FakeAgentCore()))
    await _launch(other, _fake_executor(_FakeAgentCore()))

    scoped = await agents.list_launches(engagement_id=engagement)
    assert scoped["returned"] == 2
    ids = [row["launch_id"] for row in scoped["launches"]]
    assert ids == sorted(ids, reverse=True), "most recent first, like the runs list"
    assert all(str(row["engagement_id"]) == engagement for row in scoped["launches"])

    everything = await agents.list_launches()
    assert {row["launch_id"] for row in everything["launches"]} >= set(ids)


@pytest.mark.requires_db
@pytest.mark.usefixtures("runtime_configured")
async def test_reading_the_list_is_not_gated_on_being_a_reviewer(engagement: str, sql: Any) -> None:
    """Writing a launch record is attributed and therefore refused to anyone
    off the roster; reading the list is the same class of fact as `/ui/runs`
    showing every run's `started_by`, which that page has never gated.

    Asserted because the asymmetry is deliberate. Gating the read would take
    the whole runs page away from everyone not on the roster in order to hide
    a task name from them.
    """
    await _launch(engagement, _fake_executor(_FakeAgentCore()))
    listed = await agents.list_launches(engagement_id=engagement)
    assert listed["returned"] == 1


# ---------------------------------------------------------------------------
# What the operator actually sees
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
async def test_a_launch_that_never_ran_is_visible_on_the_runs_page(
    engagement: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The walkthrough, as a test: launch with no runtime deployed, then load
    the page an operator would load next.

    This is the failure the record was built for -- the launch produced no
    proposal, so the review queue shows nothing, and before db/018 the console
    had no page that would admit the launch had ever happened.
    """
    monkeypatch.delenv("FDE_RUNTIME_ARN_ENGAGEMENT", raising=False)
    get_gate_settings.cache_clear()
    get_settings.cache_clear()

    with pytest.raises(GateError):
        await _launch(engagement, _fake_executor(_FakeAgentCore()))

    page = await ui.runs_page(Request(method="GET", path="/ui/runs", principal=SME))
    body = " ".join(str(page.body).split())

    assert "Agent launches" in body
    assert "engagement &middot; ingest_interview" in body or "engagement · ingest_interview" in body
    assert engagement in body
    assert SME in body
    # The pill, and the sentence out of the stored error envelope rather than
    # the envelope itself.
    assert "failed" in body
    assert "FDE_RUNTIME_ARN_ENGAGEMENT" in body
    assert '{"error":' not in body, "the error renders as a sentence, not as JSON"


@pytest.mark.requires_db
@pytest.mark.usefixtures("runtime_configured")
async def test_the_runs_page_says_what_a_running_launch_does_and_does_not_mean(
    engagement: str, sql: Any
) -> None:
    """A `running` row is ambiguous by construction: from the database, an
    agent still working and a request that died waiting are the same row. The
    page says both rather than picking one, because picking one would be a
    claim the console cannot support.
    """
    await _launch(engagement, _fake_executor(_FakeAgentCore()))
    sql(
        "UPDATE wf.agent_launch SET status='running', completed_at=NULL "
        "WHERE engagement_id = %(eng)s::uuid",
        {"eng": engagement},
    )

    page = await ui.runs_page(Request(method="GET", path="/ui/runs", principal=SME))
    body = " ".join(str(page.body).split())
    assert "nothing has reported back yet" in body
    assert "the request that was waiting for it is gone" in body


@pytest.mark.requires_db
@pytest.mark.usefixtures("runtime_configured")
async def test_a_run_status_filter_does_not_empty_the_launch_table(
    engagement: str, sql: Any
) -> None:
    """`?status=` takes `wf.run_status` members -- `awaiting_human`,
    `cancelled` -- and a launch can be none of them. Filtering the launches
    with it would render an empty table that reads as "no launches", which is
    a different and untrue statement.
    """
    await _launch(engagement, _fake_executor(_FakeAgentCore()))

    page = await ui.runs_page(
        Request(
            method="GET",
            path="/ui/runs",
            query={"status": "awaiting_human"},
            principal=SME,
        )
    )
    body = " ".join(str(page.body).split())
    assert "Agent launches (0)" not in body
    assert "No agent has been launched from the console yet." not in body
    assert engagement in body


@pytest.mark.requires_db
async def test_the_empty_state_says_so_rather_than_rendering_a_headless_table(
    seed: dict[str, Any], sql: Any
) -> None:
    """Only reachable on a database nobody has launched anything on, so the
    fixture clears the table rather than pretending. An empty `<table>` with
    a header row is a page that looks broken.
    """
    sql("DELETE FROM wf.agent_launch")
    page = await ui.runs_page(Request(method="GET", path="/ui/runs", principal=SME))
    body = " ".join(str(page.body).split())
    assert "Agent launches (0)" in body
    assert "No agent has been launched from the console yet." in body
