"""The console agent launcher: what it offers, what it sends, what it refuses.

Four separate claims, and they fail for four different reasons:

* **The task list has not drifted.** `fde-gate` may not import `fde_agents`
  (its pyproject: "must never pull strands-agents or the AgentCore SDK"), so
  the launcher's task registry is a copy. The test below imports the real
  ones and fails when the copy falls behind -- which is the only thing
  standing between a renamed task and a dropdown that dispatches a name no
  agent recognises.
* **The pickers are filled from the graph**, not typed. A source picker
  listing registered sources and a process picker listing what
  `kg.process_flow` can actually walk are what make this page usable by
  someone who does not know a node key.
* **The payload is the one the agent entrypoint parses.** Asserted against a
  fake boto client, not AWS -- see the honesty note below.
* **Refusals are sentences.** A missing runtime ARN, a deactivated reviewer
  and a transcript submitted twice each produce something an operator can
  act on.

AgentCore dispatch is stub-tested
----------------------------------
`_FakeAgentCore` stands in for `bedrock-agentcore`'s client, the same way
`test_runner.py`'s does. Nothing here has run against a live AgentCore
runtime: the invocation is written against verified API shapes, not
validated here. What these tests DO establish is everything on this side of
that call -- which ARN is selected, what JSON is handed to it, and what the
console does with what comes back.
"""

from __future__ import annotations

import json
import re
import uuid
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from gate_seed import SME

from fde_gate import ui
from fde_gate.config import get_gate_settings
from fde_gate.executors import AgentExecutor, StepExecutionError
from fde_gate.http import GateError, Request
from fde_gate.service import agents
from fde_mcp.config import get_settings

ENGAGEMENT_ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fde-engagement"

TRANSCRIPT_A = "Every deal over ten percent goes to discount review before it ships."
TRANSCRIPT_B = "The deal desk analyst owns the exception, not the rep."


# ---------------------------------------------------------------------------
# Drift: the copied task registry against the real one
# ---------------------------------------------------------------------------

# `importorskip` rather than a plain import, matching
# `fde-sor/tests/test_gateway_schema.py`: fde-gate does not DEPEND on
# fde-agents, it only shares a workspace virtualenv with it, and CI's
# `uv sync --all-packages` is what guarantees this test runs there.
engagement_agent = pytest.importorskip(
    "fde_agents.engagement.agent",
    reason="fde-agents is not installed in this environment (run `uv sync --all-packages`)",
)
workflow_agent = pytest.importorskip(
    "fde_agents.workflow.agent",
    reason="fde-agents is not installed in this environment (run `uv sync --all-packages`)",
)
development_agent = pytest.importorskip(
    "fde_agents.development.agent",
    reason="fde-agents is not installed in this environment (run `uv sync --all-packages`)",
)


def test_task_lists_match_the_agents() -> None:
    """The copy in `service/agents.py` against `VALID_TASKS` itself.

    A task renamed in the agent and not here produces a dropdown entry the
    runtime rejects with "unknown task" AFTER the operator has waited for a
    dispatch. A task added there and not here is simply unreachable from the
    console, silently.
    """
    assert set(agents.AGENT_TASKS["engagement"]) == set(engagement_agent.VALID_TASKS)
    assert set(agents.AGENT_TASKS["workflow"]) == set(workflow_agent.VALID_TASKS)


def test_the_development_agent_is_deliberately_not_offered() -> None:
    """It authors AGENTS, from a terminal, for whoever is building one --
    not a product-operations task. Asserted rather than assumed so a fourth
    agent cannot appear in the console without someone deciding it should.
    """
    assert set(agents.AGENT_TASKS) == {"engagement", "workflow"}
    assert not set(agents.AGENT_TASKS) & set(development_agent.VALID_TASKS)


def test_every_offered_task_has_a_form_and_none_of_them_ask_for_json() -> None:
    """The feature, stated as a test. `never a raw JSON textarea` is the
    spec's wording; what that means concretely is that every field an
    operator meets is labelled and typed.
    """
    for agent, tasks in agents.AGENT_TASKS.items():
        for task in tasks:
            fields = agents.task_fields(agent, task)
            assert fields, f"{agent}/{task} has no fields"
            assert task in agents.TASK_SUMMARY, f"{agent}/{task} has no plain-language summary"
            for field in fields:
                assert field.label, f"{agent}/{task}: {field.name} has no label"
                assert "json" not in field.label.lower()
                assert "json" not in field.hint.lower()


def test_an_unknown_task_names_the_ones_that_exist() -> None:
    with pytest.raises(GateError) as caught:
        agents.task_fields("engagement", "author_workflow")
    assert "not a task the engagement agent performs" in caught.value.message
    # The whole point: the message carries the answer, not just the refusal.
    assert "ingest_interview" in caught.value.message


# ---------------------------------------------------------------------------
# Everything below needs the database
# ---------------------------------------------------------------------------


@pytest.fixture
def engagement(seed: dict[str, Any], sql: Any) -> str:
    """A fresh engagement holding one process, two activities and a source.

    A `process` node with `belongs_to` activities is what makes a key
    eligible for `kg.process_flow`, which is what the `author_workflow`
    picker offers -- so the fixture builds exactly that shape rather than
    reusing the session seed's bare activity nodes.
    """
    engagement_id = str(uuid.uuid4())
    rows = sql(
        """
        INSERT INTO kg.commit (engagement_id, status, title, authored_by, sealed_by,
                               sealed_at, content_digest)
        VALUES (%(eng)s, 'sealed', 'launcher fixture', 'pytest', 'pytest', now(),
                encode(digest('launcher fixture', 'sha256'), 'hex'))
        RETURNING commit_id
        """,
        {"eng": engagement_id},
    )
    commit_id = rows[0]["commit_id"]

    for node_key, node_type, label in (
        ("proc.quote_to_cash", "process", "Quote to Cash"),
        ("proc.dunning", "process", "Dunning"),
        ("act.create_quote", "activity", "Create Quote"),
        ("act.discount_review", "activity", "Discount Review"),
    ):
        sql(
            """
            INSERT INTO kg.node (engagement_id, node_key, node_type, label, summary, commit_id)
            VALUES (%(eng)s, %(key)s, %(type)s, %(label)s, 'launcher fixture', %(cid)s)
            """,
            {
                "eng": engagement_id,
                "key": node_key,
                "type": node_type,
                "label": label,
                "cid": commit_id,
            },
        )
    for src in ("act.create_quote", "act.discount_review"):
        sql(
            """
            INSERT INTO kg.edge (engagement_id, edge_key, edge_type, src_key, dst_key, commit_id)
            VALUES (%(eng)s, kg.make_edge_key(%(src)s, 'belongs_to', 'proc.quote_to_cash'),
                    'belongs_to', %(src)s, 'proc.quote_to_cash', %(cid)s)
            """,
            {"eng": engagement_id, "src": src, "cid": commit_id},
        )
    return engagement_id


@pytest.fixture
def registered_source(engagement: str, sql: Any) -> int:
    """A source whose two chunks are the document the agent should read."""
    rows = sql(
        """
        INSERT INTO kg.source (engagement_id, source_kind, title, captured_at, captured_by)
        VALUES (%(eng)s, 'interview', 'RevOps interview', now(), %(by)s)
        RETURNING source_id
        """,
        {"eng": engagement, "by": SME},
    )
    source_id = int(rows[0]["source_id"])
    for ordinal, content in enumerate((TRANSCRIPT_A, TRANSCRIPT_B), start=1):
        sql(
            """
            INSERT INTO kg.chunk (engagement_id, source_id, ordinal, content, anchor_keys)
            VALUES (%(eng)s, %(sid)s, %(ord)s, %(content)s, ARRAY['act.discount_review'])
            """,
            {"eng": engagement, "sid": source_id, "ord": ordinal, "content": content},
        )
    return source_id


@pytest.fixture
def deactivated_reviewer(seed: dict[str, Any], sql: Any) -> str:
    principal = f"pytest-off-{uuid.uuid4().hex[:8]}@example.com"
    sql(
        """
        INSERT INTO hitl.reviewer (principal, display_name, is_active)
        VALUES (%(p)s, 'Deactivated Reviewer', false)
        """,
        {"p": principal},
    )
    return principal


@pytest.fixture
def runtime_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FDE_RUNTIME_ARN_ENGAGEMENT", ENGAGEMENT_ARN)
    get_gate_settings.cache_clear()
    get_settings.cache_clear()


class _FakeStream:
    """A botocore `StreamingBody` of SSE frames, split mid-frame on purpose."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def iter_chunks(self) -> Any:
        midpoint = len(self._body) // 2
        yield self._body[:midpoint]
        yield self._body[midpoint:]


class _FakeAgentCore:
    """Stands in for the `bedrock-agentcore` client. See the module docstring."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        body = (
            b'data: {"type":"chunk","text":"reading the transcript"}\n\n'
            b'data: {"type":"final","result":{"proposal_id":31}}\n\n'
        )
        return {"response": _FakeStream(body)}


def _fake_executor(client: _FakeAgentCore) -> AgentExecutor:
    return AgentExecutor(client_factory=lambda: client)


# ---------------------------------------------------------------------------
# What the page offers
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
async def test_the_source_picker_lists_registered_sources_with_their_coverage(
    engagement: str, registered_source: int
) -> None:
    """Registered sources, each labelled with how much of it search can see.

    The number is the point: picking a source whose passages are all dark
    means handing the agent text nothing else can find, and the operator
    should be able to see that before they pick it rather than after.
    """
    context = await agents.launcher_context(
        SME, engagement_id=engagement, agent="engagement", task="ingest_interview"
    )
    source_field, paste_field = context["fields"]
    assert source_field.name == "source_id"
    assert paste_field.name == "material"

    values = [value for value, _ in source_field.options]
    assert values[0] == "", "the paste alternative has to be reachable from the picker"
    assert str(registered_source) in values
    (label,) = [text for value, text in source_field.options if value == str(registered_source)]
    assert "RevOps interview" in label
    assert "2/2 passages searchable" in label


@pytest.mark.requires_db
async def test_a_source_with_no_chunks_says_so_rather_than_reading_zero_of_zero(
    engagement: str, sql: Any
) -> None:
    """`kg_register_source` without `kg_ingest_chunks` leaves a real row --
    the demo seed has three. "0/0 passages searchable" describes it as a
    document search cannot reach; there is no document. Picking one IS
    refused, but the label has to explain that before the click.
    """
    sql(
        """
        INSERT INTO kg.source (engagement_id, source_kind, title, captured_at, captured_by)
        VALUES (%(eng)s, 'sop_document', 'Registered but never ingested', now(), %(by)s)
        """,
        {"eng": engagement, "by": SME},
    )
    context = await agents.launcher_context(
        SME, engagement_id=engagement, agent="engagement", task="ingest_interview"
    )
    labels = [text for _, text in context["fields"][0].options]
    (empty,) = [text for text in labels if "never ingested" in text]
    assert empty == "Registered but never ingested (sop_document, no text stored)"


@pytest.mark.requires_db
@pytest.mark.usefixtures("runtime_configured")
async def test_picking_a_source_with_no_text_is_refused_with_the_alternative(
    engagement: str, sql: Any
) -> None:
    rows = sql(
        """
        INSERT INTO kg.source (engagement_id, source_kind, title, captured_at, captured_by)
        VALUES (%(eng)s, 'sop_document', 'Registered but never ingested', now(), %(by)s)
        RETURNING source_id
        """,
        {"eng": engagement, "by": SME},
    )
    with pytest.raises(GateError) as caught:
        await agents.launch(
            SME,
            agent="engagement",
            task="ingest_interview",
            engagement_id=engagement,
            form={"source_id": str(rows[0]["source_id"]), "material": ""},
            executor=_fake_executor(_FakeAgentCore()),
        )
    assert "no stored passages" in caught.value.message
    assert "paste the text instead" in caught.value.message


@pytest.mark.requires_db
async def test_the_process_picker_offers_what_process_flow_can_walk(engagement: str) -> None:
    """Process nodes, labelled with how many activities belong to each.

    `kg.process_flow` walks a process's `belongs_to` activities, so a process
    with none of them produces an empty flow -- and `author_workflow` against
    it produces a puzzled answer rather than a workflow. The count is on the
    option so that is visible before the launch, not after it.
    """
    context = await agents.launcher_context(
        SME, engagement_id=engagement, agent="workflow", task="author_workflow"
    )
    process_field = context["fields"][0]
    assert process_field.name == "root_process_key"
    assert process_field.required
    assert dict(process_field.options) == {
        "proc.quote_to_cash": "Quote to Cash (2 activities)",
        "proc.dunning": "Dunning (0 activities)",
    }


@pytest.mark.requires_db
async def test_the_launcher_page_renders_fields_and_never_a_json_box(
    engagement: str, registered_source: int
) -> None:
    """The rendered HTML, because a form nobody can see is not a form."""
    context = await agents.launcher_context(
        SME, engagement_id=engagement, agent="engagement", task="ingest_interview"
    )
    page = ui.render(
        "agent_run.html.j2",
        principal=SME,
        is_admin=False,
        error=None,
        notice=None,
        rendered_at=0.0,
        **context,
    )
    assert 'name="source_id"' in page
    assert 'name="material"' in page
    assert "JSON" not in page
    assert "Read an interview or document" in page, "the plain-language summary"


# ---------------------------------------------------------------------------
# What it sends
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
@pytest.mark.usefixtures("runtime_configured")
async def test_dispatch_sends_the_payload_the_agent_entrypoint_parses(engagement: str) -> None:
    """The contract in `engagement/agent.py`'s module docstring:

        {"task": ..., "engagement_id": ..., "input": {...}}

    with the transcript at `input.material`, which is the only key
    `_build_task_prompt` reads for `ingest_interview` (agent.py:100).
    """
    client = _FakeAgentCore()
    result = await agents.launch(
        SME,
        agent="engagement",
        task="ingest_interview",
        engagement_id=engagement,
        form={"source_id": "", "material": TRANSCRIPT_A},
        executor=_fake_executor(client),
    )

    (call,) = client.calls
    assert call["agentRuntimeArn"] == ENGAGEMENT_ARN
    assert call["contentType"] == "application/json"
    assert call["accept"] == "text/event-stream"
    assert len(call["runtimeSessionId"]) >= 33, "AgentCore's verified minimum"

    payload = json.loads(call["payload"])
    assert payload["task"] == "ingest_interview"
    assert payload["engagement_id"] == engagement
    assert payload["input"]["material"] == TRANSCRIPT_A

    # Both SSE frames survived the mid-frame split, and the launch reports
    # the count rather than claiming an outcome it cannot see.
    assert result["events"] == 2
    assert result["result"] == {"type": "final", "result": {"proposal_id": 31}}


@pytest.mark.requires_db
@pytest.mark.usefixtures("runtime_configured")
async def test_picking_a_registered_source_sends_that_source_s_text(
    engagement: str, registered_source: int
) -> None:
    """The picker and the paste box arrive at the same place.

    Chunks are the document -- intake split it and kept no original -- so
    what the agent reads is them, rejoined, in order.
    """
    client = _FakeAgentCore()
    await agents.launch(
        SME,
        agent="engagement",
        task="ingest_interview",
        engagement_id=engagement,
        form={"source_id": str(registered_source), "material": ""},
        executor=_fake_executor(client),
    )

    payload = json.loads(client.calls[0]["payload"])
    assert payload["input"]["material"] == f"{TRANSCRIPT_A}\n\n{TRANSCRIPT_B}"
    # Provenance travels with the text: "which document was this?" is
    # otherwise unanswerable from a trace.
    assert payload["input"]["source_id"] == registered_source


@pytest.mark.requires_db
async def test_a_workflow_task_sends_its_own_labelled_fields(
    engagement: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FDE_RUNTIME_ARN_WORKFLOW", ENGAGEMENT_ARN.replace("engagement", "workflow"))
    get_gate_settings.cache_clear()
    get_settings.cache_clear()

    client = _FakeAgentCore()
    await agents.launch(
        SME,
        agent="workflow",
        task="author_workflow",
        engagement_id=engagement,
        form={
            "root_process_key": "proc.quote_to_cash",
            "title": "Quote to cash discount approval",
            "slug": "q2c-discount-approval",
        },
        executor=_fake_executor(client),
    )

    payload = json.loads(client.calls[0]["payload"])
    assert payload["task"] == "author_workflow"
    assert payload["input"] == {
        "root_process_key": "proc.quote_to_cash",
        "title": "Quote to cash discount approval",
        "slug": "q2c-discount-approval",
    }


@pytest.mark.requires_db
@pytest.mark.usefixtures("runtime_configured")
async def test_a_console_launch_does_not_touch_any_workflow_run(engagement: str, sql: Any) -> None:
    """A launch belongs to no `wf.run`, and must not pretend otherwise.

    The executor stamps `wf.run.runtime_session_id` for a workflow step; a
    console launch pre-mints the session id so that path is never taken. If
    it were, this would be an UPDATE against a run_id of None.
    """
    before = sql("SELECT count(*) AS n FROM wf.run")[0]["n"]
    await agents.launch(
        SME,
        agent="engagement",
        task="ingest_interview",
        engagement_id=engagement,
        form={"material": TRANSCRIPT_A},
        executor=_fake_executor(_FakeAgentCore()),
    )
    assert sql("SELECT count(*) AS n FROM wf.run")[0]["n"] == before


# ---------------------------------------------------------------------------
# What it refuses
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
async def test_no_runtime_arn_names_the_variable_and_the_local_alternative(
    engagement: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The message an operator gets on a deployment where the agents are not
    deployed. It has to name the fix AND the way to get the work done today,
    because "not configured" on its own leaves them stuck.
    """
    monkeypatch.delenv("FDE_RUNTIME_ARN_ENGAGEMENT", raising=False)
    get_gate_settings.cache_clear()
    get_settings.cache_clear()

    with pytest.raises(GateError) as caught:
        await agents.launch(
            SME,
            agent="engagement",
            task="ingest_interview",
            engagement_id=engagement,
            form={"material": TRANSCRIPT_A},
            executor=_fake_executor(_FakeAgentCore()),
        )

    message = caught.value.message
    assert caught.value.status == HTTPStatus.SERVICE_UNAVAILABLE
    assert "not deployed in this environment" in message
    assert "FDE_RUNTIME_ARN_ENGAGEMENT" in message
    # The dev escape hatch, spelled as a command that can be pasted.
    assert "fde-agents-local engagement --task ingest_interview" in message
    assert engagement in message


def test_the_document_the_refusal_points_at_exists() -> None:
    """The first version of that message cited packages/fde-agents/README.md,
    which does not exist. An error whose only actionable half is a dead path
    is an error that wastes the reader's time twice.
    """
    root = Path(__file__).resolve().parents[3]
    referenced = re.findall(r"\(([\w/.-]+\.md) §", agents._NO_RUNTIME)
    assert referenced, "the message is supposed to name a document"
    for path in referenced:
        assert (root / path).is_file(), f"{path} does not exist"


@pytest.mark.requires_db
@pytest.mark.usefixtures("runtime_configured")
async def test_a_deactivated_reviewer_may_not_launch_anything(
    engagement: str, deactivated_reviewer: str
) -> None:
    """Launching is attributed to a person, so it ends when the person's
    reviewer row is switched off -- the same promise `set_active` makes about
    everything else.
    """
    client = _FakeAgentCore()
    with pytest.raises(GateError) as caught:
        await agents.launch(
            deactivated_reviewer,
            agent="engagement",
            task="ingest_interview",
            engagement_id=engagement,
            form={"material": TRANSCRIPT_A},
            executor=_fake_executor(client),
        )
    assert caught.value.status == HTTPStatus.FORBIDDEN
    assert "not an active reviewer" in caught.value.message
    assert "/ui/reviewers" in caught.value.message
    assert client.calls == [], "refused before anything was dispatched"


@pytest.mark.requires_db
@pytest.mark.usefixtures("runtime_configured")
async def test_an_anonymous_caller_may_not_launch_anything(engagement: str) -> None:
    client = _FakeAgentCore()
    with pytest.raises(GateError) as caught:
        await agents.launch(
            "",
            agent="engagement",
            task="ingest_interview",
            engagement_id=engagement,
            form={"material": TRANSCRIPT_A},
            executor=_fake_executor(client),
        )
    assert caught.value.status == HTTPStatus.FORBIDDEN
    assert client.calls == []


@pytest.mark.requires_db
@pytest.mark.usefixtures("runtime_configured")
async def test_picking_a_source_and_pasting_text_is_refused_rather_than_guessed(
    engagement: str, registered_source: int
) -> None:
    """`sources._submitted_text`'s rule, in the other direction.

    Silently preferring one would mean the agent reads a document the
    operator did not think they had submitted, and nothing on any later
    screen would reveal which one it was.
    """
    with pytest.raises(GateError) as caught:
        await agents.launch(
            SME,
            agent="engagement",
            task="ingest_interview",
            engagement_id=engagement,
            form={"source_id": str(registered_source), "material": TRANSCRIPT_B},
            executor=_fake_executor(_FakeAgentCore()),
        )
    assert "picked a registered source AND pasted text" in caught.value.message


@pytest.mark.requires_db
@pytest.mark.usefixtures("runtime_configured")
async def test_submitting_neither_a_source_nor_text_says_what_to_do(engagement: str) -> None:
    with pytest.raises(GateError) as caught:
        await agents.launch(
            SME,
            agent="engagement",
            task="ingest_interview",
            engagement_id=engagement,
            form={"source_id": "", "material": "   "},
            executor=_fake_executor(_FakeAgentCore()),
        )
    assert "nothing for the agent to read" in caught.value.message
    assert "Pick a registered source, or paste the transcript" in caught.value.message


@pytest.mark.requires_db
@pytest.mark.usefixtures("runtime_configured")
async def test_a_missing_required_field_is_refused_before_any_dispatch(engagement: str) -> None:
    client = _FakeAgentCore()
    with pytest.raises(GateError) as caught:
        await agents.launch(
            SME,
            agent="workflow",
            task="author_workflow",
            engagement_id=engagement,
            form={"root_process_key": "proc.quote_to_cash", "title": "Untitled"},
            executor=_fake_executor(client),
        )
    assert caught.value.message == "Short name is required."
    assert client.calls == []


@pytest.mark.requires_db
@pytest.mark.usefixtures("runtime_configured")
async def test_a_failed_invocation_reaches_the_operator_verbatim(engagement: str) -> None:
    """`StepExecutionError.detail` is what the runner stores on an attempt and
    what docs/10 §2 tells the operator to read. The launcher has no attempt
    row to store it on, so it carries the same sentence to the page.
    """

    class _Exploding:
        async def execute(self, step: Any, run: Any) -> dict[str, Any]:
            raise StepExecutionError({"error": "agent invocation failed: connection reset"})

    with pytest.raises(GateError) as caught:
        await agents.launch(
            SME,
            agent="engagement",
            task="ingest_interview",
            engagement_id=engagement,
            form={"material": TRANSCRIPT_A},
            executor=_Exploding(),
        )
    assert caught.value.status == HTTPStatus.BAD_GATEWAY
    assert caught.value.message == "agent invocation failed: connection reset"


# ---------------------------------------------------------------------------
# Through the console handlers
# ---------------------------------------------------------------------------


def _post(engagement_id: str, **form: str) -> Request:
    return Request(
        method="POST",
        path="/ui/agents/run",
        form={"engagement_id": engagement_id, **form},
        principal=SME,
    )


@pytest.mark.requires_db
async def test_a_refused_launch_re_renders_the_form_with_the_transcript_intact(
    engagement: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The paste box holds an hour of somebody's work. A misconfigured
    runtime must not be the reason they have to produce it again -- so this
    handler re-renders rather than redirecting, exactly as the New source
    page does.
    """
    monkeypatch.delenv("FDE_RUNTIME_ARN_ENGAGEMENT", raising=False)
    get_gate_settings.cache_clear()
    get_settings.cache_clear()

    response = await ui.agent_run_post(
        _post(engagement, agent="engagement", task="ingest_interview", material=TRANSCRIPT_A)
    )
    assert response.status == HTTPStatus.SERVICE_UNAVAILABLE
    body = str(response.body)
    assert TRANSCRIPT_A in body, "the pasted transcript came back"
    assert "FDE_RUNTIME_ARN_ENGAGEMENT" in body


@pytest.mark.requires_db
async def test_a_mismatched_agent_and_task_pair_shows_the_picker_again(engagement: str) -> None:
    """One flat task dropdown makes the wrong cross-product reachable. The
    page says which tasks the chosen agent has instead of rendering nothing.
    """
    response = await ui.agent_run_page(
        Request(
            method="GET",
            path="/ui/agents/run",
            query={"engagement_id": engagement, "agent": "engagement", "task": "monitor_drift"},
            principal=SME,
        )
    )
    assert response.status == HTTPStatus.BAD_REQUEST
    assert "not a task the engagement agent performs" in str(response.body)


@pytest.mark.requires_db
async def test_a_non_reviewer_gets_a_readable_403_not_a_wall_of_braces(
    engagement: str, deactivated_reviewer: str
) -> None:
    response = await ui.agent_run_page(
        Request(method="GET", path="/ui/agents/run", principal=deactivated_reviewer)
    )
    assert response.status == HTTPStatus.FORBIDDEN
    assert response.content_type.startswith("text/html")
    assert "not an active reviewer" in str(response.body)


@pytest.mark.requires_db
@pytest.mark.usefixtures("runtime_configured")
async def test_a_successful_launch_lands_on_the_runs_page(
    engagement: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where the spec puts it, and the notice says where the OUTPUT went --
    a proposal in the review queue, which is not the page being redirected to.
    """
    monkeypatch.setattr(agents, "AgentExecutor", lambda: _fake_executor(_FakeAgentCore()))

    response = await ui.agent_run_post(
        _post(engagement, agent="engagement", task="ingest_interview", material=TRANSCRIPT_A)
    )
    assert response.status == HTTPStatus.SEE_OTHER
    location = response.headers["Location"]
    assert location.startswith("/ui/runs?notice=")
    assert "review%20queue" in location
